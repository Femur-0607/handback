"""Launch the same installation from any working directory."""
from pathlib import Path
import shlex
import sys


def entry_args(script=None):
    # A source checkout need not be pip-installed; its absolute script anchors
    # imports even when a hook or detached process runs in a different project.
    source = Path(script) if script is not None else Path(__file__).resolve().parent.parent / "handback.py"
    return [str(source)] if source.is_file() else ["-m", "handback"]


def command_text(*arguments):
    argv = [sys.executable, "-X", "utf8", *entry_args(), *arguments]
    if sys.platform == "win32":
        return "& " + " ".join("'" + part.replace("'", "''") + "'" for part in argv)
    return shlex.join(argv)
