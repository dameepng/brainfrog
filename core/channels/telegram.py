"""Telegram Channel Adapter for BrainFrog.

Connects BrainFrog to the official Telegram Bot API with:
- Strict user allowlisting (unauthorized users are rejected before reaching runtime)
- Session isolation per (user_id, chat_id)
- Rate limiting per user
- Chunked message delivery (<4096 chars)
- Graceful threaded polling and clean shutdown
"""
from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Set

import requests

from core.runtime.messages import IncomingMessage, OutgoingMessage

from .base import BaseChannel, MessageHandler

TELEGRAM_MAX_MSG_LEN = 4000


class TelegramTransport(ABC):
    """Abstract transport for delivering messages to Telegram."""

    @abstractmethod
    def send(self, chat_id: str, text: str) -> bool:
        """Send a message chunk to the specified Telegram chat ID."""
        raise NotImplementedError


class MockTelegramTransport(TelegramTransport):
    """Deterministic in-memory transport for testing Telegram message delivery without credentials."""

    def __init__(self) -> None:
        self.sent_messages: List[Dict[str, str]] = []

    def send(self, chat_id: str, text: str) -> bool:
        self.sent_messages.append({"chat_id": str(chat_id), "text": text})
        return True

    def clear(self) -> None:
        self.sent_messages.clear()


class TelegramChannel(BaseChannel):
    """Channel adapter for Telegram Bots."""

    name: str = "telegram"

    def __init__(
        self,
        bot_token: Optional[str] = None,
        allowed_users: Optional[List[str | int] | Set[str | int]] = None,
        rate_limit_seconds: float = 1.0,
        poll_interval: float = 2.0,
        transport: Optional[TelegramTransport] = None,
    ) -> None:
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.rate_limit_seconds = rate_limit_seconds
        self.poll_interval = poll_interval
        self.transport = transport

        # Parse allowlist
        self.allowed_users: Set[str] = set()
        if allowed_users is not None:
            self.allowed_users = {str(u).strip().lower() for u in allowed_users if str(u).strip()}
        else:
            raw_env = os.environ.get("TELEGRAM_ALLOWED_USERS", "").strip()
            if raw_env:
                for token in raw_env.replace(",", " ").split():
                    clean = token.strip().lower()
                    if clean:
                        self.allowed_users.add(clean)

        self._handler: Optional[MessageHandler] = None
        self._is_running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_user_time: Dict[str, float] = {}
        self._last_update_id = 0

    @property
    def is_running(self) -> bool:
        return self._is_running

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def is_allowed_user(self, user_info: Dict[str, Any]) -> bool:
        """Verify whether an incoming user is on the strict authorization allowlist."""
        if not self.allowed_users:
            # Safe default: closed system if no allowlist is configured
            return False

        user_id = str(user_info.get("id", "")).strip().lower()
        username = str(user_info.get("username", "")).strip().lower()

        if user_id and user_id in self.allowed_users:
            return True
        if username and (username in self.allowed_users or f"@{username}" in self.allowed_users):
            return True

        return False

    def _check_rate_limit(self, user_id: str) -> bool:
        if self.rate_limit_seconds <= 0.0:
            return True
        now = time.time()
        last = self._last_user_time.get(user_id, 0.0)
        if (now - last) < self.rate_limit_seconds:
            return False
        self._last_user_time[user_id] = now
        return True

    def process_update(self, update: Dict[str, Any]) -> Optional[OutgoingMessage]:
        """Process a single Telegram update dict and return the response."""
        update_id = update.get("update_id", 0)
        message = update.get("message") or update.get("edited_message")
        if not message:
            return None

        from_user = message.get("from", {})
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        user_id = str(from_user.get("id", ""))
        text = str(message.get("text", "")).strip()

        if not text or not chat_id:
            return None

        # 1. Strict Allowlist Security Check
        if not self.is_allowed_user(from_user):
            unauthorized_msg = (
                "⛔ Akses Ditolak: Akun Anda tidak terdaftar dalam allowlist BrainFrog.\n"
                f"User ID Anda: `{user_id}`\n"
                "Hubungi administrator untuk menambahkan ID Anda ke `TELEGRAM_ALLOWED_USERS`."
            )
            self._send_text(chat_id, unauthorized_msg)
            return OutgoingMessage(text="Unauthorized", success=False, status="rejected", error="User not allowlisted")

        # 2. Rate Limiting Check
        if not self._check_rate_limit(user_id):
            rate_msg = "⏳ Terlalu cepat. Harap tunggu sebentar sebelum mengirim pesan berikutnya."
            self._send_text(chat_id, rate_msg)
            return OutgoingMessage(text="Rate limited", success=False, status="rejected", error="Rate limit exceeded")

        # 3. Create Normalized IncomingMessage
        incoming = IncomingMessage(
            id=f"tg-{update_id}",
            channel="telegram",
            user_id=user_id,
            conversation_id=chat_id,
            text=text,
            metadata={
                "chat_type": chat.get("type", "private"),
                "username": from_user.get("username"),
                "first_name": from_user.get("first_name"),
            },
        )

        # 4. Route to BrainFrog Runtime Handler
        if not self._handler:
            err_msg = "BrainFrog Runtime handler not attached."
            self._send_text(chat_id, f"⚠️ {err_msg}")
            return OutgoingMessage(text=err_msg, success=False, error=err_msg)

        out = self._handler(incoming)

        # 5. Deliver Response back to Telegram
        if out and out.text:
            self._send_text(chat_id, out.text)

        return out

    def _send_text(self, chat_id: str, text: str) -> bool:
        if not self.bot_token and not self.transport:
            return False

        chunks = [text[i : i + TELEGRAM_MAX_MSG_LEN] for i in range(0, len(text), TELEGRAM_MAX_MSG_LEN)]
        all_ok = True

        for chunk in chunks:
            if self.transport:
                ok = self.transport.send(chat_id, chunk)
                all_ok = all_ok and ok
                continue

            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
            }
            try:
                res = requests.post(url, json=payload, timeout=10)
                if not res.ok:
                    # Retry without markdown if parsing failed
                    payload.pop("parse_mode", None)
                    res = requests.post(url, json=payload, timeout=10)
                all_ok = all_ok and res.ok
            except Exception:
                all_ok = False

        return all_ok

    def send(self, message: OutgoingMessage, destination: str) -> bool:
        return self._send_text(destination, message.text)

    def simulate_incoming(
        self,
        chat_id: str,
        user_id: str,
        text: str,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        update_id: int = 1,
    ) -> Optional[OutgoingMessage]:
        """Convenience method for testing: simulate an update arriving from Telegram."""
        update = {
            "update_id": update_id,
            "message": {
                "from": {
                    "id": user_id,
                    "username": username or f"user_{user_id}",
                    "first_name": first_name or "TestUser",
                },
                "chat": {
                    "id": chat_id,
                    "type": "private",
                },
                "text": text,
            },
        }
        return self.process_update(update)

    def _poll_worker(self) -> None:
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        while not self._stop_event.is_set():
            try:
                params = {"offset": self._last_update_id + 1, "timeout": 5}
                resp = requests.get(url, params=params, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    for upd in data.get("result", []):
                        upd_id = upd.get("update_id", 0)
                        if upd_id > self._last_update_id:
                            self._last_update_id = upd_id
                        self.process_update(upd)
            except Exception:
                pass
            time.sleep(self.poll_interval)

    def start(self) -> None:
        if not self.bot_token and not self.transport:
            raise RuntimeError("Cannot start TelegramChannel: TELEGRAM_BOT_TOKEN is missing.")
        if self._is_running:
            return

        self._stop_event.clear()
        self._is_running = True
        if self.bot_token:
            self._thread = threading.Thread(target=self._poll_worker, daemon=True, name="TelegramPollWorker")
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

