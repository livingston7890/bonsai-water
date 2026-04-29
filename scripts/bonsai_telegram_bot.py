#!/usr/bin/env python3
"""Telegram poller for Project Bonsai deterministic ops."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib import parse, request, error as urlerror

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "generated" / "telegram"
OFFSET_FILE = STATE_DIR / "bonsai_bot_offset.txt"
CHAT_FILE = STATE_DIR / "bonsai_bot_chat_id.txt"
ENV_FILE = ROOT / ".env"

sys.path.insert(0, str(ROOT / "scripts"))
import bonsai_ops  # noqa: E402


def load_dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    return bonsai_ops.load_dotenv(path)


def cfg(name: str, default: str = "") -> str:
    return os.environ.get(name) or load_dotenv().get(name) or default


def token() -> str:
    value = cfg("BONSAI_TELEGRAM_BOT_TOKEN")
    if not value:
        raise RuntimeError(f"BONSAI_TELEGRAM_BOT_TOKEN missing. Run {ROOT / 'scripts/set-bonsai-bot-env.sh'}")
    return value


def api(method: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token()}/{method}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def send_message(chat_id: int | str, text: str) -> None:
    # Telegram max message is 4096; stay comfortably under it.
    text = (text or "").strip() or "(empty)"
    for chunk in [text[i : i + 3500] for i in range(0, len(text), 3500)]:
        api("sendMessage", {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}, timeout=15)


def read_offset() -> int | None:
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def write_offset(offset: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(offset), encoding="utf-8")


def write_chat(chat_id: int | str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CHAT_FILE.write_text(str(chat_id), encoding="utf-8")


def allowed_user_id() -> str:
    value = cfg("BONSAI_TELEGRAM_ALLOWED_USER_ID")
    if not value:
        raise RuntimeError("BONSAI_TELEGRAM_ALLOWED_USER_ID missing. Message the bot once, then rerun scripts/set-bonsai-bot-env.sh.")
    return value


def update_sender(update: dict[str, Any]) -> tuple[str | None, str | None, int | str | None]:
    msg = update.get("message") or update.get("edited_message") or {}
    if not msg and update.get("callback_query"):
        msg = update["callback_query"].get("message") or {}
    user = (update.get("message") or update.get("edited_message") or update.get("callback_query") or {}).get("from") or {}
    text = msg.get("text") or ""
    chat = msg.get("chat") or {}
    return str(user.get("id")) if user.get("id") is not None else None, text, chat.get("id")


def handle_update(update: dict[str, Any]) -> None:
    user_id, text, chat_id = update_sender(update)
    if chat_id is None:
        return
    if user_id != allowed_user_id():
        send_message(chat_id, "Not authorized for Project Bonsai ops.")
        return
    write_chat(chat_id)
    try:
        reply = bonsai_ops.apply_command(text or "help")
    except Exception as exc:
        reply = f"Project Bonsai command failed: {exc}"
    send_message(chat_id, reply)


def poll_once() -> int:
    params = {"timeout": 25, "allowed_updates": ["message", "edited_message", "callback_query"]}
    offset = read_offset()
    if offset is not None:
        params["offset"] = offset
    result = api("getUpdates", params, timeout=35)
    updates = result.get("result") or []
    handled = 0
    max_update_id = None
    for upd in updates:
        max_update_id = upd.get("update_id", max_update_id)
        handle_update(upd)
        handled += 1
    if max_update_id is not None:
        write_offset(int(max_update_id) + 1)
    return handled


def run_forever() -> None:
    while True:
        try:
            poll_once()
        except urlerror.URLError as exc:
            print(f"Telegram network error: {exc}", file=sys.stderr)
            time.sleep(10)
        except Exception as exc:
            print(f"Bonsai Telegram bot error: {exc}", file=sys.stderr)
            time.sleep(10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--send-test", action="store_true", help="send a test message to saved chat/allowed user")
    args = parser.parse_args(argv)
    if args.send_test:
        chat_id = CHAT_FILE.read_text(encoding="utf-8").strip() if CHAT_FILE.exists() else allowed_user_id()
        send_message(chat_id, "Project Bonsai bot online. Send 'status' or 'moisture'.")
        return 0
    if args.once:
        poll_once()
        return 0
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
