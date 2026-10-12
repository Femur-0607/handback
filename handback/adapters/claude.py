"""Claude Lead inbox capability; Desktop worker creation remains unverified."""
import os
import shutil
import subprocess

from .base import BaseAdapter
from ..discovery import claude_desktop_app


class ClaudeAdapter(BaseAdapter):
    name = "claude"

    def __init__(self, executable=None):
        self.executable = executable

    def detect(self):
        executable = self.executable or os.environ.get("HANDBACK_CLAUDE") or shutil.which("claude")
        version = None
        warnings = ["Claude worker creation and Desktop hook execution are unverified; Lead inbox only"]
        if executable:
            try:
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                result = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=15, **options)
                if result.returncode == 0:
                    version = result.stdout.strip()
                else:
                    warnings.append("Claude --version failed")
            except (OSError, subprocess.TimeoutExpired) as exc:
                warnings.append(str(exc))
        # The desktop app's Code tab is a full Lead; the CLI is optional.
        desktop = None if version else claude_desktop_app()
        if desktop:
            warnings.append(f"Claude CLI not found; using the Claude desktop app ({desktop})")
        return {"agent": self.name, "installed": bool(version or desktop), "executable": executable,
                "desktop_app": desktop, "version": version,
                "warnings": warnings, "capabilities": {"lead": True, "worker": False, "inbox": True,
                                                        "hooks_verified": False}}

    def parse_hook(self, event, stdin_json):
        event = event.lower().replace("_", "")
        return {"thread": stdin_json.get("session_id"), "turn": stdin_json.get("prompt_id"),
                "text": (stdin_json.get("last_assistant_message") if event == "stop" else stdin_json.get("prompt")) or "",
                "outcome": "completed" if event == "stop" else "failed" if event == "stopfailure" else "submitted"}
