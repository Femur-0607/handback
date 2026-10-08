"""Project execution defaults through the dashboard's actual Tk controls."""
import unittest
from unittest.mock import patch

from handback import config, dashboard
from handback.state import ProjectState
from tests import test_dashboard_gui as dashboard_gui


class ExecutionTargetsTests(unittest.TestCase):
    def test_roles_remain_separate_and_claude_control_is_disabled(self):
        targets = dashboard.execution_targets({"lead": "claude:session", "workers": ["codex", "antigravity"]})
        self.assertEqual([(item["agent"], item["role"], item["enabled"]) for item in targets],
                         [("claude", "lead", False), ("codex", "worker", True), ("antigravity", "worker", True)])
        targets = dashboard.execution_targets({"lead": "codex:lead", "workers": ["codex"]})
        self.assertEqual([item["role"] for item in targets], ["lead", "worker"])


class DashboardExecutionTests(unittest.TestCase):
    setUp = dashboard_gui.DashboardMenuTests.setUp
    destroy_root = dashboard_gui.DashboardMenuTests.destroy_root
    widgets = dashboard_gui.DashboardMenuTests.widgets
    entry = dashboard_gui.DashboardMenuTests.entry

    def project(self):
        root = self.home.parent / "checkout"
        root.mkdir(exist_ok=True)
        state = ProjectState(root, home=self.home)
        state.write_json("topology.json", {"lead": "claude:session", "workers": ["codex"],
                                           "fallback": "ask", "root": str(root)})
        return root, state

    def open_editor(self, root, agent="codex", role="worker"):
        self.strip._execution_settings(str(root), agent, role)
        window = self.strip._execution_window
        entries = [item for item in self.widgets(window) if isinstance(item, self.tk.Entry)]
        buttons = {item.cget("text"): item for item in self.widgets(window) if isinstance(item, self.tk.Button)}
        return window, entries, buttons

    def test_save_ultra_defaults_without_changing_existing_thread(self):
        root, state = self.project()
        state.register_thread({"handle": "codex:t", "agent": "codex", "id": "t",
                               "execution": {"model": "saved-model", "reasoning_effort": "high"}})
        window, entries, buttons = self.open_editor(root)
        entries[0].insert(0, "chosen-model")
        entries[1].insert(0, "ultra")
        buttons["저장"].invoke()
        self.assertFalse(window.winfo_exists())
        values = config.resolve(root, home=self.home)["values"]
        self.assertEqual(config.execution_settings(values, "codex"),
                         {"model": "chosen-model", "reasoning_effort": "ultra"})
        self.assertEqual(state.threads()["codex:t"]["execution"],
                         {"model": "saved-model", "reasoning_effort": "high"})
        window, entries, buttons = self.open_editor(root)
        self.assertEqual([entry.get() for entry in entries], ["chosen-model", "ultra"])
        for entry in entries:
            entry.delete(0, "end")
        buttons["저장"].invoke()
        self.assertEqual(config.execution_settings(config.resolve(root, home=self.home)["values"], "codex"),
                         {"model": None, "reasoning_effort": None})

    def test_invalid_effort_keeps_editor_open_and_state_unchanged(self):
        root, state = self.project()
        before = state.read_json("topology.json")
        window, entries, buttons = self.open_editor(root)
        entries[1].insert(0, "typo")
        buttons["저장"].invoke()
        self.assertTrue(window.winfo_exists())
        self.assertEqual(state.read_json("topology.json"), before)
        buttons["취소"].invoke()
        self.assertFalse(window.winfo_exists())

    def test_antigravity_only_offers_model_and_cancel_does_not_write(self):
        root, state = self.project()
        before = state.read_json("topology.json")
        window, entries, buttons = self.open_editor(root, "antigravity")
        self.assertEqual(len(entries), 1)
        entries[0].insert(0, "pro")
        buttons["취소"].invoke()
        self.assertFalse(window.winfo_exists())
        self.assertEqual(state.read_json("topology.json"), before)

    def test_project_menu_routes_worker_settings_and_disables_claude(self):
        root, _ = self.project()
        self.strip.render(dashboard.snapshot(self.home))
        self.strip._menu(self.event, str(root))
        menu = self.strip._context_menu
        menu.select(self.entry(menu, "모델·추론 설정 ▶"))
        menu.open_child()
        child = menu.child
        self.assertFalse(child.items[0]["enabled"])
        self.assertEqual(child.items[1]["label"], "워커 · Codex")
        with patch.object(self.strip, "_execution_settings") as editor:
            child.invoke(1)
        editor.assert_called_once_with(str(root), "codex", "worker")


if __name__ == "__main__":
    unittest.main()
