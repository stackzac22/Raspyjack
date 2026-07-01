#!/usr/bin/env python3
"""Public extension API for RaspyJack payloads. Author: m0usem0use"""
from __future__ import annotations

try:
    from .actions import REQUIRE_CAPABILITY, RUN_PAYLOAD
    from .gates import WAIT_FOR_NOTPRESENT, WAIT_FOR_PRESENT
    from ._bluez import watch_presence as WATCH_PRESENCE
except ImportError:
    from actions import REQUIRE_CAPABILITY, RUN_PAYLOAD
    from gates import WAIT_FOR_NOTPRESENT, WAIT_FOR_PRESENT
    from _bluez import watch_presence as WATCH_PRESENCE

__all__ = [
    "WAIT_FOR_PRESENT",
    "WAIT_FOR_NOTPRESENT",
    "WATCH_PRESENCE",
    "REQUIRE_CAPABILITY",
    "RUN_PAYLOAD",
]
