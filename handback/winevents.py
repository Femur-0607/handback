"""Foreground-change watcher for the dashboard. Windows only, stdlib ctypes.

Clicking a taskbar button makes Explorer's taskbar the foreground window for a
moment, which lifts it over our topmost widget. Polling alone shows that as a
flicker, so a daemon thread owns out-of-context WinEvent hooks with its own
message loop and puts the widget back above the taskbar as soon as the
foreground changes. The same thread watches the foreground window's own moves
and resizes, so a window that turns full-screen (F11, a game, a video) covers
the widget at once instead of on the next Tk tick. That thread never touches
Tk: it uses native calls on the window handles the Tk thread hands it, and sets
``changed`` so the Tk thread can re-check full-screen coverage and the taskbar
colour sooner.
Everywhere except Windows the class is a harmless no-op.
"""
import os
import sys
import threading

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND = 0x0016, 0x0017
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
OBJID_WINDOW = 0
WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS = 0x0000, 0x0002
WM_QUIT = 0x0012
GW_HWNDPREV = 3
GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
HWND_TOPMOST, HWND_BOTTOM = -1, 1
# NOMOVE | NOSIZE | NOACTIVATE | ASYNCWINDOWPOS: never wait on the Tk thread.
RAISE_FLAGS = 0x0001 | 0x0002 | 0x0010 | 0x4000
SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})


def fullscreen_covers_widget(rect, monitor_rect, foreground_monitor, widget_monitor,
                             class_name="", own_window=False, notification_state=None):
    """Notification state is global: it cannot establish same-monitor coverage."""
    if (own_window or class_name in SHELL_CLASSES
            or not foreground_monitor or foreground_monitor != widget_monitor
            or not rect or not monitor_rect):
        return False
    left, top, right, bottom = monitor_rect
    return (right > left and bottom > top and rect[0] <= left and rect[1] <= top
            and rect[2] >= right and rect[3] >= bottom)


_NATIVE = None


def native_user32():
    """A private user32 handle with every signature this module calls.

    Private so these argtypes never leak into the dashboard's own calls; shared
    between the Tk thread and the hook thread because it is configured once.
    """
    global _NATIVE
    if _NATIVE is None:
        import ctypes
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        user = ctypes.WinDLL("user32")
        user.MonitorInfo = MonitorInfo
        user.GetForegroundWindow.restype = wintypes.HWND
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.IsIconic.argtypes = [wintypes.HWND]
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user.MonitorFromWindow.restype = wintypes.HANDLE
        user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user.MonitorFromPoint.restype = wintypes.HANDLE
        user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user.FindWindowW.restype = wintypes.HWND
        user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetWindow.restype = wintypes.HWND
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        _NATIVE = user
    return _NATIVE


def foreground_covers(user, hwnd, point=None):
    """True when another visible window fills the monitor that holds ``hwnd``.

    ``point`` replaces the window's own monitor while the widget is about to move.
    """
    import ctypes
    from ctypes import wintypes
    try:
        foreground = user.GetForegroundWindow()
        if (not foreground or not hwnd or not user.IsWindowVisible(foreground)
                or user.IsIconic(foreground)):
            return False
        monitor = user.MonitorFromWindow(foreground, MONITOR_DEFAULTTONEAREST)
        own_monitor = (user.MonitorFromPoint(wintypes.POINT(*point), MONITOR_DEFAULTTONEAREST)
                       if point is not None else user.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST))
        if monitor != own_monitor:
            return False
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
        name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(foreground, name, len(name))
        rect, info = wintypes.RECT(), user.MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if (not user.GetWindowRect(foreground, ctypes.byref(rect))
                or not user.GetMonitorInfoW(monitor, ctypes.byref(info))):
            return False
        bounds = lambda r: (r.left, r.top, r.right, r.bottom)
        return fullscreen_covers_widget(bounds(rect), bounds(info.rcMonitor), monitor, own_monitor,
                                        name.value, pid.value == os.getpid())
    except (AttributeError, OSError, TypeError):
        return False


class ForegroundWatcher:
    def __init__(self):
        self.changed = threading.Event()
        self.target = None  # Tk's native wrapper (GA_ROOT) handle
        self.taskbar = None
        self.active = False
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self._api = None
        self._follow = None  # hooks the current foreground process's window moves

    # ---- public API (Tk thread) -------------------------------------------------
    def start(self, timeout=3.0):
        if sys.platform != "win32" or self._thread is not None:
            return self.active
        self._thread = threading.Thread(target=self._run, name="handback-foreground", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.active

    def stop(self):
        """Unhook and end the message loop; safe to call repeatedly."""
        thread, self._thread = self._thread, None
        self.active = False
        if thread is None:
            return
        if self._thread_id and self._api is not None:
            try:
                self._api.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except (AttributeError, OSError):
                pass
        if thread is not threading.current_thread():
            thread.join(2.0)

    # ---- hook thread (no Tk) ------------------------------------------------------
    def _event(self):
        self.changed.set()
        target, taskbar = self.target, self.taskbar
        if not target or self._api is None:
            return
        if foreground_covers(self._api, target):
            # Sink below everything: the full-screen window covers the widget in
            # this same frame. The Tk thread withdraws it on its next tick and
            # restores topmost when the cover ends.
            if self._api.IsWindowVisible(target):
                self._api.SetWindowPos(target, HWND_BOTTOM, 0, 0, 0, 0, RAISE_FLAGS)
        else:
            raise_above_taskbar(self._api, target, taskbar)

    def _run(self):
        try:
            self._loop()
        except (AttributeError, OSError, ValueError, TypeError):
            self.active = False
        finally:
            self._ready.set()

    def _loop(self):
        import ctypes
        from ctypes import wintypes
        user, kernel = native_user32(), ctypes.WinDLL("kernel32")
        WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                          wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
        user.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
        user.SetWinEventHook.restype = wintypes.HANDLE
        user.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user.GetMessageW.restype = ctypes.c_int
        user.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        self._api = user
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS

        def follow_foreground():
            # LOCATIONCHANGE is noisy system-wide (every cursor move raises one),
            # so only the foreground window's process is hooked, re-armed here.
            if self._follow:
                user.UnhookWinEvent(self._follow)
                self._follow = None
            pid = wintypes.DWORD()
            foreground = user.GetForegroundWindow()
            if foreground and user.GetWindowThreadProcessId(foreground, ctypes.byref(pid)) \
                    and pid.value and pid.value != os.getpid():
                self._follow = user.SetWinEventHook(
                    EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE, None, proc,
                    pid.value, 0, flags) or None

        def callback(hook, event, hwnd, object_id, child, thread, time):
            try:
                if event == EVENT_OBJECT_LOCATIONCHANGE:
                    if (object_id != OBJID_WINDOW or child
                            or hwnd != user.GetForegroundWindow()):
                        return
                elif event == EVENT_SYSTEM_FOREGROUND:
                    follow_foreground()
                self._event()
            except Exception:  # A callback must never unwind into native code.
                pass

        proc = WINEVENTPROC(callback)  # keep a reference for the lifetime of the loop
        hooks = [user.SetWinEventHook(low, high, None, proc, 0, 0, flags)
                 for low, high in ((EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
                                   (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND))]
        try:
            if not all(hooks):
                return
            follow_foreground()
            self._thread_id = kernel.GetCurrentThreadId()
            self.active = True
            self._ready.set()
            message = wintypes.MSG()
            while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                pass  # Out-of-context hooks are delivered while this thread waits here.
        finally:
            self.active = False
            for hook in hooks + [self._follow]:
                if hook:
                    user.UnhookWinEvent(hook)
            self._follow = None


def raise_above_taskbar(user, hwnd, taskbar=None):
    """Native-only twin of dashboard.keep_above_taskbar for the hook thread."""
    if not user.IsWindowVisible(hwnd):
        return False
    taskbar = taskbar or user.FindWindowW("Shell_TrayWnd", None)
    if not taskbar:
        return False
    above = user.GetWindow(hwnd, GW_HWNDPREV)
    # Bound traversal in case Explorer changes the order while it is being read.
    for _ in range(256):
        if not above:
            return False
        if above == taskbar:
            user.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, RAISE_FLAGS)
            return True
        above = user.GetWindow(above, GW_HWNDPREV)
    return False
