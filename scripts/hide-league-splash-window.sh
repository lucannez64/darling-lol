#!/bin/sh
# Hide the 512x216 League of Legends splash window every five seconds.
#
# Run it while the Riot/League client is open:
#
#   ./scripts/hide-league-splash-window.sh
#
# Press Ctrl+C to stop. The helper only unmaps windows whose WM_CLASS is
# LeagueClient/LeagueClient, whose size is exactly 512x216, and that are
# override-redirect. It never touches the game or the managed client windows.
#
# Diagnose one observation without changing any window:
#
#   ./scripts/hide-league-splash-window.sh --once
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export DPREFIX="${DPREFIX:-$HOME/.darling}"
export RCLOL_HIDE_LEAGUE_SPLASH=1
exec python3 -u "$repo/tools/hide-league-splash-window.py" "$@"
