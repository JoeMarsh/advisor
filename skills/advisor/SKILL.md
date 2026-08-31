---
name: advisor
description: Enable, disable, inspect, reset, or configure the local Oh My Pi-style advisor for Codex. Use when the user invokes $advisor, /advisor, the Advisor picker item, or asks to manage the advisor model or status.
---

# Advisor controls

The app may serialize the picker invocation as `[$advisor:advisor](.../skills/advisor/SKILL.md)` followed by optional arguments. Treat that exactly like `/advisor` followed by those arguments.

The synchronous `UserPromptSubmit` hook normally executes the control before this skill is loaded. If developer context says the Advisor control was already executed, do not run it again and do not do unrelated work; report that result concisely.

Only if the hook did not execute, resolve the plugin root by walking two parents up from this file and run `scripts/advisor_ctl.py` with the requested command:

- `$advisor` or `/advisor` with no arguments -> `toggle`

- `$advisor status` -> `status`
- `$advisor on` -> `on`
- `$advisor off` -> `off`
- `$advisor reset` -> `reset`
- `$advisor dump` -> `dump`
- `$advisor usage` -> `usage` (show the most recently active task's main/advisor token comparison)
- `$advisor feed` -> call the `show_advisor_feed` tool from the `advisor-feed` MCP server for the current task. This opens a read-only, auto-refreshing feed; do not run `advisor_ctl.py`.
- `$advisor model <model> [reasoning]` -> `configure --model <model> --reasoning <reasoning>`

The default is `gpt-5.6-luna` with `max` reasoning. Advice is routed only to the root or subagent transcript that produced it; never copy subagent advice into the root agent's context. The separate feed is the user oversight surface. Explain that Codex plugins cannot register native slash commands; `$advisor` and the app's Advisor picker item are the plugin control surfaces. A newly installed or updated hook or MCP plugin is loaded in a new Codex task.
