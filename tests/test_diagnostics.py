import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from handback import cli, config, diagnostics as d, envelope, inbox
from handback.state import ProjectState


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "project"
        self.root.mkdir()
        self.home = Path(temp.name) / "state"
        env = patch.dict(os.environ, HANDBACK_HOME=str(self.home))
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HANDBACK_LEAD", None)
        os.environ.pop("HANDBACK_WORKERS", None)
        self.state = ProjectState(self.root)
        self.now = datetime.now(timezone.utc)
        self.agents = [{"agent": name, "installed": True, "executable": "fake-app",
                        "version": version, "capabilities": {"hooks_installed": True}}
                       for name, version in (("codex", "codex-cli 0.153.4"),
                                             ("claude", "2.1.292 (Claude Code)"),
                                             ("antigravity", None))]
        for target, value in (("queue_supported", True), ("skill_status", "points at this installation"),
                              ("hooks_present", True), ("state_warnings", []), ("legacy_warnings", [])):
            mock = patch.object(d, target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.object(cli, "get_adapter", side_effect=lambda name, values=None:
                            Mock(detect=Mock(return_value=next(a for a in self.agents if a["agent"] == name))))
        mock.start()
        self.addCleanup(mock.stop)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*args, "--root", str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def request(self, key="a" * 32, **extra):
        value = {"id": key, "status": "accepted", "created_utc": self.now.isoformat(),
                 "handle": "codex:worker-private", "return_to": "claude:lead-private",
                 "agent": "codex", **extra}
        self.state.save_request(value)
        return value

    def result(self, request, lead_status=None, ack=False):
        mail = envelope.make(request["id"], request["handle"], request["return_to"], "fixture result",
                             kind="result", message_id=request["id"][::-1])
        inbox.put(self.state.path / "inbox", mail)
        request["reply_id"] = mail["id"]
        self.state.save_request(request)
        if lead_status:
            self.state.write_json("inbox/delivered/" + mail["id"] + ".json",
                                  {"id": mail["id"], "status": lead_status, "attempts": 3})
        if ack:
            inbox.acknowledge(self.state.path / "inbox", mail["id"], mail["recipient"])
        return mail

    def test_checklist_each_warning_has_exactly_one_next_step_and_warn_exit_zero(self):
        code, out, _ = self.invoke("doctor")
        self.assertEqual(code, 0)
        for line in out.splitlines():
            self.assertIn(line.split()[0], {"OK", "WARN", "FAIL"})
            self.assertEqual(line.count(" | Next: "), int(not line.startswith("OK")))
        self.assertIn("WARN Claude Monitor", out)
        self.assertFalse(self.home.exists())

    def test_check_failure_exit_five(self):
        with patch.object(d, "queue_supported", return_value=False):
            code, out, _ = self.invoke("doctor")
        self.assertEqual(code, 5)
        self.assertIn("FAIL Codex queue --thread", out)

    def test_codex_checks_follow_lead_and_workers_in_human_and_report_output(self):
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({
            "agents": {"antigravity": {"enabled": True}}}), encoding="utf-8")
        for lead, workers, required in (
                ("claude:lead", ["antigravity"], False),
                ("claude:lead", ["codex"], True),
                ("codex:lead-id", ["antigravity"], True),
                ("claude:lead", ["antigravity", "codex"], True)):
            self.state.write_json("topology.json", {"lead": lead, "workers": workers})
            self.assertNotIn("validation_error", config.resolve(self.root, validate=False))
            for installed, queue in ((False, False), (True, False), (True, True)):
                self.agents[0].update(installed=installed, executable="fake-app" if installed else None)
                missing_level = "FAIL" if required else "WARN"
                expected = {"Codex executable": "OK" if installed else missing_level,
                            "Codex queue --thread": "OK" if queue else missing_level}
                for options in ((), ("--report",)):
                    with self.subTest(lead=lead, workers=workers, installed=installed,
                                      queue=queue, options=options):
                        with patch.object(d, "queue_supported", return_value=queue):
                            code, out, _ = self.invoke("doctor", *options)
                        self.assertEqual(code, 5 if "FAIL" in expected.values() else 0)
                        if options:
                            report = json.loads(out.removeprefix("```json\n").removesuffix("\n```\n"))
                            levels = {r["check"]: r["level"] for r in report["checks"]}
                            self.assertEqual({name: levels[name] for name in expected}, expected)
                        else:
                            for name, level in expected.items():
                                line = next(line for line in out.splitlines() if f" {name}:" in line)
                                self.assertTrue(line.startswith(level + " "), line)
                                self.assertEqual(line.count(" | Next: "), int(level != "OK"))
                                if not required and level == "WARN":
                                    self.assertIn("not required by selected topology", line)
                                    self.assertIn("No action required", line)

    def test_builtin_topology_still_requires_codex(self):
        self.agents[0].update(installed=False, executable=None)
        with patch.object(d, "queue_supported", return_value=False):
            code, out, _ = self.invoke("doctor")
        self.assertEqual(code, 5)
        self.assertIn("FAIL Codex executable", out)
        self.assertIn("FAIL Codex queue --thread", out)
        self.assertIn("builtin default; not explicitly set", out)
        self.assertFalse(self.home.exists())

    def test_invalid_topology_retains_required_codex_checks(self):
        # Antigravity is disabled by default, so this selection is not valid.
        self.state.write_json("topology.json", {"lead": "claude:lead", "workers": ["antigravity"]})
        self.assertIn("validation_error", config.resolve(self.root, validate=False))
        self.agents[0].update(installed=False, executable=None)
        with patch.object(d, "queue_supported", return_value=False):
            code, out, _ = self.invoke("doctor")
        self.assertEqual(code, 5)
        self.assertIn("FAIL Topology", out)
        self.assertIn("FAIL Codex executable", out)
        self.assertIn("FAIL Codex queue --thread", out)

    def test_missing_skills_remain_nonblocking_for_used_and_unused_agents(self):
        self.state.write_json("topology.json", {"lead": "codex:lead-id", "workers": ["antigravity"]})
        (self.home / "config.json").write_text(json.dumps({
            "agents": {"antigravity": {"enabled": True}}}), encoding="utf-8")
        with patch.object(d, "skill_status", return_value="missing"):
            code, out, _ = self.invoke("doctor")
        self.assertEqual(code, 0)
        self.assertIn("WARN claude skill: missing", out)
        self.assertIn("WARN codex skill: missing", out)

    def test_json_preserves_legacy_fields_values_and_exit(self):
        from handback.router import diagnostics
        cleanup = {"candidates": [], "dry_run": True}
        with patch("handback.adapters.antigravity.cleanup_sidecars", return_value=cleanup):
            code, out, _ = self.invoke("doctor", "--json")
        value = json.loads(out)
        self.assertEqual(code, 0)
        expected = {"state_home", "configuration", "warnings", "agents", "sidecar_cleanup", "limits",
                    *diagnostics(self.state, "claude")}
        self.assertEqual(set(value), expected)
        self.assertEqual(value["agents"], self.agents)
        self.assertEqual(value["configuration"], config.resolve(self.root, validate=False))
        self.assertEqual(value["sidecar_cleanup"], cleanup)

    def test_version_mismatch_and_unknown_are_warnings(self):
        self.agents[0]["version"] = "codex-cli 0.999.0"
        self.agents[1]["version"] = None
        code, out, _ = self.invoke("doctor")
        self.assertEqual(code, 0)
        self.assertIn("WARN codex version: codex-cli 0.999.0 (untested version", out)
        self.assertIn("WARN claude version: unknown", out)

    def test_report_drops_home_project_ids_and_freeform_app_output(self):
        self.request("b" * 32)
        self.agents[0]["version"] = f"codex-cli 0.153.4 {self.root} codex:secret-session"
        with patch.object(cli, "legacy_warnings", return_value=[str(self.root) + " secret-session"]):
            code, out, err = self.invoke("doctor", "--report")
        self.assertEqual(code, 0)
        value = json.loads(out.removeprefix("```json\n").removesuffix("\n```\n"))
        self.assertEqual(value["app_versions"]["codex"], "0.153.4")
        self.assertEqual(value["topology"]["lead"], "claude")
        for private in (str(self.root), str(Path.home()), "b" * 32, "secret-session", "lead-private"):
            self.assertNotIn(private, out + err)

    def test_report_failure_does_not_leak_exception(self):
        with patch.object(cli.config, "resolve", side_effect=ValueError(str(self.root) + " secret-id")):
            code, out, err = self.invoke("doctor", "--report")
        self.assertEqual(code, 5)
        self.assertNotIn(str(self.root), out + err)
        self.assertNotIn("secret-id", out + err)

    def test_explain_missing_request(self):
        self.assertEqual(self.invoke("explain", "--request", "absent")[0], 5)
        self.assertFalse(self.home.exists())

    def test_explain_collector_branches_and_read_only(self):
        cases = [(None, None, "collector not recorded"),
                 ({"pid": 101}, False, "collector dead"),
                 ({"pid": 101}, True, "still running"),
                 ({"pid": 101}, None, "collector liveness unknown"),
                 ({"pid": 101, "started_utc": (self.now - timedelta(seconds=50)).isoformat(),
                   "timeout": 10}, True, "collector timeout elapsed")]
        for info, alive, reason in cases:
            with self.subTest(reason=reason):
                request = self.request(collector=info)
                before = {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
                with patch.object(d, "pid_alive", return_value=alive):
                    value = d.explain_request(self.state, request["id"])
                self.assertIn(reason, value["next_reason"])
                self.assertIn("wait --request", value["next_action"])
                self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob("*") if p.is_file()})

    def test_explain_delivery_and_terminal_branches(self):
        for status, accepted, reason in (("prepared", "not sent", "collector not recorded"),
                                         ("delivery_unknown", "unknown", "collector not recorded"),
                                         ("dispatching", "unknown", "collector not recorded"),
                                         ("send_failed", "failed", "worker delivery failed"),
                                         ("completed", "accepted", "terminal result missing"),
                                         ("failed", "accepted", "terminal result missing")):
            with self.subTest(status=status):
                self.request(status=status)
                result = d.explain_request(self.state, "a" * 32)
                self.assertIn(reason, result["next_reason"])
                self.assertEqual(result["timeline"][1]["status"], accepted)

    def test_explain_ready_ack_delivery_branches(self):
        for index, (status, ack, reason, action) in enumerate([
                (None, False, "result ready", "inbox ack --request"),
                ("delivered", False, "result ready", "inbox ack --request"),
                ("delivery_unknown", False, "lead delivery unknown", "Check whether"),
                ("pending", False, "lead delivery pending", "inbox redeliver"),
                ("failed", False, "lead delivery failed", "inbox redeliver"),
                ("delivery_unknown", True, "acknowledged", "No action required")]):
            with self.subTest(status=status, ack=ack):
                request = self.request(f"{index:032x}", status="completed")
                mail = self.result(request, status, ack)
                value = d.explain_request(self.state, request["id"])
                self.assertIn(reason, value["next_reason"])
                self.assertIn(action, value["next_action"])
                self.assertEqual(value["timeline"][3]["message_id"], mail["id"])

    def test_explain_cli_json_and_human(self):
        self.request()
        code, raw, _ = self.invoke("explain", "--request", "a" * 32, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(raw)["timeline"]), 6)
        code, raw, _ = self.invoke("explain", "--request", "a" * 32)
        self.assertIn("time not recorded", raw)
        self.assertEqual(raw.count("Next ("), 1)

    def test_mixed_stats_latency_retry_and_ack(self):
        for index, status in enumerate(("completed", "completed", "failed", "send_failed", "delivery_unknown", "accepted")):
            request = self.request(f"{index:032x}", status=status,
                                   completed_utc=(self.now + timedelta(seconds=(index + 1) * 10)).isoformat())
            if status in {"completed", "failed"}:
                self.result(request, "delivered", ack=index == 0)
        value = d.usage_stats(self.state)
        self.assertEqual([value[k] for k in ("requests", "completed", "failed", "unknown", "open")],
                         [6, 2, 2, 1, 1])
        self.assertEqual((value["median_seconds"], value["p90_seconds"]), (20, 30))
        self.assertEqual(value["results_unacknowledged"], 2)
        self.assertEqual(value["additional_delivery_attempts"], 6)
        self.assertIn("not persisted", " ".join(value["notes"]))
        self.assertNotIn("timed_out", value)

    def test_stats_days_empty_and_cli(self):
        self.request(created_utc=(self.now - timedelta(days=8)).isoformat())
        self.request("c" * 32)
        code, out, _ = self.invoke("status", "--stats", "--days", "7")
        self.assertEqual(code, 0)
        value = json.loads(out)
        self.assertEqual(value["requests"], 1)
        self.assertIsNone(value["p90_seconds"])
        self.assertIsNone(value["median_seconds"])
        with self.assertRaises(SystemExit):
            self.invoke("status", "--days", "7")
        with self.assertRaises(SystemExit):
            self.invoke("status", "--stats", "--days", "0")

    def test_pid_inspection_permission_error_is_unknown(self):
        with patch.object(d, "pid_alive", side_effect=ValueError("permission")):
            self.assertIsNone(d.collector_info({"collector": {"pid": 123}})["alive"])

    def test_zero_timeout_does_not_expire(self):
        self.assertFalse(d.collector_info({"collector": {"started_utc": "2000-01-01T00:00:00Z", "timeout": 0}})["expired"])

    def test_antigravity_enabled_detection_hook_and_unknown_version(self):
        resolved = config.resolve(self.root, validate=False)
        resolved["values"]["agents"]["antigravity"]["enabled"] = True
        self.agents[2]["capabilities"]["hooks_installed"] = False
        rows = d.checklist(self.state, resolved, self.agents)
        rows = {r["check"]: r for r in rows}
        self.assertEqual(rows["Antigravity"]["level"], "OK")
        self.assertEqual(rows["Antigravity hooks"]["level"], "WARN")
        self.assertEqual(rows["antigravity version"]["level"], "WARN")

    def test_report_preserves_prerelease_and_suppresses_reader_errors(self):
        import sys
        self.agents[0]["version"] = "codex-cli 0.162.0-alpha.2"
        original = d.checklist
        def noisy(*args):
            print("private-session " + str(self.root), file=sys.stderr)
            return original(*args)
        with patch.object(d, "checklist", side_effect=noisy):
            code, out, err = self.invoke("doctor", "--report")
        self.assertEqual(code, 0)
        self.assertIn("0.162.0-alpha.2", out)
        self.assertNotIn("private-session", out + err)
        self.assertIn("Diagnostic reader warnings", out)

    def test_report_home_is_tilde_and_invalid_topology_does_not_leak(self):
        self.state.home = Path.home() / ".handback"
        resolved = config.resolve(self.root, validate=False)
        resolved["values"].update(lead="private-agent:secret", workers=["private-worker"])
        value = d.bug_report(self.state, resolved, self.agents, [])
        self.assertEqual(value["state_home"], "~/.handback")
        self.assertEqual(value["topology"], {"lead": "unknown", "workers": ["unknown"]})

    def test_human_diagnostic_error_stays_a_checklist_failure(self):
        with patch.object(cli.config, "resolve", side_effect=ValueError("bad config")):
            code, out, _ = self.invoke("doctor")
        self.assertEqual(code, 5)
        self.assertTrue(out.startswith("FAIL "))
        self.assertEqual(out.count(" | Next: "), 1)


if __name__ == "__main__":
    unittest.main()
