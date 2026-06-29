#!/usr/bin/env python3
"""Publish RaspyJack live status (monitor mode, USB wifi dongle) to MQTT for HA.
Reuses the broker creds in /etc/raspyjack/ragnar_mqtt.conf (netsurvey @ HA Mosquitto)."""
import subprocess, time, sys
try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.stderr.write("paho-mqtt missing\n"); sys.exit(1)

def load_conf(path="/etc/raspyjack/ragnar_mqtt.conf"):
    cfg = {"MQTT_HOST": "100.127.43.75", "MQTT_PORT": "1883",
           "MQTT_USER": "netsurvey", "MQTT_PASS": ""}
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg

def monitor_active():
    try:
        out = subprocess.run(["iw", "dev"], capture_output=True, text=True, timeout=5).stdout
        return "type monitor" in out
    except Exception:
        return False

def usb_wifi_present():
    # external USB wlan adapters show as wlan1/wlan2... (wlan0 is built-in)
    try:
        out = subprocess.run(["iw", "dev"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Interface ") and line.split()[1] not in ("wlan0",) \
               and line.split()[1].startswith(("wlan", "mon")):
                return True
    except Exception:
        pass
    return False

def main():
    cfg = load_conf()
    base = "homeassistant/raspyjack"
    c = mqtt.Client(client_id="rj-status")
    if cfg["MQTT_USER"]:
        c.username_pw_set(cfg["MQTT_USER"], cfg["MQTT_PASS"])
    c.will_set(base + "/status", "offline", qos=1, retain=True)
    c.connect(cfg["MQTT_HOST"], int(cfg["MQTT_PORT"]), 30)
    c.loop_start()
    c.publish(base + "/status", "online", qos=1, retain=True)
    print("[rj-status] connected, publishing to %s/*" % base, flush=True)
    last = {}
    while True:
        state = {"monitor": "ON" if monitor_active() else "OFF",
                 "dongle":  "ON" if usb_wifi_present() else "OFF"}
        for k, v in state.items():
            if last.get(k) != v:
                c.publish(base + "/" + k, v, qos=1, retain=True)
                print("[rj-status] %s=%s" % (k, v), flush=True)
                last[k] = v
        time.sleep(5)

if __name__ == "__main__":
    main()
