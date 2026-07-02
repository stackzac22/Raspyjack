#!/usr/bin/env python3
"""Kismet capture watchdog — auto-recovers the rtw88 (8821cu) DFS wedge on jack.

The Realtek rtw88 driver silently wedges when Kismet hops onto a high-5GHz DFS
channel it can't compute TX power for: the datasource still reports running, but
num_packets stops incrementing and no devices are seen. In a populated area AP
beacons never stop, so a *flat* packet counter is an unambiguous wedge signal.

On a confirmed wedge this reloads the rtw88 modules and restarts Kismet (the same
sequence proven by hand). Only wlan1/rtw88 is touched — the Broadcom uplink and
SSH are on a different driver and are never affected.

Config: /etc/raspyjack/kismet_ha.conf (KISMET_URL / KISMET_USER / KISMET_PASS),
the same file the HA bridge uses. Stdlib only.

Author: RaspyJack / jack ops
"""
from __future__ import annotations

import base64
import json
import subprocess
import time
import urllib.request

CONF = "/etc/raspyjack/kismet_ha.conf"

CHECK_INTERVAL = 30      # seconds between packet-count samples
FLAT_STRIKES = 3         # consecutive flat samples => wedged (~90s of no packets)
API_FAIL_STRIKES = 4     # consecutive API failures => treat as down, recover
COOLDOWN = 150           # seconds to settle after a recovery before watching again
RELOAD_MODULES = ["rtw88_8821cu", "rtw88_8821c", "rtw88_usb", "rtw88_core"]


def load_conf() -> dict:
    cfg = {
        "KISMET_URL": "http://127.0.0.1:2501",
        "KISMET_USER": "admin",
        "KISMET_PASS": "raspyjack",
    }
    try:
        with open(CONF) as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    cfg[key.strip()] = value.strip()
    except OSError:
        pass
    return cfg


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def total_packets(base: str, user: str, password: str) -> int:
    request = urllib.request.Request(base.rstrip("/") + "/datasource/all_sources.json")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=8) as response:
        sources = json.load(response)
    return sum(int(s.get("kismet.datasource.num_packets", 0) or 0) for s in sources)


def recover() -> None:
    log("WEDGE confirmed -> stop kismet, reload rtw88, start kismet")
    subprocess.run(["systemctl", "stop", "kismet.service"], timeout=90)
    time.sleep(2)
    subprocess.run(["modprobe", "-r", *RELOAD_MODULES], timeout=45)
    time.sleep(2)
    subprocess.run(["modprobe", RELOAD_MODULES[0]], timeout=45)
    time.sleep(4)
    subprocess.run(["systemctl", "start", "kismet.service"], timeout=90)
    log("recovery sequence complete")


def main() -> int:
    cfg = load_conf()
    base, user, password = cfg["KISMET_URL"], cfg["KISMET_USER"], cfg["KISMET_PASS"]
    log(
        f"watchdog up: interval={CHECK_INTERVAL}s flat_strikes={FLAT_STRIKES} "
        f"api_fail_strikes={API_FAIL_STRIKES} cooldown={COOLDOWN}s"
    )
    last = None
    flat = 0
    api_fail = 0
    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            current = total_packets(base, user, password)
            api_fail = 0
        except Exception as exc:  # noqa: BLE001 - watchdog must never die
            api_fail += 1
            log(f"kismet API unreachable ({api_fail}/{API_FAIL_STRIKES}): {exc}")
            if api_fail >= API_FAIL_STRIKES:
                recover()
                last, flat, api_fail = None, 0, 0
                time.sleep(COOLDOWN)
            continue

        if last is None:
            last = current
            continue

        if current < last:
            # counter reset (kismet was restarted) — not a wedge
            log(f"packet counter reset ({last} -> {current}); resyncing")
            last, flat = current, 0
        elif current == last:
            flat += 1
            log(f"no new packets (strike {flat}/{FLAT_STRIKES}, total={current})")
            if flat >= FLAT_STRIKES:
                recover()
                last, flat = None, 0
                time.sleep(COOLDOWN)
        else:
            if flat:
                log(f"packets flowing again (+{current - last}); clearing strikes")
            last, flat = current, 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
