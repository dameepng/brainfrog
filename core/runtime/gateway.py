"""BrainFrog Gateway — Multi-Channel Process Supervisor.

Coordinates BrainFrogRuntime and enabled external communication channels
(CLI, Telegram, WhatsApp) within a single coherent process.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.channels.base import BaseChannel
from core.channels.telegram import TelegramChannel
from core.channels.whatsapp import MockWhatsAppTransport, WhatsAppChannel, WhatsAppTransport

from .runtime import BrainFrogRuntime
from .session import session_manager


class BrainFrogGateway:
    """Process orchestrator managing BrainFrogRuntime and connected channels."""

    def __init__(
        self,
        runtime: Optional[BrainFrogRuntime] = None,
        channels: Optional[List[BaseChannel]] = None,
        repo_dir: Optional[Path] = None,
        auto_configure_channels: bool = True,
        whatsapp_transport: Optional[WhatsAppTransport] = None,
    ) -> None:
        self.repo_dir = (repo_dir or Path.cwd()).resolve()
        self.runtime = runtime or BrainFrogRuntime(repo_dir=self.repo_dir)
        self.channels: List[BaseChannel] = list(channels or [])

        if auto_configure_channels and not self.channels:
            # 1. Telegram Auto-Discovery
            tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            if tg_token:
                tg_channel = TelegramChannel(bot_token=tg_token)
                self.channels.append(tg_channel)

            # 2. WhatsApp Auto-Discovery
            wa_enabled = os.environ.get("WHATSAPP_ENABLED", "").lower().strip() in ("1", "true", "yes")
            if wa_enabled:
                wa_channel = WhatsAppChannel(transport=whatsapp_transport or MockWhatsAppTransport())
                self.channels.append(wa_channel)

        # Wire up runtime handler to each channel
        for ch in self.channels:
            ch.set_handler(self.runtime.handle_message)

        self._is_running = False

    @property
    def is_running(self) -> bool:
        return self._is_running

    def add_channel(self, channel: BaseChannel) -> None:
        channel.set_handler(self.runtime.handle_message)
        self.channels.append(channel)

    def start(self) -> None:
        """Start all configured communication channels."""
        if self._is_running:
            return
        self._is_running = True
        for ch in self.channels:
            try:
                ch.start()
            except Exception as e:
                sys.stderr.write(f"[BrainFrog Gateway] Failed to start channel '{ch.name}': {e}\n")

    def stop(self) -> None:
        """Gracefully stop all channels."""
        self._is_running = False
        for ch in self.channels:
            try:
                ch.stop()
            except Exception as e:
                sys.stderr.write(f"[BrainFrog Gateway] Error stopping channel '{ch.name}': {e}\n")

    def get_status(self) -> Dict[str, Any]:
        """Diagnostic snapshot of gateway status and active channels."""
        channels_info = []
        for ch in self.channels:
            channels_info.append({
                "name": ch.name,
                "running": ch.is_running,
            })

        return {
            "gateway_running": self._is_running,
            "workspace": str(self.repo_dir),
            "channels_count": len(self.channels),
            "channels": channels_info,
            "active_sessions_count": session_manager.count(),
            "default_backend": getattr(self.runtime, "default_backend", "jev"),
            "default_provider": getattr(self.runtime, "default_provider", "auto"),
        }

    def run_forever(self) -> None:
        """Run gateway loop until interrupted."""
        self.start()

        def _handle_exit(sig: int, frame: Any) -> None:
            self.stop()

        try:
            signal.signal(signal.SIGINT, _handle_exit)
            signal.signal(signal.SIGTERM, _handle_exit)
        except Exception:
            pass

        try:
            while self._is_running:
                time.sleep(0.5)
        except (KeyboardInterrupt, SystemExit):
            self.stop()
