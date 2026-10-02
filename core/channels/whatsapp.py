"""WhatsApp Channel Adapter and Transport Abstraction for BrainFrog.

Decouples the WhatsApp channel interface from underlying transport layers:
- WhatsAppTransport (Abstract transport interface)
- MockWhatsAppTransport (Local deterministic testing and development)
- WhatsAppCloudTransport (Official Meta WhatsApp Business / Cloud API structure)
- WhatsAppChannel (Allowlist security, rate limiting, anti-spoofing, chunking, and session isolation)
"""
from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Set

from core.runtime.messages import IncomingMessage, OutgoingMessage

from .base import BaseChannel, MessageHandler

WHATSAPP_MAX_MSG_LEN = 4000


def normalize_phone_number(phone: str) -> str:
    """Normalize phone number to clean string (strips +, spaces, dashes, parentheses, whatsapp: prefix)."""
    clean = str(phone or "").strip().lower()
    if clean.startswith("whatsapp:"):
        clean = clean[len("whatsapp:") :]
    for ch in ("+", " ", "-", "(", ")", "."):
        clean = clean.replace(ch, "")
    return clean


class WhatsAppTransport(ABC):
    """Abstract transport backend for WhatsApp communication."""

    @abstractmethod
    def send(self, to: str, text: str) -> bool:
        """Send a text message chunk to a WhatsApp recipient phone number."""
        raise NotImplementedError

    @abstractmethod
    def start(self) -> None:
        """Start transport listener or background connection."""
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        """Stop transport listener."""
        raise NotImplementedError

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """Return True if transport is running."""
        raise NotImplementedError


class MockWhatsAppTransport(WhatsAppTransport):
    """In-memory mock transport for deterministic testing and local development without WhatsApp credentials."""

    def __init__(self) -> None:
        self.sent_messages: List[Dict[str, str]] = []
        self._running = False
        self._incoming_callback: Optional[Callable[[Dict[str, Any]], Optional[OutgoingMessage]]] = None

    def set_incoming_callback(self, callback: Callable[[Dict[str, Any]], Optional[OutgoingMessage]]) -> None:
        self._incoming_callback = callback

    def simulate_incoming(
        self,
        from_number: str,
        text: str,
        message_id: str = "wa-mock-1",
        conversation_id: Optional[str] = None,
    ) -> Optional[OutgoingMessage]:
        """Simulate an incoming message arriving from a WhatsApp user."""
        if self._incoming_callback:
            payload = {
                "id": message_id,
                "from": from_number,
                "conversation_id": conversation_id,
                "text": text,
                "timestamp": int(time.time()),
            }
            return self._incoming_callback(payload)
        return None

    def send(self, to: str, text: str) -> bool:
        self.sent_messages.append({"to": str(to), "text": text})
        return True

    def clear(self) -> None:
        """Clear sent messages buffer."""
        self.sent_messages.clear()

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running


class WhatsAppCloudTransport(WhatsAppTransport):
    """Transport implementation for the official Meta WhatsApp Cloud API."""

    def __init__(
        self,
        phone_number_id: Optional[str] = None,
        access_token: Optional[str] = None,
        api_version: str = "v18.0",
    ) -> None:
        self.phone_number_id = (
            phone_number_id
            or os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        ).strip()
        self.access_token = (
            access_token
            or os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
            or os.environ.get("WHATSAPP_API_TOKEN", "")
        ).strip()
        self.api_version = api_version
        self._running = False

    def send(self, to: str, text: str) -> bool:
        if not self.phone_number_id or not self.access_token:
            return False
        import requests

        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        clean_to = normalize_phone_number(to)
        payload = {
            "messaging_product": "whatsapp",
            "to": clean_to,
            "type": "text",
            "text": {"body": text},
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            return resp.ok
        except Exception:
            return False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running


class WhatsAppChannel(BaseChannel):
    """WhatsApp Channel Adapter with allowlist gating, rate limiting, chunking, and session isolation."""

    name: str = "whatsapp"

    def __init__(
        self,
        transport: Optional[WhatsAppTransport] = None,
        allowed_users: Optional[List[str] | Set[str]] = None,
        rate_limit_seconds: float = 0.0,
    ) -> None:
        if transport is not None:
            self.transport = transport
        else:
            t_type = os.environ.get("WHATSAPP_TRANSPORT", "").lower().strip()
            cloud_token = (
                os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
                or os.environ.get("WHATSAPP_API_TOKEN", "")
            ).strip()
            cloud_phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()

            if t_type == "cloud" or (cloud_token and cloud_phone_id):
                self.transport = WhatsAppCloudTransport(
                    phone_number_id=cloud_phone_id,
                    access_token=cloud_token,
                )
            else:
                self.transport = MockWhatsAppTransport()

        self.rate_limit_seconds = rate_limit_seconds
        self._last_user_time: Dict[str, float] = {}

        # Parse allowlist
        self.allowed_users: Set[str] = set()
        if allowed_users is not None:
            self.allowed_users = {
                normalize_phone_number(u)
                for u in allowed_users
                if normalize_phone_number(u)
            }
        else:
            raw_env = os.environ.get("WHATSAPP_ALLOWED_USERS", "").strip()
            if raw_env:
                for token in raw_env.replace(",", " ").split():
                    clean = normalize_phone_number(token)
                    if clean:
                        self.allowed_users.add(clean)

        self._handler: Optional[MessageHandler] = None

        if isinstance(self.transport, MockWhatsAppTransport):
            self.transport.set_incoming_callback(self.handle_raw_message)

    @property
    def is_running(self) -> bool:
        return self.transport.is_running

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def is_allowed_user(self, phone_number: str) -> bool:
        """Verify whether an incoming phone number is on the strict authorization allowlist."""
        if not self.allowed_users:
            # Safe default: closed system if no allowlist is configured
            return False
        clean = normalize_phone_number(phone_number)
        return clean in self.allowed_users

    def _check_rate_limit(self, user_id: str) -> bool:
        """Enforce per-user message rate limiting."""
        if self.rate_limit_seconds <= 0.0:
            return True
        now = time.time()
        last = self._last_user_time.get(user_id, 0.0)
        if (now - last) < self.rate_limit_seconds:
            return False
        self._last_user_time[user_id] = now
        return True

    def _send_text(self, to: str, text: str) -> bool:
        """Send outgoing text chunked into message blocks of <= WHATSAPP_MAX_MSG_LEN."""
        if not text or not to:
            return False

        chunks = [
            text[i : i + WHATSAPP_MAX_MSG_LEN]
            for i in range(0, len(text), WHATSAPP_MAX_MSG_LEN)
        ]
        all_ok = True
        for chunk in chunks:
            ok = self.transport.send(to, chunk)
            all_ok = all_ok and ok
        return all_ok

    def handle_raw_message(self, raw_payload: Dict[str, Any]) -> Optional[OutgoingMessage]:
        """Process an incoming WhatsApp message payload from the transport."""
        msg_id = str(raw_payload.get("id", "wa-msg"))
        from_number = str(raw_payload.get("from", "")).strip()
        text = str(raw_payload.get("text", "")).strip()

        if not from_number or not text:
            return None

        clean_user_id = normalize_phone_number(from_number)
        raw_conv = raw_payload.get("conversation_id") or raw_payload.get("chat_id")
        if raw_conv is not None and str(raw_conv).strip():
            conv_id = str(raw_conv).strip()
            if conv_id.startswith("+") or conv_id.startswith("whatsapp:"):
                conv_id = normalize_phone_number(conv_id)
        else:
            conv_id = clean_user_id

        # 1. Strict Allowlist Security Gate
        if not self.is_allowed_user(from_number):
            unauthorized_text = (
                "⛔ Akses Ditolak: Nomor WhatsApp Anda tidak terdaftar dalam allowlist BrainFrog.\n"
                f"Nomor Anda: `{from_number}`\n"
                "Hubungi administrator untuk menambahkan nomor Anda ke `WHATSAPP_ALLOWED_USERS`."
            )
            self._send_text(from_number, unauthorized_text)
            return OutgoingMessage(
                text="Unauthorized",
                success=False,
                status="rejected",
                error="User not allowlisted",
            )

        # 2. Rate Limiting Check
        if not self._check_rate_limit(clean_user_id):
            rate_msg = "⏳ Terlalu cepat. Harap tunggu sebentar sebelum mengirim pesan berikutnya."
            self._send_text(from_number, rate_msg)
            return OutgoingMessage(
                text="Rate limited",
                success=False,
                status="rejected",
                error="Rate limit exceeded",
            )

        # 3. Build Normalized IncomingMessage (Channel strictly forced to 'whatsapp' to prevent spoofing)
        incoming = IncomingMessage(
            id=msg_id,
            channel="whatsapp",
            user_id=clean_user_id,
            conversation_id=conv_id,
            text=text,
            metadata={
                "raw_phone": from_number,
                "source_channel": "whatsapp",
            },
        )

        # 4. Route to BrainFrog Runtime Handler
        if not self._handler:
            err_msg = "BrainFrog Runtime handler not attached."
            self._send_text(from_number, f"⚠️ {err_msg}")
            return OutgoingMessage(
                text=err_msg,
                success=False,
                status="error",
                error=err_msg,
            )

        out = self._handler(incoming)

        # 5. Deliver response via transport (with automatic chunking)
        if out and out.text:
            self._send_text(from_number, out.text)

        return out

    def simulate_incoming(
        self,
        from_number: str,
        text: str,
        message_id: str = "wa-msg-1",
        conversation_id: Optional[str] = None,
    ) -> Optional[OutgoingMessage]:
        """Convenience method for testing: simulate an incoming message from a WhatsApp user."""
        payload = {
            "id": message_id,
            "from": from_number,
            "conversation_id": conversation_id,
            "text": text,
            "timestamp": int(time.time()),
        }
        return self.handle_raw_message(payload)

    def send(self, message: OutgoingMessage, destination: str) -> bool:
        """Send an outgoing message to the specified recipient phone number."""
        return self._send_text(destination, message.text)

    def start(self) -> None:
        """Start transport listener."""
        self.transport.start()

    def stop(self) -> None:
        """Stop transport listener."""
        self.transport.stop()
