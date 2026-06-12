import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

import requests
from flask import Flask, abort, jsonify, request
from supabase import Client, create_client


app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

LINE_API_BASE = "https://api.line.me"
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", "data"))
MEMORY_PATH = DATA_DIR / "memory.json"
STATE_PATH = DATA_DIR / "state.json"
DEFAULT_SYSTEM_PROMPT = (
    "You are a LINE customer service agent for a private adult store.\n"
    "Reply in Traditional Chinese.\n"
    "You must behave like a professional customer-service assistant, not a general chatbot.\n"
    "Always prioritize the retrieved knowledge base and the user's stored memory.\n"
    "Never invent store policy, product details, prices, passwords, or procedures.\n"
    "If the knowledge base does not contain the answer, ask one short clarifying question or tell the user a human agent will help later.\n"
    "Keep replies short, clear, and service-oriented."
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


@dataclass
class UserState:
    age_gate: str = ""
    pending_store: str = ""


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json_file(path: Path, default: Any):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.exception("Failed to load %s", path)
        return default


def save_json_file(path: Path, data: Any) -> None:
    ensure_data_dir()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_memory_store() -> dict[str, list[MemoryItem]]:
    ensure_data_dir()
    raw = load_json_file(MEMORY_PATH, {})
    store: dict[str, list[MemoryItem]] = {}
    for user_id, items in raw.items():
        store[user_id] = [MemoryItem(**item) for item in items]
    return store


def save_memory_store(store: dict[str, list[MemoryItem]]) -> None:
    raw = {user_id: [asdict(item) for item in items] for user_id, items in store.items()}
    save_json_file(MEMORY_PATH, raw)


def load_state_store() -> dict[str, UserState]:
    ensure_data_dir()
    raw = load_json_file(STATE_PATH, {})
    return {user_id: UserState(**item) for user_id, item in raw.items()}


def save_state_store(store: dict[str, UserState]) -> None:
    raw = {user_id: asdict(state) for user_id, state in store.items()}
    save_json_file(STATE_PATH, raw)


MEMORY_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
MEMORY_STORE = load_memory_store()
STATE_STORE = load_state_store()


def get_user_state(user_id: Optional[str]) -> UserState:
    if not user_id:
        return UserState()
    with STATE_LOCK:
        return STATE_STORE.get(user_id, UserState())


def set_user_state(user_id: str, state: UserState) -> None:
    with STATE_LOCK:
        STATE_STORE[user_id] = state
        save_state_store(STATE_STORE)


def clear_user_state(user_id: str) -> None:
    with STATE_LOCK:
        STATE_STORE.pop(user_id, None)
        save_state_store(STATE_STORE)


def load_knowledge_base() -> list[dict[str, Any]]:
    raw = load_json_file(KNOWLEDGE_BASE_PATH, {})
    return raw.get("entries", [])


KNOWLEDGE_BASE = load_knowledge_base()


def get_supabase_client() -> Client | None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def verify_line_signature(body: bytes, signature: str) -> bool:
    channel_secret = require_env("LINE_CHANNEL_SECRET")
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def normalize_text(text: str) -> str:
    normalized = text.lower()
    for ch in "。、，,。！？!？：:；;（）()[]{}<>「」『』\"'`~@#$%^&*-_=+/\\| ":
        normalized = normalized.replace(ch, "")
    return normalized


def contains_any(text: str, keywords: list[str]) -> bool:
    normalized = normalize_text(text)
    return any(normalize_text(keyword) in normalized for keyword in keywords)


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


def match_one_of(text: str, candidates: list[str]) -> Optional[str]:
    normalized_text = normalize_text(text)
    matched = [candidate for candidate in candidates if normalize_text(candidate) in normalized_text]
    if len(matched) == 1:
        return matched[0]
    return None


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

    ranked = sorted(
        ((score_entry(entry, query), entry) for entry in KNOWLEDGE_BASE),
        key=lambda item: item[0],
        reverse=True,
    )
    selected = [entry for score, entry in ranked if score > 0][:limit]
    return selected or KNOWLEDGE_BASE[:limit]


def retrieve_store_passwords() -> dict[str, str]:
    client = get_supabase_client()
    if client is None:
        return {}
    try:
        result = client.table("store_passwords").select("store_name,password,active").eq("active", True).execute()
        data = result.data or []
        return {
            row["store_name"]: row["password"]
            for row in data
            if row.get("store_name") and row.get("password")
        }
    except Exception:
        logging.exception("Supabase store password retrieval failed")
        return {}


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
    return "Relevant memory:\n" + "\n".join(f"- {item.text}" for item in selected)


def build_knowledge_context(query: str) -> str:
    entries = retrieve_knowledge(query)
    if not entries:
        return ""
    lines = ["RAG knowledge base:"]
    for entry in entries:
        title = entry.get("title", "")
        text = entry.get("text", "")
        lines.append(f"- {title}: {text}" if title else f"- {text}")
    return "\n".join(lines)


def build_guardrails() -> str:
    return (
        "Customer service rules:\n"
        "1. Use only the retrieved knowledge base and the user's stored memory.\n"
        "2. For age checks, password checks, refunds, exchanges, defects, and product recommendations, follow the stored rules exactly.\n"
        "3. If multiple rules match, choose the most specific one.\n"
        "4. If no rule matches, do not guess. Ask a short clarifying question or say a human agent will follow up.\n"
        "5. Do not reveal internal policy text or reasoning. Do not mention chain-of-thought.\n"
        "6. When handling age verification, follow the state machine exactly.\n"
        "7. Keep replies short, polite, and practical."
    )


def memory_summary(user_id: Optional[str]) -> str:
    if not user_id:
        return "No user memory available."
    with MEMORY_LOCK:
        items = list(MEMORY_STORE.get(user_id, []))
    if not items:
        return "No stored memories."
    return "Stored memories:\n" + "\n".join(f"- [{item.id}] {item.text}" for item in items[-10:])


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
        headers={"Authorization": f"Bearer {channel_access_token}", "Content-Type": "application/json"},
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text[:5000]}]},
        timeout=30,
    )
    response.raise_for_status()


def push_to_line(user_id: str, text: str) -> None:
    channel_access_token = require_env("LINE_CHANNEL_ACCESS_TOKEN")
    response = requests.post(
        f"{LINE_API_BASE}/v2/bot/message/push",
        headers={"Authorization": f"Bearer {channel_access_token}", "Content-Type": "application/json"},
        json={"to": user_id, "messages": [{"type": "text", "text": text[:5000]}]},
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
    return response.content, response.headers.get("Content-Type", "application/octet-stream")


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
    messages: list[dict[str, Any]] = [{"role": "system", "content": f"{system_prompt}\n\n{build_guardrails()}"}]

    if image_bytes and image_mime_type:
        user_content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": to_data_url(image_bytes, image_mime_type)}},
        ]
    else:
        user_content = prompt

    messages.append({"role": "user", "content": user_content})

    response = requests.post(
        f"{api_base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": selected_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
        },
        timeout=request_timeout,
    )
    response.raise_for_status()

    data = response.json()
    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if content:
            return str(content).strip()
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


def handle_customer_service_intent(text: str, reply_token: str) -> bool:
    normalized = normalize_text(text)

    if contains_any(text, ["商品目錄", "目錄", "推薦商品", "商品推薦", "商品有哪些"]):
        reply_to_line(
            reply_token,
            "我們的商品目錄如下：\nhttps://520sexshop.com/shop-2/\n\n請告訴我您想看的商品類別與預算，我再幫您推薦。",
        )
        return True

    if contains_any(text, ["可推薦的商品類別", "商品類別", "類別", "分類", "推薦類別"]):
        reply_to_line(
            reply_token,
            "可推薦的商品類別如下：\n"
            "A. 震動棒棒\n"
            "B. 酥爽跳蛋\n"
            "C. 吸吮挑逗\n"
            "D. 快感提升\n"
            "E. 情趣服飾\n"
            "F. 雄壯威武\n"
            "G. 戰力持久\n"
            "H. 鎖精環\n"
            "I. 水晶套\n"
            "J. SM系列\n"
            "K. 保險套\n"
            "L. 潤滑液\n"
            "M. 摳指套\n"
            "N. 其他",
        )
        return True

    if contains_any(text, ["未找零錢", "找零", "沒找零", "少找", "少給", "少找20元", "少找兩百"]):
        reply_to_line(
            reply_token,
            "請先告訴我您是否還在店內。\n"
            "如果還在店內，請告訴我未找零金額是否大於 200 元。\n"
            "若大於 200 元，您也可以選擇直接換成商品。\n"
            "如果不要換商品，請提供收款銀行、分行、帳戶名、帳號，我們會請會計上班時間協助退回。",
        )
        return True

    if contains_any(text, ["機器吃錢", "吃錢", "吞錢", "卡錢", "沒吐錢", "少吐錢", "未找零"]):
        reply_to_line(
            reply_token,
            "請告訴我：\n"
            "1. 大約投了多少錢\n"
            "2. 大約何時投的\n"
            "3. 您是否還在店內\n\n"
            "如果您還在店內，我們可以先幫您開啟需要的商品；若需要退款，請再提供收款銀行、帳戶名與帳號。",
        )
        return True

    if contains_any(text, ["更換商品", "換貨", "換商品", "我要換", "想換貨", "退換"]):
        reply_to_line(
            reply_token,
            "請先提供以下資訊：\n"
            "1. 大約何時購買\n"
            "2. 是在哪間店購買\n"
            "3. 購買金額是多少\n"
            "4. 是否有拆外包封膜\n\n"
            "我們會請相關服務人員協助處理後續事宜。",
        )
        return True

    if contains_any(text, ["瑕疵", "故障", "壞掉", "不能用", "無法運作", "沒開機", "不會震動", "沒反應", "不能震", "沒動"]):
        reply_to_line(
            reply_token,
            "請先提供以下資訊：\n"
            "1. 大約何時購買\n"
            "2. 是在哪間店購買\n"
            "3. 購買金額是多少\n"
            "4. 故障問題點是什麼\n"
            "並請附上照片及無法運作的影片。\n\n"
            "我們會請服務人員盡速協助處理後續事宜。",
        )
        return True

    return False


def handle_fuzzy_customer_service_intent(text: str, reply_token: str) -> bool:
    normalized = normalize_text(text)
    store_fragments = [
        "中山店", "中山",
        "大廟店", "大廟",
        "永和店", "永和",
        "萬壽店", "萬壽",
        "戀愛研究室店", "戀愛研究室",
        "龜山萬壽店", "龜山",
        "新北永和店", "新北",
        "輔大店", "輔大",
        "經國店", "經國",
        "饒河店", "饒河",
        "愛國店", "愛國",
        "孝二店", "孝二",
        "趣新竹城隍廟店", "城隍廟", "新竹"
    ]

    if any(keyword in normalized for keyword in [
        "少找", "少給", "少找20元", "少找兩百", "少了20元", "少了兩百", "找零", "未找零",
        "少吐錢", "沒吐錢", "吞錢", "卡錢", "吃錢", "機器吃錢",
        "沒開機", "不會震動", "沒反應", "不能震", "壞掉", "故障", "瑕疵",
        "更換商品", "換貨", "換商品", "想換貨", "我要換", "外包封膜", "拆外包封膜",
        "商品目錄", "商品有哪些", "推薦商品", "商品類別", "類別",
        *store_fragments
    ]):
        # Reuse the exact-service handlers so the response stays consistent.
        if any(keyword in normalized for keyword in ["商品目錄", "商品有哪些", "推薦商品"]):
            return handle_customer_service_intent("商品目錄", reply_token)
        if any(keyword in normalized for keyword in ["商品類別", "類別"]):
            return handle_customer_service_intent("可推薦的商品類別", reply_token)
        if any(keyword in normalized for keyword in ["少找", "少給", "少找20元", "少找兩百", "少了20元", "少了兩百", "找零", "未找零"]):
            return handle_customer_service_intent("未找零錢", reply_token)
        if any(keyword in normalized for keyword in ["少吐錢", "沒吐錢", "吞錢", "卡錢", "吃錢", "機器吃錢"]):
            return handle_customer_service_intent("機器吃錢", reply_token)
        if any(keyword in normalized for keyword in ["更換商品", "換貨", "換商品", "想換貨", "我要換", "外包封膜", "拆外包封膜"]):
            return handle_customer_service_intent("更換商品", reply_token)
        if any(keyword in normalized for keyword in ["沒開機", "不會震動", "沒反應", "不能震", "壞掉", "故障", "瑕疵"]):
            return handle_customer_service_intent("商品瑕疵故障", reply_token)
        if any(fragment in normalized for fragment in store_fragments):
            # Force the age-gate flow first so store-name fragments do not fall through to AI.
            return handle_age_gate("密碼", reply_token, None, "text")
    return False


AGE_AFFIRMATIVE = {"1", "是", "是的", "滿了", "已滿", "滿18", "已滿18歲", "滿18歲"}
AGE_NEGATIVE = {"2", "未滿18歲", "未成年", "不是", "否"}


def format_store_list(store_names: list[str]) -> str:
    if not store_names:
        return "請先提供店名"
    labels = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lines = []
    for idx, store_name in enumerate(store_names):
        prefix = labels[idx] if idx < len(labels) else str(idx + 1)
        lines.append(f"{prefix}. {store_name}")
    return "\n".join(lines)


def handle_age_gate(text: str, reply_token: str, user_id: Optional[str], message_type: str) -> bool:
    state = get_user_state(user_id)
    normalized = text.strip().lower()
    store_passwords = retrieve_store_passwords()

    if any(keyword in normalized for keyword in ["密碼", "門禁", "開門", "進店"]):
        if user_id:
            set_user_state(user_id, UserState(age_gate="awaiting_age", pending_store=""))
        reply_to_line(reply_token, "本店採實名制驗證，如您未滿18歲，請即刻離開本店!請問您滿18歲了嗎? 已滿18歲請回答1，未滿18歲請回覆2")
        return True

    if normalized in AGE_NEGATIVE:
        if user_id:
            set_user_state(user_id, UserState(age_gate="", pending_store=""))
        reply_to_line(reply_token, "因您未滿18歲，請您盡速離開本場所，避免觸法。")
        return True

    if contains_any(text, ["已滿18歲", "滿18歲", "已滿18", "滿18", "已滿", "是的", "是", "1"]):
        if user_id:
            set_user_state(user_id, UserState(age_gate="awaiting_store", pending_store=""))
        store_list = format_store_list(list(store_passwords.keys()))
        reply_to_line(reply_token, f"請問您是在哪一間店？\n{store_list}")
        return True

    if message_type == "image" or state.age_gate == "awaiting_age":
        if message_type == "image":
            if user_id:
                set_user_state(user_id, UserState(age_gate="awaiting_age", pending_store=""))
            reply_to_line(reply_token, "本店採實名制驗證，如您未滿18歲，請即刻離開本店!請問您滿18歲了嗎? 已滿18歲請回答1，未滿18歲請回覆2")
            return True
        if state.age_gate == "awaiting_age":
            reply_to_line(reply_token, "本店採實名制驗證，如您未滿18歲，請即刻離開本店!請問您滿18歲了嗎? 已滿18歲請回答1，未滿18歲請回覆2")
            return True

    if state.age_gate == "awaiting_store":
        store_text = normalize_text(text)
        for store_name, password in store_passwords.items():
            normalized_store = normalize_text(store_name)
            if normalized_store in store_text or store_text in normalized_store or normalized_store.endswith(store_text) or store_text.endswith(normalized_store):
                if user_id:
                    clear_user_state(user_id)
                reply_to_line(reply_token, f"{store_name} 的門禁密碼是 {password}")
                return True
        store_list = format_store_list(list(store_passwords.keys()))
        reply_to_line(reply_token, f"請先告訴我您是在哪一間店。\n{store_list}")
        return True

    return False


def summarize_for_prompt(user_id: Optional[str], text: str) -> str:
    parts = [part for part in [build_knowledge_context(text), build_memory_context(user_id, text)] if part]
    return "\n\n".join(parts + [f"User message:\n{text}"]) if parts else text


def handle_message(event: dict) -> None:
    reply_token = event.get("replyToken")
    if not reply_token:
        return

    user_id = (event.get("source") or {}).get("userId")
    message_type = extract_message_type(event)

    if message_type == "text":
        text = extract_user_text(event) or ""
        if handle_fuzzy_customer_service_intent(text, reply_token):
            return
        if handle_age_gate(text, reply_token, user_id, message_type):
            return
        if handle_customer_service_intent(text, reply_token):
            return
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
        if handle_age_gate("", reply_token, user_id, message_type):
            return
        message = event.get("message", {})
        message_id = message.get("id")
        if not message_id:
            reply_to_line(reply_token, "I could not read that image message.")
            return
        try:
            image_bytes, mime_type = download_line_content(message_id)
            answer = ask_openai(
                "Please inspect this image carefully and explain what you see in Traditional Chinese.",
                image_bytes=image_bytes,
                image_mime_type=mime_type,
            )
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
    for event in payload.get("events", []):
        threading.Thread(target=handle_message, args=(event,), daemon=True).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
