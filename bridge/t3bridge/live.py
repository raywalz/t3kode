"""Live transcription of a phone call with OpenAI (gpt-live-transcribe).

Twilio streams the call as 8 kHz μ-law. This converts it to the 24 kHz PCM16 OpenAI
takes, finds where speech starts and stops (the model leaves that to the client), sends
only the speech with a little lead-in, and commits each phrase at a pause. Silence is
never sent, so a long pause while you read costs nothing. Transcripts come back per
phrase, in the order the phrases were spoken.
"""

from __future__ import annotations

import base64
import json
import logging
import sys
import threading
import time
from array import array

from . import ws

log = logging.getLogger("t3bridge.live")

REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
PROMPT = "Someone dictating a message to an AI coding agent working in a software project."

FRAME_MS = 20  # Twilio sends 20 ms of audio per message (160 samples)
START_FRAMES = 3  # this much sound in a row starts a phrase (60 ms)
END_SILENCE_MS = 1000  # this much quiet ends it
PREROLL_MS = 300  # audio kept from just before a phrase starts, so its first sound isn't cut
MIN_PHRASE_MS = 250  # shorter than this is a click or a breath: dropped (OpenAI also refuses < 100 ms)
MAX_PHRASE_MS = 30_000  # commit a long unbroken stretch anyway
SEND_MS = 100  # audio goes to OpenAI in chunks this long


def _ulaw_table() -> list[int]:
    out = []
    for u in range(256):
        u = ~u & 0xFF
        sign, exponent, mantissa = u & 0x80, (u >> 4) & 0x07, u & 0x0F
        sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
        out.append(-sample if sign else sample)
    return out


ULAW = _ulaw_table()


def decode_ulaw(data: bytes) -> list[int]:
    return [ULAW[b] for b in data]


def to_pcm24k(samples: list[int]) -> bytes:
    """8 kHz samples to 24 kHz little-endian PCM16, by linear interpolation."""
    out = array("h")
    n = len(samples)
    for i, a in enumerate(samples):
        b = samples[i + 1] if i + 1 < n else a
        out.extend((a, a + (b - a) // 3, a + 2 * (b - a) // 3))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def rms(samples: list[int]) -> float:
    return (sum(s * s for s in samples) / len(samples)) ** 0.5 if samples else 0.0


class Phrases:
    """Splits a stream of 20 ms frames into phrases. feed() returns a list of actions:
    ("start", [frames before it]), ("audio", frame), ("end", ms) or ("drop", ms)."""

    def __init__(self):
        self.noise = 200.0  # running estimate of the line's background level
        self.loud = 0  # frames in a row above the threshold
        self.quiet = 0  # frames in a row below it, while in a phrase
        self.in_phrase = False
        self.phrase_ms = 0
        self.preroll: list[list[int]] = []

    def feed(self, frame: list[int]) -> list[tuple]:
        level = rms(frame)
        threshold = max(300.0, self.noise * 3)
        loud = level > threshold
        if not loud:  # follow the background: quickly down, slowly up
            self.noise = level if level < self.noise else self.noise + (level - self.noise) * 0.01
        if not self.in_phrase:
            self.preroll.append(frame)
            del self.preroll[:-(PREROLL_MS // FRAME_MS)]
            self.loud = self.loud + 1 if loud else 0
            if self.loud < START_FRAMES:
                return []
            self.in_phrase, self.quiet, self.loud = True, 0, 0
            lead, self.preroll = self.preroll, []
            self.phrase_ms = len(lead) * FRAME_MS
            return [("start", lead)]
        self.phrase_ms += FRAME_MS
        self.quiet = 0 if loud else self.quiet + 1
        actions = [("audio", frame)]
        if self.quiet * FRAME_MS >= END_SILENCE_MS or self.phrase_ms >= MAX_PHRASE_MS:
            self.in_phrase = False
            spoken = self.phrase_ms - self.quiet * FRAME_MS
            actions.append(("end", spoken) if spoken >= MIN_PHRASE_MS else ("drop", spoken))
        return actions


class LiveSession:
    """One call's transcription. feed() takes Twilio's μ-law payloads; on_text(text) is
    called with each phrase's transcript, in order; finish() commits what's left and
    waits for it."""

    def __init__(self, api_key: str, model: str, on_text, prompt: str = PROMPT,
                 language: str = "en", connect=ws.connect):
        self.api_key = api_key
        self.model = model
        self.on_text = on_text
        self.prompt = prompt
        self.language = language
        self.connect = connect
        self.phrases = Phrases()
        self.sock: ws.WebSocket | None = None
        self.cond = threading.Condition()
        self.order: list[str] = []  # committed item ids, in the order spoken
        self.results: dict[str, str | None] = {}
        self.outstanding = 0  # commits sent, transcript not back yet
        self.buffer = bytearray()  # converted audio not sent yet
        self.last_speech = time.monotonic()
        self.reader: threading.Thread | None = None

    @property
    def speaking(self) -> bool:
        return self.phrases.in_phrase

    def start(self) -> None:
        self.sock = self.connect(REALTIME_URL, {"Authorization": f"Bearer {self.api_key}"})
        transcription = {"model": self.model, "prompt": self.prompt}
        if self.language:
            transcription["languages"] = [self.language]
        self._send({"type": "session.update", "session": {
            "type": "transcription",
            "audio": {"input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "noise_reduction": {"type": "near_field"},
                "transcription": transcription,
                "turn_detection": None,
            }},
        }})
        self.reader = threading.Thread(target=self._read, daemon=True, name="live-transcribe")
        self.reader.start()

    def _send(self, message: dict) -> None:
        if self.sock and not self.sock.closed:
            self.sock.send_text(json.dumps(message))

    def _flush(self) -> None:
        if self.buffer:
            self._send({"type": "input_audio_buffer.append",
                        "audio": base64.b64encode(bytes(self.buffer)).decode()})
            self.buffer.clear()

    def _audio(self, samples: list[int]) -> None:
        self.buffer += to_pcm24k(samples)
        if len(self.buffer) >= 48 * SEND_MS:  # 24 kHz × 2 bytes = 48 bytes per ms
            self._flush()

    def feed(self, payload: bytes) -> None:
        """One Twilio media message's audio (base64-decoded μ-law)."""
        samples = decode_ulaw(payload)
        for i in range(0, len(samples), 160):
            for action in self.phrases.feed(samples[i:i + 160]):
                kind = action[0]
                if kind == "start":
                    self.last_speech = time.monotonic()
                    for frame in action[1]:
                        self._audio(frame)
                elif kind == "audio":
                    self._audio(action[1])
                    self.last_speech = time.monotonic()
                elif kind == "end":
                    self._commit()
                else:  # drop: a click, not speech
                    self.buffer.clear()
                    self._send({"type": "input_audio_buffer.clear"})

    def _commit(self) -> None:
        self._flush()
        with self.cond:
            self.outstanding += 1
        self._send({"type": "input_audio_buffer.commit"})

    def _read(self) -> None:
        try:
            while True:
                event = json.loads(self.sock.recv())
                kind = event.get("type", "")
                if kind == "input_audio_buffer.committed":
                    with self.cond:
                        self.order.append(event.get("item_id"))
                elif kind == "conversation.item.input_audio_transcription.completed":
                    self._result(event.get("item_id"), (event.get("transcript") or "").strip())
                elif kind == "conversation.item.input_audio_transcription.failed":
                    log.warning("a phrase couldn't be transcribed: %s", event.get("error"))
                    self._result(event.get("item_id"), None)
                elif kind == "error":
                    log.warning("OpenAI: %s", (event.get("error") or {}).get("message"))
        except (ws.WebSocketClosed, OSError, ValueError):
            pass
        finally:
            with self.cond:
                self.outstanding = 0
                self.cond.notify_all()

    def _result(self, item_id, text) -> None:
        ready = []
        with self.cond:
            self.results[item_id] = text
            self.outstanding = max(0, self.outstanding - 1)
            while self.order and self.order[0] in self.results:
                ready.append(self.results.pop(self.order.pop(0)))
            self.cond.notify_all()
        for t in ready:
            if t:
                self.on_text(t)

    def finish(self, timeout: float = 5.0) -> None:
        """The call is over: commit a phrase in progress, wait for the transcripts
        still coming (up to `timeout`), then close."""
        if self.phrases.in_phrase:
            self.phrases.in_phrase = False
            if self.phrases.phrase_ms >= MIN_PHRASE_MS:
                self._commit()
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.outstanding > 0 and time.monotonic() < deadline:
                self.cond.wait(deadline - time.monotonic())
        self.close()

    def close(self) -> None:
        if self.sock:
            self.sock.close()
