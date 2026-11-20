"""Keeps the bridge's T3 session token fresh.

T3 bearer sessions expire (`t3 auth session issue --ttl ...`). If T3_TOKEN_COMMAND is
set, the bridge runs it to mint a new token when the current one is close to expiry
or has been rejected, and saves the new token back to the .env file. Without it,
the bridge only reports how long the token has left, so the device can warn.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .t3client import T3Error

log = logging.getLogger("t3bridge.token")

RENEW_BEFORE_DAYS = 7
CHECK_EVERY = 3600.0  # seconds
RETRY_FAILED_AFTER = 600.0  # seconds between renewal attempts after a failure


class TokenKeeper:
    def __init__(self, client, command: str = "", env_path: Path | None = None):
        self.client = client
        self.command = command
        self.env_path = env_path
        self.expires_at: datetime | None = None
        self.last_check = 0.0
        self.last_failure = 0.0
        self.error: str | None = None

    @property
    def days_left(self) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - datetime.now(timezone.utc)).total_seconds() / 86400

    def notice(self) -> str | None:
        """A warning for the device when the bridge is about to lose access to T3."""
        if self.error and self.expires_at is None:
            return f"The bridge can't sign in to T3: {self.error}"
        left = self.days_left
        if left is None or left > RENEW_BEFORE_DAYS:
            return None
        if self.command and not self.error:
            return None  # it will renew itself
        days = round(left)
        when = "today" if left < 1 else f"in {days} day{'s' if days != 1 else ''}"
        return f"The bridge's T3 token expires {when}. Renew it on the computer running the bridge."

    def due(self) -> bool:
        return time.monotonic() - self.last_check >= CHECK_EVERY

    def check(self, rejected: bool = False) -> None:
        """Look up the expiry; renew if it's close (or the token was rejected) and we can."""
        self.last_check = time.monotonic()
        if not rejected:
            try:
                session = self.client.auth_session()
                self.expires_at = _parse(session.get("expiresAt"))
                self.error = None if session.get("authenticated") else "token not accepted"
                rejected = not session.get("authenticated")
            except T3Error as e:
                if getattr(e, "status", None) != 401:
                    log.warning("couldn't check the T3 session: %s", e)
                    return
                rejected = True
        if rejected:
            self.expires_at = None
            self.error = self.error or "token rejected"
        left = self.days_left
        if self.command and (rejected or (left is not None and left <= RENEW_BEFORE_DAYS)):
            if time.monotonic() - self.last_failure >= RETRY_FAILED_AFTER or rejected:
                self.renew()

    def renew(self) -> bool:
        try:
            out = subprocess.run(shlex.split(self.command), capture_output=True, text=True,
                                 timeout=60, check=True).stdout
        except (OSError, subprocess.SubprocessError) as e:
            self._failed(f"T3_TOKEN_COMMAND failed ({e.__class__.__name__})")
            return False
        token = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if not token or " " in token:
            self._failed("T3_TOKEN_COMMAND didn't print a token")
            return False
        old = self.client.token
        self.client.set_token(token)
        try:
            session = self.client.auth_session()
        except T3Error as e:
            self.client.set_token(old)
            self._failed(f"the new token didn't work ({e})")
            return False
        self.expires_at = _parse(session.get("expiresAt"))
        self.error = None
        self._save(token)
        log.info("renewed the T3 token; it now expires %s", self.expires_at)
        return True

    def _failed(self, why: str) -> None:
        self.last_failure = time.monotonic()
        self.error = why
        log.warning("token renewal: %s", why)

    def _save(self, token: str) -> None:
        """Write the token back to .env, so a restart doesn't mint yet another session."""
        if not self.env_path or not self.env_path.exists():
            return
        text = self.env_path.read_text()
        line = f"T3_TOKEN={token}"
        text, n = re.subn(r"(?m)^T3_TOKEN=.*$", lambda _m: line, text)
        if n == 0:
            text = text.rstrip("\n") + "\n" + line + "\n"
        fd, tmp = tempfile.mkstemp(dir=self.env_path.parent, prefix=".env.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.env_path)
        except OSError as e:
            log.warning("couldn't save the new token to %s: %s", self.env_path, e)
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
