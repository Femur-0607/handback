"""Launch the same installation from any working directory."""
from pathlib import Path
import shlex
import sys


def frozen():
    """True inside a PyInstaller bundle, where sys.executable is handback.exe itself."""
    return bool(getattr(sys, "frozen", False))


def entry_args(script=None):
    if frozen():
        return []
    # A source checkout need not be pip-installed; its absolute script anchors
    # imports even when a hook or detached process runs in a different project.
    source = Path(script) if script is not None else Path(__file__).resolve().parent.parent / "handback.py"
    return [str(source)] if source.is_file() else ["-m", "handback"]


def _frozen_cli():
    """The console handback.exe, even when running inside the windowed dashboard exe."""
    return Path(sys.executable).with_name("handback.exe")


def self_argv(script=None):
    """Interpreter plus entry arguments that re-launch this installation."""
    if frozen():
        current = Path(sys.executable)
        if current.name.lower() == "handback.exe":
            return [sys.executable]
        cli = _frozen_cli()
        if not cli.is_file():
            raise RuntimeError(f"handback.exe를 찾을 수 없습니다: {cli}")
        return [str(cli)]
    return [sys.executable, "-X", "utf8", *entry_args(script)]


def command_text(*arguments):
    argv = [*self_argv(), *arguments]
    if sys.platform == "win32":
        return "& " + " ".join("'" + part.replace("'", "''") + "'" for part in argv)
    return shlex.join(argv)
