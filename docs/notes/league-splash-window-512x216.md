# League splash window (512x216) — hide, do not present

Local observation from a Darling session running Riot Client 143 / Electron 43
on 2026-11-12. This is a runtime/X11 description, not a stable client API:
window IDs and PIDs change on every launch.

## What the window is

A borderless, override-redirect X11 window created by the League client. It
displays the League of Legends splash overlay and floats above the normal client
UI. It is **not** the main League window and should not be shown during normal
use.

## Observed properties

| Property | Value |
| --- | --- |
| X11 window ID | `0x2600004` (ephemeral; changes per launch) |
| `WM_CLASS` | `"LeagueClient", "LeagueClient"` |
| `WM_NAME` / `_NET_WM_NAME` | empty |
| Geometry | `512x216+704+432` |
| Size hints | minimum `512x216`, maximum `512x216` (fixed size) |
| `override_redirect` | `1` (not managed by the window manager) |
| `_NET_WM_WINDOW_TYPE` | `_NET_WM_WINDOW_TYPE_NORMAL` |
| `_MOTIF_WM_HINTS` | decorations disabled (borderless) |
| `_NET_WM_STATE` while mapped | `_NET_WM_STATE_SKIP_PAGER`, `_NET_WM_STATE_SKIP_TASKBAR` |
| `_NET_WM_ALLOWED_ACTIONS` | change desktop, above, below |
| `WM_PROTOCOLS` | `WM_DELETE_WINDOW` |
| `_NET_WM_PID` | guest PID (observed `870`; not stable) |
| `WM_STATE` after hiding | `Withdrawn` |

Because it is override-redirect, it does not appear in `_NET_CLIENT_LIST`,
cannot be moved/minimized through the window manager, and can stay on screen
without a taskbar entry.

## Do not confuse it with

- Main League window: `WM_CLASS = "LeagueClientUx"`, managed, usually
  `1024x576`. This is the window that should stay visible.
- Riot Client startup window: `WM_CLASS = "RiotClientServices"`, `392x488`,
  title `"Cliente de Riot"`.
- Main Riot Client window: `WM_CLASS = "Riot Client"`, `1536x864`, title
  `"Riot Client"`.

## Detect it

`_NET_CLIENT_LIST` only lists managed windows, so the splash is absent there.
Enumerate the X11 tree and match on the class, fixed size and
`override_redirect` flag, for example:

```sh
xwininfo -root -tree | grep -i LeagueClient
xprop -id <window-id> WM_CLASS _NET_WM_STATE WM_NORMAL_HINTS
```

`xwininfo` may not be installed. A small `XQueryTree`/`XGetWindowAttributes`
walker works as well.

## Hide and restore

Hiding is an unmap, not a kill; the client keeps running and the window can be
mapped again:

- Hide: `XUnmapWindow(display, window)` (for example with a small Xlib helper).
- Restore: `XMapWindow(display, window)`.

The opt-in launcher helper `tools/hide-league-splash-window.py` performs this
automatically. It polls every five seconds and matches only the class, the
fixed size and the override-redirect flag, so a randomly recreated splash is
hidden again:

```sh
RCLOL_HIDE_LEAGUE_SPLASH=1 ./scripts/run.sh
```

Do not match by window ID across launches. Match by:

```text
WM_CLASS == LeagueClient/LeagueClient
size hints min == max == 512x216
override_redirect == 1
```

## Main-window recovery

The managed League window (`LeagueClientUx`) may start on a secondary output,
for example at `+1920+89` on a `2944x1080` Xwayland screen whose primary output
is `1920x1080+0+0`. In that case it is mapped but not visible on the primary
monitor. Move and raise the managed window if needed; leave the 512x216
override-redirect splash unmapped.
