"""Normalized runtime message and event abstractions for BrainFrog.

These lightweight models decouple incoming channels (CLI, Telegram, WhatsApp)
from the internal BrainFrog reasoning and orchestration engines.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentEvent:
    """Normalized lifecycle event emitted during task execution."""

    type: str
    timestamp: float = field(default_factory=time.time)
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentEvent":
        return cls(
            type=data.get("type", "unknown"),
            timestamp=float(data.get("timestamp", time.time())),
            payload=dict(data.get("payload", {})),
        )


@dataclass
class IncomingMessage:
    """Normalized incoming message arriving from any interface/channel."""

    text: str
    channel: str = "cli"
    user_id: str = "local"
    conversation_id: str = "default"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    attachments: List[Any] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def session_id(self) -> str:
        """Isolated session identifier scoped by channel, user, and conversation."""
        return f"{self.channel}:{self.user_id}:{self.conversation_id}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IncomingMessage":
        return cls(
            text=data.get("text", ""),
            channel=data.get("channel", "cli"),
            user_id=data.get("user_id", "local"),
            conversation_id=data.get("conversation_id", "default"),
            id=data.get("id") or str(uuid.uuid4()),
            timestamp=float(data.get("timestamp", time.time())),
            attachments=list(data.get("attachments", [])),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class OutgoingMessage:
    """Normalized outgoing response returned from the BrainFrog runtime."""

    text: str
    attachments: List[Any] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    events: List[AgentEvent] = field(default_factory=list)
    success: bool = True
    status: str = "completed"
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        res = asdict(self)
        res["events"] = [e.to_dict() if isinstance(e, AgentEvent) else e for e in self.events]
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OutgoingMessage":
        events_raw = data.get("events", [])
        events = [
            AgentEvent.from_dict(e) if isinstance(e, dict) else e
            for e in events_raw
        ]
        return cls(
            text=data.get("text", ""),
            attachments=list(data.get("attachments", [])),
            metadata=dict(data.get("metadata", {})),
            events=events,
            success=bool(data.get("success", True)),
            status=str(data.get("status", "completed")),
            error=data.get("error"),
        )
