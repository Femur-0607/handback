# Verification record

This is a privacy-reviewed summary of development checks recorded on October 7–8, 2026. The recorded environment and historical outcomes below describe earlier development runs, not a guarantee of compatibility with later application releases. Fresh public-source checks are reported separately in the final section.

## Recorded environment

The live integration checks ran on Windows, on one computer under one user account. The October 7 preflight report recorded `codex-cli 0.153.4` from `codex --version` and Claude CLI `2.1.292` from `claude --version`. Those observations do not establish the versions used in every later run. This summary does not establish exact Windows, Python, or Antigravity build versions.

## Historical outcomes

| Area | Recorded result | Scope |
|---|---|---|
| Live round trips | Completed | Claude Lead with Codex and Antigravity workers; Codex Lead with an Antigravity worker. Synchronous replies and asynchronous collection were exercised. |
| Concurrent collection and Lead changes | Completed after a collector fix | Separate worker results reached the Claude Monitor. An open request retained its original Codex Lead return address after a topology change, and the result was delivered and acknowledged. A later request used the new Lead. |
| Recovery and cleanup | Checked within the test setup | Sandbox delivery deferral was checked separately from a live external delivery. Temporary hook/configuration changes were compared with their backups. Existing uncertain deliveries were preserved. These checks do not establish every recovery path. |
| Automated regression tests, October 7 | Three consecutive successful runs | Each final run executed 230 tests with one skip and no failures or errors. Earlier runs and failures were retained in the private records. |
| Skill installation and documentation checks, October 8 | Successful temporary-home and dry-run checks | Installation, backups, skipped targets, and preservation of shared files were checked. The documentation-stage full-suite run executed 233 tests with one skip and no failures or errors. |

The final live integration record superseded an earlier failed Lead-switching case: the collector had mistaken an Antigravity background-task notification for a new external input. After the fix, a new live request with a background task completed and returned to its original Lead. This summary preserves that qualification rather than treating the earlier failure as a successful run.

## Limits and unresolved observations

- macOS and Linux execution were not verified. Antigravity Lead remained deferred; Claude workers and Codex Lead → Codex workers were not supported.
- Actual loading and automatic triggering of the newly installed skill files were not verified by the October 8 installer work. The official skill validator could not run because its YAML dependency was unavailable; separate structure checks are not a substitute for that validator.
- The final live round-trip pass did not newly exercise the installed Codex recovery hook and trust workflow. Antigravity `selftest` performed detection only; its exit code 5 did not indicate a successful runtime self-test.
- Some historical runs emitted Python executable-location warnings and subprocess `ResourceWarning` messages. One file-replacement test intermittently errored in an extended run series and did not reproduce in subsequent repetitions. Its cause was not established.

## Privacy and reproducibility

The original development reports included local account paths, environment identifiers, real test-conversation identifiers, application transcripts, and process/configuration details. They are not included in the public documentation. The maintainer retained byte-identical local copies with a SHA-256 manifest before removing the originals from the public working tree.

This summary omits those identifiers and raw transcripts. Removing files from the working tree does **not** remove them from earlier Git commits; existing history requires a separate review before publication.

Use the commands and supported combinations in the [main README](../../README.md) and the checked-in [tests](../../tests) to perform fresh verification. Integration checks require separately installed, signed-in applications and explicit authorization for the application actions involved. Automated tests alone do not demonstrate a live application round trip.

## Public-source preparation, October 8, 2026

The prepared source was exported without Git history, private development records, or live app state, then extracted into a new directory. Checks ran on Windows with Python 3.13.15. The imported `agent_relay` package was verified to come from that extracted directory.

| Check | Result |
|---|---|
| Full extracted-source suite | 250 tests run in 146.493 seconds; 249 passed, one skipped, no failures or errors. The skipped test required symbolic-link creation permission. |
| Hidden Tk GUI checks | All five executed successfully, including menu resource cleanup, project deregistration, and drag-release behavior. Native popup display was suppressed. |
| Installation | CLI help, isolated installer dry run, installation, repeated-install backup, and removal of the isolated skill passed. No live app hooks were installed and no agent tasks were sent. |
| Standalone source | Runtime and test import inspection found only standard-library and local package imports. No sibling-project imports, paths, or submodules were required. |
| Public-tree hygiene | Relative documentation links resolved. Local pattern checks found no personal-home paths or the checked credential patterns in publishable files. This is not a comprehensive security audit. |

An initial extraction into a deeply nested directory failed when generated paths reached the Windows legacy limit. A separate filesystem probe succeeded at 259 characters and failed at 260; the full suite then passed from a short extraction path. Deep checkout or state paths remain a limitation on such Windows environments. The tool does not modify Windows path policies.

The passing run still emitted an interpreter-location warning and a subprocess `ResourceWarning`. These observations remain unresolved. Python 3.10 and 3.13 are configured in Windows CI, but the hosted workflow has not yet been run as part of this preparation. No new live agent round trip was performed. Removing private records from the source snapshot does not remove them or author metadata from the original repository's history.

## Version-awareness baseline

The small historical version table is `VERIFIED_VERSIONS` in `agent_relay/diagnostics.py`: Codex CLI 0.153.4 and Claude CLI 2.1.292 from the October 7 preflight above. Antigravity has an explicit unknown placeholder because its build was not recorded. These values are CLI observations, not desktop UI build guarantees or proof of the versions used in every later run. `doctor` warns on a different or unknown version; it never treats a mismatch alone as a failure. Update this table only with recorded live verification evidence.
