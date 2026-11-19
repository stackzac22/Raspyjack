#!/usr/bin/env python3
"""
RaspyJack Payload -- AI Vulnerability Report
=============================================

Turns the loot RaspyJack has already collected (Wi-Fi survey, captured WPA
handshakes, Nmap scans, sniffed creds) into a plain-language security report,
using a local 7B model on the 6T (Ollama) or any cloud model -- whichever the
config points at. The call is OpenAI-compatible either way, so local<->cloud is
just a config flip in config/ai_report.json.

Endpoint auto-discovery: if the configured host is not answering (e.g. the 6T
picked up a new DHCP lease on the field hotspot), it probes candidate hosts and
each interface gateway for an open Ollama port before giving up.

Two run modes:
  * On the Pi with the LCD attached  ->  python3 ai_report.py   (button UI)
  * Headless anywhere (x1, 6T, test) ->  python3 ai_report.py --cli
                                         [--loot DIR] [--provider local|cloud]

Loot in : /root/Raspyjack/loot/
Report  : /root/Raspyjack/loot/AIReport/report_<timestamp>.md

Controls (LCD mode):
  OK    -- Generate report from current loot
  KEY1  -- Toggle provider (local 6T  <->  cloud)
  KEY2  -- Push last report to the 6T / x1
  KEY3  -- Exit
"""

import os
import sys
import json
import time
import glob
import socket
import signal
import struct
import fcntl
import subprocess
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

REPO_ROOT = os.path.abspath(os.path.join(__file__, "..", "..", ".."))
sys.path.append(REPO_ROOT)

CONFIG_PATH = os.path.join(REPO_ROOT, "config", "ai_report.json")
LOOT_DIR = os.path.join(REPO_ROOT, "loot")
REPORT_DIR = os.path.join(LOOT_DIR, "AIReport")

CLI_MODE = "--cli" in sys.argv

# Cloud auth: reuse the OpenAI key already provisioned for Ragnar on this box
# rather than duplicating the secret into this config or a systemd env. Read at
# call time so nothing is cached and no key value lives in a git-tracked file.
_RAGNAR_ENV = os.path.join(REPO_ROOT, "vendor", "ragnar", ".env")
def _ragnar_api_key():
    try:
        with open(_RAGNAR_ENV) as _fh:
            for _ln in _fh:
                if _ln.startswith("RAGNAR_OPENAI_API_KEY="):
                    return _ln.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------- config

def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    # allow --provider override
    if "--provider" in sys.argv:
        cfg["provider"] = sys.argv[sys.argv.index("--provider") + 1]
    return cfg


def backend(cfg):
    """Return the active provider block merged with report settings."""
    b = dict(cfg[cfg["provider"]])
    b["provider"] = cfg["provider"]
    b["report"] = cfg.get("report", {})
    return b


# ---------------------------------------------------------- loot gathering

def _read_tail(path, n=4000):
    try:
        with open(path, "r", errors="replace") as f:
            data = f.read()
        return data[-n:]
    except Exception:
        return ""


def capture_source(relpath, rules, default):
    """Which device grabbed this capture, inferred from its path/name.

    First rule whose 'match' substring appears in the (case-insensitive) path
    wins -- so tag a device by dropping its .cap in a folder named for it or by
    prefixing the filename (e.g. loot/m5stick/ or 'cyd_home.cap')."""
    low = relpath.lower()
    for rule in rules:
        if rule.get("match", "").lower() in low:
            return rule.get("label", default)
    return default


def source_options(cfg):
    """De-duplicated list of source labels for the on-screen picker."""
    rep = (cfg or {}).get("report", {})
    labels = ["(auto-detect)"]
    for r in rep.get("capture_sources", []):
        lbl = r.get("label")
        if lbl and lbl not in labels:
            labels.append(lbl)
    dflt = rep.get("capture_source_default")
    if dflt and dflt not in labels:
        labels.append(dflt)
    return labels


def gather_loot(loot_dir, cfg=None, source_override=None):
    """Collect a compact, prioritized summary of what RaspyJack found.

    source_override: if set (and not the auto-detect sentinel), it becomes the
    label for any capture that doesn't already sit in a device-named folder --
    i.e. the 'I just grabbed this on the M5' pick from the menu, no file moving.
    """
    parts = []
    rep = (cfg or {}).get("report", {})
    src_rules = rep.get("capture_sources", [])
    src_default = rep.get("capture_source_default", "RaspyJack")
    if source_override and source_override != "(auto-detect)":
        src_default = source_override

    # Wi-Fi handshakes (.cap/.pcap) anywhere under loot
    caps = []
    for ext in ("*.cap", "*.pcap", "*.pcapng"):
        caps += glob.glob(os.path.join(loot_dir, "**", ext), recursive=True)
    if caps:
        lines = []
        for c in sorted(caps, key=os.path.getmtime, reverse=True)[:20]:
            size = os.path.getsize(c)
            rel = os.path.relpath(c, loot_dir)
            src = capture_source(rel, src_rules, src_default)
            lines.append(f"  - {rel} ({size} bytes)  [captured by: {src}]")
        parts.append("CAPTURED WPA HANDSHAKE / PMKID FILES "
                     "(note the capture device per file):\n" + "\n".join(lines))

    # Handshake quality reports (from handshake_quality.py)
    for rpt in sorted(glob.glob(os.path.join(loot_dir, "HandshakeQuality", "*.json")),
                      key=os.path.getmtime, reverse=True)[:3]:
        parts.append(f"HANDSHAKE QUALITY ({os.path.basename(rpt)}):\n" + _read_tail(rpt, 2000))

    # Nmap results
    nmaps = sorted(glob.glob(os.path.join(loot_dir, "Nmap", "*")),
                   key=os.path.getmtime, reverse=True)[:4]
    for n in nmaps:
        if os.path.isfile(n):
            parts.append(f"NMAP SCAN ({os.path.basename(n)}):\n" + _read_tail(n, 3000))

    # Sniffed credentials / MITM
    for creds in sorted(glob.glob(os.path.join(loot_dir, "MITM", "**", "*"), recursive=True),
                        key=os.path.getmtime, reverse=True)[:5]:
        if os.path.isfile(creds):
            parts.append(f"MITM/CREDS ({os.path.basename(creds)}):\n" + _read_tail(creds, 1500))

    if not parts:
        return None
    return "\n\n".join(parts)


# ------------------------------------------------------- endpoint discovery

def _http_ok(url, timeout=3):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status < 500
    except Exception:
        return False


def _iface_gateways():
    """Best-effort list of likely gateway IPs for each up interface's /24."""
    gws = set()
    try:
        out = subprocess.run(["ip", "-4", "route"], capture_output=True,
                             text=True, timeout=4).stdout
        for line in out.splitlines():
            toks = line.split()
            if "via" in toks:
                gws.add(toks[toks.index("via") + 1])
            # our own IP -> assume .1 is the AP/gateway on that /24
            if "src" in toks:
                ip = toks[toks.index("src") + 1]
                octs = ip.split(".")
                if len(octs) == 4:
                    gws.add(".".join(octs[:3]) + ".1")
    except Exception:
        pass
    return list(gws)


def _local_subnets():
    """/24 prefixes we hold an address on, for an optional full sweep."""
    prefixes = set()
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True,
                             text=True, timeout=4).stdout
        for line in out.splitlines():
            for tok in line.split():
                if "/" in tok and tok[0].isdigit():
                    ip = tok.split("/")[0]
                    octs = ip.split(".")
                    if len(octs) == 4 and not ip.startswith("127."):
                        prefixes.add(".".join(octs[:3]))
    except Exception:
        pass
    return list(prefixes)


def _port_open(host, port, timeout=0.4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def resolve_base_url(b, log=print):
    """Return a working OpenAI-compatible base_url, discovering if needed."""
    configured = b["base_url"]
    if _http_ok(configured.rstrip("/") + "/models"):
        return configured

    if not b.get("auto_discover"):
        return configured  # cloud, or discovery disabled: use as-is

    port = b.get("discover_port", 11434)
    tried = []

    # 1) explicit candidate hosts
    for h in b.get("host_candidates", []):
        tried.append(h)
        if _port_open(h, port):
            url = f"http://{h}:{port}/v1"
            if _http_ok(url + "/models"):
                log(f"[discover] model host found: {h}")
                return url

    # 2) gateways of each interface (the field hotspot / the 6T-as-AP)
    for gw in _iface_gateways():
        if gw in tried:
            continue
        if _port_open(gw, port):
            url = f"http://{gw}:{port}/v1"
            if _http_ok(url + "/models"):
                log(f"[discover] model host found at gateway: {gw}")
                return url

    # 3) optional full /24 sweep (off by default -- slow)
    if b.get("discover_full_subnet"):
        for pfx in _local_subnets():
            log(f"[discover] sweeping {pfx}.0/24 for :{port} ...")
            for i in range(1, 255):
                host = f"{pfx}.{i}"
                if _port_open(host, port, timeout=0.15):
                    url = f"http://{host}:{port}/v1"
                    if _http_ok(url + "/models"):
                        log(f"[discover] model host found by sweep: {host}")
                        return url

    log("[discover] no model host reachable; using configured base_url")
    return configured


# --------------------------------------------------------------- the model

SYSTEM_PROMPT = (
    "You are a penetration tester writing a short security report for a "
    "non-technical homeowner who gave written consent for this test. "
    "Use plain language. Structure the report as: (1) a one-paragraph summary "
    "a homeowner can understand, (2) a prioritized list of findings, each with "
    "'What we found', 'Why it matters', and 'What to do', ordered most urgent "
    "first, and (3) a short technical appendix for a future tester. "
    "Only describe findings supported by the evidence provided. Do not invent "
    "results. If a WPA handshake was captured, explain that it means the Wi-Fi "
    "password can now be tested offline, and that a long, random passphrase is "
    "the fix. Never print or guess actual passwords. "
    "When the evidence names the device that captured a handshake, state it "
    "plainly in that finding (e.g. 'captured with a $20 M5StickC keychain') to "
    "make the point that this does not require expensive or specialized gear."
)


def call_model(b, base_url, findings, log=print):
    payload = {
        "model": b["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": "Here is the evidence RaspyJack collected during the "
                        "authorized test. Write the report.\n\n" + findings},
        ],
        "temperature": b["report"].get("temperature", 0.2),
        "max_tokens": b["report"].get("max_tokens", 800),
        "stream": False,
    }
    data = json.dumps(payload).encode()
    url = base_url.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    # Prefer config key, but for cloud fall back to an env var so no secret
    # ever has to live in the git-tracked config file.
    key = (b.get("api_key")
           or os.environ.get("AI_REPORT_API_KEY")
           or os.environ.get("OPENAI_API_KEY")
           or _ragnar_api_key()
           or "x")
    req.add_header("Authorization", f"Bearer {key}")

    timeout = b["report"].get("timeout_seconds", 180)
    log(f"[model] calling {b['model']} at {url} ...")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode())
    return resp["choices"][0]["message"]["content"]


# --------------------------------------------------------------- output

def save_report(text, provider, model):
    os.makedirs(REPORT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(REPORT_DIR, f"report_{ts}.md")
    header = (f"# RaspyJack Security Report\n\n"
              f"*Generated {datetime.now():%Y-%m-%d %H:%M} "
              f"via {provider} model `{model}`*\n\n---\n\n")
    with open(path, "w") as f:
        f.write(header + text + "\n")
    return path


def push_report(path, targets, log=print):
    """Best-effort scp of the report to the 6T / x1. Never fatal.

    Reports real success/failure per target and creates the destination
    directory first (scp won't make it and fails silently otherwise).
    Target form 'host:/dir/' -> ssh mkdir -p /dir, then scp into it.
    """
    for tgt in targets:
        try:
            host, _, remote = tgt.partition(":")
            remote_dir = remote if remote.endswith("/") else os.path.dirname(remote)
            if remote_dir:
                subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                     host, "mkdir", "-p", remote_dir],
                    capture_output=True, timeout=20)
            r = subprocess.run(
                ["scp", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                 path, tgt],
                capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                log(f"[push] sent to {tgt}")
            else:
                log(f"[push] FAILED {tgt}: {r.stderr.strip()[:80]}")
        except Exception as e:
            log(f"[push] FAILED {tgt}: {e}")


def import_from_bruce(cfg, log=print):
    """Pull new .cap/.pcap files off the M5StickC's Bruce WebUI into loot/.

    Bruce protocol (from firmware source): POST /login (form user/pwd) sets a
    BRUCESESSION cookie; GET /listfiles?fs=..&folder=.. returns 'Fi:name:size'
    lines; GET /file?fs=..&name=<path>&action=download streams the file.
    Best-effort, never fatal. Returns (count_imported, dest_dir)."""
    import http.cookiejar
    b = cfg.get("bruce", {})
    base = b.get("base_url", "http://bruce.local").rstrip("/")
    fs = b.get("fs", "LittleFS")
    folder = b.get("folder", "/")
    dest = os.path.join(LOOT_DIR, b.get("dest_subdir", "m5stick"))
    os.makedirs(dest, exist_ok=True)

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    # 1) login -> cookie
    try:
        creds = urllib.parse.urlencode(
            {"username": b.get("user", "admin"), "password": b.get("pwd", "bruce")}
        ).encode()
        opener.open(base + "/login", data=creds, timeout=10)
    except Exception as e:
        log(f"[bruce] no WebUI at {base} ({e})")
        return 0, dest

    # 2) list the folder
    try:
        q = urllib.parse.urlencode({"fs": fs, "folder": folder})
        listing = opener.open(base + "/listfiles?" + q, timeout=10).read().decode(
            "utf-8", "replace")
    except Exception as e:
        log(f"[bruce] list failed: {e}")
        return 0, dest

    names = []
    for line in listing.splitlines():
        if line.startswith("Fi:"):
            name = line[3:].rsplit(":", 1)[0]      # strip trailing :size
            if name.lower().endswith((".cap", ".pcap", ".pcapng")):
                names.append(name)
    if not names:
        log(f"[bruce] no captures in {folder}")
        return 0, dest

    # 3) download any we don't already have
    count = 0
    for name in names:
        local = os.path.join(dest, name)
        if os.path.exists(local):
            continue
        full = (folder.rstrip("/") + "/" + name) if folder != "/" else "/" + name
        try:
            q = urllib.parse.urlencode({"fs": fs, "name": full, "action": "download"})
            data = opener.open(base + "/file?" + q, timeout=30).read()
            with open(local, "wb") as f:
                f.write(data)
            count += 1
            log(f"[bruce] got {name} ({len(data)}B)")
        except Exception as e:
            log(f"[bruce] {name} failed: {e}")
    log(f"[bruce] imported {count} new capture(s)")
    return count, dest


def generate(cfg, log=print, source_override=None):
    b = backend(cfg)
    findings = gather_loot(LOOT_DIR, cfg, source_override)
    if not findings:
        log("[loot] nothing to report -- run a survey/handshake/scan first")
        return None, "No loot found. Capture something first."
    base_url = resolve_base_url(b, log)
    text = call_model(b, base_url, findings, log)
    path = save_report(text, b["provider"], b["model"])
    log(f"[report] saved -> {path}")
    return path, text


# =================================================================== CLI

def run_cli():
    cfg = load_config()
    if "--loot" in sys.argv:
        global LOOT_DIR
        LOOT_DIR = sys.argv[sys.argv.index("--loot") + 1]
    logerr = lambda m: print(m, file=sys.stderr)
    if "--import-bruce" in sys.argv:
        n, dest = import_from_bruce(cfg, log=logerr)
        print(f"Imported {n} capture(s) into {dest}", file=sys.stderr)
    src = None
    if "--source" in sys.argv:
        src = sys.argv[sys.argv.index("--source") + 1]
    path, text = generate(cfg, log=logerr, source_override=src)
    if path:
        push_report(path, cfg.get("report", {}).get("push_to", []),
                    log=lambda m: print(m, file=sys.stderr))
        print("\n" + "=" * 60 + "\n")
        print(text)
    else:
        print(text)
        sys.exit(1)


# =================================================================== LCD

def run_lcd():
    import RPi.GPIO as GPIO
    import LCD_1in44
    import LCD_Config
    from PIL import Image, ImageDraw
    from payloads._display_helper import ScaledDraw, scaled_font
    from payloads._input_helper import get_button

    PINS = {"UP": 6, "DOWN": 19, "LEFT": 5, "RIGHT": 26,
            "OK": 13, "KEY1": 21, "KEY2": 20, "KEY3": 16}
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for p in PINS.values():
        GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    lcd = LCD_1in44.LCD()
    lcd.LCD_Init(LCD_1in44.SCAN_DIR_DFT)
    font = scaled_font(11)

    cfg = load_config()
    sources = source_options(cfg)          # picker list; [0] = auto-detect
    sel = [0]                              # index into sources
    last_path = [None]
    msg = ["Pick device, then OK"]         # transient status line

    def short(label):
        # trim long labels to fit the 128px screen
        return label.split(" (")[0][:20]

    def draw():
        img = Image.new("RGB", (128, 128), "black")
        d = ScaledDraw(img)
        d.text((4, 2), "AI VULN REPORT", font=font, fill="yellow")
        pc = "lime" if cfg["provider"] == "local" else "orange"
        d.text((4, 22), f"Model: {cfg['provider']}", font=font, fill=pc)
        d.text((4, 40), "Captured on:", font=font, fill="cyan")
        d.text((8, 54), f">{short(sources[sel[0]])}", font=font, fill="white")
        d.text((4, 78), msg[0][:21], font=font, fill="magenta")
        d.text((4, 98), "UP/DN pick  OK=go", font=font, fill="gray")
        d.text((4, 110), "K1=model K2=import", font=font, fill="gray")
        d.text((4, 120), "K3=exit", font=font, fill="gray")
        lcd.LCD_ShowImage(img, 0, 0)

    def set_msg(text):
        msg[0] = text
        draw()

    def signal_exit(*_):
        GPIO.cleanup()
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_exit)

    draw()
    while True:
        btn = get_button(PINS, GPIO)
        if btn == "UP":
            sel[0] = (sel[0] - 1) % len(sources)
            set_msg("Pick device, then OK")
        elif btn == "DOWN":
            sel[0] = (sel[0] + 1) % len(sources)
            set_msg("Pick device, then OK")
        elif btn == "KEY1":
            cfg["provider"] = "cloud" if cfg["provider"] == "local" else "local"
            set_msg(f"Model -> {cfg['provider']}")
        elif btn == "KEY2":
            set_msg("Importing from M5...")
            n, _ = import_from_bruce(cfg, log=lambda m: set_msg(m[:21]))
            set_msg(f"Got {n} cap(s) from M5")
            # jump the picker to the M5 entry so OK tags it right
            for i, s in enumerate(sources):
                if "m5" in s.lower() or "stick" in s.lower():
                    sel[0] = i
                    break
        elif btn == "OK":
            override = sources[sel[0]]
            set_msg("Working... (30-90s)")
            path, text = generate(
                cfg, source_override=override,
                log=lambda m: set_msg(m.replace("[", "").replace("]", "")[:21]))
            if path:
                last_path[0] = path
                set_msg("Sending report...")
                push_report(path, cfg.get("report", {}).get("push_to", []),
                            log=lambda m: set_msg(m[:21]))
                set_msg("DONE - on 6T + x1")
            else:
                set_msg(text[:21])
        elif btn == "KEY3":
            signal_exit()
        time.sleep(0.12)


if __name__ == "__main__":
    if CLI_MODE:
        run_cli()
    else:
        run_lcd()
