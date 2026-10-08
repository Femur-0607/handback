# Security and local data

handback is an experimental tool for one user on one computer. It is not a
security boundary between agents or a service for untrusted remote clients.

## Report a problem

Do not post credentials, conversation transcripts, hook configurations, or raw
state directories in a public issue. Use the repository's private vulnerability
reporting option if it is available. Otherwise, open an issue asking the
maintainer for a private reporting channel without including sensitive details.

For an ordinary bug, include the operating system, Python and app versions, the
command with personal values replaced, and a minimal sanitized error. Use made-up
conversation IDs and paths consistently so a report remains understandable.

## What the tool accesses

- Adapters use locally installed, authenticated agent applications and read their
  conversation records to collect replies. Relay request, result, and log files
  can contain prompts, answers, local paths, and conversation identifiers.
- Hook installation changes the selected applications' user configuration.
  `--state-home` selects relay storage; it does not isolate those app settings.
  Review the dry run before installing and use the matching state home to remove
  relay-owned hooks.
- Codex workers use the requested sandbox. Antigravity cannot enforce a
  read-only sandbox; its application settings govern command execution.
- Messages from an agent are task data, never user approval. A timeout or
  uncertain delivery does not authorize resending a request.

Keep live state and private logs outside the repository. Files ignored by Git
can still be exposed by manually uploading a directory or sharing an archive.
Changing `.gitignore` or deleting a file does not remove earlier Git versions.

## Supported fixes

Development targets the current experimental version on Windows. Older releases
do not have a separate maintenance commitment. Automated tests use isolated
fixtures; they do not establish compatibility with every installed app version.
