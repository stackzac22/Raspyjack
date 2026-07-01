#!/usr/bin/env python3
"""Show what nearby devices are probing for, via Kismet.

Kismet (already running on jack, owning wlan1mon) records every device it hears
and the SSIDs it probes for. Run this to spot a device that "looks weird", read
the SSIDs it is asking for, and copy an identifier (its probed SSID or MAC) into
a presence_watch target.

    python3 probe_discover.py                 # everything seen recently
    python3 probe_discover.py --max-age 120   # only devices heard in last 2 min
    python3 probe_discover.py --probing-only  # only devices probing a named SSID

Author: m0usem0use
"""
from __future__ import annotations

import argparse
import time

from _kismet import fetch_devices, load_kismet_conf


def main() -> int:
    parser = argparse.ArgumentParser(description="List devices and their probed SSIDs via Kismet.")
    parser.add_argument("--max-age", type=int, default=0, help="Only show devices heard within N seconds (0 = all).")
    parser.add_argument("--probing-only", action="store_true", help="Only show devices probing at least one named SSID.")
    parser.add_argument("--url", default="", help="Override Kismet URL.")
    parser.add_argument("--user", default="", help="Override Kismet user.")
    parser.add_argument("--password", default="", help="Override Kismet password.")
    args = parser.parse_args()

    cfg = load_kismet_conf(overrides={"url": args.url, "user": args.user, "password": args.password})
    devices = fetch_devices(cfg["url"], cfg["user"], cfg["password"])
    if not devices:
        print("No devices returned (Kismet down, wrong creds, or empty). Check /etc/raspyjack/kismet_ha.conf.")
        return 1

    now = time.time()
    rows = []
    for device in devices:
        age = int(now - float(device.get("last_time", 0))) if device.get("last_time") else -1
        if args.max_age and (age < 0 or age > args.max_age):
            continue
        probes = sorted(device.get("probes") or [])
        if args.probing_only and not probes:
            continue
        rows.append((age, device.get("mac", ""), device.get("type", ""), device.get("signal"), probes))

    rows.sort(key=lambda r: (r[0] < 0, r[0]))
    print(f"{len(rows)} device(s):")
    for age, mac, typ, signal, probes in rows:
        age_s = f"{age}s" if age >= 0 else "?"
        probe_s = ", ".join(probes) if probes else "-"
        print(f"  {mac}  [{typ}]  age={age_s}  sig={signal}dBm  probes: {probe_s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
