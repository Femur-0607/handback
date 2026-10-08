"""Read-only native-window diagnostics for this test process, including hidden HWNDs."""
import os
import sys


def windows():
    if sys.platform != "win32":
        return {}
    import ctypes
    from ctypes import wintypes
    user = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowExW.restype = wintypes.HWND
    result = {}

    def record(hwnd):
        process = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(process))
        if process.value == os.getpid():
            name = ctypes.create_unicode_buffer(256)
            user.GetClassNameW(hwnd, name, len(name))
            result[hwnd] = name.value

    @callback_type
    def child(hwnd, _):
        record(hwnd)
        return True

    @callback_type
    def top(hwnd, _):
        record(hwnd)
        if hwnd in result:
            user.EnumChildWindows(hwnd, child, 0)
        return True

    user.EnumWindows(top, 0)
    after = None
    while True:
        after = user.FindWindowExW(-3, after, None, None)  # HWND_MESSAGE is not in EnumWindows.
        if not after:
            return result
        top(after, 0)


def difference(before, after):
    return {"added": {hex(hwnd): name for hwnd, name in after.items() if hwnd not in before},
            "removed": {hex(hwnd): name for hwnd, name in before.items() if hwnd not in after}}


def growth(before, after):
    """Count each resource's increase without offsetting it by another's release."""
    return tuple(max(0, current - baseline) for baseline, current in zip(before, after))
