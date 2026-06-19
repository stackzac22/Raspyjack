"""
gui_background.py -- themed LCD background layer for RaspyJack.

Renders a configurable background *underneath* the menu/icons/text. It is a
pure-PIL module with no hardware dependencies, so it can be exercised headless
(see tests/render_bg_preview.py) and reused by raspyjack.py.

Config lives in gui_conf.json under the "BACKGROUND" section, e.g.:

    "BACKGROUND": {
        "mode": "gradient",          # "gradient" | "image" | "none"
        "image_path": "assets/bg.png",
        "gradient_top": "#0a1014",
        "gradient_bottom": "#04210a",
        "scrim": 0.30                 # 0..1 dark veil for text legibility
    }

- "none"     -> stock look (solid color.background fill, handled by caller).
- "gradient" -> vertical gradient from gradient_top to gradient_bottom.
- "image"    -> 128x128 (or any) image scaled to the panel, e.g. assets/bg.png.

All coordinates handed to paint_region() are *actual device pixels*; this
module never scales (the caller already works in scaled pixels).
"""
import os
from PIL import Image, ImageDraw

# Default: the shipped top-hat skull image (assets/bg.png), kept legible with a
# light scrim. Falls back to the gradient colours below if the image is missing.
DEFAULTS = {
    "mode": "image",
    "image_path": "assets/bg.png",
    "gradient_top": "#0a1410",
    "gradient_bottom": "#02160a",
    "scrim": 0.20,
}

VALID_MODES = ("none", "gradient", "image")


def normalize(section):
    """Merge a raw BACKGROUND dict from gui_conf.json over the defaults."""
    cfg = dict(DEFAULTS)
    if isinstance(section, dict):
        for k in DEFAULTS:
            if k in section and section[k] is not None:
                cfg[k] = section[k]
    if cfg.get("mode") not in VALID_MODES:
        cfg["mode"] = "none"
    try:
        cfg["scrim"] = max(0.0, min(1.0, float(cfg["scrim"])))
    except (TypeError, ValueError):
        cfg["scrim"] = DEFAULTS["scrim"]
    return cfg


def hex_to_rgb(value):
    """'#rrggbb' (or 'rrggbb') -> (r, g, b). Falls back to black."""
    try:
        s = str(value).lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (ValueError, IndexError):
        return (0, 0, 0)


def vertical_gradient(width, height, top, bottom):
    """Build a vertical top->bottom gradient Image (RGB)."""
    top = hex_to_rgb(top) if isinstance(top, str) else top
    bottom = hex_to_rgb(bottom) if isinstance(bottom, str) else bottom
    img = Image.new("RGB", (width, height))
    d = ImageDraw.Draw(img)
    span = max(1, height - 1)
    for y in range(height):
        t = y / span
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        d.line([(0, y), (width, y)], fill=(r, g, b))
    return img


def build_layer(width, height, cfg, base_dir="."):
    """Return an RGB Image (panel-sized) for the background, or None for 'none'.

    *base_dir* resolves a relative image_path (e.g. the RaspyJack install dir).
    On any image failure we degrade gracefully to a gradient so the menu is
    never left unpainted.
    """
    cfg = normalize(cfg)
    mode = cfg["mode"]
    if mode == "none":
        return None

    if mode == "image":
        path = cfg.get("image_path") or DEFAULTS["image_path"]
        if not os.path.isabs(path):
            path = os.path.join(base_dir, path)
        try:
            im = Image.open(path).convert("RGB")
            if im.size != (width, height):
                im = im.resize((width, height))
            return im
        except Exception:
            mode = "gradient"  # fall through to a safe default

    if mode == "gradient":
        return vertical_gradient(
            width, height, cfg["gradient_top"], cfg["gradient_bottom"]
        )
    return None


def paint_region(image, region, layer, scrim=0.30):
    """Paste the themed background into *image* over *region* (actual pixels).

    *region* is (x0, y0, x1, y1) inclusive of the lower-right pixel, matching
    PIL's ImageDraw.rectangle() so the painted area equals the stock fill.
    A faint dark *scrim* (0..1) is blended in to keep light/icon text legible.
    """
    if layer is None:
        return
    x0, y0, x1, y1 = region
    box = (x0, y0, x1 + 1, y1 + 1)
    crop = layer.crop(box)
    if scrim and scrim > 0:
        veil = Image.new("RGB", crop.size, (0, 0, 0))
        crop = Image.blend(crop, veil, min(1.0, max(0.0, scrim)))
    image.paste(crop, (x0, y0))
