"""Patch-content checks for the experimental native Wayland path of the League game window.

The guest side lives in patches/cocotron.patch (AppKit X11 backend: X11DisplayWayland.m, WaylandWindow.m,
WaylandSubWindow.m). These checks read the patch text, so they need neither GNU patch nor a Darling checkout."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = 'AppKit/X11.backend/'


def patch_sections():
    """Map each file in the cocotron patch to its added and context lines (hunks joined)."""
    sections = {}
    current = None
    for line in (ROOT / 'patches/cocotron.patch').read_text().splitlines():
        match = re.match(r'diff --git a/(\S+) b/\S+', line)
        if match:
            current = match.group(1)
            sections[current] = []
        elif current and line[:1] in ('+', ' ') and not line.startswith('+++'):
            sections[current].append(line[1:])
    return {path: '\n'.join(lines) for path, lines in sections.items()}


SECTIONS = patch_sections()


def added(name):
    return SECTIONS[BACKEND + name] if (BACKEND + name) in SECTIONS else SECTIONS[name]


def body(source, signature):
    return source.split(signature, 1)[1].split('\n}\n', 1)[0]


class WaylandGameBackend(unittest.TestCase):
    def test_new_sources_are_in_the_x11_backend(self):
        cmake = SECTIONS['AppKit/CMakeLists.txt']
        backend = cmake.split('X11.backend/X11KeySymToUCS.m', 1)[1] if 'X11.backend/X11KeySymToUCS.m' in cmake else cmake
        for source in ('X11DisplayWayland.m', 'WaylandWindow.m', 'WaylandSubWindow.m'):
            self.assertIn('X11.backend/' + source, cmake)
            self.assertIn(BACKEND + source, SECTIONS)
        self.assertIn('X11.backend/WaylandSubWindow.m', backend)

    def test_gate_needs_socket_and_the_game_process(self):
        gate = body(added('X11DisplayWayland.m'), '- (void) waylandConnectIfRequested {')
        self.assertIn('getenv("RCLOL_WAYLAND_SOCKET")', gate)
        self.assertIn('X11WaylandProcessKind()', gate)
        self.assertIn('X11WaylandProcessGame', added('X11DisplayWayland.m'))
        self.assertLess(gate.index('RCLOL_WAYLAND_SOCKET'), gate.index('dlopen'))
        for needle in ('getenv("DARLING_WAYLAND_LIBRARY")', 'RTLD_NOW | RTLD_LOCAL', '"DarlingWaylandGetAPI"',
                       'api->version < minimumVersion', 'api->connect(socket)', 'CGLRegisterNativeWaylandDisplay',
                       'registerDisplay(api->native_display())', 'weak_import'):
            self.assertIn(needle, added('X11DisplayWayland.m'))

    def test_any_failure_stays_on_x11(self):
        gate = body(added('X11DisplayWayland.m'), '- (void) waylandConnectIfRequested {')
        failure = gate.split('if (reason[0] != \'\\0\') {', 1)[1].split('return;', 1)[0]
        self.assertIn('WLLOG("staying on X11: %s", reason);', failure)
        self.assertIn('api->disconnect();', failure)
        self.assertIn('WLLOG("game windows use native Wayland (socket %s)", socket);', gate)
        # Success state is only set after the failure branch returned.
        self.assertGreater(gate.index('_wayland = api;'), gate.index('staying on X11'))

    def test_every_log_line_has_the_prefix(self):
        self.assertIn('"darling-wayland: "', added('WaylandBackend.h'))

    def test_default_path_is_gated_on_wayland_mode(self):
        display = added('X11Display.m')
        self.assertIn('BOOL X11IsLeagueGameProcess(void) {', display)
        self.assertIn('if (X11IsLeagueGameProcess()) return YES;', display)
        routing = body(display, '- (CGWindow *) newWindowWithDelegate: (NSWindow *) delegate {')
        self.assertIn('if (X11IsLeagueGameWindow(_display, None, delegate))', routing)
        self.assertIn('if (_wayland) {', routing)
        self.assertIn('return [[X11Window alloc] initWithDelegate: delegate];', routing)
        for guard in ('if (_wayland && [self waylandSetCursor: cursor])', 'if (_wayland && [self waylandWarpMouse: position])',
                      'if (_wayland && [self waylandHandleGrabMouse: doGrab])',
                      'if (_wayland && [self waylandMouseLocation: &waylandLocation])'):
            self.assertIn(guard, display)

    def test_app_id_matches_the_desktop_entry_class(self):
        window = added('WaylandWindow.m')
        self.assertIn('const char *appID = "LeagueofLegends";', window)
        self.assertIn('api->window_create(width, height, _title ? [_title UTF8String] : "", appID)', window)
        entry = (ROOT / 'tools/desktop-entry.py').read_text()
        self.assertIn('LeagueofLegends', entry)

    def test_focus_taking_paths_and_logging(self):
        window = added('WaylandWindow.m')
        focus = body(window, '- (void) acquireFocusExplicitly: (BOOL) explicitly {')
        env = focus.index('RCLOL_WAYLAND_ACTIVATION_TOKEN')
        bridge = focus.index('X11ActivationTime(display)')
        own = focus.index('api->request_token(')
        self.assertLess(env, bridge)
        self.assertLess(bridge, own)
        self.assertIn('unsetenv("RCLOL_WAYLAND_ACTIVATION_TOKEN")', focus)
        self.assertIn('if (activationTime != CurrentTime)', focus)
        self.assertIn('"rclol-%s-%d-%u_TIME%lu"', focus)
        self.assertIn('"game" : "client"', focus)
        self.assertIn('"League of Legends"', focus)
        self.assertIn('new: ID=\\"%s\\" NAME=\\"%s\\" SCREEN=%d TIMESTAMP=%lu', focus)
        self.assertIn('api->window_activate(_dwlID, token)', focus)
        for path in ('"env"', '"x11-startup"', '"own-serial"', '"none"'):
            self.assertIn('_activationPath = ' + path, focus)
        self.assertIn('_NET_STARTUP_INFO_BEGIN', window)
        self.assertIn('remove: ID=\\"%s\\"', window)
        self.assertIn('WLLOG("activate window=%ld path=%s result=%s"', window)
        for result in ('"focused"', '"not-focused"', '"pending"'):
            self.assertIn(result, window)

    def test_make_key_and_first_configure_take_focus(self):
        window = added('WaylandWindow.m')
        self.assertIn('[self acquireFocusExplicitly: YES];', body(window, '- (void) makeKey {'))
        configure = body(window, '- (void) handleConfigureWidth: (int) width height: (int) height flags: (uint32_t) flags {')
        self.assertIn('[self acquireFocusExplicitly: _focusExplicit];', configure)
        self.assertIn('DWL_CONFIGURE_ACTIVATED', configure)
        self.assertIn('platformWindow: self frameChanged: [self frame] didSize: YES', configure)

    def test_pump_waits_on_the_wayland_fd_and_survives_a_lost_connection(self):
        display = added('X11Display.m')
        self.assertIn('if (_occlusionDirty)\n        [self _updateOcclusion];', display)
        self.assertIn('if (_wayland)\n        [self waylandDispatchEvents];', display)
        self.assertIn('pfds[1].fd = [self waylandPollDescriptor];', display)
        self.assertIn('[self waylandPollTimeoutMillis]', display)
        wayland = added('X11DisplayWayland.m')
        dispatch = body(wayland, '- (void) waylandDispatchEvents {')
        self.assertIn('if (_wayland->dispatch() < 0)', dispatch)
        self.assertIn('[self waylandConnectionLost]', dispatch)
        lost = body(wayland, '- (void) waylandConnectionLost {')
        self.assertIn('if (_wlInput.dead)', lost)
        self.assertIn('[self waylandRequestCloseForWindow: window]', lost)

    def test_pointer_requests_use_the_native_extensions(self):
        wayland = added('X11DisplayWayland.m')
        for needle in ('api->pointer_lock(', 'api->pointer_confine(', 'api->pointer_warp(', 'DWL_EVENT_RELATIVE_MOTION',
                       'WaylandRateLimited(&_wlInput.warpLogStamp, 5000000000ULL)', 'WL_BTN_SIDE'):
            self.assertIn(needle, wayland)
        self.assertIn('api->cursor_set_image(', added('WaylandWindow.m'))
        self.assertIn('api->cursor_hide(', added('WaylandWindow.m'))
        self.assertIn('XcursorLibraryLoadImage(', added('X11Cursor.m'))

    def test_game_sub_view_shares_the_parent_surface(self):
        sub = added('WaylandSubWindow.m')
        self.assertIn('- (void *) sharedCGLWindow {', sub)
        self.assertIn('[_parent layerDrawableFor: self]', sub)
        self.assertIn('return [_parent eglWindow];', sub)
        window = added('WaylandWindow.m')
        self.assertIn('CGLGetWindow(egl)', window)
        self.assertIn('api->window_egl_window(_dwlID)', window)
        self.assertIn('api->window_present_pixels(', window)


if __name__ == '__main__':
    unittest.main()
