"""Compact Windows taskbar status widget with an optional expanded panel.

It never routes or changes agent combinations. Display preferences live in
dashboard.json; project actions acknowledge results, move state aside, or save
explicitly selected model and reasoning defaults.
"""

from datetime import datetime, timezone
import json
import math
from importlib import resources
import re
import webbrowser
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from . import config, inbox
from .collector import OPEN_STATES
from .state import atomic_json, home_lock, state_home


AGENT_NAME = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity"}
REFRESH_MS = 3000
TASKBAR_ZORDER_MS = 750
DETAIL_LIMIT = 5
DEFAULT_PREFS = {"max_rows": 3, "order": [], "hidden": [], "mode": "taskbar", "compact_x": None}


def _read(path):
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def agent_name(handle):
    agent = str(handle).partition(":")[0]
    return AGENT_NAME.get(agent, agent or "?")


def execution_targets(data):
    """Role defaults are separate even when an agent appears in both roles."""
    targets = []
    lead = data.get("lead", "").partition(":")[0]
    if lead:
        targets.append({"agent": lead, "role": "lead", "label": "리드 · " + agent_name(lead),
                        "enabled": lead in ("codex", "antigravity")})
    for worker in data.get("workers", []):
        targets.append({"agent": worker, "role": "worker", "label": "워커 · " + agent_name(worker),
                        "enabled": worker in ("codex", "antigravity")})
    return targets


# Only schemes and IDs verified for relay-native conversation handles belong here.
_THREAD_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
UNSUPPORTED_CONVERSATION = "이 앱은 대화 바로 열기를 지원하지 않습니다"


def conversation_link(handle):
    if not isinstance(handle, str):
        return None
    agent, separator, identifier = handle.partition(":")
    if agent == "codex" and separator and _THREAD_ID.fullmatch(identifier):
        return "codex://threads/" + identifier.lower()
    return None


def conversation_targets(data, kind):
    """Preserve worker identity separately from human-readable result labels."""
    targets = {}
    field = "handle" if kind == "running" else "sender"
    for item in data[kind]:
        handle = item.get(field)
        if not isinstance(handle, str) or not handle:
            continue
        created = item.get("created_utc") or ""
        if handle not in targets or created > targets[handle]["created_utc"]:
            targets[handle] = {"handle": handle, "name": item.get("name") or item.get("from") or handle,
                               "created_utc": created}
    return list(targets.values())


def open_conversation_url(url):
    # Validate again at the OS boundary; never interpolate an envelope into a command.
    prefix = "codex://threads/"
    if not isinstance(url, str) or not url.startswith(prefix) or conversation_link(
            "codex:" + url[len(prefix):]) != url:
        raise ValueError("지원하지 않는 대화 링크입니다")
    if sys.platform == "win32":
        os.startfile(url)
    elif not webbrowser.open(url):
        raise OSError("대화 링크를 열지 못했습니다")


def _is_scratch(root):
    """Probe projects created under the temp directory are not real work."""
    try:
        return Path(root).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, ValueError):
        return False


def _latest_mtime(folder):
    latest = 0.0
    for pattern in ("topology.json", "requests/*.json", "inbox/*.json", "inbox/acks/*.json"):
        for path in folder.glob(pattern):
            try:
                latest = max(latest, path.stat().st_mtime)
            except OSError:
                pass
    return latest


def project_row(folder):
    topology = _read(folder / "topology.json")
    root = topology.get("root")
    if not root or not Path(root).is_dir() or _is_scratch(root):
        return None
    threads = _read(folder / "threads.json")
    running = []
    for path in sorted((folder / "requests").glob("*.json")):
        request = _read(path)
        if request.get("status") in OPEN_STATES:
            thread = threads.get(request.get("handle"), {})
            running.append({"id": request.get("id", path.stem), "agent": request.get("agent"),
                            "name": thread.get("name") or request.get("handle", ""),
                            "handle": request.get("handle", ""),
                            "status": request.get("status"), "created_utc": request.get("created_utc")})
    unread = []
    mail_dir = folder / "inbox"
    if mail_dir.is_dir():
        for mail in inbox.pending(mail_dir):
            if mail.get("kind") == "request":
                continue  # Outgoing brief copies are not results waiting for the Lead.
            name = threads.get(mail.get("sender"), {}).get("name") or mail.get("sender", "")
            unread.append({"id": mail["id"], "kind": mail.get("kind"), "from": name, "sender": mail.get("sender", ""),
                           "created_utc": mail.get("created_utc"), "body": mail.get("body", "")})
        unread.sort(key=lambda item: item["created_utc"] or "", reverse=True)
    lead = topology.get("lead", "")
    workers = topology.get("workers") or []
    return {"name": Path(root).name, "root": root, "lead": lead, "workers": workers,
            "combo": agent_name(lead) + " → " + " + ".join(agent_name(w) for w in workers),
            "running": running, "unread": unread, "last_activity": _latest_mtime(folder)}


def snapshot(home=None):
    """Every project with relay state, most recently active first."""
    projects = Path(home or state_home()) / "projects"
    rows = []
    if projects.is_dir():
        for folder in projects.iterdir():
            if folder.is_dir():
                row = project_row(folder)
                if row:
                    rows.append(row)
    return sorted(rows, key=lambda row: row["last_activity"], reverse=True)


def acknowledge_all(root, home=None):
    """ACK every pending result/error of one project, as `inbox ack` would."""
    home = Path(home or state_home())
    with home_lock(home):
        folder = _folder_for(root, home)
        done = 0
        for mail in inbox.pending(folder / "inbox"):
            if mail.get("kind") != "request":
                inbox.acknowledge(folder / "inbox", mail["id"])
                done += 1
        return done


def release(root, home=None):
    """Unregister a project by moving its state aside; never deletes history.

    Refuses while a request is still open. Using relay there again starts a
    fresh registration.
    """
    home = Path(home or state_home())
    with home_lock(home):
        folder = _folder_for(root, home)
        row = project_row(folder) or {"running": []}
        if row["running"]:
            raise ValueError(f"진행 중인 요청 {len(row['running'])}개가 있어 해제할 수 없습니다")
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        target = home / "released" / f"{folder.name}-{stamp}"
        target.parent.mkdir(parents=True, exist_ok=True)
        folder.rename(target)
    prefs = load_prefs(home)
    save_prefs({**prefs, "order": [r for r in prefs["order"] if r != root],
                "hidden": [r for r in prefs["hidden"] if r != root]}, home)
    return target


def _folder_for(root, home):
    for folder in (home / "projects").iterdir():
        if folder.is_dir() and _read(folder / "topology.json").get("root") == root:
            return folder
    raise ValueError(f"handback 상태에 없는 프로젝트입니다: {root}")


def prefs_path(home=None):
    return Path(home or state_home()) / "dashboard.json"


def load_prefs(home=None):
    prefs = dict(DEFAULT_PREFS)
    stored = _read(prefs_path(home))
    if isinstance(stored.get("max_rows"), int) and stored["max_rows"] >= 0:
        prefs["max_rows"] = stored["max_rows"]
    for key in ("order", "hidden"):
        if isinstance(stored.get(key), list):
            prefs[key] = [str(root) for root in stored[key]]
    if stored.get("mode") in ("taskbar", "panel"):
        prefs["mode"] = stored["mode"]
    if type(stored.get("compact_x")) is int:
        prefs["compact_x"] = stored["compact_x"]
    return prefs


def save_prefs(prefs, home=None):
    atomic_json(prefs_path(home), prefs)


def arrange(rows, prefs):
    """Pinned order first, then the rest by recent activity; hidden rows dropped.

    Returns (shown, overflow); max_rows 0 means no limit.
    """
    rank = {root: index for index, root in enumerate(prefs["order"])}
    visible = [row for row in rows if row["root"] not in prefs["hidden"]]
    visible.sort(key=lambda row: (rank.get(row["root"], len(rank)), -row["last_activity"]))
    limit = prefs["max_rows"] or len(visible)
    return visible[:limit], visible[limit:]


def column_widths(measure):
    """Pixel budgets shared by every row, independent of live counts."""
    columns = (("프로젝트 이름 프로젝트", 26), ("Claude → Codex + Antigravity", 16),
               ("● 진행 999", 14), ("✉ 999", 14))
    return tuple(measure(text) + padding for text, padding in columns) + (
        max(measure(text) for text in ("59분 전", "23시간 전", "999일 전")) + 4,)


def truncate_text(text, width, measure):
    text = " ".join(str(text).split())
    if measure(text) <= width:
        return text
    if measure("…") > width:
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(text[:middle] + "…") <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low] + "…"


def display_rows(rows, prefs, show_all=False):
    shown, overflow = arrange(rows, prefs)
    return (shown + overflow, []) if show_all else (shown, overflow)


class DarkTooltip:
    """One window and one cancellable timer owned by the strip."""
    def __init__(self, strip):
        self.strip = strip
        self.pending = self.window = None

    def hide(self, event=None):
        if self.pending is not None:
            self.strip.root.after_cancel(self.pending)
            self.pending = None
        if self.window is not None:
            self.window.destroy()
            self.window = None

    def schedule(self, widget, text):
        self.hide()
        self.pending = self.strip.root.after(500, lambda: self.show(widget, text))

    def show(self, widget, text):
        self.hide()
        if not widget.winfo_exists():
            return
        strip = self.strip
        window = self.window = strip.tk.Toplevel(strip.root)
        window.withdraw()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        window.configure(bg=DarkMenu.HOVER)
        strip.tk.Label(window, text=text, bg=DarkMenu.BG, fg=strip.FG,
                       font=strip.font, wraplength=520, justify="left",
                       padx=8, pady=5).pack(padx=1, pady=1)
        window.update_idletasks()
        x, y = widget.winfo_rootx(), widget.winfo_rooty()
        x, y, width, height = popup_position(x, y - 6, window.winfo_reqwidth(),
                                           window.winfo_reqheight(), _monitor_area(widget, x, y))
        # Prefer above the strip, keeping the hovered target unobscured.
        y = max(_monitor_area(widget, x, y)[1], widget.winfo_rooty() - height - 6)
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.deiconify()

    def bind(self, widget, text):
        widget.bind("<Enter>", lambda event: self.schedule(widget, text), add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")


def move(prefs, roots, root, step):
    """Pin the currently displayed order, then shift one project by step."""
    order = list(roots)
    if root not in order:
        return prefs
    index = order.index(root)
    order.insert(max(0, min(len(order) - 1, index + step)), order.pop(index))
    return {**prefs, "order": order}


def _local_time(created_utc):
    try:
        return datetime.fromisoformat(created_utc).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return "?"


def _preview(body, width=48):
    line = " ".join(str(body).split())
    return line if len(line) <= width else line[:width - 1] + "…"


def ago(timestamp, now=None):
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
            or timestamp < 946684800 or timestamp > now + 86400 or not math.isfinite(timestamp)):
        return "-"
    seconds = max(0, now - timestamp)
    if seconds < 60:
        return "방금"
    if seconds < 3600:
        return f"{int(seconds // 60)}분 전"
    if seconds < 86400:
        return f"{int(seconds // 3600)}시간 전"
    return f"{int(seconds // 86400)}일 전"


def _work_area():
    """Desktop rectangle excluding the taskbar, in physical pixels."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.right, rect.bottom
    return None


def taskbar_info():
    """Primary shell taskbar bounds and notification area, in this process's DPI space."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class AppBarData(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                    ("uCallbackMessage", wintypes.UINT), ("uEdge", wintypes.UINT),
                    ("rc", wintypes.RECT), ("lParam", ctypes.c_ssize_t)]

    try:
        shell, user = ctypes.windll.shell32, ctypes.windll.user32
        shell.SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(AppBarData)]
        shell.SHAppBarMessage.restype = ctypes.c_size_t
        data = AppBarData()
        data.cbSize = ctypes.sizeof(data)
        if not shell.SHAppBarMessage(5, ctypes.byref(data)):  # ABM_GETTASKBARPOS
            return None
        auto_hide = bool(shell.SHAppBarMessage(4, ctypes.byref(data)) & 1)
        user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user.FindWindowW.restype = wintypes.HWND
        user.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
        user.FindWindowExW.restype = wintypes.HWND
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        tray = user.FindWindowExW(user.FindWindowW("Shell_TrayWnd", None), None, "TrayNotifyWnd", None)
        rect = wintypes.RECT()
        notification = rect.left if tray and user.GetWindowRect(tray, ctypes.byref(rect)) else None
        r = data.rc
        return {"rect": (r.left, r.top, r.right, r.bottom), "edge": data.uEdge,
                "auto_hide": auto_hide, "notification_left": notification}
    except (AttributeError, OSError):
        return None


def keep_above_taskbar(widget):
    """Repair Explorer's topmost ordering without activating or repainting Tk."""
    if sys.platform != "win32" or not widget.winfo_ismapped():
        return
    import ctypes
    from ctypes import wintypes
    user = ctypes.windll.user32
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user.FindWindowW.restype = wintypes.HWND
    user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetWindow.restype = wintypes.HWND
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.SetWindowPos.restype = wintypes.BOOL
    hwnd = user.GetAncestor(widget.winfo_id(), 2)  # GA_ROOT: Tk's native wrapper.
    taskbar = user.FindWindowW("Shell_TrayWnd", None)  # Re-find after Explorer restarts.
    if not hwnd or not taskbar:
        return
    above = user.GetWindow(hwnd, 3)  # GW_HWNDPREV
    # Bound traversal in case Explorer changes the order while it is being read.
    for _ in range(256):
        if not above:
            break
        if above == taskbar:
            # HWND_TOPMOST; NOMOVE | NOSIZE | NOACTIVATE. No hide/show or redraw.
            user.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            break
        above = user.GetWindow(above, 3)


def compact_geometry(info, area, width, height, x=None):
    """Use a bottom taskbar, otherwise put the widget just above the work-area bottom."""
    left, top, right, bottom = area
    if (info and info["edge"] == 3 and not info["auto_hide"]
            and info["rect"][2] > info["rect"][0] and info["rect"][3] > info["rect"][1]):
        left, y, right, bottom = info["rect"]
        height = bottom - y
        default_right = info.get("notification_left")
        if default_right is None or not left < default_right <= right:
            default_right = right
    else:
        height = min(height, bottom - top)
        y, default_right = bottom - height - 4, right - 8
        y = max(top, y)
    width = min(width, right - left)
    x = default_right - width if x is None else x
    return max(left, min(x, right - width)), y, width, height


def compact_lines(rows, prefs, name_width, measure):
    shown, _ = arrange(rows, {**prefs, "max_rows": 0})
    lines = []
    for i, row in enumerate(shown[:2]):
        lines.append({"row": row, "name": truncate_text(row["name"], name_width, measure),
                      "running": f"● {len(row['running'])}" if row["running"] else "대기",
                      "unread": f"✉ {len(row['unread'])}" if row["unread"] else "",
                      "more": f"+{len(shown) - 2}" if i == 1 and len(shown) > 2 else ""})
    return lines


def _window_dpi(widget):
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        try:
            user = ctypes.windll.user32
            user.GetParent.argtypes = [wintypes.HWND]
            user.GetParent.restype = wintypes.HWND
            user.GetDpiForWindow.argtypes = [wintypes.HWND]
            user.GetDpiForWindow.restype = wintypes.UINT
            dpi = user.GetDpiForWindow(user.GetParent(widget.winfo_id()))
            if dpi:
                return dpi
        except (AttributeError, OSError):
            pass
    return widget.winfo_fpixels("1i")


def _luminance(rgb):
    channels = [value / 255 for value in rgb]
    channels = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
                for value in channels]
    return sum(value * weight for value, weight in zip(channels, (.2126, .7152, .0722)))


def compact_palette(samples=()):
    """Dominant sampled shade, rejecting isolated icon pixels; fixed dark fallback."""
    groups = {}
    for rgb in samples:
        if (isinstance(rgb, (tuple, list)) and len(rgb) == 3
                and all(type(value) is int and 0 <= value <= 255 for value in rgb)):
            groups.setdefault(tuple(value // 8 for value in rgb), []).append(rgb)
    if groups:
        group = max(groups.values(), key=len)
        rgb = tuple(sorted(pixel[channel] for pixel in group)[len(group) // 2] for channel in range(3))
    else:
        rgb = (28, 28, 28)
    light = _luminance(rgb) > .179
    palette = ({"fg": "#202020", "dim": "#595959", "green": "#126b35", "yellow": "#805000"}
               if light else {"fg": "#e6e6e6", "dim": "#8a8a8a", "green": "#5fd38a", "yellow": "#f2c94c"})
    background = _luminance(rgb)
    for key, color in palette.items():
        foreground = _luminance(tuple(int(color[i:i+2], 16) for i in (1, 3, 5)))
        if (max(background, foreground) + .05) / (min(background, foreground) + .05) < 4.5:
            palette[key] = "#000000" if light else "#ffffff"
    return {"bg": "#%02x%02x%02x" % rgb, **palette}


def taskbar_sample_points(info, dpi, excluded=None):
    """Sample inset top/bottom bands, away from icon centres and our own window."""
    if not info or info["auto_hide"] or info["edge"] != 3:
        return []
    left, top, right, bottom = info["rect"]
    inset = max(2, round(4 * dpi / 96))
    if right - left <= inset * 2 or bottom - top <= inset * 2:
        return []
    points = [(left + (right-left) * i // 10, y)
              for i in range(1, 10) for y in (top + inset, bottom - inset - 1)]
    return [(x, y) for x, y in points if excluded is None or not
            (excluded[0] <= x < excluded[2] and excluded[1] <= y < excluded[3])]


def taskbar_colors(info, dpi, excluded=None):
    """Read a few composited screen pixels; always release the borrowed screen DC."""
    points = taskbar_sample_points(info, dpi, excluded)
    if sys.platform != "win32" or not points:
        return []
    import ctypes
    from ctypes import wintypes
    try:
        user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
        user.GetDC.argtypes = [wintypes.HWND]
        user.GetDC.restype = wintypes.HDC
        user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        gdi.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi.GetPixel.restype = wintypes.DWORD
        dc = user.GetDC(None)
        if not dc:
            return []
        try:
            colors = [gdi.GetPixel(dc, x, y) for x, y in points]
            return [(color & 255, (color >> 8) & 255, (color >> 16) & 255)
                    for color in colors if color != 0xffffffff]
        finally:
            user.ReleaseDC(None, dc)
    except (AttributeError, OSError):
        return []


def popup_position(x, y, width, height, area, parent=None):
    """Clamp a popup to a monitor work area; prefer right, then left of parent."""
    left, top, right, bottom = area
    width, height = min(width, right - left), min(height, bottom - top)
    if parent is not None:
        x = parent[2] if parent[2] + width <= right else parent[0] - width
    elif x + width > right:
        x -= width
    if y + height > bottom:
        y = bottom - height if parent is not None else y - height
    return max(left, min(x, right - width)), max(top, min(y, bottom - height)), width, height


def _monitor_area(widget, x, y):
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

        user = ctypes.windll.user32
        user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user.MonitorFromPoint.restype = wintypes.HANDLE
        user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        handle = user.MonitorFromPoint(wintypes.POINT(x, y), 2)
        if user.GetMonitorInfoW(handle, ctypes.byref(info)):
            rect = info.work
            return rect.left, rect.top, rect.right, rect.bottom
    return (widget.winfo_vrootx(), widget.winfo_vrooty(),
            widget.winfo_vrootx() + widget.winfo_vrootwidth(),
            widget.winfo_vrooty() + widget.winfo_vrootheight())


def _dark_titlebar(window):
    """Ask supported Windows versions to use a dark native title bar."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    try:
        window.update_idletasks()
        user = ctypes.windll.user32
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        hwnd = user.GetParent(window.winfo_id())
        setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
        setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        enabled = wintypes.BOOL(True)
        setter(hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
    except (AttributeError, OSError):
        pass  # The themed client area also works without DWM support.


class DarkMenu:
    """One owned popup tree. All event callbacks belong to disposable widgets."""
    BG, FG, HOVER, DISABLED = "#2b2b2b", "#e6e6e6", "#3a3a3a", "#6b6b6b"

    def __init__(self, strip, items, area, parent=None):
        tk = strip.tk
        self.strip, self.items, self.area, self.parent = strip, items, area, parent
        self.child = None
        self.active = None
        self.pending = None
        self.window = tk.Toplevel(parent.window if parent else strip.root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.configure(bg=self.HOVER)
        self.canvas = tk.Canvas(self.window, bg=self.BG, bd=0, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=1, pady=1)
        self.body = tk.Frame(self.canvas, bg=self.BG)
        self.body_id = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.labels = []
        for index, item in enumerate(items):
            if item is None:
                label = tk.Frame(self.body, height=1, bg=self.HOVER)
                label.pack(fill="x", padx=8, pady=5)
            else:
                enabled = item.get("enabled", True)
                label = tk.Label(self.body, text=item["label"], anchor="w", bg=self.BG,
                                 fg=self.FG if enabled else self.DISABLED,
                                 font=("Segoe UI", strip.font[1] + 1), padx=14, pady=6)
                label.pack(fill="x")
                label.bind("<Enter>", lambda event, i=index: self.select(i, hover=True))
                label.bind("<ButtonRelease-1>", lambda event, i=index: self.invoke(i))
                if item.get("tooltip"):
                    strip.tooltip.bind(label, item["tooltip"])
            self.labels.append(label)
        self.window.bind("<Escape>", lambda event: self.close())
        self.window.bind("<Up>", lambda event: self.step(-1))
        self.window.bind("<Down>", lambda event: self.step(1))
        self.window.bind("<Return>", lambda event: self.invoke(self.active))
        self.window.bind("<Right>", lambda event: self.open_child(keyboard=True))
        self.window.bind("<Left>", self.back)
        self.window.bind("<ButtonPress-1>", self.outside)
        self.window.bind("<ButtonPress-3>", self.outside)
        self.window.bind("<FocusOut>", self.focus_out)
        self.window.bind("<MouseWheel>", self.wheel)

    def show(self, x, y, parent=None):
        self.window.update_idletasks()
        width = max(260, self.body.winfo_reqwidth() + 2)
        height = self.body.winfo_reqheight() + 2
        x, y, width, height = popup_position(x, y, width, height, self.area, parent)
        self.canvas.itemconfigure(self.body_id, width=width - 2)
        self.canvas.configure(scrollregion=(0, 0, width - 2, self.body.winfo_reqheight()))
        # +negative coordinates mean absolute virtual-desktop positions in Tk.
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.deiconify()
        self.window.lift()
        if not self.parent:
            self.window.grab_set()
            self.window.focus_force()

    def top(self):
        return self.parent.top() if self.parent else self

    def close(self):
        self.strip._close_menu()
        return "break"

    def destroy(self):
        if self.pending is not None:
            self.window.after_cancel(self.pending)
            self.pending = None
        if self.child:
            self.child.destroy()
            self.child = None
        self.window.destroy()

    def select(self, index, hover=False):
        item = self.items[index]
        if item is None or not item.get("enabled", True):
            return
        if hover:
            self.window.focus_force()
        if self.active != index:
            if self.child:
                self.child.destroy()
                self.child = None
            if self.active is not None:
                self.labels[self.active].configure(bg=self.BG)
            self.active = index
            self.labels[index].configure(bg=self.HOVER)
        if hover and "children" in item:
            self.open_child()

    def step(self, delta):
        eligible = [i for i, item in enumerate(self.items) if item and item.get("enabled", True)]
        if eligible:
            index = ((eligible.index(self.active) + delta) % len(eligible)
                     if self.active in eligible else (0 if delta > 0 else -1))
            self.select(eligible[index])
            label = self.labels[self.active]
            top = self.canvas.canvasy(0)
            bottom = top + self.canvas.winfo_height()
            y, height = label.winfo_y(), label.winfo_height()
            if y < top or y + height > bottom:
                offset = y if y < top else y + height - self.canvas.winfo_height()
                self.canvas.yview_moveto(offset / max(1, self.body.winfo_height()))
        return "break"

    def invoke(self, index):
        if index is None:
            return "break"
        item = self.items[index]
        if item is None or not item.get("enabled", True):
            return "break"
        self.select(index)
        if "children" in item:
            return self.open_child(keyboard=True)
        command = item.get("command")
        self.close()
        if command:
            command()
        return "break"

    def open_child(self, keyboard=False):
        if self.active is None or "children" not in self.items[self.active]:
            return "break"
        if self.child is None:
            self.child = DarkMenu(self.strip, self.items[self.active]["children"], self.area, self)
            x, y = self.window.winfo_rootx(), self.labels[self.active].winfo_rooty()
            self.child.show(x, y, (x, y, x + self.window.winfo_width(), y))
        if keyboard:
            self.child.window.focus_force()
            if self.child.active is None:
                self.child.step(1)
        return "break"

    def back(self, event=None):
        if self.parent:
            parent = self.parent
            parent.window.focus_force()
            self.destroy()
            parent.child = None
        return "break"

    def outside(self, event):
        menu = self.top()
        while menu:
            w = menu.window
            if (w.winfo_rootx() <= event.x_root < w.winfo_rootx() + w.winfo_width()
                    and w.winfo_rooty() <= event.y_root < w.winfo_rooty() + w.winfo_height()):
                return
            menu = menu.child
        return self.close()

    def focus_out(self, event):
        top = self.top()
        if top.pending is None:
            top.pending = top.window.after_idle(top.check_focus)

    def check_focus(self):
        self.pending = None
        focus = self.window.focus_displayof()
        menu = self
        while menu:
            if focus is not None and focus.winfo_toplevel() == menu.window:
                return
            menu = menu.child
        self.close()

    def wheel(self, event):
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"


class Strip:
    BG, FG, DIM = "#1f1f1f", "#e6e6e6", "#8a8a8a"
    COLORS = {"claude": "#d97757", "codex": "#e6e6e6", "antigravity": "#5b9bf8"}
    GREEN, YELLOW = "#5fd38a", "#f2c94c"

    def __init__(self, home=None, opener=None):
        import tkinter as tk
        self.opener = opener or open_conversation_url
        self.tk = tk
        self.home = home
        self.expanded = set()
        self.show_all = False
        self._max_rows = None
        self.anchor = None
        self.order = []
        self.names = {}
        self.unread_counts = {}
        self._context_menu = None
        self.panel = None
        self.panel_frame = None
        self._outside_timer = None
        self._refresh_timer = None
        self._zorder_timer = None
        self._mouse_down = False
        self._start = None
        self._rows = []
        self._palette_key = None
        self._palette_at = 0
        self._compact_palette = compact_palette()
        self.mode = load_prefs(home)["mode"]
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("handback")
        self.icons = []
        for size in (256, 64, 48, 32, 16):
            try:
                asset = resources.files("handback").joinpath("assets", f"icon-{size}.png")
                self.icons.append(tk.PhotoImage(master=self.root, data=asset.read_bytes(), format="png"))
            except (OSError, tk.TclError):
                pass  # Source-only or incomplete installs still have a usable dashboard.
        if self.icons:
            self.root.iconphoto(True, *self.icons)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.93)
        self.root.configure(bg=self.BG)
        self.font = ("Segoe UI", 9)
        self.bold = ("Segoe UI Semibold", 9)
        from tkinter.font import Font
        self.metrics = Font(root=self.root, font=self.font)
        self.bold_metrics = Font(root=self.root, font=self.bold)
        self.compact_metrics = Font(root=self.root, family="Segoe UI", size=8)
        self.widths = column_widths(self.bold_metrics.measure)
        self.row_height = max(22, self.metrics.metrics("linespace") + 6)
        self.tooltip = DarkTooltip(self)
        self.frame = tk.Frame(self.root, bg=self.BG, padx=10, pady=6,
                              highlightthickness=1, highlightbackground=DarkMenu.HOVER)
        self.frame.pack()
        self.root.bind("<Destroy>", lambda event: self.tooltip.hide()
                       if event.widget == self.root else None)
        self.root.bind("<Button-3>", lambda event: self._menu(event, None))
        self.root.bind("<ButtonPress-1>", self._press)
        self.root.bind("<B1-Motion>", self._drag)
        self.root.bind("<ButtonRelease-1>", self._release)
        self.root.bind("<Escape>", lambda event: self._collapse_panel())
        self.root.bind("<Destroy>", self._destroyed, add="+")

    def _destroyed(self, event):
        if event.widget == self.root:
            for name in ("_outside_timer", "_refresh_timer", "_zorder_timer"):
                timer = getattr(self, name)
                if timer is not None:
                    self.root.after_cancel(timer)
                    setattr(self, name, None)

    def _sync_zorder_timer(self):
        if self.mode == "taskbar" and sys.platform == "win32":
            if self._zorder_timer is None:
                self._watch_zorder()
        elif self._zorder_timer is not None:
            self.root.after_cancel(self._zorder_timer)
            self._zorder_timer = None

    def _watch_zorder(self):
        self._zorder_timer = None
        keep_above_taskbar(self.root)
        self._zorder_timer = self.root.after(TASKBAR_ZORDER_MS, self._watch_zorder)

    def _label(self, text, row, column, fg=None, font=None, padx=(0, 10), project=None):
        label = self.tk.Label(self.frame, text=text, bg=self.BG, fg=fg or self.FG,
                              font=font or self.font, anchor="w")
        label.grid(row=row, column=column, sticky="w", padx=padx)
        if project:
            label.bind("<Button-3>", lambda event: self._menu(event, project))
        return label

    def _hover(self, widget, project=None):
        targets = [widget]
        def collect(parent):
            for child in parent.winfo_children():
                targets.append(child)
                collect(child)
        collect(widget)
        def paint(color):
            for target in targets:
                target.configure(bg=color)
        for target in targets:
            target.bind("<Enter>", lambda event: paint(DarkMenu.BG))
            target.bind("<Leave>", lambda event: paint(self.BG))
            if project:
                target.bind("<Button-3>", lambda event: self._menu(event, project))

    def _toggle_overflow(self):
        self.show_all = not self.show_all
        self.refresh(reschedule=False)

    def render(self, rows):
        self._rows = rows
        prefs = load_prefs(self.home)
        if self.mode != prefs["mode"]:
            self._collapse_panel()
            self.mode = prefs["mode"]
            self.anchor = None
        self._sync_zorder_timer()
        if self.mode == "panel":
            self.root.attributes("-alpha", 0.93)
            self.frame.configure(bg=self.BG, padx=10, pady=6, highlightthickness=1)
            self._render_panel(rows)
        else:
            self._render_compact(rows, prefs)
            if self.panel is not None:
                original = self.frame
                self.frame = self.panel_frame
                try:
                    self._render_panel(rows)
                finally:
                    self.frame = original

    def _render_compact(self, rows, prefs):
        self._close_menu()
        for child in self.frame.winfo_children():
            child.destroy()
        self.names = {row["root"]: row["name"] for row in rows}
        self.unread_counts = {row["root"]: len(row["unread"]) for row in rows}
        shown, _ = arrange(rows, {**prefs, "max_rows": 0})
        self.order = [row["root"] for row in shown]
        info = taskbar_info()
        dpi = _window_dpi(self.root)
        excluded = None
        if self.root.winfo_ismapped():
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            excluded = (x, y, x + self.root.winfo_width(), y + self.root.winfo_height())
        key = ((tuple(info["rect"]), info["edge"], info["auto_hide"]) if info else None, dpi, excluded)
        now = time.monotonic()
        if key != self._palette_key or now - self._palette_at >= 30:
            self._compact_palette = compact_palette(taskbar_colors(info, dpi, excluded))
            self._palette_key, self._palette_at = key, now
        palette = self._compact_palette
        bg = palette["bg"]
        padding, gap = max(1, round(7 * dpi / 96)), max(1, round(3 * dpi / 96))
        # Samples already include the taskbar's transparency; avoid blending them twice.
        self.root.attributes("-alpha", 1.0)
        self.root.configure(bg=bg)
        self.frame.configure(bg=bg, padx=padding, pady=0, highlightthickness=0)
        self.frame.pack_configure(fill="both", expand=True)
        point = (info["rect"][0], info["rect"][1]) if info else (self.root.winfo_x(), self.root.winfo_y())
        area = _monitor_area(self.root, *point)
        # Negative Tk font sizes are pixels; derive them from this window's actual DPI.
        pixels = max(6, round(8 * dpi / 72))
        self.compact_metrics.configure(size=-pixels)
        if info and info["edge"] == 3 and not info["auto_hide"]:
            available = info["rect"][3] - info["rect"][1]
            while self.compact_metrics.metrics("linespace") * 2 > available and pixels > 5:
                pixels -= 1
                self.compact_metrics.configure(size=-pixels)
        measure = self.compact_metrics.measure
        name_width = measure("프로젝트 이름 프로젝트")
        lines = compact_lines(rows, prefs, name_width, measure)
        if not lines:
            self.tk.Label(self.frame, text="handback · 대기", bg=bg, fg=palette["dim"],
                          font=self.compact_metrics, bd=0, padx=0).pack(anchor="w", expand=True)
        for line in lines:
            row = self.tk.Frame(self.frame, bg=bg)
            row.pack(fill="x", expand=True)
            for key, color in (("name", palette["fg"]),
                               ("running", palette["green"] if line["row"]["running"] else palette["dim"]),
                               ("unread", palette["yellow"]), ("more", palette["dim"])):
                label = self.tk.Label(row, text=line[key], bg=bg, fg=color,
                                      font=self.compact_metrics, bd=0, padx=0, pady=0)
                label.pack(side="left", padx=(0, gap if key != "more" else 0))
                label.bind("<Button-3>", lambda event, project=line["row"]["root"]: self._menu(event, project))
                if key in ("running", "unread"):
                    targets = conversation_targets(line["row"], key)
                    if targets:
                        label.configure(cursor="hand2")
                        label.bind("<Button-1>", lambda event, targets=targets: self._conversations(event, targets))
                        # A release after a status click must not toggle the panel.
                        label.bind("<ButtonRelease-1>", lambda event: "break")
        self.root.update_idletasks()
        width = max(self.frame.winfo_reqwidth(), name_width + sum(measure(text) for text in
                    ("● 99", "✉ 99", "+99")) + 3 * gap + 2 * padding)
        geometry = compact_geometry(info, area, width, self.compact_metrics.metrics("linespace") * 2 + 4,
                                    prefs["compact_x"])
        self._compact_geometry = geometry
        x, y, width, height = geometry
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _collapse_panel(self):
        if self._outside_timer is not None:
            self.root.after_cancel(self._outside_timer)
            self._outside_timer = None
        if self.panel is not None:
            self._close_menu()
            self.panel.destroy()
            self.panel = self.panel_frame = None

    def _toggle_panel(self):
        if self.panel is not None:
            self._collapse_panel()
            return
        if self.mode != "taskbar":
            return
        panel = self.panel = self.tk.Toplevel(self.root)
        panel.withdraw()
        panel.overrideredirect(True)
        panel.attributes("-topmost", True)
        panel.configure(bg=self.BG)
        self.panel_frame = self.tk.Frame(panel, bg=self.BG, padx=10, pady=6,
                                        highlightthickness=1, highlightbackground=DarkMenu.HOVER)
        self.panel_frame.pack()
        panel.bind("<Escape>", lambda event: self._collapse_panel())
        panel.bind("<Button-3>", lambda event: self._menu(event, None))
        original = self.frame
        self.frame = self.panel_frame
        try:
            self._render_panel(self._rows)
        finally:
            self.frame = original
        panel.deiconify()
        panel.focus_force()
        self._mouse_down = False
        if sys.platform == "win32":
            import ctypes
            # Discard clicks that occurred before this panel existed.
            left = ctypes.windll.user32.GetAsyncKeyState(1)
            right = ctypes.windll.user32.GetAsyncKeyState(2)
            self._mouse_down = bool(left & 0x8000 or right & 0x8000)
        self._watch_outside()

    def _watch_outside(self):
        self._outside_timer = None
        if self.panel is None:
            return
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            user = ctypes.windll.user32
            # The low bit also catches clicks completed between polling ticks.
            down = bool(user.GetAsyncKeyState(1) & 0x8001 or user.GetAsyncKeyState(2) & 0x8001)
            point = wintypes.POINT()
            if down and not self._mouse_down and user.GetCursorPos(ctypes.byref(point)):
                windows = [self.root, self.panel]
                windows += [w for w in self.root.winfo_children() if isinstance(w, self.tk.Toplevel)]
                menu = self._context_menu
                while menu:
                    windows.append(menu.window)
                    menu = menu.child
                if not any(w.winfo_rootx() <= point.x < w.winfo_rootx() + w.winfo_width()
                           and w.winfo_rooty() <= point.y < w.winfo_rooty() + w.winfo_height()
                           for w in windows):
                    self._collapse_panel()
                    return
            self._mouse_down = down
        self._outside_timer = self.root.after(80, self._watch_outside)

    def _render_panel(self, rows):
        self._close_menu()
        self.tooltip.hide()
        for child in self.frame.winfo_children():
            child.destroy()
        prefs = load_prefs(self.home)
        if self._max_rows != prefs["max_rows"]:
            self.show_all = False
            self._max_rows = prefs["max_rows"]
        self.names = {row["root"]: row["name"] for row in rows}
        self.unread_counts = {row["root"]: len(row["unread"]) for row in rows}
        limited, hidden = arrange(rows, prefs)
        shown, overflow = display_rows(rows, prefs, self.show_all)
        self.order = [row["root"] for row in limited + hidden]
        if not shown:
            self._label("표시할 handback 프로젝트 없음 (우클릭으로 설정)", 0, 0, fg=self.DIM)
        line = 0
        for data in shown:
            key = data["root"]
            row = self.tk.Frame(self.frame, bg=self.BG, width=sum(self.widths), height=self.row_height)
            row.grid(row=line, column=0, sticky="ew")
            row.pack_propagate(False)
            cells = []
            for width in self.widths:
                cell = self.tk.Frame(row, bg=self.BG, width=width, height=self.row_height)
                cell.pack(side="left", fill="y")
                cell.pack_propagate(False)
                cells.append(cell)
            name = self.tk.Label(cells[0], bg=self.BG, fg=self.FG, font=self.bold, anchor="w",
                                 cursor="hand2", text=("▾ " if key in self.expanded else "▸ ") +
                                 truncate_text(data["name"], self.widths[0] - 26, self.bold_metrics.measure))
            name.pack(fill="both", expand=True)
            name.bind("<Button-1>", lambda event, key=key: self._toggle(key))
            parts = [(data["lead"], True), ("→", False)]
            for position, worker in enumerate(data["workers"]):
                if position:
                    parts.append(("+", False))
                parts.append((worker, True))
            for text, is_agent in parts:
                agent = str(text).partition(":")[0]
                self.tk.Label(cells[1], text=agent_name(text) if is_agent else text, bg=self.BG,
                              fg=self.COLORS.get(agent, self.DIM) if is_agent else self.DIM,
                              font=self.bold if is_agent else self.font, padx=1, bd=0).pack(side="left")
            running, unread = len(data["running"]), len(data["unread"])
            for column, text, color in (
                    (2, f"● 진행 {running}" if running else "대기", self.GREEN if running else self.DIM),
                    (3, f"✉ {unread}" if unread else "", self.YELLOW),
                    (4, ago(data["last_activity"]), self.DIM)):
                label = self.tk.Label(cells[column], text=truncate_text(text, self.widths[column] - 4,
                                      self.metrics.measure), bg=self.BG, fg=color, font=self.font,
                                      anchor="e" if column == 4 else "w")
                label.pack(fill="both", expand=True)
            self._hover(row, key)
            self.tooltip.bind(name, data["name"])
            for column, kind in ((2, "running"), (3, "unread")):
                targets = conversation_targets(data, kind)
                if not targets:
                    continue
                cell = cells[column].winfo_children()[0]
                cell.configure(cursor="hand2")
                cell.bind("<Button-1>", lambda event, targets=targets: self._conversations(event, targets))
                tip = (agent_name(targets[0]["handle"]) + " 대화 열기" if len(targets) == 1
                       and conversation_link(targets[0]["handle"]) else "워커 대화 선택")
                if len(targets) == 1 and not conversation_link(targets[0]["handle"]):
                    tip = UNSUPPORTED_CONVERSATION
                self.tooltip.bind(cell, tip)
            line += 1
            if key in self.expanded:
                details = [("● 진행 중 · " + item["name"], self.GREEN, None) for item in data["running"]]
                for index, item in enumerate(data["unread"][:DETAIL_LIMIT]):
                    when = _local_time(item["created_utc"])
                    details.append((f"✉ {when} · {item['from']} · {item['body']}",
                                    self.YELLOW if item["kind"] == "result" else "#f28b82", index))
                if len(data["unread"]) > DETAIL_LIMIT:
                    details.append((f"… 전체 {len(data['unread'])}개 보기", self.DIM, 0))
                box = self.tk.Frame(self.frame, bg=self.BG)
                box.grid(row=line, column=0, sticky="ew", padx=(9, 0), pady=(0, 3))
                self.tk.Frame(box, bg=DarkMenu.HOVER, width=2).pack(side="left", fill="y", padx=(0, 8))
                body = self.tk.Frame(box, bg=self.BG)
                body.pack(side="left", fill="both", expand=True)
                for text, color, index in details or [("세부 항목 없음", self.DIM, None)]:
                    label = self.tk.Label(body, text=truncate_text(text, min(520, sum(self.widths) - 32),
                                          self.metrics.measure), bg=self.BG, fg=color, font=self.font,
                                          anchor="w", cursor="hand2" if index is not None else "")
                    label.pack(fill="x")
                    if index is not None:
                        self._hover(label, key)
                        label.bind("<Button-1>", lambda event, data=data, index=index: self._open(data, index))
                    self.tooltip.bind(label, text)
                line += 1
        if overflow or (self.show_all and hidden):
            busy = any(row["running"] or row["unread"] for row in overflow)
            label = self._label(f"+{len(overflow)}개 더 보기" if overflow else "접기", line, 0,
                                fg=self.YELLOW if busy else self.DIM, padx=(0, 0))
            label.configure(cursor="hand2")
            self._hover(label)
            label.bind("<Button-1>", lambda event: self._toggle_overflow())
        self._place()

    def _close_menu(self):
        self.tooltip.hide()
        if self._context_menu is not None:
            menu, self._context_menu = self._context_menu, None
            menu.destroy()

    def _launch_conversation(self, handle):
        url = conversation_link(handle)
        if url:
            try:
                self.opener(url)
            except (OSError, ValueError) as error:
                self._dialog("대화 열기 실패", str(error))

    def _conversations(self, event, targets):
        self._close_menu()
        if len(targets) == 1 and conversation_link(targets[0]["handle"]):
            self._launch_conversation(targets[0]["handle"])
            return "break"
        items = []
        for target in targets:
            handle = target["handle"]
            supported = conversation_link(handle) is not None
            try:
                timestamp = datetime.fromisoformat(target["created_utc"]).timestamp()
            except (ValueError, TypeError):
                timestamp = 0
            label = f"{_preview(target['name'], 32)} · {agent_name(handle)} · {ago(timestamp)}"
            items.append({"label": label, "enabled": supported,
                          "tooltip": target["name"] if supported else UNSUPPORTED_CONVERSATION,
                          "command": lambda handle=handle: self._launch_conversation(handle)})
        if not items:
            return "break"
        menu = DarkMenu(self, items, _monitor_area(self.root, event.x_root, event.y_root))
        self._context_menu = menu
        menu.show(event.x_root, event.y_root)
        return "break"

    def _menu(self, event, project):
        self.tooltip.hide()
        self._close_menu()
        prefs = load_prefs(self.home)
        items = []
        if project:
            data = next((row for row in self._rows if row["root"] == project), {})
            execution = [{"label": target["label"], "enabled": target["enabled"],
                          "tooltip": "Claude 앱에서 직접 설정합니다" if not target["enabled"] else "",
                          "command": lambda target=target: self._execution_settings(
                              project, target["agent"], target["role"])}
                         for target in execution_targets(data)]
            items.extend([
                {"label": "위로 올리기", "command": lambda: self._save(move(prefs, self.order, project, -1))},
                {"label": "아래로 내리기", "command": lambda: self._save(move(prefs, self.order, project, 1))},
                {"label": "이 프로젝트 숨기기", "command": lambda: self._save(
                    {**prefs, "hidden": prefs["hidden"] + [project]})}, None,
                {"label": f"미확인 결과 모두 확인 처리 ({self.unread_counts.get(project, 0)}개)",
                 "enabled": bool(self.unread_counts.get(project, 0)),
                 "command": lambda: self._acknowledge(project)},
                {"label": "handback 등록 해제…", "command": lambda: self._release_project(project)}, None])
            if execution:
                items.extend([{"label": "모델·추론 설정 ▶", "children": execution}, None])
        settings = [{"label": "표시 줄 수", "enabled": False}]
        for count, label in ((2, "2줄"), (3, "3줄"), (0, "전부")):
            settings.append({"label": ("✓ " if prefs["max_rows"] == count else "    ") + label,
                             "command": lambda count=count: self._set_max_rows(prefs, count)})
        settings.extend([None, {"label": "표시 방식", "enabled": False}])
        for mode, label in (("taskbar", "작업표시줄 모드"), ("panel", "펼친 패널 고정")):
            settings.append({"label": ("✓ " if prefs["mode"] == mode else "    ") + label,
                             "command": lambda mode=mode: self._save({**prefs, "mode": mode})})
        settings.extend([None, {"label": "숨긴 프로젝트", "enabled": False}])
        for root in prefs["hidden"]:
            settings.append({"label": self.names.get(root, Path(root).name) + " 다시 표시",
                             "command": lambda root=root: self._save(
                                 {**prefs, "hidden": [x for x in prefs["hidden"] if x != root]})})
        if not prefs["hidden"]:
            settings.append({"label": "숨긴 프로젝트 없음", "enabled": False})
        settings.extend([None, {"label": "순서 초기화 (최근 활동순)",
                                "command": lambda: self._save({**prefs, "order": []})}])
        items.extend([{"label": "보기 설정 ▶", "children": settings}, None,
                      {"label": "새로고침", "command": lambda: self.refresh(reschedule=False)},
                      {"label": "닫기", "command": self.root.destroy}])
        menu = DarkMenu(self, items, _monitor_area(self.root, event.x_root, event.y_root))
        self._context_menu = menu
        menu.show(event.x_root, event.y_root)
        return "break"

    def _button(self, parent, text, command):
        button = self.tk.Button(parent, text=text, command=command, font=self.font,
                                bg="#2b2b2b", fg=self.FG, activebackground="#3a3a3a",
                                activeforeground=self.FG, relief="flat", bd=0, padx=14, pady=6,
                                highlightthickness=1, highlightbackground="#3a3a3a",
                                highlightcolor=self.YELLOW, cursor="hand2")
        button.bind("<Enter>", lambda event: button.configure(bg="#3a3a3a"))
        button.bind("<Leave>", lambda event: button.configure(bg="#2b2b2b"))
        button.bind("<Return>", lambda event: button.invoke())
        return button

    def _execution_settings(self, project, agent, role):
        self._close_menu()
        previous = getattr(self, "_execution_window", None)
        if previous is not None and previous.winfo_exists():
            previous.destroy()
        try:
            values = config.resolve(project, home=self.home, validate=False)["values"]
        except (OSError, ValueError, TimeoutError) as error:
            self._dialog("설정 읽기 실패", str(error))
            return
        settings = values.get("agents", {}).get(agent, {})
        selected = settings.get(role, {})
        tk = self.tk
        window = tk.Toplevel(self.root)
        self._execution_window = window
        window.withdraw()
        window.title(f"{self.names.get(project, Path(project).name)} · 모델·추론 설정")
        window.configure(bg=self.BG)
        window.resizable(False, False)
        window.attributes("-topmost", True)
        window.transient(self.root)
        frame = tk.Frame(window, bg=self.BG, padx=18, pady=16)
        frame.pack(fill="both", expand=True)
        role_label = "리드" if role == "lead" else "워커"
        tk.Label(frame, text=f"{role_label} · {agent_name(agent)}", bg=self.BG, fg=self.YELLOW,
                 font=("Segoe UI Semibold", 11)).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        entries = {}
        fields = [("model", "모델 ID")]
        if agent == "codex":
            fields.append(("reasoning_effort", "추론 강도"))
        for row, (key, label) in enumerate(fields, 1):
            tk.Label(frame, text=label, bg=self.BG, fg=self.FG,
                     font=self.font).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=6)
            entry = tk.Entry(frame, width=34, bg="#2b2b2b", fg=self.FG,
                             insertbackground=self.FG, relief="flat", font=self.font,
                             highlightthickness=1, highlightbackground="#555555", highlightcolor=self.YELLOW)
            entry.grid(row=row, column=1, sticky="ew", pady=6, ipady=5)
            entry.insert(0, selected.get(key) or "")
            entries[key] = entry
        notes = "빈 값은 공통 설정·앱 기본값을 상속합니다.\n새 대화의 기본값으로 사용하며, 기존 대화는 저장된 선택값을 유지합니다."
        if agent == "codex":
            notes += "\n추론 강도는 선택한 모델이 지원하는 값을 입력합니다(예: ultra)."
        else:
            notes += "\nAntigravity 모델은 대화의 첫 생성 시 적용됩니다."
        tk.Label(frame, text=notes, bg=self.BG, fg=self.DIM, font=self.font, wraplength=430,
                 justify="left").grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(12, 4))
        error_text = tk.StringVar(window)
        tk.Label(frame, textvariable=error_text, bg=self.BG, fg="#f28b82", font=self.font,
                 wraplength=430, justify="left").grid(row=len(fields) + 2, column=0, columnspan=2, sticky="w")
        footer = tk.Frame(frame, bg=self.BG)
        footer.grid(row=len(fields) + 3, column=0, columnspan=2, sticky="e", pady=(12, 0))

        def save():
            requested = {key: entry.get().strip() or None for key, entry in entries.items()}
            try:
                config.configure_execution(project, agent, role, home=self.home, **requested)
            except (OSError, ValueError, TimeoutError) as error:
                error_text.set(str(error))
                return
            window.destroy()
            self.refresh(reschedule=False)

        self._button(footer, "저장", save).pack(side="right", padx=(8, 0))
        self._button(footer, "취소", window.destroy).pack(side="right")
        window.bind("<Escape>", lambda event: window.destroy())
        window.bind("<Return>", lambda event: save())
        window.update_idletasks()
        x, y, width, height = popup_position(self.root.winfo_rootx(), self.root.winfo_rooty(),
                                           window.winfo_reqwidth(), window.winfo_reqheight(),
                                           _monitor_area(self.root, self.root.winfo_rootx(), self.root.winfo_rooty()))
        window.geometry(f"{width}x{height}+{x}+{y}")
        _dark_titlebar(window)
        window.deiconify()
        entries["model"].focus_set()

    def _dialog(self, title, message, parent=None, confirm=False):
        self.tooltip.hide()
        self._close_menu()
        tk = self.tk
        parent = parent or self.root
        window = tk.Toplevel(parent)
        window.withdraw()
        window.title(title)
        window.configure(bg=self.BG)
        window.resizable(False, False)
        window.attributes("-topmost", True)
        window.transient(parent)
        result = False

        def finish(value=False):
            nonlocal result
            result = value
            window.destroy()

        window.protocol("WM_DELETE_WINDOW", finish)
        window.bind("<Escape>", lambda event: finish())
        tk.Label(window, text=message, bg=self.BG, fg=self.FG, font=("Segoe UI", 10),
                 wraplength=420, justify="left", padx=20, pady=18).pack(fill="both")
        footer = tk.Frame(window, bg=self.BG, padx=16, pady=12)
        footer.pack(fill="x")
        safe = self._button(footer, "아니요" if confirm else "확인", finish)
        safe.pack(side="right", padx=(8, 0))
        if confirm:
            self._button(footer, "예", lambda: finish(True)).pack(side="right")
        window.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - window.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - window.winfo_reqheight()) // 2
        x, y, width, height = popup_position(x, y, window.winfo_reqwidth(), window.winfo_reqheight(),
                                           _monitor_area(parent, parent.winfo_rootx(), parent.winfo_rooty()))
        window.geometry(f"{width}x{height}+{x}+{y}")
        previous_focus = parent.focus_displayof()
        previous_grab = parent.grab_current()
        window.deiconify()
        window.wait_visibility()
        _dark_titlebar(window)
        window.grab_set()
        safe.focus_force()
        try:
            parent.wait_window(window)
        finally:
            if previous_grab is not None and previous_grab.winfo_exists():
                previous_grab.grab_set()
            if previous_focus is not None and previous_focus.winfo_exists():
                previous_focus.focus_set()
        return result

    def _open(self, data, focus):
        """Viewer for unacknowledged results; ACK only via its confirmed button."""
        self.tooltip.hide()
        tk = self.tk
        window = tk.Toplevel(self.root)
        window.title(f"{data['name']} · 미확인 결과 {len(data['unread'])}개")
        window.attributes("-topmost", True)
        window.geometry("760x520")
        window.configure(bg=self.BG)
        footer = tk.Frame(window, bg=self.BG, padx=8, pady=6)
        footer.pack(side="bottom", fill="x")
        self._button(footer, text="모두 확인 처리", command=lambda: self._acknowledge(
            data["root"], parent=window) and window.destroy()).pack(side="right")
        tk.Label(footer, text="확인 처리해도 내용은 수신함에 남습니다", bg=self.BG, fg=self.DIM,
                 font=self.font).pack(side="left")
        text = tk.Text(window, wrap="word", bg=self.BG, fg=self.FG, insertbackground=self.FG,
                       font=("Segoe UI", 10), padx=12, pady=10, bd=0)
        from tkinter import ttk
        style = ttk.Style(window)
        style.theme_use("clam")
        style.configure("Dark.Vertical.TScrollbar", background="#2b2b2b", troughcolor=self.BG,
                        bordercolor=self.BG, arrowcolor=self.FG, lightcolor="#2b2b2b",
                        darkcolor="#2b2b2b")
        style.map("Dark.Vertical.TScrollbar", background=[("active", "#3a3a3a")])
        bar = ttk.Scrollbar(window, command=text.yview, style="Dark.Vertical.TScrollbar")
        text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("head", foreground=self.YELLOW, font=("Segoe UI Semibold", 10))
        text.tag_configure("error", foreground="#f28b82", font=("Segoe UI Semibold", 10))
        for index, item in enumerate(data["unread"]):
            text.mark_set(f"m{index}", "end-1c")
            text.mark_gravity(f"m{index}", "left")
            head = f"{_local_time(item['created_utc'])} · {item['kind']} · {item['from']}\n"
            text.insert("end", head, "error" if item["kind"] == "error" else "head")
            text.insert("end", item["body"].strip() + "\n\n")
        text.configure(state="disabled")
        text.see(f"m{focus}")
        text.yview(f"m{focus}")
        _dark_titlebar(window)

    def _acknowledge(self, project, parent=None):
        name = self.names.get(project, project)
        count = self.unread_counts.get(project, 0)
        if not self._dialog(
                "미확인 결과 정리",
                f"{name}의 미확인 결과 {count}개를 모두 확인 처리할까요?\n\n"
                "확인 처리한 결과는 리더 세션의 Monitor·복구 훅이 다시 전달하지 않습니다. "
                "내용은 지워지지 않고 수신함에 남습니다.", parent=parent or self.root, confirm=True):
            return False
        try:
            done = acknowledge_all(project, self.home)
        except (OSError, ValueError, TimeoutError) as error:
            self._dialog("정리 실패", str(error), parent=parent or self.root)
            return False
        self.refresh(reschedule=False)
        self._dialog("정리 완료", f"{done}개를 확인 처리했습니다.", parent=parent or self.root)
        return True

    def _release_project(self, project):
        name = self.names.get(project, project)
        if not self._dialog(
                "handback 등록 해제",
                f"{name}을(를) handback에서 등록 해제할까요?\n\n"
                "리더·워커 설정, 요청 기록, 수신함이 상태 홈의 released 폴더로 옮겨집니다(삭제 아님). "
                "이 프로젝트에서 handback을 다시 쓰면 새로 등록됩니다.", parent=self.root, confirm=True):
            return
        try:
            target = release(project, self.home)
        except (OSError, ValueError, TimeoutError) as error:
            self._dialog("해제 실패", str(error), parent=self.root)
            return
        self.refresh(reschedule=False)
        self._dialog("해제 완료", f"보관 위치:\n{target}", parent=self.root)

    def _set_max_rows(self, prefs, count):
        self.show_all = False
        self._save({**prefs, "max_rows": count})

    def _save(self, prefs):
        save_prefs(prefs, self.home)
        self.refresh(reschedule=False)

    def _toggle(self, key):
        self.expanded.symmetric_difference_update({key})
        self.refresh(reschedule=False)

    def _place(self):
        self.root.update_idletasks()
        if self.panel is not None and self.frame == self.panel_frame:
            width, height = self.panel.winfo_reqwidth(), self.panel.winfo_reqheight()
            x, y = self.root.winfo_x(), self.root.winfo_y()
            area = _monitor_area(self.root, x, y)
            width, height = min(width, area[2] - area[0]), min(height, area[3] - area[1])
            x = max(area[0], min(x + self.root.winfo_width() - width, area[2] - width))
            y = max(area[1], min(y - height, area[3] - height))
            self.panel.geometry(f"{width}x{height}+{x}+{y}")
            return
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        if self.anchor is None:
            area = _work_area() or (self.root.winfo_screenwidth(), self.root.winfo_screenheight() - 48)
            self.anchor = (area[0] - 8, area[1] - 4)
        right, bottom = self.anchor
        self.root.geometry(f"{width}x{height}+{right - width}+{bottom - height}")

    def _press(self, event):
        self._start = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag(self, event):
        if self._start is None:
            return
        x0, y0, wx, wy = self._start
        if self.mode == "taskbar":
            self._collapse_panel()
            info = taskbar_info()
            area = _monitor_area(self.root, wx, wy)
            x, y, width, height = compact_geometry(info, area, self.root.winfo_width(),
                                                 self.root.winfo_height(), wx + event.x_root - x0)
            self.root.geometry(f"+{x}+{y}")
            return
        self.root.geometry(f"+{wx + event.x_root - x0}+{wy + event.y_root - y0}")

    def _release(self, event):
        if self.mode == "taskbar":
            start, self._start = self._start, None
            if start is None:
                return
            if abs(event.x_root - start[0]) > 3:
                prefs = load_prefs(self.home)
                save_prefs({**prefs, "compact_x": self.root.winfo_x()}, self.home)
            else:
                self._toggle_panel()
            return
        # Keep the bottom-right corner fixed so expanding grows upward and left.
        self.anchor = (self.root.winfo_x() + self.root.winfo_width(),
                       self.root.winfo_y() + self.root.winfo_height())

    def refresh(self, reschedule=True):
        if reschedule and self._refresh_timer is not None:
            self.root.after_cancel(self._refresh_timer)
            self._refresh_timer = None
        self.tooltip.hide()
        self._close_menu()
        try:
            self.render(snapshot(self.home))
        except Exception as error:  # Keep the strip alive through partial writes.
            for child in self.frame.winfo_children():
                child.destroy()
            self._label("읽기 오류: " + str(error)[:60], 0, 0, fg=self.YELLOW)
        if reschedule:
            self._refresh_timer = self.root.after(REFRESH_MS, self.refresh)

    def run(self):
        self.refresh()
        self.root.deiconify()
        self.root.mainloop()


def startup_shortcut():
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/handback 현황판.lnk"


def set_autostart(enabled):
    """Add or remove a per-user Startup shortcut that runs dashboard.pyw."""
    if sys.platform != "win32":
        raise RuntimeError("자동 실행은 Windows에서만 지원합니다")
    link = startup_shortcut()
    if not enabled:
        link.unlink(missing_ok=True)
        return {"autostart": False, "shortcut": str(link)}
    from .invocation import entry_args
    launcher = Path(__file__).resolve().parent.parent / "dashboard.pyw"
    arguments = subprocess.list2cmdline([str(launcher)] if launcher.is_file() else [*entry_args(), "dashboard"])
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        raise RuntimeError(f"pythonw.exe를 찾을 수 없습니다: {pythonw}")

    def quoted(value):
        return "'" + str(value).replace("'", "''") + "'"
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + quoted(link) + ");"
              "$s.TargetPath = " + quoted(pythonw) + ";"
              "$s.Arguments = " + quoted(arguments) + ";"
              "$s.WorkingDirectory = " + quoted(launcher.parent) + ";"
              "$s.Description = 'handback 현황판';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, capture_output=True, timeout=30)
    return {"autostart": True, "shortcut": str(link), "target": str(pythonw), "launcher": str(launcher)}


def main(home=None):
    if sys.platform == "win32":
        import ctypes
        # One strip per user session: autostart plus a manual launch must not stack.
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\handback-dashboard")
        if mutex and ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    Strip(home).run()
