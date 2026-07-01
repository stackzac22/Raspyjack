#!/usr/bin/env python3
"""List nearby BLE advertisers so you can pick a target identifier.

Run this with the phone you want to track right next to RaspyJack. Repeat the
scan a couple of times: a MAC that stays stable across scans is usable directly;
phones that change MAC every scan are randomizing, so look for a stable Name or
ServiceUUID instead (use --uuids to resolve those).

Author: m0usem0use
"""
from __future__ import annotations

import argparse

from _bluez import ensure_bluetooth_ready, scan_ble


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan and list nearby BLE advertisers.")
    parser.add_argument("--window-seconds", type=int, default=8, help="Scan duration.")
    parser.add_argument("--uuids", action="store_true", help="Also resolve service UUIDs (slower).")
    parser.add_argument("--named-only", action="store_true", help="Only show devices that advertise a name.")
    parser.add_argument("--wifi", action="store_true", help="Also list visible Wi-Fi SSIDs (for ssid targets).")
    args = parser.parse_args()

    state = ensure_bluetooth_ready()
    if not state.get("ready"):
        print(f"Bluetooth not ready: powered={state.get('powered')} power_state={state.get('power_state')}")
        return 2

    devices = scan_ble(max(1, args.window_seconds), include_service_uuids=args.uuids)
    rows = []
    for dev in devices:
        name = str(dev.get("name", "") or "")
        if args.named_only and not name:
            continue
        rows.append((str(dev.get("mac", "")), name, list(dev.get("service_uuids") or [])))

    rows.sort(key=lambda r: (r[1] == "", r[1].lower(), r[0]))
    print(f"Found {len(rows)} device(s):")
    for mac, name, uuids in rows:
        print(f"  {mac}  {name or '(no name)'}")
        if args.uuids and uuids:
            for uuid in uuids:
                print(f"      uuid: {uuid}")
    if not rows:
        print("  (nothing seen — bring the phone closer / wake its screen and retry)")

    if args.wifi:
        try:
            from _wifi import scan_wifi_ssids
        except ImportError:
            scan_wifi_ssids = None
        ssids = sorted(scan_wifi_ssids()) if scan_wifi_ssids else []
        print(f"\nVisible Wi-Fi SSIDs ({len(ssids)}):")
        for ssid in ssids:
            print(f"  {ssid}")
        if not ssids:
            print("  (none cached — nmcli missing or no scan yet)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
