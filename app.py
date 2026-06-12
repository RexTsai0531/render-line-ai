import base64
import hashlib
import hmac
import logging
import os
import threading
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
    model = os.getenv("OPENAI_MODEL", "llama-3.1-8b-instruct")
    request_timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))
    max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "256"))
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
        "max_tokens": max_tokens,
    }

    response = requests.post(
        f"{api_base.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=request_timeout,
    )
    try:
        response.raise_for_status()
    except requests.ReadTimeout:
        logging.exception("OpenAI-compatible API request timed out after %s seconds", request_timeout)
        raise
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
            "messages": [{"type": "text", "text": text[:5000]}],
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


def push_to_line(user_id: str, text: str) -> None:
    channel_access_token = require_env("LINE_CHANNEL_ACCESS_TOKEN")
    response = requests.post(
        f"{LINE_API_BASE}/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        },
        json={
            "to": user_id,
            "messages": [{"type": "text", "text": text[:5000]}],
        },
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError:
        logging.exception(
            "LINE push API error status=%s body=%s",
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


def handle_message(event: dict) -> None:
    reply_token = event.get("replyToken")
    if not reply_token:
        return

    text = extract_user_text(event)
    if not text:
        try:
            reply_to_line(reply_token, "Only text messages are supported.")
        except requests.RequestException:
            logging.exception("Failed to send non-text warning")
        return

    user_id = (event.get("source") or {}).get("userId")

    try:
        reply_to_line(reply_token, "收到，正在思考中...")
    except requests.RequestException:
        logging.exception("Failed to send immediate acknowledgement")

    try:
        answer = ask_openai(text)
    except requests.RequestException as exc:
        answer = f"AI service temporarily unavailable: {exc.__class__.__name__}"

    try:
        if user_id:
            push_to_line(user_id, answer)
        else:
            logging.warning("No userId available for push message")
    except requests.RequestException:
        logging.exception("Failed to push final answer")


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
        threading.Thread(target=handle_message, args=(event,), daemon=True).start()

    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
