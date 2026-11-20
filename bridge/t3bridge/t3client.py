"""Minimal client for the T3 Code server API (stdlib only).

Reads go over plain HTTP (`/api/orchestration/...`). Writes go over the
WebSocket RPC socket (`/ws`), which speaks Effect RPC with JSON serialization.
See docs/t3-api.md for the protocol notes this is based on.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

ORCHESTRATION_PROTOCOL = "2"


class T3Error(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status, when the error came from an HTTP response


class T3SocketError(T3Error):
    """The WebSocket closed or broke; safe to reconnect."""


class T3RpcError(T3Error):
    def __init__(self, method: str, exit_value: Any):
        self.method = method
        self.exit = exit_value
        super().__init__(f"{method} failed: {json.dumps(exit_value)[:500]}")


def new_id() -> str:
    return str(uuid.uuid4())


class T3Client:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._ws: _WebSocket | None = None
        self._next_rpc_id = 0
        self._rpc_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "T3Client":
        url = os.environ.get("T3_URL", "http://127.0.0.1:3773")
        token = os.environ.get("T3_TOKEN")
        if not token:
            raise T3Error("T3_TOKEN is not set (see .env.example)")
        return cls(url, token)

    # ---- HTTP ---------------------------------------------------------------

    def _http(self, method: str, path: str, query: dict | None = None, body: Any = None) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("authorization", f"Bearer {self.token}")
        req.add_header("x-t3-orchestration-protocol", ORCHESTRATION_PROTOCOL)
        if data is not None:
            req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:500].decode(errors="replace")
            raise T3Error(f"{method} {path} -> HTTP {e.code}: {detail}", status=e.code) from None
        return json.loads(raw) if raw else None

    def set_token(self, token: str) -> None:
        """Switch to a new token; the WebSocket reconnects with it on next use."""
        self.token = token
        self.close()

    def auth_session(self) -> dict:
        """{authenticated, scopes, expiresAt, ...} for the current token."""
        return self._http("GET", "/api/auth/session")

    def environment(self) -> dict:
        return self._http("GET", "/.well-known/t3/environment")

    def shell_snapshot(self) -> dict:
        """All projects and (non-archived) thread summaries, with status."""
        return self._http("GET", "/api/orchestration/shell")

    def server_config(self) -> dict:
        """Server config, including the user's settings (default model and runtime mode)."""
        return self.rpc("server.getConfig", {})

    def thread_snapshot(self, thread_id: str) -> dict:
        """Full projection: runs, messages, runtimeRequests, turnItems, ..."""
        return self._http("GET", f"/api/orchestration/threads/{_q(thread_id)}")

    def thread_bounded(self, thread_id: str) -> dict:
        """A size-bounded snapshot: the most recent turn items (`projection.visibleTurnItems`),
        plus a cursor for older history."""
        return self._http("GET", f"/api/orchestration/threads/{_q(thread_id)}/bounded")

    def thread_transcript(self, thread_id: str) -> dict:
        """Ordered turn items (messages, tool calls, ...) with `position`."""
        return self._http("GET", f"/api/orchestration/threads/{_q(thread_id)}/transcript")

    # ---- WebSocket RPC ------------------------------------------------------

    def _socket(self) -> "_WebSocket":
        if self._ws is None:
            ticket = self._http("POST", "/api/auth/websocket-ticket/rpc")["ticket"]
            qs = urllib.parse.urlencode(
                {"orchestrationProtocol": ORCHESTRATION_PROTOCOL, "wsTicket": ticket}
            )
            self._ws = _WebSocket.connect(f"{self.base_url}/ws?{qs}", self.timeout)
        return self._ws

    def rpc(self, method: str, payload: Any) -> Any:
        """Call a unary RPC and return its success value.

        Reconnects once if the socket has died. That makes a retry possible after the
        server already ran the command, so callers should pass a stable `commandId`:
        T3 answers a repeated commandId with the original receipt instead of running it twice.
        """
        with self._rpc_lock:
            try:
                return self._rpc_once(method, payload)
            except (OSError, T3SocketError):
                self.close()
                return self._rpc_once(method, payload)

    def _rpc_once(self, method: str, payload: Any) -> Any:
        ws = self._socket()
        self._next_rpc_id += 1
        rid = str(self._next_rpc_id)
        ws.send_text(
            json.dumps(
                {"_tag": "Request", "id": rid, "tag": method, "payload": payload, "headers": []}
            )
        )
        while True:
            for msg in _decode_frame(ws.recv_text()):
                tag = msg.get("_tag")
                if tag == "Ping":
                    ws.send_text(json.dumps({"_tag": "Pong"}))
                elif tag == "Exit" and str(msg.get("requestId")) == rid:
                    exit_ = msg["exit"]
                    if exit_.get("_tag") == "Success":
                        return exit_.get("value")
                    raise T3RpcError(method, exit_)
                elif tag == "Defect":
                    raise T3RpcError(method, msg)
                # Chunks/acks for other requests are ignored: this client is unary-only.

    def dispatch(self, command: dict) -> dict:
        command.setdefault("commandId", new_id())
        return self.rpc("orchestration.dispatchCommand", command)

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    # ---- Commands -----------------------------------------------------------

    def launch_thread(
        self,
        project_id: str,
        title: str,
        text: str,
        model_selection: dict,
        runtime_mode: str = "approval-required",
        interaction_mode: str = "default",
        command_id: str | None = None,
        message_id: str | None = None,
        generate_title: bool = False,
    ) -> dict:
        """Create a thread in the project root and send its first message."""
        return self.rpc(
            "orchestration.launchThread",
            {
                "commandId": command_id or new_id(),
                "generateTitle": generate_title,
                "creationSource": "mobile",
                "projectId": project_id,
                "title": title,
                "modelSelection": model_selection,
                "runtimeMode": runtime_mode,
                "interactionMode": interaction_mode,
                "workspaceStrategy": {"type": "root"},
                "initialMessage": {"messageId": message_id or new_id(), "text": text, "attachments": []},
            },
        )

    def send_message(self, thread_id: str, text: str, queue: bool = False,
                     command_id: str | None = None, message_id: str | None = None) -> dict:
        """Send a user message, as the composer does. `queue` waits for the active run."""
        return self.dispatch(
            {
                "type": "message.dispatch",
                "commandId": command_id or new_id(),
                "createdBy": "user",
                "creationSource": "mobile",
                "threadId": thread_id,
                "messageId": message_id or new_id(),
                "text": text,
                "attachments": [],
                **({} if queue else {"deliveryIntent": "auto"}),
                "dispatchMode": {"type": "queue_after_active" if queue else "start_immediately"},
            }
        )

    def delete_thread(self, thread_id: str) -> dict:
        return self.dispatch({"type": "thread.delete", "threadId": thread_id})

    def settle_thread(self, thread_id: str) -> dict:
        """Mark a thread handled, as the app's Settle does."""
        return self.dispatch({"type": "thread.settle", "threadId": thread_id})

    def unsettle_thread(self, thread_id: str) -> dict:
        return self.dispatch({"type": "thread.unsettle", "threadId": thread_id, "reason": "user"})

    def stop_thread(self, thread_id: str, command_id: str | None = None) -> dict:
        """What the web app's Stop sends when the server has the threadStop capability."""
        return self.dispatch({"type": "thread.stop", "commandId": command_id or new_id(),
                              "threadId": thread_id})

    def interrupt_run(self, thread_id: str, run_id: str, command_id: str | None = None) -> dict:
        """Stop for servers without threadStop: interrupt the active run, hold the queue."""
        return self.dispatch({"type": "run.interrupt", "commandId": command_id or new_id(),
                              "threadId": thread_id, "runId": run_id, "holdQueue": True})

    def visit_thread(self, thread_id: str, visited_at: str) -> dict:
        """Mark the thread read, as opening it in the app does."""
        return self.dispatch({"type": "thread.visit", "threadId": thread_id,
                              "visitedAt": visited_at, "opened": True})

    def respond_approval(self, thread_id: str, request_id: str, decision: str,
                         command_id: str | None = None) -> dict:
        """decision: accept | acceptForSession | acceptAlways | decline | cancel"""
        return self.dispatch(
            {
                "type": "runtime-request.respond",
                "commandId": command_id or new_id(),
                "threadId": thread_id,
                "requestId": request_id,
                "decision": decision,
            }
        )

    def respond_user_input(self, thread_id: str, request_id: str, answers: dict,
                           command_id: str | None = None) -> dict:
        """answers: {question id: option label, or a list of labels for multiSelect}"""
        return self.dispatch(
            {
                "type": "runtime-request.respond",
                "commandId": command_id or new_id(),
                "threadId": thread_id,
                "requestId": request_id,
                "answers": answers,
            }
        )

    def pending_requests(self, thread_id: str) -> list[dict]:
        """Pending approvals and questions, joined the way the web app does
        (client-runtime derivePendingThreadRequests)."""
        return pending_requests_from(self.thread_snapshot(thread_id)["projection"])


def pending_requests_from(proj: dict) -> list[dict]:
    """[{request, item}] for each pending request in a thread projection."""
    items = proj["turnItems"]
    out = []
    for req in proj["runtimeRequests"]:
        if req["status"] != "pending" or req["kind"] in ("auth_refresh", "dynamic_tool_call"):
            continue
        want = "user_input_request" if req["kind"] == "user_input" else "approval_request"
        item = next(
            (i for i in reversed(items) if i.get("type") == want and i.get("requestId") == req["id"]),
            None,
        )
        if req["kind"] == "user_input" and item is None:
            continue
        out.append({"request": req, "item": item})
    return out


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="")


def _decode_frame(text: str) -> list[dict]:
    decoded = json.loads(text)
    return decoded if isinstance(decoded, list) else [decoded]


class _WebSocket:
    """Just enough RFC 6455 for a text-frame client."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buf = b""

    @classmethod
    def connect(cls, http_url: str, timeout: float) -> "_WebSocket":
        u = urllib.parse.urlsplit(http_url)
        secure = u.scheme in ("https", "wss")
        port = u.port or (443 if secure else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + ("?" + u.query if u.query else "")
        host = u.hostname if u.port is None else f"{u.hostname}:{u.port}"
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        ws = cls(sock)
        head = ws._read_until(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            body = ws._buf[:300].decode(errors="replace")
            sock.close()
            raise T3Error(f"WebSocket upgrade failed: {status} {body}")
        return ws

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise T3Error("connection closed during handshake")
            self._buf += chunk
        head, _, self._buf = self._buf.partition(marker)
        return head

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise T3SocketError("WebSocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode())

    def recv_text(self) -> str:
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if b2 & 0x80 else None
            data = self._read_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:  # ping
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise T3SocketError(f"WebSocket closed by server: {data[2:].decode(errors='replace')}")
            parts.append(data)
            if fin:
                return b"".join(parts).decode()

    def close(self) -> None:
        try:
            self._send_frame(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        self.sock.close()
