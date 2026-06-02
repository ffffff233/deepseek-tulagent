# Changelog

## Unreleased

- Fixed raw-terminal palette rendering by using CRLF line endings and byte-level key reads; width clipping now uses the real terminal column count.
- Reworked the slash palette into a plain left-aligned vertical list and made arrow-key parsing more tolerant; `j/k` also move the selection.
- Added version/update commands and startup update checks against GitHub tags.
- Added safe update behavior: user config, API keys, model defaults, sessions, skills, and uncommitted source edits are not overwritten.
- Slash palette skill selection now inserts `Use skill <name>: ` into the composer so the user can continue typing a task and send it to the agent.
- Added an interactive `/model` picker that switches the current session model.
- Moved the slash command palette into an isolated terminal screen to avoid corrupting chat layout.
- Added fallback execution for assistant-emitted action shell blocks, including multiple `bash` blocks in one response.
- Added Chinese documentation and a prominent Simplified Chinese entry link in the default README.
