# RaspyJack Extensions

Author: m0usem0use

This directory holds shared helpers that payloads can import when they need a reusable gate or action.

The current focus is BLE-driven workflow control. Instead of embedding the same wait logic in multiple payloads, RaspyJack exposes a small extension API that any payload can call.

On RaspyJack, the BLE path uses the Pi's onboard Bluetooth through BlueZ and `bluetoothctl`. It does not depend on a separate UART BLE module.

## What is here

- `gates.py` provides condition-style helpers such as `WAIT_FOR_PRESENT` and `WAIT_FOR_NOTPRESENT`.
- `actions.py` provides shared actions such as `REQUIRE_CAPABILITY` and `RUN_PAYLOAD`.
- `api.py` re-exports the public helpers for payload authors.
- the command-line scripts in this directory are thin wrappers over the same API.

## Public API

Payloads should import the helpers from `EXTENSIONS.api`:

```python
from EXTENSIONS.api import (
    WAIT_FOR_PRESENT,
    WAIT_FOR_NOTPRESENT,
    REQUIRE_CAPABILITY,
    RUN_PAYLOAD,
)
```

These imports are regular Python functions. There is no extra payload language or parser layer.

## Example usage

Wait until a known BLE advertiser is present, then continue:

```bash
python3 /root/Raspyjack/EXTENSIONS/wait_for_present.py --name TestRJ --timeout-seconds 30
```

Require a dependency before the payload continues:

```bash
python3 /root/Raspyjack/EXTENSIONS/require_capability.py binary bluetoothctl
```

Run another payload by relative path:

```bash
python3 /root/Raspyjack/EXTENSIONS/run_payload.py utilities/trigger_marker.py test_run
```

## Continuous presence watcher

`WAIT_FOR_PRESENT` is a one-shot gate: it blocks until a target shows up, then the
payload continues. To instead **keep running and trigger an action every time** a
target arrives (and optionally when it leaves), use the presence watcher.

Step 1 — find each phone's identifier. Put the phone next to RaspyJack and scan a
couple of times:

```bash
python3 /root/Raspyjack/EXTENSIONS/ble_discover.py --window-seconds 8 --uuids
```

A MAC that stays the same across repeated scans is usable directly. Modern phones
randomize their BLE MAC (~every 15 min), so if the MAC keeps changing, match on a
stable `Name` or `service_uuid` instead — or pair/bond the phone to the Pi.

Step 2 — copy `presence_watch.example.json`, fill in `mac` / `name` /
`service_uuid` and the `on_arrive` / `on_leave` shell commands per target, then:

```bash
python3 /root/Raspyjack/EXTENSIONS/presence_watch.py /root/Raspyjack/EXTENSIONS/my_presence.json
```

Each action runs with `RJ_LABEL`, `RJ_MAC`, and `RJ_NAME` set in its environment.
`leave_grace_scans` debounces brief dropouts before declaring a target gone, and
`arrive_cooldown_seconds` stops re-arrivals from spamming the action.

### Trigger types

A target fires when **any** identifier you set matches:

| Field | Source | Notes |
|-------|--------|-------|
| `mac` | BLE + Kismet | exact address; phones rotate it |
| `name` | BLE + Kismet | advertised / common name |
| `service_uuid` | BLE | e.g. a fixed iBeacon UUID |
| `ssid` | Wi-Fi (nmcli) | a network currently in range |
| `probe` | Kismet | an SSID a device is **probing for** |

### Probe-request triggers (recommended for phones)

On jack, Kismet already owns the monitor radio (`wlan1mon`), channel-hops, and
records the SSIDs every device probes for — the networks it has joined before.
That probed SSID is a far more stable fingerprint than a phone's MAC (which
randomizes ~every 15 min). Use `probe_discover.py` to see what is nearby and
what each device is asking for:

```bash
python3 /root/Raspyjack/EXTENSIONS/probe_discover.py --probing-only
#   A0:6A:44:44:6E:D5  [Wi-Fi Client]  age=8s  sig=-88dBm  probes: DEAN
```

Copy a distinctive probe string into a target's `probe` field. The watcher polls
Kismet's REST API (auth/URL from `/etc/raspyjack/kismet_ha.conf`, overridable via
the config `kismet` block) and treats a target present when a matching device was
heard within `freshness_seconds`. Caveat: up-to-date iOS/Android often send
*wildcard* probes with no SSID, so not every phone leaks a usable probe — check
with `probe_discover.py` first.

## Notes for payload authors

- Extensions do not replace the normal payload template.
- Interactive payloads should still use `ScaledDraw`, `scaled_font()`, and `get_button`.
- Keeping the payload in the standard `try/finally` layout is still the right way to guarantee `LCD.LCD_Clear()` and `GPIO.cleanup()` on both supported screen sizes.
