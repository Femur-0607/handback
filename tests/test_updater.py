"""In-app update: version compare, release parsing, verified download, installer launch."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from handback import updater

BASE = updater.DOWNLOAD_PREFIX + "v0.4.0/"
PAYLOAD = b"fake installer"


def release_json(tag="v0.4.0", installer=BASE + "handback-0.4.0-setup.exe", sums=BASE + "SHA256SUMS.txt"):
    assets = [{"name": "handback-0.4.0-setup.exe", "browser_download_url": installer},
              {"name": "SHA256SUMS.txt", "browser_download_url": sums}]
    return json.dumps({"tag_name": tag, "assets": assets}).encode()


def fetcher(responses):
    def fetch(url, timeout=None):
        return responses[url]
    return fetch


class VersionTests(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(updater.is_newer("v0.4.0", "0.3.4"))
        self.assertTrue(updater.is_newer("0.3.10", "0.3.9"))
        self.assertFalse(updater.is_newer("0.3.4", "0.3.4"))
        self.assertFalse(updater.is_newer("0.3.3", "0.3.4"))
        self.assertFalse(updater.is_newer("v0.4.0-rc1", "0.3.4"))
        self.assertFalse(updater.is_newer("garbage", "0.3.4"))


class ReleaseTests(unittest.TestCase):
    def test_check_returns_newer_release(self):
        fetch = fetcher({updater.API_LATEST: release_json()})
        release = updater.check(fetch, current="0.3.4")
        self.assertEqual(release["version"], "0.4.0")
        self.assertIsNone(updater.check(fetch, current="0.4.0"))

    def test_assets_outside_this_repository_are_refused(self):
        evil = "https://example.com/handback-0.4.0-setup.exe"
        fetch = fetcher({updater.API_LATEST: release_json(installer=evil)})
        self.assertIsNone(updater.check(fetch, current="0.3.4", installer_required=True))
        # A source checkout still learns about the release, without any download URL.
        release = updater.check(fetch, current="0.3.4", installer_required=False)
        self.assertEqual((release["version"], release["installer"], release["sums"]), ("0.4.0", None, None))
        with self.assertRaises(updater.UpdateError):
            updater.download(release, "C:/never", fetch)

    def test_release_without_installer_is_ignored_only_when_this_build_installs_itself(self):
        data = json.dumps({"tag_name": "v0.4.0", "assets": []}).encode()
        fetch = fetcher({updater.API_LATEST: data})
        self.assertIsNone(updater.check(fetch, current="0.3.4", installer_required=True))
        self.assertEqual(updater.check(fetch, current="0.3.4", installer_required=False)["version"], "0.4.0")
        with mock.patch("handback.updater.supported", return_value=True):
            self.assertIsNone(updater.check(fetch, current="0.3.4"))
        with mock.patch("handback.updater.supported", return_value=False):
            self.assertEqual(updater.check(fetch, current="0.3.4")["version"], "0.4.0")

    def test_release_page_comes_from_this_repository_only(self):
        fetch = fetcher({updater.API_LATEST: release_json()})
        self.assertEqual(updater.latest_release(fetch, installer_required=False)["page"],
                         "https://github.com/Femur-0607/handback/releases/tag/v0.4.0")
        data = json.loads(release_json())
        data["html_url"] = "https://github.com/Femur-0607/handback/releases/tag/v0.4.0"
        fetch = fetcher({updater.API_LATEST: json.dumps(data).encode()})
        self.assertEqual(updater.latest_release(fetch, installer_required=False)["page"], data["html_url"])
        data["html_url"] = "https://example.com/phish"
        fetch = fetcher({updater.API_LATEST: json.dumps(data).encode()})
        self.assertEqual(updater.latest_release(fetch, installer_required=False)["page"],
                         "https://github.com/Femur-0607/handback/releases/tag/v0.4.0")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.release = updater.latest_release(fetcher({updater.API_LATEST: release_json()}))

    def responses(self, digest):
        return fetcher({self.release["sums"]: f"{digest}  handback-0.4.0-setup.exe\n".encode(),
                        self.release["installer"]: PAYLOAD})

    def test_verified_download(self):
        path = updater.download(self.release, self.tmp.name, self.responses(hashlib.sha256(PAYLOAD).hexdigest()))
        self.assertEqual(path.name, "handback-0.4.0-setup.exe")
        self.assertEqual(path.read_bytes(), PAYLOAD)

    def test_hash_mismatch_saves_nothing(self):
        with self.assertRaises(updater.UpdateError):
            updater.download(self.release, self.tmp.name, self.responses("0" * 64))
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_missing_sum_entry(self):
        fetch = fetcher({self.release["sums"]: b"abc  other.exe\n"})
        with self.assertRaises(updater.UpdateError):
            updater.download(self.release, self.tmp.name, fetch)


class InstallerTests(unittest.TestCase):
    def test_runs_silently_and_asks_for_relaunch(self):
        popen = mock.Mock()
        updater.run_installer(Path("C:/x/setup.exe"), popen=popen)
        args = popen.call_args.args[0]
        self.assertEqual(Path(args[0]), Path("C:/x/setup.exe"))
        self.assertIn("/VERYSILENT", args)
        self.assertIn("/RELAUNCH=1", args)
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_only_frozen_windows_build_updates_itself(self):
        with mock.patch("handback.invocation.frozen", return_value=False):
            self.assertFalse(updater.supported())

    def test_installer_relaunches_only_on_request(self):
        script = (Path(__file__).resolve().parents[1] / "packaging" / "installer.iss").read_text(encoding="utf-8-sig")
        self.assertIn("Check: RelaunchRequested", script)
        self.assertIn("{param:RELAUNCH|0}", script)

    def test_installer_stops_only_long_running_relay_processes(self):
        packaging = Path(__file__).resolve().parents[1] / "packaging"
        script = (packaging / "installer.iss").read_text(encoding="utf-8-sig")
        self.assertIn("function PrepareToInstall", script)
        self.assertIn("ExtractTemporaryFile('stop-relay.ps1')", script)
        source = (packaging / "stop-relay.ps1").read_text(encoding="utf-8")
        import re
        pattern = re.compile(re.search(r"\$pattern = '([^']+)'", source).group(1), re.I)
        exe = r'"C:\Users\x\AppData\Local\Programs\handback\handback.exe"'
        stopped = [exe + r" inbox watch --root C:\p --for claude:1 --idle-exit 1200",
                   exe + " collect --request abc --root C:\\p --timeout 86400",
                   r"C:\handback\handback.exe  INBOX  WATCH"]
        kept = [exe + " route --root C:\\p --conversation c",
                exe + " hook claude UserPromptSubmit",
                exe + r' inbox list --root "C:\my collect files"',
                exe + " inbox ack --id 1",
                exe + " dashboard --autostart on",
                exe + " collector-status"]
        for line in stopped:
            self.assertRegex(line, pattern)
        for line in kept:
            self.assertNotRegex(line, pattern)


if __name__ == "__main__":
    unittest.main()
