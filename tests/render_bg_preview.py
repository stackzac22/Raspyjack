#!/usr/bin/env python3
"""
Headless preview for the themed LCD background (STEP 3).

This dev box has no LCD/GPIO, so we cannot run raspyjack.py directly. Instead we
drive the *same* gui_background code path that raspyjack.py uses
(build_layer + paint_region + the S(3)/S(14) menu region) and replicate the
three menu renderers (list / grid / carousel) closely enough to confirm:

  - the background renders underneath icons/text,
  - text/icons stay legible (scrim),
  - all three view modes work,
  - "none" restores the stock solid-background look.

Outputs:
  /dev/shm/raspyjack_last.jpg     <- canonical frame (list view, gradient)
  /dev/shm/rj_bg_preview.png      <- 3 view modes x 3 bg modes contact sheet
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw, ImageFont
import gui_background as gb

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = H = 128                      # 128x128 panel, 0..127 coords
SCALE = 1                        # S() == identity at 128


def S(v):
    return int(v * SCALE)


# Stock theme colours (mirror raspyjack template()).
BG = "#000000"
BORDER = "#05ff00"
TEXT = "#05ff00"
SEL_TEXT = "#00ff55"
SELECT = "#2d0fff"

FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", S(10))
ICON = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", S(11))
BIG = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", S(40))

ITEMS = ["Network", "WiFi", "Payloads", "Files", "NewScan", "GhostCap", "RigTest", "Config"]
GLYPHS = {"Network": "⇄", "WiFi": "≋", "Payloads": "⚑", "Files": "▤",
          "NewScan": "◎", "GhostCap": "☗", "RigTest": "✦", "Config": "⚙"}


def _toolbar(draw):
    draw.line([(0, S(4)), (W, S(4))], fill="#222222", width=S(10))
    draw.text((1, S(-2)), "39 °C", fill="white", font=FONT)


def _border(draw):
    bw, by = S(5), S(12)
    draw.line([(W - 1, by), (W - 1, H - 1)], fill=BORDER, width=bw)
    draw.line([(W - 1, H - 1), (0, H - 1)], fill=BORDER, width=bw)
    draw.line([(0, H - 1), (0, by)], fill=BORDER, width=bw)
    draw.line([(0, by), (W, by)], fill=BORDER, width=bw)


def _menu_bg(image, draw, layer, scrim):
    region = (S(3), S(14), W - S(4), H - S(4))
    if layer is None:
        draw.rectangle(region, fill=BG)
    else:
        gb.paint_region(image, region, layer, scrim)


def render(mode, view, selected=2):
    cfg = dict(gb.DEFAULTS, mode=mode)
    layer = gb.build_layer(W, H, cfg, base_dir=REPO)
    scrim = cfg["scrim"]

    image = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(image)
    _toolbar(draw)
    _menu_bg(image, draw, layer, scrim)

    if view == "list":
        start = [S(12), S(22)]
        gap = S(14)
        for i, txt in enumerate(ITEMS[:7]):
            y = start[1] + gap * i
            fill = SEL_TEXT if i == selected else TEXT
            if i == selected:
                draw.rectangle((start[0] - S(5), y, W - S(8), y + gap - 2), fill=SELECT)
            draw.text((start[0], y), GLYPHS[txt], font=ICON, fill=fill)
            draw.text((start[0] + S(14), y), txt, font=FONT, fill=fill)
    elif view == "grid":
        for i, txt in enumerate(ITEMS[:6]):
            col, row = i % 2, i // 2
            x = S(12) + col * S(55)
            y = S(22) + row * S(25)
            fill = SEL_TEXT if i == selected else TEXT
            if i == selected:
                draw.rectangle((x - 2, y - 2, x + S(53), y + S(23)), fill=SELECT)
            draw.text((x + 2, y), GLYPHS[txt], font=ICON, fill=fill)
            draw.text((x, y + S(13)), txt[:8], font=FONT, fill=fill)
    else:  # carousel
        cx, cy = W // 2, H // 2
        txt = ITEMS[selected]
        draw.text((cx, cy - S(12)), GLYPHS[txt], font=BIG, fill=SEL_TEXT, anchor="mm")
        draw.text((cx, cy + S(28)), txt, font=ICON, fill=SEL_TEXT, anchor="mm")
        draw.text((S(20), cy), "<", font=BIG, fill=TEXT, anchor="mm")
        draw.text((W - S(20), cy), ">", font=BIG, fill=TEXT, anchor="mm")

    _border(draw)
    return image


def main():
    modes = ["gradient", "image", "none"]
    views = ["list", "grid", "carousel"]

    pad = 6
    sheet = Image.new("RGB", (W * 3 + pad * 4, H * 3 + pad * 4), "#101010")
    label = ImageDraw.Draw(sheet)
    for r, mode in enumerate(modes):
        for c, view in enumerate(views):
            img = render(mode, view)
            x = pad + c * (W + pad)
            y = pad + r * (H + pad)
            sheet.paste(img, (x, y))
            label.text((x + 2, y + 1), f"{mode}/{view}", fill="#888888", font=FONT)

    sheet.save("/dev/shm/rj_bg_preview.png")
    render("gradient", "list").save("/dev/shm/raspyjack_last.jpg", "JPEG", quality=90)
    print("wrote /dev/shm/rj_bg_preview.png  and  /dev/shm/raspyjack_last.jpg")


if __name__ == "__main__":
    main()
