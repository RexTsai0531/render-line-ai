import base64
import hashlib
import hmac
import logging
import os
from typing import Optional

import requests
from flask import Flask, abort, jsonify, request


app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

LINE_API_BASE = "https://api.line.me"


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def verify_line_signature(body: bytes, signature: str) -> bool:
    channel_secret = require_env("LINE_CHANNEL_SECRET")
    digest = hmac.new(
        channel_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def ask_openai(prompt: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "Missing OPENAI_API_KEY in Render environment variables."

    api_base = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    system_prompt = os.getenv(
        "SYSTEM_PROMPT",
        "You are a helpful LINE AI assistant. Reply in Traditional Chinese and be concise.",
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    }

    response = requests.post(
        f"{api_base.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError:
        logging.exception(
            "OpenAI-compatible API error status=%s body=%s",
            response.status_code,
            response.text,
        )
        raise

    data = response.json()

    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if content:
            return str(content).strip()

    logging.warning("No text returned from OpenAI-compatible API response: %s", data)
    return "AI returned an empty response."


def reply_to_line(reply_token: str, text: str) -> None:
    channel_access_token = require_env("LINE_CHANNEL_ACCESS_TOKEN")
    response = requests.post(
        f"{LINE_API_BASE}/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        },
        json={
            "replyToken": reply_token,
            "messages": [
                {
                    "type": "text",
                    "text": text[:5000],
                }
            ],
        },
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError:
        logging.exception(
            "LINE reply API error status=%s body=%s",
            response.status_code,
            response.text,
        )
        raise


def extract_user_text(event: dict) -> Optional[str]:
    message = event.get("message", {})
    if event.get("type") != "message":
        return None
    if message.get("type") != "text":
        return None
    return message.get("text")


@app.get("/")
def healthcheck():
    return jsonify({"status": "ok"})


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return jsonify({"ok": True, "method": "GET"})

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not signature or not verify_line_signature(body, signature):
        abort(400, description="Invalid LINE signature")

    payload = request.get_json(silent=True) or {}
    events = payload.get("events", [])

    for event in events:
        reply_token = event.get("replyToken")
        if not reply_token:
            continue

        text = extract_user_text(event)
        if not text:
            reply_to_line(reply_token, "Only text messages are supported.")
            continue

        try:
            answer = ask_openai(text)
        except requests.RequestException as exc:
            answer = f"AI service temporarily unavailable: {exc.__class__.__name__}"

        try:
            reply_to_line(reply_token, answer)
        except requests.RequestException:
            continue

    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
