"""Updates: newer releases, updating the bridge on request, and serving the plugin.

Releases are the `vX.Y.Z` tags on the checkout's own git remote (`origin`), checked about
once a day. Nothing updates by itself: the device asks (someone tapped Update), then the
bridge fetches the release, runs its tests in a separate checkout, fast-forwards to it
only if they pass, and restarts. It updates only to a release tag of its own remote, and
only when that's a plain fast-forward of a clean checkout; otherwise it says how to
update by hand. `python3 -m t3bridge rollback` goes back to the version before.

The device updates its plugin from the bridge's copy (`plugin/t3.koplugin`), so the
plugin always matches the bridge.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import __version__

log = logging.getLogger("t3bridge.update")

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugin" / "t3.koplugin"
PREVIOUS = REPO / "state" / "previous-version"  # state/ is gitignored
CHECK_EVERY = 24 * 3600.0  # seconds
RETRY_CHECK_AFTER = 3600.0  # after a failed check (offline, no remote)
TEST_TIMEOUT = 600  # seconds for the new version's tests
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_FILE = re.compile(r"^[\w.-]+$")


class UpdateError(Exception):
    pass


def parse_version(v: str | None) -> tuple[int, int, int] | None:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def plugin_version(plugin: Path = PLUGIN) -> str | None:
    try:
        m = re.search(r'\bversion\s*=\s*"([^"]+)"', (plugin / "_meta.lua").read_text())
    except OSError:
        return None
    return m.group(1) if m else None


def plugin_manifest(plugin: Path = PLUGIN) -> dict:
    """The plugin's files with their sizes and SHA-256, for the device to update from."""
    files = []
    for p in sorted(plugin.iterdir()):
        if p.is_file() and _FILE.match(p.name) and not p.name.startswith("."):
            data = p.read_bytes()
            files.append({"path": p.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return {"version": plugin_version(plugin), "files": files}


def plugin_file(name: str, plugin: Path = PLUGIN) -> bytes:
    """One of the plugin's own files. Only names the manifest lists: nothing else is served."""
    if not _FILE.match(name) or name.startswith(".") or not (plugin / name).is_file():
        raise UpdateError(f"no plugin file {name!r}")
    return (plugin / name).read_bytes()


class Updater:
    def __init__(self, repo: Path = REPO, version: str = __version__, enabled: bool = True,
                 test_command: list[str] | None = None, previous: Path | None = None):
        self.repo = repo
        self.version = version
        self.enabled = enabled
        self.test_command = test_command or [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
        self.previous = previous or repo / "state" / "previous-version"
        self.latest: str | None = None  # the newest release tag, when newer than this bridge
        self.last_check: float | None = None
        self.last_failed = False
        self.status = "idle"  # idle | updating | restarting | failed
        self.message = ""
        self.failed_tag: str | None = None
        self._lock = threading.Lock()

    # ---- git ------------------------------------------------------------------

    def _git(self, *args: str, cwd: Path | None = None, timeout: float = 120) -> str:
        try:
            out = subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True, text=True,
                                 timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        except (OSError, subprocess.SubprocessError) as e:
            raise UpdateError(f"git {args[0]} failed ({e.__class__.__name__})") from None
        if out.returncode != 0:
            detail = (out.stderr or out.stdout).strip().splitlines()
            raise UpdateError(f"git {args[0]} failed: {detail[-1] if detail else out.returncode}")
        return out.stdout.strip()

    # ---- checking -------------------------------------------------------------

    def due(self) -> bool:
        if not self.enabled:
            return False
        if self.last_check is None:
            return True
        wait = RETRY_CHECK_AFTER if self.last_failed else CHECK_EVERY
        return time.monotonic() - self.last_check >= wait

    def check(self) -> None:
        """Look for a newer release tag on the remote. Quiet when there's no remote or no network."""
        self.last_check = time.monotonic()
        try:
            out = self._git("ls-remote", "--tags", "--refs", "origin", timeout=30)
        except UpdateError as e:
            self.last_failed = True
            log.info("release check: %s", e)
            return
        self.last_failed = False
        tags = [line.rsplit("refs/tags/", 1)[-1] for line in out.splitlines()]
        newest = max((t for t in tags if _TAG.match(t)), key=parse_version, default=None)
        mine = parse_version(self.version)
        self.latest = newest if newest and mine and parse_version(newest) > mine else None
        if self.status == "failed" and self.latest != self.failed_tag:
            self.status, self.message = "idle", ""  # a newer release: the old failure no longer applies
        if self.latest:
            log.info("T3 KOde %s is available (this is %s)", self.latest, self.version)

    # ---- what the device sees ---------------------------------------------------

    def blocker(self) -> str | None:
        """Why the bridge can't update itself, or None when it can."""
        try:
            top = self._git("rev-parse", "--show-toplevel", timeout=10)
        except UpdateError:
            return "the bridge isn't running from a git clone"
        if Path(top).resolve() != self.repo.resolve():
            return "the bridge isn't running from a git clone of T3 KOde"
        try:
            if self._git("status", "--porcelain", "--untracked-files=no", timeout=20):
                return "the bridge's files have local changes"
        except UpdateError as e:
            return str(e)
        if not os.access(self.repo, os.W_OK) or not os.access(self.repo / ".git", os.W_OK):
            return "the bridge can't write to its folder"
        return None

    def info(self) -> dict:
        info = {"bridge": self.version, "plugin": plugin_version(self.repo / "plugin" / "t3.koplugin"),
                "release": None, "status": self.status}
        if self.message:
            info["message"] = self.message
        if self.latest and self.status != "restarting":
            info["release"] = self.latest.lstrip("v")
            reason = self.blocker()
            info["self_update"] = reason is None
            if reason:
                info["manual"] = (f"Update it on the computer running the bridge ({reason}): "
                                  "git pull, then restart the bridge.")
        return info

    # ---- updating ---------------------------------------------------------------

    def start(self, restart) -> dict:
        """Update to the newest release in the background; `restart()` runs the new code."""
        with self._lock:
            if self.status in ("updating", "restarting"):
                return self.info()
            if not self.latest:
                raise UpdateError("there's no newer release")
            reason = self.blocker()
            if reason:
                raise UpdateError(f"can't update by itself: {reason}")
            self.status, self.message = "updating", ""
        threading.Thread(target=self._run, args=(self.latest, restart), name="update", daemon=True).start()
        return self.info()

    def _run(self, tag: str, restart) -> None:
        try:
            self.update_to(tag)
        except UpdateError as e:
            log.warning("update to %s failed: %s", tag, e)
            self.status, self.message, self.failed_tag = "failed", str(e), tag
            return
        except Exception as e:  # never leave the status stuck on "updating"
            log.exception("update to %s failed", tag)
            self.status, self.message, self.failed_tag = "failed", f"{e.__class__.__name__}: {e}", tag
            return
        self.status, self.message = "restarting", ""
        log.info("updated to %s; restarting", tag)
        restart()

    def update_to(self, tag: str) -> None:
        """Fetch `tag`, test it in a separate checkout, then fast-forward to it."""
        self._git("fetch", "--no-tags", "--force", "origin", f"refs/tags/{tag}:refs/tags/{tag}", timeout=180)
        head = self._git("rev-parse", "HEAD")
        target = self._git("rev-parse", f"{tag}^{{commit}}")
        if self._is_ancestor(target, head):
            raise UpdateError(f"this checkout already includes {tag}")
        if not self._is_ancestor(head, target):
            raise UpdateError(f"this checkout has commits that {tag} doesn't; update it by hand")
        self._test(target)
        self.previous.parent.mkdir(parents=True, exist_ok=True)
        self.previous.write_text(f"{head} {self.version}\n")
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":
            self._git("checkout", "--quiet", "--detach", target)
        else:
            self._git("merge", "--ff-only", "--quiet", target)

    def _is_ancestor(self, a: str, b: str) -> bool:
        try:
            self._git("merge-base", "--is-ancestor", a, b)
            return True
        except UpdateError:
            return False

    def _test(self, commit: str) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="t3kode-update-"))
        work = tmp / "checkout"
        try:
            self._git("worktree", "add", "--detach", "--quiet", str(work), commit)
            try:
                out = subprocess.run(self.test_command, cwd=work / "bridge", capture_output=True, text=True,
                                     timeout=TEST_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise UpdateError("the new version's tests took too long") from None
            if out.returncode != 0:
                log.warning("tests of %s failed:\n%s", commit[:12], (out.stderr or out.stdout)[-4000:])
                raise UpdateError("the new version's tests failed (details in the bridge's log)")
        finally:
            try:
                self._git("worktree", "remove", "--force", str(work))
            except UpdateError:
                pass
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                self._git("worktree", "prune")
            except UpdateError:
                pass


def rollback(repo: Path = REPO, previous: Path | None = None) -> str:
    """Go back to the version before the last update. Returns a message for the user."""
    previous = previous or repo / "state" / "previous-version"
    try:
        sha, version = previous.read_text().split()[:2]
    except (OSError, ValueError):
        raise UpdateError("there's no earlier version to go back to") from None
    up = Updater(repo=repo, enabled=False)
    if up._git("status", "--porcelain", "--untracked-files=no"):
        raise UpdateError("the bridge's files have local changes; not touching them")
    if up._git("rev-parse", "--abbrev-ref", "HEAD") == "HEAD":
        up._git("checkout", "--quiet", "--detach", sha)
    else:
        up._git("reset", "--keep", "--quiet", sha)
    previous.unlink()
    return f"Back on {version}. Restart the bridge to run it."
