# AirCodex Plan

## Goal

Fix the interactive slash-command flow where pressing `/` can trap the user in a palette/subagent-related command path that does not answer clearly and is hard to exit. Then inspect the project for other concrete quality issues, validate the fix, and update the GitHub project with a concise release record.

## Task Boundaries

- T1: Diagnose and repair terminal slash-command/palette behavior and subagent command usability.
- T2: Review adjacent CLI/TUI/desktop areas for obvious regressions or improvement opportunities.
- T3: Run tests/static checks, update changelog/version/readme links, and publish via GitHub CLI.

## Validation Strategy

- Add regression tests around slash item behavior and palette affordances where possible without requiring a real TTY.
- Run the existing pytest suite.
- Inspect git diff for unrelated churn.
- Use `gh` to update the repository after local verification.
