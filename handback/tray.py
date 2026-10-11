"""Notification-area (system tray) icon for the dashboard. Windows only, stdlib ctypes.

The icon is owned by a hidden window that runs its own message loop on a daemon
thread. That thread never touches Tk: it only puts ``(kind, (x, y))`` tuples
("left", "right", "quit") into ``Tray.events``, which the dashboard polls from
the Tk thread. Everywhere except Windows the class is a harmless no-op.

The window is a hidden top-level popup rather than a message-only window on
purpose: Explorer announces a restart (``TaskbarCreated``) with a broadcast, and
broadcasts are not delivered to message-only windows.
"""
import queue
import sys
import threading
import time
from pathlib import Path

CALLBACK_MESSAGE = 0x8001  # WM_APP + 1
WM_DESTROY, WM_CLOSE, WM_NULL = 0x0002, 0x0010, 0x0000
WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x0202, 0x0205, 0x007B
NIN_SELECT, NIN_KEYSELECT = 0x0400, 0x0401
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x80  # SHOWTIP: required for tips under VERSION_4
NOTIFYICON_VERSION_4 = 4
TIP_LIMIT = 127
DEBOUNCE_SECONDS = 0.25
WINDOW_CLASS = "HandbackTrayWindow"
ICON_ID = 1


def clip_tip(text):
    """szTip holds 127 characters plus the terminator."""
    text = str(text)
    return text if len(text) <= TIP_LIMIT else text[:TIP_LIMIT - 1] + "…"


class Tray:
    def __init__(self, icon_path=None, tooltip="handback", clock=None):
        self.events = queue.Queue()
        self.icon_path = str(icon_path) if icon_path else None
        self.tooltip = clip_tip(tooltip)
        self.active = False
        self._clock = clock or time.monotonic
        self._last = {}
        self._lock = threading.Lock()
        self._thread = None
        self._ready = threading.Event()
        self._hwnd = None
        self._hicon = None
        self._taskbar_created = None
        self._failed = False
        self._api = None

    # ---- public API (Tk thread) -------------------------------------------------
    def start(self, timeout=3.0):
        """Create the icon. Returns False (and stays inert) when it cannot be shown."""
        if sys.platform != "win32" or self._thread is not None:
            return self.active
        self._thread = threading.Thread(target=self._run, name="handback-tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        self.active = bool(self._hwnd) and not self._failed
        return self.active

    def set_tooltip(self, text):
        """NIM_MODIFY only when the text really changed."""
        text = clip_tip(text)
        with self._lock:
            if text == self.tooltip:
                return False
            self.tooltip = text
            if not self.active:
                return False
        return self._notify(NIM_MODIFY)

    def stop(self):
        """Remove the icon and end the message loop; safe to call repeatedly."""
        if not self.active and self._thread is None:
            return
        self.active = False
        thread, self._thread = self._thread, None
        hwnd = self._hwnd
        if sys.platform == "win32" and hwnd and self._api is not None:
            try:
                self._api["user"].PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except (AttributeError, OSError):
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)

    # ---- message handling (tray thread; no Tk) ----------------------------------
    def _post(self, kind, point=None):
        self.events.put((kind, point))

    def _debounced(self, kind):
        now = self._clock()
        if now - self._last.get(kind, -1e9) < DEBOUNCE_SECONDS:
            return False
        self._last[kind] = now
        return True

    def _cursor(self):
        return None

    def _handle(self, message, wparam, lparam):
        """Pure message dispatch; returns True when the message was consumed."""
        if message == CALLBACK_MESSAGE:
            event = lparam & 0xFFFF  # NOTIFYICON_VERSION_4: LOWORD(lParam)
            if event in (WM_LBUTTONUP, NIN_SELECT, NIN_KEYSELECT):
                if self._debounced("left"):
                    self._post("left", self._cursor())
            elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                if self._debounced("right"):
                    self._post("right", self._cursor())
            return True
        if self._taskbar_created and message == self._taskbar_created:
            self._notify(NIM_ADD)  # Explorer restarted: icons must be re-added.
            return True
        if message == WM_DESTROY:
            self._notify(NIM_DELETE)
            self._post("quit")
            return True
        return False

    def _notify(self, action):  # replaced by the Win32 implementation in _run
        return False

    # ---- Win32 plumbing ----------------------------------------------------------
    def _run(self):
        try:
            self._loop()
        except (AttributeError, OSError, ValueError, TypeError):
            self._failed = True
        finally:
            self._ready.set()

    def _loop(self):
        import ctypes
        from ctypes import wintypes
        # Private DLL handles: argtypes set here must not leak into the dashboard's own calls.
        user, kernel, shell = (ctypes.WinDLL(name) for name in ("user32", "kernel32", "shell32"))
        self._api = {"user": user}
        LRESULT, WPARAM, LPARAM = ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
                        ("hIconSm", wintypes.HICON)]

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                        ("d", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                        ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                        ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]

        user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
        user.DefWindowProcW.restype = LRESULT
        user.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        user.RegisterClassExW.restype = wintypes.ATOM
        user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user.CreateWindowExW.restype = wintypes.HWND
        user.DestroyWindow.argtypes = [wintypes.HWND]
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM]
        user.PostMessageW.restype = wintypes.BOOL
        user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user.GetMessageW.restype = ctypes.c_int
        user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user.LoadImageW.restype = wintypes.HANDLE
        user.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user.LoadIconW.restype = wintypes.HICON
        user.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user.RegisterWindowMessageW.restype = wintypes.UINT
        user.PostQuitMessage.argtypes = [ctypes.c_int]
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        shell.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell.Shell_NotifyIconW.restype = wintypes.BOOL

        def cursor():
            point = wintypes.POINT()
            user.GetCursorPos(ctypes.byref(point))
            return (point.x, point.y)

        self._cursor = cursor
        self._hicon = self._load_icon(user)
        self._taskbar_created = user.RegisterWindowMessageW("TaskbarCreated")

        def data():
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = ICON_ID
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
            nid.uCallbackMessage = CALLBACK_MESSAGE
            nid.hIcon = self._hicon
            nid.szTip = self.tooltip
            nid.uVersion = NOTIFYICON_VERSION_4
            return nid

        def notify(action):
            if not self._hwnd:
                return False
            ok = bool(shell.Shell_NotifyIconW(action, ctypes.byref(data())))
            if ok and action == NIM_ADD:
                shell.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data()))
            return ok

        self._notify = notify

        def wndproc(hwnd, message, wparam, lparam):
            try:
                handled = self._handle(message, wparam, lparam)
            except Exception:  # A callback must never unwind into native code.
                handled = False
            if message == WM_DESTROY:
                user.PostQuitMessage(0)
                return 0
            if handled:
                return 0
            return user.DefWindowProcW(hwnd, message, wparam, lparam)

        proc = WNDPROC(wndproc)  # keep a reference for the lifetime of the loop
        instance = kernel.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = proc
        wc.hInstance = instance
        wc.lpszClassName = WINDOW_CLASS
        user.RegisterClassExW(ctypes.byref(wc))  # already-registered is fine
        # WS_POPUP, WS_EX_TOOLWINDOW: never visible, no taskbar button, but top-level
        # so it receives the TaskbarCreated broadcast.
        self._hwnd = user.CreateWindowExW(0x80, WINDOW_CLASS, "handback tray", 0x80000000,
                                          0, 0, 0, 0, None, None, instance, None)
        if not self._hwnd:
            self._failed = True
            return
        if not notify(NIM_ADD):
            self._failed = True
            user.DestroyWindow(self._hwnd)
            self._hwnd = None
            return
        self._ready.set()
        message = wintypes.MSG()
        while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user.TranslateMessage(ctypes.byref(message))
            user.DispatchMessageW(ctypes.byref(message))
        self._hwnd = None

    def _load_icon(self, user):
        import ctypes
        try:
            dpi_metric = getattr(user, "GetSystemMetricsForDpi", None)
            dpi = user.GetDpiForSystem() if hasattr(user, "GetDpiForSystem") else 96
            size = (dpi_metric(49, dpi) if dpi_metric else user.GetSystemMetrics(49)) or 16  # SM_CXSMICON
        except (AttributeError, OSError):
            size = 16
        if self.icon_path and Path(self.icon_path).is_file():
            # LR_LOADFROMFILE: the .ico holds several frames; Windows picks the closest to `size`.
            handle = user.LoadImageW(None, self.icon_path, 1, size, size, 0x10)
            if handle:
                return handle
        return user.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION
