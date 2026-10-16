"""App update detection and the read-only compatibility check (compat.py)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from handback import compat, discovery, tray

PASS = [{"check": "agentapi --help", "ok": True, "detail": "ok"}]
FAIL = [{"check": "agentapi --help", "ok": True, "detail": "ok"},
        {"check": "sidecar registry schema", "ok": False, "detail": "unexpected: x"}]


def checks(rows, calls=None):
    def run():
        if calls is not None:
            calls.append(1)
        return rows
    return {agent: run for agent in compat.AGENTS}


class VersionDetectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.logs = Path(temporary.name)

    def test_main_log_reports_the_latest_launch_in_either_format(self):
        (self.logs / "main.log").write_text(
            "Spawning: language_server.exe --override_ide_version 2.15.1 --x\n"
            "[2026-10-16] [info]  Starting app (v2.23.0) with dynamic port\n"
            "[2026-10-16] [info]  Starting app (v2.23.1) with dynamic port\n", encoding="utf-8")
        self.assertEqual(discovery.antigravity_log_version(self.logs), "2.23.1")

    def test_older_override_flag_is_read(self):
        (self.logs / "main.log").write_text("x --override_ide_version 2.15.1 --y\n", encoding="utf-8")
        self.assertEqual(discovery.antigravity_log_version(self.logs), "2.15.1")

    def test_language_server_log_is_the_fallback(self):
        (self.logs / "language_server.log").write_text("I1016 Language server version: 2.23.1\n", encoding="utf-8")
        self.assertEqual(discovery.antigravity_log_version(self.logs), "2.23.1")

    def test_no_logs_reads_as_unknown(self):
        self.assertIsNone(discovery.antigravity_log_version(self.logs))

    def test_installed_executable_wins_over_logs(self):
        (self.logs / "main.log").write_text("Starting app (v2.15.1)\n", encoding="utf-8")
        with patch.object(discovery, "antigravity_executable", return_value="Antigravity.exe"), \
                patch.object(discovery, "file_version", return_value="2.23.1"):
            self.assertEqual(discovery.antigravity_version(self.logs), "2.23.1")
        with patch.object(discovery, "antigravity_executable", return_value=None):
            self.assertEqual(discovery.antigravity_version(self.logs), "2.15.1")

    def test_file_version_of_a_missing_file_is_none(self):
        self.assertIsNone(discovery.file_version(self.logs / "missing.exe"))

    def test_prerelease_suffix_is_part_of_the_version(self):
        self.assertEqual(compat.number("codex-cli 0.163.0-alpha.10.2"), "0.163.0-alpha.10.2")
        self.assertEqual(compat.number("2.1.292 (Claude Code)"), "2.1.292")

    def test_one_failing_reader_does_not_hide_the_others(self):
        def broken():
            raise OSError("boom")
        versions = compat.current_versions({"codex": broken, "antigravity": lambda: "2.23.1"})
        self.assertEqual(versions, {"codex": None, "antigravity": "2.23.1"})


class ScanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)

    def test_first_sighting_that_passes_is_recorded_quietly(self):
        results = compat.scan(self.home, {"antigravity": "2.15.1"}, checks(PASS))
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["notify"])
        self.assertEqual(compat.load_record(self.home)["antigravity"]["verified"], "2.15.1")

    def test_same_version_is_not_rechecked(self):
        compat.scan(self.home, {"antigravity": "2.15.1"}, checks(PASS))
        calls = []
        self.assertEqual(compat.scan(self.home, {"antigravity": "2.15.1"}, checks(PASS, calls)), [])
        self.assertEqual(calls, [])

    def test_update_that_passes_notifies_and_becomes_verified(self):
        compat.scan(self.home, {"antigravity": "2.15.1"}, checks(PASS))
        [result] = compat.scan(self.home, {"antigravity": "2.23.1"}, checks(PASS))
        self.assertTrue(result["notify"] and result["ok"])
        self.assertEqual(result["previous"], "2.15.1")
        self.assertEqual(compat.headline(result), "Antigravity 2.23.1 감지 — 호환성 점검 통과")
        self.assertEqual(compat.load_record(self.home)["antigravity"]["verified"], "2.23.1")
        self.assertEqual(compat.failures(self.home), [])

    def test_update_that_fails_keeps_the_old_verified_version_and_is_announced_once(self):
        compat.scan(self.home, {"antigravity": "2.15.1"}, checks(PASS))
        with patch.object(compat, "relevant_log_lines", return_value=["main.log: [error] sidecar"]):
            [result] = compat.scan(self.home, {"antigravity": "2.23.1"}, checks(FAIL))
        self.assertTrue(result["notify"])
        self.assertFalse(result["ok"])
        self.assertEqual(compat.headline(result), "Antigravity 2.23.1 감지 — 호환성 점검 실패")
        record = compat.load_record(self.home)["antigravity"]
        self.assertEqual((record["verified"], record["checked"]), ("2.15.1", "2.23.1"))
        self.assertEqual([f["version"] for f in compat.failures(self.home)], ["2.23.1"])
        # The next poll must not re-check or re-announce the same failing version.
        self.assertEqual(compat.scan(self.home, {"antigravity": "2.23.1"}, checks(FAIL)), [])

    def test_first_sighting_that_fails_is_announced(self):
        [result] = compat.scan(self.home, {"codex": "0.163.0"}, checks(FAIL))
        self.assertTrue(result["notify"])

    def test_force_rechecks_and_a_fix_clears_the_failure(self):
        compat.scan(self.home, {"antigravity": "2.23.1"}, checks(FAIL))
        [result] = compat.scan(self.home, {"antigravity": "2.23.1"}, checks(PASS), force=True)
        self.assertTrue(result["ok"])
        self.assertEqual(compat.failures(self.home), [])

    def test_rollback_to_the_verified_version_clears_the_failure(self):
        compat.scan(self.home, {"antigravity": "2.15.1"}, checks(PASS))
        compat.scan(self.home, {"antigravity": "2.23.1"}, checks(FAIL))
        self.assertEqual(compat.scan(self.home, {"antigravity": "2.15.1"}, checks(PASS)), [])
        self.assertEqual(compat.failures(self.home), [])

    def test_uninstalled_app_is_skipped(self):
        self.assertEqual(compat.scan(self.home, {"claude": None}, checks(PASS)), [])
        self.assertFalse((self.home / compat.RECORD).exists())

    def test_crashing_check_counts_as_a_failure(self):
        def crash():
            raise KeyError("sidecars")
        [result] = compat.scan(self.home, {"antigravity": "2.23.1"}, {"antigravity": crash})
        self.assertFalse(result["ok"])
        self.assertIn("KeyError", result["checks"][0]["detail"])


class AntigravityCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gemini = Path(temporary.name)
        (self.gemini / "config" / "projects").mkdir(parents=True)
        self.write("config/config.json", {"sidecars": {"handback-abc": {"enabled": False, "projectId": "p"}}})
        self.write("config/sidecars/handback-abc/sidecar.json", {"command": "handback.exe", "args": ["x"]})
        self.write("config/projects/p.json", {"name": "repo", "projectResources": {"resources": [
            {"gitFolder": {"folderUri": "file:///c%3A/repo"}}]}})
        self.write("config/hooks.json", {"handback": {}})

    def write(self, relative, value):
        path = self.gemini / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def run_checks(self):
        from handback.adapters import antigravity
        with patch.object(antigravity.AntigravityAdapter, "_agentapi_help", return_value="agentapi.exe"), \
                patch.object(compat, "_run", return_value=(0, "new-conversation send-message")) as run:
            rows = compat.check_antigravity(self.gemini)
        # Only --help is ever run: nothing is sent to the app.
        for call in run.call_args_list:
            self.assertEqual(call.args[0][-1], "--help")
        return {row["check"]: row for row in rows}

    def test_current_formats_pass(self):
        rows = self.run_checks()
        self.assertTrue(all(row["ok"] for row in rows.values()), rows)

    def test_changed_sidecar_registry_fails(self):
        self.write("config/config.json", {"sidecars": [{"name": "handback-abc"}]})
        self.assertFalse(self.run_checks()["sidecar registry schema"]["ok"])

    def test_changed_sidecar_manifest_fails(self):
        self.write("config/sidecars/handback-abc/sidecar.json", {"exec": "handback.exe"})
        self.assertFalse(self.run_checks()["sidecar manifest format"]["ok"])

    def test_projects_without_git_folders_fail(self):
        self.write("config/projects/p.json", {"name": "repo", "resources": ["c:/repo"]})
        self.assertFalse(self.run_checks()["project file format"]["ok"])

    def test_agentapi_without_send_message_fails(self):
        from handback.adapters import antigravity
        with patch.object(antigravity.AntigravityAdapter, "_agentapi_help", return_value="agentapi.exe"), \
                patch.object(compat, "_run", return_value=(0, "new-conversation")):
            rows = {row["check"]: row for row in compat.check_antigravity(self.gemini)}
        self.assertFalse(rows["agentapi --help"]["ok"])
        self.assertIn("send-message", rows["agentapi --help"]["detail"])


class SummaryTests(unittest.TestCase):
    def test_summary_lists_version_failures_and_redacted_logs(self):
        home = str(Path.home())
        result = {"agent": "antigravity", "version": "2.23.1", "previous": "2.15.1", "ok": False,
                  "utc": "2026-10-16T01:00:00+00:00", "checks": FAIL,
                  "log_lines": ["main.log: " + compat._redact(f"[error] sidecar at {home}\\x --csrf_token abc123")]}
        text = compat.summary(result)
        self.assertIn("Antigravity: 2.23.1 (마지막 검증 버전: 2.15.1)", text)
        self.assertIn("FAIL sidecar registry schema: unexpected: x", text)
        self.assertIn("[error] sidecar at ~", text)
        self.assertNotIn(home, text)
        self.assertNotIn("abc123", text)
        self.assertEqual(compat.issue_title(result), "Antigravity 2.23.1: 호환성 점검 실패")

    def test_log_lines_keep_relevant_lines_and_drop_noise(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "main.log").write_text(
                "[info] window opened\n[error] Failed to list WSL distros\n[error] sidecar did not start\n",
                encoding="utf-8")
            lines = compat.relevant_log_lines("antigravity", folder)
        self.assertEqual(lines, ["main.log: [error] sidecar did not start"])
        self.assertEqual(compat.relevant_log_lines("codex"), [])


class TrayBalloonTests(unittest.TestCase):
    def test_balloon_sets_info_once_and_click_is_queued(self):
        icon = tray.Tray("icon.ico")
        icon.active = True
        seen = []
        icon._notify = lambda action: seen.append((action, icon._balloon)) or True
        self.assertTrue(icon.balloon("handback", "Antigravity 2.23.1 감지", warning=True))
        self.assertEqual(seen, [(tray.NIM_MODIFY, ("handback", "Antigravity 2.23.1 감지", tray.NIIF_WARNING))])
        self.assertIsNone(icon._balloon)  # later tooltip updates must not repeat it
        icon._handle(tray.CALLBACK_MESSAGE, 0, tray.NIN_BALLOONUSERCLICK)
        self.assertEqual(icon.events.get_nowait(), ("balloon", None))

    def test_inactive_tray_shows_nothing(self):
        icon = tray.Tray("icon.ico")
        self.assertFalse(icon.balloon("handback", "x"))


if __name__ == "__main__":
    unittest.main()
