#!/usr/bin/env python3
"""
SquachWatch -> MQTT bridge (signot fleet)
=========================================

Owns the USB serial port of one SquachWatch-CYD board, polls its detection
ring with the firmware's `LOG` console command, and turns what changed into
MQTT messages plus a local JSONL loot file.  No firmware change is needed:
`LOG` exists on every SquachWatch build.

This process is the ONLY thing that should open the board's serial port.
Everything else (the Raspyjack payload, Home Assistant) reads MQTT or the
loot file.

Topics (prefix defaults to signot/squachwatch/<node>):
  <prefix>/status      online | offline            (retained, LWT)
  <prefix>/state       JSON summary                (retained)
  <prefix>/detection   JSON per event: new / here / gone   (not retained)
  homeassistant/...    MQTT discovery configs      (retained)

Config is environment variables (or the same names as --flags):
  SQW_PORT        serial device, prefer /dev/serial/by-id/...  (required)
  SQW_BAUD        2000000 for cyd* builds, 921600 for most others (default 2000000)
  SQW_NODE        node name in topics (default: hostname)
  SQW_POLL        seconds between LOG polls (default 5)
  SQW_ALERT_MIN   minutes a high-value sighting keeps the alert on (default 10)
  SQW_LOOT        loot dir (default: <raspyjack>/loot/SquachWatch)
  MQTT_HOST       broker (default 127.0.0.1); MQTT_PORT (1883)
  MQTT_USER / MQTT_PASS   optional; never commit them
  SQW_NO_MQTT=1   run without a broker (loot file + stdout only)
"""

import argparse
import json
import os
import re
import signal
import socket
import sys
import time
from datetime import datetime, timezone

try:
    import serial  # pyserial (apt: python3-serial)
except ImportError:  # pragma: no cover
    sys.exit("pyserial missing: apt install python3-serial")

try:
    import paho.mqtt.client as mqtt  # apt: python3-paho-mqtt
except ImportError:  # pragma: no cover
    mqtt = None

# Types worth a red light.  Names are exactly detectionTypeName() in the
# firmware's include/state.h (note "EVIL TWIN" and "MY ALERT" have spaces).
ALERT_TYPES = {"FLOCK", "AXON", "ALPR", "SKIMMER", "DRONE", "RAVEN", "META",
               "HACKER", "DEAUTH", "EVIL TWIN", "MY ALERT"}

ROW_RE = re.compile(
    r"^(\d+),([A-Z_ ]+),([0-9a-f]{2}(?::[0-9a-f]{2}){5}),(-?\d+),(\d+),(here|gone|KEPT),(.*)$")
COUNT_RE = re.compile(r"^\[log\] (\d+) rows")
LOCKED_MARK = "[security] locked"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_log(lines):
    """LOG dump lines -> list of row dicts.  Other console chatter that lands
    in the middle of a dump is ignored."""
    rows = []
    for ln in lines:
        m = ROW_RE.match(ln.strip())
        if m:
            rows.append({"type": m.group(2), "mac": m.group(3).upper(),
                         "rssi": int(m.group(4)), "hits": int(m.group(5)),
                         "state": m.group(6), "when": m.group(7)})
    return rows


class Board:
    """The serial side.  Opened with DTR/RTS low so the CYD's auto-reset
    circuit does not reboot the board every time the bridge connects."""

    def __init__(self, port, baud):
        self.port, self.baud, self.ser = port, baud, None

    def open(self):
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = self.port, self.baud, 0.2
        s.dtr = False
        s.rts = False
        s.open()
        self.ser = s
        time.sleep(0.3)
        s.reset_input_buffer()

    def close(self):
        try:
            if self.ser:
                self.ser.close()
        finally:
            self.ser = None

    def command(self, cmd, quiet=0.6, limit=4.0):
        """Send one console line; return the lines that come back until the
        board has been quiet for `quiet` s (or `limit` s total)."""
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\n").encode())
        self.ser.flush()
        out, buf = [], b""
        start = last = time.monotonic()
        expected = None
        while True:
            chunk = self.ser.read(4096)
            t = time.monotonic()
            if chunk:
                last = t
                buf += chunk
                *done, buf = buf.split(b"\n")
                for raw in done:
                    ln = raw.decode("utf-8", "replace").rstrip("\r")
                    out.append(ln)
                    m = COUNT_RE.match(ln)
                    if m:
                        expected = int(m.group(1))
                if expected is not None and sum(1 for x in out if ROW_RE.match(x)) >= expected:
                    break
            if t - last > quiet or t - start > limit:
                break
        return out


class Bus:
    """The MQTT side.  Missing paho or SQW_NO_MQTT -> prints instead."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.prefix = f"signot/squachwatch/{cfg.node}"
        self.client = None
        if cfg.no_mqtt or mqtt is None:
            if mqtt is None and not cfg.no_mqtt:
                print("[bridge] paho-mqtt missing (apt install python3-paho-mqtt); stdout only",
                      flush=True)
            return
        cid = f"squachwatch-{cfg.node}"
        try:  # paho 2.x
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        except AttributeError:  # paho 1.x
            self.client = mqtt.Client(client_id=cid)
        if cfg.mqtt_user:
            self.client.username_pw_set(cfg.mqtt_user, cfg.mqtt_pass or None)
        self.client.will_set(f"{self.prefix}/status", "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.reconnect_delay_set(2, 60)
        self.client.connect_async(cfg.mqtt_host, cfg.mqtt_port, keepalive=30)
        self.client.loop_start()

    def _on_connect(self, client, *_a, **_k):
        print(f"[bridge] mqtt connected to {self.cfg.mqtt_host}", flush=True)
        self.discovery()
        client.publish(f"{self.prefix}/status", "online", qos=1, retain=True)

    def pub(self, sub, payload, retain=False):
        topic = f"{self.prefix}/{sub}"
        data = payload if isinstance(payload, str) else json.dumps(payload)
        if self.client:
            self.client.publish(topic, data, qos=0, retain=retain)
        else:
            print(f"{topic} {data}", flush=True)

    def discovery(self):
        """Home Assistant MQTT discovery: three entities on one device."""
        if not self.client:
            return
        node = self.cfg.node
        dev = {"identifiers": [f"squachwatch_{node}"], "name": f"SquachWatch {node}",
               "manufacturer": "SquachWare", "model": "SquachWatch-CYD"}
        avail = {"availability_topic": f"{self.prefix}/status"}
        state = f"{self.prefix}/state"
        ents = {
            ("binary_sensor", "alert"): {
                "name": "Surveillance alert", "device_class": "safety",
                "value_template": "{{ 'ON' if value_json.alert else 'OFF' }}",
                "json_attributes_topic": state,
                "json_attributes_template": "{{ value_json.alert_hits | tojson }}"},
            ("sensor", "here"): {
                "name": "Devices here", "unit_of_measurement": "devices",
                "state_class": "measurement",
                "value_template": "{{ value_json.here }}",
                "json_attributes_topic": state,
                "json_attributes_template": "{{ value_json.by_type | tojson }}"},
            ("sensor", "last"): {
                "name": "Last detection", "icon": "mdi:cctv",
                "value_template": "{{ value_json.last.type if value_json.last else 'none' }}",
                "json_attributes_topic": state,
                "json_attributes_template": "{{ (value_json.last or {}) | tojson }}"},
        }
        for (comp, key), extra in ents.items():
            uid = f"squachwatch_{node}_{key}"
            cfg = {"unique_id": uid, "object_id": uid, "state_topic": state,
                   "device": dev, **avail, **extra}
            self.client.publish(f"homeassistant/{comp}/{uid}/config",
                                json.dumps(cfg), qos=1, retain=True)

    def close(self):
        if self.client:
            self.client.publish(f"{self.prefix}/status", "offline", qos=1, retain=True)
            time.sleep(0.3)
            self.client.loop_stop()
            self.client.disconnect()


class Tracker:
    """Diffs successive LOG dumps and keeps the alert window."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.seen = {}          # (type, mac) -> row
        self.alerts = {}        # (type, mac) -> epoch last seen 'here'
        self.last = None

    def update(self, rows):
        events, now = [], time.time()
        current = {}
        for r in rows:
            key = (r["type"], r["mac"])
            if key in current:  # ring can hold a re-sighting; newest first wins
                continue
            current[key] = r
            old = self.seen.get(key)
            if old is None:
                ev = "new"
            elif old["state"] != r["state"]:
                ev = r["state"] if r["state"] in ("here", "gone") else None
            else:
                ev = None
            if ev:
                events.append({**r, "event": ev, "ts": now_iso(), "node": self.cfg.node,
                               "alert_type": r["type"] in ALERT_TYPES})
            if r["type"] in ALERT_TYPES and r["state"] == "here":
                self.alerts[key] = now
        self.seen.update(current)
        if events:
            self.last = events[0]
        window = self.cfg.alert_min * 60
        self.alerts = {k: t for k, t in self.alerts.items() if now - t < window}
        return events

    def summary(self):
        here = [r for r in self.seen.values() if r["state"] == "here"]
        by_type = {}
        for r in here:
            by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        return {"here": len(here), "tracked": len(self.seen), "by_type": by_type,
                "alert": bool(self.alerts),
                "alert_hits": {f"{t} {m}": datetime.fromtimestamp(ts, timezone.utc)
                               .strftime("%H:%M:%SZ") for (t, m), ts in self.alerts.items()},
                "last": self.last, "updated": now_iso()}


def loot_write(loot_dir, events):
    if not events:
        return
    os.makedirs(loot_dir, exist_ok=True)
    path = os.path.join(loot_dir, f"squachwatch-{datetime.now().strftime('%Y%m%d')}.jsonl")
    with open(path, "a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def config():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    env = os.environ.get
    p = argparse.ArgumentParser(description="SquachWatch serial -> MQTT bridge")
    p.add_argument("--port", default=env("SQW_PORT"))
    p.add_argument("--baud", type=int, default=int(env("SQW_BAUD", "2000000")))
    p.add_argument("--node", default=env("SQW_NODE", socket.gethostname()))
    p.add_argument("--poll", type=float, default=float(env("SQW_POLL", "5")))
    p.add_argument("--alert-min", type=float, default=float(env("SQW_ALERT_MIN", "10")))
    p.add_argument("--loot", default=env("SQW_LOOT", os.path.join(root, "loot", "SquachWatch")))
    p.add_argument("--mqtt-host", default=env("MQTT_HOST", "127.0.0.1"))
    p.add_argument("--mqtt-port", type=int, default=int(env("MQTT_PORT", "1883")))
    p.add_argument("--mqtt-user", default=env("MQTT_USER"))
    p.add_argument("--mqtt-pass", default=env("MQTT_PASS"))
    p.add_argument("--no-mqtt", action="store_true", default=env("SQW_NO_MQTT") == "1")
    p.add_argument("--once", action="store_true", help="one poll, then exit (testing)")
    cfg = p.parse_args()
    if not cfg.port:
        p.error("set SQW_PORT or --port (prefer /dev/serial/by-id/...)")
    return cfg


def main():
    cfg = config()
    bus, track, board = Bus(cfg), Tracker(cfg), Board(cfg.port, cfg.baud)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    backoff = 2
    try:
        while not stop["now"]:
            try:
                if board.ser is None:
                    board.open()
                    print(f"[bridge] opened {cfg.port} @ {cfg.baud}", flush=True)
                    # The boards have no RTC; give them the Pi's clock so the
                    # SD log rows carry real UTC stamps.
                    board.command(f"TIME {int(time.time())}", quiet=0.3, limit=1.5)
                    backoff = 2
                lines = board.command("LOG")
                if any(LOCKED_MARK in ln for ln in lines):
                    print("[bridge] board is locked; unlock it on the screen", flush=True)
                    time.sleep(30)
                    continue
                events = track.update(parse_log(lines))
                for e in events:
                    bus.pub("detection", e)
                loot_write(cfg.loot, events)
                bus.pub("state", track.summary(), retain=True)
                if cfg.once:
                    break
                time.sleep(cfg.poll)
            except (serial.SerialException, OSError) as e:
                print(f"[bridge] serial: {e}; retry in {backoff}s", flush=True)
                board.close()
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
    finally:
        board.close()
        bus.close()


if __name__ == "__main__":
    main()
