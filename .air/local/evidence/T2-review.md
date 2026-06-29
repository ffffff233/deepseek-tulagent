## Project Review Notes

| Area | Finding | Severity | Recommendation |
|---|---|---:|---|
| Terminal slash palette | `/subagents` in quick palette caused users to accidentally enter delegation behavior. | High | Fixed in v0.1.49 by hiding it from quick palette. |
| Terminal palette exit | Footer did not mention Ctrl-C/Ctrl-D and Ctrl-C raised out of palette instead of returning cleanly. | Medium | Fixed in v0.1.49. |
| Subagent cancellation | delegate_agent had no parent cancellation hook, so a nested subagent could keep the turn occupied. | High | Fixed with `should_cancel` propagation. |
| Non-streaming API calls | A blocking HTTP/model request cannot be soft-cancelled until the request returns. | Medium | Future improvement: pass cancellation into provider layer with short read timeouts or async client cancellation. |
| Historical docs | Older changelog entries still say slash palette exposes subagents. | Low | Kept as historical record; v0.1.49 changelog documents current behavior. |
| User rescue path | If a terminal is already stuck in a previous installed version, code changes do not release that running process. | Medium | User should press Ctrl-C twice, Ctrl-D, or kill the process; update to v0.1.49 after exit. |

Verification:
- `pytest -q`: 120 passed.
- Manual TTY: started v0.1.49, opened `/`, verified no `/subagents`, Ctrl-C returned to prompt, second Ctrl-C exited.
