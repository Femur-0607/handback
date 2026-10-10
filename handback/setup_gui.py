"""First-run setup window for people who double-click handback-dashboard.exe."""
import sys

from . import setup_wizard
from .invocation import frozen

NEED_CLAUDE = "Claude가 필요합니다. 설치·로그인 후 다시 여세요."
RISKY = "권장: 공백 없는 고정 폴더(예: C:\\handback)로 옮긴 뒤 다시 실행하세요."
NEXT_STEP = "다음: Claude에서 /handback 스킬로 작업을 맡기세요."


def needs_setup():
    """True when the Claude skill is missing or points at another installation."""
    try:
        from . import diagnostics
        return diagnostics.skill_status("claude") != "points at this installation"
    except Exception:
        return False


def app_lines(found):
    lines = []
    for agent, label, required in setup_wizard.APPS:
        if found.get(agent):
            lines.append(f"✓ {label}")
        else:
            lines.append(f"– {label} 없음" + ("" if required else " (선택)"))
    return lines


def run_install(skills, autostart, install=None, set_autostart=None):
    """Return result lines; raises on failure. Runs off the Tk thread."""
    if install is None:
        from .skill_install import install
    if set_autostart is None:
        from .dashboard import set_autostart
    lines = []
    if skills:
        lines.extend(setup_wizard._skill_lines(install(None, False)))
    if autostart:
        set_autostart(True)
        lines.append("✓ 시작 시 자동 실행")
    return lines


def open_setup(strip, detect=None, install=None, set_autostart=None):
    """Show (or raise) the setup window; the dashboard keeps running when it closes."""
    from . import dashboard
    existing = getattr(strip, "_setup_window", None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return existing
    detect = detect or setup_wizard.detect_apps
    tk = strip.tk
    window = tk.Toplevel(strip.root)
    window.withdraw()
    window.title("handback 설정")
    window.configure(bg=strip.BG)
    window.resizable(False, False)
    strip._setup_window = window
    if strip.icons:
        try:
            window.iconphoto(False, *strip.icons)
        except tk.TclError:
            pass
    bg, fg, dim = strip.BG, strip.FG, strip.DIM
    body = tk.Frame(window, bg=bg, padx=22, pady=16)
    body.pack(fill="both")

    def label(text, color=fg, font=("Segoe UI", 10), pady=1):
        item = tk.Label(body, text=text, bg=bg, fg=color, font=font, justify="left",
                        anchor="w", wraplength=380)
        item.pack(fill="x", pady=pady)
        return item

    label("handback 설정", font=("Segoe UI Semibold", 13), pady=(0, 8))
    apps_box = tk.Frame(body, bg=bg)
    apps_box.pack(fill="x")
    status = label("앱 확인 중…", dim)
    state = {"claude": False, "done": False}
    skills_on, auto_on = tk.BooleanVar(window, True), tk.BooleanVar(window, False)

    risky_text = RISKY if frozen() and setup_wizard.risky_location() else ""
    if risky_text:
        label(risky_text, strip.YELLOW, pady=(8, 0))
    options = tk.Frame(body, bg=bg)
    options.pack(fill="x", pady=(10, 0))
    body_options = [tk.Checkbutton(options, text="에이전트 스킬 설치", variable=skills_on, bg=bg, fg=fg,
                                   selectcolor="#3a3a3a", activebackground=bg, activeforeground=fg,
                                   font=("Segoe UI", 10), anchor="w", bd=0, highlightthickness=0,
                                   cursor="hand2")]
    if sys.platform == "win32":
        body_options.append(tk.Checkbutton(options, text="Windows 시작 시 실행", variable=auto_on, bg=bg,
                                           fg=fg, selectcolor="#3a3a3a", activebackground=bg,
                                           activeforeground=fg, font=("Segoe UI", 10), anchor="w",
                                           bd=0, highlightthickness=0, cursor="hand2"))
    for box in body_options:
        box.pack(fill="x", pady=1)
    result_box = tk.Frame(body, bg=bg)
    result_box.pack(fill="x", pady=(8, 0))
    footer = tk.Frame(window, bg=bg, padx=18, pady=12)
    footer.pack(fill="x")
    close = strip._button(footer, "닫기", window.destroy)
    close.pack(side="right", padx=(8, 0))
    go = strip._button(footer, "설치", lambda: start())
    go.pack(side="right")
    go.configure(state="disabled")

    def fit():
        """A fixed-size window must be re-fitted by hand when its content changes."""
        if not window.winfo_exists():
            return
        window.update_idletasks()
        window.geometry(f"{max(window.winfo_reqwidth(), 440)}x{window.winfo_reqheight()}")

    def show_results(lines, color=fg):
        for child in result_box.winfo_children():
            child.destroy()
        for line in lines:
            tk.Label(result_box, text=line, bg=bg, fg=color, font=("Segoe UI", 10), justify="left",
                     anchor="w", wraplength=380).pack(fill="x", pady=1)
        fit()

    def detected(found, error):
        if not window.winfo_exists():
            return
        found = found if isinstance(found, dict) and not error else {}
        status.destroy()
        for line in app_lines(found):
            tk.Label(apps_box, text=line, bg=bg, fg=fg, font=("Segoe UI", 10), anchor="w").pack(fill="x")
        state["claude"] = bool(found.get("claude"))
        fit()
        if state["claude"]:
            go.configure(state="normal")
        else:
            show_results([NEED_CLAUDE], strip.YELLOW)

    def installed(lines, error):
        if not window.winfo_exists():
            return
        if error:
            go.configure(state="normal")
            show_results(["실패: " + setup_wizard._reason(error)], strip.YELLOW)
            return
        state["done"] = True
        show_results([*lines, NEXT_STEP])
        close.configure(text="완료")

    def start():
        if state["done"] or not state["claude"]:
            return
        go.configure(state="disabled")
        show_results(["설치 중…"], dim)
        skills, auto = skills_on.get(), auto_on.get()
        strip._run_async(("setup-install",),
                         lambda: run_install(skills, auto, install, set_autostart), installed)

    window.protocol("WM_DELETE_WINDOW", window.destroy)
    window.bind("<Escape>", lambda event: window.destroy())
    window.update_idletasks()
    width, height = max(window.winfo_reqwidth(), 440), window.winfo_reqheight() + 90
    x = (window.winfo_screenwidth() - width) // 2
    y = (window.winfo_screenheight() - height) // 3
    window.geometry(f"{width}x{height}+{x}+{y}")
    window.deiconify()
    try:
        window.wait_visibility()
        dashboard._dark_titlebar(window)
    except tk.TclError:
        pass
    strip._run_async(("setup-detect",), detect, detected)
    return window
