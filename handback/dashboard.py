"""Compact floating status widget with optional taskbar and expanded views.

It never routes or changes agent combinations. Display preferences live in
dashboard.json; project actions acknowledge results, move state aside, or save
explicitly selected model and reasoning defaults.
"""

from contextlib import contextmanager
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

from . import config, envelope, inbox
from .collector import OPEN_STATES
from .state import _read_json, atomic_json, home_lock, state_home


AGENT_NAME = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity"}
REFRESH_MS = 3000
TASKBAR_ZORDER_MS = 750
DETAIL_LIMIT = 5
DEFAULT_PREFS = {"max_rows": 3, "order": [], "hidden": [], "mode": "taskbar",
                 "compact_x": None, "compact_y": None, "docked": False}


def _read(path, errors=None):
    try:
        value = _read_json(path, {})
        if not isinstance(value, dict):
            raise ValueError("상태가 JSON 객체가 아닙니다")
        return value
    except (OSError, ValueError, UnicodeError) as error:
        if errors is not None:
            errors.append((str(path), ui_error(error)))
        return {}


def _read_mail(path):
    """Keep inbox validation/size limits and the state reader's Windows retries."""
    for attempt in range(6):
        try:
            return inbox._read_json(path)
        except PermissionError:
            if sys.platform != "win32" or attempt == 5:
                raise
            time.sleep(0.01 * 2 ** attempt)


def _pending_results(directory, errors):
    """Display-only scan: explicit receipts precede immutable message bodies.

    A well-formed receipt is a display hint; authoritative recipient checks and
    all ACK operations still use inbox.pending/acknowledge without this shortcut.
    """
    for path in sorted(directory.glob("*.json")):
        if not inbox.ID_PATTERN.fullmatch(path.stem):
            continue
        ack_path = directory / "acks" / path.name
        try:
            receipt = _read_mail(ack_path)
            if (not isinstance(receipt, dict) or receipt.get("id") != path.stem
                    or not isinstance(receipt.get("recipient"), str)
                    or not receipt["recipient"].strip()):
                raise ValueError("확인 기록이 올바르지 않습니다")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, UnicodeError) as error:
            errors.append((str(ack_path), ui_error(error)))
        else:
            continue
        try:
            mail = envelope.validate(_read_mail(path))
            if mail["id"] != path.stem:
                raise ValueError("파일 이름과 메시지 ID가 다릅니다")
        except (OSError, ValueError, UnicodeError) as error:
            errors.append((str(path), ui_error(error)))
            continue
        if mail["kind"] != "request":
            yield mail


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
    topology_path = folder / "topology.json"
    try:
        topology = _read_json(topology_path, {})
        if not isinstance(topology, dict):
            raise ValueError("프로젝트 상태가 올바르지 않습니다")
    except (OSError, ValueError, UnicodeError) as error:
        # The checkout is unknown, but the state folder is a stable display key.
        # Keep this failure local so healthy projects remain visible.
        try:
            activity = topology_path.stat().st_mtime
        except OSError:
            activity = 0
        return {"name": "상태 " + folder.name, "root": str(folder), "unresolved": True,
                "lead": "", "workers": [], "combo": "프로젝트 확인 불가",
                "running": [], "unread": [], "last_activity": activity,
                "unreadable": 1, "first_unreadable": str(topology_path),
                "read_error": ui_error(error)}
    root = topology.get("root")
    if not root or not Path(root).is_dir() or _is_scratch(root):
        return None
    errors = []
    threads = _read(folder / "threads.json", errors)
    running = []
    for path in sorted((folder / "requests").glob("*.json")):
        try:
            request = _read_json(path, None)
            if (not isinstance(request, dict) or not isinstance(request.get("status"), str)
                    or not request["status"]):
                raise ValueError("요청 상태가 올바르지 않습니다")
        except (OSError, ValueError, UnicodeError) as error:
            errors.append((str(path), ui_error(error)))
            continue
        if request.get("status") in OPEN_STATES:
            thread = threads.get(request.get("handle"), {})
            running.append({"id": request.get("id", path.stem), "agent": request.get("agent"),
                            "name": thread.get("name") or request.get("handle", ""),
                            "handle": request.get("handle", ""),
                            "status": request.get("status"), "created_utc": request.get("created_utc")})
    unread = []
    mail_dir = folder / "inbox"
    if mail_dir.is_dir():
        for mail in _pending_results(mail_dir, errors):
            name = threads.get(mail.get("sender"), {}).get("name") or mail.get("sender", "")
            unread.append({"id": mail["id"], "kind": mail.get("kind"), "from": name, "sender": mail.get("sender", ""),
                           "created_utc": mail.get("created_utc"), "body": mail.get("body", "")})
        unread.sort(key=lambda item: item["created_utc"] or "", reverse=True)
    lead = topology.get("lead", "")
    workers = topology.get("workers") or []
    return {"name": Path(root).name, "root": root, "lead": lead, "workers": workers,
            "combo": agent_name(lead) + " → " + " + ".join(agent_name(w) for w in workers),
            "running": running, "unread": unread, "last_activity": _latest_mtime(folder),
            "unreadable": len(errors), "first_unreadable": errors[0][0] if errors else "",
            "read_error": errors[0][1] if errors else ""}


def read_error_text(row):
    if not row.get("unreadable"):
        return ""
    return (f"읽기 오류: {row['unreadable']}개 · 집계 불완전\n"
            f"{row['first_unreadable']}\n{row['read_error']}")


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
    # Every ProjectState.lock (including request creation) first holds a shared
    # home lock. Exclusive ownership covers the check AND rename, and lives
    # outside the moved directory, so project-lock ownership cannot split.
    with home_lock(home, exclusive=True):
        folder = _folder_for(root, home)
        running = 0
        for path in (folder / "requests").glob("*.json"):
            # Display filtering and forgiving reads must never hide active or
            # damaged requests from this destructive state transition.
            try:
                with path.open(encoding="utf-8-sig") as stream:
                    request = json.load(stream)
            except (ValueError, UnicodeError) as error:
                raise ValueError(f"요청 상태를 읽을 수 없어 해제할 수 없습니다: {path.name}") from error
            if not isinstance(request, dict) or not isinstance(request.get("status"), str):
                raise ValueError(f"요청 상태가 올바르지 않아 해제할 수 없습니다: {path.name}")
            running += request["status"] in OPEN_STATES
        if running:
            raise ValueError(f"진행 중인 요청 {running}개가 있어 해제할 수 없습니다")
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


def ui_error(error):
    """Translate common UI failures while retaining unknown diagnostic details."""
    message = str(error)
    if ".reasoning_effort must be one of:" in message:
        return "추론 강도는 다음 값 중 하나를 입력하세요: " + ", ".join(config.REASONING_EFFORTS)
    if "must be a nonempty string or null" in message:
        return "모델 ID와 추론 강도는 줄바꿈 없는 값을 입력하거나 비워 두세요."
    if isinstance(error, TimeoutError):
        return "다른 작업이 상태를 사용 중입니다. 잠시 후 다시 시도하세요.\n" + message
    if isinstance(error, PermissionError):
        return "파일 접근 권한이 없습니다.\n" + message
    if isinstance(error, FileNotFoundError):
        return "파일 또는 폴더를 찾을 수 없습니다.\n" + message
    if message.startswith("Agent "):
        for suffix, explanation in ((" is disabled in user configuration", "사용자 설정에서 비활성화되어 있습니다"),
                                    (" is disabled in project configuration", "프로젝트 설정에서 비활성화되어 있습니다"),
                                    (" is not allowed by project configuration", "프로젝트 설정에서 허용되지 않습니다")):
            if message.endswith(suffix):
                return agent_name(message[6:-len(suffix)]) + ": " + explanation
    return message


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
    if type(stored.get("compact_y")) is int:
        prefs["compact_y"] = stored["compact_y"]
    if type(stored.get("docked")) is bool:
        prefs["docked"] = stored["docked"]
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
    """One tooltip with a stable hover deadline across view refreshes."""
    def __init__(self, strip):
        self.strip = strip
        self.pending = self.window = None
        self.target = None
        self.text = None
        self._rebuild = None
        self._retarget_timer = self._retarget_rect = None

    def hide(self, event=None):
        if event is not None and event.widget != self.target:
            return
        self.target = None
        self.text = None
        self._rebuild = None
        self._cancel_retarget()
        if self.pending is not None:
            self.strip.root.after_cancel(self.pending)
            self.pending = None
        if self.window is not None:
            self.window.destroy()
            self.window = None

    def hide_within(self, parent):
        if self.target is not None and (self.target == parent or
                str(self.target).startswith(str(parent) + ".")):
            self.hide()

    @staticmethod
    def _rect(widget):
        return (widget.winfo_rootx(), widget.winfo_rooty(),
                widget.winfo_width(), widget.winfo_height())

    def _cancel_retarget(self):
        if self._retarget_timer is not None:
            self.strip.root.after_cancel(self._retarget_timer)
        self._retarget_timer = self._retarget_rect = None

    def _check_retarget(self):
        expected = self._retarget_rect
        self._cancel_retarget()
        if expected is None:
            return
        # Run after rendering has committed the new window and viewport geometry.
        self.strip.root.update_idletasks()
        if (self.target is None or not self.target.winfo_exists()
                or not self.target.winfo_ismapped() or self._rect(self.target) != expected):
            self.hide()

    @contextmanager
    def preserve_on_rebuild(self, parent):
        saved = None
        if (self.target is not None and self.target.winfo_exists()
                and str(self.target).startswith(str(parent) + ".")):
            key = getattr(self.target, "_tooltip_key", None)
            if key is not None:
                saved = {"key": key, "text": self.text, "replacement": None,
                         "rect": self._retarget_rect or self._rect(self.target)}
                self._cancel_retarget()
                self._rebuild = saved
        try:
            yield
        except BaseException:
            if saved is not None:
                self.hide()
            raise
        finally:
            if saved is not None and self._rebuild is saved:
                self._rebuild = None
                if saved["replacement"] is None:
                    self.hide()
                else:
                    self._retarget_rect = saved["rect"]
                    self._retarget_timer = self.strip.root.after(0, self._check_retarget)

    def schedule(self, widget, text):
        if self.target == widget and self.text == text and (self.pending or self.window):
            return
        self.hide()
        self.target = widget
        self.text = text
        self.pending = self.strip.root.after(500, self._show_pending)

    def _show_pending(self):
        self.pending = None
        self._check_retarget()
        if self.target is not None:
            self.show(self.target, self.text)

    def show(self, widget, text):
        self.hide()
        if not widget.winfo_exists():
            return
        self.target = widget
        self.text = text
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

    def bind(self, widget, text, *, key=None):
        widget._tooltip_key = key
        if (self._rebuild is not None and key == self._rebuild["key"]
                and text == self._rebuild["text"]):
            self._rebuild["replacement"] = self.target = widget
        widget.bind("<Enter>", lambda event: self.schedule(widget, text), add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")
        widget.bind("<Destroy>", lambda event: self.hide()
                    if event.widget == self.target and self._rebuild is None else None, add="+")


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


def taskbar_info(point=None):
    """Explorer taskbar bounds on the requested monitor, in this process's DPI space."""
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
        hwnd = user.FindWindowW("Shell_TrayWnd", None)
        if point is not None:
            user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
            user.MonitorFromPoint.restype = wintypes.HANDLE
            user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user.MonitorFromWindow.restype = wintypes.HANDLE
            monitor = user.MonitorFromPoint(wintypes.POINT(*point), 2)
            if user.MonitorFromWindow(hwnd, 2) != monitor:
                secondary = None
                for _ in range(64):
                    secondary = user.FindWindowExW(None, secondary, "Shell_SecondaryTrayWnd", None)
                    if not secondary:
                        return None
                    if user.MonitorFromWindow(secondary, 2) == monitor:
                        r = wintypes.RECT()
                        if not user.GetWindowRect(secondary, ctypes.byref(r)):
                            return None
                        # Secondary Explorer taskbars have no TrayNotifyWnd.
                        class MonitorInfo(ctypes.Structure):
                            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
                        info = MonitorInfo()
                        info.size = ctypes.sizeof(info)
                        user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
                        if not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
                            return None
                        if r.right - r.left >= r.bottom - r.top:
                            edge = 1 if abs(r.top - info.monitor.top) < abs(r.bottom - info.monitor.bottom) else 3
                        else:
                            edge = 0 if abs(r.left - info.monitor.left) < abs(r.right - info.monitor.right) else 2
                        return {"rect": (r.left, r.top, r.right, r.bottom), "edge": edge,
                                "auto_hide": auto_hide, "notification_left": None, "hwnd": secondary}
                return None
        tray = user.FindWindowExW(hwnd, None, "TrayNotifyWnd", None)
        rect = wintypes.RECT()
        notification = rect.left if tray and user.GetWindowRect(tray, ctypes.byref(rect)) else None
        r = data.rc
        return {"rect": (r.left, r.top, r.right, r.bottom), "edge": data.uEdge,
                "auto_hide": auto_hide, "notification_left": notification, "hwnd": hwnd}
    except (AttributeError, OSError):
        return None


def fullscreen_covers_widget(rect, monitor_rect, foreground_monitor, widget_monitor,
                             class_name="", own_window=False, notification_state=None):
    """Notification state is global: it cannot establish same-monitor coverage."""
    if (own_window or class_name in {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
            or not foreground_monitor or foreground_monitor != widget_monitor
            or not rect or not monitor_rect):
        return False
    left, top, right, bottom = monitor_rect
    return (right > left and bottom > top and rect[0] <= left and rect[1] <= top
            and rect[2] >= right and rect[3] >= bottom)


def widget_fullscreen_covered(widget, point=None):
    """Compare foreground bounds with the full monitor, excluding our process."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    try:
        user = ctypes.windll.user32
        user.GetForegroundWindow.restype = wintypes.HWND
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
        foreground = user.GetForegroundWindow()
        hwnd = user.GetAncestor(widget.winfo_id(), 2)
        monitor = user.MonitorFromWindow(foreground, 2)
        own_monitor = (user.MonitorFromPoint(wintypes.POINT(*point), 2) if point is not None
                       else user.MonitorFromWindow(hwnd, 2))
        if not foreground or not hwnd or monitor != own_monitor:
            return False
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
        name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(foreground, name, len(name))
        rect, info = wintypes.RECT(), MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not user.GetWindowRect(foreground, ctypes.byref(rect)) or not user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return False
        bounds = lambda r: (r.left, r.top, r.right, r.bottom)
        return fullscreen_covers_widget(bounds(rect), bounds(info.rcMonitor), monitor,
                                        own_monitor, name.value, pid.value == os.getpid())
    except (AttributeError, OSError):
        return False


def show_widget_without_activation(widget, visible):
    # Keep Tk's mapping state in sync; ShowWindow(SW_HIDE) alone is undone
    # when a subsequent idle geometry update remaps the wrapper.
    if not visible:
        widget.withdraw()
        return
    widget.deiconify()  # Override-redirect Tk windows are shown without activation.
    import ctypes
    from ctypes import wintypes
    user = ctypes.windll.user32
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.ShowWindow(user.GetAncestor(widget.winfo_id(), 2), 4)  # SW_SHOWNOACTIVATE


def keep_above_taskbar(widget, taskbar=None):
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
    taskbar = taskbar or user.FindWindowW("Shell_TrayWnd", None)
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


def _set_window_corners(widget, width, height, radius, *, wrapper=True):
    """Clip a Tk wrapper or child; Windows owns a region only after success."""
    if sys.platform != "win32" or width <= 0 or height <= 0:
        return False
    import ctypes
    from ctypes import wintypes
    try:
        user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        user.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
        user.SetWindowRgn.restype = ctypes.c_int
        gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        gdi.CreateRoundRectRgn.restype = wintypes.HRGN
        gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi.DeleteObject.restype = wintypes.BOOL
        hwnd = user.GetAncestor(widget.winfo_id(), 2) if wrapper else widget.winfo_id()
        if not hwnd:
            return False
        redraw = bool(widget.winfo_ismapped())
        if radius <= 0:
            return bool(user.SetWindowRgn(hwnd, None, redraw))
        diameter = min(width, height, max(2, round(radius * 2)))
        region = gdi.CreateRoundRectRgn(0, 0, width, height, diameter, diameter)
        if not region:
            return False
        try:
            if user.SetWindowRgn(hwnd, region, redraw):
                region = None  # Ownership transferred; never delete this handle.
                return True
            return False
        finally:
            if region:
                gdi.DeleteObject(region)
    except (AttributeError, OSError):
        return False


def compact_taskbar_rect(info, dpi=96):
    """Leave the taskbar's desktop-facing separator visible (one logical pixel)."""
    left, top, right, bottom = info["rect"]
    inset = min(max(1, round(dpi / 96)), max(0, bottom - top - 1))
    if info["edge"] == 3:
        top += inset
    elif info["edge"] == 1:
        bottom -= inset
    return left, top, right, bottom


def compact_geometry(info, area, width, height, x=None, *, dpi=96):
    """Dock to a horizontal taskbar, respecting the tray when there is enough room."""
    left, top, right, bottom = area
    if (info and info["edge"] in (1, 3) and not info["auto_hide"]
            and info["rect"][2] > info["rect"][0] and info["rect"][3] > info["rect"][1]):
        left, y, right, bottom = compact_taskbar_rect(info, dpi)
        height = bottom - y
        default_right = info.get("notification_left")
        if default_right is None or not left < default_right <= right:
            default_right = right
        if default_right - left >= width:
            right = default_right
    else:
        height = min(height, bottom - top)
        y, default_right = bottom - height - 4, right - 8
        y = max(top, y)
    width = min(width, right - left)
    x = default_right - width if x is None else x
    return max(left, min(x, right - width)), y, width, height


def clamp_compact(x, y, width, height, bounds):
    left, top, right, bottom = bounds
    width, height = min(width, right - left), min(height, bottom - top)
    return (max(left, min(x, right - width)), max(top, min(y, bottom - height)), width, height)


def compact_work_area(monitor, info=None, work_area=None):
    """Use the OS work area, also excluding any visible Explorer taskbar."""
    left, top, right, bottom = monitor
    if work_area is not None:
        left, top = max(left, work_area[0]), max(top, work_area[1])
        right, bottom = min(right, work_area[2]), min(bottom, work_area[3])
    if info and not info["auto_hide"]:
        x1, y1, x2, y2 = info["rect"]
        if x2 > left and x1 < right and y2 > top and y1 < bottom:
            if info["edge"] == 0:
                left = max(left, x2)
            elif info["edge"] == 1:
                top = max(top, y2)
            elif info["edge"] == 2:
                right = min(right, x1)
            elif info["edge"] == 3:
                bottom = min(bottom, y1)
    return (left, top, right, bottom) if right > left and bottom > top else monitor


def floating_geometry(area, width, height, x=None, y=None, dpi=96, *, snap_distance=None):
    """Keep a small inset from usable screen edges, with gentle corner snapping."""
    left, top, right, bottom = area
    width, height = min(width, right - left), min(height, bottom - top)
    margin = max(1, round(8 * dpi / 96))
    horizontal = min(margin, (right - left - width) // 2)
    vertical = min(margin, (bottom - top - height) // 2)
    left, right = left + horizontal, right - horizontal - width
    top, bottom = top + vertical, bottom - vertical - height
    threshold = max(1, round(12 * dpi / 96)) if snap_distance is None else snap_distance

    def align(value, low, high):
        value = high if value is None else max(low, min(value, high))
        nearest = min((low, high), key=lambda edge: abs(value - edge))
        return nearest if abs(value - nearest) <= threshold else value

    return align(x, left, right), align(y, top, bottom), width, height


def compact_drag_geometry(info, monitor, virtual, x, y, width, height, dpi=96, *, from_dock=False,
                          snap_distance=None, work_area=None):
    """Every drag is free; docking is decided only when the user releases it."""
    return False, clamp_compact(x, y, width, height, monitor)


def compact_drop_geometry(info, monitor, x, y, width, height, *, cursor=None, dpi=96):
    """Magnetize an intentional drop inside a taskbar, never mere proximity."""
    geometry = clamp_compact(x, y, width, height, monitor)
    x, y, width, height = geometry
    if info and info["edge"] in (1, 3) and not info["auto_hide"]:
        left, top, right, bottom = info["rect"]
        px, py = cursor if cursor is not None else (x + width / 2, y + height / 2)
        overlap_width = max(0, min(x + width, right) - max(x, left))
        overlap_height = max(0, min(y + height, bottom) - max(y, top))
        if (width > 0 and height > 0 and left <= px < right and top <= py < bottom
                and 2 * overlap_width * overlap_height >= width * height):
            return True, compact_geometry(info, monitor, width, height, x, dpi=dpi)
    return False, geometry


def compact_panel_geometry(widget_rect, width, height, area):
    x, y, widget_width, widget_height = widget_rect
    left, top, right, bottom = area
    width, height = min(width, right - left), min(height, bottom - top)
    x = max(left, min(x + widget_width - width, right - width))
    y = y - height if y - height >= top else y + widget_height
    return x, max(top, min(y, bottom - height)), width, height


def drag_position(start, cursor):
    """Always use the press position, even after a snap, clamp or stale event."""
    x, y, wx, wy = start
    return wx + cursor[0] - x, wy + cursor[1] - y


def drag_threshold(dpi):
    return max(1, round(4 * dpi / 96))


def monitor_at_point(rectangles, point):
    """Choose from cached rectangles, including negative origins and desktop gaps."""
    x, y = point
    def distance(rect):
        left, top, right, bottom = rect
        return max(left-x, x-right+1, 0)**2 + max(top-y, y-bottom+1, 0)**2
    return min(rectangles, key=distance)


def _monitor_rects(widget):
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        user = ctypes.windll.user32
        # GetSystemMetrics can be DPI-virtualized independently of shell rectangles.
        # Use the same GetMonitorInfo coordinate space as taskbar placement instead.
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                        ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                          ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        rectangles = []
        def collect(handle, hdc, rect, data):
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if user.GetMonitorInfoW(handle, ctypes.byref(info)):
                r = info.monitor
                rectangles.append((r.left, r.top, r.right, r.bottom))
            return True
        user.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                            callback_type, wintypes.LPARAM]
        user.EnumDisplayMonitors(None, None, callback_type(collect), 0)
        if rectangles:
            return rectangles
        x, y = user.GetSystemMetrics(76), user.GetSystemMetrics(77)
        return [(x, y, x + user.GetSystemMetrics(78), y + user.GetSystemMetrics(79))]
    return [(widget.winfo_vrootx(), widget.winfo_vrooty(),
            widget.winfo_vrootx() + widget.winfo_vrootwidth(),
            widget.winfo_vrooty() + widget.winfo_vrootheight())]


def _virtual_area(widget):
    rectangles = _monitor_rects(widget)
    return (min(r[0] for r in rectangles), min(r[1] for r in rectangles),
            max(r[2] for r in rectangles), max(r[3] for r in rectangles))


def _pointer_capture(widget):
    """Tk's local grab uses a native capture while the real mouse button is down."""
    widget.grab_set()
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        user = ctypes.windll.user32
        user.GetCapture.restype = wintypes.HWND
        return user.GetCapture()
    return None


def _pointer_capture_owned(widget, native_capture):
    if widget.grab_current() != widget:
        return False
    if native_capture and sys.platform == "win32":
        import ctypes
        return ctypes.windll.user32.GetCapture() == native_capture
    return True


def _native_drag_mover(widget):
    """Cache the owned HWND and native call; resize/style changes still use Tk."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    user = ctypes.windll.user32
    user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user.GetAncestor.restype = wintypes.HWND
    hwnd = user.GetAncestor(widget.winfo_id(), 2)
    if not hwnd:
        return None
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user.SetWindowPos.restype = wintypes.BOOL
    # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
    return lambda x, y: bool(user.SetWindowPos(hwnd, None, x, y, 0, 0, 0x15))


def compact_lines(rows, prefs, name_width, measure):
    shown, _ = arrange(rows, {**prefs, "max_rows": 0})
    overflow_unread = sum(len(row["unread"]) for row in shown[2:])
    lines = []
    for i, row in enumerate(shown[:2]):
        lines.append({"row": row, "name": truncate_text(row["name"], name_width, measure),
                      "running": (f"읽기 오류 {row['unreadable']}" if row.get("unreadable") else
                                  f"● {len(row['running'])}" if row["running"] else "대기"),
                      "unread": f"✉ {len(row['unread'])}" if row["unread"] else "",
                      "more": f"+{len(shown) - 2}" if i == 1 and len(shown) > 2 else "",
                      "overflow_unread": overflow_unread if i == 1 else 0})
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
            hwnd = widget.winfo_id()
            # A withdrawn Tk window may not have its wrapper yet. Its client
            # HWND already carries the correct DPI; Tk's screen cache may not.
            dpi = user.GetDpiForWindow(user.GetParent(hwnd) or hwnd)
            if dpi:
                return dpi
        except (AttributeError, OSError):
            pass
    # Tk may report 143.96 before the native wrapper exists at 150% scaling.
    # Match the integer native DPI so a one-DIP border does not change on map.
    return max(1, round(widget.winfo_fpixels("1i")))


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
    if not info or info["auto_hide"] or info["edge"] not in (1, 3):
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


def _monitor_area(widget, x, y, full=False):
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
            rect = info.monitor if full else info.work
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


class PanelViewport:
    """A bounded panel body with a pixel offset that survives row refreshes."""
    def __init__(self, strip, parent):
        tk = strip.tk
        self.row_height = strip.row_height
        self.tooltip = strip.tooltip
        self.offset = 0
        self._wheel_delta = 0
        self.container = tk.Frame(parent, bg=strip.BG)
        self.container.pack_propagate(False)
        self.canvas = tk.Canvas(self.container, bg=strip.BG, bd=0,
                                highlightthickness=0, takefocus=0, yscrollincrement=1)
        from tkinter import ttk
        style = ttk.Style(parent)
        if style.theme_use() != "clam":
            style.theme_use("clam")
        style.configure("Dark.Vertical.TScrollbar", background=DarkMenu.BG, troughcolor=strip.BG,
                        bordercolor=strip.BG, arrowcolor=strip.FG, lightcolor=DarkMenu.BG,
                        darkcolor=DarkMenu.BG)
        style.map("Dark.Vertical.TScrollbar", background=[("active", DarkMenu.HOVER)])
        self.scrollbar = ttk.Scrollbar(self.container, orient="vertical", command=self.scroll,
                                       takefocus=0, style="Dark.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.content = tk.Frame(self.canvas, bg=strip.BG, padx=10, pady=6,
                                highlightthickness=1, highlightbackground=DarkMenu.HOVER)
        self._item = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        # Scrollbar gestures must not reach Strip's top-level drag bindings.
        self.scrollbar.bindtags(tuple(tag for tag in self.scrollbar.bindtags()
                                      if tag != str(parent)))
        parent.bind("<MouseWheel>", self.wheel, add="+")
        parent.bind("<Button-4>", self.wheel, add="+")
        parent.bind("<Button-5>", self.wheel, add="+")

    def dimensions(self, max_width, max_height):
        height = min(self.content.winfo_reqheight(), max_height)
        overflow = self.content.winfo_reqheight() > height
        width = self.content.winfo_reqwidth() + (self.scrollbar.winfo_reqwidth() if overflow else 0)
        return max(1, min(width, max_width)), max(1, height)

    def layout(self, width, height):
        content_height = self.content.winfo_reqheight()
        overflow = content_height > height
        if overflow:
            self.scrollbar.pack(side="right", fill="y", before=self.canvas)
        else:
            self.scrollbar.pack_forget()
        content_width = max(1, width - (self.scrollbar.winfo_reqwidth() if overflow else 0))
        self.container.configure(width=width, height=height)
        self.canvas.configure(width=content_width, height=height,
                              scrollregion=(0, 0, content_width, content_height))
        self.canvas.itemconfigure(self._item, width=content_width)
        self.offset = max(0, min(self.offset, content_height - height))
        self.canvas.yview_moveto(self.offset / max(1, content_height))

    def scroll(self, *args):
        self.tooltip.hide_within(self.content)
        self.canvas.yview(*args)
        self.offset = max(0, round(self.canvas.canvasy(0)))

    def wheel(self, event):
        if not self.container.winfo_ismapped():
            return
        if getattr(event, "num", None) in (4, 5):
            steps = 1 if event.num == 4 else -1
        else:
            self._wheel_delta += event.delta
            steps = int(self._wheel_delta / 120)
            self._wheel_delta -= steps * 120
        if steps:
            self.scroll("scroll", -steps * 3 * self.row_height, "units")
        return "break"


class Strip:
    BG, FG, DIM = "#1f1f1f", "#e6e6e6", "#8a8a8a"
    BORDER = "#6a6a70"
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
        self._panel_viewport = None
        self._fixed_viewport = None
        self._outside_timer = None
        self._refresh_timer = None
        self._zorder_timer = None
        self._fullscreen_hidden = False
        self.docked = load_prefs(home)["docked"]
        self._dragging = False
        self._dragged = False
        self._compact_position = None
        self._compact_view_key = None
        self._render_error = None
        self._compact_style_key = None
        self._compact_font_key = None
        self._compact_geometry = None
        self._corner_key = None
        self._corner_dpi = 96
        self._corner_syncing = False
        self._corner_timer = None
        self._drag_from_dock = False
        self._drag_timer = None
        self._drag_capture_timer = None
        self._drag_context = None
        self._drag_cursor = None
        self._drag_native_capture = None
        self._drag_native_move = None
        self._taskbar_hwnd = None
        self._mouse_down = False
        self._start = None
        self._status_press = None
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
        self._root_frame = self.frame
        self.frame.pack()
        self.root.bind("<Destroy>", lambda event: self.tooltip.hide()
                       if event.widget == self.root else None)
        self.root.bind("<Button-3>", lambda event: self._menu(event, None))
        self.root.bind("<ButtonPress-1>", self._press)
        self.root.bind("<B1-Motion>", self._drag)
        self.root.bind("<ButtonRelease-1>", self._release)
        self.root.bind("<Unmap>", self._drag_unmapped)
        self.root.bind("<Escape>", lambda event: self._collapse_panel())
        self.root.bind("<Return>", self._keyboard_toggle_panel)
        self.root.bind("<space>", self._keyboard_toggle_panel)
        self.root.bind("<Destroy>", self._destroyed, add="+")
        self.root.bind("<Configure>", self._sync_window_corners, add="+")
        self._root_frame.bind("<Configure>", self._sync_window_corners, add="+")
        self.root.bind("<Map>", lambda event: self._sync_window_corners(event, force=True), add="+")

    def _corner_parameters(self):
        floating = self.mode == "taskbar" and not self.docked
        radius = max(1, round(10 * self._corner_dpi / 96)) if floating else 0
        border = max(1, round(self._corner_dpi / 96)) if floating else 0
        return (self.root.winfo_width(), self.root.winfo_height(), radius,
                self._root_frame.winfo_width(), self._root_frame.winfo_height(), border)

    def _sync_window_corners(self, event=None, *, force=False):
        if (sys.platform != "win32" or self._corner_syncing
                or event is not None and event.widget not in (self.root, self._root_frame)):
            return
        if self._corner_timer is not None:
            self.root.after_cancel(self._corner_timer)
            self._corner_timer = None
        if force:
            self._corner_key = None
        key = self._corner_parameters()
        if key == self._corner_key:
            return  # Configure also fires on moves: no native queries or new regions.
        # SetWindowRgn sends WM_WINDOWPOSCHANGED. Let Tk finish a pending geometry
        # update first so the old native rectangle cannot overwrite the new one.
        self._corner_timer = self.root.after_idle(self._apply_window_corners)

    def _apply_window_corners(self):
        self._corner_timer = None
        key = self._corner_parameters()
        if key == self._corner_key:
            return
        width, height, radius, frame_width, frame_height, border = key
        self._corner_syncing = True
        try:
            outer = _set_window_corners(self.root, width, height, radius)
            inner = _set_window_corners(self._root_frame, frame_width, frame_height,
                                        max(0, radius - border), wrapper=False)
            if outer and inner:
                self._corner_key = key
        finally:
            self._corner_syncing = False

    def _destroyed(self, event):
        if event.widget == self.root:
            for name in ("_outside_timer", "_refresh_timer", "_zorder_timer",
                         "_drag_timer", "_drag_capture_timer", "_corner_timer"):
                timer = getattr(self, name)
                if timer is not None:
                    self.root.after_cancel(timer)
                    setattr(self, name, None)
            self._start = None
            self._dragging = False

    def _sync_zorder_timer(self):
        if self.mode == "taskbar" and sys.platform == "win32":
            if self._zorder_timer is None:
                self._watch_zorder()
        elif self._zorder_timer is not None:
            self.root.after_cancel(self._zorder_timer)
            self._zorder_timer = None
        if self.mode != "taskbar" and self._fullscreen_hidden:
            show_widget_without_activation(self.root, True)
            self._fullscreen_hidden = False

    def _watch_zorder(self):
        self._zorder_timer = None
        hidden = widget_fullscreen_covered(self.root)
        if hidden != self._fullscreen_hidden:
            if hidden:
                self._collapse_panel()
                self._close_menu()
                self.tooltip.hide()
            show_widget_without_activation(self.root, not hidden)
            self._fullscreen_hidden = hidden
        if not hidden:
            keep_above_taskbar(self.root, self._taskbar_hwnd)
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
            self.tooltip.hide()
            self._collapse_panel()
            self.mode = prefs["mode"]
            self.anchor = None
            self._compact_view_key = self._compact_style_key = self._compact_font_key = None
        if not self._dragging:
            self.docked = prefs["docked"]
        self._sync_zorder_timer()
        if self.mode == "panel":
            self._root_frame.pack_forget()
            if self._fixed_viewport is None:
                self._fixed_viewport = PanelViewport(self, self.root)
            self._fixed_viewport.container.pack(fill="both", expand=True)
            self.frame = self._fixed_viewport.content
            self.root.attributes("-alpha", 0.93)
            self.root.configure(bg=self.BG)
            self.frame.configure(bg=self.BG, padx=10, pady=6, highlightthickness=1)
            self._render_panel(rows)
        else:
            if self._fixed_viewport is not None:
                self._fixed_viewport.container.pack_forget()
            self.frame = self._root_frame
            self.frame.pack(fill="both", expand=True)
            self._render_compact(rows, prefs)
            if self.panel is not None:
                original = self.frame
                self.frame = self.panel_frame
                try:
                    self._render_panel(rows)
                finally:
                    self.frame = original
        self._sync_window_corners()

    def _render_compact(self, rows, prefs):
        self.names = {row["root"]: row["name"] for row in rows}
        self.unread_counts = {row["root"]: len(row["unread"]) for row in rows}
        shown, _ = arrange(rows, {**prefs, "max_rows": 0})
        self.order = [row["root"] for row in shown]
        position = self._compact_position if self._dragging else (prefs["compact_x"], prefs["compact_y"])
        px, py = position or (None, None)
        if self._dragging and self._drag_context is not None:
            info, area, dpi = self._drag_context
            work_area = self._drag_work_areas[area]
            virtual = self._drag_virtual
            point = (px, py)
        else:
            primary = taskbar_info()
            virtual = _virtual_area(self.root)
            point = (px if px is not None else primary["rect"][0] if primary else virtual[0],
                     py if py is not None else primary["rect"][1] if primary else virtual[1])
            point = clamp_compact(*point, 1, 1, virtual)[:2]
            info = primary if px is None and py is None else taskbar_info(point)
            area = _monitor_area(self.root, *point, full=True)
            work_area = _monitor_area(self.root, *point)
            dpi = _window_dpi(self.root)
        work_area = compact_work_area(area, info, work_area)
        self._corner_dpi = dpi
        self._taskbar_hwnd = info.get("hwnd") if info else None
        excluded = None
        if self.root.winfo_ismapped():
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            excluded = (x, y, x + self.root.winfo_width(), y + self.root.winfo_height())
        key = ((tuple(info["rect"]), info["edge"], info["auto_hide"]) if info else None, dpi, excluded)
        now = time.monotonic()
        if (self.docked and not self._dragging and not self._fullscreen_hidden
                and not widget_fullscreen_covered(self.root, point)
                and (key != self._palette_key or now - self._palette_at >= 30)):
            samples = [rgb for rgb in taskbar_colors(info, dpi, excluded) if rgb != (0, 0, 0)]
            if samples:
                self._compact_palette = compact_palette(samples)
            self._palette_key, self._palette_at = key, now
        palette = self._compact_palette if self.docked else {
            "bg": self.BG, "fg": self.FG, "dim": self.DIM, "green": self.GREEN, "yellow": self.YELLOW}
        bg = palette["bg"]
        padding, gap = max(1, round(7 * dpi / 96)), max(1, round(3 * dpi / 96))
        border = 0 if self.docked else max(1, round(dpi / 96))
        # Negative Tk font sizes are pixels; derive them from this window's actual DPI.
        pixels = max(6, round(9 * dpi / 72))
        available = None
        if self.docked and info and info["edge"] in (1, 3) and not info["auto_hide"]:
            _, dock_top, _, dock_bottom = compact_taskbar_rect(info, dpi)
            available = dock_bottom - dock_top
        font_key = (dpi, available)
        if font_key != self._compact_font_key:
            self.compact_metrics.configure(size=-pixels)
            self._compact_font_key = font_key
        if available is not None:
            pixels = abs(int(self.compact_metrics.cget("size")))
            while self.compact_metrics.metrics("linespace") * 2 > available and pixels > 5:
                pixels -= 1
                self.compact_metrics.configure(size=-pixels)
        measure = self.compact_metrics.measure
        name_width = measure("프로젝트 이름 프로젝트")
        lines = compact_lines(rows, prefs, name_width, measure)
        view_key = (self.docked, dpi, self.compact_metrics.cget("size"), tuple(palette.items()),
                    tuple((line["row"]["root"], line["name"], line["running"], line["unread"], line["more"],
                           line["overflow_unread"],
                           read_error_text(line["row"]),
                           bool(conversation_targets(line["row"], "running")),
                           bool(conversation_targets(line["row"], "unread"))) for line in lines))
        if view_key != self._compact_view_key:
            self._render_compact_items(lines, palette, padding, gap, border)
            self.root.update_idletasks()
            self._compact_view_key = view_key
        width = max(self.frame.winfo_reqwidth(), name_width + sum(measure(text) for text in
                    ("● 99", "✉ 99", "+99")) + 3 * gap + 2 * padding) + 2 * border
        self._compact_free_height = max(self.frame.winfo_reqheight(),
                                        self.compact_metrics.metrics("linespace") * 2 + 6) + 2 * border
        if self.docked and info and info["edge"] in (1, 3) and not info["auto_hide"]:
            geometry = compact_geometry(info, area, width, self._compact_free_height, px, dpi=dpi)
        else:
            # Work-area margins choose an initial position only. Preserve an
            # intentional drag over the taskbar, including after refresh/restart.
            default = floating_geometry(work_area, width, self._compact_free_height, dpi=dpi)
            geometry = clamp_compact(px if px is not None else default[0],
                                     py if py is not None else default[1],
                                     width, self._compact_free_height, area)
        self._compact_position = geometry[:2]
        self._compact_geometry = geometry
        actual = (self.root.winfo_x(), self.root.winfo_y(), self.root.winfo_width(), self.root.winfo_height())
        if actual != geometry:
            x, y, width, height = geometry
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        if self._dragging:
            self._drag_width = geometry[2]
            self._drag_height = self._compact_free_height
            self._drag_applied_geometry = geometry
        self._sync_window_corners()

    def _compact_targets(self, project, kind):
        row = next((row for row in self._rows if row["root"] == project), None)
        return conversation_targets(row, kind) if row is not None else []

    def _render_compact_items(self, lines, palette, padding, gap, border):
        with self.tooltip.preserve_on_rebuild(self.frame):
            bg = palette["bg"]
            style_key = (self.docked, bg, padding, border)
            if style_key != self._compact_style_key:
                if self.root.attributes("-alpha") != 1.0:
                    self.root.attributes("-alpha", 1.0)
                root_bg = self.BORDER if border else bg
                if self.root.cget("bg") != root_bg:
                    self.root.configure(bg=root_bg)
                self.frame.configure(bg=bg, padx=padding, pady=0 if self.docked else 2,
                                     highlightthickness=0,
                                     highlightbackground=DarkMenu.HOVER)
                self.frame.pack_configure(fill="both", expand=True, padx=border, pady=border)
                self._compact_style_key = style_key
            for child in self.frame.winfo_children():
                child.destroy()
            if not lines:
                self.tk.Label(self.frame, text="handback · 대기", bg=bg, fg=palette["dim"],
                              font=self.compact_metrics, bd=0, padx=0).pack(anchor="w", expand=True)
            for line in lines:
                row = self.tk.Frame(self.frame, bg=bg)
                row.pack(fill="x", expand=True)
                error = read_error_text(line["row"])
                for key, color in (("name", palette["fg"]),
                                   ("running", palette["yellow"] if error else
                                    palette["green"] if line["row"]["running"] else palette["dim"]),
                                   ("unread", palette["yellow"]),
                                   ("more", palette["yellow"] if line["overflow_unread"] else palette["dim"])):
                    label = self.tk.Label(row, text=line[key], bg=bg, fg=color,
                                          font=self.compact_metrics, bd=0, padx=0, pady=0)
                    label.pack(side="left", padx=(0, gap if key != "more" else 0))
                    label.bind("<Button-3>", lambda event, project=line["row"]["root"]: self._menu(event, project))
                    if key == "more" and line["more"]:
                        self.tooltip.bind(label, f"다른 프로젝트의 미확인 결과 {line['overflow_unread']}개"
                                          if line["overflow_unread"] else "다른 프로젝트 보기",
                                          key=("compact-overflow", line["row"]["root"]))
                    if key == "running" and error:
                        self.tooltip.bind(label, error, key=("compact-error", line["row"]["root"]))
                        continue
                    if key in ("running", "unread"):
                        targets = conversation_targets(line["row"], key)
                        if targets:
                            label.configure(cursor="hand2")
                            label.bind("<Button-1>", lambda event, project=line["row"]["root"], kind=key:
                                       self._press_status(event, project, kind))

    def _collapse_panel(self):
        if self._outside_timer is not None:
            self.root.after_cancel(self._outside_timer)
            self._outside_timer = None
        if self.panel is not None:
            self._close_menu()
            self.panel.destroy()
            self.panel = self.panel_frame = None
            self._panel_viewport = None

    def _keyboard_toggle_panel(self, event):
        self._toggle_panel()
        return "break"

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
        self._panel_viewport = PanelViewport(self, panel)
        self._panel_viewport.container.pack(fill="both", expand=True)
        self.panel_frame = self._panel_viewport.content
        panel.bind("<Escape>", lambda event: self._collapse_panel())
        panel.bind("<Return>", self._keyboard_toggle_panel)
        panel.bind("<space>", self._keyboard_toggle_panel)
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
        prefs = load_prefs(self.home)
        if self._max_rows != prefs["max_rows"]:
            self.show_all = False
            self._max_rows = prefs["max_rows"]
        view_key = (json.dumps(rows, sort_keys=True, ensure_ascii=False),
                    tuple(ago(row["last_activity"]) for row in rows),
                    tuple(prefs["order"]), tuple(prefs["hidden"]), prefs["max_rows"],
                    self.show_all, frozenset(self.expanded), self.widths, self.row_height)
        if getattr(self.frame, "_panel_view_key", None) == view_key:
            self._place()
            return
        with self.tooltip.preserve_on_rebuild(self.frame):
            for child in self.frame.winfo_children():
                child.destroy()
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
                parts = ([("프로젝트 확인 불가", False)] if data.get("unresolved") else
                         [(data["lead"], True), ("→", False)])
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
                error = read_error_text(data)
                for column, text, color in (
                        (2, f"읽기 오류 {data['unreadable']}" if error else f"● 진행 {running}" if running else "대기",
                         self.YELLOW if error else self.GREEN if running else self.DIM),
                        (3, f"✉ {unread}" if unread else "", self.YELLOW),
                        (4, ago(data["last_activity"]), self.DIM)):
                    label = self.tk.Label(cells[column], text=truncate_text(text, self.widths[column] - 4,
                                          self.metrics.measure), bg=self.BG, fg=color, font=self.font,
                                          anchor="e" if column == 4 else "w")
                    label.pack(fill="both", expand=True)
                self._hover(row, key)
                self.tooltip.bind(name, data["name"], key=("name", key))
                for column, kind in ((2, "running"), (3, "unread")):
                    if kind == "running" and error:
                        self.tooltip.bind(cells[column].winfo_children()[0], error, key=("error", key))
                        continue
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
                    self.tooltip.bind(cell, tip, key=("status", key, kind,
                                      tuple(target["handle"] for target in targets)))
                line += 1
                if key in self.expanded:
                    details = [("● 진행 중 · " + item["name"], self.GREEN, None,
                                ("running", item.get("id"), item.get("handle"))) for item in data["running"]]
                    if error:
                        details.insert(0, (error.replace("\n", " · "), self.YELLOW, None, ("error",)))
                    for index, item in enumerate(data["unread"][:DETAIL_LIMIT]):
                        when = _local_time(item["created_utc"])
                        details.append((f"✉ {when} · {item['from']} · {item['body']}",
                                        self.YELLOW if item["kind"] == "result" else "#f28b82", index,
                                        ("unread", item.get("id"), item.get("sender"), item.get("created_utc"))))
                    if len(data["unread"]) > DETAIL_LIMIT:
                        details.append((f"… 전체 {len(data['unread'])}개 보기", self.DIM, 0, ("all",)))
                    box = self.tk.Frame(self.frame, bg=self.BG)
                    box.grid(row=line, column=0, sticky="ew", padx=(9, 0), pady=(0, 3))
                    self.tk.Frame(box, bg=DarkMenu.HOVER, width=2).pack(side="left", fill="y", padx=(0, 8))
                    body = self.tk.Frame(box, bg=self.BG)
                    body.pack(side="left", fill="both", expand=True)
                    for text, color, index, identity in details or [("세부 항목 없음", self.DIM, None, ("empty",))]:
                        label = self.tk.Label(body, text=truncate_text(text, min(520, sum(self.widths) - 32),
                                              self.metrics.measure), bg=self.BG, fg=color, font=self.font,
                                              anchor="w", cursor="hand2" if index is not None else "")
                        label.pack(fill="x")
                        if index is not None:
                            self._hover(label, key)
                            label.bind("<Button-1>", lambda event, data=data, index=index: self._open(data, index))
                        self.tooltip.bind(label, text, key=("detail", key, identity))
                    line += 1
            if overflow or (self.show_all and hidden):
                busy = any(row["running"] or row["unread"] or row.get("unreadable") for row in overflow)
                label = self._label(f"+{len(overflow)}개 더 보기" if overflow else "접기", line, 0,
                                    fg=self.YELLOW if busy else self.DIM, padx=(0, 0))
                label.configure(cursor="hand2")
                self._hover(label)
                label.bind("<Button-1>", lambda event: self._toggle_overflow())
            self.frame._panel_view_key = view_key
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
                self._dialog("대화 열기 실패", ui_error(error))

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
                {"label": "handback 등록 해제…", "enabled": not data.get("unresolved", False),
                 "tooltip": "프로젝트 경로를 읽을 수 없어 해제할 수 없습니다" if data.get("unresolved") else "",
                 "command": lambda: self._release_project(project)}, None])
            if execution:
                items.extend([{"label": "모델·추론 설정 ▶", "children": execution}, None])
        settings = [{"label": "펼친 패널 표시 줄 수", "enabled": False}]
        for count, label in ((2, "2줄"), (3, "3줄"), (0, "전부")):
            settings.append({"label": ("✓ " if prefs["max_rows"] == count else "    ") + label,
                             "command": lambda count=count: self._set_max_rows(prefs, count)})
        settings.extend([None, {"label": "표시 방식", "enabled": False}])
        info = taskbar_info((self.root.winfo_x(), self.root.winfo_y()))
        can_dock = bool(info and info["edge"] in (1, 3) and not info["auto_hide"])
        for mode, docked, label in (("taskbar", False, "작은 위젯"),
                                    ("taskbar", True, "작업표시줄에 넣기"),
                                    ("panel", prefs["docked"], "펼친 패널 고정")):
            selected = prefs["mode"] == mode and (mode == "panel" or prefs["docked"] == docked)
            settings.append({"label": ("✓ " if selected else "    ") + label,
                             "enabled": mode != "taskbar" or not docked or can_dock,
                             "tooltip": "위쪽·아래쪽의 항상 표시된 작업표시줄에서 사용할 수 있습니다"
                                        if mode == "taskbar" and docked and not can_dock else "",
                             "command": lambda mode=mode, docked=docked: self._save(
                                 {**prefs, "mode": mode, "docked": docked})})
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
            self._dialog("설정 읽기 실패", ui_error(error))
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
        failed_values = None

        def fit():
            window.update_idletasks()
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            x, y, width, height = popup_position(x, y, window.winfo_reqwidth(), window.winfo_reqheight(),
                                               _monitor_area(self.root, x, y))
            window.geometry(f"{width}x{height}+{x}+{y}")

        def clear_error(event):
            requested = {key: entry.get().strip() or None for key, entry in entries.items()}
            if error_text.get() and requested != failed_values:
                error_text.set("")
                fit()

        for entry in entries.values():
            entry.bind("<KeyRelease>", clear_error)

        def save():
            nonlocal failed_values
            requested = {key: entry.get().strip() or None for key, entry in entries.items()}
            try:
                config.configure_execution(project, agent, role, home=self.home, **requested)
            except (OSError, ValueError, TimeoutError) as error:
                failed_values = requested
                error_text.set(ui_error(error))
                fit()
                return
            window.destroy()
            self.refresh(reschedule=False)

        self._button(footer, "저장", save).pack(side="right", padx=(8, 0))
        self._button(footer, "취소", window.destroy).pack(side="right")
        window.bind("<Escape>", lambda event: window.destroy())
        window.bind("<Return>", lambda event: save())
        fit()
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
            self._dialog("정리 실패", ui_error(error), parent=parent or self.root)
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
            self._dialog("해제 실패", ui_error(error), parent=self.root)
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
            x, y = self.root.winfo_x(), self.root.winfo_y()
            area = _monitor_area(self.root, x, y)
            width, height = self._panel_viewport.dimensions(area[2] - area[0], area[3] - area[1])
            x, y, width, height = compact_panel_geometry(
                (x, y, self.root.winfo_width(), self.root.winfo_height()), width, height, area)
            self._panel_viewport.layout(width, height)
            self.panel.geometry(f"{width}x{height}+{x}+{y}")
            return
        if self.mode == "panel" and self._fixed_viewport is not None:
            point = ((self.anchor[0] - 1, self.anchor[1] - 1) if self.anchor is not None
                     else (self.root.winfo_x(), self.root.winfo_y()))
            area = _monitor_area(self.root, *point)
            if self.anchor is None:
                self.anchor = (area[2] - 8, area[3] - 4)
            width, height = self._fixed_viewport.dimensions(area[2] - area[0], area[3] - area[1])
            x, y, width, height = clamp_compact(self.anchor[0] - width, self.anchor[1] - height,
                                               width, height, area)
            self._fixed_viewport.layout(width, height)
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            return
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        if self.anchor is None:
            area = _work_area() or (self.root.winfo_screenwidth(), self.root.winfo_screenheight() - 48)
            self.anchor = (area[0] - 8, area[1] - 4)
        right, bottom = self.anchor
        self.root.geometry(f"{width}x{height}+{right - width}+{bottom - height}")

    def _press_status(self, event, project, kind):
        self._press(event)
        self._status_press = (project, kind)
        return "break"

    def _press(self, event):
        if self._start is not None:
            self._finish_drag()
        self._status_press = None
        cursor = self.root.winfo_pointerxy()
        self._start = (*cursor, self.root.winfo_x(), self.root.winfo_y())
        self._dragged = False
        self._drag_from_dock = self.docked
        self._drag_cursor = cursor
        self._drag_monitors = _monitor_rects(self.root)
        self._drag_virtual = (min(r[0] for r in self._drag_monitors),
                              min(r[1] for r in self._drag_monitors),
                              max(r[2] for r in self._drag_monitors),
                              max(r[3] for r in self._drag_monitors))
        self._drag_contexts = {}
        self._drag_work_areas = {}
        self._drag_context = None
        self._set_drag_context(cursor)
        self._drag_threshold = drag_threshold(self._drag_context[2])
        self._drag_width = self.root.winfo_width()
        self._drag_height = (self._compact_free_height if self.mode == "taskbar"
                             else self.root.winfo_height())
        self._drag_prefs = load_prefs(self.home)
        self._drag_native_move = _native_drag_mover(self.root)
        self._drag_applied_geometry = (self.root.winfo_x(), self.root.winfo_y(),
                                       self.root.winfo_width(), self.root.winfo_height())

    def _set_drag_context(self, cursor):
        area = monitor_at_point(self._drag_monitors, cursor)
        if area not in self._drag_contexts:
            # Only a monitor crossing can add a new taskbar/DPI query during a drag.
            self._drag_contexts[area] = (taskbar_info(cursor), area, _window_dpi(self.root))
            self._drag_work_areas[area] = _monitor_area(self.root, *cursor)
        self._drag_context = self._drag_contexts[area]
        self._drag_snap_distance = max(1, round(20 * self._drag_context[2] / 96))

    def _drag(self, event):
        if self._start is None:
            return
        cursor = self._drag_cursor = self.root.winfo_pointerxy()
        if not self._dragged:
            x0, y0, _, _ = self._start
            if max(abs(cursor[0]-x0), abs(cursor[1]-y0)) < self._drag_threshold:
                return
            self._dragging = self._dragged = True
            self._drag_native_capture = _pointer_capture(self.root)
            self._collapse_panel()
            self._close_menu()
            self.tooltip.hide()
            if self.mode == "taskbar":
                keep_above_taskbar(self.root, self._taskbar_hwnd)
            self._drag_capture_timer = self.root.after(50, self._watch_drag_capture)
        # One idle callback consumes the latest cursor, however many events arrived.
        if self._drag_timer is None:
            self._drag_timer = self.root.after_idle(self._flush_drag)
        return "break"

    def _flush_drag(self, read_pointer=True):
        self._drag_timer = None
        if self._start is None or not self._dragged:
            return
        cursor = self.root.winfo_pointerxy() if read_pointer else self._drag_cursor
        self._drag_cursor = cursor
        old_dpi = self._drag_context[2]
        self._set_drag_context(cursor)
        info, area, dpi = self._drag_context
        x, y = drag_position(self._start, cursor)
        if self.mode == "taskbar":
            docked, geometry = compact_drag_geometry(info, area, self._drag_virtual,
                x, y, self._drag_width, self._drag_height, dpi,
                from_dock=self._drag_from_dock, snap_distance=self._drag_snap_distance,
                work_area=self._drag_work_areas[area])
            changed = docked != self.docked
            self.docked = docked
            if not docked:
                self._drag_from_dock = False
            self._compact_position = geometry[:2]
            self._taskbar_hwnd = info.get("hwnd") if info else None
            if changed or dpi != old_dpi:
                self._palette_key = None
                self._render_compact(self._rows, self._drag_prefs)
            else:
                self._compact_geometry = geometry
                self._move_drag_window(geometry)
            return
        self._move_drag_window(clamp_compact(x, y, self._drag_width, self._drag_height, area))

    def _move_drag_window(self, geometry):
        if geometry == self._drag_applied_geometry:
            return
        x, y, width, height = geometry
        if (self._drag_native_move is None
                or (width, height) != (self.root.winfo_width(), self.root.winfo_height())
                or not self._drag_native_move(x, y)):
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        self._drag_applied_geometry = geometry

    def _watch_drag_capture(self):
        self._drag_capture_timer = None
        if not self._dragging:
            return
        if not _pointer_capture_owned(self.root, self._drag_native_capture):
            self._finish_drag()
            return
        self._drag_capture_timer = self.root.after(50, self._watch_drag_capture)

    def _drag_unmapped(self, event):
        if event.widget == self.root and self._start is not None:
            self._finish_drag()

    def _finish_drag(self, click=False, read_pointer=False, event=None):
        if self._start is None:
            return
        for name in ("_drag_timer", "_drag_capture_timer"):
            timer = getattr(self, name)
            if timer is not None:
                self.root.after_cancel(timer)
                setattr(self, name, None)
        drop_geometry = None
        if self._dragged:
            # Flush before saving, including a release ahead of the pending idle move.
            self._flush_drag(read_pointer=read_pointer)
            if click and self.mode == "taskbar":
                info, area, dpi = self._drag_context
                docked, geometry = compact_drop_geometry(
                    info, area, *self._compact_geometry, cursor=self._drag_cursor, dpi=dpi)
                if docked:
                    self.docked = True
                    self._palette_key = None
                    drop_geometry = geometry
        status_press, self._status_press = self._status_press, None
        self._start = None
        self._dragging = False
        self._drag_context = None
        if self.root.grab_current() == self.root:
            self.root.grab_release()
        self._drag_native_capture = None
        if self._dragged:
            if drop_geometry is not None:
                self._render_compact(self._rows, {
                    **self._drag_prefs, "compact_x": drop_geometry[0],
                    "compact_y": drop_geometry[1], "docked": True})
            self.root.update_idletasks()
            if self.mode == "taskbar":
                prefs = load_prefs(self.home)
                save_prefs({**prefs, "compact_x": self.root.winfo_x(),
                            "compact_y": self.root.winfo_y(), "docked": self.docked}, self.home)
            else:
                self.anchor = (self.root.winfo_x() + self.root.winfo_width(),
                               self.root.winfo_y() + self.root.winfo_height())
        elif click and self.mode == "taskbar":
            if status_press is not None:
                self._conversations(event, self._compact_targets(*status_press))
            else:
                self._toggle_panel()

    def _release(self, event):
        if self._start is not None:
            # A final pointer move can arrive before its queued motion event.
            self._drag(event)
            self._finish_drag(click=True, read_pointer=True, event=event)
            return "break" if self._dragged else None
        if self.mode == "taskbar":
            return
        # Keep the bottom-right corner fixed so expanding grows upward and left.
        self.anchor = (self.root.winfo_x() + self.root.winfo_width(),
                       self.root.winfo_y() + self.root.winfo_height())

    def refresh(self, reschedule=True):
        if reschedule and self._refresh_timer is not None:
            self.root.after_cancel(self._refresh_timer)
            self._refresh_timer = None
        try:
            if self._render_error is not None:
                self._compact_view_key = None
            self.render(snapshot(self.home))
            self._render_error = None
        except Exception as error:  # Keep the strip alive through partial writes.
            self._compact_view_key = None
            message = "읽기 오류: " + ui_error(error)[:60]
            if message != self._render_error:
                self.tooltip.hide_within(self.frame)
                self.frame._panel_view_key = None
                for child in self.frame.winfo_children():
                    child.destroy()
                self._label(message, 0, 0, fg=self.YELLOW)
            self._render_error = message
        if reschedule:
            self._refresh_timer = self.root.after(REFRESH_MS, self.refresh)

    def run(self):
        self.refresh()
        self.root.deiconify()
        if self._fullscreen_hidden:
            show_widget_without_activation(self.root, False)
        self.root.mainloop()


def startup_shortcut():
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/handback 현황판.lnk"


def _write_startup_shortcut(link, target, arguments, working_directory):
    def quoted(value):
        return "'" + str(value).replace("'", "''") + "'"
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + quoted(link) + ");"
              "$s.TargetPath = " + quoted(target) + ";"
              "$s.Arguments = " + quoted(arguments) + ";"
              "$s.WorkingDirectory = " + quoted(working_directory) + ";"
              "$s.Description = 'handback 현황판';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, capture_output=True, timeout=30)


def set_autostart(enabled):
    """Add or remove a per-user Startup shortcut that runs dashboard.pyw."""
    if sys.platform != "win32":
        raise RuntimeError("자동 실행은 Windows에서만 지원합니다")
    link = startup_shortcut()
    if not enabled:
        link.unlink(missing_ok=True)
        return {"autostart": False, "shortcut": str(link)}
    from .invocation import entry_args, frozen
    if frozen():
        # Windowed sibling of handback.exe; no interpreter, launcher script or arguments.
        target = Path(sys.executable).with_name("handback-dashboard.exe")
        if not target.exists():
            raise RuntimeError(f"handback-dashboard.exe를 찾을 수 없습니다: {target}")
        _write_startup_shortcut(link, target, "", target.parent)
        return {"autostart": True, "shortcut": str(link), "target": str(target), "launcher": str(target)}
    launcher = Path(__file__).resolve().parent.parent / "dashboard.pyw"
    arguments = subprocess.list2cmdline([str(launcher)] if launcher.is_file() else [*entry_args(), "dashboard"])
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        raise RuntimeError(f"pythonw.exe를 찾을 수 없습니다: {pythonw}")
    _write_startup_shortcut(link, pythonw, arguments, launcher.parent)
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
