# SquachWatch on the fleet

[SquachWatch-CYD](https://github.com/skizzophrenic/SquachWatch-CYD) is an ESP32
surveillance-device detector (Flock/ALPR cameras, Axon body cams, Meta glasses,
skimmers, AirTags and other trackers, Remote ID drones, deauth floods, evil
twins, Flipper/Pwnagotchi). Plugged into a Raspyjack node over USB, it feeds
the signot MQTT bus and Home Assistant.

```
 SquachWatch board ──USB serial──► squachwatch_bridge.py ──► MQTT ──► Home Assistant (x1)
                                          │
                                          └──► loot/SquachWatch/*.jsonl ──► payload "squachwatch" (LCD)
```

## Rule: one owner per serial port
Only `squachwatch-bridge` opens the board's port. The payload and Home Assistant
read what the bridge writes. Don't point GPS helpers, Ragnar or a
`pio device monitor` at the same port while the service is running. Stop the
service first.

## Install on a node
```bash
sudo apt install python3-serial python3-paho-mqtt      # installer does this too
ls -l /dev/serial/by-id/                                # find the board
sudo cp deploy/squachwatch/squachwatch-bridge.env.example /etc/default/squachwatch-bridge
sudo $EDITOR /etc/default/squachwatch-bridge            # port, baud, node, broker
sudo cp deploy/squachwatch/squachwatch-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now squachwatch-bridge
journalctl -u squachwatch-bridge -f
```
Baud: **2000000** for `cyd*` firmware builds, **921600** for most other boards
(`platformio.ini` in SquachWatch-CYD). ESP32-S3/Tab5 boards use native USB, so
any baud works.

## How it works
- The bridge sends the firmware console command `LOG` every `SQW_POLL` s. The
  reply is CSV `row,type,mac,rssi,hits,state,when`, newest first, from a
  200-row ring. It diffs each reply against the last one by `(type, mac)`.
- On connect it sends `TIME <epoch>` so the board's SD log gets real UTC
  stamps (the boards have no RTC).
- The port is opened with DTR/RTS low, so the CYD's auto-reset circuit doesn't
  reboot the board on connect.
- If the board's screen lock is on, it refuses every command. The bridge
  logs `board is locked` and retries every 30 s.

## MQTT
| Topic | Retained | Payload |
|---|---|---|
| `signot/squachwatch/<node>/status` | yes (LWT) | `online` / `offline` |
| `signot/squachwatch/<node>/state` | yes | `{here, tracked, by_type, alert, alert_hits, last, updated}` |
| `signot/squachwatch/<node>/detection` | no | `{type, mac, rssi, hits, state, when, event: new\|here\|gone, ts, node, alert_type}` |

Home Assistant discovery creates the device **SquachWatch &lt;node&gt;** with three
entities: `binary_sensor` *Surveillance alert* (on while FLOCK, AXON, ALPR,
SKIMMER, DRONE, RAVEN, META, HACKER, DEAUTH, EVIL TWIN or MY ALERT was "here"
within `SQW_ALERT_MIN` minutes), `sensor` *Devices here* (counts per type as
attributes), and `sensor` *Last detection*.

Example HA automation trigger:
```yaml
trigger:
  - platform: mqtt
    topic: signot/squachwatch/+/detection
condition: "{{ trigger.payload_json.event == 'new' and trigger.payload_json.type in ['FLOCK','AXON','SKIMMER'] }}"
```

## Payload
`payloads/reconnaissance/squachwatch.py` lists the latest event per device,
newest first. Alert types are red and devices that are gone are grey.
UP/DOWN scroll, KEY1 toggles alerts only, KEY2 marks everything seen (the
header goes back to green), KEY3 exits.

## Other SquachWatch outputs (no bridge needed)
- SD card: `/squachwatch-YYYYMMDD.log`, CSV `time,type,rssi,MAC,channel,vendor,name[,lat,lon,acc]`
- GPS builds: `/wigle-YYYYMMDD-HHMM.csv` (WigleWifi-1.6), ready for WiGLE upload
- Serial console: `SD LS`, `SD CAT <path>`, `BLACKBOX`, `GPS`, `WARDRIVE ON|OFF`
