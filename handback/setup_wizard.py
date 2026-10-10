"""Interactive first-run setup for people who double-click handback.exe."""
import subprocess
import sys
from pathlib import Path

from . import __version__
from .invocation import frozen, self_argv

APPS = (("claude", "Claude", True), ("codex", "Codex", False), ("antigravity", "Antigravity", False))


def _reason(error):
    text = " ".join(str(error).split()) or type(error).__name__
    return text[:80]


def detect_apps():
    """Return {agent: installed} using the same adapters as doctor."""
    from . import config
    from .cli import get_adapter
    values = config.resolve(".", validate=False)["values"]
    found = {}
    for agent, _, _ in APPS:
        try:
            found[agent] = bool(get_adapter(agent, values).detect().get("installed"))
        except Exception:
            found[agent] = False
    return found


def _skill_lines(messages):
    lines = []
    for message in messages:
        if message.startswith(("installed:", "dry-run:")):
            dry = message.startswith("dry-run:")
            path = message.split(":", 1)[1].replace("\\", "/")
            name = "Claude" if ".claude/" in path else "Codex" if ".codex/" in path else \
                "Antigravity" if "antigravity" in path.lower() or ".gemini" in path else \
                message.split(":", 1)[1].split("->")[0].strip().capitalize()
            lines.append(f"✓ {name} 스킬 " + ("설치 예정" if dry else "설치됨"))
        elif message.startswith("skip:"):
            detail = message[5:].strip().lower()
            name = next((label for agent, label, _ in APPS if agent in detail), "")
            lines.append(f"– {name} 스킬 건너뜀".replace("–  ", "– "))
    return lines


def risky_location(path=None):
    """True when the install path is likely to move or break embedded absolute paths."""
    text = str(path or sys.executable)
    parts = {part.lower() for part in Path(text).parts}
    return " " in text or bool(parts & {"downloads", "desktop", "temp", "tmp"})


def _launch_dashboard():
    if frozen():
        target = Path(sys.executable).with_name("handback-dashboard.exe")
        if not target.exists():
            raise RuntimeError("handback-dashboard.exe가 없습니다")
        argv = [str(target)]
    else:
        argv = [*self_argv(), "dashboard"]
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    subprocess.Popen(argv, **options)


def _console(stream):
    """True only when the stream is a real Windows console (not NUL, a pipe or a file)."""
    import ctypes
    import msvcrt
    handle = msvcrt.get_osfhandle(stream.fileno())
    return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(ctypes.c_uint32())))


def _interactive():
    try:
        if sys.platform == "win32":
            return bool(sys.stdin and sys.stdout and _console(sys.stdin) and _console(sys.stdout))
        return bool(sys.stdin and sys.stdout and sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def _ask(prompt, default, input_fn):
    try:
        answer = input_fn(prompt + (" [Y/n] " if default else " [y/N] ")).strip().lower()
    except EOFError:
        return default
    return default if not answer else answer.startswith(("y", "예", "네", "ㅇ"))


def run(yes=False, dry_run=False, input_fn=input, interactive=None, detect=detect_apps,
        install=None, set_autostart=None, launch=_launch_dashboard, out=print):
    """Run the wizard; return an exit code."""
    if interactive is None:
        interactive = _interactive()
    if install is None:
        from .skill_install import install
    if set_autostart is None:
        from .dashboard import set_autostart
    out(f"handback {__version__} 설정")
    try:
        found = detect()
    except Exception as error:
        out(f"실패: 앱 확인 ({_reason(error)})")
        found = {}
    for agent, label, required in APPS:
        if found.get(agent):
            out(f"✓ {label}")
        else:
            out(f"– {label} 없음" + ("" if required else " (선택)"))
    if not found.get("claude") and not dry_run:
        out("Claude가 필요합니다. 먼저 Claude를 설치하고 로그인한 뒤 다시 실행하세요.")
        _pause(interactive, input_fn, out)
        return 1
    prompting = interactive and not yes and not dry_run
    if frozen() and risky_location():
        out("권장: 공백 없는 고정 폴더(예: C:\\handback)로 옮긴 뒤 다시 실행하세요.")
        if prompting and not _ask("그래도 계속할까요?", False, input_fn):
            _pause(True, input_fn, out)
            return 1
    plan_only = dry_run or (not interactive and not yes)
    if plan_only:
        out("변경 없이 계획만 표시합니다." + ("" if dry_run else " 적용하려면 --yes를 붙이세요."))
    code = 2 if plan_only and not dry_run else 0
    do_skills = _ask("에이전트 스킬을 설치할까요?", True, input_fn) if prompting else True
    do_auto = (_ask("Windows 시작 시 현황판을 실행할까요?", False, input_fn)
               if prompting and sys.platform == "win32" else False)
    do_open = _ask("지금 현황판을 열까요?", True, input_fn) if prompting else False
    if do_skills:
        try:
            for line in _skill_lines(install(None, plan_only)):
                out(line)
        except Exception as error:
            out(f"실패: 스킬 설치 ({_reason(error)})")
            code = 1
    if do_auto:
        try:
            set_autostart(True)
            out("✓ 시작 시 자동 실행")
        except Exception as error:
            out(f"실패: 자동 실행 ({_reason(error)})")
            code = 1
    elif plan_only and sys.platform == "win32":
        out("– 시작 시 자동 실행은 묻고 설정합니다" if dry_run else "– 시작 시 자동 실행 안 함")
    if do_open:
        try:
            launch()
            out("✓ 현황판 열기")
        except Exception as error:
            out(f"실패: 현황판 열기 ({_reason(error)})")
            code = 1
    if not plan_only:
        out("다음: Claude에서 /handback 스킬로 작업을 맡기세요.")
        out("업데이트: 현황판을 닫고 새 파일로 덮어쓰세요.")
    _pause(interactive and not yes and not dry_run, input_fn, out)
    return code


def _pause(interactive, input_fn, out):
    if not interactive:
        return
    try:
        input_fn("Enter를 누르면 닫힙니다.")
    except EOFError:
        pass
