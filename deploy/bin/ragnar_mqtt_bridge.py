#!/usr/bin/env python3
"""
ragnar_mqtt_bridge.py — publish Ragnar WiFi-monitoring + AI summary onto the
home Mosquitto broker (the one Home Assistant uses), alongside the existing
Kismet bridge. Isolated, read-only with respect to Ragnar; never touches the
LCD or the Raspyjack install.

  Ragnar (local :8091 / data files)  --poll-->  this bridge
        --paho-mqtt-->  Mosquitto @ HA   topic prefix: homeassistant/wifi/ragnar/

Topics published:
  homeassistant/wifi/ragnar/status   retained "online"/"offline"  (LWT)
  homeassistant/wifi/ragnar/summary  retained JSON  (networks/clients/rogue/AI)
  homeassistant/wifi/ragnar/alert    JSON per rogue-AP / event    (not retained)

Config: KEY=VALUE lines in /etc/raspyjack/ragnar_mqtt.conf (overrides DEFAULTS),
mirroring kismet_mqtt.conf. Path overridable via $RAGNAR_MQTT_CONF.
"""
import json
import os
import random
import ssl
import sys
import time
from datetime import datetime, timezone

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.stderr.write("paho-mqtt not installed: pip install paho-mqtt\n")
    sys.exit(1)

DEFAULTS = {
    # MQTT broker (Home Assistant's Mosquitto, on the tailnet) — reuses the
    # same known-good 'netsurvey' account the Kismet bridge uses.
    "MQTT_HOST": "100.127.43.75",
    "MQTT_PORT": "1883",
    "MQTT_TLS": "false",
    "MQTT_USER": "netsurvey",
    "MQTT_PASS": "",                      # set in /etc/raspyjack/ragnar_mqtt.conf
    "MQTT_CLIENT_ID": "ragnar-bridge",
    "TOPIC_PREFIX": "homeassistant/wifi/ragnar",
    # behaviour
    "NODE_NAME": "raspyjackboy",
    "POLL_SECONDS": "30",
    "RAGNAR_URL": "http://127.0.0.1:8091",
    "RAGNAR_USER": "tec",
    "RAGNAR_PASS": "",
}


def load_conf():
    cfg = dict(DEFAULTS)
    path = os.environ.get("RAGNAR_MQTT_CONF", "/etc/raspyjack/ragnar_mqtt.conf")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    # env always wins
    for k in cfg:
        if k in os.environ:
            cfg[k] = os.environ[k]
    return cfg


import threading as _threading

_sess = None
_sess_lock = _threading.Lock()


def _ragnar_login(cfg):
    """Return a freshly logged-in requests.Session to the local Ragnar API."""
    import requests
    url = cfg.get("RAGNAR_URL", "http://127.0.0.1:8091").rstrip("/")
    s = requests.Session()
    s.post(url + "/api/auth/login",
           json={"username": cfg.get("RAGNAR_USER", ""),
                 "password": cfg.get("RAGNAR_PASS", "")}, timeout=10)
    return s


def get_ragnar_data(cfg):
    """Real Ragnar summary from the local web API (/api/ai/network-summary).

    Logs into http://127.0.0.1:8091 (creds in ragnar_mqtt.conf). The Ragnar
    AI service caches summaries ~1h, so frequent polling does NOT burn tokens.
    Never synthesizes alerts (source is now 'ragnar' -> would speak on the M5).
    Degrades gracefully (empty ai_summary, no alerts) if Ragnar/AI is down.
    """
    global _sess
    now = datetime.now(timezone.utc).isoformat()
    url = cfg.get("RAGNAR_URL", "http://127.0.0.1:8091").rstrip("/")
    base = {
        "timestamp": now,
        "node": cfg["NODE_NAME"],
        "monitoring": True,
        "networks_seen": 0,
        "clients_seen": 0,
        "rogue_aps": 0,
        "ai_risk_score": 0,
        "ai_summary": "",
        "_alerts": [],            # real alerts only; never synthesize
        "source": "ragnar",
    }
    try:
        with _sess_lock:
            if _sess is None:
                _sess = _ragnar_login(cfg)
            r = _sess.get(url + "/api/ai/network-summary", timeout=60)
            if r.status_code == 401 or '"Unauthorized"' in r.text:
                _sess = _ragnar_login(cfg)
                r = _sess.get(url + "/api/ai/network-summary", timeout=60)
        j = r.json()
        nd = j.get("network_data", {}) or {}
        vulns = int(nd.get("vulnerability_count", 0) or 0)
        creds = int(nd.get("credential_count", 0) or 0)
        targets = int(nd.get("target_count", 0) or 0)
        ports = int(nd.get("port_count", 0) or 0)
        base.update({
            "networks_seen": targets,
            "clients_seen": ports,
            "ai_risk_score": min(100, vulns // 2 + creds * 20),
            "ai_summary": (j.get("summary") or "").strip(),
            "vulnerabilities": vulns,
            "credentials": creds,
            "open_ports": ports,
            "ai_enabled": bool(j.get("enabled")),
        })
    except Exception as exc:
        print("[ragnar-bridge] data fetch failed: %r" % (exc,), flush=True)
        _sess = None
    return base


def main():
    cfg = load_conf()
    prefix = cfg["TOPIC_PREFIX"].rstrip("/")
    t_status = f"{prefix}/status"
    t_summary = f"{prefix}/summary"
    t_alert = f"{prefix}/alert"
    poll = int(cfg["POLL_SECONDS"])

    client = mqtt.Client(client_id=cfg["MQTT_CLIENT_ID"], clean_session=True)
    if cfg.get("MQTT_USER"):
        client.username_pw_set(cfg["MQTT_USER"], cfg.get("MQTT_PASS", ""))
    if cfg["MQTT_TLS"].lower() in ("1", "true", "yes"):
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
    client.will_set(t_status, "offline", qos=1, retain=True)

    def on_connect(c, u, flags, rc, *args):
        if rc == 0:
            c.publish(t_status, "online", qos=1, retain=True)
            print(f"[ragnar-bridge] connected MQTT {cfg['MQTT_HOST']}:{cfg['MQTT_PORT']}",
                  flush=True)
        else:
            print(f"[ragnar-bridge] connect failed rc={rc}", flush=True)

    client.on_connect = on_connect
    client.connect_async(cfg["MQTT_HOST"], int(cfg["MQTT_PORT"]), keepalive=30)
    client.loop_start()
    print(f"[ragnar-bridge] up — polling Ragnar every {poll}s -> {prefix}/...",
          flush=True)

    try:
        while True:
            data = get_ragnar_data(cfg)
            alerts = data.pop("_alerts", [])
            client.publish(t_summary, json.dumps(data), qos=1, retain=True)
            for a in alerts:
                a = dict(a, timestamp=data["timestamp"], node=data["node"],
                         source=data["source"])
                client.publish(t_alert, json.dumps(a), qos=0, retain=False)
            print(f"[ragnar-bridge] published summary "
                  f"(risk={data['ai_risk_score']} rogue={data['rogue_aps']} "
                  f"alerts={len(alerts)})", flush=True)
            time.sleep(poll)
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(t_status, "offline", qos=1, retain=True)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
