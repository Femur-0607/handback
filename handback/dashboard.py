"""Compact floating status widget with optional taskbar and expanded views.

It never routes or changes agent combinations. Display preferences live in
dashboard.json; project actions acknowledge results, move state aside, or save
explicitly selected model and reasoning defaults.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import queue
from importlib import resources
import re
import webbrowser
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from . import __version__, compat, config, dock_motion, envelope, inbox, updater
from .collector import OPEN_STATES
from .tray import Tray
from .winevents import (ForegroundWatcher, foreground_covers, fullscreen_covers_widget,  # noqa: F401
                        native_user32)
from .state import _read_json, atomic_json, home_lock, state_home


AGENT_NAME = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity"}
# One source of truth for agent colours: dark backgrounds (panel, floating
# widget, dark taskbar) and darker variants for light docked taskbars.
AGENT_COLORS_DARK = {"claude": "#d97757", "codex": "#9fb3c8", "antigravity": "#5b9bf8"}
AGENT_COLORS_LIGHT = {"claude": "#b45309", "codex": "#4b5f73", "antigravity": "#1d4ed8"}
# Window themes for every dashboard surface; a docked widget follows the
# sampled taskbar instead (compact_palette). Each value is unique within its
# theme so open windows can be recoloured by mapping old colours to new ones.
THEMES = {
    "dark": {"bg": "#1f1f1f", "fg": "#e6e6e6", "dim": "#8a8a8a", "border": "#6a6a70",
             "green": "#5fd38a", "yellow": "#f2c94c", "red": "#f28b82", "surface": "#2b2b2b",
             "hover": "#3a3a3a", "disabled": "#6b6b6b", "field": "#555555", "agents": AGENT_COLORS_DARK},
    "light": {"bg": "#f3f3f3", "fg": "#202020", "dim": "#595959", "border": "#9a9aa2",
              "green": "#126b35", "yellow": "#805000", "red": "#b3261e", "surface": "#ffffff",
              "hover": "#e1e1e1", "disabled": "#a6a6a6", "field": "#b8b8b8", "agents": AGENT_COLORS_LIGHT},
}
# Theme key -> Strip attribute (BG, FG, …), in Strip's order.
THEME_KEYS = ("bg", "fg", "dim", "border", "green", "yellow", "red", "surface", "hover", "disabled", "field")
THEME_CHOICES = ("dark", "light", "system")
OPACITY_CHOICES = (100, 90, 80, 70, 60)
AGENT_DOT = "●"
REFRESH_MS = 3000
# Cheap native checks; a short tick keeps full-screen hiding prompt.
TASKBAR_ZORDER_MS = 100
# One taskbar colour readback costs a few ms: sample every second, and more
# often for a moment after the foreground window changes.
PALETTE_SECONDS = 1.0
PALETTE_BURST_SECONDS = 0.2
PALETTE_BURST_LENGTH = 2.0
TRAY_POLL_MS = 150
SHOW_POLL_MS = 300
DASHBOARD_MUTEX = "Local\\handback-dashboard"
DASHBOARD_SHOW_EVENT = "Local\\handback-dashboard-show"
DETAIL_LIMIT = 5
DEFAULT_PREFS = {"max_rows": 3, "order": [], "hidden": [], "mode": "taskbar",
                 "compact_x": None, "compact_y": None, "panel_x": None, "panel_y": None,
                 "docked": False, "update_check": True,
                 # Options window: ✉ click behaviour, idle hiding, attention effects.
                 "ack_on_click": True, "auto_hide": False, "highlight_unread": True,
                 "animate_unread": True, "animate_running": True,
                 # Appearance: window opacity (%), colour theme, grab bar on the floating widget.
                 "opacity": 100, "theme": "dark", "drag_handle": True}
# Attention effects: one shared animation tick, a short pulse for new results.
ANIMATION_MS = 60
PULSE_SECONDS = 6.0
UPDATE_FIRST_MS = 60_000
# App updates (Codex/Claude/Antigravity): first look soon after start, then every 10 minutes.
COMPAT_FIRST_MS = 20_000
COMPAT_INTERVAL_MS = 600_000


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


def agent_key(handle):
    """Known agent id of a "agent:name" handle, or "" (rendered with a dim dot)."""
    agent = str(handle or "").partition(":")[0]
    return agent if agent in AGENT_COLORS_DARK else ""


def agent_color(handle, palette):
    """Dot colour from a compact palette's "agent_<id>" entries; dim when unknown."""
    agent = agent_key(handle)
    return palette.get("agent_" + agent, palette["dim"]) if agent else palette["dim"]


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


def unread_tip(data, ack):
    """Tooltip for a clickable ✉ count: where the click goes and whether it clears."""
    targets = conversation_targets(data, "unread")
    linked = [target for target in targets if conversation_link(target["handle"])]
    where = (agent_name(linked[0]["handle"]) + " 대화 열기" if len(linked) == 1 and len(targets) == 1
             else "워커 대화 선택" if linked else "결과 보기")
    return where + (" · 클릭하면 확인 처리" if ack else "")


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


def acknowledge_ids(root, ids, home=None):
    """ACK only the pending results/errors whose id is in ids (what a viewer showed)."""
    wanted = set(ids)
    home = Path(home or state_home())
    with home_lock(home):
        folder = _folder_for(root, home)
        done = 0
        for mail in inbox.pending(folder / "inbox"):
            if mail.get("kind") != "request" and mail["id"] in wanted:
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
            raise ValueError(f"진행 중인 요청 {running}개가 있어 해제할 수 없습니다. "
                             "멈춘 요청은 프로젝트 메뉴의 '요청 닫기'로 먼저 정리하세요")
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        target = home / "released" / f"{folder.name}-{stamp}"
        target.parent.mkdir(parents=True, exist_ok=True)
        folder.rename(target)
    prefs = load_prefs(home)
    save_prefs({**prefs, "order": [r for r in prefs["order"] if r != root],
                "hidden": [r for r in prefs["hidden"] if r != root]}, home)
    return target


def close_request(root, request_id, home=None):
    """Close one open request by hand (failed + closed); a later result is not collected.

    For requests stuck open, e.g. a delivery whose outcome was never learned.
    Returns False when it was no longer open.
    """
    from .state import ProjectState
    state = ProjectState(root, home=home)
    with state.lock():
        request = state.load_request(request_id)
        if request is None:
            raise ValueError("요청 기록을 찾을 수 없습니다")
        if request.get("status") not in OPEN_STATES:
            return False
        request["closed"] = {"utc": datetime.now(timezone.utc).isoformat(), "from": request["status"],
                             "by": "dashboard"}
        request["status"] = "failed"
        request["error"] = "Closed by hand from the dashboard; a later result is not collected"
        state.save_request(request)
    return True


REQUEST_STATES = {"prepared": "준비 중", "dispatching": "전달 중", "accepted": "작업 중",
                  "delivery_unknown": "전달 확인 안 됨"}


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
    for key in ("panel_x", "panel_y"):  # bottom-right anchor of the expanded panel
        if type(stored.get(key)) is int:
            prefs[key] = stored[key]
    if type(stored.get("docked")) is bool:
        prefs["docked"] = stored["docked"]
    for key in ("update_check", "ack_on_click", "auto_hide", "highlight_unread",
                "animate_unread", "animate_running", "drag_handle"):
        if type(stored.get(key)) is bool:
            prefs[key] = stored[key]
    if type(stored.get("opacity")) is int and stored["opacity"] in OPACITY_CHOICES:
        prefs["opacity"] = stored["opacity"]
    if stored.get("theme") in THEME_CHOICES:
        prefs["theme"] = stored["theme"]
    return prefs


def system_prefers_light():
    """Windows app mode (Settings > Personalization > Colors); dark when unknown."""
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 1
    except OSError:
        return False


def resolve_theme(choice):
    """The concrete theme ("dark"/"light") for a stored theme choice."""
    if choice == "system":
        return "light" if system_prefers_light() else "dark"
    return choice if choice in THEMES else "dark"


_COLOR_OPTIONS = ("background", "foreground", "activebackground", "activeforeground", "selectcolor",
                  "highlightbackground", "highlightcolor", "insertbackground")


def recolor(widget, mapping):
    """Swap theme colours in place across a widget tree, Text tags included."""
    import tkinter
    for option in _COLOR_OPTIONS:
        try:
            value = str(widget.cget(option)).lower()
        except tkinter.TclError:
            continue  # this widget class has no such option
        if value in mapping:
            widget.configure({option: mapping[value]})
    if isinstance(widget, tkinter.Text):
        for tag in widget.tag_names():
            value = str(widget.tag_cget(tag, "foreground")).lower()
            if value in mapping:
                widget.tag_configure(tag, foreground=mapping[value])
    for child in widget.winfo_children():
        recolor(child, mapping)


def blend(start, end, amount):
    """Mix two #rrggbb colours; amount 0 keeps start, 1 gives end."""
    a = [int(start[i:i+2], 16) for i in (1, 3, 5)]
    b = [int(end[i:i+2], 16) for i in (1, 3, 5)]
    amount = min(1.0, max(0.0, amount))
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * amount) for x, y in zip(a, b))


def is_idle(rows, prefs):
    """No visible project is running, waiting to be read, or failing to read."""
    return not any(row["running"] or row["unread"] or row.get("unreadable")
                   for row in rows if row["root"] not in prefs["hidden"])


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
    # The name column also holds the lead-agent dot ("● ") before "▸ name".
    columns = ((AGENT_DOT + " 프로젝트 이름 프로젝트", 26), ("Claude → Codex + Antigravity", 16),
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
        window.configure(bg=strip.HOVER)
        strip.tk.Label(window, text=text, bg=strip.SURFACE, fg=strip.FG,
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


def _utc_timestamp(text):
    try:
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


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
        # ABM_GETTASKBARPOS reports physical pixels even when Windows DPI-virtualizes
        # this process (the display scale changed after launch). GetWindowRect is
        # virtualized like Tk's own geometry, so it keeps every rectangle in one space.
        r = wintypes.RECT()
        if not (hwnd and user.GetWindowRect(hwnd, ctypes.byref(r))):
            r = data.rc
        return {"rect": (r.left, r.top, r.right, r.bottom), "edge": data.uEdge,
                "auto_hide": auto_hide, "notification_left": notification, "hwnd": hwnd}
    except (AttributeError, OSError):
        return None


def widget_fullscreen_covered(widget, point=None):
    """Compare foreground bounds with the full monitor, excluding our process."""
    if sys.platform != "win32":
        return False
    try:
        return foreground_covers(native_user32(), _root_hwnd(widget), point)
    except (AttributeError, OSError):
        return False


def _root_hwnd(widget):
    """Tk's native wrapper (GA_ROOT) for a mapped widget, or None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    try:
        user = ctypes.windll.user32
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        return user.GetAncestor(widget.winfo_id(), 2) or None
    except (AttributeError, OSError):
        return None


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
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongW.restype = ctypes.c_long
    hwnd = user.GetAncestor(widget.winfo_id(), 2)  # GA_ROOT: Tk's native wrapper.
    taskbar = taskbar or user.FindWindowW("Shell_TrayWnd", None)
    if not hwnd or not taskbar:
        return
    if not user.GetWindowLongW(hwnd, -20) & 0x8:  # GWL_EXSTYLE lacks WS_EX_TOPMOST
        # The topmost taskbar is above any normal window, and hundreds of windows can sit
        # between them, past the bounded walk below; restore our topmost band directly.
        user.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
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


APP_USER_MODEL_ID = "Femur0607.handback.dashboard"


def set_app_user_model_id(app_id=APP_USER_MODEL_ID):
    """Give the process its own taskbar identity (grouping + logo); failures are ignored."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
        return True
    except (AttributeError, OSError):
        return False


def tray_state_text(rows, hidden=()):
    """Short state shown in the tray tooltip, the same facts the strip shows."""
    shown = [row for row in rows if row["root"] not in hidden]
    if any(row.get("unreadable") for row in shown):
        return "읽기 오류"
    running = sum(len(row["running"]) for row in shown)
    unread = sum(len(row["unread"]) for row in shown)
    parts = ([f"진행 {running}"] if running else []) + ([f"결과 {unread}개"] if unread else [])
    return " · ".join(parts) or "대기"


def bring_to_foreground(widget):
    """Make a Tk window foreground so its popup closes on an outside click (tray quirk)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user = ctypes.windll.user32
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        hwnd = user.GetAncestor(widget.winfo_id(), 2)
        return bool(hwnd and user.SetForegroundWindow(hwnd))
    except (AttributeError, OSError, ValueError):
        return False


def _icon_file():
    try:
        path = Path(str(resources.files("handback").joinpath("assets", "icon.ico")))
        return str(path) if path.is_file() else None
    except (OSError, TypeError):
        return None


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


def taskbar_dockable(info):
    return bool(info and info["edge"] in (1, 3) and not info["auto_hide"]
                and info["rect"][2] > info["rect"][0] and info["rect"][3] > info["rect"][1])


def panel_dock_geometry(info, area, width, height, x):
    """Sit the expanded panel flush against a horizontal taskbar, keeping its x."""
    left, top, right, bottom = area
    width, height = min(width, right - left), min(height, bottom - top)
    tb_left, tb_top, tb_right, tb_bottom = info["rect"]
    y = tb_top - height if info["edge"] == 3 else tb_bottom
    return (max(left, min(x, right - width)), max(top, min(y, bottom - height)), width, height)


def compact_magnet_geometry(info, area, geometry, cursor, dpi=96):
    """Where a free drag is shown: the raw geometry, drawn toward the dock near a taskbar.

    Only the shown position changes; docking is still decided from the raw
    geometry on release. The approach leans part of the way; a drop that would
    dock is drawn most of the way in, and the release springs the rest.
    """
    x, y, width, height = geometry
    gap = dock_motion.taskbar_gap(info, cursor)
    strength = (dock_motion.magnet_strength(gap, dock_motion.scaled(dock_motion.MAGNET_RADIUS, dpi))
                * dock_motion.MAGNET_LEAN)
    would_dock, dock = compact_drop_geometry(info, area, x, y, width, height, cursor=cursor, dpi=dpi)
    if would_dock:
        strength, target = max(strength, dock_motion.DOCK_PREVIEW), dock[:2]
    elif strength > 0:
        target = compact_geometry(info, area, width, height, x, dpi=dpi)[:2]
    else:
        return geometry
    return (*dock_motion.magnet_pull((x, y), target, strength), width, height)


def panel_drop_geometry(info, area, x, y, width, height, *, cursor):
    """Magnetize the expanded panel when it is released with the pointer in the taskbar."""
    geometry = clamp_compact(x, y, width, height, area)
    if taskbar_dockable(info):
        left, top, right, bottom = info["rect"]
        if left <= cursor[0] < right and top <= cursor[1] < bottom:
            return True, panel_dock_geometry(info, area, width, height, geometry[0])
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
        lines.append({"row": row, "lead": agent_key(row.get("lead")),
                      "name": truncate_text(row["name"], name_width, measure),
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
    theme = THEMES["light" if light else "dark"]
    palette = {key: theme[key] for key in ("fg", "dim", "green", "yellow")}
    background = _luminance(rgb)
    for key, color in palette.items():
        foreground = _luminance(tuple(int(color[i:i+2], 16) for i in (1, 3, 5)))
        if (max(background, foreground) + .05) / (min(background, foreground) + .05) < 4.5:
            palette[key] = "#000000" if light else "#ffffff"
    # The lead dot is a small graphic, not text: keep the agent hue on any
    # taskbar shade and only fall back to the text colour below 3:1.
    for agent, color in (AGENT_COLORS_LIGHT if light else AGENT_COLORS_DARK).items():
        foreground = _luminance(tuple(int(color[i:i+2], 16) for i in (1, 3, 5)))
        if (max(background, foreground) + .05) / (min(background, foreground) + .05) < 3:
            color = palette["fg"]
        palette["agent_" + agent] = color
    return {"bg": "#%02x%02x%02x" % rgb, **palette}


def taskbar_sample_points(info, dpi, excluded=None):
    """Sample inset top/bottom bands, away from icon centres and our own window.

    A translucent (acrylic) taskbar tints each stretch with the wallpaper behind
    it, so one bar-wide shade can be far from what surrounds the widget. When
    the widget sits on the bar, sample only the strips right beside it.
    """
    if not info or info["auto_hide"] or info["edge"] not in (1, 3):
        return []
    left, top, right, bottom = info["rect"]
    inset = max(2, round(4 * dpi / 96))
    if right - left <= inset * 2 or bottom - top <= inset * 2:
        return []
    bands = (top + inset, bottom - inset - 1)
    if excluded is not None and excluded[0] < right and excluded[2] > left \
            and excluded[1] < bottom and excluded[3] > top:
        step = max(1, round(3 * dpi / 96))
        beside = [excluded[0] - inset - step * i for i in range(3)]
        beside += [excluded[2] + inset - 1 + step * i for i in range(3)]
        points = [(x, y) for x in beside if left <= x < right for y in bands]
        if points:
            return points
    points = [(left + (right-left) * i // 10, y)
              for i in range(1, 10) for y in bands]
    return [(x, y) for x, y in points if excluded is None or not
            (excluded[0] <= x < excluded[2] and excluded[1] <= y < excluded[3])]


def taskbar_colors(info, dpi, excluded=None):
    """Read a few composited screen pixels; always release the borrowed GDI objects.

    One BitBlt of the sampled band reads the screen back once. GetPixel on the
    screen DC pays that readback per point (~90 ms for the whole set), which is
    too slow to follow taskbar colour changes promptly.
    """
    points = taskbar_sample_points(info, dpi, excluded)
    if sys.platform != "win32" or not points:
        return []
    import ctypes
    from ctypes import wintypes
    left, top = min(x for x, _ in points), min(y for _, y in points)
    width = max(x for x, _ in points) - left + 1
    height = max(y for _, y in points) - top + 1
    try:
        user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
        user.GetDC.argtypes = [wintypes.HWND]
        user.GetDC.restype = wintypes.HDC
        user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi.CreateCompatibleDC.restype = wintypes.HDC
        gdi.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi.SelectObject.restype = wintypes.HGDIOBJ
        gdi.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
        gdi.BitBlt.restype = wintypes.BOOL
        gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi.DeleteDC.argtypes = [wintypes.HDC]
        gdi.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi.GetPixel.restype = wintypes.DWORD
        dc = user.GetDC(None)
        if not dc:
            return []
        memory = bitmap = old = None
        try:
            memory = gdi.CreateCompatibleDC(dc)
            bitmap = gdi.CreateCompatibleBitmap(dc, width, height) if memory else None
            if not bitmap:
                return []
            old = gdi.SelectObject(memory, bitmap)
            if not gdi.BitBlt(memory, 0, 0, width, height, dc, left, top, 0x00CC0020):  # SRCCOPY
                return []
            colors = [gdi.GetPixel(memory, x - left, y - top) for x, y in points]
            return [(color & 255, (color >> 8) & 255, (color >> 16) & 255)
                    for color in colors if color != 0xffffffff]
        finally:
            if old:
                gdi.SelectObject(memory, old)
            if bitmap:
                gdi.DeleteObject(bitmap)
            if memory:
                gdi.DeleteDC(memory)
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


def _dark_titlebar(window, dark=True):
    """Ask supported Windows versions for a dark (or, with dark=False, light) title bar."""
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
        enabled = wintypes.BOOL(dark)
        setter(hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
    except (AttributeError, OSError):
        pass  # The themed client area also works without DWM support.


class DarkMenu:
    """One owned popup tree. All event callbacks belong to disposable widgets."""
    # Dark defaults; each menu takes the strip's current theme in __init__.
    BG, FG, HOVER, DISABLED = (THEMES["dark"][key] for key in ("surface", "fg", "hover", "disabled"))

    def __init__(self, strip, items, area, parent=None):
        tk = strip.tk
        self.strip, self.items, self.area, self.parent = strip, items, area, parent
        self.BG, self.FG, self.HOVER, self.DISABLED = strip.SURFACE, strip.FG, strip.HOVER, strip.DISABLED
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
            if hover and self.active is not None:  # pointing elsewhere leaves the old row and its submenu
                if self.child:
                    self.child.destroy()
                    self.child = None
                self.labels[self.active].configure(bg=self.BG)
                self.active = None
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
        strip._style_scrollbar(parent)
        self.scrollbar = ttk.Scrollbar(self.container, orient="vertical", command=self.scroll,
                                       takefocus=0, style="Dark.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.content = tk.Frame(self.canvas, bg=strip.BG, padx=10, pady=6,
                                highlightthickness=1, highlightbackground=strip.HOVER)
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
    # Dark theme colours; _set_theme replaces them per instance.
    BG, FG, DIM, BORDER, GREEN, YELLOW, RED, SURFACE, HOVER, DISABLED, FIELD = (
        THEMES["dark"][key] for key in THEME_KEYS)
    COLORS = AGENT_COLORS_DARK
    theme = "dark"
    _opacity = 100
    _handle = None  # grab bar of the floating widget, when shown
    _handle_press = False
    # Notification-area state (class defaults keep partially built test instances valid).
    tray = None
    _tray_timer = None
    _tray_text = "대기"
    _update = None  # newer release found by the update check
    _compat_failures = ()  # last failed app compatibility checks (compat.failures)
    _user_hidden = False
    _show_event = None
    _show_timer = None
    _foreground = None  # foreground-change hook thread (winevents)
    # Idle auto-hide: hidden while nothing is happening, unless shown from the tray.
    _idle_hidden = False
    _idle_shown = False
    # Attention effects: labels the animation tick recolours, keyed (kind, project).
    _anim_timer = None
    _anim_labels = {}
    _pulse_until = {}
    _unread_seen = None
    _effects = {}
    _options_window = None

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
        self._foreground = None
        self._palette_burst_until = 0
        self._palette_source = None
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
        self._drag_held = False
        self._settle_timer = None
        self._settle_frames = []
        self._settle_move = None
        self._drag_timer = None
        self._drag_capture_timer = None
        self._drag_poll_timer = None
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
        self._anim_labels = {}
        self._pulse_until = {}
        prefs = load_prefs(home)
        self.mode = prefs["mode"]
        self._set_theme(resolve_theme(prefs["theme"]))
        self._opacity = prefs["opacity"]
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("handback")
        self.icons = []
        for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
            try:
                asset = resources.files("handback").joinpath("assets", f"icon-{size}.png")
                self.icons.append(tk.PhotoImage(master=self.root, data=asset.read_bytes(), format="png"))
            except (OSError, tk.TclError):
                pass  # Source-only or incomplete installs still have a usable dashboard.
        if sys.platform == "win32":
            ico = _icon_file()
            if ico:
                try:
                    self.root.iconbitmap(default=ico)  # window/dialog logo
                except tk.TclError:
                    pass
        if self.icons:
            self.root.iconphoto(True, *self.icons)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self._opacity / 100)
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
                              highlightthickness=1, highlightbackground=self.HOVER)
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

    def _set_theme(self, name):
        theme = THEMES[name]
        self.theme = name
        for key in THEME_KEYS:
            setattr(self, key.upper(), theme[key])
        self.COLORS = theme["agents"]

    def _style_scrollbar(self, master):
        from tkinter import ttk
        style = ttk.Style(master)
        if style.theme_use() != "clam":
            style.theme_use("clam")
        style.configure("Dark.Vertical.TScrollbar", background=self.SURFACE, troughcolor=self.BG,
                        bordercolor=self.BG, arrowcolor=self.FG, lightcolor=self.SURFACE,
                        darkcolor=self.SURFACE)
        style.map("Dark.Vertical.TScrollbar", background=[("active", self.HOVER)])

    def _apply_appearance(self, prefs):
        """Follow the opacity and theme preferences; recolour open windows on a theme change."""
        if prefs["opacity"] != self._opacity and self.panel is not None:
            self.panel.attributes("-alpha", prefs["opacity"] / 100)
        self._opacity = prefs["opacity"]
        theme = resolve_theme(prefs["theme"])
        if theme == self.theme:
            return
        old, new = THEMES[self.theme], THEMES[theme]
        mapping = {old[key]: new[key] for key in THEME_KEYS}
        mapping.update({old["agents"][agent]: new["agents"][agent] for agent in old["agents"]})
        self._set_theme(theme)
        self.tooltip.hide()
        self._close_menu()
        self._style_scrollbar(self.root)
        recolor(self.root, mapping)
        for window in self.root.winfo_children():
            if isinstance(window, self.tk.Toplevel):
                _dark_titlebar(window, theme == "dark")
        # Rebuild whatever caches its look: the compact view, the panel rows.
        self._compact_view_key = self._compact_style_key = None
        for frame in (self.frame, self.panel_frame):
            if frame is not None:
                frame._panel_view_key = None

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
            self._stop_tray()
            self._stop_foreground()
            for name in ("_outside_timer", "_refresh_timer", "_zorder_timer",
                         "_drag_timer", "_drag_capture_timer", "_drag_poll_timer", "_settle_timer",
                         "_corner_timer", "_show_timer",
                         "_anim_timer"):
                timer = getattr(self, name)
                if timer is not None:
                    self.root.after_cancel(timer)
                    setattr(self, name, None)
            self._show_event = None  # stop re-arming; the handle closes with the process
            self._start = None
            self._dragging = False

    # ---- notification-area icon: the tray thread only queues events; Tk acts here ----
    def _start_tray(self):
        if sys.platform != "win32" or self.tray is not None:
            return self.tray is not None
        tray = Tray(_icon_file(), "handback — " + self._tray_text)
        if not tray.start():
            return False
        self.tray = tray
        self._tray_timer = self.root.after(TRAY_POLL_MS, self._poll_tray)
        return True

    def _stop_tray(self):
        tray, self.tray = self.tray, None
        if self._tray_timer is not None:
            self.root.after_cancel(self._tray_timer)
            self._tray_timer = None
        if tray is not None:
            tray.stop()

    def _poll_tray(self):
        self._tray_timer = None
        tray = self.tray
        if tray is None:
            return
        while self.tray is tray:
            try:
                kind, point = tray.events.get_nowait()
            except queue.Empty:
                break
            self._tray_event(kind, point)
        if self.tray is tray:
            tray.set_tooltip("handback — " + self._tray_text)
            self._tray_timer = self.root.after(TRAY_POLL_MS, self._poll_tray)

    # ---- a second launch signals a named event; only this Tk timer reacts ----
    def watch_show_requests(self, handle):
        if sys.platform != "win32" or not handle:
            return False
        self._show_event = handle
        self._show_timer = self.root.after(SHOW_POLL_MS, self._poll_show_request)
        return True

    def _poll_show_request(self):
        self._show_timer = None
        try:
            if show_requested(self._show_event):
                self.show()  # un-hide from the tray; an open panel stays as is
                bring_to_foreground(self.root)
        finally:
            if self._show_event is not None:
                try:
                    self._show_timer = self.root.after(SHOW_POLL_MS, self._poll_show_request)
                except self.tk.TclError:
                    pass  # the root is being destroyed

    def _tray_event(self, kind, point):
        if kind == "left":
            self._toggle_visible()
        elif kind == "right":
            self._tray_menu(point)
        elif kind == "balloon":
            if self._compat_failures:
                self._compat_report(self._compat_failures[0])
        elif kind == "quit":
            self.quit()

    def _toggle_visible(self):
        self.show() if self._user_hidden or self._idle_hidden else self.hide()

    def hide(self):
        """Withdraw the dashboard to the tray (it keeps running)."""
        self._collapse_panel()
        self._close_menu()
        self.tooltip.hide()
        self._user_hidden = True
        self.root.withdraw()

    def show(self):
        self._user_hidden = False
        if self._idle_hidden:  # an explicit show outlasts idle hiding until new activity
            self._idle_hidden, self._idle_shown = False, True
        if self._fullscreen_hidden:
            return  # the full-screen watcher shows it again when the cover ends
        show_widget_without_activation(self.root, True)
        self.root.lift()
        self._sync_animation()

    def close(self):
        """Close means hide while the tray can bring it back, otherwise exit."""
        self.hide() if self.tray is not None else self.root.destroy()

    def quit(self):
        self._stop_tray()
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass

    def _tray_menu(self, point):
        x, y = point or (self.root.winfo_pointerx(), self.root.winfo_pointery())
        self._menu(SimpleNamespace(x_root=x, y_root=y), None)
        if self._context_menu is not None:
            bring_to_foreground(self._context_menu.window)

    def _sync_zorder_timer(self):
        watched = self.mode == "taskbar" or self.docked
        if watched and sys.platform == "win32":
            if self._zorder_timer is None:
                self._watch_zorder()
        elif self._zorder_timer is not None:
            self.root.after_cancel(self._zorder_timer)
            self._zorder_timer = None
        if watched and sys.platform == "win32":
            if self._foreground is None:
                watcher = ForegroundWatcher()
                if watcher.start():
                    self._foreground = watcher
        else:
            self._stop_foreground()
        if not watched and self._fullscreen_hidden:
            if not self._user_hidden and not self._idle_hidden:
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
            if not self._user_hidden and not self._idle_hidden:  # tray/idle hiding wins
                show_widget_without_activation(self.root, not hidden)
            self._fullscreen_hidden = hidden
            if not hidden:
                self._sync_animation()
        if not hidden:
            keep_above_taskbar(self.root, self._taskbar_hwnd)
        # Schedule first: a colour change re-renders, and rendering must find the
        # timer already armed instead of starting a second one.
        self._zorder_timer = self.root.after(TASKBAR_ZORDER_MS, self._watch_zorder)
        self._follow_foreground(hidden)

    def _stop_foreground(self):
        watcher, self._foreground = self._foreground, None
        if watcher is not None:
            watcher.stop()

    def _follow_foreground(self, hidden):
        """Hand the hook thread fresh handles; resample the taskbar colour on time."""
        now = time.monotonic()
        watcher = self._foreground
        if watcher is not None:
            if watcher.changed.is_set():
                watcher.changed.clear()
                self._palette_burst_until = now + PALETTE_BURST_LENGTH
            watcher.target = None if hidden or self._user_hidden else _root_hwnd(self.root)
            watcher.taskbar = self._taskbar_hwnd
        if (hidden or self._user_hidden or not self.docked or self._dragging
                or self._palette_source is None or not self.root.winfo_ismapped()):
            return
        interval = PALETTE_BURST_SECONDS if now < self._palette_burst_until else PALETTE_SECONDS
        if now - self._palette_at < interval:
            return
        info, dpi = self._palette_source
        x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
        excluded = (x, y, x + self.root.winfo_width(), y + self.root.winfo_height())
        if self._sample_taskbar_palette(info, dpi, excluded):
            self._compact_view_key = None
            self.refresh()

    def _sample_taskbar_palette(self, info, dpi, excluded):
        """Return True when the docked colours changed."""
        self._palette_at = time.monotonic()
        samples = [rgb for rgb in taskbar_colors(info, dpi, excluded) if rgb != (0, 0, 0)]
        if not samples:
            return False
        palette = compact_palette(samples)
        changed = palette != self._compact_palette
        self._compact_palette = palette
        return changed

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
            target.bind("<Enter>", lambda event: paint(self.SURFACE))
            target.bind("<Leave>", lambda event: paint(self.BG))
            if project:
                target.bind("<Button-3>", lambda event: self._menu(event, project))

    def _toggle_overflow(self):
        self.show_all = not self.show_all
        self.refresh(reschedule=False)

    def render(self, rows):
        self._rows = rows
        prefs = load_prefs(self.home)
        self._apply_appearance(prefs)
        self._effects = {key: prefs[key] for key in
                         ("highlight_unread", "animate_unread", "animate_running")}
        self._note_new_results(rows)
        self._tray_text = tray_state_text(rows, prefs["hidden"]) + (
            f" · 업데이트 {self._update['version']} 있음" if self._update else "")
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
            self.root.attributes("-alpha", self._opacity / 100)
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
        self._sync_idle(prefs["auto_hide"] and is_idle(rows, prefs))
        self._sync_animation()

    def _note_new_results(self, rows):
        """Start a pulse for projects whose unread count grew since the last render."""
        counts = {row["root"]: len(row["unread"]) for row in rows}
        if self._unread_seen is not None:  # the first render is not an arrival
            now = time.monotonic()
            for root, count in counts.items():
                if count > self._unread_seen.get(root, 0):
                    self._pulse_until[root] = now + PULSE_SECONDS
        for root in [root for root in self._pulse_until if not counts.get(root)]:
            del self._pulse_until[root]
        self._unread_seen = counts

    def _sync_idle(self, idle):
        """Withdraw while idle; new activity re-arms hiding after a tray show."""
        if not idle:
            self._idle_shown = False
        hidden = idle and not self._idle_shown and self.panel is None and not self._dragging
        if hidden == self._idle_hidden:
            return
        self._idle_hidden = hidden
        if hidden:
            self._close_menu()
        if not self._user_hidden and not self._fullscreen_hidden:
            show_widget_without_activation(self.root, not hidden)

    def _register_effect(self, kind, project, label, color, rest, attr="fg"):
        """Remember a label the animation tick may recolour between color and rest."""
        self._anim_labels[(kind, project, id(label))] = (label, attr, color, rest)

    def _sync_animation(self):
        if self._anim_timer is None and self._animating():
            self._anim_timer = self.root.after(ANIMATION_MS, self._animate)

    def _animating(self, now=None):
        now = time.monotonic() if now is None else now
        effects = self._effects
        return any(kind == "unread" and effects.get("animate_unread") and self._pulse_until.get(project, 0) > now
                   or kind == "running" and effects.get("animate_running")
                   for kind, project, _ in self._anim_labels)

    def _animate(self):
        self._anim_timer = None
        now = time.monotonic()
        visible = not (self._user_hidden or self._fullscreen_hidden or self._idle_hidden)
        for key, (label, attr, color, rest) in list(self._anim_labels.items()):
            kind, project, _ = key
            try:
                if not label.winfo_exists():
                    del self._anim_labels[key]
                    continue
                if kind == "unread" and self._effects.get("animate_unread") and                         self._pulse_until.get(project, 0) > now:
                    amount = (1 - math.cos(now * 2 * math.pi * 1.4)) / 2  # ~1.4 blinks/s
                elif kind == "running" and self._effects.get("animate_running"):
                    amount = 0.55 * (1 - math.cos(now * 2 * math.pi * 0.5)) / 2  # slow breathing
                else:
                    amount = 0
                value = blend(color, rest, amount) if visible else color
                if label.cget(attr) != value:
                    label.configure(**{attr: value})
            except (self.tk.TclError, ValueError):
                self._anim_labels.pop(key, None)
        if visible and self._animating(now):
            self._anim_timer = self.root.after(ANIMATION_MS, self._animate)

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
            self._sample_taskbar_palette(info, dpi, excluded)
            self._palette_key = key
        self._palette_source = (info, dpi) if self.docked and info else None
        palette = self._compact_palette if self.docked else {
            "bg": self.BG, "fg": self.FG, "dim": self.DIM, "green": self.GREEN, "yellow": self.YELLOW,
            **{"agent_" + agent: color for agent, color in self.COLORS.items()}}
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
        handle = prefs["drag_handle"] and not self.docked
        view_key = (self.docked, dpi, self.compact_metrics.cget("size"), tuple(palette.items()),
                    self._effects.get("highlight_unread"), handle, self._opacity,
                    tuple((line["row"]["root"], line["lead"], line["name"], line["running"], line["unread"],
                           line["more"],
                           line["overflow_unread"],
                           read_error_text(line["row"]),
                           bool(conversation_targets(line["row"], "running")),
                           bool(conversation_targets(line["row"], "unread"))) for line in lines))
        if view_key != self._compact_view_key:
            self._render_compact_items(lines, palette, padding, gap, border, handle=handle, dpi=dpi)
            self.root.update_idletasks()
            self._compact_view_key = view_key
        width = max(self.frame.winfo_reqwidth(), name_width + sum(measure(text) for text in
                    (AGENT_DOT + " ", "● 99", "✉ 99", "+99")) + 3 * gap + 2 * padding) + 2 * border
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
        shown = geometry
        if self._dragging and not self._drag_held and self._drag_cursor is not None:
            # A refresh mid-drag keeps the magnetic lean instead of flicking to raw.
            shown = compact_magnet_geometry(info, area, geometry, self._drag_cursor, dpi)
        actual = (self.root.winfo_x(), self.root.winfo_y(), self.root.winfo_width(), self.root.winfo_height())
        if actual != shown:
            self._cancel_settle()
            x, y, width, height = shown
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        if self._dragging:
            self._drag_width = geometry[2]
            self._drag_height = self._compact_free_height
            self._drag_applied_geometry = shown
        self._sync_window_corners()

    def _ack_on_click(self):
        return load_prefs(self.home)["ack_on_click"]

    def _purge_effects(self):
        for key, entry in list(self._anim_labels.items()):
            try:
                alive = entry[0].winfo_exists()
            except self.tk.TclError:
                alive = False
            if not alive:
                del self._anim_labels[key]

    def _open_unread(self, event, project):
        """✉ click: clear the shown results (option), then open where they came from."""
        row = next((row for row in self._rows if row["root"] == project), None)
        if row is None or not row["unread"]:
            return "break"
        acknowledged = False
        if self._ack_on_click():
            ids = [item["id"] for item in row["unread"] if item.get("id")]
            try:
                acknowledge_ids(project, ids, self.home)
            except (OSError, ValueError, TimeoutError) as error:
                self._dialog("확인 처리 실패", ui_error(error))
                return "break"
            acknowledged = True
            self._pulse_until.pop(project, None)
            self.refresh(reschedule=False)
        targets = conversation_targets(row, "unread")
        if any(conversation_link(target["handle"]) for target in targets):
            return self._conversations(event, targets)
        self._open(row, 0, acknowledged=acknowledged)
        return "break"

    def _compact_targets(self, project, kind):
        row = next((row for row in self._rows if row["root"] == project), None)
        return conversation_targets(row, kind) if row is not None else []

    def _render_compact_items(self, lines, palette, padding, gap, border, handle=False, dpi=96):
        with self.tooltip.preserve_on_rebuild(self.frame):
            bg = palette["bg"]
            # The floating border is the root showing through: tint it while results wait.
            attention = bool(border and self._effects.get("highlight_unread")
                             and any(line["row"]["unread"] or line["overflow_unread"] for line in lines))
            # Docked colours are sampled through the taskbar's own transparency: stay opaque.
            alpha = 1.0 if self.docked else self._opacity / 100
            style_key = (self.docked, bg, padding, border, attention, alpha, self.BORDER)
            if style_key != self._compact_style_key:
                if self.root.attributes("-alpha") != alpha:
                    self.root.attributes("-alpha", alpha)
                root_bg = (blend(self.BORDER, palette["yellow"], 0.7) if attention
                           else self.BORDER if border else bg)
                if self.root.cget("bg") != root_bg:
                    self.root.configure(bg=root_bg)
                self.frame.configure(bg=bg, padx=padding, pady=0 if self.docked else 2,
                                     highlightthickness=0,
                                     highlightbackground=self.HOVER)
                self.frame.pack_configure(fill="both", expand=True, padx=border, pady=border)
                self._compact_style_key = style_key
            for child in self.frame.winfo_children():
                child.destroy()
            self._purge_effects()
            self._handle = None
            if not lines:
                self.tk.Label(self.frame, text="handback · 대기", bg=bg, fg=palette["dim"],
                              font=self.compact_metrics, bd=0, padx=0).pack(anchor="w", expand=True)
            for line in lines:
                row = self.tk.Frame(self.frame, bg=bg)
                row.pack(fill="x", expand=True)
                error = read_error_text(line["row"])
                project = line["row"]["root"]
                badge = bool(line["unread"] and self._effects.get("highlight_unread"))
                for key, color in (("name", palette["fg"]),
                                   ("running", palette["yellow"] if error else
                                    palette["green"] if line["row"]["running"] else palette["dim"]),
                                   ("unread", bg if badge else palette["yellow"]),
                                   ("more", palette["yellow"] if line["overflow_unread"] else palette["dim"])):
                    label = self.tk.Label(row, text=line[key], bg=palette["yellow"] if key == "unread" and badge
                                          else bg, fg=color, font=self.compact_metrics, bd=0,
                                          padx=max(1, gap) if key == "unread" and badge else 0, pady=0)
                    label.pack(side="left", padx=(0, gap if key != "more" else 0))
                    if key == "unread" and line["unread"]:
                        # Badge: the background blinks; plain text: the glyph fades to dim.
                        self._register_effect("unread", project, label, palette["yellow"],
                                              bg if badge else palette["dim"], "bg" if badge else "fg")
                    if key == "running" and line["row"]["running"] and not error:
                        self._register_effect("running", project, label, palette["green"], bg)
                    label.bind("<Button-3>", lambda event, project=line["row"]["root"]: self._menu(event, project))
                    if key == "name":
                        name = label
                    if key == "more" and line["more"]:
                        self.tooltip.bind(label, f"다른 프로젝트의 미확인 결과 {line['overflow_unread']}개"
                                          if line["overflow_unread"] else "다른 프로젝트 보기",
                                          key=("compact-overflow", line["row"]["root"]))
                    if key == "running" and error:
                        self.tooltip.bind(label, error, key=("compact-error", line["row"]["root"]))
                        continue
                    if key == "unread" and line["unread"]:
                        label.configure(cursor="hand2")
                        label.bind("<Button-1>", lambda event, project=project:
                                   self._press_status(event, project, "unread"))
                        self.tooltip.bind(label, unread_tip(line["row"], self._ack_on_click()),
                                          key=("compact-unread", project))
                    elif key == "running":
                        targets = conversation_targets(line["row"], key)
                        if targets:
                            label.configure(cursor="hand2")
                            label.bind("<Button-1>", lambda event, project=line["row"]["root"], kind=key:
                                       self._press_status(event, project, kind))
                # Lead-agent dot: its own label (a Tk label has one colour), created
                # last so existing child indices stay stable, packed before the name
                # and bound like it (press/drag/release come from the root bindings).
                dot = self.tk.Label(row, text=AGENT_DOT + " ", bg=bg, fg=agent_color(line["lead"], palette),
                                    font=self.compact_metrics, bd=0, padx=0, pady=0)
                dot.pack(side="left", before=name)
                dot.bind("<Button-3>", lambda event, project=line["row"]["root"]: self._menu(event, project))
                if line["lead"]:
                    self.tooltip.bind(dot, "리드 · " + agent_name(line["lead"]),
                                      key=("compact-lead", line["row"]["root"]))
            if handle:
                self._build_handle(palette, dpi)

    def _build_handle(self, palette, dpi):
        """Grab bar above the rows: drags like the rest, but a click opens nothing.

        Created last and packed first, so row child indices stay stable.
        """
        bar = self._handle = self.tk.Frame(self.frame, bg=palette["bg"], cursor="fleur",
                                           height=dock_motion.scaled(8, dpi))
        bar.pack(fill="x", before=self.frame.winfo_children()[0])
        grip = self.tk.Frame(bar, bg=palette["dim"], cursor="fleur", width=dock_motion.scaled(28, dpi),
                             height=dock_motion.scaled(3, dpi))
        grip.place(relx=0.5, rely=0.5, anchor="center")
        for target in (bar, grip):
            target.bind("<ButtonPress-1>", self._press_handle)
            target.bind("<Enter>", lambda event: grip.configure(bg=palette["fg"]))
            target.bind("<Leave>", lambda event: grip.configure(bg=palette["dim"]))
            target.bind("<Button-3>", lambda event: self._menu(event, None))

    def _press_handle(self, event):
        self._press(event)
        self._handle_press = True
        return "break"

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
        panel.attributes("-alpha", self._opacity / 100)
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
            self._purge_effects()
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
                dot_text = AGENT_DOT + " "
                name = self.tk.Label(cells[0], bg=self.BG, fg=self.FG, font=self.bold, anchor="w",
                                     cursor="hand2", text=("▾ " if key in self.expanded else "▸ ") +
                                     truncate_text(data["name"], self.widths[0] - 26 -
                                                   self.bold_metrics.measure(dot_text), self.bold_metrics.measure))
                name.pack(side="left", fill="both", expand=True)
                name.bind("<Button-1>", lambda event, key=key: self._toggle(key))
                # Same lead dot as the compact widget; created after the name so
                # child indices stay stable, packed before it, and clicks like it.
                lead = agent_key(data.get("lead"))
                dot = self.tk.Label(cells[0], bg=self.BG, fg=self.COLORS.get(lead, self.DIM),
                                    font=self.bold, text=dot_text, cursor="hand2", bd=0, padx=0)
                dot.pack(side="left", fill="y", before=name)
                dot.bind("<Button-1>", lambda event, key=key: self._toggle(key))
                if lead:
                    self.tooltip.bind(dot, "리드 · " + agent_name(lead), key=("lead", key))
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
                    cell = cells[column].winfo_children()[0]
                    if kind == "unread" and data["unread"]:
                        cell.configure(cursor="hand2")
                        cell.bind("<Button-1>", lambda event, key=key: self._open_unread(event, key))
                        self.tooltip.bind(cell, unread_tip(data, self._ack_on_click()),
                                          key=("status", key, kind, len(data["unread"])))
                        self._register_effect("unread", key, cell, self.YELLOW, self.DIM)
                        continue
                    if kind == "running" and running:
                        self._register_effect("running", key, cell, self.GREEN, self.BG)
                    targets = conversation_targets(data, kind)
                    if not targets:
                        continue
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
                                        self.YELLOW if item["kind"] == "result" else self.RED, index,
                                        ("unread", item.get("id"), item.get("sender"), item.get("created_utc"))))
                    if len(data["unread"]) > DETAIL_LIMIT:
                        details.append((f"… 전체 {len(data['unread'])}개 보기", self.DIM, 0, ("all",)))
                    box = self.tk.Frame(self.frame, bg=self.BG)
                    box.grid(row=line, column=0, sticky="ew", padx=(9, 0), pady=(0, 3))
                    self.tk.Frame(box, bg=self.HOVER, width=2).pack(side="left", fill="y", padx=(0, 8))
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
            agents = self._role_items(project, data)
            if execution:
                agents.extend([None, {"label": "모델·추론", "enabled": False}] + execution)
            items.append({"label": "에이전트 ▶", "children": agents})
            stuck = [{"label": f"{_preview(item.get('name') or item.get('id', ''), 28)} · {agent_name(item.get('agent') or '')} · "
                               f"{REQUEST_STATES.get(item.get('status'), item.get('status'))} · "
                               f"{ago(_utc_timestamp(item.get('created_utc')))}",
                      "command": lambda item=item: self._close_request(project, item)}
                     for item in data.get("running", [])]
            if stuck:
                items.append({"label": "요청 닫기 ▶", "children": stuck})
            testing = self._is_busy(("try", project))
            items.extend([{"label": "연결 테스트 (테스트 중…)" if testing else "연결 테스트",
                           "enabled": not testing and not data.get("running"),
                           "tooltip": "진행 중인 작업이 있어 지금은 할 수 없습니다" if data.get("running") else "",
                           "command": lambda: self._connection_test(project)}, None])
        if self._compat_failures and not project:
            items[:0] = [{"label": f"⚠ {compat.NAMES[r['agent']]} {r['version']} 호환성 점검 실패 — 진단 요약",
                          "tooltip": "실패 항목과 관련 로그를 복사해 GitHub 이슈로 올릴 수 있습니다",
                          "command": lambda r=r: self._compat_report(r)}
                         for r in self._compat_failures] + [None]
        checking = self._is_busy(("doctor",))
        compat_busy = self._is_busy(("compat",))
        repair = [
            {"label": "설치 상태 점검 (점검 중…)" if checking else "설치 상태 점검",
             "enabled": not checking, "command": self._check_install},
            {"label": "앱 호환성 점검 (점검 중…)" if compat_busy else "앱 호환성 점검",
             "tooltip": "Codex·Claude·Antigravity 연동 형식을 읽기만 해서 확인합니다 (메시지는 보내지 않음)",
             "enabled": not compat_busy, "command": self._check_compat},
            {"label": "스킬·훅 설치·복구", "command": self._open_setup}]
        items.extend([
            {"label": "옵션…", "tooltip": "표시 방식·숨긴 프로젝트·시작 시 실행·업데이트 확인을 한 창에서 바꿉니다",
             "command": self._options},
            self._update_item(),
            {"label": "점검·복구 ▶", "children": repair},
            None]
            + ([{"label": "보이기" if self._user_hidden else "숨기기",
                 "command": self.show if self._user_hidden else self.hide},
                {"label": "종료", "command": self.quit}] if self.tray is not None
               else [{"label": "닫기", "command": self.close}]))
        menu = DarkMenu(self, items, _monitor_area(self.root, event.x_root, event.y_root))
        self._context_menu = menu
        menu.show(event.x_root, event.y_root)
        return "break"

    def _button(self, parent, text, command):
        button = self.tk.Button(parent, text=text, command=command, font=self.font,
                                bg=self.SURFACE, fg=self.FG, activebackground=self.HOVER,
                                activeforeground=self.FG, relief="flat", bd=0, padx=14, pady=6,
                                highlightthickness=1, highlightbackground=self.HOVER,
                                highlightcolor=self.YELLOW, cursor="hand2")
        button.bind("<Enter>", lambda event: button.configure(bg=self.HOVER))
        button.bind("<Leave>", lambda event: button.configure(bg=self.SURFACE))
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
            entry = tk.Entry(frame, width=34, bg=self.SURFACE, fg=self.FG,
                             insertbackground=self.FG, relief="flat", font=self.font,
                             highlightthickness=1, highlightbackground=self.FIELD, highlightcolor=self.YELLOW)
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
        tk.Label(frame, textvariable=error_text, bg=self.BG, fg=self.RED, font=self.font,
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
        _dark_titlebar(window, self.theme == "dark")
        window.deiconify()
        entries["model"].focus_set()

    # Options window rows: (pref key or None for a heading, label, help text).
    OPTION_SECTIONS = (
        ("표시", [("mode", "표시 방식", (("taskbar", "작게 보기"), ("panel", "펼쳐 보기"))),
                  ("max_rows", "펼친 패널 줄 수", ((2, "2줄"), (3, "3줄"), (0, "전부")))]),
        ("모양", [("theme", "테마", (("dark", "다크"), ("light", "라이트"), ("system", "시스템"))),
                ("opacity", "불투명도", tuple((value, f"{value}%") for value in OPACITY_CHOICES)),
                ("drag_handle", "드래그 손잡이 표시",
                 "떠 있는 작은 위젯 위쪽에 잡아 옮기는 막대를 둡니다. 막대는 눌러도 패널이 열리지 않습니다.")]),
        ("미확인 결과", [("ack_on_click", "✉ 클릭 시 확인 처리",
                          "대화를 열면서 ✉ 표시를 지웁니다. 끄면 우클릭 메뉴나 결과 창에서 정리합니다."),
                         ("highlight_unread", "강조색으로 표시",
                          "✉ 개수를 노란 배지로, 떠 있는 위젯은 테두리까지 물들입니다.")]),
        ("움직임", [("animate_unread", "새 결과가 오면 깜빡이기",
                    f"새 결과가 도착하면 ✉가 {PULSE_SECONDS:.0f}초 동안 깜빡입니다."),
                   ("animate_running", "진행 중 표시를 천천히 숨쉬게",
                    "작업이 진행되는 동안 ● 표시가 은은하게 밝아졌다 어두워집니다.")]),
        ("작업 표시줄", [("auto_hide", "할 일이 없으면 숨기기",
                        "진행 중·미확인 결과가 없으면 위젯을 감추고, 새 작업이 생기면 다시 보입니다. "
                        "트레이 아이콘을 누르면 언제든 꺼낼 수 있습니다."),
                       ("update_check", "업데이트 자동 확인", "")]),
    )

    def _options(self):
        """One dark window for every display/behaviour preference; edits apply at once."""
        self._close_menu()
        previous = self._options_window
        if previous is not None and previous.winfo_exists():
            previous.deiconify()
            previous.lift()
            previous.focus_force()
            return
        tk = self.tk
        window = tk.Toplevel(self.root)
        self._options_window = window
        window.withdraw()
        window.title("handback 옵션")
        window.configure(bg=self.BG)
        window.resizable(False, False)
        window.attributes("-topmost", True)
        frame = tk.Frame(window, bg=self.BG, padx=20, pady=14)
        frame.pack(fill="both", expand=True)
        prefs = load_prefs(self.home)
        variables = {}
        common = dict(bg=self.BG, fg=self.FG, activebackground=self.BG, activeforeground=self.FG,
                      selectcolor=self.SURFACE, font=self.font, bd=0, highlightthickness=0,
                      cursor="hand2", anchor="w")

        def apply(key):
            value = variables[key].get()
            if key == "max_rows":
                value = int(value)
                self.show_all = False
            self._save({**load_prefs(self.home), key: value})

        line = 0
        for heading, options in self.OPTION_SECTIONS:
            tk.Label(frame, text=heading, bg=self.BG, fg=self.YELLOW, font=("Segoe UI Semibold", 10),
                     anchor="w").grid(row=line, column=0, columnspan=6, sticky="w", pady=(10 if line else 0, 4))
            line += 1
            for key, label, extra in options:
                if isinstance(extra, tuple):  # radio choices
                    variables[key] = (tk.IntVar if isinstance(extra[0][0], int) else tk.StringVar)(
                        window, value=prefs[key])
                    tk.Label(frame, text=label, bg=self.BG, fg=self.FG, font=self.font,
                             anchor="w").grid(row=line, column=0, sticky="w", padx=(8, 16), pady=2)
                    for column, (value, text) in enumerate(extra, 1):
                        tk.Radiobutton(frame, text=text, value=value, variable=variables[key],
                                       command=lambda key=key: apply(key), **common).grid(
                            row=line, column=column, sticky="w", padx=(0, 12), pady=2)
                    line += 1
                    continue
                variables[key] = tk.BooleanVar(window, value=prefs[key])
                box = tk.Checkbutton(frame, text=label, variable=variables[key],
                                     command=lambda key=key: apply(key), **common)
                box.grid(row=line, column=0, columnspan=6, sticky="w", padx=(4, 0), pady=(2, 0))
                line += 1
                if extra:
                    tk.Label(frame, text=extra, bg=self.BG, fg=self.DIM, font=self.font, wraplength=380,
                             justify="left", anchor="w").grid(row=line, column=0, columnspan=6, sticky="w",
                                                              padx=(30, 0), pady=(0, 4))
                    line += 1
        tk.Label(frame, text="프로젝트", bg=self.BG, fg=self.YELLOW, font=("Segoe UI Semibold", 10),
                 anchor="w").grid(row=line, column=0, columnspan=6, sticky="w", pady=(10, 4))
        line += 1

        def unhide(root, button):
            self._save({**load_prefs(self.home), "hidden": [r for r in load_prefs(self.home)["hidden"] if r != root]})
            button.configure(text="표시됨", state="disabled")

        for root in prefs["hidden"]:
            tk.Label(frame, text=self.names.get(root, Path(root).name), bg=self.BG, fg=self.DIM,
                     font=self.font, anchor="w").grid(row=line, column=0, columnspan=2, sticky="w", padx=(8, 16))
            button = self._button(frame, "다시 표시", None)
            button.configure(command=lambda root=root, button=button: unhide(root, button))
            button.grid(row=line, column=2, columnspan=4, sticky="e", pady=2)
            line += 1
        if not prefs["hidden"]:
            tk.Label(frame, text="숨긴 프로젝트 없음", bg=self.BG, fg=self.DIM, font=self.font,
                     anchor="w").grid(row=line, column=0, columnspan=6, sticky="w", padx=(8, 0), pady=2)
            line += 1
        order = self._button(frame, "순서 초기화 (최근 활동순)", None)
        order.configure(command=lambda: (self._save({**load_prefs(self.home), "order": []}),
                                         order.configure(text="초기화됨", state="disabled")))
        order.grid(row=line, column=0, columnspan=6, sticky="w", padx=(8, 0), pady=(4, 2))
        line += 1
        if sys.platform == "win32":
            autostart = tk.BooleanVar(window, value=startup_shortcut().exists())
            tk.Checkbutton(frame, text="Windows 시작 시 실행", variable=autostart,
                           command=lambda: self._toggle_autostart(autostart.get()), **common).grid(
                row=line, column=0, columnspan=6, sticky="w", padx=(4, 0), pady=(2, 0))
            line += 1
        footer = tk.Frame(frame, bg=self.BG)
        footer.grid(row=line, column=0, columnspan=6, sticky="ew", pady=(14, 0))
        tk.Label(footer, text="바꾸는 즉시 적용됩니다", bg=self.BG, fg=self.DIM,
                 font=self.font).pack(side="left")
        self._button(footer, "닫기", window.destroy).pack(side="right")
        window.bind("<Escape>", lambda event: window.destroy())
        window.variables = variables  # tests and live refresh read the bound values
        window.update_idletasks()
        x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
        x, y, width, height = popup_position(x, y, window.winfo_reqwidth(), window.winfo_reqheight(),
                                           _monitor_area(self.root, x, y))
        window.geometry(f"{width}x{height}+{x}+{y}")
        _dark_titlebar(window, self.theme == "dark")
        window.deiconify()
        window.focus_force()

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
        _dark_titlebar(window, self.theme == "dark")
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

    def _open(self, data, focus, acknowledged=False):
        """Viewer for results; ACK via its confirmed button unless a ✉ click already did."""
        self.tooltip.hide()
        tk = self.tk
        window = tk.Toplevel(self.root)
        window.title(f"{data['name']} · {'결과' if acknowledged else '미확인 결과'} {len(data['unread'])}개")
        window.attributes("-topmost", True)
        window.geometry("760x520")
        window.configure(bg=self.BG)
        footer = tk.Frame(window, bg=self.BG, padx=8, pady=6)
        footer.pack(side="bottom", fill="x")
        # ACK exactly what this window rendered; mail arriving later stays pending.
        shown = [item["id"] for item in data["unread"]]
        if acknowledged:
            self._button(footer, text="닫기", command=window.destroy).pack(side="right")
        else:
            self._button(footer, text="모두 확인 처리", command=lambda: self._acknowledge(
                data["root"], parent=window, ids=shown) and window.destroy()).pack(side="right")
        tk.Label(footer, text="확인 처리했습니다 · 내용은 수신함에 남습니다" if acknowledged
                 else "확인 처리해도 내용은 수신함에 남습니다", bg=self.BG, fg=self.DIM,
                 font=self.font).pack(side="left")
        text = tk.Text(window, wrap="word", bg=self.BG, fg=self.FG, insertbackground=self.FG,
                       font=("Segoe UI", 10), padx=12, pady=10, bd=0)
        from tkinter import ttk
        self._style_scrollbar(window)
        bar = ttk.Scrollbar(window, command=text.yview, style="Dark.Vertical.TScrollbar")
        text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("head", foreground=self.YELLOW, font=("Segoe UI Semibold", 10))
        text.tag_configure("error", foreground=self.RED, font=("Segoe UI Semibold", 10))
        for index, item in enumerate(data["unread"]):
            text.mark_set(f"m{index}", "end-1c")
            text.mark_gravity(f"m{index}", "left")
            head = f"{_local_time(item['created_utc'])} · {item['kind']} · {item['from']}\n"
            text.insert("end", head, "error" if item["kind"] == "error" else "head")
            text.insert("end", item["body"].strip() + "\n\n")
        text.configure(state="disabled")
        text.see(f"m{focus}")
        text.yview(f"m{focus}")
        _dark_titlebar(window, self.theme == "dark")

    def _acknowledge(self, project, parent=None, ids=None):
        """ACK ids when given (viewer), otherwise every pending result (project menu)."""
        name = self.names.get(project, project)
        count = len(ids) if ids is not None else self.unread_counts.get(project, 0)
        if not self._dialog(
                "미확인 결과 정리",
                f"{name}의 미확인 결과 {count}개를 모두 확인 처리할까요?\n\n"
                "확인 처리한 결과는 리더 세션의 Monitor·복구 훅이 다시 전달하지 않습니다. "
                "내용은 지워지지 않고 수신함에 남습니다.", parent=parent or self.root, confirm=True):
            return False
        try:
            done = (acknowledge_ids(project, ids, self.home) if ids is not None
                    else acknowledge_all(project, self.home))
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

    def _close_request(self, project, item):
        status = REQUEST_STATES.get(item.get("status"), item.get("status"))
        if not self._dialog(
                "요청 닫기",
                f"{item.get('name')} ({agent_name(item.get('agent') or '')} · {status})\n\n"
                "이 요청을 실패로 닫을까요? 진행 중 표시에서 빠지고, 나중에 결과가 와도 받지 않습니다. "
                "요청 기록은 지워지지 않습니다.", parent=self.root, confirm=True):
            return
        try:
            close_request(project, item["id"], self.home)
        except (OSError, ValueError, TimeoutError) as error:
            self._dialog("요청 닫기 실패", ui_error(error), parent=self.root)
            return
        self.refresh(reschedule=False)

    # ---- menu actions ----
    def _is_busy(self, key):
        return key in getattr(self, "_busy", set())

    def _run_async(self, key, work, done):
        """Run work() in a thread; done(result, error) runs on the Tk thread.

        The worker only fills a holder; the Tk thread polls it with root.after,
        so no Tk call is ever made off-thread.
        """
        import threading
        if not hasattr(self, "_busy"):
            self._busy = set()
        self._busy.add(key)
        holder = {}

        def target():
            try:
                holder["result"] = work()
            except Exception as error:  # surfaced to the user as a one-line dialog
                holder["error"] = error
            holder["done"] = True

        def poll():
            if not holder.get("done"):
                self.root.after(150, poll)
                return
            self._busy.discard(key)
            done(holder.get("result"), holder.get("error"))

        threading.Thread(target=target, daemon=True).start()
        self.root.after(150, poll)

    def _toggle_autostart(self, enable):
        # PowerShell can take seconds; never block the Tk thread on it.
        def done(_result, error):
            if error:
                self._dialog("자동 실행", "자동 실행을 바꾸지 못했습니다 — " + " ".join(str(error).split())[:80])
        self._run_async(("autostart",), lambda: set_autostart(enable), done)

    def _check_install(self):
        root = self._rows[0]["root"] if self._rows else str(Path.cwd())

        def done(lines, error):
            if error:
                self._dialog("설치 상태 점검", "점검하지 못했습니다 — " + " ".join(ui_error(error).split())[:80])
            else:
                self._dialog("설치 상태 점검", "\n".join(lines) if lines else "문제 없음")
        self._run_async(("doctor",), lambda: doctor_problems(root, self.home), done)

    def _update_timer(self):
        if load_prefs(self.home)["update_check"]:
            self._check_update(manual=False)
        self.root.after(updater.CHECK_INTERVAL * 1000, self._update_timer)

    def _check_update(self, manual=True):
        if self._is_busy(("update",)):
            return

        def done(release, error):
            if release:
                self._update = release
                self.refresh(reschedule=False)
            if not manual:
                return
            if error:
                self._dialog("업데이트 확인", "확인하지 못했습니다 — " + " ".join(ui_error(error).split())[:80])
            elif release:
                self._install_update()
            else:
                self._dialog("업데이트 확인", f"최신 버전입니다 ({__version__})")
        self._run_async(("update",), updater.check, done)

    def _update_item(self):
        """One menu item for updates: it checks until a newer release is known, then upgrades to it."""
        busy = self._is_busy(("update",))
        if not self._update:
            return {"label": "확인 중…" if busy else f"최신 버전 확인 (현재 {__version__})",
                    "enabled": not busy, "command": self._check_update}
        return {"label": "업데이트 받는 중…" if busy else f"업그레이드 ({__version__} → {self._update['version']})",
                "tooltip": ("설치 파일을 받아 검증한 뒤 그 자리에서 설치합니다" if updater.supported()
                            else "소스 실행 중이라 GitHub 릴리스 페이지를 엽니다"),
                "enabled": not busy, "command": self._install_update}

    def _install_update(self):
        release = self._update
        if not release or self._is_busy(("update",)):
            return
        if not updater.supported():
            # A source checkout (or a non-Windows run) cannot replace itself.
            if self._dialog("업그레이드", f"handback {release['version']}이(가) 나왔습니다 (현재 {__version__}).\n"
                            "소스 체크아웃으로 실행 중이라 자동 설치는 하지 않고 GitHub 릴리스 페이지를 엽니다.",
                            confirm=True):
                webbrowser.open(release["page"])
            return
        if not self._dialog(
                "업그레이드", f"handback {release['version']}(으)로 업그레이드합니다 (현재 {__version__}).\n"
                f"설치 파일을 받아 SHA-256을 확인한 뒤 설치합니다. 현황판이 잠시 닫혔다가 다시 열립니다.",
                confirm=True):
            return

        def done(path, error):
            if error:
                self._dialog("업데이트", "업데이트하지 못했습니다 — " + " ".join(ui_error(error).split())[:80])
                return
            try:
                updater.run_installer(path)
            except OSError as failure:
                self._dialog("업데이트", "설치 파일을 실행하지 못했습니다 — " + str(failure)[:80])
                return
            self.quit()  # the installer replaces these files, then relaunches the dashboard
        # The installed dashboard starts with home=None; Path(None) raised inside the Tk callback, so
        # "예" silently did nothing.
        folder = Path(self.home or state_home()) / "updates"
        self._run_async(("update",), lambda: updater.download(release, folder), done)

    def _compat_timer(self):
        self._check_compat(manual=False)
        self.root.after(COMPAT_INTERVAL_MS, self._compat_timer)

    def _check_compat(self, manual=True):
        """Detect app updates and run the read-only compatibility check (worker thread)."""
        if self._is_busy(("compat",)):
            return

        def done(results, error):
            try:
                self._compat_failures = compat.failures(self.home)
            except (OSError, ValueError):
                pass
            if error:
                if manual:
                    self._dialog("앱 호환성 점검", "점검하지 못했습니다 — " + " ".join(ui_error(error).split())[:80])
                return
            if manual:
                lines = [compat.headline(r) for r in results] or ["확인된 앱이 없습니다"]
                if any(not r["ok"] for r in results):
                    lines.append("\n메뉴의 '⚠ … 진단 요약'에서 실패 항목을 복사할 수 있습니다.")
                self._dialog("앱 호환성 점검", "\n".join(lines))
                return
            for result in results:
                if result["notify"] and self.tray is not None:
                    self.tray.balloon("handback", compat.headline(result) +
                                      ("" if result["ok"] else "\n클릭하면 진단 요약을 엽니다"),
                                      warning=not result["ok"])
        self._run_async(("compat",), lambda: compat.scan(self.home, force=manual), done)

    def _compat_report(self, result):
        """Copyable diagnostic summary of a failed check; the user decides whether to file it."""
        self.tooltip.hide()
        self._close_menu()
        tk = self.tk
        window = tk.Toplevel(self.root)
        window.title(compat.issue_title(result))
        window.attributes("-topmost", True)
        window.geometry("720x480")
        window.configure(bg=self.BG)
        report = compat.summary(result)
        footer = tk.Frame(window, bg=self.BG, padx=8, pady=6)
        footer.pack(side="bottom", fill="x")
        status = tk.StringVar(window, "GitHub 이슈는 자동으로 올리지 않습니다. 내용을 확인한 뒤 붙여 넣으세요.")

        def copy():
            window.clipboard_clear()
            window.clipboard_append(report)
            status.set("진단 요약을 클립보드에 복사했습니다.")

        def issue():
            from urllib.parse import quote as url_quote
            copy()
            webbrowser.open(compat.ISSUE_URL + "?title=" + url_quote(compat.issue_title(result)))
            status.set("복사했습니다. 열린 이슈 페이지의 본문에 붙여 넣으세요.")

        self._button(footer, "닫기", window.destroy).pack(side="right", padx=(8, 0))
        self._button(footer, "GitHub 이슈 열기", issue).pack(side="right", padx=(8, 0))
        self._button(footer, "요약 복사", copy).pack(side="right")
        tk.Label(footer, textvariable=status, bg=self.BG, fg=self.DIM, font=self.font).pack(side="left")
        text = tk.Text(window, wrap="word", bg=self.BG, fg=self.FG, insertbackground=self.FG,
                       font=("Consolas", 10), padx=12, pady=10, bd=0)
        text.pack(fill="both", expand=True)
        text.insert("1.0", report)
        text.configure(state="disabled")
        window.bind("<Escape>", lambda event: window.destroy())
        _dark_titlebar(window, self.theme == "dark")
        return window

    def _open_setup(self):
        from . import setup_gui
        self._close_menu()
        setup_gui.open_setup(self)

    def _auto_setup(self):
        from . import setup_gui
        if setup_gui.needs_setup():
            setup_gui.open_setup(self)

    def _hooks(self, action):
        verb = "추가합니다" if action == "install" else "제거합니다"
        paths = "\n".join(hook_settings_paths())
        if not self._dialog(
                "Hook " + ("설치" if action == "install" else "해제"),
                f"Claude·Codex 설정에서 handback hook을 {verb}. 기존 설정은 백업됩니다.\n\n"
                f"설정 파일:\n{paths}\n백업 위치:\n{hook_backup_dir(self.home)}", confirm=True):
            return
        try:
            backups = run_hooks(action, self.home)
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            self._dialog("Hook", "실패 — " + " ".join(ui_error(error).split())[:80])
            return
        done = "설치했습니다" if action == "install" else "해제했습니다"
        self._dialog("Hook", done + ("\n백업: " + "\n".join(backups) if backups else ""))

    def _role_items(self, project, data):
        lead = data.get("lead", "")
        lead_agent = lead.partition(":")[0]
        workers = list(data.get("workers", []))
        items = [{"label": "리드", "enabled": False}]
        for agent in config.AGENTS:
            current = agent == lead_agent
            usable = agent == "claude" or current
            items.append({"label": ("✓ " if current else "    ") + agent_name(agent),
                          "enabled": usable and not current,
                          "tooltip": "" if usable else "Codex·Antigravity 리드는 해당 앱 대화에서 직접 지정합니다",
                          "command": lambda agent=agent: self._change_roles(
                              project, agent + ":lead", workers)})
        items.extend([None, {"label": "워커", "enabled": False}])
        for agent in ("codex", "antigravity"):
            chosen = [w for w in config.AGENTS if (w in workers) != (w == agent)]
            items.append({"label": ("✓ " if agent in workers else "    ") + agent_name(agent),
                          "command": lambda chosen=chosen: self._change_roles(project, lead, chosen)})
        return items

    def _change_roles(self, project, lead, workers):
        data = next((row for row in self._rows if row["root"] == project), {})
        old_lead, old_workers = data.get("lead", ""), list(data.get("workers", []))
        if data.get("running"):
            self._dialog("역할 지정", "진행 중인 작업이 있어 바꿀 수 없습니다")
            return
        over = role_env_override()
        if over:
            self._dialog("역할 지정", "환경변수 " + "·".join(over) + "가 설정돼 있어 바꿀 수 없습니다 — "
                         "Remove-Item Env:HANDBACK_LEAD, Env:HANDBACK_WORKERS")
            return
        old_name, new_name = old_lead.partition(":")[0], lead.partition(":")[0]
        summary = role_summary(old_name, old_workers, new_name, workers)
        if not summary:
            return
        if not self._dialog("역할 지정", summary + " 로 바꿉니다", confirm=True):
            return
        try:
            fallback = config.resolve(project, home=self.home, validate=False)["values"].get("fallback", "ask")
            config.use_topology(project, lead, workers, fallback, home=self.home)
        except (OSError, ValueError, TimeoutError) as error:
            self._dialog("역할 지정", "실패 — " + " ".join(ui_error(error).split())[:80])
            return
        self.refresh(reschedule=False)
        self._dialog("역할 지정", "역할을 바꿨습니다")

    def _connection_test(self, project):
        from .state import ProjectState
        key = ("try", project)
        if self._is_busy(key):
            return
        data = next((row for row in self._rows if row["root"] == project), {})
        if data.get("running"):
            self._dialog("연결 테스트", "진행 중인 작업이 있어 지금은 할 수 없습니다")
            return
        try:
            saved = ProjectState(project, home=self.home).read_json("topology.json", {})
        except (OSError, ValueError):
            saved = {}
        message = "Codex 워커가 실제로 1회 실행됩니다 (사용량 발생)"
        if not saved:
            message += "\n역할이 Lead=Claude, Workers=Codex로 저장됩니다"
        if not self._dialog("연결 테스트", message, confirm=True):
            return

        def done(result, error):
            if error:
                self._dialog("연결 테스트", "실패 — " + " ".join(ui_error(error).split())[:80])
            else:
                ok, seconds, reason = result
                self._dialog("연결 테스트", f"연결 정상 ({seconds}초)" if ok else f"실패 — {reason}")
            self.refresh(reschedule=False)
        self._run_async(key, lambda: run_connection_test(project), done)

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
        self._cancel_settle()
        if self.mode == "panel" and self._fixed_viewport is not None:
            if self.anchor is None:
                prefs = load_prefs(self.home)
                if prefs["panel_x"] is not None and prefs["panel_y"] is not None:
                    self.anchor = (prefs["panel_x"], prefs["panel_y"])
            point = ((self.anchor[0] - 1, self.anchor[1] - 1) if self.anchor is not None
                     else (self.root.winfo_x(), self.root.winfo_y()))
            area = _monitor_area(self.root, *point)
            if self.anchor is None:
                self.anchor = (area[2] - 8, area[3] - 4)
            width, height = self._fixed_viewport.dimensions(area[2] - area[0], area[3] - area[1])
            info = taskbar_info(point) if self.docked and not self._dragging else None
            if taskbar_dockable(info):
                self._taskbar_hwnd = info.get("hwnd")
                full = _monitor_area(self.root, *point, full=True)
                x, y, width, height = panel_dock_geometry(info, full, width, height,
                                                          self.anchor[0] - width)
            else:
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
        self._stop_settle()
        self._status_press = None
        self._handle_press = False
        cursor = self.root.winfo_pointerxy()
        self._start = (*cursor, self.root.winfo_x(), self.root.winfo_y())
        self._dragged = False
        self._drag_from_dock = self.docked
        # A docked widget resists until it is pulled past the breakaway distance.
        self._drag_held = self.docked and self.mode == "taskbar"
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
            self._drag_poll_timer = self.root.after(dock_motion.SETTLE_FRAME_MS, self._poll_drag)
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
        if self._drag_held:
            shown, broke = dock_motion.breakaway(
                (cursor[0] - self._start[0], cursor[1] - self._start[1]),
                dock_motion.scaled(dock_motion.BREAKAWAY_DISTANCE, dpi))
            if not broke:
                width, height = self._drag_applied_geometry[2:]
                self._move_drag_window((self._start[2] + shown[0], self._start[3] + shown[1],
                                        width, height))
                return
            self._drag_held = False
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
                # _compact_geometry stays raw for the drop decision; only the shown window leans.
                self._move_drag_window(compact_magnet_geometry(info, area, geometry, cursor, dpi))
            return
        self.docked = False
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

    def _poll_drag(self):
        """Follow the pointer even when it is outside the window and Tk sends no motion.

        The magnet and breakaway move the window relative to the pointer, so the
        pointer can leave the window mid-drag; the drag must not stall there.
        """
        self._drag_poll_timer = None
        if not self._dragging:
            return
        if self._drag_timer is None and self.root.winfo_pointerxy() != self._drag_cursor:
            self._flush_drag()
        self._drag_poll_timer = self.root.after(dock_motion.SETTLE_FRAME_MS, self._poll_drag)

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
        for name in ("_drag_timer", "_drag_capture_timer", "_drag_poll_timer"):
            timer = getattr(self, name)
            if timer is not None:
                self.root.after_cancel(timer)
                setattr(self, name, None)
        drop_geometry = None
        shown = None
        if self._dragged:
            # Flush before saving, including a release ahead of the pending idle move.
            self._flush_drag(read_pointer=read_pointer)
            shown = self._drag_applied_geometry[:2]
            if self._drag_held:
                # Released before breaking away: it never left the dock.
                self._move_drag_window(self._compact_geometry)
            elif click and self.mode == "taskbar":
                info, area, dpi = self._drag_context
                docked, geometry = compact_drop_geometry(
                    info, area, *self._compact_geometry, cursor=self._drag_cursor, dpi=dpi)
                if docked:
                    self.docked = True
                    self._palette_key = None
                    drop_geometry = geometry
        status_press, self._status_press = self._status_press, None
        handle_press, self._handle_press = self._handle_press, False
        self._start = None
        self._dragging = False
        drag_context, self._drag_context = self._drag_context, None
        self._drag_held = False
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
                # The shown window may still lean toward the dock; restore the real place.
                self._move_drag_window(self._compact_geometry)
                self.root.update_idletasks()
                prefs = load_prefs(self.home)
                save_prefs({**prefs, "compact_x": self.root.winfo_x(),
                            "compact_y": self.root.winfo_y(), "docked": self.docked}, self.home)
                self._settle(shown)
            else:
                x, y = self.root.winfo_x(), self.root.winfo_y()
                width, height = self.root.winfo_width(), self.root.winfo_height()
                if click and drag_context is not None:
                    info, area, _ = drag_context
                    docked, (x, y, width, height) = panel_drop_geometry(
                        info, area, x, y, width, height, cursor=self._drag_cursor)
                    self.docked = docked
                self.anchor = (x + width, y + height)
                prefs = load_prefs(self.home)
                save_prefs({**prefs, "panel_x": self.anchor[0], "panel_y": self.anchor[1],
                            "docked": self.docked}, self.home)
                self._place()
                self._sync_zorder_timer()
                self._settle(shown)
        elif click and self.mode == "taskbar" and not handle_press:
            if status_press is not None and status_press[1] == "unread":
                self._open_unread(event, status_press[0])
            elif status_press is not None:
                self._conversations(event, self._compact_targets(*status_press))
            else:
                self._toggle_panel()

    def _settle(self, start):
        """Spring from where the drag left the window into its final place."""
        self._stop_settle()
        self.root.update_idletasks()
        frames = dock_motion.settle_frames(start, (self.root.winfo_x(), self.root.winfo_y()))
        if len(frames) < 2:
            return
        mover = _native_drag_mover(self.root)

        def move(point):
            if mover is None or not mover(*point):
                self.root.geometry(f"+{point[0]}+{point[1]}")

        self._settle_move, self._settle_frames = move, frames
        move(start)
        self._settle_timer = self.root.after(dock_motion.SETTLE_FRAME_MS, self._settle_step)

    def _settle_step(self):
        self._settle_timer = None
        if not self._settle_frames:
            return
        self._settle_move(self._settle_frames.pop(0))
        if self._settle_frames:
            self._settle_timer = self.root.after(dock_motion.SETTLE_FRAME_MS, self._settle_step)

    def _stop_settle(self):
        """Finish an unfinished settle at once, so saved and shown positions agree."""
        frames = self._settle_frames
        self._cancel_settle()
        if frames:
            self._settle_move(frames[-1])

    def _cancel_settle(self):
        """Drop a settle without moving; whoever calls this places the window itself."""
        if self._settle_timer is not None:
            self.root.after_cancel(self._settle_timer)
            self._settle_timer = None
        self._settle_frames = []

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
        self._start_tray()
        self.root.deiconify()
        if self._fullscreen_hidden or self._idle_hidden:
            show_widget_without_activation(self.root, False)
        if getattr(sys, "frozen", False):  # source/PyPI users keep the current behaviour
            self.root.after(400, self._auto_setup)
        self.root.after(UPDATE_FIRST_MS, self._update_timer)
        try:
            self._compat_failures = compat.failures(self.home)
        except (OSError, ValueError):
            pass
        self.root.after(COMPAT_FIRST_MS, self._compat_timer)
        self.root.mainloop()


def startup_shortcut():
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/handback 현황판.lnk"


def _no_window_flags():
    """Keep console children (PowerShell) from flashing a window from the windowed exe."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _write_startup_shortcut(link, target, arguments, working_directory):
    def quoted(value):
        return "'" + str(value).replace("'", "''") + "'"
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + quoted(link) + ");"
              "$s.TargetPath = " + quoted(target) + ";"
              "$s.Arguments = " + quoted(arguments) + ";"
              "$s.WorkingDirectory = " + quoted(working_directory) + ";"
              "$s.Description = 'handback 현황판';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, capture_output=True, timeout=30, creationflags=_no_window_flags())


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


def _expected_autostart_target():
    from .invocation import frozen
    if frozen():
        return Path(sys.executable).with_name("handback-dashboard.exe")
    return Path(sys.executable).with_name("pythonw.exe")


def _shortcut_target(link):
    # UTF-8 output keeps non-ASCII targets intact regardless of the console codepage.
    script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
              "(New-Object -ComObject WScript.Shell).CreateShortcut('" + str(link).replace("'", "''")
              + "').TargetPath")
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                          creationflags=_no_window_flags())
    return (done.stdout or "").replace("\ufeff", "").strip()


def repair_autostart():
    """Silently rewrite a stale Startup shortcut (moved/reinstalled launcher)."""
    if sys.platform != "win32":
        return False
    try:
        link = startup_shortcut()
        if not link.exists():
            return False
        target = _shortcut_target(link)
        expected = _expected_autostart_target()
        same = bool(target) and os.path.normcase(os.path.normpath(target)) == \
            os.path.normcase(os.path.normpath(str(expected)))
        if same and Path(target).exists():
            return False
        set_autostart(True)
        return True
    except (OSError, RuntimeError, KeyError, subprocess.SubprocessError, UnicodeDecodeError, ValueError):
        return False


# ---- right-click menu helpers (pure logic; Strip wires them to dialogs) ----
HOOK_AGENTS = ("claude", "codex")
ROLE_MIGRATION_ENV = ("HANDBACK_LEAD", "HANDBACK_WORKERS")
_SETUP_FIX = "우클릭 › 설정"


def _problem_line(row):
    name, detail = row.get("check", ""), str(row.get("detail", ""))
    if name.endswith(" version") or name == "Claude Monitor":
        return None  # informational only
    if name == "Python":
        return "Python 버전이 낮음 — 3.11 이상을 설치하세요"
    if name == "State home":
        return "상태 폴더에 쓸 수 없음 — HANDBACK_HOME 폴더 권한을 확인하세요"
    if name in ("Codex executable", "Codex queue --thread"):
        return "Codex 실행 파일을 쓸 수 없음 — Codex 앱을 설치·업데이트하세요"
    if name.startswith("Antigravity hooks"):
        return "Antigravity hook 없음 — handback install-hooks --agents antigravity"
    if name == "Antigravity":
        return "Antigravity 실행 파일 없음 — Antigravity를 설치하세요"
    if name.endswith(" skill"):
        who = agent_name(name.split()[0])
        kind = ("스킬 없음" if detail.startswith("missing") else "예전 스킬만 있음"
                if detail.startswith("legacy") else "스킬이 다른 설치를 가리킴")
        return f"{who} {kind} — {_SETUP_FIX}"
    if name == "Topology":
        return "역할 설정 문제 — 프로젝트 우클릭 › 역할 지정"
    if name.endswith(" recovery hooks"):
        return f"{agent_name(name.split()[0])} 복구 훅 없음 — {_SETUP_FIX}"
    if name.startswith("Collector "):
        return "결과 수집이 멈춤 — 잠시 후 다시 점검하세요"
    if name == "Lead delivery":
        return "전달 안 된 결과 있음 — 프로젝트 우클릭 › 미확인 결과 확인"
    if name.startswith("State warning"):
        return "상태 폴더 경고 — handback doctor로 자세히 확인하세요"
    return f"{name} 문제 — handback doctor로 확인하세요"


def problem_lines(rows):
    lines = []
    for row in rows:
        if row.get("level") == "OK":
            continue
        line = _problem_line(row)
        if line and line not in lines:
            lines.append(line)
    return lines


def doctor_problems(root, home=None, adapter_for=None):
    """Same checks as `handback doctor`, returned as Korean one-line problems."""
    from . import diagnostics
    from .state import ProjectState
    if adapter_for is None:
        from .cli import get_adapter as adapter_for
    state = ProjectState(root, home=home)
    resolved = config.resolve(root, home=state.home, validate=False)
    values = resolved["values"]
    agents = [adapter_for(a, values).detect() for a in ("codex", "claude", "antigravity")
              if a != "antigravity" or values["agents"][a]["enabled"]]
    return problem_lines(diagnostics.checklist(state, resolved, agents))


def hook_settings_paths():
    from . import hook_install
    return [str(hook_install._config_path(agent, None)) for agent in HOOK_AGENTS]


def hook_backup_dir(home=None):
    return Path(home or state_home()) / "hook-install" / "backups"


def run_hooks(action, home=None, config_paths=None):
    """install/uninstall Claude+Codex hooks; returns backup paths written."""
    from . import hook_install
    run = hook_install.install if action == "install" else hook_install.uninstall
    result = run(list(HOOK_AGENTS), home=home, config_paths=config_paths)
    return [change["backup"] for change in result.get("changes", []) if change.get("backup")]


def run_connection_test(root):
    """Same path as `handback try --root <root> --json`, run as a child process.

    A subprocess keeps global stdout untouched (the windowed exe has none).
    Returns (ok, seconds, reason).
    """
    from .invocation import self_argv
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        done = subprocess.run([*self_argv(), "try", "--root", str(root), "--json"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env={**os.environ, "PYTHONUTF8": "1"}, creationflags=flags,
                              stdin=subprocess.DEVNULL, timeout=330)
    except subprocess.TimeoutExpired:
        return False, 330, "시간 초과"
    try:
        report = json.loads(done.stdout)
    except ValueError:
        report = {}
    seconds = round(float(report.get("elapsed_seconds") or 0))
    if done.returncode == 0:
        return True, seconds, ""
    reason = (report.get("error") or report.get("status") or (done.stderr or "").strip()
              or f"코드 {done.returncode}")
    return False, seconds, " ".join(str(reason).split())[:80]


def role_env_override():
    return [name for name in ROLE_MIGRATION_ENV if os.environ.get(name)]


def role_summary(old_lead, old_workers, new_lead, new_workers):
    parts = []
    if old_lead != new_lead:
        parts.append(f"Lead: {old_lead} → {new_lead}")
    if list(old_workers) != list(new_workers):
        parts.append(f"Workers: {', '.join(old_workers) or '없음'} → {', '.join(new_workers) or '없음'}")
    return ", ".join(parts)


_KERNEL32 = None


def _kernel32():
    """kernel32 with per-thread last-error capture (windll's GetLastError can be stale)."""
    global _KERNEL32
    if _KERNEL32 is None:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateEventW.restype = wintypes.HANDLE
        kernel.SetEvent.argtypes = [wintypes.HANDLE]
        kernel.SetEvent.restype = wintypes.BOOL
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        _KERNEL32 = kernel
    return _KERNEL32


def _single_instance():
    """("primary", (mutex, show_event)) or ("secondary", None) for this user session."""
    if sys.platform != "win32":
        return "primary", (None, None)
    import ctypes
    try:
        kernel = _kernel32()
        # One strip per user session: autostart plus a manual launch must not stack.
        mutex = kernel.CreateMutexW(None, False, DASHBOARD_MUTEX)
        already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        return "primary", (None, None)
    if mutex and already:
        kernel.CloseHandle(mutex)
        return "secondary", None
    try:
        # Auto-reset, initially clear: a later launch sets it to ask us to show.
        event = kernel.CreateEventW(None, False, False, DASHBOARD_SHOW_EVENT)
    except (AttributeError, OSError):
        event = None
    return "primary", (mutex, event or None)


def signal_running_instance():
    """Ask the running dashboard to come to the front; False when it cannot be asked."""
    if sys.platform != "win32":
        return False
    import ctypes
    try:
        kernel = _kernel32()
        # Opens the primary's event (or creates an orphan nobody waits on: harmless).
        event = kernel.CreateEventW(None, False, False, DASHBOARD_SHOW_EVENT)
    except (AttributeError, OSError):
        return False
    if not event:
        return False
    try:
        try:
            # This launch owns the user's foreground right; let the primary use it.
            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except (AttributeError, OSError):
            pass
        return bool(kernel.SetEvent(event))
    except (AttributeError, OSError):
        return False
    finally:
        kernel.CloseHandle(event)


def show_requested(handle):
    """Non-blocking check of the show event; True once per signal (auto-reset)."""
    if sys.platform != "win32" or not handle:
        return False
    try:
        return _kernel32().WaitForSingleObject(handle, 0) == 0  # WAIT_OBJECT_0
    except (AttributeError, OSError):
        return False


def main(home=None):
    role, handles = _single_instance()
    if role == "secondary":
        signal_running_instance()  # on failure this stays the old silent exit
        return 0
    mutex, show_event = handles  # keep the mutex handle alive for the process lifetime
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
        set_app_user_model_id()
    import threading
    threading.Thread(target=repair_autostart, daemon=True).start()
    strip = Strip(home)
    strip.watch_show_requests(show_event)
    strip.run()
