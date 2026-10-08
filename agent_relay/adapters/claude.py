"""Claude Lead inbox capability; Desktop worker creation remains unverified."""
import os
import shutil
import subprocess

from .base import BaseAdapter


class ClaudeAdapter(BaseAdapter):
    name = "claude"

    def __init__(self, executable=None):
        self.executable = executable

    def detect(self):
        executable = self.executable or os.environ.get("AGENT_RELAY_CLAUDE") or shutil.which("claude")
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
        return {"agent": self.name, "installed": bool(version), "executable": executable, "version": version,
                "warnings": warnings, "capabilities": {"lead": True, "worker": False, "inbox": True,
                                                        "hooks_verified": False}}

    def parse_hook(self, event, stdin_json):
        event = event.lower().replace("_", "")
        return {"thread": stdin_json.get("session_id"), "turn": stdin_json.get("prompt_id"),
                "text": (stdin_json.get("last_assistant_message") if event == "stop" else stdin_json.get("prompt")) or "",
                "outcome": "completed" if event == "stop" else "failed" if event == "stopfailure" else "submitted"}
