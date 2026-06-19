#!/usr/bin/env bash
# raspyjack_banner.sh -- print the RaspyJack login banner.
#
# Edit the ASCII art in:  <repo>/assets/banner.txt
# Colour defaults to green via tput; override with RJ_BANNER_COLOR (0-7).
#
# Printed on interactive shells via the guarded block in ~/.bashrc
# (see scripts/install_banner.sh).

# Resolve repo root from this script's location (works through symlinks/sourcing).
_self="${BASH_SOURCE[0]:-$0}"
_dir="$(cd "$(dirname "$_self")" >/dev/null 2>&1 && pwd)"
ART="${RJ_BANNER_ART:-$_dir/../assets/banner.txt}"

# Colour setup (only when stdout is a terminal that supports colour).
_grn=""; _dim=""; _rst=""
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
    _grn="$(tput setaf "${RJ_BANNER_COLOR:-2}")"
    _dim="$(tput setaf 8 2>/dev/null || tput dim 2>/dev/null)"
    _rst="$(tput sgr0)"
fi

# --- ASCII art ---------------------------------------------------------------
if [ -r "$ART" ]; then
    printf '%s' "$_grn"
    cat "$ART"
    printf '%s\n' "$_rst"
fi

# --- a couple of rig stats ---------------------------------------------------
_host="$(hostname 2>/dev/null || echo unknown)"
_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++)if($i=="src"){print $(i+1);exit}}')"
[ -z "$_ip" ] && _ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$_ip" ] && _ip="offline"
_iface="$(ip -4 route show default 2>/dev/null | awk '{print $5; exit}')"
[ -z "$_iface" ] && _iface="-"
_up="$(uptime -p 2>/dev/null | sed 's/^up //')"
[ -z "$_up" ] && _up="$(uptime 2>/dev/null)"
_temp="n/a"
if [ -r /sys/class/thermal/thermal_zone0/temp ]; then
    _t="$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)"
    [ -n "$_t" ] && _temp="$(( _t / 1000 ))°C"
fi

printf '%s  host:%s %s    %sip:%s %s (%s)\n' "$_dim" "$_rst" "$_host" "$_dim" "$_rst" "$_ip" "$_iface"
printf '%s  up:%s   %s    %stemp:%s %s%s\n\n' "$_dim" "$_rst" "$_up" "$_dim" "$_rst" "$_temp" "$_rst"
