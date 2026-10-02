"""Base channel interface for BrainFrog.

Decouples external messaging platforms (CLI, Telegram, WhatsApp, Discord)
from internal agent runtime execution.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from core.runtime.messages import IncomingMessage, OutgoingMessage

MessageHandler = Callable[[IncomingMessage], OutgoingMessage]


class BaseChannel(ABC):
    """Abstract base class for all BrainFrog communication channels."""

    name: str = "base"

    @abstractmethod
    def start(self) -> None:
        """Start listening for incoming messages."""
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        """Gracefully stop listening."""
        raise NotImplementedError

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """Check if channel is actively running."""
        raise NotImplementedError

    @abstractmethod
    def send(self, message: OutgoingMessage, destination: str) -> bool:
        """Send an outgoing message to the platform."""
        raise NotImplementedError

    @abstractmethod
    def set_handler(self, handler: MessageHandler) -> None:
        """Set the callback handler to process incoming messages."""
        raise NotImplementedError
