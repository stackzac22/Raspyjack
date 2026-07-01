#!/usr/bin/env python3
"""Kismet-backed presence source for RaspyJack extensions.

On jack, Kismet already owns the monitor radio (wlan1mon), channel-hops, and
tracks every 802.11 device it hears -- including the list of SSIDs each device
is *probing for* (its preferred-network fingerprint). This module queries
Kismet's REST API so the presence watcher can trigger on:

  * a device MAC,
  * a device common name, or
  * a probed SSID string (stable even when the device randomizes its MAC).

Auth + URL default to /etc/raspyjack/kismet_ha.conf (same file the HA bridge
uses), overridable from the watcher config's ``kismet`` block.

Author: m0usem0use
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse
import urllib.request

DEFAULT_CONF = "/etc/raspyjack/kismet_ha.conf"

# Field simplification: [kismet_path, alias]. Aliases keep the response tiny and
# give us stable keys regardless of Kismet's internal naming.
_FIELDS = [
    ["kismet.device.base.macaddr", "mac"],
    ["kismet.device.base.commonname", "name"],
    ["kismet.device.base.type", "type"],
    ["kismet.device.base.last_time", "last_time"],
    ["kismet.device.base.signal/kismet.common.signal.last_signal", "signal"],
    ["dot11.device/dot11.device.probed_ssid_map", "probes"],
]


def load_kismet_conf(path: str = DEFAULT_CONF, overrides: dict | None = None) -> dict:
    cfg = {"url": "http://127.0.0.1:2501", "user": "admin", "password": "raspyjack"}
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = (part.strip() for part in line.split("=", 1))
                if key == "KISMET_URL":
                    cfg["url"] = value
                elif key == "KISMET_USER":
                    cfg["user"] = value
                elif key == "KISMET_PASS":
                    cfg["password"] = value
    except OSError:
        pass
    if overrides:
        for key in ("url", "user", "password"):
            if overrides.get(key):
                cfg[key] = str(overrides[key])
    return cfg


def fetch_devices(url: str, user: str, password: str, *, timeout_seconds: int = 8) -> list[dict]:
    """Return normalized device records from Kismet.

    Each record: ``{mac, name, type, signal, last_time, probes(set)}``.
    Returns an empty list on any transport/parse error (fail-soft).
    """
    endpoint = url.rstrip("/") + "/devices/views/all/devices.json"
    data = urllib.parse.urlencode({"json": json.dumps({"fields": _FIELDS})}).encode()
    request = urllib.request.Request(endpoint, data=data)
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = json.load(response)
    except (OSError, ValueError):
        return []

    out: list[dict] = []
    for entry in raw:
        probe_map = entry.get("probes") or []
        probes = {
            record.get("dot11.probedssid.ssid")
            for record in probe_map
            if isinstance(record, dict) and record.get("dot11.probedssid.ssid")
        }
        out.append(
            {
                "mac": str(entry.get("mac", "")).upper(),
                "name": str(entry.get("name", "")),
                "type": str(entry.get("type", "")),
                "signal": entry.get("signal"),
                "last_time": entry.get("last_time", 0) or 0,
                "probes": probes,
            }
        )
    return out


def fresh_index(devices: list[dict], freshness_seconds: int) -> dict:
    """Collapse fresh devices into fast-lookup sets for matching."""
    now = time.time()
    macs: set[str] = set()
    names: set[str] = set()
    probes: set[str] = set()
    for device in devices:
        if freshness_seconds > 0 and now - float(device.get("last_time", 0)) > freshness_seconds:
            continue
        if device.get("mac"):
            macs.add(str(device["mac"]).upper())
        if device.get("name"):
            names.add(str(device["name"]))
        for ssid in device.get("probes") or ():
            probes.add(ssid)
    return {"macs": macs, "names": names, "probes": probes}
