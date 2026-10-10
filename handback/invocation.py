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


def self_argv(script=None):
    """Interpreter plus entry arguments that re-launch this installation."""
    if frozen():
        return [sys.executable]
    return [sys.executable, "-X", "utf8", *entry_args(script)]


def command_text(*arguments):
    argv = [*self_argv(), *arguments]
    if sys.platform == "win32":
        return "& " + " ".join("'" + part.replace("'", "''") + "'" for part in argv)
    return shlex.join(argv)
