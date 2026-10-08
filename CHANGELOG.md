# Changelog

All notable changes to this project are documented here. This file follows the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.

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

[0.2.1]: https://github.com/Femur-0607/handback/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Femur-0607/handback/releases/tag/v0.2.0
