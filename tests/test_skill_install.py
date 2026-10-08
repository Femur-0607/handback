"""Skill installation uses this checkout and preserves the previous version."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
from unittest import mock
from handback import skill_install
import unittest


class SkillInstallTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt' and shutil.which('powershell'), 'PowerShell installer')
    def test_placeholder_and_existing_backup(self):
        repo = Path(__file__).absolute().parent.parent
        with tempfile.TemporaryDirectory(dir=repo / 'tests') as tmp:
            target = Path(tmp) / '.claude' / 'skills' / 'handback' / 'SKILL.md'
            target.parent.mkdir(parents=True)
            target.write_bytes(b'previous skill')
            result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(repo / 'install.ps1'), '-TargetHome', tmp],
                                    env=dict(os.environ, USERPROFILE=tmp), capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = target.read_text(encoding='utf-8')
            self.assertNotIn('{{HANDBACK}}', installed)
            self.assertIn(str(repo / 'handback.py'), installed)
            backups = list(target.parent.glob('SKILL.md.*.bak'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b'previous skill')

    def run_installer(self, root, *args):
        repo = Path(__file__).absolute().parent.parent
        return subprocess.run(
            [sys.executable, '-m', 'handback', 'install-skills', '--target-home', str(root), *args],
            capture_output=True, timeout=30)

    def fixture_apps(self, root):
        for name in ('OpenAI/Codex/bin/codex.exe', 'antigravity/Antigravity.exe'):
            executable = root / 'AppData/Local/Programs' / name
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.touch()
        registration = root / '.gemini/config/skills.json'
        registration.parent.mkdir(parents=True)
        registration.write_text('{"entries":[{"path":"~/.gemini/config/skills"}]}')

    def test_two_installs_backups_and_shared_files_untouched(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            self.fixture_apps(root)
            shared = [root / '.codex/AGENTS.md', root / '.gemini/GEMINI.md',
                      root / '.gemini/config/skills.json']
            for path in shared[:2]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'other rules\r\n<!-- handback:start -->\r\nold\r\n<!-- handback:end -->\r\ntail')
            originals = {p: p.read_bytes() for p in shared}
            first = self.run_installer(root)
            self.assertEqual(first.returncode, 0, first.stderr)
            targets = [root / p / 'handback/SKILL.md' for p in
                       ('.claude/skills', '.codex/skills', '.gemini/config/skills')]
            old = {p: p.read_bytes() for p in targets}
            second = self.run_installer(root)
            self.assertEqual(second.returncode, 0, second.stderr)
            for path in targets:
                self.assertEqual(path.read_bytes(), old[path])
                backups = list(path.parent.glob('SKILL.md.*.bak'))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), old[path])
                self.assertNotIn(b'{{', path.read_bytes())
                self.assertTrue(path.read_bytes().startswith(b'---'))
            for path in shared:
                self.assertEqual(path.read_bytes(), originals[path])

    def test_dry_run_and_missing_apps(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            result = self.run_installer(root, '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'skip: codex app not detected', result.stdout)
            self.assertIn(b'skip: antigravity app not detected', result.stdout)
            self.assertEqual(list(root.iterdir()), [])
            self.fixture_apps(root)
            before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
            result = self.run_installer(root, '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'dry-run: antigravity', result.stdout)
            self.assertEqual(before, {p: p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_unverified_and_external_registration_skipped(self):
        import json
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp, tempfile.TemporaryDirectory(dir=Path(__file__).parent) as outside:
            root = Path(tmp)
            self.fixture_apps(root)
            registration = root / '.gemini/config/skills.json'
            registration.unlink()
            result = self.run_installer(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'location unverified', result.stdout)
            registration.write_text(json.dumps({'entries': [{'path': outside}]}))
            result = self.run_installer(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'outside target home', result.stdout)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_isolated_never_uses_host_discovery(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp, mock.patch.object(skill_install.shutil, "which", side_effect=AssertionError("host PATH consulted")), mock.patch.dict(os.environ, {"CODEX_HOME": "outside"}):
            messages = skill_install.install(tmp, dry_run=True)
            self.assertTrue(any("dry-run: claude" in line for line in messages))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_packaged_templates_load_and_resolve(self):
        from importlib import resources
        for relative in ("SKILL.md", "codex/SKILL.md.in", "antigravity/SKILL.md.in"):
            self.assertTrue(resources.files("handback").joinpath("skills", *relative.split("/")).read_text(encoding="utf-8").strip())
        for agent in ("claude", "codex", "antigravity"):
            self.assertNotIn("{{", skill_install.render(agent))

    def test_normal_install_honors_codex_home_and_path(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            with mock.patch.object(skill_install.Path, "home", return_value=root), mock.patch.object(skill_install.shutil, "which", side_effect=lambda name: "/bin/codex" if name == "codex" else None), mock.patch.dict(os.environ, {"CODEX_HOME": str(root / "custom-codex")}):
                skill_install.install()
            self.assertTrue((root / "custom-codex/skills/handback/SKILL.md").is_file())
