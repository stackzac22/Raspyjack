#!/usr/bin/env python3
"""
signot mesh bridge: Meshtastic radio <-> MQTT
=============================================

Owns the Pi's own Meshtastic radio (USB serial) and joins it to the fleet:

  UPLINK   mesh -> MQTT/Home Assistant
    signot/mesh/nodes/<!id>     retained JSON per node (name, hw, position, battery, snr, hops)
    signot/mesh/messages        JSON per text message heard
    signot/mesh/<self>/status   online/offline (LWT)
    signot/mesh/<self>/state    retained summary (nodes heard, last message)
    homeassistant/...           discovery: node count, last message, and a
                                device_tracker for every id in MESH_MY_NODES

  DOWNLINK fleet alerts -> mesh (shows up in Wardrive Go chat)
    signot/squachwatch/+/detection   new sightings of alert types
    kismet/alerts/wifi               severity >= MESH_KISMET_MIN_SEV, or deauth/spoof/rogue
  sent ONLY on the private channel named MESH_CHANNEL (default "signot"),
  never on the primary/public channel, with dedupe and a token-bucket airtime guard.

Config (environment, see deploy/mesh/signot-mesh-bridge.env.example):
  MESH_PORT            /dev/serial/by-id/... of the Pi's Meshtastic radio (required)
  MESH_CHANNEL         channel name for alerts (default signot)
  MESH_MY_NODES        comma list of node ids to track in HA, e.g. !a1b2c3d4,!deadbeef
  MESH_KISMET_MIN_SEV  default 7
  MESH_RATE_SEC        seconds per token (default 60); MESH_BURST (default 3)
  MESH_DEDUPE_MIN      default 30
  MESH_DRY_RUN=1       log alerts instead of transmitting
  MQTT_HOST/MQTT_PORT/MQTT_USER/MQTT_PASS
"""

import json
import os
import re
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    sys.exit("paho-mqtt missing: apt install python3-paho-mqtt")

ALERT_SQW = {"FLOCK", "AXON", "ALPR", "SKIMMER", "DRONE", "RAVEN", "META",
             "HACKER", "DEAUTH", "EVIL TWIN", "MY ALERT"}
KISMET_CATS = {"deauth", "spoof", "rogue"}
MAX_TEXT = 120


def now_iso(ts=None):
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hhmm(ts=None):
    return time.strftime("%H:%M", time.localtime(ts or time.time()))


# ---------------------------------------------------------------- downlink --

def format_alert(topic, payload):
    """MQTT alert -> (dedupe_key, short text) or None if it should not go out."""
    try:
        d = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    if topic.startswith("signot/squachwatch/") and topic.endswith("/detection"):
        if d.get("event") != "new" or d.get("type") not in ALERT_SQW:
            return None
        mac = str(d.get("mac", ""))
        node = d.get("node") or topic.split("/")[2]
        text = f"SQW {d['type']} {d.get('rssi', '?')}dB ..{mac[-5:]} @{node} {hhmm()}"
        return ("sqw", d["type"], mac), text[:MAX_TEXT]
    if topic == "kismet/alerts/wifi":
        sev = int(d.get("severity") or 0)
        cat = str(d.get("category") or "")
        if sev < KISMET_MIN_SEV and cat not in KISMET_CATS:
            return None
        mac = str(d.get("source_mac") or "")
        ch = d.get("channel")
        bits = [f"KIS {d.get('type', cat or 'ALERT')}"]
        if ch:
            bits.append(f"ch{ch}")
        if mac:
            bits.append(mac[:8] + "..")
        bits.append(f"@{d.get('node', '?')} {hhmm(d.get('ts'))}")
        return ("kis", d.get("type"), mac), " ".join(bits)[:MAX_TEXT]
    return None


class AirtimeGuard:
    """Dedupe + token bucket. Alerts that find no token are counted and folded
    into a single '+N more' message when the next token arrives."""

    def __init__(self, rate_sec, burst, dedupe_sec, clock=time.monotonic):
        self.rate, self.burst, self.dedupe = rate_sec, burst, dedupe_sec
        self.clock = clock
        self.tokens = float(burst)
        self.last = clock()
        self.seen = {}
        self.held = 0

    def _refill(self):
        t = self.clock()
        self.tokens = min(self.burst, self.tokens + (t - self.last) / self.rate)
        self.last = t

    def offer(self, key):
        """True -> send now. False -> dropped (duplicate) or held for '+N more'."""
        t = self.clock()
        self.seen = {k: v for k, v in self.seen.items() if t - v < self.dedupe}
        if key in self.seen:
            return False
        self.seen[key] = t
        self._refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        self.held += 1
        return False

    def flush_held(self):
        """Text for the folded overflow, if a token is available for it."""
        if not self.held:
            return None
        self._refill()
        if self.tokens < 1:
            return None
        self.tokens -= 1
        n, self.held = self.held, 0
        return f"+{n} more fleet alerts (see HA)"


# ------------------------------------------------------------------ uplink --

def node_record(nid, rec):
    """Meshtastic node DB entry -> flat JSON for MQTT."""
    user = rec.get("user") or {}
    pos = rec.get("position") or {}
    met = rec.get("deviceMetrics") or {}
    lat = pos.get("latitude", pos.get("latitudeI", 0) / 1e7 if pos.get("latitudeI") else None)
    lon = pos.get("longitude", pos.get("longitudeI", 0) / 1e7 if pos.get("longitudeI") else None)
    if lat in (0, 0.0) and lon in (0, 0.0):
        lat = lon = None
    return {
        "id": nid, "long_name": user.get("longName"), "short_name": user.get("shortName"),
        "hw": user.get("hwModel"), "role": user.get("role"),
        "lat": lat, "lon": lon, "alt": pos.get("altitude"),
        "battery": met.get("batteryLevel"), "voltage": met.get("voltage"),
        "snr": rec.get("snr"), "hops": rec.get("hopsAway"),
        "last_heard": now_iso(rec["lastHeard"]) if rec.get("lastHeard") else None,
    }


def safe_id(nid):
    return re.sub(r"[^0-9a-zA-Z_]", "", str(nid))


class Bridge:
    def __init__(self, cfg, iface_factory):
        self.cfg = cfg
        self.iface_factory = iface_factory
        self.iface = None
        self.chan_index = None
        self.self_id = cfg["node_name"]
        self.nodes = {}
        self.last_msg = None
        self.lock = threading.Lock()
        self.guard = AirtimeGuard(cfg["rate_sec"], cfg["burst"], cfg["dedupe_min"] * 60)
        self.stop = threading.Event()
        self.mq = self._mqtt()

    # ---- MQTT
    def _mqtt(self):
        cid = f"signot-mesh-{self.cfg['node_name']}"
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        except AttributeError:
            c = mqtt.Client(client_id=cid)
        if self.cfg["mqtt_user"]:
            c.username_pw_set(self.cfg["mqtt_user"], self.cfg["mqtt_pass"] or None)
        c.will_set(self.t("status"), "offline", qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_message = self._on_mqtt
        c.reconnect_delay_set(2, 60)
        c.connect_async(self.cfg["mqtt_host"], self.cfg["mqtt_port"], keepalive=30)
        c.loop_start()
        return c

    def t(self, sub):
        return f"signot/mesh/{self.cfg['node_name']}/{sub}"

    def _on_connect(self, client, *_a, **_k):
        print(f"[mesh] mqtt connected to {self.cfg['mqtt_host']}", flush=True)
        client.subscribe([("signot/squachwatch/+/detection", 0), ("kismet/alerts/wifi", 0)])
        self._discovery()
        client.publish(self.t("status"), "online" if self.iface else "no-radio", qos=1, retain=True)

    def _on_mqtt(self, _c, _u, msg):
        out = format_alert(msg.topic, msg.payload)
        if not out:
            return
        key, text = out
        if self.guard.offer(key):
            self.send(text)
        else:
            print(f"[mesh] held/deduped: {text}", flush=True)

    def _discovery(self):
        n = self.cfg["node_name"]
        dev = {"identifiers": [f"signot_mesh_{n}"], "name": f"Mesh bridge {n}",
               "manufacturer": "Meshtastic", "model": "signot mesh bridge"}
        base = {"availability_topic": self.t("status"), "device": dev, "state_topic": self.t("state")}
        ents = {
            ("sensor", "nodes"): {"name": "Mesh nodes heard", "icon": "mdi:access-point-network",
                                  "unit_of_measurement": "nodes", "state_class": "measurement",
                                  "value_template": "{{ value_json.nodes }}",
                                  "json_attributes_topic": self.t("state"),
                                  "json_attributes_template": "{{ value_json.node_names | tojson }}"},
            ("sensor", "last_msg"): {"name": "Last mesh message", "icon": "mdi:message-text",
                                     "value_template": "{{ (value_json.last_msg or {}).text | default('none') | truncate(250) }}",
                                     "json_attributes_topic": self.t("state"),
                                     "json_attributes_template": "{{ (value_json.last_msg or {}) | tojson }}"},
        }
        for (comp, key), extra in ents.items():
            uid = f"signot_mesh_{n}_{key}"
            self.mq.publish(f"homeassistant/{comp}/{uid}/config",
                            json.dumps({"unique_id": uid, "object_id": uid, **base, **extra}),
                            qos=1, retain=True)
        for nid in self.cfg["my_nodes"]:
            sid = safe_id(nid)
            uid = f"signot_mesh_tracker_{sid}"
            self.mq.publish(f"homeassistant/device_tracker/{uid}/config", json.dumps({
                "unique_id": uid, "object_id": uid, "name": f"Mesh {nid}", "source_type": "gps",
                "json_attributes_topic": f"signot/mesh/tracker/{sid}",
                "device": {"identifiers": [uid], "name": f"Meshtastic {nid}", "manufacturer": "Meshtastic"},
            }), qos=1, retain=True)

    def publish_state(self):
        with self.lock:
            names = {k: (v.get("long_name") or v.get("short_name") or k) for k, v in self.nodes.items()}
            st = {"nodes": len(self.nodes), "node_names": names, "last_msg": self.last_msg,
                  "channel": self.cfg["channel"], "channel_index": self.chan_index,
                  "updated": now_iso()}
        self.mq.publish(self.t("state"), json.dumps(st), retain=True)

    # ---- radio
    def connect_radio(self):
        self.iface = self.iface_factory(self.cfg["port"])
        self.chan_index = self.find_channel()
        me = getattr(self.iface, "getMyNodeInfo", lambda: None)() or {}
        print(f"[mesh] radio up: {(me.get('user') or {}).get('longName', '?')} "
              f"alert channel '{self.cfg['channel']}' -> index {self.chan_index}", flush=True)
        for nid, rec in (getattr(self.iface, "nodes", None) or {}).items():
            self.update_node(nid, rec)
        self.mq.publish(self.t("status"), "online", qos=1, retain=True)
        self.publish_state()

    def find_channel(self):
        """Index of the private alert channel by name. None -> never transmit."""
        want = self.cfg["channel"].lower()
        chans = getattr(getattr(self.iface, "localNode", None), "channels", None) or []
        for ch in chans:
            name = getattr(getattr(ch, "settings", None), "name", "") or ""
            role = getattr(ch, "role", None)
            idx = getattr(ch, "index", None)
            # role 1 = PRIMARY: refuse to put fleet alerts on the primary channel
            if name.lower() == want and idx and role != 1:
                return idx
        print(f"[mesh] WARNING: no secondary channel named '{self.cfg['channel']}' on the radio; "
              "alerts will NOT be transmitted (add it: meshtastic --ch-add signot)", flush=True)
        return None

    def on_receive(self, packet, interface=None):
        dec = packet.get("decoded") or {}
        port = dec.get("portnum")
        nid = packet.get("fromId") or (f"!{packet['from']:08x}" if isinstance(packet.get("from"), int) else None)
        if not nid:
            return
        rec = (getattr(self.iface, "nodes", None) or {}).get(nid)
        if rec:
            self.update_node(nid, rec)
        if port == "TEXT_MESSAGE_APP":
            msg = {"from": nid, "from_name": (self.nodes.get(nid) or {}).get("long_name"),
                   "channel": packet.get("channel", 0), "to": packet.get("toId"),
                   "text": dec.get("text", ""), "rx_snr": packet.get("rxSnr"),
                   "rx_rssi": packet.get("rxRssi"), "ts": now_iso()}
            with self.lock:
                self.last_msg = msg
            self.mq.publish("signot/mesh/messages", json.dumps(msg))
            self.publish_state()

    def update_node(self, nid, rec):
        r = node_record(nid, rec)
        with self.lock:
            self.nodes[nid] = r
        self.mq.publish(f"signot/mesh/nodes/{safe_id(nid)}", json.dumps(r), retain=True)
        if nid in self.cfg["my_nodes"] and r["lat"] is not None:
            self.mq.publish(f"signot/mesh/tracker/{safe_id(nid)}", json.dumps({
                "latitude": r["lat"], "longitude": r["lon"], "gps_accuracy": 25,
                "battery_level": r["battery"], "altitude": r["alt"], "last_heard": r["last_heard"]}),
                retain=True)

    def send(self, text):
        if self.cfg["dry_run"]:
            print(f"[mesh] DRY-RUN would send on ch{self.chan_index}: {text}", flush=True)
            return
        if self.iface is None or self.chan_index is None:
            print(f"[mesh] not sent (no radio or no '{self.cfg['channel']}' channel): {text}", flush=True)
            return
        try:
            self.iface.sendText(text, channelIndex=self.chan_index)
            print(f"[mesh] sent ch{self.chan_index}: {text}", flush=True)
        except Exception as e:  # radio dropped mid-send
            print(f"[mesh] send failed: {e}", flush=True)

    def on_lost(self, interface=None):
        print("[mesh] radio connection lost", flush=True)
        self.iface = None
        self.mq.publish(self.t("status"), "no-radio", qos=1, retain=True)

    def run(self):
        backoff = 3
        while not self.stop.is_set():
            if self.iface is None and self.cfg["port"]:
                try:
                    self.connect_radio()
                    backoff = 3
                except Exception as e:
                    print(f"[mesh] radio: {e}; retry in {backoff}s", flush=True)
                    self.iface = None
                    self.stop.wait(backoff)
                    backoff = min(backoff * 2, 120)
                    continue
            extra = self.guard.flush_held()
            if extra:
                self.send(extra)
            self.stop.wait(5)

    def close(self):
        self.stop.set()
        try:
            if self.iface:
                self.iface.close()
        except Exception:
            pass
        self.mq.publish(self.t("status"), "offline", qos=1, retain=True)
        time.sleep(0.3)
        self.mq.loop_stop()
        self.mq.disconnect()


def real_iface_factory(bridge):
    def make(port):
        import meshtastic.serial_interface
        from pubsub import pub
        pub.subscribe(bridge.on_receive, "meshtastic.receive")
        pub.subscribe(bridge.on_lost, "meshtastic.connection.lost")
        return meshtastic.serial_interface.SerialInterface(devPath=port)
    return make


def load_cfg():
    e = os.environ.get
    return {
        "port": e("MESH_PORT", ""),
        "channel": e("MESH_CHANNEL", "signot"),
        "my_nodes": [x.strip() for x in e("MESH_MY_NODES", "").split(",") if x.strip()],
        "rate_sec": float(e("MESH_RATE_SEC", "60")),
        "burst": int(e("MESH_BURST", "3")),
        "dedupe_min": float(e("MESH_DEDUPE_MIN", "30")),
        "dry_run": e("MESH_DRY_RUN") == "1",
        "node_name": e("MESH_NODE", socket.gethostname().lower()),
        "mqtt_host": e("MQTT_HOST", "127.0.0.1"),
        "mqtt_port": int(e("MQTT_PORT", "1883")),
        "mqtt_user": e("MQTT_USER"), "mqtt_pass": e("MQTT_PASS"),
    }


KISMET_MIN_SEV = int(os.environ.get("MESH_KISMET_MIN_SEV", "7"))


def main():
    cfg = load_cfg()
    if not cfg["port"] and not cfg["dry_run"]:
        sys.exit("set MESH_PORT (the Pi's Meshtastic radio, /dev/serial/by-id/...) or MESH_DRY_RUN=1")
    holder = {}
    b = Bridge(cfg, lambda port: real_iface_factory(holder["b"])(port))
    holder["b"] = b
    signal.signal(signal.SIGTERM, lambda *_: b.stop.set())
    signal.signal(signal.SIGINT, lambda *_: b.stop.set())
    try:
        b.run()
    finally:
        b.close()


if __name__ == "__main__":
    main()
