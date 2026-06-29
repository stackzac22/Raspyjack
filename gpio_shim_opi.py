"""
gpio_shim_opi – Drop-in replacement for RPi.GPIO on the Orange Pi Zero 2W (Allwinner H618).

RaspyJack and its payloads do `import RPi.GPIO as GPIO` and address pins by
Raspberry-Pi BCM number. On the OPi Zero 2W the 40-pin header is driven by the
H618 pin controller exposed as gpiochip1 (`300b000.pinctrl`, 288 lines), so this
shim translates BCM pin numbers -> gpiochip1 line offsets and drives them through
libgpiod v2 (python3-libgpiod 2.x).

Install (like the CardputerZero gpio_shim): create /root/Raspyjack/RPi/GPIO.py
containing `from gpio_shim_opi import *` so it shadows the system RPi.GPIO.

Pin map authority: munts.com OrangePiZero2WPinout (the OPi header is labelled with
RPi-BCM-compatible GPIOxx names). Line offset = bank*32 + pin
(PA=0, PC=64, PD=96, PF=160, PG=192, PH=224, PI=256).
"""

import os

import gpiod
from gpiod.line import Direction, Value, Bias

# RPi.GPIO constants
BCM = 11
BOARD = 10
IN = 0
OUT = 1
PUD_OFF = 20
PUD_DOWN = 21
PUD_UP = 22
HIGH = 1
LOW = 0

_CHIP_PATH = os.environ.get("RJ_GPIOCHIP", "/dev/gpiochip1")

# BCM pin -> gpiochip1 line offset (Waveshare 1.44" HAT on OPi Zero 2W).
_BCM_TO_LINE = {
    27: 227,  # PH3  LCD RST
    25: 262,  # PI6  LCD DC
    24: 228,  # PH4  LCD backlight
    6:  271,  # PI15 Joy UP
    19: 258,  # PI2  Joy DOWN
    5:  256,  # PI0  Joy LEFT
    26: 272,  # PI16 Joy RIGHT
    13: 268,  # PI12 Joy PRESS
    21: 259,  # PI3  KEY1
    20: 260,  # PI4  KEY2
    16: 76,   # PC12 KEY3
}

# BCM8 (PH5) is the SPI1 hardware CS0 — owned by /dev/spidev1.0. Never grab it as
# a GPIO line; CS is handled in hardware by spidev. setup/output on it are no-ops.
_SPI_CS_PINS = {8}

# Active per-line requests: bcm_pin -> gpiod.LineRequest
_requests = {}
_warnings = True


def _iter_pins(pin):
    if isinstance(pin, (list, tuple)):
        return list(pin)
    return [pin]


def setmode(mode):
    pass  # always BCM here


def setwarnings(flag):
    global _warnings
    _warnings = bool(flag)


def _release(pin):
    req = _requests.pop(pin, None)
    if req is not None:
        try:
            req.release()
        except Exception:
            pass


def setup(pin, direction, pull_up_down=PUD_OFF, initial=None):
    for p in _iter_pins(pin):
        if p in _SPI_CS_PINS:
            continue  # SPI hardware CS, nothing to do
        line = _BCM_TO_LINE.get(p)
        if line is None:
            continue  # unmapped pin -> silently ignore (parity w/ stub behaviour)
        _release(p)  # re-configure cleanly if already set up
        try:
            if direction == OUT:
                ov = Value.ACTIVE if initial in (HIGH, 1, True) else Value.INACTIVE
                settings = gpiod.LineSettings(direction=Direction.OUTPUT, output_value=ov)
            else:
                if pull_up_down == PUD_UP:
                    bias = Bias.PULL_UP
                elif pull_up_down == PUD_DOWN:
                    bias = Bias.PULL_DOWN
                else:
                    bias = Bias.AS_IS
                settings = gpiod.LineSettings(direction=Direction.INPUT, bias=bias)
            _requests[p] = gpiod.request_lines(
                _CHIP_PATH, consumer="raspyjack", config={line: settings}
            )
        except Exception:
            # leave the pin unconfigured rather than crash the whole app
            pass


def output(pin, value):
    values = value if isinstance(value, (list, tuple)) else None
    pins = _iter_pins(pin)
    for i, p in enumerate(pins):
        if p in _SPI_CS_PINS:
            continue
        req = _requests.get(p)
        line = _BCM_TO_LINE.get(p)
        if req is None or line is None:
            continue
        v = values[i] if values is not None else value
        try:
            req.set_value(line, Value.ACTIVE if v in (HIGH, 1, True) else Value.INACTIVE)
        except Exception:
            pass


def input(pin):
    if pin in _SPI_CS_PINS:
        return 1
    req = _requests.get(pin)
    line = _BCM_TO_LINE.get(pin)
    if req is None or line is None:
        return 1  # unconfigured/unmapped -> read as "not pressed" (active-low convention)
    try:
        return 1 if req.get_value(line) == Value.ACTIVE else 0
    except Exception:
        return 1


def cleanup(pin=None):
    if pin is None:
        for p in list(_requests):
            _release(p)
    else:
        for p in _iter_pins(pin):
            _release(p)


# Edge-detection API used by a few payloads — not supported on this backend; provide
# no-op stubs so imports/calls don't crash (events simply never fire).
RISING = 31
FALLING = 32
BOTH = 33


def add_event_detect(*a, **k):
    pass


def remove_event_detect(*a, **k):
    pass


def event_detected(*a, **k):
    return False


def add_event_callback(*a, **k):
    pass


def wait_for_edge(*a, **k):
    return None
