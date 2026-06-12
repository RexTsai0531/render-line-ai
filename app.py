import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

import requests
from flask import Flask, abort, jsonify, request
from supabase import create_client, Client


app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

LINE_API_BASE = "https://api.line.me"
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "data"))
MEMORY_PATH = DATA_DIR / "memory.json"
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful LINE AI assistant. Reply in Traditional Chinese. "
    "Be concise, natural, and practical. If relevant memory is provided, use it before answering. "
    "If a user has given profile facts, apply them carefully."
)
KNOWLEDGE_BASE_PATH = Path(os.getenv("KNOWLEDGE_BASE_PATH", "/var/data/knowledge_base.json"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


@dataclass
class MemoryItem:
    id: int
    text: str
    created_at: float
    kind: str = "fact"


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_memory_store() -> dict[str, list[MemoryItem]]:
    ensure_data_dir()
    if not MEMORY_PATH.exists():
        return {}
    try:
        raw = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.exception("Memory store is corrupt, starting empty")
        return {}

    store: dict[str, list[MemoryItem]] = {}
    for user_id, items in raw.items():
        store[user_id] = [MemoryItem(**item) for item in items]
    return store


def save_memory_store(store: dict[str, list[MemoryItem]]) -> None:
    ensure_data_dir()
    raw: dict[str, list[dict[str, Any]]] = {
        user_id: [asdict(item) for item in items] for user_id, items in store.items()
    }
    MEMORY_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


MEMORY_LOCK = threading.Lock()
MEMORY_STORE = load_memory_store()


def load_knowledge_base() -> list[dict[str, Any]]:
    if not KNOWLEDGE_BASE_PATH.exists():
        logging.warning("Knowledge base not found at %s", KNOWLEDGE_BASE_PATH)
        return []
    try:
        raw = json.loads(KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.exception("Knowledge base is corrupt")
        return []
    return raw.get("entries", [])


KNOWLEDGE_BASE = load_knowledge_base()


def get_supabase_client() -> Client | None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def verify_line_signature(body: bytes, signature: str) -> bool:
    channel_secret = require_env("LINE_CHANNEL_SECRET")
    digest = hmac.new(
        channel_secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def normalize_text(text: str) -> str:
    return " ".join(text.lower().split())


def score_entry(entry: dict[str, Any], query: str) -> int:
    query_norm = normalize_text(query)
    tags = [normalize_text(tag) for tag in entry.get("tags", [])]
    text = normalize_text(entry.get("text", ""))
    score = 0
    for tag in tags:
        if tag and tag in query_norm:
            score += 4
    for term in query_norm.split():
        if term and term in text:
            score += 1
    return score


def retrieve_knowledge(query: str, limit: int = 4) -> list[dict[str, Any]]:
    client = get_supabase_client()
    if client is not None:
        try:
            result = client.rpc(
                "search_knowledge_chunks",
                {"query_text": query, "match_limit": limit},
            ).execute()
            data = result.data or []
            if data:
                return [
                    {
                        "id": row.get("id"),
                        "title": row.get("title", ""),
                        "text": row.get("content", ""),
                        "tags": row.get("tags", []),
                    }
                    for row in data
                ]
        except Exception:
            logging.exception("Supabase knowledge retrieval failed, falling back to local knowledge base")

    if not KNOWLEDGE_BASE:
        return []
    ranked = sorted(
        ((score_entry(entry, query), entry) for entry in KNOWLEDGE_BASE),
        key=lambda item: item[0],
        reverse=True,
    )
    selected = [entry for score, entry in ranked if score > 0][:limit]
    if not selected:
        selected = KNOWLEDGE_BASE[:limit]
    return selected


def build_memory_context(user_id: Optional[str], query: str) -> str:
    if not user_id:
        return ""

    with MEMORY_LOCK:
        items = list(MEMORY_STORE.get(user_id, []))

    if not items:
        return ""

    query_terms = set(normalize_text(query).split())
    scored: list[tuple[int, MemoryItem]] = []
    for item in items:
        item_terms = set(normalize_text(item.text).split())
        score = len(query_terms & item_terms)
        if score > 0:
            scored.append((score, item))

    selected = [item for _, item in sorted(scored, key=lambda pair: (-pair[0], -pair[1].created_at))[:5]]
    if not selected:
        selected = items[-3:]

    lines = ["Relevant memory:"]
    for item in selected:
        lines.append(f"- {item.text}")
    return "\n".join(lines)


def build_knowledge_context(query: str) -> str:
    entries = retrieve_knowledge(query)
    if not entries:
        return ""
    lines = ["RAG knowledge base:"]
    for entry in entries:
        title = entry.get("title", "")
        text = entry.get("text", "")
        if title:
            lines.append(f"- {title}: {text}")
        else:
            lines.append(f"- {text}")
    return "\n".join(lines)


def build_guardrails() -> str:
    return (
        "Follow these rules strictly:\n"
        "1. Identify yourself as AI客服 when appropriate.\n"
        "2. If the user asks for age/password verification, use the age-gate flow from the knowledge base.\n"
        "3. If the information is missing or uncertain, do not invent answers.\n"
        "4. If the answer is not in the knowledge base, ask a clarifying question or request human follow-up.\n"
        "5. Keep tone professional, warm, and concise."
    )


def memory_summary(user_id: Optional[str]) -> str:
    if not user_id:
        return "No user memory available."
    with MEMORY_LOCK:
        items = list(MEMORY_STORE.get(user_id, []))
    if not items:
        return "No stored memories."
    lines = ["Stored memories:"]
    for item in items[-10:]:
        lines.append(f"- [{item.id}] {item.text}")
    return "\n".join(lines)


def add_memory(user_id: str, text: str, kind: str = "fact") -> MemoryItem:
    with MEMORY_LOCK:
        items = MEMORY_STORE.setdefault(user_id, [])
        next_id = (items[-1].id + 1) if items else 1
        item = MemoryItem(id=next_id, text=text.strip(), created_at=time.time(), kind=kind)
        items.append(item)
        save_memory_store(MEMORY_STORE)
        return item


def forget_memory(user_id: str, target: str) -> int:
    with MEMORY_LOCK:
        items = MEMORY_STORE.get(user_id, [])
        if not items:
            return 0
        before = len(items)
        if target.isdigit():
            target_id = int(target)
            items[:] = [item for item in items if item.id != target_id]
        else:
            needle = normalize_text(target)
            items[:] = [item for item in items if needle not in normalize_text(item.text)]
        if items:
            MEMORY_STORE[user_id] = items
        else:
            MEMORY_STORE.pop(user_id, None)
        save_memory_store(MEMORY_STORE)
        return before - len(items)


def clear_memories(user_id: str) -> None:
    with MEMORY_LOCK:
        MEMORY_STORE.pop(user_id, None)
        save_memory_store(MEMORY_STORE)


def reply_to_line(reply_token: str, text: str) -> None:
    channel_access_token = require_env("LINE_CHANNEL_ACCESS_TOKEN")
    response = requests.post(
        f"{LINE_API_BASE}/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
        },
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text[:5000]}]},
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
        json={"to": user_id, "messages": [{"type": "text", "text": text[:5000]}]},
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


def reply_to_line_with_image(reply_token: str, image_bytes: bytes, mime_type: str, prompt: str) -> None:
    answer = ask_openai(prompt, image_bytes=image_bytes, image_mime_type=mime_type)
    reply_to_line(reply_token, answer)


def extract_user_text(event: dict) -> Optional[str]:
    message = event.get("message", {})
    if event.get("type") != "message":
        return None
    if message.get("type") != "text":
        return None
    return message.get("text")


def extract_message_type(event: dict) -> Optional[str]:
    message = event.get("message", {})
    if event.get("type") != "message":
        return None
    return message.get("type")


def download_line_content(message_id: str) -> tuple[bytes, str]:
    channel_access_token = require_env("LINE_CHANNEL_ACCESS_TOKEN")
    response = requests.get(
        f"{LINE_API_BASE}/v2/bot/message/{message_id}/content",
        headers={"Authorization": f"Bearer {channel_access_token}"},
        timeout=30,
    )
    response.raise_for_status()
    mime_type = response.headers.get("Content-Type", "application/octet-stream")
    return response.content, mime_type


def to_data_url(image_bytes: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"


def ask_openai(prompt: str, *, image_bytes: bytes | None = None, image_mime_type: str | None = None) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "Missing OPENAI_API_KEY in Render environment variables."

    api_base = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    model = os.getenv("OPENAI_MODEL", "meta/llama-3.1-8b-instruct")
    vision_model = os.getenv("OPENAI_VISION_MODEL", model)
    request_timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))
    max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "1024"))
    temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
    top_p = float(os.getenv("OPENAI_TOP_P", "0.7"))
    system_prompt = os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)

    selected_model = vision_model if image_bytes else model
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": f"{system_prompt}\n\n{build_guardrails()}",
        }
    ]

    if image_bytes and image_mime_type:
        user_content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": to_data_url(image_bytes, image_mime_type)}},
        ]
    else:
        user_content = prompt

    messages.append({"role": "user", "content": user_content})

    payload = {
        "model": selected_model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
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


def handle_command(text: str, reply_token: str, user_id: Optional[str]) -> bool:
    normalized = text.strip().lower()

    if normalized in {"help", "/help", "說明", "說明一下"}:
        reply_to_line(
            reply_token,
            "可用指令：\n"
            "- help: 顯示指令\n"
            "- reset: 清除你的記憶庫\n"
            "- remember <內容>: 加入一條記憶\n"
            "- memories: 查看目前記憶\n"
            "- forget <id/關鍵字>: 刪除某條記憶\n"
            "- image <說明>: 對圖片提問",
        )
        return True

    if normalized in {"reset", "/reset", "清除", "重置"}:
        if user_id:
            clear_memories(user_id)
        reply_to_line(reply_token, "已清除你的記憶庫。")
        return True

    if normalized.startswith("remember "):
        if not user_id:
            reply_to_line(reply_token, "目前無法建立記憶，因為找不到 userId。")
            return True
        item = add_memory(user_id, text.split(" ", 1)[1])
        reply_to_line(reply_token, f"已記住這條資料 #{item.id}。")
        return True

    if normalized in {"memories", "/memories"}:
        if not user_id:
            reply_to_line(reply_token, "目前沒有可讀取的個人記憶。")
            return True
        reply_to_line(reply_token, memory_summary(user_id))
        return True

    if normalized.startswith("forget "):
        if not user_id:
            reply_to_line(reply_token, "目前無法刪除記憶，因為找不到 userId。")
            return True
        target = text.split(" ", 1)[1].strip()
        removed = forget_memory(user_id, target)
        reply_to_line(reply_token, f"已刪除 {removed} 筆記憶。")
        return True

    return False


def summarize_for_prompt(user_id: Optional[str], text: str) -> str:
    memory_context = build_memory_context(user_id, text)
    knowledge_context = build_knowledge_context(text)
    parts = [part for part in [knowledge_context, memory_context] if part]
    if not parts:
        return text
    return "\n\n".join(parts + [f"User message:\n{text}"])


def handle_message(event: dict) -> None:
    reply_token = event.get("replyToken")
    if not reply_token:
        return

    user_id = (event.get("source") or {}).get("userId")
    message_type = extract_message_type(event)

    if message_type == "text":
        text = extract_user_text(event) or ""
        try:
            if handle_command(text, reply_token, user_id):
                return
        except requests.RequestException:
            logging.exception("Failed to handle command")
            return

        query = summarize_for_prompt(user_id, text)
        try:
            answer = ask_openai(query)
        except requests.RequestException as exc:
            answer = f"AI service temporarily unavailable: {exc.__class__.__name__}"

        try:
            if user_id:
                push_to_line(user_id, answer)
            else:
                reply_to_line(reply_token, answer)
        except requests.RequestException:
            logging.exception("Failed to send final answer")
        return

    if message_type == "image":
        message = event.get("message", {})
        message_id = message.get("id")
        if not message_id:
            reply_to_line(reply_token, "I could not read that image message.")
            return

        try:
            image_bytes, mime_type = download_line_content(message_id)
            prompt = "Please inspect this image carefully and explain what you see in Traditional Chinese."
            answer = ask_openai(prompt, image_bytes=image_bytes, image_mime_type=mime_type)
        except requests.RequestException as exc:
            answer = f"Image analysis temporarily unavailable: {exc.__class__.__name__}"

        try:
            if user_id:
                push_to_line(user_id, answer)
            else:
                reply_to_line(reply_token, answer)
        except requests.RequestException:
            logging.exception("Failed to send image answer")
        return

    try:
        reply_to_line(reply_token, "Only text and image messages are supported.")
    except requests.RequestException:
        logging.exception("Failed to send unsupported message warning")


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
