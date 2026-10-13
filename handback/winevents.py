"""Foreground-change watcher for the dashboard. Windows only, stdlib ctypes.

Clicking a taskbar button makes Explorer's taskbar the foreground window for a
moment, which lifts it over our topmost widget. Polling alone shows that as a
flicker, so a daemon thread owns out-of-context WinEvent hooks with its own
message loop and puts the widget back above the taskbar as soon as the
foreground changes. That thread never touches Tk: it uses native calls on the
window handles the Tk thread hands it, and sets ``changed`` so the Tk thread
can re-check full-screen coverage and the taskbar colour sooner.
Everywhere except Windows the class is a harmless no-op.
"""
import sys
import threading

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND = 0x0016, 0x0017
WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS = 0x0000, 0x0002
WM_QUIT = 0x0012
GW_HWNDPREV = 3
HWND_TOPMOST = -1
# NOMOVE | NOSIZE | NOACTIVATE | ASYNCWINDOWPOS: never wait on the Tk thread.
RAISE_FLAGS = 0x0001 | 0x0002 | 0x0010 | 0x4000


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
        if target and self._api is not None:
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
        # Private DLL handle: argtypes set here must not leak into the dashboard's own calls.
        user, kernel = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
        WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                          wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
        user.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WINEVENTPROC,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
        user.SetWinEventHook.restype = wintypes.HANDLE
        user.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user.GetMessageW.restype = ctypes.c_int
        user.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user.FindWindowW.restype = wintypes.HWND
        user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetWindow.restype = wintypes.HWND
        user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self._api = user

        def callback(hook, event, hwnd, object_id, child, thread, time):
            try:
                self._event()
            except Exception:  # A callback must never unwind into native code.
                pass

        proc = WINEVENTPROC(callback)  # keep a reference for the lifetime of the loop
        flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
        hooks = [user.SetWinEventHook(low, high, None, proc, 0, 0, flags)
                 for low, high in ((EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
                                   (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND))]
        try:
            if not all(hooks):
                return
            self._thread_id = kernel.GetCurrentThreadId()
            self.active = True
            self._ready.set()
            message = wintypes.MSG()
            while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                pass  # Out-of-context hooks are delivered while this thread waits here.
        finally:
            self.active = False
            for hook in hooks:
                if hook:
                    user.UnhookWinEvent(hook)


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
