"""Run the bridge: python3 -m t3bridge [--env PATH]  (from the bridge/ directory)

Or install the plugin onto an e-reader: python3 -m t3bridge install-plugin <drive> (see install.py)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Config, load_dotenv
from .core import Bridge
from .server import serve
from .suggest import Suggester
from .t3client import T3Client
from .token import TokenKeeper


def main() -> None:
    if sys.argv[1:2] == ["install-plugin"]:
        from .install import main as install_main
        raise SystemExit(install_main(sys.argv[2:]))
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
    bridge = Bridge(client, suggester, poll_fast=cfg.poll_fast, poll_slow=cfg.poll_slow,
                    token_keeper=keeper)
    bridge.start()
    httpd = serve(bridge, cfg.host, cfg.port, cfg.device_token)
    logging.info("listening on http://%s:%d (suggestions %s)", cfg.host, cfg.port,
                 "on" if suggester else "off")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        httpd.server_close()


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
    import os

    from .demo import DemoT3

    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "8787"))
    token = os.environ.get("BRIDGE_TOKEN", "")
    bridge = Bridge(DemoT3(), _DemoSuggester(), poll_fast=1, poll_slow=5)
    bridge.start()
    httpd = serve(bridge, host, port, token)
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
