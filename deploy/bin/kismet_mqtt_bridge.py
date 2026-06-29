#!/usr/bin/env python3
"""
kismet_mqtt_bridge.py — runs locally on the RaspyJack.

Reads Kismet alerts live from the local Kismet REST endpoint and republishes a
clean JSON block onto the home Mosquitto broker (the one Home Assistant uses) so
HA can turn Wi-Fi threats into entities / notifications.

  Kismet (localhost:2501)  --poll-->  this bridge  --paho-mqtt-->  Mosquitto @ HA
                                                     topic: kismet/alerts/wifi

It is deliberately stdlib-only except for paho-mqtt:
    pip3 install paho-mqtt            # (PEP668 Kali/Debian: add --break-system-packages
                                      #  or run inside a venv)

Config is read from /etc/raspyjack/kismet_mqtt.conf (KEY=VALUE, # comments).
See kismet_mqtt.conf in this directory for the keys.

Alert categories published:
  * Kismet native alerts (DEAUTHFLOOD, BCASTDISCON, APSPOOF, CHANCHANGE,
    NEWAPHANTOM, etc.) — the full alert engine, ~50 types.
  * Optional "new unmapped MAC" events — devices Kismet sees for the first time
    that are not in the watchlist (enable with NEW_DEVICE_ALERTS=true).

Each MQTT message on kismet/alerts/wifi is a JSON object, e.g.:
  {
    "category": "deauth",
    "type": "DEAUTHFLOOD",
    "severity": 10,
    "text": "Deauthenticate/Disassociate flood on AA:BB:...",
    "source_mac": "AA:BB:CC:DD:EE:FF",
    "dest_mac": "FF:FF:FF:FF:FF:FF",
    "channel": "6",
    "ts": 1718900000,
    "node": "raspyjackboy"
  }
A retained companion message is published to kismet/alerts/wifi/status
("online"/"offline" via LWT) so HA can show bridge availability.
"""

import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.stderr.write(
        "paho-mqtt not installed. Run: pip3 install paho-mqtt "
        "(add --break-system-packages on Kali/Debian, or use a venv)\n"
    )
    sys.exit(1)

# ---------------------------------------------------------------- config -----

DEFAULTS = {
    "KISMET_HOST": "localhost",
    "KISMET_PORT": "2501",
    "KISMET_USER": "admin",
    "KISMET_PASS": "raspyjack",
    "MQTT_HOST": "100.127.43.75",      # homeassistant on the tailnet
    "MQTT_PORT": "1883",
    "MQTT_USER": "netsurvey",          # known-good Mosquitto account; swap for a
    "MQTT_PASS": "039b3860703ff5408b75e088",  # dedicated 'kismet' user if you like
    "MQTT_TLS": "false",
    "MQTT_TOPIC": "kismet/alerts/wifi",
    "MQTT_CLIENT_ID": "kismet-bridge",
    "NODE_NAME": "raspyjackboy",
    "POLL_SECONDS": "5",
    "NEW_DEVICE_ALERTS": "false",      # also report first-seen MACs
    "NEW_DEVICE_MIN_SIGNAL": "-95",    # ignore far/weak first-sightings (dBm)
    "IGNORE_ALERTS": "ROOTUSER",       # comma-sep Kismet alert types to drop (benign self-alerts)
}

CONF_PATH = os.environ.get("KISMET_MQTT_CONF", "/etc/raspyjack/kismet_mqtt.conf")


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.isfile(CONF_PATH):
        with open(CONF_PATH) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                cfg[key.strip()] = val.strip().strip('"').strip("'")
    # env overrides win (handy for testing)
    for key in cfg:
        if key in os.environ:
            cfg[key] = os.environ[key]
    return cfg


# ------------------------------------------------------------- kismet --------

# Map Kismet alert headers to the coarse categories HA cares about.
CATEGORY = {
    "DEAUTHFLOOD": "deauth",
    "BCASTDISCON": "deauth",
    "DISASSOCTRAFFIC": "deauth",
    "NULLPROBERESP": "malformed",
    "APSPOOF": "spoof",
    "BSSTIMESTAMP": "spoof",
    "CRYPTODROP": "downgrade",
    "DHCPCONFLICT": "rogue",
    "NEWAPHANTOM": "rogue",
    "PROBENOJOIN": "recon",
    "CHANCHANGE": "anomaly",
    "ADHOCSPOOF": "spoof",
    "WMMOVERFLOW": "malformed",
    "BEACONRATE": "anomaly",
}


def categorize(header):
    return CATEGORY.get(header, "alert")


class Kismet:
    def __init__(self, cfg):
        self.base = f"http://{cfg['KISMET_HOST']}:{cfg['KISMET_PORT']}"
        token = b64encode(
            f"{cfg['KISMET_USER']}:{cfg['KISMET_PASS']}".encode()
        ).decode()
        self.auth_header = f"Basic {token}"

    def _get(self, path):
        req = urllib.request.Request(self.base + path)
        req.add_header("Authorization", self.auth_header)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def alerts_since(self, ts):
        """Return alerts newer than epoch ts (0 = last buffered)."""
        data = self._get(f"/alerts/last-time/{int(ts)}/alerts.json")
        # Kismet returns either a bare list or {"kismet.alert.list": [...]}.
        if isinstance(data, dict):
            return data.get("kismet.alert.list", [])
        return data

    def devices_since(self, ts):
        data = self._get(f"/devices/last-time/{int(ts)}/devices.json")
        if isinstance(data, dict):
            return data.get("kismet.device.list", data.get("devices", []))
        return data


def alert_to_payload(a, node):
    header = a.get("kismet.alert.header", "ALERT")
    return {
        "category": categorize(header),
        "type": header,
        "severity": a.get("kismet.alert.severity", 0),
        "text": a.get("kismet.alert.text", ""),
        "source_mac": a.get("kismet.alert.source_mac", ""),
        "dest_mac": a.get("kismet.alert.dest_mac", ""),
        "transmitter_mac": a.get("kismet.alert.transmitter_mac", ""),
        "channel": str(a.get("kismet.alert.channel", "")),
        "ts": int(float(a.get("kismet.alert.timestamp", time.time()))),
        "node": node,
    }


def device_to_payload(d, node):
    return {
        "category": "new_mac",
        "type": "NEWDEVICE",
        "severity": 1,
        "text": "New device {} ({}) seen on {}".format(
            d.get("kismet.device.base.macaddr", "?"),
            d.get("kismet.device.base.commonname", "")
            or d.get("kismet.device.base.type", "device"),
            node,
        ),
        "source_mac": d.get("kismet.device.base.macaddr", ""),
        "dest_mac": "",
        "transmitter_mac": d.get("kismet.device.base.macaddr", ""),
        "channel": str(d.get("kismet.device.base.channel", "")),
        "signal": d.get("kismet.device.base.signal", {}).get(
            "kismet.common.signal.last_signal", 0
        ),
        "ts": int(float(d.get("kismet.device.base.last_time", time.time()))),
        "node": node,
    }


# --------------------------------------------------------------- main --------


def main():
    cfg = load_config()
    topic = cfg["MQTT_TOPIC"]
    status_topic = topic + "/status"
    node = cfg["NODE_NAME"]
    poll = max(1, int(cfg["POLL_SECONDS"]))
    do_new_dev = cfg["NEW_DEVICE_ALERTS"].lower() in ("1", "true", "yes")
    min_sig = int(cfg["NEW_DEVICE_MIN_SIGNAL"])
    ignore_alerts = {x.strip().upper() for x in cfg["IGNORE_ALERTS"].split(",") if x.strip()}

    client = mqtt.Client(client_id=cfg["MQTT_CLIENT_ID"], clean_session=True)
    if cfg["MQTT_USER"]:
        client.username_pw_set(cfg["MQTT_USER"], cfg["MQTT_PASS"])
    if cfg["MQTT_TLS"].lower() in ("1", "true", "yes"):
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
    # Last Will: if the bridge dies, HA flips the sensor to unavailable.
    client.will_set(status_topic, "offline", qos=1, retain=True)

    def on_connect(c, u, flags, rc):
        if rc == 0:
            c.publish(status_topic, "online", qos=1, retain=True)
            print(f"[bridge] connected to MQTT {cfg['MQTT_HOST']}:{cfg['MQTT_PORT']}",
                  flush=True)
        else:
            print(f"[bridge] MQTT connect failed rc={rc}", flush=True)

    client.on_connect = on_connect
    client.connect_async(cfg["MQTT_HOST"], int(cfg["MQTT_PORT"]), keepalive=30)
    client.loop_start()

    kismet = Kismet(cfg)
    seen_macs = set()
    # Start "now" so we don't dump the entire historical buffer on first poll.
    last_alert_ts = int(time.time())
    last_dev_ts = int(time.time())

    print(f"[bridge] up — polling Kismet every {poll}s, publishing to {topic} "
          f"(new_mac={'on' if do_new_dev else 'off'})", flush=True)

    while True:
        try:
            alerts = kismet.alerts_since(last_alert_ts)
            for a in alerts:
                ats = int(float(a.get("kismet.alert.timestamp", 0)))
                if ats <= last_alert_ts:
                    continue
                last_alert_ts = max(last_alert_ts, ats)
                payload = alert_to_payload(a, node)
                if payload["type"].upper() in ignore_alerts:
                    continue
                client.publish(topic, json.dumps(payload), qos=1, retain=False)
                print(f"[alert] {payload['category']}/{payload['type']}: "
                      f"{payload['text'][:80]}", flush=True)

            if do_new_dev:
                devices = kismet.devices_since(last_dev_ts)
                for d in devices:
                    dts = int(float(d.get("kismet.device.base.last_time", 0)))
                    last_dev_ts = max(last_dev_ts, dts)
                    mac = d.get("kismet.device.base.macaddr", "")
                    if not mac or mac in seen_macs:
                        continue
                    sig = (d.get("kismet.device.base.signal", {})
                           .get("kismet.common.signal.last_signal", 0))
                    if sig and sig < min_sig:
                        continue
                    seen_macs.add(mac)
                    payload = device_to_payload(d, node)
                    client.publish(topic, json.dumps(payload), qos=0, retain=False)
                    print(f"[new_mac] {mac} sig={sig}", flush=True)

        except urllib.error.HTTPError as e:
            print(f"[bridge] Kismet HTTP {e.code} — check creds/endpoint", flush=True)
        except (urllib.error.URLError, socket.timeout) as e:
            print(f"[bridge] Kismet unreachable: {e}", flush=True)
        except Exception as e:  # keep the daemon alive
            print(f"[bridge] error: {e!r}", flush=True)

        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
