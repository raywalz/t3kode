"""Bridge configuration, read from environment variables (optionally via a .env file)."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE loader. Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


# Environment variable -> Config field, for voice replies.
VOICE_REQUIRED = {
    "TWILIO_ACCOUNT_SID": "twilio_account_sid",
    "TWILIO_API_KEY": "twilio_api_key",
    "TWILIO_API_SECRET": "twilio_api_secret",
    "TWILIO_NUMBER": "twilio_number",
    "VOICE_MY_NUMBER": "voice_my_number",
    "VOICE_PUBLIC_URL": "voice_public_url",
    "VOICE_WEBHOOK_SECRET": "voice_webhook_secret",
}


@dataclass(frozen=True)
class Config:
    t3_url: str
    t3_token: str
    host: str = "127.0.0.1"
    port: int = 8787
    # Token the device sends to the bridge. Required unless the bridge only listens on loopback.
    device_token: str = ""
    # OpenAI-compatible endpoint for suggested replies. Empty disables suggestions.
    suggest_url: str = ""
    suggest_api_key: str = ""
    suggest_model: str = ""
    # Sent as `reasoning_effort` when non-empty. Reasoning models otherwise spend the
    # whole token budget thinking and return nothing.
    suggest_reasoning_effort: str = "none"
    suggest_count: int = 3
    # Seconds between T3 polls while something is live or a device is waiting, and otherwise.
    poll_fast: float = 2.0
    poll_slow: float = 20.0
    # Optional command that prints a fresh T3 token (e.g. `t3 auth session issue ... --token-only`).
    # The bridge runs it when its token is about to expire or is rejected.
    t3_token_command: str = ""
    # Look for new T3 KOde releases on the checkout's git remote about once a day.
    update_check: bool = True
    # Voice replies by phone call through Twilio (see voice.py). On when all of the
    # VOICE_REQUIRED values are set.
    twilio_account_sid: str = ""
    twilio_api_key: str = ""
    twilio_api_secret: str = ""
    twilio_number: str = ""
    voice_my_number: str = ""
    voice_public_url: str = ""
    voice_webhook_secret: str = ""
    voice_port: int = 8791
    voice_speech_model: str = "deepgram_nova-3"
    voice_language: str = "en-US"
    # Optional: transcribe calls with OpenAI's live transcription instead of Twilio's.
    voice_openai_api_key: str = ""
    voice_openai_model: str = "gpt-live-transcribe"
    voice_transcriber: str = ""  # the default: openai when its key is set; a device can pick per call

    @property
    def voice_enabled(self) -> bool:
        return all(getattr(self, f) for f in VOICE_REQUIRED.values())

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        token = env.get("T3_TOKEN", "")
        if not token:
            raise SystemExit("T3_TOKEN is not set. See .env.example.")
        cfg = cls(
            t3_url=env.get("T3_URL", "http://127.0.0.1:3773"),
            t3_token=token,
            host=env.get("BRIDGE_HOST", "127.0.0.1"),
            port=int(env.get("BRIDGE_PORT", "8787")),
            device_token=env.get("BRIDGE_TOKEN", ""),
            suggest_url=env.get("SUGGEST_URL", ""),
            suggest_api_key=env.get("SUGGEST_API_KEY", ""),
            suggest_model=env.get("SUGGEST_MODEL", ""),
            suggest_reasoning_effort=env.get("SUGGEST_REASONING_EFFORT", "none"),
            suggest_count=int(env.get("SUGGEST_COUNT", "3")),
            poll_fast=float(env.get("POLL_FAST", "2")),
            poll_slow=float(env.get("POLL_SLOW", "20")),
            t3_token_command=env.get("T3_TOKEN_COMMAND", ""),
            update_check=env.get("UPDATE_CHECK", "1").strip().lower() not in ("0", "false", "off", "no"),
            voice_port=int(env.get("VOICE_PORT", "8791")),
            voice_speech_model=env.get("VOICE_SPEECH_MODEL", "") or "deepgram_nova-3",
            voice_language=env.get("VOICE_LANGUAGE", "") or "en-US",
            voice_openai_api_key=env.get("VOICE_OPENAI_API_KEY", "").strip(),
            voice_openai_model=env.get("VOICE_OPENAI_MODEL", "") or "gpt-live-transcribe",
            voice_transcriber=env.get("VOICE_TRANSCRIBER", "").strip().lower(),
            **{field: env.get(name, "").strip() for name, field in VOICE_REQUIRED.items()},
        )
        if not cfg.device_token and not cfg.listens_on_loopback_only:
            raise SystemExit(
                f"BRIDGE_HOST={cfg.host} is reachable from other machines; set BRIDGE_TOKEN too."
            )
        if cfg.suggest_url and not cfg.suggest_model:
            raise SystemExit("SUGGEST_URL is set but SUGGEST_MODEL is not.")
        cfg.check_voice()
        return cfg

    def check_voice(self) -> None:
        missing = [name for name, field in VOICE_REQUIRED.items() if not getattr(self, field)]
        if len(missing) == len(VOICE_REQUIRED):
            return  # voice replies are off
        if missing:
            raise SystemExit(f"Voice replies need {', '.join(missing)} too (or leave all of "
                             f"{', '.join(VOICE_REQUIRED)} empty). See .env.example.")
        if not self.voice_public_url.startswith("https://"):
            raise SystemExit("VOICE_PUBLIC_URL must be an https:// URL that Twilio can reach.")
        for name in ("TWILIO_NUMBER", "VOICE_MY_NUMBER"):
            if not getattr(self, VOICE_REQUIRED[name]).startswith("+"):
                raise SystemExit(f"{name} must be in +<country code><number> form, e.g. +15555550123.")
        if self.voice_transcriber not in ("", "twilio", "openai"):
            raise SystemExit("VOICE_TRANSCRIBER is `twilio` or `openai`.")
        if self.voice_transcriber == "openai" and not self.voice_openai_api_key:
            raise SystemExit("VOICE_TRANSCRIBER=openai needs VOICE_OPENAI_API_KEY.")
        if len(self.voice_webhook_secret) < 16:
            raise SystemExit("VOICE_WEBHOOK_SECRET is too short; see .env.example for how to make one.")

    @property
    def listens_on_loopback_only(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False
