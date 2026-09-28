"""Generic stdio Model Context Protocol (MCP) client.

Layer 1: Protocol transport layer for stdio-based MCP servers.
This module is completely generic and has no domain knowledge of quality gates,
browser navigation, or specific tool names. It implements:
- stdio process lifecycle management
- MCP JSON-RPC 2.0 handshake (initialize & notifications/initialized)
- tools/list discovery
- tools/call execution with timeout and response normalization (text & image blocks)
- clean process shutdown and resource cleanup
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional


class McpToolResult:
    """Normalized response from an MCP tool call."""

    def __init__(self, raw_response: Dict[str, Any]):
        self.raw = raw_response
        result_payload = raw_response.get("result", {})
        self.content: List[Dict[str, Any]] = result_payload.get("content", [])
        self.is_error: bool = bool(
            result_payload.get("isError", False) or ("error" in raw_response)
        )
        self.error_message: Optional[str] = None
        if "error" in raw_response:
            err = raw_response["error"]
            self.error_message = err.get("message") if isinstance(err, dict) else str(err)

    @property
    def text(self) -> str:
        """Concatenated text from all text content blocks."""
        texts = [
            c.get("text", "")
            for c in self.content
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        return "\n".join(texts).strip()

    @property
    def image_base64(self) -> Optional[str]:
        """First base64 image data found in content blocks, if any."""
        for c in self.content:
            if isinstance(c, dict) and c.get("type") == "image":
                data = c.get("data")
                if data:
                    return str(data)
        return None

    @property
    def parsed_json(self) -> Optional[Any]:
        """Attempt to parse the response text as JSON."""
        txt = self.text.strip()
        if not txt:
            return None
        try:
            return json.loads(txt)
        except Exception:
            return None

    def __repr__(self) -> str:
        status = "ERROR" if self.is_error else "OK"
        summary = self.text[:80] if self.text else f"{len(self.content)} content blocks"
        return f"<McpToolResult [{status}]: {summary}>"


class McpClient:
    """Generic client for any standard Model Context Protocol (MCP) server over stdio."""

    def __init__(
        self,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        client_name: str = "brainfrog-mcp-client",
        client_version: str = "1.0.0",
        protocol_version: str = "2024-11-05",
    ) -> None:
        self.command = command
        self.args = args or []
        self.env = env
        self.cwd = cwd
        self.client_name = client_name
        self.client_version = client_version
        self.protocol_version = protocol_version

        self.proc: Optional[subprocess.Popen] = None
        self._next_id: int = 1
        self._lock = threading.Lock()
        self._pending_requests: Dict[int, queue.Queue] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._server_info: Dict[str, Any] = {}
        self._server_capabilities: Dict[str, Any] = {}

    @property
    def is_connected(self) -> bool:
        return bool(self._running and self.proc and self.proc.poll() is None)

    @property
    def server_info(self) -> Dict[str, Any]:
        return self._server_info

    def connect(self, timeout: float = 15.0) -> Dict[str, Any]:
        """Spawn the MCP server process and perform the standard MCP handshake."""
        if self.is_connected:
            return self._server_info

        merged_env = os.environ.copy()
        if self.env:
            merged_env.update(self.env)

        full_cmd = [self.command, *self.args]
        self.proc = subprocess.Popen(
            full_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=merged_env,
            cwd=self.cwd,
        )

        self._running = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"mcp-reader-{self.client_name}",
            daemon=True,
        )
        self._reader_thread.start()

        # Step 1: Send initialize request
        init_params = {
            "protocolVersion": self.protocol_version,
            "capabilities": {},
            "clientInfo": {
                "name": self.client_name,
                "version": self.client_version,
            },
        }

        try:
            init_response = self._send_request("initialize", init_params, timeout=timeout)
        except Exception as e:
            self.disconnect()
            raise ConnectionError(
                f"Failed MCP initialize handshake with command '{self.command}': {e}"
            ) from e

        result = init_response.get("result", {})
        self._server_info = result.get("serverInfo", {})
        self._server_capabilities = result.get("capabilities", {})

        # Step 2: Send initialized notification
        self._send_notification("notifications/initialized", {})

        return self._server_info

    def list_tools(self, timeout: float = 10.0) -> List[Dict[str, Any]]:
        """Query the MCP server for available tools via tools/list."""
        response = self._send_request("tools/list", {}, timeout=timeout)
        result = response.get("result", {})
        return result.get("tools", [])

    def call_tool(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> McpToolResult:
        """Call a specific tool via tools/call and return normalized McpToolResult."""
        params: Dict[str, Any] = {
            "name": name,
            "arguments": arguments or {},
        }
        response = self._send_request("tools/call", params, timeout=timeout)
        return McpToolResult(response)

    def disconnect(self) -> None:
        """Terminate the server process and clean up reader thread."""
        self._running = False

        if self.proc:
            try:
                if self.proc.stdin and not self.proc.stdin.closed:
                    self.proc.stdin.close()
            except Exception:
                pass

            try:
                self.proc.terminate()
                self.proc.wait(timeout=2.0)
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=1.0)
                except Exception:
                    pass

            self.proc = None

        # Clear any remaining pending queues
        with self._lock:
            for q in self._pending_requests.values():
                try:
                    q.put_nowait({"error": {"code": -1, "message": "Client disconnected"}})
                except Exception:
                    pass
            self._pending_requests.clear()

    def _reader_loop(self) -> None:
        """Background thread reading newline-delimited JSON-RPC from server stdout."""
        while self._running and self.proc and self.proc.stdout:
            try:
                line = self.proc.stdout.readline()
            except Exception:
                break

            if not line:
                # EOF reached (process closed stdout)
                break

            stripped = line.strip()
            if not stripped:
                continue

            try:
                message = json.loads(stripped)
            except json.JSONDecodeError:
                continue

            req_id = message.get("id")
            if req_id is not None:
                with self._lock:
                    pending_queue = self._pending_requests.get(req_id)
                if pending_queue is not None:
                    try:
                        pending_queue.put_nowait(message)
                    except Exception:
                        pass

    def _send_notification(self, method: str, params: Dict[str, Any]) -> None:
        """Send a one-way JSON-RPC notification (no response expected)."""
        if not self.proc or not self.proc.stdin or self.proc.stdin.closed:
            raise ConnectionError("Cannot send notification: MCP client is not connected.")

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        raw_bytes = (json.dumps(payload) + "\n").encode("utf-8")

        with self._lock:
            try:
                self.proc.stdin.buffer.write(raw_bytes)
                self.proc.stdin.buffer.flush()
            except Exception as e:
                raise ConnectionError(f"Failed writing notification to MCP process stdin: {e}") from e

    def _send_request(
        self,
        method: str,
        params: Dict[str, Any],
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Send a JSON-RPC request and block waiting for matching response id."""
        if not self.proc or not self.proc.stdin or self.proc.stdin.closed:
            raise ConnectionError("Cannot send request: MCP client is not connected.")

        response_queue: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending_requests[request_id] = response_queue

            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            raw_bytes = (json.dumps(payload) + "\n").encode("utf-8")

            try:
                self.proc.stdin.buffer.write(raw_bytes)
                self.proc.stdin.buffer.flush()
            except Exception as e:
                self._pending_requests.pop(request_id, None)
                raise ConnectionError(f"Failed writing request to MCP process stdin: {e}") from e

        try:
            response = response_queue.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending_requests.pop(request_id, None)
            raise TimeoutError(
                f"MCP request '{method}' (id={request_id}) timed out after {timeout:.1f}s."
            )

        if "error" in response:
            err = response["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(f"MCP server returned error for '{method}': {msg}")

        return response

    def __enter__(self) -> "McpClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()
