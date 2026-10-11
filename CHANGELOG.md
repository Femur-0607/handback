# Changelog

All notable changes to this project are documented here. This file follows the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

## [0.3.2] - 2026-10-11

### Added

- Notification-area (tray) icon: left-click shows or hides the dashboard,
  right-click opens its menu (with **숨기기** and **종료**), and the tooltip shows
  the current state. **닫기** now hides the dashboard to the tray.

### Removed

- The 0.3.1 taskbar button; handback now lives in the notification area only.

### Changed

- Dashboard window icons are loaded at 16, 20, 24, 32, 40, 48, 64, 128 and
  256 px for sharper rendering at every display scale.
- The Windows download is now a single installer, `handback-<ver>-setup.exe`. It
  installs the agent skills in one step, adds a Start menu entry and an optional
  desktop shortcut, offers optional autostart, upgrades in place, and uninstalls
  from Windows Settings. The zip is no longer published.
- Dashboard menu item renamed from "설정 다시 실행" to "설정".
- The installer adds its folder to the per-user PATH, so `handback` works in
  new terminals; uninstall removes exactly that entry.
- CI now builds and smoke-tests the Windows installer on every push and manual
  run, and the release workflow's installer job can be run manually (tag check
  and GitHub Release upload stay tag-only).

### Fixed

- Uninstalling now also removes handback's Antigravity hooks and its own
  Claude, Codex and Antigravity skills (new `handback uninstall-skills`, which
  deletes only skill files that point at this installation and keeps backups),
  so nothing is left pointing at the deleted exe. `~/.handback` data is kept.
- The windowed dashboard no longer flashes console windows: its `git` and
  PowerShell child processes run with `CREATE_NO_WINDOW`. Toggling
  **Windows 시작 시 실행** no longer freezes the dashboard while PowerShell
  runs, and the startup-shortcut check reads non-ASCII paths as UTF-8.
- The setup window no longer warns "move to a folder without spaces" for the
  installer location (`%LOCALAPPDATA%\Programs`) when the Windows user name
  contains a space. Its update hint now points at the new setup.exe (or at
  `pip install -U` for source installs) instead of overwriting files.
- **모두 확인 처리** in the unread-results viewer now acknowledges only the
  results that viewer showed; results that arrive while it is open stay
  pending and still reach the Lead.

## [0.3.1] - 2026-10-11

### Added

- Setup window on the first dashboard launch (exe) and a **설정 다시 실행**
  menu item to reopen it.
- Dashboard taskbar button with the handback logo.

### Changed

- README: start from `handback-dashboard.exe`.

### Fixed

- The release job now creates a missing GitHub release (PowerShell 5.1 stderr).
- Dashboard menu tests no longer depend on the real Startup folder.

## [0.3.0] - 2026-10-10

### Added

- Windows download: `handback-<version>-windows-x64.zip` on GitHub Releases with
  `SHA256SUMS.txt` and a build provenance attestation. It contains
  `handback.exe` and `handback-dashboard.exe` and needs no Python.
- Setup wizard: double-click `handback.exe`, or run `handback setup` (`--yes`,
  `--dry-run`). It checks apps, installs skills, offers Windows autostart, opens
  the dashboard, and prints the next step.
- Dashboard right-click menu: Windows startup toggle, install check, skill
  reinstall, Hook install/remove, per-project role selection, and a connection
  test that runs one real Codex worker task. The dashboard can set only Claude
  as Lead; Codex and Antigravity Leads are set from their own app.
- `handback --version`.

### Changed

- Running `handback` with no arguments prints help from source. The Windows exe
  runs the setup wizard instead.
- The Windows startup shortcut repairs itself if the folder was moved.
- Hook-scan checkpoints are deleted when a scan completes. Cursors untouched for
  7 days are pruned (at most 16 per write). `limits.log` is kept.

### Fixed

- Windows sharing violations (`PermissionError`) in state locks and atomic file
  replaces could crash concurrent routers. They are now retried.

## [0.2.4] - 2026-10-09

### Changed

- The compact dashboard widget drags freely over the taskbar and to the monitor
  edges, from anywhere including status counts. It docks only when released
  inside a top or bottom taskbar with at least half of it overlapping. Floating
  widgets have rounded corners and an outline on Windows.
- The dashboard now starts as a small widget (`docked` default changed from
  true to false). It docks only when released inside the taskbar.
- Long expanded panels scroll, including pinned panels. `+N` turns yellow when
  the remaining projects have unread results. Unchanged tooltips and scroll
  positions survive refresh.

### Fixed

- The dashboard shows state read errors instead of an empty project list.
- The recovery hook resumes bounded directory scans from a per-scope checkpoint
  instead of restarting at the 4096-entry cap, so later entries are no longer
  skipped on every call. Incomplete scans are reported in the hook context and
  `hook-scans/limits.log`.

## [0.2.3] - 2026-10-09

### Fixed

- A collected result is kept until it is published. A transient failure while
  saving the result mail or request state no longer stops the watcher from
  completing the request, and the worker is never asked again.
- `doctor` reports a missing Codex executable or `queue --thread` support as a
  failure only when Codex is the selected Lead or a worker. Otherwise these
  checks are advisory warnings.
- On Windows, reading relay state retries briefly when another process is
  replacing the same file, instead of failing the router or collector.

## [0.2.2] - 2026-10-09

### Fixed

- An Antigravity Stop that fires before its observation file exists is now
  collected once instead of being missed.
- Codex install discovery is shared by the adapter and `install-skills`, so
  version-specific install paths are found by both.
- Releasing a project from the dashboard re-checks open requests under the
  state lock, so a request created meanwhile is never archived.
- Open menus, submenus, and focus survive the periodic dashboard refresh.
- The dashboard redraws when its error state changes, and the model/reasoning
  dialog resizes after showing or clearing errors.

### Changed

- Common dashboard errors are worded in Korean, the compact font is slightly
  larger, and the panel responds to Enter/Space/Esc.
- The release workflow publishes only after the Windows test matrix passes.

## [0.2.1] - 2026-10-09

### Added

- Per-project model and reasoning defaults for Codex Lead and worker roles, with
  CLI overrides, inheritance, and a dashboard settings dialog. Antigravity worker
  models can be selected before their first conversation is created.
- Free movement of the compact dashboard widget, snapping to top/bottom taskbars
  within about 20 DPI-scaled pixels, and persistent horizontal/vertical position
  and docking state. Off-screen positions are clamped back onto the screen.
- Automatic hiding of the docked or floating compact widget while a full-screen
  window covers its monitor.

### Fixed

- Pulling the widget away from the taskbar now undocks reliably at scaled DPI.
- Unchanged widget content no longer triggers periodic repaint flicker.
- Fast dragging follows the current pointer position without lagging behind
  queued motion events.

## [0.2.0] - 2026-10-09

### Added

- First public PyPI release of the local relay, using Python's standard library,
  with `handback` and optional `handback-dashboard` entry points.
- Supported Claude Lead → Codex and/or Antigravity worker combinations and
  Codex Lead → Antigravity, with durable requests, result collection, inbox ACK,
  recovery commands, diagnostics, and local usage statistics.
- A compact two-line taskbar widget, expanded project panel, and project/view
  context menus, including unread-result acknowledgement and project release.

### Changed

- Renamed agent-relay to handback, including the package, commands, and installed
  skills. The Windows state default is `%USERPROFILE%\.handback`, selected with
  `HANDBACK_HOME`; project policy uses `.handback.json`. Legacy state is detected
  with migration guidance, legacy policy remains readable with a warning, and
  relay markers and state formats are preserved.
- README links use absolute URLs so they work on PyPI.

### Fixed

- The docked widget restores its position above the Windows taskbar when covered.
- Legacy-state checks recognize chained `MOVED.json` receipts, including stores
  migrated through `.agent-relay` to `.handback`.
- Windows extended-length paths no longer cause valid project-state paths to be
  rejected during concurrent delivery checks.
- The release version check imports the checkout when run from a temporary
  directory.

[0.3.2]: https://github.com/Femur-0607/handback/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/Femur-0607/handback/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Femur-0607/handback/compare/v0.2.4...v0.3.0
[0.2.4]: https://github.com/Femur-0607/handback/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/Femur-0607/handback/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/Femur-0607/handback/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Femur-0607/handback/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Femur-0607/handback/releases/tag/v0.2.0
