"""Run the bridge: python3 -m t3bridge [--env PATH]  (from the bridge/ directory)"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import Config, load_dotenv
from .core import Bridge
from .server import serve
from .suggest import Suggester
from .t3client import T3Client


def main() -> None:
    ap = argparse.ArgumentParser(prog="t3bridge")
    ap.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[2] / ".env",
                    help="dotenv file to read (default: .env at the repo root)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(args.env)
    cfg = Config.from_env()

    suggester = None
    if cfg.suggest_url:
        suggester = Suggester(cfg.suggest_url, cfg.suggest_api_key, cfg.suggest_model,
                              cfg.suggest_reasoning_effort, cfg.suggest_count)
    bridge = Bridge(T3Client(cfg.t3_url, cfg.t3_token), suggester, poll_fast=cfg.poll_fast,
                    poll_slow=cfg.poll_slow, default_runtime_mode=cfg.default_runtime_mode)
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


if __name__ == "__main__":
    main()
