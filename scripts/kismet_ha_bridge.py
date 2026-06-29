#!/usr/bin/env python3
"""RaspyJack -> Kismet alert -> Home Assistant bridge.

Polls Kismet's alert REST API and forwards each new alert to a Home Assistant
webhook (which fires an automation -> TTS on the M5Stack voice device).

Config file: /etc/raspyjack/kismet_ha.conf  (simple KEY=VALUE lines)
  KISMET_URL=http://127.0.0.1:2501
  KISMET_USER=admin
  KISMET_PASS=raspyjack
  HA_WEBHOOK_URL=            # e.g. http://homeassistant:8123/api/webhook/kismet_alert
  POLL_SECS=5
If HA_WEBHOOK_URL is blank, alerts are only logged (safe to run as groundwork).
"""
import base64
import json
import os
import sys
import time
import urllib.request

CONF = "/etc/raspyjack/kismet_ha.conf"


def load_conf():
    cfg = {
        "KISMET_URL": "http://127.0.0.1:2501",
        "KISMET_USER": "admin",
        "KISMET_PASS": "raspyjack",
        "HA_WEBHOOK_URL": "",
        "POLL_SECS": "5",
    }
    try:
        with open(CONF) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get(url, user, pw):
    req = urllib.request.Request(url)
    tok = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req.add_header("Authorization", f"Basic {tok}")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def post_ha(webhook, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(webhook, data=data,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status


def main():
    cfg = load_conf()
    ku, kp = cfg["KISMET_USER"], cfg["KISMET_PASS"]
    base = cfg["KISMET_URL"].rstrip("/")
    ha = cfg["HA_WEBHOOK_URL"].strip()
    poll = max(2, int(cfg.get("POLL_SECS", "5")))
    last_ts = time.time()
    log(f"bridge up. kismet={base} ha={'(set)' if ha else '(none - log only)'} poll={poll}s")
    while True:
        try:
            url = f"{base}/alerts/last-time/{last_ts}/alerts.json"
            alerts = http_get(url, ku, kp)
            for a in alerts:
                last_ts = max(last_ts, a.get("kismet.alert.timestamp", last_ts))
                header = a.get("kismet.alert.header", "ALERT")
                text = a.get("kismet.alert.text", "")
                sev = a.get("kismet.alert.severity", 0)
                log(f"ALERT {header}: {text}")
                if ha:
                    try:
                        post_ha(ha, {"source": "kismet", "type": header,
                                     "message": text, "severity": sev})
                    except Exception as e:
                        log(f"  HA post failed: {e}")
        except Exception as e:
            log(f"poll error: {e}")
        time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main())
