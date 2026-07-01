#!/usr/bin/env python3
"""Continuous BLE presence watcher: fire commands when a target arrives/leaves.

Unlike WAIT_FOR_PRESENT (a one-shot gate), this stays running and edge-triggers
a shell command the moment a watched advertiser appears, and optionally another
when it disappears. Drive it with a JSON config of targets.

Author: m0usem0use
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from _bluez import watch_presence


def _make_runner(command: str, label: str):
    command = (command or "").strip()
    if not command:
        return None

    def _run(target: dict) -> None:
        env = os.environ.copy()
        env["RJ_LABEL"] = str(target.get("label", label))
        env["RJ_MAC"] = str(target.get("mac", ""))
        env["RJ_NAME"] = str(target.get("name", ""))
        try:
            subprocess.Popen(command, shell=True, cwd=os.getcwd(), env=env)
        except OSError as exc:  # pragma: no cover - defensive
            print(f"[presence] failed to launch action for {label}: {exc}", file=sys.stderr)

    return _run


def _load_targets(config: dict) -> list[dict]:
    raw_targets = config.get("targets") or []
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("config must contain a non-empty 'targets' list")
    targets: list[dict] = []
    for entry in raw_targets:
        label = str(entry.get("label", "")) or entry.get("mac") or entry.get("name") or "?"
        targets.append(
            {
                "label": label,
                "name": entry.get("name", ""),
                "mac": entry.get("mac", ""),
                "service_uuid": entry.get("service_uuid", ""),
                "ssid": entry.get("ssid", ""),
                "probe": entry.get("probe", ""),
                "on_arrive": _make_runner(entry.get("on_arrive", ""), label),
                "on_leave": _make_runner(entry.get("on_leave", ""), label),
            }
        )
    return targets


def _log_event(event: str, label: str, target: dict) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    arrow = "->" if event == "arrive" else "<-"
    print(f"[{stamp}] {arrow} {label} {event.upper()}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Continuously watch BLE presence and fire actions.")
    parser.add_argument("config", help="Path to a JSON config file (see presence_watch.example.json).")
    args = parser.parse_args()

    config_path = Path(args.config)
    try:
        config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read config {config_path}: {exc}", file=sys.stderr)
        return 2

    try:
        targets = _load_targets(config)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"[presence] watching {len(targets)} target(s); Ctrl-C to stop", flush=True)
    try:
        watch_presence(
            targets,
            scan_window_seconds=int(config.get("scan_window_seconds", 5)),
            poll_interval_seconds=int(config.get("poll_interval_seconds", 3)),
            leave_grace_scans=int(config.get("leave_grace_scans", 3)),
            arrive_cooldown_seconds=int(config.get("arrive_cooldown_seconds", 60)),
            wifi_options=config.get("wifi_options") or {},
            kismet_options=config.get("kismet") or {},
            on_event=_log_event,
        )
    except KeyboardInterrupt:
        print("\n[presence] stopped", flush=True)
        return 0
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
