#!/usr/bin/env bash
# install_banner.sh -- idempotently install the RaspyJack login banner.
#
# Appends a guarded block to ~/.bashrc that runs scripts/raspyjack_banner.sh on
# interactive shells. Re-running is safe: the old block (between the markers) is
# removed first, so it is never duplicated.
#
# Usage:
#   scripts/install_banner.sh           # install / refresh
#   scripts/install_banner.sh --remove  # uninstall
#
# Target a different rc file with RJ_BASHRC=/path/to/rcfile (used by tests).
set -euo pipefail

BEGIN_MARK="# >>> RASPYJACK BANNER >>>"
END_MARK="# <<< RASPYJACK BANNER <<<"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
BANNER="$SCRIPT_DIR/raspyjack_banner.sh"
RC="${RJ_BASHRC:-$HOME/.bashrc}"

# Strip any existing block (idempotency). Portable across GNU/BSD sed via a temp.
_strip_block() {
    [ -f "$RC" ] || return 0
    awk -v b="$BEGIN_MARK" -v e="$END_MARK" '
        $0==b {skip=1}
        skip==0 {print}
        $0==e {skip=0}
    ' "$RC" > "$RC.rj.tmp" && mv "$RC.rj.tmp" "$RC"
    # Drop trailing blank lines so re-runs do not accumulate whitespace.
    if [ -s "$RC" ]; then
        awk 'NF{p=NR} {l[NR]=$0} END{for(i=1;i<=p;i++)print l[i]}' "$RC" > "$RC.rj.tmp" \
            && mv "$RC.rj.tmp" "$RC"
    fi
}

_strip_block

if [ "${1:-}" = "--remove" ]; then
    echo "RaspyJack banner removed from $RC"
    exit 0
fi

chmod +x "$BANNER" 2>/dev/null || true

# Trim a trailing blank line, then append the fresh block.
cat >> "$RC" <<EOF

$BEGIN_MARK
# Print the RaspyJack banner on interactive shells. Edit art in assets/banner.txt
case \$- in
    *i*) [ -r "$BANNER" ] && bash "$BANNER" ;;
esac
$END_MARK
EOF

echo "RaspyJack banner installed in $RC (runs $BANNER on interactive shells)"
