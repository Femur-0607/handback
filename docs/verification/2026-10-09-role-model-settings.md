# Role model and reasoning settings verification

Checks recorded on **October 9, 2026** used Windows, Codex CLI **0.162.0-alpha.2**, and desktop bundle **26.1002.7124.0**. These are observations of those installed builds, not a compatibility guarantee for later versions.

No model turn was executed. Native app-server checks used isolated temporary configuration and state. The queue command was mocked in the persistence checks; a separate real-CLI check sent requests only to a fake loopback WebSocket endpoint. Installed desktop source was inspected without reading user conversation logs. No existing user conversation settings or live application database rows were changed.

## Native persistence

| Check | Verified result |
|---|---|
| Create a thread with configured reasoning, set its name, explicitly call `thread/settings/update` with `model` and `effort`, then close stdin and wait for normal exit | A new app-server process's `thread/read` returned the selected model and `ultra`. |
| Resume an isolated existing thread with `excludeTurns: true`, update `model` and `effort`, and close normally | A fresh adapter restored the updated `high` value. Read-only sandbox, disabled network access, and approval policy `never` were preserved. |
| Rely on a creation response and stop the app-server immediately | Preliminary reproductions returned null stored model/reasoning metadata. A successful response alone did not prove durable persistence. |
| Add generic `--model` and `-c model_reasoning_effort=...` flags to the real `queue` CLI while capturing its fake-endpoint request | The `thread/queue/add` payload contained neither model nor effort. These flags did not carry the selection to the queue request. |

The implementation therefore saves native settings explicitly before opening the new conversation or queueing a message. App-server shutdown first allows up to five seconds for normal EOF exit. Termination and killing are fallback cleanup only; failure to exit normally prevents opening or queueing because persistence is uncertain.

## Desktop source evidence

Static inspection of the installed `app.asar` found the following in `webview/assets/app-shared-40678a67f0e3.js`:

- Stored thread metadata maps `model` and `reasoningEffort` into the conversation's current model/reasoning fields.
- Queued follow-up execution calls `thread/queue/start` with thread/submission identifiers, without model or effort overrides.
- A `thread/settings/updated` notification updates an already-loaded conversation's cached settings.

This supports native metadata restoration when the app loads a newly created conversation. It does **not** establish that a settings update from a separate private app-server process reaches a conversation already loaded by the desktop app. No reliable shared local IPC update route was established during these checks.

## Remaining limits

- Actual model effort on a first or subsequent live turn was not checked.
- Changing an already-loaded desktop conversation's actual next-turn settings through `send` remains unverified; the stored settings update is implemented, but runtime settings can be cached.
- Direct user turns entered in the app were not tested.
- Antigravity help exposed `new-conversation --model=flash_lite|flash|pro` and no reasoning control or existing-conversation model-change option. Help inspection did not execute an Antigravity task.

Automated regression tests separately cover role/shared/thread precedence, clearing to inheritance, accepted and uncertain delivery, confirmed-completion persistence, Lead routing, and the dashboard settings editor. They do not replace the live-turn checks above.

The full `python -m unittest` regression run completed successfully: 396 tests, including one skip. The final transport revision was separately verified with 34 passing `tests.test_codex` cases after its last code changes. The role configuration, CLI, router, and collector modules also passed all 95 of their tests. The dashboard editor's five cases were included in the full regression run.
