#!/usr/bin/env python3
"""Wi-Fi SSID presence helper for RaspyJack extensions.

BLE covers name / mac / service_uuid; this adds the fourth trigger type, ``ssid``,
by reading the list of currently visible Wi-Fi networks. It uses nmcli's cached
scan results so it never forces a rescan on the uplink interface (which would
briefly drop connectivity). Set ``rescan`` only for a spare interface you are
sure is not carrying your session.

Author: m0usem0use
"""
from __future__ import annotations

import shutil
import subprocess


def _run(cmd: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds)
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        return None


def _unescape_nmcli(field: str) -> str:
    # nmcli -t escapes ':' and '\' as '\:' and '\\'; undo that for the SSID field.
    out = []
    i = 0
    while i < len(field):
        ch = field[i]
        if ch == "\\" and i + 1 < len(field):
            out.append(field[i + 1])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def scan_wifi_ssids(interface: str = "", *, rescan: bool = False, timeout_seconds: int = 8) -> set[str]:
    """Return the set of Wi-Fi SSIDs currently visible to the host.

    Reads nmcli's cached list by default (non-disruptive). ``rescan`` triggers a
    fresh scan; only pass an ``interface`` you know is not your uplink.
    """
    if not shutil.which("nmcli"):
        return set()

    if rescan:
        rescan_cmd = ["nmcli", "device", "wifi", "rescan"]
        if interface:
            rescan_cmd += ["ifname", interface]
        _run(rescan_cmd, timeout_seconds=timeout_seconds)

    list_cmd = ["nmcli", "-t", "-f", "SSID", "device", "wifi", "list"]
    if interface:
        list_cmd += ["ifname", interface]
    proc = _run(list_cmd, timeout_seconds=timeout_seconds)
    if not proc or not proc.stdout:
        return set()

    ssids: set[str] = set()
    for raw in proc.stdout.splitlines():
        ssid = _unescape_nmcli(raw.strip())
        if ssid:
            ssids.add(ssid)
    return ssids
