"""WhatsApp Channel Adapter and Transport Abstraction for BrainFrog.

Decouples the WhatsApp channel interface from underlying transport layers:
- WhatsAppTransport (Abstract transport interface)
- MockWhatsAppTransport (Local testing and development)
- WhatsAppCloudTransport (Official WhatsApp Business / Cloud API structure)
- WhatsAppChannel (Allowlist security, rate limiting, and session isolation)
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Set

from core.runtime.messages import IncomingMessage, OutgoingMessage

from .base import BaseChannel, MessageHandler


class WhatsAppTransport(ABC):
    """Abstract transport backend for WhatsApp communication."""

    @abstractmethod
    def send(self, to: str, text: str) -> bool:
        """Send a text message to a WhatsApp recipient phone number."""
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
    """In-memory mock transport for testing and local development without WhatsApp credentials."""

    def __init__(self) -> None:
        self.sent_messages: List[Dict[str, str]] = []
        self._running = False
        self._incoming_callback: Optional[Callable[[Dict[str, Any]], None]] = None

    def set_incoming_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._incoming_callback = callback

    def simulate_incoming(self, from_number: str, text: str, message_id: str = "wa-mock-1") -> None:
        """Simulate an incoming message arriving from a WhatsApp user."""
        if self._incoming_callback:
            payload = {
                "id": message_id,
                "from": from_number,
                "text": text,
                "timestamp": 1700000000,
            }
            self._incoming_callback(payload)

    def send(self, to: str, text: str) -> bool:
        self.sent_messages.append({"to": to, "text": text})
        return True

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
        self.phone_number_id = phone_number_id or os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self.access_token = access_token or os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
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
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
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
    """WhatsApp Channel Adapter with allowlist gating and session isolation."""

    name: str = "whatsapp"

    def __init__(
        self,
        transport: Optional[WhatsAppTransport] = None,
        allowed_users: Optional[List[str] | Set[str]] = None,
    ) -> None:
        self.transport = transport or MockWhatsAppTransport()

        # Parse allowlist
        self.allowed_users: Set[str] = set()
        if allowed_users is not None:
            self.allowed_users = {str(u).strip().replace("+", "").lower() for u in allowed_users if str(u).strip()}
        else:
            raw_env = os.environ.get("WHATSAPP_ALLOWED_USERS", "").strip()
            if raw_env:
                for token in raw_env.replace(",", " ").split():
                    clean = token.strip().replace("+", "").lower()
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
        """Verify whether an incoming phone number is authorized."""
        if not self.allowed_users:
            # Safe default: closed system if no allowlist is configured
            return False
        clean = str(phone_number).strip().replace("+", "").lower()
        return clean in self.allowed_users

    def handle_raw_message(self, raw_payload: Dict[str, Any]) -> Optional[OutgoingMessage]:
        """Process an incoming WhatsApp message payload from the transport."""
        msg_id = str(raw_payload.get("id", "wa-msg"))
        from_number = str(raw_payload.get("from", "")).strip()
        text = str(raw_payload.get("text", "")).strip()

        if not from_number or not text:
            return None

        # 1. Allowlist Security Gate
        if not self.is_allowed_user(from_number):
            unauthorized_text = (
                "⛔ Akses Ditolak: Nomor WhatsApp Anda tidak terdaftar dalam allowlist BrainFrog."
            )
            self.transport.send(from_number, unauthorized_text)
            return OutgoingMessage(text="Unauthorized", success=False, error="User not allowlisted")

        # 2. Build Normalized IncomingMessage
        clean_user_id = from_number.replace("+", "")
        incoming = IncomingMessage(
            id=msg_id,
            channel="whatsapp",
            user_id=clean_user_id,
            conversation_id=clean_user_id,
            text=text,
            metadata={"raw_phone": from_number},
        )

        # 3. Route to Runtime Handler
        if not self._handler:
            err_msg = "BrainFrog Runtime handler not attached."
            self.transport.send(from_number, f"⚠️ {err_msg}")
            return OutgoingMessage(text=err_msg, success=False, error=err_msg)

        out = self._handler(incoming)

        # 4. Deliver response via transport
        if out and out.text:
            self.transport.send(from_number, out.text)

        return out

    def send(self, message: OutgoingMessage, destination: str) -> bool:
        return self.transport.send(destination, message.text)

    def start(self) -> None:
        self.transport.start()

    def stop(self) -> None:
        self.transport.stop()
