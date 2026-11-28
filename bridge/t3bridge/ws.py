"""A minimal WebSocket (RFC 6455) for the bridge, standard library only: the server end
for Twilio's media stream, and a client for OpenAI's live transcription.

Text and close frames only matter here; pings are answered, binary frames are
returned like text. Fragmented messages are joined.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketClosed(Exception):
    pass


def _xor(payload: bytes, key: bytes) -> bytes:
    """Mask or unmask a payload (the same XOR both ways), a whole frame at a time."""
    n = len(payload)
    if not n:
        return payload
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


class WebSocket:
    """One open WebSocket over a file-like `rfile` (reads) and a socket-ish `send`
    (writes). `mask`: the client end masks what it sends (RFC 6455 §5.3)."""

    def __init__(self, rfile, write, mask: bool, close=None):
        self.rfile = rfile
        self._write = write
        self.mask = mask
        self._close = close
        self.lock = threading.Lock()  # one writer at a time
        self.closed = False

    def _read(self, n: int) -> bytes:
        data = self.rfile.read(n)
        if data is None or len(data) < n:
            raise WebSocketClosed("connection closed")
        return data

    def _frame(self, opcode: int, payload: bytes) -> bytes:
        head = bytes([0x80 | opcode])
        n = len(payload)
        bit = 0x80 if self.mask else 0
        if n < 126:
            head += bytes([bit | n])
        elif n < 1 << 16:
            head += bytes([bit | 126]) + struct.pack("!H", n)
        else:
            head += bytes([bit | 127]) + struct.pack("!Q", n)
        if self.mask:
            key = os.urandom(4)
            payload = _xor(payload, key)
            head += key
        return head + payload

    def _send(self, opcode: int, payload: bytes) -> None:
        with self.lock:
            if self.closed:
                raise WebSocketClosed("already closed")
            self._write(self._frame(opcode, payload))

    def send_text(self, text: str) -> None:
        self._send(0x1, text.encode())

    def recv(self) -> str:
        """The next text (or binary) message. Raises WebSocketClosed at the end."""
        parts = []
        while True:
            b0, b1 = self._read(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            key = self._read(4) if b1 & 0x80 else None
            payload = self._read(n)
            if key:
                payload = _xor(payload, key)
            if opcode == 0x8:  # close: answer it, then stop
                try:
                    self._send(0x8, payload[:2])
                except (OSError, WebSocketClosed):
                    pass
                self.closed = True
                raise WebSocketClosed("closed by the other end")
            if opcode == 0x9:  # ping
                self._send(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            parts.append(payload)
            if b0 & 0x80:  # final fragment
                return b"".join(parts).decode(errors="replace")

    def close(self) -> None:
        try:
            self._send(0x8, struct.pack("!H", 1000))
        except (OSError, WebSocketClosed):
            pass
        self.closed = True
        if self._close:
            try:
                self._close()
            except OSError:
                pass


def server_handshake(handler) -> WebSocket | None:
    """Upgrade a BaseHTTPRequestHandler's GET to a WebSocket, or None if it isn't one."""
    key = handler.headers.get("sec-websocket-key")
    if "websocket" not in handler.headers.get("upgrade", "").lower() or not key:
        return None
    # The status line must say HTTP/1.1: Twilio drops an "HTTP/1.0 101" (BaseHTTPRequestHandler's default).
    handler.protocol_version = "HTTP/1.1"
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(key))
    handler.end_headers()
    handler.wfile.flush()
    handler.close_connection = True

    def write(data: bytes) -> None:
        handler.wfile.write(data)
        handler.wfile.flush()

    return WebSocket(handler.rfile, write, mask=False)


def connect(url: str, headers: dict[str, str] | None = None, timeout: float = 15.0) -> WebSocket:
    """Open a client WebSocket to a ws:// or wss:// URL."""
    u = urlsplit(url)
    secure = u.scheme == "wss"
    port = u.port or (443 if secure else 80)
    sock = socket.create_connection((u.hostname, port), timeout=timeout)
    if secure:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
    key = base64.b64encode(os.urandom(16)).decode()
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    lines = [f"GET {path} HTTP/1.1", f"Host: {u.hostname}", "Upgrade: websocket",
             "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    rfile = sock.makefile("rb")
    status = rfile.readline().decode(errors="replace")
    response = {}
    while True:
        line = rfile.readline().decode(errors="replace").strip()
        if not line:
            break
        k, _, v = line.partition(":")
        response[k.strip().lower()] = v.strip()
    if not status.startswith("HTTP/1.1 101") or response.get("sec-websocket-accept") != accept_key(key):
        sock.close()
        raise WebSocketClosed(f"handshake refused: {status.strip()}")
    sock.settimeout(None)  # reads block until a message comes; close() ends them
    return WebSocket(rfile, sock.sendall, mask=True, close=sock.close)
