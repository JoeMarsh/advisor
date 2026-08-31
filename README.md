# Advisor for Codex

This plugin adapts Oh My Pi's advisor to Codex while retaining the
upstream advisor prompts byte-for-byte. It defaults to `gpt-5.6-luna` at `max`
reasoning.

## Install

Advisor and Autoresearch share the universal JoeMarsh marketplace:

```powershell
codex plugin marketplace add JoeMarsh/CodexPlugins --ref main
codex plugin add advisor@JoeMarsh
```

Alternatively, register Advisor's self-contained marketplace by itself:

```powershell
codex plugin marketplace add JoeMarsh/advisor
codex plugin add advisor@advisor
```

After installation, review and trust the plugin hooks in Codex, then start a
new task so Codex loads them.

## Runtime

`PostToolUse` hooks only coalesce transcript high-water marks and drain completed
advice. One detached, task-scoped worker owns a persistent `codex app-server`
process, one advisor thread, and an independent ordered cursor for every root or
subagent transcript in the task. The worker runs one review at a time and
coalesces each transcript's updates while a review is pending, so Codex's
independently scheduled hook processes cannot create parallel advisor runs,
overwrite another agent's cursor, or recount its token usage. Each fast
synchronous hook returns promptly
after enqueueing the latest high-water mark and surfaces advice completed by the
background worker at the next safe tool boundary. Both ordinary advice and
blockers are visible in chat and injected into the active Codex agent's context. `Stop`
waits for the final high-water mark and continues the task when terminal advice
is a blocker.

Every delivered advisory is also emitted as a visible hook warning. A compact
footer compares exact model-processed token usage for the main task and advisor,
including cached input and the advisor's share of their combined usage. Silent
reviews remain silent.

The advisor runs with read-only sandboxing and always exposes `advise`, `read`,
`grep`, and `glob` through a persistent local MCP server. When an applicable
`WATCHDOG.yml` advisor tool list requests `bash`, the app-server enables Codex's
shell tool for that advisor; `lsp` or `debug` connects to the installed
`godot-rust-devtools` MCP server. One task-scoped broker owns the
stateful devtools process and lightweight stdio proxies reconnect each advisor
turn to it, so LSP/DAP state persists without spawning another Node server after
every tool event. The broker is root-scoped to the task cwd and retires after an
hour without a connection. That server's read-tier inspection tools run without
approval; its exec-tier tools retain `writes` approval mode and are routed
through Codex Auto-review ("Approve for me"). The persistent app-server uses the
current Codex login and is marked so the plugin cannot recursively advise itself.
It receives an isolated task-scoped `CODEX_HOME`, so user hooks, notifications,
plugins, and unrelated MCP servers are not inherited. It runs in its own process
group. Active reviews have no wall-clock deadline: hard tasks and slow model
servers may continue for as long as needed. During otherwise silent model work,
the worker probes the local app-server control plane with metadata-only
`thread/read` requests. Only a dead process, closed protocol stream, or three
unanswered control-plane probes triggers transport recovery. That recovery
terminates the complete app-server descendant tree and retires the devtools
broker and its Node/LSP/DAP descendants before retrying.

Installed plugins store configuration, persistent advisor threads, and usage
counters in their Codex-provided `PLUGIN_DATA` directory (normally
`$CODEX_HOME/plugins/data/advisor-<marketplace>`), so they survive cache-busted
reinstalls. Direct source execution falls back to `$CODEX_HOME/plugin-data/advisor`.

## Data flow

When enabled, the hook sends task transcript deltas plus applicable `AGENTS.md`
and `WATCHDOG.yml` instructions to the configured Codex model using the account
already signed in to Codex. This is required for the advisor to understand and
review work in progress. The local MCP tools are read-only, but their requested
file contents are likewise model-visible. Do not enable the plugin for tasks
whose contents should not be sent to the configured model.

Codex provides background review with synchronous safe-point context injection
rather than Oh My Pi's native token-stream steering. That is the unavoidable
host adapter boundary;
the singleton runtime, per-transcript ordered coalescing queue, persistent
context, prompt, advice formatting, mid-task delivery, final blocker
continuation, and noise/dedup
guards are implemented locally.

## Controls

Use the app's Advisor picker item or bare `$advisor` to toggle the advisor. Add
`status`, `usage`, `on`, `off`, `reset`, or
`$advisor model <model> <reasoning>`. Codex does not currently let a plugin add a
native `/advisor` command, so `$advisor` and the picker item are the control
surface. A synchronous prompt-submit adapter recognizes the app's serialized
skill-link form and applies the control before the main model runs.

`$advisor usage` shows the most recently active task's detailed totals: input,
cached and uncached input, output, reasoning output, main request count, advisor
invocations, silent reviews, visible advisories, severity mix, and comparison
ratios. These are token-processing counters, not unique transcript size or a
currency estimate; reasoning output is already included in output tokens.

## Attribution

The four files under `prompts/` are copied byte-for-byte from Oh My Pi commit
[`33cc6b9a043a74e00a157e72ca909272796d8461`](https://github.com/can1357/oh-my-pi/commit/33cc6b9a043a74e00a157e72ca909272796d8461).
See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and the copied upstream
license under [`third_party/OH_MY_PI_LICENSE`](third_party/OH_MY_PI_LICENSE).
