# Contributing

agent-relay is an experimental local relay for one computer and one OS user. Windows is the verified integration platform. Start with the [quick start](docs/quickstart.md), [Korean manual](docs/usage.ko.md), and [verification summary](docs/verification/README.md).

## Scope

Claude Lead supports Codex and Antigravity workers. Codex Lead supports Antigravity workers only. Claude workers, Antigravity Lead, and automatic quota fallback are unsupported. macOS and Linux changes need explicit validation; existing platform branches do not establish support.

Keep changes focused and describe the concrete behavior before and after the change. Include the affected app versions, commands used for validation, and any untested behavior. Compatibility changes should preserve pending requests and explicit ACK semantics. Unknown delivery must never cause an automatic duplicate submission.

## Development and tests

Python 3.10 or newer is required. There are no third-party Python package dependencies. Git enables the worktree tests; Windows PowerShell enables installer and hook-command tests. Checks that require an unavailable tool are skipped. From the repository root:

```powershell
python -m unittest
```

The compatibility entry point is `agent_relay.py`; implementation is in `agent_relay/` and tests are in `tests/`. Windows CI runs the standard-library unittest suite. PowerShell installer tests are platform-specific and may be skipped elsewhere.

Use a short checkout path on Windows. Tests create nested state paths below `tests/`; with legacy Windows path limits, a deeply nested checkout can cause `FileNotFoundError` at 260 characters. The public snapshot was also tested outside its original Git checkout so implicit sibling-project dependencies could be detected.

GUI tests open hidden Tk windows (alpha zero before mapping, including menus and dialogs). Only explicit screenshot capture scripts show windows.

Unit tests must use temporary homes, configuration files, and fixtures. Do not let them install hooks in a contributor's real app settings, submit live agent tasks, or depend on account credentials. Test meaningful failure and recovery behavior when changing delivery, persistence, locking, or ownership handling.

Unit tests and live integration checks are separate. A passing suite does not prove compatibility with a current app build. Live checks require installed, signed-in apps and intentionally created test conversations. Record the tested Windows, Python, and app versions; distinguish detection, command acceptance, confirmed completion, Lead delivery, and ACK. Remove temporary hooks with the same state-home ownership records used to install them.

## Releasing

One-time setup: create the `pypi` environment in the GitHub repository and configure its deployment protection rules. In PyPI, add a Trusted Publisher (a pending publisher for the first release) for project `agent-relay`, owner `Femur-0607`, repository `agent-relay`, workflow filename `release.yml`, and environment `pypi`. No API token is needed.

To release, bump `__version__` in `agent_relay/__init__.py`, commit the release changes, create tag `vX.Y.Z` matching that version, and push the tag (`git tag vX.Y.Z` then `git push origin vX.Y.Z`). The release workflow builds and checks the distributions, tests the wheel on Windows, Linux, and macOS with Python 3.10 and 3.13, then publishes through PyPI Trusted Publishing. A manual workflow dispatch runs build and smoke checks only; it does not publish.

## Reports and fixtures

For a bug report, include a minimal reproduction, expected and actual behavior, relevant versions, exit code, and a sanitized request or delivery status. State whether acceptance was uncertain. Do not resend an uncertain task merely to reproduce it; inspect its saved request first.

Use synthetic identifiers and paths in committed fixtures. Before sharing logs, remove credentials, personal paths, real session or conversation IDs, prompts, transcripts, and unrelated app settings. Hook backups and state-home contents can contain private data; do not attach them wholesale. Publish a short sanitized verification summary instead of raw live transcripts.
