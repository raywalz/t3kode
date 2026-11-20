"""Images in threads, fetched through T3 and prepared for e-ink.

A thread view registers each image it shows; the device can then fetch only those,
by id. The bytes come from T3's own signed asset URLs (assets.createUrl), so T3's
access rules apply: files an agent reply refers to need the token's filesystem:read
permission, and message attachments need only orchestration:read.

When ImageMagick is installed, images are shrunk to the device's width and turned
grayscale, which makes them several times smaller and faster to draw on e-ink.
Without it, the original bytes are sent.
"""

from __future__ import annotations

import html
import logging
import os
import shutil
import subprocess
import threading
import urllib.request
from collections import OrderedDict

from . import render

log = logging.getLogger("t3bridge.images")

MAX_SOURCE_BYTES = 30 * 1024 * 1024
CACHE_ENTRIES = 48


class ImageError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class Images:
    def __init__(self, client, converter: str | None = "auto"):
        self.client = client
        self.registry: dict[str, tuple[dict, str]] = {}  # id -> (T3 asset resource, mime hint)
        self.cache: OrderedDict[tuple[str, int], tuple[bytes, str]] = OrderedDict()
        self.lock = threading.Lock()
        if converter == "auto":
            converter = shutil.which("magick") or shutil.which("convert")
        self.converter = converter or None

    def ref(self, thread_id: str, kind: str, source, alt: str) -> str | None:
        """HTML for an image in a thread view, or None to show a placeholder."""
        if kind == "path":
            src = str(source)
            if "://" in src or src.startswith("data:"):
                return None  # web images: not fetched
            if os.path.isabs(src):
                resource = {"_tag": "media-file", "threadId": thread_id, "path": src}
            else:
                resource = {"_tag": "workspace-file", "threadId": thread_id, "path": src}
            mime = ""
        elif kind == "tool-output" and isinstance(source, tuple):
            item_id, index = source
            resource = {"_tag": "tool-output-image", "threadId": thread_id, "itemId": item_id,
                        "index": int(index)}
            mime = ""
        elif kind == "attachment" and isinstance(source, dict) and source.get("id"):
            resource = {"_tag": "attachment", "attachmentId": source["id"], "disposition": "inline"}
            if source.get("mimeType"):
                resource["mimeType"] = source["mimeType"]
            mime = source.get("mimeType") or ""
        else:
            return None
        image_id = render.short_id(thread_id + "\0" + repr(sorted(resource.items())))
        with self.lock:
            self.registry[image_id] = (resource, mime)
        if kind == "tool-output":
            # Screenshots in tool steps: a link only (fetched when tapped), so tool lines stay short.
            return f'<a href="t3:img:{image_id}">{html.escape(alt or "image", quote=False)} ›</a>'
        alt_attr = html.escape(alt or "image")
        # The device swaps t3img:<id> for its downloaded copy; tapping opens it full screen.
        return f'<a href="t3:img:{image_id}"><img src="t3img:{image_id}" alt="{alt_attr}"/></a>'

    def get(self, image_id: str, width: int) -> tuple[bytes, str]:
        """(bytes, content type), at most `width` pixels wide when a converter exists."""
        width = max(64, min(int(width), 4000))
        key = (image_id, width if self.converter else 0)
        with self.lock:
            hit = self.cache.get(key)
            if hit:
                self.cache.move_to_end(key)
                return hit
            entry = self.registry.get(image_id)
        if entry is None:
            raise ImageError(404, "no such image (open its thread first)")
        resource, mime = entry
        try:
            minted = self.client.rpc("assets.createUrl", {"resource": resource})
        except Exception as e:
            raise ImageError(404, f"T3 won't share this image: {e}") from None
        url = minted.get("relativeUrl") or minted.get("url") or ""
        if not url.startswith("http"):
            url = self.client.base_url + url
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                ctype = resp.headers.get("content-type", mime or "application/octet-stream")
                data = resp.read(MAX_SOURCE_BYTES + 1)
        except OSError as e:
            raise ImageError(502, f"couldn't fetch the image: {e}") from None
        if len(data) > MAX_SOURCE_BYTES:
            raise ImageError(413, "image too large")
        if not ctype.startswith("image/"):
            raise ImageError(415, f"not an image ({ctype})")
        result = self._for_eink(data, ctype, width)
        with self.lock:
            self.cache[key] = result
            while len(self.cache) > CACHE_ENTRIES:
                self.cache.popitem(last=False)
        return result

    def _for_eink(self, data: bytes, ctype: str, width: int) -> tuple[bytes, str]:
        if not self.converter:
            return data, ctype
        photo = "jpeg" in ctype
        out_format = "jpg:-" if photo else "png:-"
        cmd = [self.converter, "-", "-auto-orient", "-resize", f"{width}x{width * 4}>",
               "-colorspace", "Gray", "-strip"]
        cmd += ["-quality", "82"] if photo else ["-define", "png:compression-level=9"]
        try:
            out = subprocess.run(cmd + [out_format], input=data, capture_output=True,
                                 timeout=30, check=True).stdout
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("image conversion failed, sending the original: %s", e)
            return data, ctype
        if not out:
            return data, ctype
        return out, "image/jpeg" if photo else "image/png"
