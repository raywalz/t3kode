"""Run the bridge: python3 -m t3bridge [--env PATH]  (from the bridge/ directory)

Or install the plugin onto an e-reader: python3 -m t3bridge install-plugin <drive> (see install.py)
Or go back to the version before the last update: python3 -m t3bridge rollback
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

from .config import Config, load_dotenv
from .core import Bridge
from .server import serve
from .suggest import Suggester
from .t3client import T3Client
from .token import TokenKeeper
from .update import REPO, Updater, UpdateError, rollback


def main() -> None:
    if sys.argv[1:2] == ["install-plugin"]:
        from .install import main as install_main
        raise SystemExit(install_main(sys.argv[2:]))
    if sys.argv[1:2] == ["rollback"]:
        try:
            print(rollback())
        except UpdateError as e:
            raise SystemExit(f"rollback: {e}")
        return
    ap = argparse.ArgumentParser(prog="t3bridge", epilog="Also: t3bridge install-plugin --help")
    ap.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[2] / ".env",
                    help="dotenv file to read (default: .env at the repo root)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--demo", action="store_true",
                    help="serve made-up threads without a T3 server (BRIDGE_PORT, BRIDGE_HOST, "
                         "and BRIDGE_TOKEN still apply)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.demo:
        return run_demo()
    load_dotenv(args.env)
    cfg = Config.from_env()

    suggester = None
    if cfg.suggest_url:
        suggester = Suggester(cfg.suggest_url, cfg.suggest_api_key, cfg.suggest_model,
                              cfg.suggest_reasoning_effort, cfg.suggest_count)
    client = T3Client(cfg.t3_url, cfg.t3_token)
    keeper = TokenKeeper(client, cfg.t3_token_command, args.env)
    updater = Updater(enabled=cfg.update_check)
    bridge = Bridge(client, suggester, poll_fast=cfg.poll_fast, poll_slow=cfg.poll_slow,
                    token_keeper=keeper, updater=updater)
    hooks = None
    if cfg.voice_enabled:
        from .voice import Twilio, Voice, serve_webhooks
        bridge.voice = Voice(bridge, Twilio(cfg.twilio_account_sid, cfg.twilio_api_key, cfg.twilio_api_secret),
                             my_number=cfg.voice_my_number, from_number=cfg.twilio_number,
                             public_url=cfg.voice_public_url, secret=cfg.voice_webhook_secret,
                             speech_model=cfg.voice_speech_model, language=cfg.voice_language)
        # Loopback only: Twilio reaches it through a tunnel or reverse proxy on VOICE_PUBLIC_URL.
        hooks = serve_webhooks(bridge.voice, "127.0.0.1", cfg.voice_port)
        threading.Thread(target=hooks.serve_forever, daemon=True, name="voice-hooks").start()
    bridge.start()
    httpd = serve(bridge, cfg.host, cfg.port, cfg.device_token, restart=lambda: _restart(bridge))
    logging.info("listening on http://%s:%d (suggestions %s, voice replies %s)", cfg.host, cfg.port,
                 "on" if suggester else "off",
                 f"on, webhooks on 127.0.0.1:{cfg.voice_port}" if hooks else "off")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        httpd.server_close()
        if hooks:
            hooks.server_close()


def _restart(bridge: Bridge) -> None:
    """Run the updated code: replace this process with a fresh bridge, same arguments.
    The same process ID, so a service manager such as systemd doesn't notice."""
    time.sleep(1)  # let the reply that started the update reach the device
    bridge.close()
    logging.shutdown()
    os.chdir(REPO / "bridge")
    os.execv(sys.executable, [sys.executable, "-m", "t3bridge", *sys.argv[1:]])


class _DemoSuggester:
    """Canned suggestions, so the demo shows the whole reply row."""

    def suggest(self, entries):
        last = entries[-1]["text"].lower() if entries else ""
        if "pull request" in last:
            return ["Yes, open the pull request", "Run it on CI a few times first", "What caused the race?"]
        if "decision" in last:
            return ["Skip the charting upgrade for now", "Do both, update the snapshots", "Show me the legend diff"]
        return ["Thanks, looks good", "Can you add a test?", "What's left to do?"]


def run_demo() -> None:
    from .demo import DemoT3, demo_voice

    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "8787"))
    token = os.environ.get("BRIDGE_TOKEN", "")
    updater = Updater(enabled=os.environ.get("UPDATE_CHECK", "1").strip().lower() not in ("0", "false", "off", "no"))
    bridge = Bridge(DemoT3(), _DemoSuggester(), poll_fast=1, poll_slow=5, updater=updater)
    bridge.voice = demo_voice(bridge)
    bridge.start()
    httpd = serve(bridge, host, port, token, restart=lambda: _restart(bridge))
    logging.info("demo bridge on http://%s:%d (no T3; made-up threads)", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        httpd.server_close()


if __name__ == "__main__":
    main()
