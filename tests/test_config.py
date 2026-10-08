import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from handback.config import resolve, use_topology, validate_topology
from handback.state import ProjectState, atomic_json


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.home = Path(self.temporary.name) / "state"
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ["GIT_CEILING_DIRECTORIES"] = self.temporary.name
        for name in ("HANDBACK_HOME", "HANDBACK_LEAD", "HANDBACK_WORKERS"):
            os.environ.pop(name, None)

    def user(self, value):
        atomic_json(self.home / "config.json", value)

    def project(self, value):
        atomic_json(self.root / ".handback.json", value)

    def test_defaults_are_read_only_and_have_provenance(self):
        config = resolve(self.root, home=self.home)
        self.assertEqual(config["values"]["lead"], "claude")
        self.assertEqual(config["values"]["workers"], ["codex"])
        self.assertEqual(config["sources"]["lead"], "builtin")
        self.assertFalse(self.home.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_diagnostics_can_read_disabled_topology_without_creating_state(self):
        self.user({"agents": {"codex": {"enabled": False}}})
        result = resolve(self.root, home=self.home, validate=False)
        self.assertIn("disabled in user", result["validation_error"])
        self.assertFalse(result["values"]["agents"]["codex"]["enabled"])
        self.assertEqual(result["sources"]["agents.codex.enabled"], "user")
        self.assertFalse((self.home / "projects").exists())

    @unittest.skipUnless(shutil.which("git"), "git unavailable")
    def test_nested_root_reads_worktree_project_policy(self):
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True,
                       capture_output=True, timeout=15)
        self.project({"allowed_agents": ["codex"], "lead": "codex", "agents": {"codex": {"model": "project-model"}}})
        nested = self.root / "nested"
        nested.mkdir()
        result = resolve(nested, home=self.home, validate=False)
        self.assertEqual(result["values"]["agents"]["codex"]["model"], "project-model")
        self.assertEqual(result["policy"]["allowed_agents"], ["codex"])
        with self.assertRaisesRegex(ValueError, "not allowed"):
            use_topology(nested, "claude", ["codex"], home=self.home)

    def test_layers_deep_merge_and_track_leaf_sources(self):
        self.user({"lead": "claude:user", "agents": {"codex": {"path": "user-codex", "effort": "low"}}})
        self.project({"lead": "codex:project", "agents": {"codex": {"effort": "high"}}})
        state = ProjectState(self.root, home=self.home)
        state.write_json("topology.json", {"lead": "claude:topology"})
        os.environ["HANDBACK_LEAD"] = "codex:environment"
        os.environ["HANDBACK_WORKERS"] = " codex "
        result = resolve(self.root, home=self.home, overrides={"lead": "claude:cli"})
        self.assertEqual(result["values"]["lead"], "claude:cli")
        self.assertEqual(result["sources"]["lead"], "cli")
        self.assertEqual(result["sources"]["workers"], "environment")
        self.assertEqual(result["sources"]["agents.codex.path"], "user")
        self.assertEqual(result["values"]["agents"]["codex"]["effort"], "high")
        self.assertEqual(result["sources"]["agents.codex.effort"], "project")

    def test_use_saves_only_external_topology_and_preserves_metadata(self):
        self.project({"agents": {"codex": {"model": "configured-model"}}})
        before = (self.root / ".handback.json").read_bytes()
        state = ProjectState(self.root, home=self.home)
        state.write_json("topology.json", {"revision_note": "preserve"})
        result = use_topology(self.root, "claude:session", ["codex"], home=self.home)
        self.assertEqual(result["values"]["lead"], "claude:session")
        self.assertEqual(result["sources"]["lead"], "topology")
        self.assertEqual(state.read_json("topology.json")["revision_note"], "preserve")
        self.assertEqual((self.root / ".handback.json").read_bytes(), before)
        self.assertEqual([path.name for path in self.root.iterdir()], [".handback.json"])

    def test_user_disabled_cannot_be_reenabled_by_later_layers(self):
        self.user({"agents": {"codex": {"enabled": False}}})
        self.project({"agents": {"codex": {"enabled": True}}})
        os.environ["HANDBACK_WORKERS"] = "codex"
        with self.assertRaisesRegex(ValueError, "disabled in user"):
            resolve(self.root, home=self.home, overrides={"agents": {"codex": {"enabled": True}}})
        with self.assertRaisesRegex(ValueError, "disabled in user"):
            use_topology(self.root, "claude", ["codex"], home=self.home)
        self.assertFalse((ProjectState(self.root, home=self.home).path / "topology.json").exists())

    def test_project_allowlist_cannot_be_bypassed_by_overrides(self):
        self.project({"allowed_agents": ["codex"], "lead": "codex"})
        config = resolve(self.root, home=self.home, validate=False)
        with self.assertRaisesRegex(ValueError, "not allowed"):
            validate_topology(config, "claude", ["codex"], "ask")
        with self.assertRaisesRegex(ValueError, "not allowed"):
            resolve(self.root, home=self.home, overrides={"lead": "claude", "allowed_agents": ["claude", "codex"]})
        os.environ["HANDBACK_LEAD"] = "claude"
        with self.assertRaisesRegex(ValueError, "not allowed"):
            resolve(self.root, home=self.home)

    def test_project_disabled_cannot_be_reenabled_by_cli(self):
        self.project({"agents": {"codex": {"enabled": False}}})
        with self.assertRaisesRegex(ValueError, "disabled in project"):
            resolve(self.root, home=self.home, overrides={"agents": {"codex": {"enabled": True}}})

    def test_unverified_roles_rejected_even_when_enabled(self):
        self.user({"agents": {"antigravity": {"enabled": True}}})
        config = resolve(self.root, home=self.home)
        for lead, workers in (("claude", ["claude"]),):
            with self.assertRaisesRegex(ValueError, "unverified"):
                validate_topology(config, lead, workers, "ask")
        # Claude remains Lead-only; Antigravity Lead requires a concrete handle.
        self.assertEqual(validate_topology(config, "claude", ["codex", "antigravity"], "ask")["workers"],
                         ["codex", "antigravity"])

    def test_unimplemented_fallback_is_explicitly_rejected(self):
        with self.assertRaisesRegex(ValueError, "not implemented; use ask"):
            use_topology(self.root, "claude", ["codex"], fallback="next", home=self.home)
        self.assertFalse((ProjectState(self.root, home=self.home).path / "topology.json").exists())

    def test_malformed_topology_and_policies_rejected(self):
        config = resolve(self.root, home=self.home)
        for lead, workers, fallback in (("claude:", ["codex"], "ask"), ("unknown", ["codex"], "ask"),
                                         ("claude", [], "ask"), ("claude", ["codex", "codex"], "ask"),
                                         ("claude", ["codex"], "silent")):
            with self.assertRaises(ValueError):
                validate_topology(config, lead, workers, fallback)
        self.project({"allowed_agents": "codex"})
        with self.assertRaisesRegex(ValueError, "allowed_agents"):
            resolve(self.root, home=self.home)
        self.project({"agents": {"codex": {"enabled": "yes"}}})
        with self.assertRaisesRegex(ValueError, "boolean"):
            resolve(self.root, home=self.home)

    def test_invalid_existing_topology_can_be_replaced_by_valid_use(self):
        state = ProjectState(self.root, home=self.home)
        state.write_json("topology.json", {"lead": "antigravity", "workers": ["claude"]})
        result = use_topology(self.root, "claude", ["codex"], home=self.home)
        self.assertEqual(result["values"]["lead"], "claude")
        self.assertEqual(result["values"]["workers"], ["codex"])

    def test_invalid_json_does_not_get_overwritten(self):
        self.home.mkdir()
        (self.home / "config.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid configuration"):
            use_topology(self.root, "claude", ["codex"], home=self.home)
        self.assertEqual((self.home / "config.json").read_text(), "{")


if __name__ == "__main__":
    unittest.main()
