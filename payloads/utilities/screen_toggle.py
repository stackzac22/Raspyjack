#!/usr/bin/env python3
"""Switch the 3.5-inch Pi screen between the existing Ragnar and RaspyJack WebUIs.
Runs once and returns to the menu. Both apps keep running.
"""
import subprocess

if __name__ == '__main__':
    raise SystemExit(subprocess.call(['/usr/local/sbin/screen-switch', 'toggle']))
