import base64
import hashlib
import hmac
import json
import os
from typing import Optional

import requests
from flask import Flask, abort, jsonify, request


app = Flask(__name__)


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
        return "目前沒有設定 OPENAI_API_KEY，請先完成 Render 環境變數設定。"

    api_base = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    system_prompt = os.getenv(
        "SYSTEM_PROMPT",
        "你是一個親切、簡潔、實用的 LINE AI 助理。請用繁體中文回答，內容要精準、可直接執行。"
    )

    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": system_prompt,
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompt,
                    }
                ],
            },
        ],
    }

    response = requests.post(
        f"{api_base.rstrip('/')}/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    text = data.get("output_text")
    if text:
        return text.strip()

    output = data.get("output", [])
    for item in output:
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                value = content.get("text") or content.get("value")
                if value:
                    return str(value).strip()

    return "我剛剛有收到訊息，但沒有拿到可讀取的 AI 回覆。"


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
    response.raise_for_status()


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
            reply_to_line(reply_token, "目前我只會處理文字訊息。")
            continue

        try:
            answer = ask_openai(text)
        except requests.RequestException as exc:
            answer = f"AI 服務暫時不可用：{exc.__class__.__name__}"

        try:
            reply_to_line(reply_token, answer)
        except requests.RequestException:
            continue

    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
