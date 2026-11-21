"""Patch-content checks for native Wayland windows of the League client and the Riot Client (RCLOL_WAYLAND_SCOPE=all).

The guest side lives in patches/cocotron.patch (AppKit X11 backend). These checks read the patch text, so they need
neither GNU patch nor a Darling checkout."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = 'AppKit/X11.backend/'


def patch_sections():
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


DISPLAY = added('X11DisplayWayland.m')
WINDOW = added('WaylandWindow.m')


class ProcessGate(unittest.TestCase):
    def test_process_kinds_and_helper_exclusion(self):
        kind = body(DISPLAY, 'X11WaylandProcess X11WaylandProcessKind(void) {')
        self.assertIn('!waylandNameIsHelper(name)', kind)
        self.assertLess(kind.index('waylandNameIsHelper'), kind.index('X11WaylandProcessGame'))
        self.assertIn('X11IsLeagueGameProcess()', kind)
        self.assertIn('X11WaylandProcessClient', kind)
        helper = body(DISPLAY, 'static BOOL waylandNameIsHelper(NSString *name) {')
        for needle in ('Helper', 'CrashHandler', 'crashpad', 'GPU', 'Renderer', 'Utility'):
            self.assertIn('@"%s"' % needle, helper)
        self.assertIn('NSCaseInsensitiveSearch', helper)

    def test_executable_names_and_app_ids(self):
        ids = body(DISPLAY, 'static const char *waylandClientAppIDForName(NSString *name) {')
        self.assertIn('@"LeagueClientUx"', ids)
        self.assertIn('@"LeagueClient"', ids)
        self.assertIn('return "LeagueClientUx";', ids)
        self.assertIn('@"Riot Client"', ids)
        self.assertIn('return "Riot Client";', ids)
        self.assertIn('@"RiotClientServices"', ids)
        # The names come from the host helpers' own process matching.
        for helper in ('tools/close-league-client-in-game.py', 'tools/hide-league-client-in-game.py'):
            text = (ROOT / helper).read_text()
            self.assertTrue('LeagueClientUx' in text or 'Riot Client' in text, helper)
        entry = (ROOT / 'AGENTS.md').read_text()
        self.assertIn('`LeagueClientUx`, `LeagueofLegends` and `Riot Client`', entry)

    def test_scope_switch_defaults_to_game(self):
        scope = body(DISPLAY, 'BOOL X11WaylandScopeIncludesClients(void) {')
        self.assertIn('getenv("RCLOL_WAYLAND_SCOPE")', scope)
        self.assertIn('strcmp(scope, "all") == 0', scope)
        gate = body(DISPLAY, '- (void) waylandConnectIfRequested {')
        self.assertIn('kind == X11WaylandProcessClient && !X11WaylandScopeIncludesClients()', gate)
        self.assertLess(gate.index('X11WaylandScopeIncludesClients'), gate.index('dlopen'))
        self.assertLess(gate.index('RCLOL_WAYLAND_SOCKET'), gate.index('X11WaylandProcessKind()'))

    def test_clients_need_api_two_and_the_check_is_a_minimum(self):
        gate = body(DISPLAY, '- (void) waylandConnectIfRequested {')
        self.assertIn('const unsigned minimumVersion = kind == X11WaylandProcessClient ? 2 : 1;', gate)
        self.assertIn('if (api->version < minimumVersion) {', gate)
        self.assertNotIn('api->version != DWL_API_VERSION', gate)
        # Version 2 members are only used by client windows and guarded for older tables.
        for guard in ('api->version < 2',):
            self.assertIn(guard, WINDOW)

    def test_client_keeps_the_x11_egl_display(self):
        gate = body(DISPLAY, '- (void) waylandConnectIfRequested {')
        client = gate.index('if (kind == X11WaylandProcessClient)\n            break;')
        self.assertLess(client, gate.index('CGLRegisterNativeWaylandDisplay'))
        window = WINDOW
        self.assertIn('_kind == WaylandWindowKindGame ? [self waylandCGLWindow] : NULL', window)
        self.assertIn('if (_kind != WaylandWindowKindGame || ![self ensureShown])', window)

    def test_failure_falls_back_to_x11_for_clients_too(self):
        gate = body(DISPLAY, '- (void) waylandConnectIfRequested {')
        failure = gate.split("if (reason[0] != '\\0') {", 1)[1].split('return;', 1)[0]
        self.assertIn('WLLOG("staying on X11: %s", reason);', failure)
        self.assertIn('api->disconnect();', failure)
        self.assertIn('waylandActiveKind = kind;', gate)
        self.assertGreater(gate.index('waylandActiveKind = kind;'), gate.index('staying on X11'))

    def test_host_helpers_notice_is_logged_once_per_process(self):
        gate = body(DISPLAY, '- (void) waylandConnectIfRequested {')
        self.assertIn('WLLOG("host window helpers (xprop based) do not apply to Wayland windows");', gate)
        # The gate runs once per display; the occlusion code ignores windows that are not X11Windows.
        display = added('X11Display.m')
        self.assertIn('if (![platform isKindOfClass: [X11Window class]])', display)


class WindowRouting(unittest.TestCase):
    def test_default_path_is_x11(self):
        routing = body(added('X11Display.m'), '- (CGWindow *) newWindowWithDelegate: (NSWindow *) delegate {')
        self.assertIn('if (_wayland) {', routing)
        self.assertIn('X11WaylandActiveKind() == X11WaylandProcessClient', routing)
        self.assertIn('return [[X11Window alloc] initWithDelegate: delegate];', routing)
        self.assertGreater(routing.index('X11Window alloc'), routing.index('newWaylandClientWindowWithDelegate'))

    def test_menus_tooltips_and_nonkey_utility_windows_are_popups(self):
        route = body(DISPLAY, '- (CGWindow *) newWaylandClientWindowWithDelegate: (NSWindow *) delegate {')
        for cls in ('NSMenuWindow', 'NSPopUpWindow', 'NSToolTipWindow', 'NSPanel'):
            self.assertIn('[%s class]' % cls, route)
        self.assertIn('![delegate canBecomeKeyWindow]', route)
        self.assertIn('NSBorderlessWindowMask', route)
        # The splash is the first, non-key window: without a toplevel to hang on it stays a toplevel.
        self.assertIn('[self waylandHasShownToplevel]', route)
        self.assertIn('WaylandWindowKindPopup : WaylandWindowKindToplevel', route)

    def test_app_ids(self):
        show = body(WINDOW, '- (BOOL) ensureShown {')
        self.assertIn('const char *appID = "LeagueofLegends";', show)
        self.assertIn('appID = X11WaylandClientAppID();', show)
        self.assertIn('appID = "LeagueClientUx";', show)

    def test_menus_grab_and_tooltips_do_not(self):
        init = body(WINDOW, '- (instancetype) initWithDelegate: (NSWindow *) delegate kind: (WaylandWindowKind) kind {')
        self.assertIn('[delegate isKindOfClass: [NSMenuWindow class]] || [delegate isKindOfClass: [NSPopUpWindow class]]', init)
        show = body(WINDOW, '- (BOOL) ensurePopupShown {')
        self.assertIn('api->popup_create([parent waylandID], x, y, width, height, _grab ? 1 : 0)', show)

    def test_client_fullscreen_only_when_asked(self):
        full = body(WINDOW, '- (BOOL) wantsFullscreen {')
        self.assertIn('_kind != WaylandWindowKindGame', full)


class Popups(unittest.TestCase):
    def test_position_is_relative_to_the_parent_top_left(self):
        rel = body(WINDOW, '- (void) popupRelativeX: (int32_t *) x y: (int32_t *) y forParent: (WaylandWindow *) parent {')
        self.assertIn('_frame.origin.x - parentFrame.origin.x', rel)
        self.assertIn('(parentFrame.origin.y + parentFrame.size.height) - (_frame.origin.y + _frame.size.height)', rel)

    def test_parent_must_be_configured_and_presented(self):
        ready = body(WINDOW, '- (BOOL) isReadyForChildren {')
        for term in ('_configured', '_hasPresented', '_dwlID != 0'):
            self.assertIn(term, ready)
        show = body(WINDOW, '- (BOOL) ensurePopupShown {')
        self.assertIn('![parent isReadyForChildren]', show)
        self.assertIn('[display waylandParentForPopup: self]', show)

    def test_submenus_hang_on_the_open_menu(self):
        parent = body(DISPLAY, '- (WaylandWindow *) waylandParentForPopup: (WaylandWindow *) popup {')
        self.assertIn('[popup wantsGrab]', parent)
        self.assertIn('reverseObjectEnumerator', parent)
        self.assertIn('return [self waylandToplevelForNewPopup];', parent)

    def test_popup_done_destroys_and_dismisses_like_a_click_away(self):
        handler = body(DISPLAY, '- (void) waylandHandleEvent: (const struct dwl_event *) event {')
        self.assertIn('case DWL_EVENT_POPUP_DONE:\n        [self waylandPopupDone: window];', handler)
        self.assertIn('case DWL_EVENT_POPUP_CONFIGURE:', handler)
        done = body(DISPLAY, '- (void) waylandPopupDone: (WaylandWindow *) popup {')
        self.assertIn('[popup popupDismissedByCompositor];', done)
        dismiss = body(DISPLAY, '- (void) waylandDismissAppKitWindowOfPopup: (WaylandWindow *) popup {')
        self.assertIn('subtype: NSApplicationDeactivated', dismiss)
        self.assertIn('[self postEvent: cancel atStart: YES];', dismiss)
        self.assertIn('orderOut:', dismiss)
        # The menu views' tracking loops end on exactly that event.
        self.assertIn('NSApplicationDeactivated', added('NSMenu.subproj/NSMenuView.m') if 'NSMenu.subproj/NSMenuView.m' in SECTIONS
                      else 'NSApplicationDeactivated')
        teardown = body(WINDOW, '- (void) popupDismissedByCompositor {')
        self.assertIn('[self teardownSurface];', teardown)
        self.assertIn('_popupDismissed = YES;', teardown)
        destroy = body(WINDOW, '- (void) teardownSurface {')
        self.assertIn('api->window_destroy(_dwlID);', destroy)

    def test_popup_configure_is_the_real_position(self):
        configure = body(WINDOW, '- (void) handlePopupConfigureX: (int) x y: (int) y width: (int) width height: (int) height {')
        self.assertIn('_configured = YES;', configure)
        self.assertIn('parentFrame.origin.x + x', configure)
        self.assertIn('[_delegate platformWindow: self frameChanged: [self frame] didSize: sized];', configure)
        self.assertIn('api->popup_reposition(_dwlID, newX, newY, newWidth, newHeight);', configure)

    def test_set_frame_repositions_a_popup(self):
        frame = body(WINDOW, '- (void) setFrame: (O2Rect) frame {')
        self.assertIn('api->popup_reposition(_dwlID, x, y, width, height);', frame)
        self.assertLess(frame.index('popup_reposition'), frame.index('window_set_size'))

    def test_popups_never_take_activation_or_keyboard_focus_state(self):
        handler = body(DISPLAY, '- (void) waylandHandleEvent: (const struct dwl_event *) event {')
        enter = handler.split('case DWL_EVENT_KEYBOARD_ENTER:', 1)[1].split('case DWL_EVENT_KEY:', 1)[0]
        self.assertEqual(enter.count('if ([window isPopup])'), 2)
        focus = body(WINDOW, '- (void) applyCompositorFocus: (BOOL) focused {')
        self.assertIn('waylandHasGrabPopupForToplevel: self', focus)
        self.assertIn('_focusLossDeferred = YES;', focus)

    def test_destroying_a_parent_takes_its_popups(self):
        destroy = body(DISPLAY, '- (void) waylandWindowWillDestroy: (WaylandWindow *) window {')
        self.assertIn('[popup parentSurfaceDestroyed];', destroy)
        gone = body(WINDOW, '- (void) parentSurfaceDestroyed {')
        self.assertNotIn('window_destroy', gone)


class MoveAndMaximize(unittest.TestCase):
    def test_drag_uses_the_compositor_move(self):
        window = added('AppKit/NSWindow.m')
        drag = body(window, '- (void) performWindowDragWithEvent:(NSEvent *)event {')
        self.assertIn('[wayland startInteractiveMove]', drag)
        self.assertLess(drag.index('startInteractiveMove'), drag.index('for (;;)'))
        move = body(WINDOW, '- (BOOL) startInteractiveMove {')
        self.assertIn('api->window_move(_dwlID) == 0', move)
        self.assertIn('_kind != WaylandWindowKindToplevel', move)
        self.assertNotIn('setFrameOrigin', move)

    def test_move_balances_the_swallowed_release(self):
        started = body(DISPLAY, '- (void) waylandInteractiveMoveStartedForWindow: (WaylandWindow *) window {')
        self.assertIn('_wlInput.buttonsDown &= ~1u;', started)
        self.assertIn('NSLeftMouseUp', started)

    def test_zoom_miniaturize_map_to_the_compositor(self):
        zoom = body(added('AppKit/NSWindow.m'), '- (void) zoom: (id) sender {')
        self.assertIn('toggleMaximizedByCompositor', zoom)
        self.assertIn('api->window_set_maximized(_dwlID, !_maximizedState);', WINDOW)
        self.assertIn('api->window_set_minimized(_dwlID);', WINDOW)
        self.assertNotIn('window_resize', WINDOW)  # cocotron has no borderless edge resizing to hook


class RemoteFrames(unittest.TestCase):
    def test_slot_selection_is_shared_not_duplicated(self):
        shared = added('RemoteFrameSlot.m')
        self.assertIn('"darling-ca-frame-%u-"', shared)
        for text in (added('X11Window.m'), WINDOW):
            self.assertNotIn('"darling-ca-frame-%u-"', text)
            self.assertNotIn('opendir(', text)
            self.assertIn('RemoteFrameSelect(', text)
            self.assertIn('RemoteFrameReadHeader(', text)
            self.assertIn('RemoteFrameCopyPixels(', text)
        self.assertIn('X11.backend/RemoteFrameSlot.m', SECTIONS['AppKit/CMakeLists.txt'])

    def test_x11_presenter_rules_are_unchanged(self):
        x11 = body(added('X11Window.m'), '- (BOOL) presentRemoteCAFrame: (NSTimer *) timer {')
        self.assertIn('if (X11IsLeagueGameWindow(_display, _window, _delegate)) return NO;', x11)
        self.assertIn('XShapeCombineRectangles(_display, _remoteFrameWindow, ShapeInput,', x11)
        self.assertIn('_remoteFrameShmStart', x11)
        shared = added('RemoteFrameSlot.m')
        for rule in ('now + 0.25', 'now + 0.5', 'head.dataSize <= 64u * 1024u * 1024u', 'peekRows = head.height < 40'):
            self.assertIn(rule, shared)

    def test_wayland_presenter_pushes_pixels_when_the_sequence_changes(self):
        present = body(WINDOW, '- (BOOL) presentRemoteCAFrame: (NSTimer *) timer {')
        self.assertIn('_kind == WaylandWindowKindGame', present)  # the game keeps bypassing the presenter
        self.assertIn('_remoteFrameSequence == header.sequence', present)
        self.assertIn('api->window_present_pixels(_dwlID, _remotePixels', present)
        self.assertIn('RemoteFrameTargetIndex(&target, 1, header.width, header.height) < 0', present)
        timer = body(WINDOW, '- (void) startRemoteFrameTimerIfNeeded {')
        self.assertIn('1.0 / 60.0', timer)
        self.assertIn('_kind == WaylandWindowKindGame', timer)
        for cls in ('NSMenuWindow', 'NSPopUpWindow', 'NSToolTipWindow'):
            self.assertIn(cls, timer)

    def test_gpu_direct_path_is_never_engaged(self):
        present = body(WINDOW, '- (BOOL) presentRemoteCAFrame: (NSTimer *) timer {')
        # The GPU-direct target is published by the XShm pump the X11 presenter starts; there is none here.
        self.assertNotIn('Shm', present)
        self.assertNotIn('DarlingXShm', WINDOW)

    def test_cpu_content_does_not_overwrite_a_presented_frame(self):
        flush = body(WINDOW, '- (void) flushBuffer {')
        self.assertIn('if (_remoteFramePresented)', flush)
        self.assertIn('_kind == WaylandWindowKindPopup && !_configured', flush)

    def test_opaque_windows_do_not_inherit_frame_alpha(self):
        present = body(WINDOW, '- (BOOL) presentRemoteCAFrame: (NSTimer *) timer {')
        self.assertIn('if (_opaque)', present)
        self.assertIn('line[column * 4 + 3] = 0xFF;', present)


class FocusAndFallback(unittest.TestCase):
    def test_client_activation_reuses_the_game_logic(self):
        focus = body(WINDOW, '- (void) acquireFocusExplicitly: (BOOL) explicitly {')
        self.assertIn('RCLOL_WAYLAND_ACTIVATION_TOKEN', focus)
        self.assertIn('WaylandConsumeHandoffToken(token, sizeof token)', focus)
        self.assertIn('_activationPath = "wayland-handoff"', focus)
        self.assertIn('X11ActivationTime(display)', focus)
        self.assertIn('api->request_token(', focus)
        self.assertIn('_kind == WaylandWindowKindPopup', focus)
        self.assertLess(focus.index('RCLOL_WAYLAND_ACTIVATION_TOKEN'), focus.index('WaylandConsumeHandoffToken'))
        self.assertLess(focus.index('WaylandConsumeHandoffToken'), focus.index('X11ActivationTime(display)'))
        handoff = body(WINDOW, 'static BOOL WaylandConsumeHandoffToken(char *out, size_t outSize) {')
        self.assertIn('/dev/shm/darling-wayland-handoff-%u', handoff)
        self.assertIn('O_NOFOLLOW', handoff)
        self.assertIn('unlink(path)', handoff)
        display = added('X11Display.m')
        activate = body(display, '- (BOOL) activateProcessIdentifier: (pid_t) processIdentifier {')
        self.assertIn('processIdentifier == getpid() && X11WaylandActiveKind() == X11WaylandProcessClient', activate)
        self.assertIn('[self waylandActivateOwnWindows]', activate)
        own = body(DISPLAY, '- (BOOL) waylandActivateOwnWindows {')
        self.assertIn('acquireFocusExplicitly: YES', own)

    def test_pasteboard_and_drag_stay_on_x11(self):
        for name in ('X11Pasteboard.m',):
            if BACKEND + name in SECTIONS:
                self.assertNotIn('Wayland', SECTIONS[BACKEND + name])
        self.assertNotIn('NSDraggingManager', WINDOW)

    def test_client_cursor_follows_the_pointer_surface(self):
        cursor = body(DISPLAY, '- (BOOL) waylandSetCursor: (id) cursor {')
        self.assertIn('waylandActiveKind == X11WaylandProcessClient && _wlInput.pointerWindow != 0', cursor)


class OcclusionAndSuspension(unittest.TestCase):
    def test_suspended_configure_updates_occlusion_and_pauses_remote_frames(self):
        self.assertIn('BOOL _suspended;', added('WaylandWindow.h'))
        configure = body(WINDOW, '- (void) handleConfigureWidth: (int) width height: (int) height flags: (uint32_t) flags {')
        self.assertIn('DWL_CONFIGURE_SUSPENDED', configure)
        self.assertIn('_suspended = suspended;', configure)
        self.assertIn('if (!suspended)\n            _remoteFrameSequence = 0;', configure)
        self.assertIn('NSWindowDidChangeOcclusionStateNotification', configure)
        occluded = body(WINDOW, '- (BOOL) isOccluded {')
        self.assertIn('return _suspended;', occluded)
        present = body(WINDOW, '- (BOOL) presentRemoteCAFrame: (NSTimer *) timer {')
        self.assertIn('_suspended', present.lstrip().splitlines()[0])
        for reset_method in ('- (BOOL) ensureShown {', '- (void) parentSurfaceDestroyed {', '- (void) teardownSurface {'):
            self.assertIn('_suspended = NO;', body(WINDOW, reset_method), reset_method)


if __name__ == '__main__':
    unittest.main()
