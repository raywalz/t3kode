"""Voice replies by phone call, through Twilio.

Two kinds of call. A reply call takes one message for one thread (or a new thread)
and ends. A microphone call (POST /v1/mic) stays up: each phrase you say becomes a
"text" event the device puts into whatever text box is open, and 1 on the phone (or
ending a phrase with "send it") a "send" event, until you hang up.

The device asks for a call (POST /v1/threads/<id>/call, or /v1/projects/<id>/call for a
new thread's first message). Twilio rings your phone, you
talk, and Twilio's speech recognition turns it into text. Press 1 to send it, or hang
up (or press #) and it waits on the device as a draft to edit. 9 starts over.

Twilio reaches the bridge through webhooks on their own listener (VOICE_PORT), so only
`/voice/<secret>/…` has to be public; the device API can stay private. The secret in
the path is what guards them, so it is never logged.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape, quoteattr

from .core import BridgeError

log = logging.getLogger("t3bridge.voice")

SAY_VOICE = "Polly.Joanna-Neural"
RING_SECONDS = 25  # hang up before most phones send it to voicemail
# Seconds of silence that end one stretch of speech. Each stretch is posted to the bridge
# and listening starts again at once, silently, so a pause costs nothing; a longer
# setting just means fewer seams, where a word or two could fall between stretches.
SPEECH_PAUSE = 5
# Seconds of complete silence before Twilio reports back with nothing; the bridge then
# listens again, saying nothing. After QUIET_LIMIT of those in a row (about 3 minutes
# of silence), it hangs up and leaves what was said as a draft.
LISTEN_SECONDS = 30
QUIET_LIMIT = 6
# A microphone call stays up through silence for about 15 minutes (30 × 30 s).
MIC_QUIET_LIMIT = 30
KEEP_CALLS = 6 * 3600.0  # forget calls this many seconds after they started
LIVE = ("ringing", "listening", "ending", "sending")
# All the call says before you talk; the e-reader shows the keys. It never talks over you.
GO = "Go ahead."
HELP = "Press 1 to send. Hang up or press pound to finish on your e-reader. Press 9 to start over."


_SEND_IT = re.compile(r"^(.*?)[\s,.;:!?-]*\bsend it\W*$", re.I | re.S)


def split_send(speech: str) -> tuple[str, bool]:
    """A phrase ending in "send it" means send: return (the words before it, True)."""
    m = _SEND_IT.match(speech.strip())
    if m:
        return m.group(1).strip(), True
    return speech.strip(), False


class TwilioError(Exception):
    pass


class Twilio:
    """The two REST calls the bridge makes: place a call, and hang one up."""

    def __init__(self, account_sid: str, api_key: str, api_secret: str, timeout: float = 20.0):
        self.base = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/"
        self.auth = "Basic " + base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self.timeout = timeout

    def post(self, path: str, form: dict) -> dict:
        req = urllib.request.Request(self.base + path, data=urllib.parse.urlencode(form).encode(),
                                     method="POST")
        req.add_header("Authorization", self.auth)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            try:
                text = json.loads(text).get("message", text)
            except (json.JSONDecodeError, AttributeError):
                pass
            raise TwilioError(f"Twilio HTTP {e.code}: {text}") from None
        except urllib.error.URLError as e:
            raise TwilioError(f"can't reach Twilio: {e.reason}") from None

    def call(self, to: str, from_: str, url: str, status_url: str) -> str:
        """Ring `to`. Returns the call's SID."""
        return self.post("Calls.json", {"To": to, "From": from_, "Url": url,
                                        "StatusCallback": status_url,
                                        "Timeout": str(RING_SECONDS)})["sid"]

    def hang_up(self, sid: str) -> None:
        self.post(f"Calls/{sid}.json", {"Status": "completed"})

    def redirect(self, sid: str, xml: str) -> None:
        """Give a call in progress new TwiML (to say something, or hang up)."""
        self.post(f"Calls/{sid}.json", {"Twiml": xml})


def twiml(*parts: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(parts) + "</Response>"


def say(text: str) -> str:
    return f"<Say voice={quoteattr(SAY_VOICE)}>{escape(text)}</Say>"


class Voice:
    # Hanging up from the device: Twilio still holds the speech since the last pause and
    # posts it just after the call ends. Wait this long after Twilio says the call is over
    # for it (and at most ending_wait in all) before making the draft.
    late_wait = 1.5
    ending_wait = 6.0

    def __init__(self, bridge, twilio, *, my_number: str, from_number: str, public_url: str,
                 secret: str, speech_model: str = "deepgram_nova-3", language: str = "en-US",
                 openai_key: str = "", openai_model: str = "gpt-live-transcribe",
                 transcriber: str = "", live_session=None):
        # Transcription: "twilio" (Twilio's own speech recognition, phrase by phrase) or
        # "openai" (the call's audio streamed to OpenAI live; see live.py). A device can
        # ask for either per call. The default: OpenAI when its key is set (it's faster and
        # more accurate), unless `transcriber` says "twilio".
        self.openai_key = openai_key
        self.openai_model = openai_model
        self.transcriber = "openai" if openai_key and transcriber != "twilio" else "twilio"
        self.live_session = live_session  # for tests: makes a live.LiveSession-like object
        self.bridge = bridge
        self.twilio = twilio
        self.my_number = my_number
        self.from_number = from_number
        self.public_url = public_url.rstrip("/")
        self.secret = secret
        self.speech_model = speech_model
        self.language = language
        self.cond = threading.Condition()
        self.calls: dict[str, dict] = {}

    # ---- device side ------------------------------------------------------------

    @property
    def transcribers(self) -> list[str]:
        return ["twilio", "openai"] if self.openai_key else ["twilio"]

    def _transcriber(self, asked: str | None) -> str:
        if asked in (None, ""):
            return self.transcriber
        if asked not in ("twilio", "openai"):
            raise BridgeError(400, "transcriber is `twilio` or `openai`")
        if asked not in self.transcribers:
            raise BridgeError(400, "OpenAI transcription isn't set up on the bridge (VOICE_OPENAI_API_KEY)")
        return asked

    def start(self, short: str, transcriber: str | None = None) -> dict:
        """Ring the phone for a reply to thread `short`."""
        self.bridge._thread(short)  # 404 for an unknown thread
        return self._place({"thread": short, "project": None, "intro": GO,
                            "transcriber": self._transcriber(transcriber)})

    def start_mic(self, transcriber: str | None = None) -> dict:
        """Ring the phone and keep the call up as a microphone for the device."""
        return self._place({"thread": None, "project": None, "mic": True, "intro": "Listening.",
                            "transcriber": self._transcriber(transcriber)})

    def start_new(self, project: str, model: str | None = None, mode: str | None = None,
                  transcriber: str | None = None) -> dict:
        """Ring the phone for the first message of a new thread in `project`. Pressing 1
        starts the thread with `model` and `mode` (as POST /v1/threads takes them)."""
        pid = self.bridge.by_short.get(project)
        p = next((x for x in (self.bridge.shell or {}).get("projects", []) if x["id"] == pid), None)
        if p is None:
            raise BridgeError(404, "no such project")
        if mode not in (None, "default", "plan"):
            raise BridgeError(400, "mode is `plan` or `default`")
        return self._place({"thread": None, "project": project, "model": model, "mode": mode,
                            "intro": f"New thread. {GO}", "transcriber": self._transcriber(transcriber)})

    def _place(self, call: dict) -> dict:
        cid = secrets.token_hex(8)
        call.update({"id": cid, "sid": None, "status": "ringing", "segments": [], "error": None, "v": 1,
                     "started": time.monotonic(), "quiet": 0, "events": []})
        call.setdefault("mic", False)
        call.setdefault("transcriber", self.transcriber)
        with self.cond:
            now = time.monotonic()
            for old in [k for k, c in self.calls.items() if now - c["started"] > KEEP_CALLS]:
                del self.calls[old]
            self.calls[cid] = call
        try:
            sid = self.twilio.call(self.my_number, self.from_number, self._hook("answer", cid),
                                   self._hook("status", cid))
        except Exception as e:
            log.warning("couldn't place a call: %s", e)
            with self.cond:
                del self.calls[cid]
            raise BridgeError(502, f"couldn't place the call: {e}") from None
        with self.cond:
            if call["sid"] is None:
                call["sid"] = sid
            return self._public(call)

    def wait(self, cid: str, v: int, timeout: float, after: int = 0) -> dict:
        """The call's state once it differs from version `v` (or after `timeout`).
        `after`: for a microphone call, return only the events numbered above it."""
        deadline = time.monotonic() + timeout
        with self.cond:
            call = self._call(cid)
            while call["v"] == v:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            return self._public(call, after)

    def cancel(self, cid: str) -> dict:
        """Hang up from the device. Like hanging up the phone: what was said is kept as a
        draft. The call goes `ending` while the last words arrive, then `draft` (or
        `empty`); a call not answered yet is `cancelled` at once."""
        with self.cond:
            call = self._call(cid)
            # Not while sending: pressing 1 already decided this call.
            live, sid = call["status"] in ("ringing", "listening"), call["sid"]
            if call["status"] == "ringing":
                self._set(call, "cancelled")
            elif live:
                self._set(call, "ending")
                self._finish_later(call, self.ending_wait)
        if live and sid:
            try:
                self.twilio.hang_up(sid)
            except Exception as e:
                log.warning("couldn't hang up: %s", e)
        with self.cond:
            return self._public(call)

    def _finish_later(self, call: dict, delay: float) -> None:
        """Turn an `ending` call into its draft after `delay` seconds."""
        def finish():
            with self.cond:
                if call["status"] == "ending":
                    self._set(call, self._final(call))
        timer = threading.Timer(delay, finish)
        timer.daemon = True
        timer.start()

    def _call(self, cid: str) -> dict:
        call = self.calls.get(cid)
        if call is None:
            raise BridgeError(404, "no such call")
        return call

    @staticmethod
    def _text(call: dict) -> str:
        return " ".join(s for s in call["segments"] if s).strip()

    def _final(self, call: dict) -> str:
        """How a call ends when it simply stops: a microphone call `ended`, a reply call
        as a `draft` of what was said (or `empty`)."""
        if call["mic"]:
            return "ended"
        return "draft" if self._text(call) else "empty"

    def _event(self, call: dict, kind: str, text: str | None = None) -> None:
        n = call["events"][-1]["n"] + 1 if call["events"] else 1
        call["events"].append({"n": n, "kind": kind, **({"text": text} if text else {})})
        del call["events"][:-200]  # a device that fell that far behind starts over

    def _public(self, call: dict, after: int = 0) -> dict:
        body = {"id": call["id"], "thread": call["thread"], "project": call["project"],
                "status": call["status"],
                "text": self._text(call), "parts": len(call["segments"]), "error": call["error"],
                "v": call["v"], "mic": call["mic"]}
        if call["mic"]:
            body["events"] = [e for e in call["events"] if e["n"] > after]
        return body

    def _set(self, call: dict, status: str | None = None, error: str | None = None) -> None:
        """Change a call (with self.cond held) and wake its waiters."""
        if status:
            call["status"] = status
        if error:
            call["error"] = error
        call["v"] += 1
        self.cond.notify_all()

    # ---- Twilio side ------------------------------------------------------------

    def _hook(self, action: str, cid: str) -> str:
        return f"{self.public_url}/voice/{self.secret}/{action}?id={cid}"

    def _listen(self, call: dict, prompt: str = "") -> str:
        """TwiML to say `prompt` (if any) and listen: Twilio's Gather, or the call's audio
        streamed to the bridge for OpenAI to transcribe."""
        if call["transcriber"] != "openai":
            return self._gather(call["id"], prompt)
        url = "wss://" + self.public_url.split("://", 1)[1] + f"/voice/{self.secret}/stream"
        # Twilio drops a query string from a stream URL: the call id goes as a parameter.
        return ((say(prompt) if prompt else "") + f"<Connect><Stream url={quoteattr(url)}>"
                f"<Parameter name=\"call\" value={quoteattr(call['id'])}/></Stream></Connect>")

    def _gather(self, cid: str, prompt: str = "") -> str:
        """Listen for speech or a key. A key press cuts the prompt short."""
        attrs = {"input": "dtmf speech", "action": self._hook("gather", cid), "method": "POST",
                 "speechModel": self.speech_model, "language": self.language,
                 # With speechModel set, Twilio wants a number here, not "auto".
                 "speechTimeout": str(SPEECH_PAUSE), "timeout": str(LISTEN_SECONDS), "numDigits": "1",
                 # Empty, so # comes through as a key like the others.
                 "finishOnKey": "", "actionOnEmptyResult": "true"}
        return ("<Gather " + " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items()) + ">"
                + (say(prompt) if prompt else "") + "</Gather>")

    def webhook(self, action: str, cid: str, form: dict) -> str | None:
        """TwiML for one Twilio request, or None for a request that isn't ours."""
        with self.cond:
            call = self.calls.get(cid)
            if call is None or action not in ("answer", "gather", "status"):
                return None
            sid = form.get("CallSid", "")
            if call["sid"] is None and action == "answer":
                call["sid"] = sid  # the answer beat start() storing the SID
            if not sid or sid != call["sid"]:
                return None
            if action == "status":
                self._status(call, form.get("CallStatus", ""))
                return ""
            if call["status"] == "ending":  # hung up from the device: keep its last words
                speech, _send = split_send(form.get("SpeechResult") or "")
                if action == "gather" and speech:
                    if call["mic"]:
                        self._event(call, "text", speech)
                    else:
                        call["segments"].append(speech)
                    self._set(call)
                return twiml("<Hangup/>")
            if call["status"] not in ("ringing", "listening"):
                return twiml(say("This call is over. Bye."), "<Hangup/>")
            if action == "answer":
                self._set(call, "listening")
                return twiml(self._listen(call, call["intro"]))
            if call["mic"]:
                return self._mic(call, form)
            digits = form.get("Digits", "")
            speech, send = split_send(form.get("SpeechResult") or "")
            if speech or send:
                call["quiet"] = 0
                if speech:
                    call["segments"].append(speech)
                    self._set(call)
                if not send:
                    return twiml(self._listen(call))  # keep listening, without a word
                digits = "1"  # "… send it": as pressing 1
            if digits == "1":
                text = self._text(call)
                if not text:
                    return twiml(self._listen(call, "Nothing to send yet. Go ahead."))
                self._set(call, "sending")
            elif digits == "#":
                self._set(call, self._final(call))
                return twiml(say("Okay, it's on your e-reader. Bye." if self._text(call) else "Okay. Bye."),
                             "<Hangup/>")
            elif digits == "9":
                call["segments"].clear()
                self._set(call)
                return twiml(self._listen(call, "Cleared. Start again."))
            elif digits:
                return twiml(self._listen(call, HELP))
            else:  # silence
                call["quiet"] += 1
                if call["quiet"] >= QUIET_LIMIT:
                    self._set(call, self._final(call))
                    return twiml(say("I'll leave it on your e-reader. Bye."), "<Hangup/>")
                return twiml(self._listen(call))  # thinking is fine: listen again, silently
            target = dict(call)
        # Sending waits on T3 (a second or two), so not under the lock.
        try:
            if target["project"]:
                view = self.bridge.start_thread(target["project"], text, client_msg_id=f"call-{cid}",
                                                model=target["model"], mode=target["mode"])
                with self.cond:
                    call["thread"] = view["id"]
            else:
                self.bridge.reply(target["thread"], text=text, client_msg_id=f"call-{cid}", deliver="queue")
        except Exception as e:
            log.warning("couldn't send a voice reply: %s", e)
            with self.cond:
                self._set(call, "draft", error=f"couldn't send it: {e}")
            return twiml(say("I couldn't send it. It's on your e-reader instead. Bye."), "<Hangup/>")
        with self.cond:
            self._set(call, "sent")
        return twiml(say("Started. Bye." if target["project"] else "Sent. Bye."), "<Hangup/>")

    def _mic(self, call: dict, form: dict) -> str:
        """A microphone call's phrase or key (self.cond held): it becomes an event for the
        device. The call never answers back; it just listens again."""
        digits = form.get("Digits", "")
        speech, send = split_send(form.get("SpeechResult") or "")
        if speech or send or digits:
            call["quiet"] = 0
        if speech:
            self._event(call, "text", speech)
        if send or digits == "1":
            self._event(call, "send")
        elif digits == "9":
            self._event(call, "clear")
        if speech or send or digits in ("1", "9"):
            self._set(call)
        elif not digits:  # silence
            call["quiet"] += 1
            if call["quiet"] >= MIC_QUIET_LIMIT:
                self._set(call, "ended")
                return twiml(say("Hanging up. Bye."), "<Hangup/>")
        return twiml(self._listen(call))

    # ---- OpenAI live transcription: Twilio streams the call's audio here ----------

    def stream(self, sock) -> None:
        """Serve one Twilio media stream (a WebSocket) until it stops. Each phrase OpenAI
        transcribes, each key pressed, and each 30 s of silence goes through webhook()
        as a Gather result would; when that wants to speak or hang up, the call is
        redirected to its TwiML."""
        session = cid = sid = None
        quiet_since = time.monotonic()
        try:
            while True:
                try:
                    message = json.loads(sock.recv())
                except ValueError:
                    continue
                event = message.get("event")
                if event == "start":
                    start = message.get("start") or {}
                    cid = (start.get("customParameters") or {}).get("call", "")
                    sid = start.get("callSid", "")
                    with self.cond:
                        call = self.calls.get(cid)
                        if call is None or call["sid"] != sid or call["transcriber"] != "openai":
                            log.warning("a media stream for no call of ours")
                            return
                    session = self._live(cid, sid)
                    try:
                        session.start()
                    except Exception as e:
                        log.warning("couldn't reach OpenAI (%s): Twilio transcribes this call", e)
                        session = None
                        self._fall_back(cid, sid)
                        return
                elif event == "media" and session:
                    media = message.get("media") or {}
                    if media.get("track", "inbound") != "inbound":
                        continue
                    session.feed(base64.b64decode(media.get("payload") or ""))
                    now = time.monotonic()
                    if session.speaking:
                        quiet_since = now
                    elif now - max(quiet_since, session.last_speech) >= LISTEN_SECONDS:
                        quiet_since = now
                        self._stream_event(cid, sid, {})  # as a Gather timing out
                elif event == "dtmf" and session:
                    quiet_since = time.monotonic()
                    self._stream_event(cid, sid, {"Digits": (message.get("dtmf") or {}).get("digit", "")})
                elif event == "stop":
                    break
        except Exception as e:  # the stream ended (hung up, or redirected)
            if isinstance(e, OSError) or type(e).__name__ == "WebSocketClosed":
                log.info("media stream closed (%s)", e)
            else:
                log.exception("media stream failed")
        finally:
            if session:
                session.finish()  # the last phrase still comes through, if it was cut off
            with self.cond:  # the call ended while its last words were on their way: done now
                call = self.calls.get(cid or "")
                if call and call["status"] == "ending":
                    self._set(call, self._final(call))

    def _fall_back(self, cid: str, sid: str) -> None:
        """OpenAI is out of reach: carry on with Twilio's speech recognition instead."""
        with self.cond:
            call = self.calls.get(cid)
            if call is None or call["status"] != "listening":
                return
            call["transcriber"] = "twilio"
            xml = twiml(self._listen(call, "OpenAI isn't working, so Twilio is listening instead."))
        try:
            self.twilio.redirect(sid, xml)
        except Exception as e:
            log.warning("couldn't switch the call to Twilio: %s", e)

    def _live(self, cid: str, sid: str):
        on_text = lambda text: self._stream_event(cid, sid, {"SpeechResult": text})
        if self.live_session:
            return self.live_session(on_text)
        from .live import LiveSession
        return LiveSession(self.openai_key, self.openai_model, on_text,
                           language=self.language.split("-")[0].lower())

    def _stream_event(self, cid: str, sid: str, form: dict) -> None:
        with self.cond:
            call = self.calls.get(cid)
            ended = call is None or call["status"] == "ending"  # hung up: nothing to redirect
        try:
            xml = self.webhook("gather", cid, {"CallSid": sid, **form})
            if ended:
                return
            if xml and ("<Say" in xml or "<Hangup" in xml):
                self.twilio.redirect(sid, xml)
        except Exception:
            log.exception("handling a streamed phrase failed")

    def _status(self, call: dict, result: str) -> None:
        if call["status"] == "ending":
            if result in ("completed", "failed", "canceled", "no-answer", "busy"):
                # Its last words may still be on the way (from OpenAI, after the stream ends).
                self._finish_later(call, self.late_wait if call["transcriber"] == "twilio"
                                   else self.ending_wait)
            return
        if call["status"] not in ("ringing", "listening"):
            return
        if result == "completed" and call["status"] == "listening" and call["transcriber"] == "openai":
            # Hung up on the phone: OpenAI may still be transcribing the last phrase. The
            # stream's end finishes the call (stream()), or this does if it never comes.
            self._set(call, "ending")
            self._finish_later(call, self.ending_wait)
            return
        if result == "completed":
            if call["status"] == "ringing":
                # Picked up by voicemail or call screening, and hung up before the answer webhook.
                self._set(call, "no-answer")
            else:
                self._set(call, self._final(call))
        elif result in ("no-answer", "busy"):
            self._set(call, "no-answer")
        elif result in ("failed", "canceled"):
            self._set(call, "failed", error=f"the call {result}")


def serve_webhooks(voice: Voice, host: str, port: int) -> ThreadingHTTPServer:
    """Twilio's webhooks: POST /voice/<secret>/<answer|gather|status>?id=<call>."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Twilio's media stream (OpenAI transcription): a WebSocket on /voice/<secret>/stream.
            from .ws import server_handshake
            parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
            if not (len(parts) == 3 and parts[0] == "voice" and parts[2] == "stream"
                    and secrets.compare_digest(parts[1], voice.secret)):
                self.send_error(404)
                return
            sock = server_handshake(self)
            if sock is None:
                self.send_error(400)
                return
            voice.stream(sock)

        def do_POST(self):
            url = urllib.parse.urlsplit(self.path)
            parts = url.path.strip("/").split("/")
            body = None
            if len(parts) == 3 and parts[0] == "voice" and secrets.compare_digest(parts[1], voice.secret):
                n = min(int(self.headers.get("content-length") or 0), 64 * 1024)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode(errors="replace")))
                cid = dict(urllib.parse.parse_qsl(url.query)).get("id", "")
                try:
                    body = voice.webhook(parts[2], cid, form)
                except Exception:
                    log.exception("voice webhook %s failed", parts[2])
                    body = twiml(say("Sorry, something went wrong."), "<Hangup/>")
            if body is None:
                self.send_error(404)
                return
            data = body.encode()
            self.send_response(200)
            self.send_header("content-type", "text/xml")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):  # the path holds the secret: log only the action
            parts = self.path.split("?")[0].strip("/").split("/")
            log.info("twilio %s -> %s", parts[2] if len(parts) == 3 else "?",
                     args[1] if len(args) > 1 else "")

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
