"""The photo of an account: the supplier's picture with the account's own look (owner, 2026-10-15: every account has its own photos).

An account may set a small caption (its name, drawn in a corner), a coloured frame, a crop of the edges and the JPEG quality. All of it is the
account's own presentation of the same tire; nothing else of the picture is touched. Without Pillow, or with an empty style, the picture is
served as it is."""
from __future__ import annotations

import hashlib
import io
import json
import re
from typing import Dict, Optional

FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/Library/Fonts/Arial Unicode.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf")
COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
CORNERS = ("bottom-right", "bottom-left", "top-right", "top-left")


def normalize(raw: dict) -> Dict[str, object]:
    """The style as it is stored: only known keys with checked values; {} means «no processing»."""
    raw = raw or {}
    out: Dict[str, object] = {}
    label = " ".join(str(raw.get("label") or "").split())[:30]
    if label:
        out["label"] = label
        corner = str(raw.get("corner") or "bottom-right")
        out["corner"] = corner if corner in CORNERS else "bottom-right"
    border = str(raw.get("border") or "").strip()
    if border:
        if not COLOR.match(border):
            raise ValueError("Цвет рамки: шесть знаков вида #1A2B3C.")
        out["border"] = border.upper()
    try:
        crop = float(str(raw.get("crop") or 0).replace(",", "."))
    except ValueError:
        raise ValueError("Обрезка краёв: число от 0 до 8 процентов.")
    if not 0 <= crop <= 8:
        raise ValueError("Обрезка краёв: число от 0 до 8 процентов.")
    if crop:
        out["crop"] = round(crop, 1)
    quality = raw.get("quality")
    if quality not in (None, "", 0):
        try:
            quality = int(quality)
        except (TypeError, ValueError):
            raise ValueError("Качество JPEG: целое число от 60 до 95.")
        if not 60 <= quality <= 95:
            raise ValueError("Качество JPEG: целое число от 60 до 95.")
        out["quality"] = quality
    return out


def version(style: dict) -> str:
    """A short name of the style: the processed copy of a photo is kept under it, so a changed style makes new files."""
    return hashlib.sha1(json.dumps(style, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]


def _font(size: int):
    from PIL import ImageFont
    for path in FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def process(data: bytes, style: dict) -> Optional[bytes]:
    """The picture with the account's look as JPEG bytes; None when it cannot be done (no Pillow, not an image): the caller serves the original."""
    if not style:
        return None
    try:
        return _process(data, style)
    except Exception:                                   # a broken picture or a missing font must never leave Avito without a photo
        return None


MAX_PIXELS = 40_000_000


def _process(data: bytes, style: dict) -> Optional[bytes]:
    from PIL import Image, ImageDraw
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    image = Image.open(io.BytesIO(data))
    if image.size[0] * image.size[1] > MAX_PIXELS:
        return None
    image.load()
    image = image.convert("RGB")
    crop = float(style.get("crop") or 0)
    if crop:
        w, h = image.size
        dx, dy = int(w * crop / 100), int(h * crop / 100)
        image = image.crop((dx, dy, w - dx, h - dy))
    if style.get("border"):
        w, h = image.size
        width = max(4, min(w, h) // 60)
        ImageDraw.Draw(image).rectangle((0, 0, w - 1, h - 1), outline=str(style["border"]), width=width)
    label = str(style.get("label") or "")
    if label:
        w, h = image.size
        font = _font(max(14, h // 22))
        draw = ImageDraw.Draw(image, "RGBA")
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        tw, th, pad = right - left, bottom - top, max(6, h // 90)
        corner = str(style.get("corner") or "bottom-right")
        x = w - tw - 2 * pad - 4 if corner.endswith("right") else 4
        y = h - th - 2 * pad - 4 if corner.startswith("bottom") else 4
        draw.rectangle((x, y, x + tw + 2 * pad, y + th + 2 * pad), fill=(255, 255, 255, 200))
        draw.text((x + pad - left, y + pad - top), label, fill=(20, 20, 20, 255), font=font)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=int(style.get("quality") or 88), optimize=True)
    return out.getvalue()
