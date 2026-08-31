from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from advisor_devtools_proxy import ensure_broker, shutdown_broker
from advisor_process import (
    advisor_detached_process_kwargs,
    process_is_running,
    terminate_pid_tree,
)

from advisor_common import (
    FileLock,
    append_delivery,
    drain_deliveries,
    compact_usage_footer,
    format_advisories,
    load_config,
    load_json,
    load_usage_state,
    plugin_root,
    record_visible_advisories,
    reset_guard,
    save_json,
    session_dir,
    update_main_usage,
)


MAX_ITEM_CHARS = 40_000
MAX_UPDATE_CHARS = 64_000
WORKER_STALE_SECONDS = 30
DEVTOOLS_SERVER = "godot-rust-devtools"
DEVTOOLS_TOOL_GROUPS = {
    "lsp": [
        "devtools_status",
        "lsp_start",
        "lsp_query",
        "lsp_diagnostics",
        "lsp_stop",
    ],
    "debug": [
        "devtools_status",
        "debug_start",
        "debug_launch",
        "debug_breakpoints",
        "debug_control",
        "debug_inspect",
        "debug_evaluate",
        "debug_wait",
        "debug_stop",
    ],
}


def hook_event(payload: dict[str, Any]) -> str:
    return str(payload.get("hook_event_name") or payload.get("hookEventName") or "")


def session_identity(payload: dict[str, Any]) -> str:
    return str(
        payload.get("session_id")
        or payload.get("sessionId")
        or payload.get("thread_id")
        or payload.get("transcript_path")
        or payload.get("transcriptPath")
        or "unknown-session"
    )


def transcript_path(payload: dict[str, Any]) -> Path | None:
    raw = payload.get("transcript_path") or payload.get("transcriptPath")
    return Path(str(raw)).resolve() if raw else None


def session_cwd(payload: dict[str, Any]) -> Path:
    raw = payload.get("cwd") or os.getcwd()
    return Path(str(raw)).resolve()


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("input_text") or item.get("output_text")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return ""


def clipped(value: Any, limit: int = MAX_ITEM_CHARS) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated {len(text) - limit} characters]"


def render_transcript_entry(entry: dict[str, Any]) -> str | None:
    entry_type = entry.get("type")
    payload = entry.get("payload", {})
    if entry_type == "response_item" and isinstance(payload, dict):
        item_type = payload.get("type")
        if item_type == "message":
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                return None
            text = text_from_content(payload.get("content"))
            if not text or "<advisory" in text:
                return None
            return f"[{role}]\n{clipped(text)}"
        if item_type == "reasoning":
            summary = text_from_content(payload.get("summary"))
            return f"[assistant reasoning summary]\n{clipped(summary)}" if summary else None
        if item_type in {"custom_tool_call", "function_call"}:
            name = payload.get("name", "tool")
            arguments = payload.get("input", payload.get("arguments", ""))
            return f"[tool call: {name}]\n{clipped(arguments)}"
        if item_type in {"custom_tool_call_output", "function_call_output"}:
            output = payload.get("output", payload.get("content", ""))
            return f"[tool result]\n{clipped(output)}"
    if entry_type == "compacted":
        return f"[session compacted]\n{clipped(payload)}"
    return None


def read_transcript_delta(path: Path | None, cursor: int) -> tuple[str, int]:
    if path is None or not path.is_file():
        return "", cursor
    size = path.stat().st_size
    if cursor < 0 or cursor > size:
        cursor = 0
    with path.open("rb") as stream:
        stream.seek(cursor)
        raw = stream.read()
    last_newline = raw.rfind(b"\n")
    if last_newline < 0:
        return "", cursor
    complete = raw[:last_newline + 1]
    new_cursor = cursor + len(complete)
    rendered: list[str] = []
    for line in complete.decode("utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            value = render_transcript_entry(entry)
            if value:
                rendered.append(value)
    text = "\n\n".join(rendered)
    if len(text) > MAX_UPDATE_CHARS:
        text = "[earlier delta truncated]\n" + text[-MAX_UPDATE_CHARS:]
    return text, new_cursor


def discover_context_files(cwd: Path) -> list[Path]:
    candidates: list[Path] = []
    user_candidates = [Path.home() / ".codex" / "AGENTS.md", Path.home() / ".agents" / "AGENTS.md"]
    candidates.extend(path for path in user_candidates if path.is_file())
    lineage = list(reversed([cwd, *cwd.parents]))
    candidates.extend(path / "AGENTS.md" for path in lineage if (path / "AGENTS.md").is_file())
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = os.path.normcase(str(path.resolve()))
        if key not in seen:
            seen.add(key)
            unique.append(path.resolve())
    return unique


def render_context_prompt(cwd: Path) -> str:
    files = discover_context_files(cwd)
    if not files:
        return ""
    template = (plugin_root() / "prompts" / "context-files.md").read_text(encoding="utf-8")
    start = template.find("{{#each contextFiles}}")
    end = template.find("{{/each}}")
    if start < 0 or end < 0:
        return ""
    prefix = template[:start]
    block = template[start + len("{{#each contextFiles}}"):end]
    suffix = template[end + len("{{/each}}"):]
    rendered = []
    for path in files:
        rendered.append(
            block.replace("{{path}}", str(path)).replace(
                "{{content}}", path.read_text(encoding="utf-8", errors="replace")
            )
        )
    return prefix + "".join(rendered) + suffix


def direct_child_repo(cwd: Path) -> Path | None:
    if (cwd / ".git").exists():
        return None
    try:
        repos = [child for child in cwd.iterdir() if child.is_dir() and (child / ".git").exists()]
    except OSError:
        return None
    return repos[0] if len(repos) == 1 else None


def watchdog_files(cwd: Path) -> list[Path]:
    candidates = [Path.home() / ".codex" / "WATCHDOG.yml"]
    candidates.extend(base / "WATCHDOG.yml" for base in reversed([cwd, *cwd.parents]))
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        key = os.path.normcase(str(path.resolve()))
        if key not in seen:
            seen.add(key)
            unique.append(path.resolve())
    return unique


def parse_watchdog_advisor_tools(text: str) -> set[str]:
    tools: set[str] = set()
    advisors_indent: int | None = None
    tools_indent: int | None = None
    for raw_line in text.splitlines():
        content = raw_line.split("#", 1)[0].rstrip()
        if not content.strip():
            continue
        indent = len(content) - len(content.lstrip())
        stripped = content.strip()
        if advisors_indent is None:
            if stripped == "advisors:":
                advisors_indent = indent
            continue
        if indent <= advisors_indent and stripped != "advisors:":
            break
        if tools_indent is not None and indent <= tools_indent:
            tools_indent = None
        if stripped == "tools:":
            tools_indent = indent
            continue
        if tools_indent is not None and indent > tools_indent and stripped.startswith("-"):
            name = stripped[1:].strip().strip("\"'")
            if name:
                tools.add(name)
    return tools


def requested_advisor_tools(cwd: Path) -> set[str]:
    tools: set[str] = set()
    for path in watchdog_files(cwd):
        tools.update(parse_watchdog_advisor_tools(path.read_text(encoding="utf-8", errors="replace")))
    return tools


def devtools_mcp_script() -> Path | None:
    configured = os.environ.get("ADVISOR_DEVTOOLS_MCP")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path.home() / "plugins" / DEVTOOLS_SERVER / "scripts" / "mcp-server.mjs")
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    cache = codex_home / "plugins" / "cache" / "personal" / DEVTOOLS_SERVER
    if cache.is_dir():
        candidates.extend(
            path / "scripts" / "mcp-server.mjs"
            for path in sorted(cache.iterdir(), key=lambda item: item.name, reverse=True)
            if path.is_dir()
        )
    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


def build_system_prompt(cwd: Path) -> str:
    parts = [(plugin_root() / "prompts" / "system.md").read_text(encoding="utf-8")]
    context = render_context_prompt(cwd)
    if context:
        parts.append(context)
    repo = direct_child_repo(cwd)
    if repo:
        watchdog = (plugin_root() / "prompts" / "active-repo-watchdog.md").read_text(encoding="utf-8")
        parts.append(watchdog.replace("{{relativeRepoRoot}}", repo.relative_to(cwd).as_posix()))
    for watchdog_file in watchdog_files(cwd):
        parts.append(watchdog_file.read_text(encoding="utf-8", errors="replace"))
    return "\n\n".join(part.rstrip() for part in parts if part.strip()) + "\n"


def toml_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def mcp_overrides(
    session: Path,
    cwd: Path,
    update_id: str | None = None,
    in_progress: bool = False,
    runtime_file: Path | None = None,
) -> list[str]:
    script = plugin_root() / "scripts" / "advisor_mcp.py"
    args = [str(script), "--session-dir", str(session), "--cwd", str(cwd)]
    if runtime_file is not None:
        args.extend(["--runtime-file", str(runtime_file)])
    else:
        args.extend(["--update-id", str(update_id or "")])
    if in_progress and runtime_file is None:
        args.append("--in-progress")
    overrides = [
        f"mcp_servers.advisor.command={toml_value(sys.executable)}",
        "mcp_servers.advisor.args=" + json.dumps(args, ensure_ascii=False),
        "mcp_servers.advisor.enabled_tools=[\"advise\",\"read\",\"grep\",\"glob\"]",
        "mcp_servers.advisor.default_tools_approval_mode=\"approve\"",
        "mcp_servers.advisor.startup_timeout_sec=15",
        "mcp_servers.advisor.tool_timeout_sec=120",
    ]
    requested = requested_advisor_tools(cwd)
    selected: list[str] = []
    for group in ("lsp", "debug"):
        if group in requested:
            for tool in DEVTOOLS_TOOL_GROUPS[group]:
                if tool not in selected:
                    selected.append(tool)
    devtools_script = devtools_mcp_script()
    if selected and devtools_script:
        try:
            state_file = ensure_broker(session / "devtools-broker.json", cwd, devtools_script)
        except (OSError, RuntimeError) as error:
            log_error(session, f"devtools broker startup failed: {error}")
        else:
            proxy = plugin_root() / "scripts" / "advisor_devtools_proxy.py"
            overrides.extend([
                f"mcp_servers.{DEVTOOLS_SERVER}.command={toml_value(sys.executable)}",
                f"mcp_servers.{DEVTOOLS_SERVER}.args=" + json.dumps(
                    [str(proxy), "--state-file", str(state_file)], ensure_ascii=False
                ),
                f"mcp_servers.{DEVTOOLS_SERVER}.cwd={toml_value(str(cwd))}",
                f"mcp_servers.{DEVTOOLS_SERVER}.enabled_tools=" + json.dumps(selected),
                f"mcp_servers.{DEVTOOLS_SERVER}.default_tools_approval_mode=\"writes\"",
                f"mcp_servers.{DEVTOOLS_SERVER}.startup_timeout_sec=30",
                f"mcp_servers.{DEVTOOLS_SERVER}.tool_timeout_sec=120",
            ])
    return overrides


def complete_transcript_cursor(path: Path | None) -> int:
    if path is None or not path.is_file():
        return 0
    with path.open("rb") as stream:
        raw = stream.read()
    newline = raw.rfind(b"\n")
    return newline + 1 if newline >= 0 else 0


def worker_state_path(session: Path) -> Path:
    return session / "worker.json"


def queue_state_path(session: Path) -> Path:
    return session / "queue.json"


def live_worker_pid(session: Path) -> int | None:
    with FileLock(session / "worker-state.lock", timeout=10, stale_after=30):
        state = load_json(worker_state_path(session), {})
        if not isinstance(state, dict):
            return None
        try:
            pid = int(state.get("pid", 0))
        except (TypeError, ValueError):
            return None
        if not process_is_running(pid):
            return None
        now = time.time()
        try:
            heartbeat = float(state.get("heartbeat", 0))
        except (TypeError, ValueError):
            heartbeat = 0
        try:
            started_at = float(state.get("started_at", 0))
        except (TypeError, ValueError):
            started_at = 0
        fresh_start = (
            state.get("status") == "starting"
            and started_at
            and now - started_at <= WORKER_STALE_SECONDS
        )
        if (heartbeat and now - heartbeat <= WORKER_STALE_SECONDS) or fresh_start:
            return pid
        terminate_pid_tree(pid)
        state.update({
            "status": "stale",
            "stopped_at": now,
            "heartbeat": now,
            "app_server_pid": None,
        })
        save_json(worker_state_path(session), state)
    append_delivery(
        session,
        warning="Advisor worker heartbeat expired; its process tree was terminated and restarted.",
    )
    return None


def ensure_worker(session: Path) -> int:
    with FileLock(session / "worker-start.lock", timeout=15, stale_after=30):
        pid = live_worker_pid(session)
        if pid is not None:
            return pid
        worker = plugin_root() / "scripts" / "advisor_worker.py"
        environment = os.environ.copy()
        environment["CODEX_ADVISOR_NESTED"] = "1"
        process = subprocess.Popen(
            [sys.executable, str(worker), "--session-dir", str(session)],
            cwd=session,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **advisor_detached_process_kwargs(),
        )
        with FileLock(session / "worker-state.lock", timeout=10, stale_after=30):
            save_json(worker_state_path(session), {
                "pid": process.pid,
                "status": "starting",
                "started_at": time.time(),
                "heartbeat": time.time(),
            })
        return process.pid


def stop_worker(session: Path, wait_seconds: float = 2.0) -> None:
    with FileLock(session / "queue.lock", timeout=10, stale_after=30):
        state = load_json(queue_state_path(session), {})
        if not isinstance(state, dict):
            state = {}
        state["shutdown"] = True
        state["generation"] = int(state.get("generation", 0)) + 1
        save_json(queue_state_path(session), state)
    pid = live_worker_pid(session)
    if pid is not None:
        deadline = time.monotonic() + wait_seconds
        while process_is_running(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if process_is_running(pid):
            terminate_pid_tree(pid)
    shutdown_broker(session / "devtools-broker.json")


def reset_queue(session: Path, path: Path | None, cwd: Path) -> None:
    cursor = complete_transcript_cursor(path)
    with FileLock(session / "queue.lock", timeout=10, stale_after=30):
        previous = load_json(queue_state_path(session), {})
        generation = int(previous.get("generation", 0)) + 1 if isinstance(previous, dict) else 1
        save_json(queue_state_path(session), {
            "transcript": str(path) if path else None,
            "cwd": str(cwd),
            "desired_cursor": cursor,
            "processed_cursor": cursor,
            "generation": generation,
            "processed_generation": generation,
            "latest_event": "SessionStart",
            "shutdown": False,
            "updated_at": time.time(),
        })


def enqueue_update(session: Path, path: Path | None, cwd: Path, event: str) -> int:
    desired_cursor = complete_transcript_cursor(path)
    with FileLock(session / "queue.lock", timeout=10, stale_after=30):
        state = load_json(queue_state_path(session), {})
        if not isinstance(state, dict):
            state = {}
        transcript_key = str(path) if path else None
        if state.get("transcript") != transcript_key:
            state = {
                "processed_cursor": 0,
                "processed_generation": 0,
                "generation": int(state.get("generation", 0)),
            }
        generation = int(state.get("generation", 0)) + 1
        state.update({
            "transcript": transcript_key,
            "cwd": str(cwd),
            "desired_cursor": max(int(state.get("desired_cursor", 0)), desired_cursor),
            "generation": generation,
            "latest_event": event,
            "shutdown": False,
            "updated_at": time.time(),
        })
        save_json(queue_state_path(session), state)
        return generation


def wait_for_generation(session: Path, generation: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = load_json(queue_state_path(session), {})
        if isinstance(state, dict) and int(state.get("processed_generation", 0)) >= generation:
            return
        if live_worker_pid(session) is None:
            ensure_worker(session)
        time.sleep(0.1)


def hook_output(
    event: str,
    notes: list[dict[str, str]],
    payload: dict[str, Any],
    usage_footer: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    warnings = warnings or []
    content = format_advisories(notes) if notes else ""
    visible_parts = [part for part in [content, *warnings, usage_footer if event == "Stop" or notes else None] if part]
    visible_content = "\n\n".join(visible_parts)
    if not visible_content:
        return {}
    if event == "PostToolUse":
        output: dict[str, Any] = {"systemMessage": visible_content}
        if content:
            output["hookSpecificOutput"] = {
                "hookEventName": "PostToolUse",
                "additionalContext": content,
            }
        return output
    if event == "Stop":
        has_blocker = any(note.get("severity") == "blocker" for note in notes)
        already_continuing = bool(payload.get("stop_hook_active") or payload.get("stopHookActive"))
        if has_blocker and not already_continuing:
            return {"decision": "block", "reason": content, "systemMessage": visible_content}
        return {"systemMessage": visible_content}
    if event == "SessionStart":
        return {"systemMessage": visible_content}
    return {}


def log_error(session: Path, message: str) -> None:
    try:
        with (session / "advisor.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def main() -> int:
    if os.environ.get("CODEX_ADVISOR_NESTED") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    config = load_config()
    if not config.get("enabled", True):
        return 0

    event = hook_event(payload)
    session = session_dir(session_identity(payload))
    path = transcript_path(payload)
    cwd = session_cwd(payload)
    update_main_usage(session, path)

    if event == "SessionEnd":
        stop_worker(session)
        return 0

    if event == "SessionStart":
        if payload.get("source") in {"compact", "clear"}:
            stop_worker(session)
            reset_guard(session)
            runtime = load_json(session / "runtime.json", {})
            if isinstance(runtime, dict):
                runtime["thread_id"] = None
                save_json(session / "runtime.json", runtime)
        reset_queue(session, path, cwd)
        ensure_worker(session)
        usage_state = load_usage_state(session)
        output = hook_output(
            "SessionStart",
            [],
            payload,
            warnings=[
                f"Advisor active · {config['model']} {config['reasoning_effort']} · persistent worker"
            ],
        )
        if output:
            json.dump(output, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
        return 0

    if event == "PostCompact":
        stop_worker(session)
        reset_guard(session)
        runtime = load_json(session / "runtime.json", {})
        if isinstance(runtime, dict):
            runtime["thread_id"] = None
            save_json(session / "runtime.json", runtime)
        reset_queue(session, path, cwd)
        ensure_worker(session)
        return 0

    generation = enqueue_update(session, path, cwd, event)
    ensure_worker(session)

    notes, warnings = drain_deliveries(session)
    if not notes and not warnings:
        if event == "Stop":
            wait_for_generation(
                session,
                generation,
                float(config.get("timeout_seconds", 300)) + 20,
            )
        notes, warnings = drain_deliveries(session)

    if notes:
        usage_state = record_visible_advisories(session, notes)
    else:
        usage_state = load_usage_state(session)
    output = hook_output(
        event,
        notes,
        payload,
        compact_usage_footer(usage_state),
        warnings,
    )
    if output or event == "Stop":
        json.dump(output, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
