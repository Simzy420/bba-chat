"""
Big Brain Ape — Ask AI chat server for the Hugging Face Space.

The dashboard posts user text to POST /send and polls GET /messages?since=
for role=agent replies. This process keeps that inbox contract and, after
each user message, asks a Virtuals GAME ChatAgent (BigBrain2Bot) for the reply.

Telegram for @BigBrain2Bot stays on Virtuals. This server does not register
a Telegram webhook.
"""

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from game_sdk.game.chat_agent import Chat, ChatAgent

logger = logging.getLogger("bba-chat")
logging.basicConfig(level=logging.INFO)

# Identity baked into each GAME conversation at create_chat time.
# Bump PROMPT_VERSION when this text changes so stored sessions are rebuilt.
PROMPT_VERSION = "bba-ask-ai-1"
AGENT_PROMPT = """You are Big Brain Ape, the public voice of BigBrain2Bot.

Identity:
- Virtuals Protocol agent #96114
- DegenClaw Arena agent #1729
- Handle @BigBrain2Bot. That Telegram bot stays hosted on Virtuals. You are answering the Big Brain Ape web app Ask AI chat. You do not own or replace that bot.
- Hyperliquid perpetuals desk covering 98 assets: crypto, equities, commodities, currencies, and indices.
- You analyze with the Druckenmiller Three Lenses: Liquidity, Valuation, and Technicals.

How to answer:
- Speak as that desk. Be direct and specific. Answer ticker analysis and questions about the Big Brain Ape app.
- Walk the three lenses when someone asks about a ticker, a market, or a trade idea.
- You do not have live Arena, exchange, or account state in this chat. Never invent account balances, open positions, fills, margin, or PnL. When a question needs live Arena or account state, say you do not have that live state here.
- Answer in this chat now. Do not defer and do not tell the person to wait or switch apps.
- Do not use these phrases: "on it", "get back to you", "I'll review", "I will review".
- Do not use the word "Telegram" in the reply. If you must name the bot, say @BigBrain2Bot.
"""

MISSING_KEY_TEXT = (
    "GAME_API_KEY is not configured on this chat server, so I cannot reach "
    "the Virtuals GAME ChatAgent. Set the GAME_API_KEY Space secret to an "
    "apt- Virtuals GAME v2 key and restart the Space."
)
BAD_PREFIX_TEXT = (
    "GAME_API_KEY is set, but ChatAgent needs a Virtuals GAME v2 key that "
    "starts with apt-. Replace the Space secret and restart the Space."
)
EMPTY_REPLY_TEXT = (
    "I did not get a reply from the Virtuals GAME ChatAgent. Send that again."
)
MAX_STORED_MESSAGES = 200
DEFAULT_GAME_TIMEOUT = 75.0
_APT_KEY = re.compile(r"apt-[A-Za-z0-9_\-]+")

_messages_lock = threading.Lock()
_session_lock = threading.Lock()
_user_locks_guard = threading.Lock()
_user_locks: dict[str, threading.Lock] = {}
_chats: dict[str, Any] = {}
_agent: Optional[ChatAgent] = None
_agent_key: Optional[str] = None
_timeout_installed = False


class SendBody(BaseModel):
    text: str
    user: str = "Simzy"


class RespondBody(BaseModel):
    text: str
    secret: str = ""


app = FastAPI(title="BBA Chat")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def data_dir() -> str:
    preferred = os.environ.get("BBA_CHAT_DATA", "/tmp/bba-chat")
    try:
        os.makedirs(preferred, exist_ok=True)
        return preferred
    except OSError:
        fallback = "/tmp/bba-chat"
        os.makedirs(fallback, exist_ok=True)
        logger.warning("Could not use BBA_CHAT_DATA=%s; using %s", preferred, fallback)
        return fallback


def messages_path() -> str:
    return os.path.join(data_dir(), "messages.json")


def sessions_path() -> str:
    return os.path.join(data_dir(), "sessions.json")


def agent_secret() -> str:
    return os.environ.get("BBA_CHAT_SECRET", "bba-chat-2026")


def game_timeout() -> float:
    raw = os.environ.get("BBA_CHAT_GAME_TIMEOUT", str(DEFAULT_GAME_TIMEOUT))
    try:
        value = float(raw)
    except ValueError:
        value = DEFAULT_GAME_TIMEOUT
    return min(110.0, max(10.0, value))


def game_api_key() -> str:
    return os.environ.get("GAME_API_KEY", "").strip()


def game_status() -> dict:
    key = game_api_key()
    return {
        "game_api_key_set": bool(key),
        "game_api_key_v2": key.startswith("apt-"),
    }


def install_request_timeout() -> None:
    """game-sdk posts with requests and no timeout. Cap those calls."""
    global _timeout_installed
    if _timeout_installed:
        return
    import requests

    original = requests.sessions.Session.request

    def wrapped(self, method, url, **kwargs):
        kwargs.setdefault("timeout", game_timeout())
        return original(self, method, url, **kwargs)

    requests.sessions.Session.request = wrapped
    _timeout_installed = True


def reset_runtime() -> None:
    """Drop in-memory agent and chat handles. Used by tests."""
    global _agent, _agent_key
    with _session_lock:
        _chats.clear()
        _agent = None
        _agent_key = None
    with _user_locks_guard:
        _user_locks.clear()


def _atomic_write(path: str, payload: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load_messages() -> list:
    try:
        with open(messages_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_messages(msgs: list) -> None:
    _atomic_write(messages_path(), msgs)


def _read_sessions() -> dict:
    try:
        with open(sessions_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_sessions(sessions: dict) -> None:
    _atomic_write(sessions_path(), sessions)


def normalize_user(user: str) -> str:
    cleaned = (user or "").strip()
    return cleaned or "Simzy"


def partner_id_for(user: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_\-]", "_", user)[:64]
    return slug or "web_user"


def user_lock(user: str) -> threading.Lock:
    with _user_locks_guard:
        lock = _user_locks.get(user)
        if lock is None:
            lock = threading.Lock()
            _user_locks[user] = lock
        return lock


def append_message(msg: dict) -> dict:
    with _messages_lock:
        msgs = load_messages()
        msgs.append(msg)
        save_messages(msgs[-MAX_STORED_MESSAGES:])
    return msg


def new_id() -> str:
    return str(uuid.uuid4())


def stamp_after(after_ts: int) -> int:
    """Dashboard Ask AI only renders agent rows with ts strictly greater than the user ts."""
    return max(int(time.time()), int(after_ts) + 1)


def make_user_message(text: str, user: str) -> dict:
    return {
        "id": new_id(),
        "ts": int(time.time()),
        "role": "user",
        "text": text,
        "user": user,
    }


def make_agent_message(text: str, after_ts: int = 0) -> dict:
    return {
        "id": new_id(),
        "ts": stamp_after(after_ts),
        "role": "agent",
        "text": text,
    }


def scrub_for_dashboard(text: str) -> str:
    """Ask AI hides replies that contain a few deferral phrases. Rewrite those."""
    cleaned = _APT_KEY.sub("apt-***", text)
    cleaned = re.sub(r"telegram", "Virtuals bot", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"get back to you", "answer in this chat", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"i['’]ll review", "here is the read", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"i will review", "here is the read", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bon its\b", "on that", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bon it\b", "on that", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def public_error(exc: BaseException) -> str:
    detail = scrub_for_dashboard(str(exc).replace("\n", " "))
    if len(detail) > 280:
        detail = detail[:280] + "..."
    if not detail:
        detail = exc.__class__.__name__
    return (
        "I could not reach the Virtuals GAME ChatAgent. "
        f"{detail} Send that again in a moment."
    )


def get_agent(api_key: str) -> ChatAgent:
    global _agent, _agent_key
    with _session_lock:
        if _agent is not None and _agent_key == api_key:
            return _agent
        agent = ChatAgent(api_key=api_key, prompt=AGENT_PROMPT)
        _agent = agent
        _agent_key = api_key
        return agent


def drop_session(user: str) -> None:
    with _session_lock:
        _chats.pop(user, None)
        sessions = _read_sessions()
        if user in sessions:
            sessions.pop(user, None)
            _write_sessions(sessions)


def open_chat(agent: ChatAgent, user: str) -> Any:
    """Reuse a stored conversation_id, or create_chat once per user.

    game-sdk ChatAgent has no resume helper. Chat(conversation_id, client)
    continues an existing conversation via update_chat, so the id is stored
    in sessions.json under BBA_CHAT_DATA.
    """
    with _session_lock:
        cached = _chats.get(user)
        if cached is not None:
            return cached
        saved = _read_sessions().get(user) or {}
        conversation_id = saved.get("conversation_id")
        if saved.get("prompt_version") == PROMPT_VERSION and conversation_id:
            chat = Chat(conversation_id, agent.client)
            _chats[user] = chat
            return chat

    chat = agent.create_chat(
        partner_id=partner_id_for(user),
        partner_name=user[:80],
    )
    with _session_lock:
        sessions = _read_sessions()
        sessions[user] = {
            "conversation_id": chat.chat_id,
            "partner_name": user[:80],
            "prompt_version": PROMPT_VERSION,
        }
        _write_sessions(sessions)
        _chats[user] = chat
    logger.info("Started GAME chat for user %s", user)
    return chat


def has_saved_session(user: str) -> bool:
    with _session_lock:
        if user in _chats:
            return True
        saved = _read_sessions().get(user) or {}
    return saved.get("prompt_version") == PROMPT_VERSION and bool(saved.get("conversation_id"))


def _reply_from_chat(agent: ChatAgent, user: str, text: str) -> str:
    chat = open_chat(agent, user)
    response = chat.next(text)
    if getattr(response, "is_finished", False):
        # Remote conversation is already finished. Drop the id so the next
        # user message calls create_chat again. Do not call chat.end(); that
        # is a second HTTP round trip before the dashboard can see the reply.
        drop_session(user)
    message = scrub_for_dashboard(getattr(response, "message", "") or "")
    return message or EMPTY_REPLY_TEXT


def ask_game(user: str, text: str) -> str:
    install_request_timeout()
    api_key = game_api_key()
    if not api_key:
        return MISSING_KEY_TEXT
    if not api_key.startswith("apt-"):
        return BAD_PREFIX_TEXT

    agent = get_agent(api_key)
    # Retry only when a stored conversation id may be stale. A brand-new
    # chat that fails should not burn a second 75s call past the dashboard's
    # 120s poll window.
    retry_stale_session = has_saved_session(user)
    try:
        return _reply_from_chat(agent, user, text)
    except Exception as exc:
        drop_session(user)
        if not retry_stale_session or "No functions provided" in str(exc):
            logger.warning("GAME reply failed for user %s: %s", user, public_error(exc))
            return public_error(exc)
    try:
        return _reply_from_chat(agent, user, text)
    except Exception as exc:
        drop_session(user)
        logger.warning("GAME reply failed for user %s: %s", user, public_error(exc))
        return public_error(exc)


def generate_agent_reply(user: str, text: str, user_ts: int) -> None:
    """Background task. Always stores an agent row so the dashboard poll can finish."""
    try:
        with user_lock(user):
            reply = ask_game(user, text)
    except Exception as exc:
        logger.exception("Unexpected GAME reply failure")
        reply = public_error(exc)
    append_message(make_agent_message(reply, after_ts=user_ts))


@app.get("/")
async def root():
    with _messages_lock:
        count = len(load_messages())
    return {
        "status": "ok",
        "service": "bba-chat",
        "messages": count,
        **game_status(),
    }


@app.get("/health")
async def health():
    with _messages_lock:
        count = len(load_messages())
    return {
        "status": "ok",
        "service": "bba-chat",
        "messages": count,
        **game_status(),
    }


@app.get("/messages")
async def get_messages(since: int = 0):
    """Messages newer than `since`. The web app polls this."""
    with _messages_lock:
        msgs = load_messages()
    recent = [m for m in msgs if m.get("ts", 0) > since]
    return {"messages": recent, "total": len(msgs)}


@app.post("/send")
async def send_message(body: SendBody, background: BackgroundTasks):
    """Store a user message, then ask BigBrain2Bot's ChatAgent in the background."""
    text = body.text.strip()
    if not text:
        return JSONResponse({"error": "empty message"}, status_code=400)

    user = normalize_user(body.user)
    msg = append_message(make_user_message(text, user))
    background.add_task(generate_agent_reply, user, text, msg["ts"])
    return {"status": "ok", "message": msg}


@app.post("/respond")
async def post_response(body: RespondBody):
    """Manual agent post. Protected by BBA_CHAT_SECRET. Does not call GAME."""
    if body.secret != agent_secret():
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    text = body.text.strip()
    if not text:
        return JSONResponse({"error": "empty response"}, status_code=400)

    msg = append_message(make_agent_message(text))
    return {"status": "ok", "message": msg}


@app.get("/pending")
async def get_pending(secret: str = ""):
    """User messages after the latest agent message. Protected by BBA_CHAT_SECRET."""
    if secret != agent_secret():
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    with _messages_lock:
        msgs = load_messages()
    last_agent_ts = 0
    for item in reversed(msgs):
        if item.get("role") == "agent":
            last_agent_ts = item.get("ts", 0)
            break
    pending = [
        item
        for item in msgs
        if item.get("role") == "user" and item.get("ts", 0) > last_agent_ts
    ]
    return {"pending": pending, "count": len(pending)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=7860)
