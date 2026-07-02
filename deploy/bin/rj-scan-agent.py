#!/usr/bin/env python3
"""
rj-scan-agent.py — remote control plane for a node's Wi-Fi capture stack.

Subscribes to the home Mosquitto broker (the one Home Assistant uses) and, on
command, runs `rj-scan on|off` locally. Publishes the resulting capture state
back so HA (and the Echo Pyramid voice) can reflect / announce it.

  HA / Pyramid voice  --set-->  raspyjack/<node>/scan/set   ("on"|"off")
                                        |
                                        v
                                   this agent  --runs-->  rj-scan on|off
                                        |
                                        +--> raspyjack/<node>/scan/state  ("on"|"off", retained)
                                        +--> raspyjack/<node>/scan/avail  ("online"|"offline", LWT retained)

This is the counterpart to kismet_mqtt_bridge.py (which only publishes). Same
broker, same account, stdlib-only except paho-mqtt:
    pip3 install paho-mqtt --break-system-packages   # (or a venv)

Config: /etc/raspyjack/scan_agent.conf (KEY=VALUE, # comments). Keys + defaults
below. NODE_NAME is the important per-box value (jack / opi / x1carbon).
"""

import json
import os
import ssl
import subprocess
import sys
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.stderr.write(
        "paho-mqtt not installed. Run: pip3 install paho-mqtt "
        "(add --break-system-packages on Kali/Debian, or use a venv)\n"
    )
    sys.exit(1)

DEFAULTS = {
    "MQTT_HOST": "100.127.43.75",      # homeassistant on the tailnet
    "MQTT_PORT": "1883",
    "MQTT_USER": "netsurvey",
    "MQTT_PASS": "039b3860703ff5408b75e088",
    "MQTT_TLS": "false",
    "NODE_NAME": "jack",
    "TOPIC_BASE": "raspyjack",          # -> raspyjack/<node>/scan/{set,state,avail}
    "RJ_SCAN": "/usr/local/bin/rj-scan",
}

CONF_PATH = os.environ.get("SCAN_AGENT_CONF", "/etc/raspyjack/scan_agent.conf")


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
    for key in cfg:
        if key in os.environ:
            cfg[key] = os.environ[key]
    return cfg


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run_rj_scan(rj_scan, action):
    """Run `rj-scan <action>`; return (ok, output)."""
    try:
        p = subprocess.run(
            [rj_scan, action],
            capture_output=True, text=True, timeout=120,
        )
        out = (p.stdout + p.stderr).strip()
        return p.returncode == 0, out
    except Exception as e:  # noqa: BLE001 — keep the daemon alive
        return False, repr(e)


def current_state(rj_scan):
    """Machine-readable 'on'/'off' from rj-scan; 'unknown' if it errors."""
    try:
        p = subprocess.run([rj_scan, "state"], capture_output=True,
                           text=True, timeout=20)
        s = p.stdout.strip().lower()
        return s if s in ("on", "off") else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def main():
    cfg = load_config()
    node = cfg["NODE_NAME"]
    base = f"{cfg['TOPIC_BASE']}/{node}/scan"
    t_set, t_state, t_avail = f"{base}/set", f"{base}/state", f"{base}/avail"
    rj_scan = cfg["RJ_SCAN"]

    client = mqtt.Client(client_id=f"rj-scan-agent-{node}", clean_session=True)
    if cfg["MQTT_USER"]:
        client.username_pw_set(cfg["MQTT_USER"], cfg["MQTT_PASS"])
    if cfg["MQTT_TLS"].lower() in ("1", "true", "yes"):
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
    client.will_set(t_avail, "offline", qos=1, retain=True)

    def publish_state(c):
        st = current_state(rj_scan)
        c.publish(t_state, st, qos=1, retain=True)
        log(f"state -> {st}")

    def on_connect(c, u, flags, rc):
        if rc != 0:
            log(f"MQTT connect failed rc={rc}")
            return
        c.publish(t_avail, "online", qos=1, retain=True)
        c.subscribe(t_set, qos=1)
        log(f"connected to MQTT {cfg['MQTT_HOST']}:{cfg['MQTT_PORT']}, "
            f"listening on {t_set}")
        publish_state(c)

    def on_message(c, u, msg):
        payload = msg.payload.decode(errors="replace").strip().lower()
        log(f"cmd: {msg.topic} = {payload!r}")
        if payload in ("on", "off"):
            ok, out = run_rj_scan(rj_scan, payload)
            log(f"rj-scan {payload} -> {'ok' if ok else 'FAILED'}: "
                f"{out.splitlines()[-1] if out else ''}")
            # give services a moment to settle, then report truth
            time.sleep(2)
            publish_state(c)
        elif payload in ("status", "state", "refresh"):
            publish_state(c)
        else:
            log(f"ignoring unknown command {payload!r}")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(cfg["MQTT_HOST"], int(cfg["MQTT_PORT"]), keepalive=30)
    log(f"rj-scan-agent up for node '{node}' (topics under {base})")
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
