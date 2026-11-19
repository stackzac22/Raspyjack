#!/usr/bin/env python3
"""
SquachWatch feed (Raspyjack payload)
------------------------------------
Shows what a USB-attached SquachWatch-CYD has detected, newest first.

It reads the loot file written by scripts/squachwatch_bridge.py
(loot/SquachWatch/squachwatch-YYYYMMDD.jsonl) and never opens the serial
port itself: the bridge service is the port's only owner.  Start the
bridge first:  systemctl start squachwatch-bridge

Controls: UP/DOWN scroll, KEY1 alerts only on/off, KEY2 mark seen, KEY3 exit.
"""

import glob
import json
import os
import subprocess
import sys
import time

sys.path.append(os.path.abspath(os.path.join(__file__, "..", "..", "..")))

import RPi.GPIO as GPIO
import LCD_1in44, LCD_Config  # noqa: F401
from PIL import Image
from payloads._display_helper import ScaledDraw, scaled_font
from payloads._input_helper import get_button

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LOOT = os.path.join(ROOT, "loot", "SquachWatch")
ALERT_TYPES = {"FLOCK", "AXON", "ALPR", "SKIMMER", "DRONE", "RAVEN", "META",
               "HACKER", "DEAUTH", "EVIL TWIN", "MY ALERT"}
PINS = {"UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26, "OK": 13,
        "KEY1": 21, "KEY2": 20, "KEY3": 16}
ROWS = 8


def bridge_running():
    try:
        r = subprocess.run(["systemctl", "is-active", "squachwatch-bridge"],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip() == "active"
    except Exception:
        return False


def load_events(limit=300):
    """Newest events first, one entry per device (its latest event)."""
    files = sorted(glob.glob(os.path.join(LOOT, "squachwatch-*.jsonl")))[-2:]
    latest = {}
    for path in files:
        try:
            with open(path) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    latest[(e.get("type"), e.get("mac"))] = e
        except OSError:
            continue
    evs = sorted(latest.values(), key=lambda e: e.get("ts", ""), reverse=True)
    return evs[:limit]


def age(ts):
    try:
        t = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        s = max(0, int(time.time() - t))
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def draw(lcd, font, w, h, evs, top, alerts_only, running, seen_mark):
    img = Image.new("RGB", (w, h), "black")
    d = ScaledDraw(img)
    here = sum(1 for e in evs if e.get("state") == "here")
    hot = any(e.get("type") in ALERT_TYPES and e.get("state") == "here"
              and e.get("ts", "") > seen_mark for e in evs)
    d.text((2, 1), f"SQUACH {here} here", font=font, fill="red" if hot else "lime")
    d.text((86, 1), "ALRT" if alerts_only else "ALL", font=font, fill="yellow")
    if not running:
        d.text((2, 14), "bridge not running", font=font, fill="orange")
    y = 26
    for e in evs[top:top + ROWS]:
        t = (e.get("type") or "?")[:7]
        mac = (e.get("mac") or "")[-8:]
        dim = e.get("state") != "here"
        col = "red" if e.get("type") in ALERT_TYPES else "white"
        if dim:
            col = "gray"
        d.text((2, y), f"{t:<7} {mac}", font=font, fill=col)
        d.text((100, y), age(e.get("ts")), font=font, fill="gray")
        y += 12
    if not evs:
        d.text((2, 40), "nothing yet", font=font, fill="gray")
    lcd.LCD_ShowImage(img, 0, 0)


def main():
    GPIO.setmode(GPIO.BCM)
    for pin in PINS.values():
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    w, h = lcd.width, lcd.height
    font = scaled_font(9)
    top, alerts_only, seen_mark = 0, False, ""
    last_load, evs, running = 0.0, [], False
    try:
        while True:
            now = time.time()
            if now - last_load > 2:
                evs_all = load_events()
                evs = [e for e in evs_all if e.get("type") in ALERT_TYPES] if alerts_only else evs_all
                running = bridge_running()
                last_load = now
                top = min(top, max(0, len(evs) - ROWS))
                draw(lcd, font, w, h, evs, top, alerts_only, running, seen_mark)
            btn = get_button(PINS, GPIO)
            if btn == "KEY3":
                break
            if btn in ("UP", "DOWN", "KEY1", "KEY2"):
                if btn == "UP":
                    top = max(0, top - 1)
                elif btn == "DOWN":
                    top = min(max(0, len(evs) - ROWS), top + 1)
                elif btn == "KEY1":
                    alerts_only, top = not alerts_only, 0
                elif btn == "KEY2":
                    seen_mark = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                last_load = 0  # redraw now
            time.sleep(0.05)
    finally:
        lcd.LCD_Clear()
        GPIO.cleanup()


if __name__ == "__main__":
    main()
