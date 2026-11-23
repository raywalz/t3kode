"""Install the plugin onto a connected e-reader:

    python3 -m t3bridge install-plugin /media/$USER/KOBOeReader [--url URL]

Finds KOReader's folder on the device (or takes it directly), copies plugin/t3.koplugin
into its plugins folder, and writes the bridge address and token from .env into
settings/t3.lua, so nothing has to be typed on the e-reader.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import shutil
from pathlib import Path

from .config import load_dotenv

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugin" / "t3.koplugin"
# Where KOReader lives, relative to the device's root: Kobo, Kindle, PocketBook.
# "." covers being given KOReader's folder itself (or a desktop KOReader's settings folder).
LAYOUTS = (".", ".adds/koreader", "koreader", "applications/koreader")


class InstallError(Exception):
    pass


def find_koreader(root: Path) -> Path:
    for rel in LAYOUTS:
        d = (root / rel).resolve()
        if (d / "plugins").is_dir():
            return d
    tried = ", ".join(str(root / rel) for rel in LAYOUTS)
    raise InstallError(f"No KOReader folder (one with a plugins folder) found. Looked in: {tried}")


def bridge_url(env: dict[str, str]) -> str:
    host = env.get("BRIDGE_HOST", "127.0.0.1")
    port = env.get("BRIDGE_PORT", "8787")
    try:
        ip = ipaddress.ip_address(host)
        local_only = ip.is_loopback or ip.is_unspecified
    except ValueError:
        local_only = host == "localhost"
    if local_only:
        raise InstallError(
            f"BRIDGE_HOST={host} isn't an address the e-reader can reach. Pass --url with one "
            "that is, e.g. --url http://192.168.1.20:8787 (or your tunnel's https:// address).")
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def lua_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def write_settings(path: Path, values: dict[str, str]) -> None:
    """Set keys in a KOReader settings file, keeping whatever else it holds."""
    lines = {k: f"    [{lua_string(k)}] = {lua_string(v)}," for k, v in values.items()}
    text = path.read_text() if path.exists() else ""
    if not re.search(r"^return\s*\{", text, re.M):
        text = "return {\n}\n"
    for key, line in lines.items():
        pattern = re.compile(rf"^\s*\[{re.escape(lua_string(key))}\]\s*=.*,\s*$", re.M)
        if pattern.search(text):
            text = pattern.sub(lambda _m: line, text, count=1)
        else:
            text = re.sub(r"^return\s*\{[ \t]*\n?", lambda m: "return {\n" + line + "\n", text,
                          count=1, flags=re.M)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def install(target: Path, url: str, token: str, plugin: Path = PLUGIN) -> tuple[Path, Path]:
    """Copy the plugin and write its settings. Returns (plugin folder, settings file)."""
    if not (plugin / "_meta.lua").exists():
        raise InstallError(f"Plugin not found at {plugin}")
    ko = find_koreader(target)
    dest = ko / "plugins" / plugin.name
    if dest.exists():
        meta = dest / "_meta.lua"
        if not (meta.exists() and re.search(r'name\s*=\s*"t3"', meta.read_text())):
            raise InstallError(f"{dest} exists and isn't this plugin; not replacing it.")
        shutil.rmtree(dest)
    shutil.copytree(plugin, dest, ignore=shutil.ignore_patterns("__pycache__", "*.part"))
    settings = ko / "settings" / "t3.lua"
    write_settings(settings, {"bridge_url": url, "bridge_token": token})
    return dest, settings


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="t3bridge install-plugin",
        description="Copy the plugin onto a connected e-reader and set its bridge address and token.")
    ap.add_argument("target", type=Path,
                    help="the e-reader's drive (e.g. /media/$USER/KOBOeReader, /Volumes/KOBOeReader) "
                         "or its KOReader folder")
    ap.add_argument("--url", help="bridge address for the e-reader (default: from BRIDGE_HOST and "
                                  "BRIDGE_PORT); use this for a tunnel's https:// address")
    ap.add_argument("--env", type=Path, default=REPO / ".env",
                    help="dotenv file to read (default: .env at the repo root)")
    args = ap.parse_args(argv)
    load_dotenv(args.env)
    try:
        url = (args.url or bridge_url(dict(os.environ))).rstrip("/")
        token = os.environ.get("BRIDGE_TOKEN", "")
        dest, settings = install(args.target, url, token)
    except InstallError as e:
        print(f"install-plugin: {e}")
        return 1
    print(f"Copied the plugin to {dest}")
    print(f"Wrote {settings}: bridge address {url}, " + ("with your BRIDGE_TOKEN" if token else "no token"))
    print("Eject the e-reader and restart KOReader (or, if T3 KOde was already installed, "
          "choose Reload T3 KOde).")
    return 0
