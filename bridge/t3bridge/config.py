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
        )
        if not cfg.device_token and not cfg.listens_on_loopback_only:
            raise SystemExit(
                f"BRIDGE_HOST={cfg.host} is reachable from other machines; set BRIDGE_TOKEN too."
            )
        if cfg.suggest_url and not cfg.suggest_model:
            raise SystemExit("SUGGEST_URL is set but SUGGEST_MODEL is not.")
        return cfg

    @property
    def listens_on_loopback_only(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False
