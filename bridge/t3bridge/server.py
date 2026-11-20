"""Device-facing HTTP/JSON API. See docs/bridge-api.md."""

from __future__ import annotations

import hmac
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .core import Bridge, BridgeError
from .images import ImageError

log = logging.getLogger("t3bridge.http")

MAX_WAIT = 120.0
MAX_BODY = 64 * 1024


def _int(q: dict, key: str, default=None, lo=None, hi=None):
    if key not in q:
        return default
    try:
        v = int(q[key][0])
    except ValueError:
        raise BridgeError(400, f"{key} must be an integer") from None
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _expand(q: dict) -> frozenset[str]:
    return frozenset(x for x in q.get("expand", [""])[0].split(",") if x)


def _flag(q: dict, key: str) -> bool:
    return q.get(key, ["0"])[0] in ("1", "true", "yes")


def _mode(body: dict) -> str | None:
    mode = body.get("mode")
    return mode if isinstance(mode, str) and mode else None


def make_handler(bridge: Bridge, device_token: str):
    routes = [
        ("GET", re.compile(r"^/v1/health$"), "health"),
        ("GET", re.compile(r"^/v1/threads$"), "list"),
        ("GET", re.compile(r"^/v1/threads/wait$"), "list_wait"),
        ("POST", re.compile(r"^/v1/threads$"), "start"),
        ("GET", re.compile(r"^/v1/projects$"), "projects"),
        ("GET", re.compile(r"^/v1/models$"), "models"),
        ("GET", re.compile(r"^/v1/images/(\w+)$"), "image"),
        ("GET", re.compile(r"^/v1/threads/(\w+)$"), "view"),
        ("GET", re.compile(r"^/v1/threads/(\w+)/wait$"), "wait"),
        ("POST", re.compile(r"^/v1/threads/(\w+)/reply$"), "reply"),
        ("POST", re.compile(r"^/v1/threads/(\w+)/settle$"), "settle"),
        ("POST", re.compile(r"^/v1/threads/(\w+)/secret$"), "secret"),
        ("POST", re.compile(r"^/v1/threads/(\w+)/model$"), "set_model"),
    ]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "t3bridge"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_bytes(self, data: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(data)))
            self.send_header("cache-control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if not device_token:
                return True
            header = self.headers.get("authorization", "")
            return hmac.compare_digest(header.encode(), f"Bearer {device_token}".encode())

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            if n > MAX_BODY:
                raise BridgeError(413, "body too large")
            if n == 0:
                return {}
            try:
                body = json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                raise BridgeError(400, "body is not JSON") from None
            if not isinstance(body, dict):
                raise BridgeError(400, "body must be a JSON object")
            return body

        def _dispatch(self, method: str) -> None:
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            for m, pattern, name in routes:
                match = pattern.match(url.path)
                if m == method and match:
                    try:
                        result = getattr(self, "r_" + name)(q, *match.groups())
                        if isinstance(result, tuple):  # (bytes, content type)
                            self._send_bytes(*result)
                        else:
                            self._send(200, result)
                    except (BridgeError, ImageError) as e:
                        self._send(e.status, {"error": str(e)})
                    except Exception as e:
                        log.exception("request failed")
                        self._send(502, {"error": f"T3 request failed: {e}"})
                    return
            self._send(404, {"error": "not found"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        # ---- routes ---------------------------------------------------------

        def r_health(self, q):
            return {"ok": True, "list_v": bridge.list_v}

        def r_list(self, q):
            project = q.get("project", [None])[0]
            return bridge.thread_list(limit=_int(q, "limit", 8, 1, 200), project=project)

        def r_list_wait(self, q):
            changed = bridge.wait_list(_int(q, "v", 0), _int(q, "timeout", 55, 0, MAX_WAIT))
            project = q.get("project", [None])[0]
            body = bridge.thread_list(limit=_int(q, "limit", 8, 1, 200), project=project)
            body["changed"] = changed
            return body

        def r_image(self, q, image_id):
            return bridge.images.get(image_id, _int(q, "w", 1000, 64, 4000))

        def r_projects(self, q):
            return bridge.projects()

        def r_models(self, q):
            return bridge.models(thread=q.get("thread", [None])[0], project=q.get("project", [None])[0])

        def r_set_model(self, q, short):
            b = self._body()
            if not isinstance(b.get("model"), str):
                raise BridgeError(400, "send `model`")
            return bridge.set_model(short, b["model"], client_msg_id=b.get("client_msg_id"),
                                    progress=bool(b.get("progress")))

        def r_view(self, q, short):
            return bridge.thread_view(short, after=_int(q, "after"), before=_int(q, "before"),
                                      turns=_int(q, "turns", 3, 1, 50),
                                      visit=_flag(q, "visit"), progress=_flag(q, "progress"),
                                      expand=_expand(q))

        def r_wait(self, q, short):
            changed, body = bridge.wait_view(
                short, q.get("v", [""])[0], _int(q, "timeout", 55, 0, MAX_WAIT),
                progress=_flag(q, "progress"), after=_int(q, "after"),
                turns=_int(q, "turns", 3, 1, 50), expand=_expand(q))
            body["changed"] = changed
            return body

        def r_reply(self, q, short):
            b = self._body()
            return bridge.reply(short, reply_id=b.get("reply"), text=b.get("text"),
                                client_msg_id=b.get("client_msg_id"), progress=bool(b.get("progress")),
                                expand=frozenset(x for x in b.get("expand") or [] if isinstance(x, str)),
                                mode=_mode(b))

        def r_secret(self, q, short):
            b = self._body()
            if b.get("decline") is True:
                return bridge.answer_secret(short, None)
            secret = b.get("secret")
            if not isinstance(secret, str):
                raise BridgeError(400, "send `secret` or `decline: true`")
            return bridge.answer_secret(short, secret)

        def r_settle(self, q, short):
            b = self._body()
            return bridge.settle(short, settled=b.get("settled", True) is not False)

        def r_start(self, q):
            b = self._body()
            model = b.get("model")
            return bridge.start_thread(str(b.get("project", "")), str(b.get("text", "")),
                                       client_msg_id=b.get("client_msg_id"),
                                       model=model if isinstance(model, str) and model else None,
                                       mode=_mode(b))

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Devices drop off Wi-Fi mid-request; that's routine, not worth a traceback.
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            log.info("%s: connection dropped (%s)", client_address[0], exc.__class__.__name__)
        else:
            super().handle_error(request, client_address)


def serve(bridge: Bridge, host: str, port: int, device_token: str) -> ThreadingHTTPServer:
    httpd = _Server((host, port), make_handler(bridge, device_token))
    httpd.daemon_threads = True
    return httpd
