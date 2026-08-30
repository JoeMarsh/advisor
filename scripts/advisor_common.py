from __future__ import annotations

import hashlib
import html
import json
import os
import time
import unicodedata
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SEVERITY_RANK = {"nit": 0, "concern": 1, "blocker": 2}
SUPPRESSED_PHRASES = {
    "", "stop", "stop here", "stop now", "halt", "abort", "done", "task done",
    "task complete", "complete", "finished", "ok", "okay", "ok done", "no issue",
    "no issues", "no issue continue", "no concerns", "no concern", "nothing to add",
    "nothing to flag", "nothing to report", "no notes", "no further input",
    "no further input needed", "no further input required", "no further watcher input",
    "no further watcher input needed", "no further advice", "no further advice needed",
    "lgtm", "looks good", "all good", "agent is on track", "agent on track",
    "on track", "continue", "carry on",
}
SEEN_LIMIT = 4096
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def plugin_root() -> Path:
    return Path(os.environ.get("PLUGIN_ROOT", Path(__file__).resolve().parents[1])).resolve()


def installed_plugin_data_root(root: Path, codex_home: Path) -> Path | None:
    try:
        relative = root.resolve().relative_to((codex_home / "plugins" / "cache").resolve())
    except ValueError:
        return None
    if len(relative.parts) < 3:
        return None
    marketplace, plugin_name = relative.parts[:2]
    return codex_home / "plugins" / "data" / f"{plugin_name}-{marketplace}"


def data_root() -> Path:
    configured = os.environ.get("PLUGIN_DATA")
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    root = Path(configured) if configured else installed_plugin_data_root(plugin_root(), codex_home)
    if root is None:
        root = codex_home / "plugin-data" / "advisor"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def default_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "immune_turns": 3,
        "timeout_seconds": 300,
        "coalesce_milliseconds": 350,
        "worker_idle_seconds": 3600,
    }


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def load_config() -> dict[str, Any]:
    value = default_config()
    stored = load_json(data_root() / "config.json", {})
    if isinstance(stored, dict):
        value.update(stored)
    return value


def session_dir(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()[:24]
    path = data_root() / "sessions" / digest
    path.mkdir(parents=True, exist_ok=True)
    return path


def empty_token_usage() -> dict[str, int]:
    return {field: 0 for field in TOKEN_FIELDS}


def normalize_token_usage(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    usage = {
        field: max(0, int(source.get(field, 0) or 0))
        for field in TOKEN_FIELDS
    }
    if not usage["total_tokens"]:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def add_token_usage(left: Any, right: Any) -> dict[str, int]:
    first = normalize_token_usage(left)
    second = normalize_token_usage(right)
    return {field: first[field] + second[field] for field in TOKEN_FIELDS}


def default_usage_state() -> dict[str, Any]:
    return {
        "updated_at": None,
        "main": {
            "totals": empty_token_usage(),
            "last_total": empty_token_usage(),
            "requests": 0,
            "cursor": 0,
            "transcript": None,
            "context_window": None,
        },
        "advisor": {
            "totals": empty_token_usage(),
            "invocations": 0,
            "successful_reviews": 0,
            "failed_reviews": 0,
            "silent_reviews": 0,
            "visible_advisories": 0,
            "severity": {"nit": 0, "concern": 0, "blocker": 0},
            "model": None,
            "reasoning_effort": None,
        },
    }


def _usage_file(session: Path) -> Path:
    return session / "usage.json"


def load_usage_state(session: Path) -> dict[str, Any]:
    state = load_json(_usage_file(session), default_usage_state())
    if not isinstance(state, dict):
        state = default_usage_state()
    defaults = default_usage_state()
    for section in ("main", "advisor"):
        if not isinstance(state.get(section), dict):
            state[section] = defaults[section]
        for key, value in defaults[section].items():
            state[section].setdefault(key, value)
    state.setdefault("updated_at", None)
    state["main"]["totals"] = normalize_token_usage(state["main"].get("totals"))
    state["main"]["last_total"] = normalize_token_usage(state["main"].get("last_total"))
    state["advisor"]["totals"] = normalize_token_usage(state["advisor"].get("totals"))
    return state


def save_usage_state(session: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_json(_usage_file(session), state)


def _usage_delta(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    if current["total_tokens"] < previous["total_tokens"]:
        return current
    return {field: max(0, current[field] - previous[field]) for field in TOKEN_FIELDS}


def token_usage_delta(current: Any, previous: Any) -> dict[str, int]:
    return _usage_delta(normalize_token_usage(current), normalize_token_usage(previous))


def update_main_usage(session: Path, transcript: Path | None) -> dict[str, Any]:
    state = load_usage_state(session)
    main = state["main"]
    if transcript is None or not transcript.is_file():
        save_usage_state(session, state)
        return state

    transcript_key = str(transcript.resolve())
    cursor = int(main.get("cursor", 0))
    if main.get("transcript") != transcript_key:
        cursor = 0
        main["transcript"] = transcript_key
    size = transcript.stat().st_size
    if cursor < 0 or cursor > size:
        cursor = 0
    with transcript.open("rb") as stream:
        stream.seek(cursor)
        raw = stream.read()
    last_newline = raw.rfind(b"\n")
    if last_newline < 0:
        save_usage_state(session, state)
        return state

    complete = raw[:last_newline + 1]
    main["cursor"] = cursor + len(complete)
    previous = normalize_token_usage(main.get("last_total"))
    totals = normalize_token_usage(main.get("totals"))
    for line in complete.decode("utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = entry.get("payload", {}) if isinstance(entry, dict) else {}
        if entry.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info", {})
        current = normalize_token_usage(info.get("total_token_usage"))
        if current == previous:
            continue
        totals = add_token_usage(totals, _usage_delta(current, previous))
        previous = current
        main["requests"] = int(main.get("requests", 0)) + 1
        if info.get("model_context_window") is not None:
            main["context_window"] = int(info["model_context_window"])
    main["last_total"] = previous
    main["totals"] = totals
    save_usage_state(session, state)
    return state


def record_advisor_invocation(
    session: Path,
    usage: Any,
    succeeded: bool,
    spoke: bool,
    config: dict[str, Any],
) -> dict[str, Any]:
    state = load_usage_state(session)
    advisor = state["advisor"]
    advisor["totals"] = add_token_usage(advisor.get("totals"), usage)
    advisor["invocations"] = int(advisor.get("invocations", 0)) + 1
    if succeeded:
        advisor["successful_reviews"] = int(advisor.get("successful_reviews", 0)) + 1
        if not spoke:
            advisor["silent_reviews"] = int(advisor.get("silent_reviews", 0)) + 1
    else:
        advisor["failed_reviews"] = int(advisor.get("failed_reviews", 0)) + 1
    advisor["model"] = str(config.get("model") or "") or None
    advisor["reasoning_effort"] = str(config.get("reasoning_effort") or "") or None
    save_usage_state(session, state)
    return state


def record_visible_advisories(session: Path, notes: list[dict[str, str]]) -> dict[str, Any]:
    state = load_usage_state(session)
    advisor = state["advisor"]
    advisor["visible_advisories"] = int(advisor.get("visible_advisories", 0)) + len(notes)
    severity = advisor.setdefault("severity", {"nit": 0, "concern": 0, "blocker": 0})
    for note in notes:
        level = note.get("severity") or "nit"
        severity[level] = int(severity.get(level, 0)) + 1
    save_usage_state(session, state)
    return state


def format_token_count(value: int) -> str:
    number = float(value)
    for suffix, scale in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if number >= scale:
            return f"{number / scale:.1f}{suffix}"
    return str(int(number))


def compact_usage_footer(state: dict[str, Any]) -> str:
    main = normalize_token_usage(state.get("main", {}).get("totals"))
    advisor = normalize_token_usage(state.get("advisor", {}).get("totals"))
    combined = main["total_tokens"] + advisor["total_tokens"]
    share = (100.0 * advisor["total_tokens"] / combined) if combined else 0.0
    return (
        "Advisor usage · "
        f"Main {format_token_count(main['total_tokens'])} "
        f"({format_token_count(main['cached_input_tokens'])} cached) · "
        f"Advisor {format_token_count(advisor['total_tokens'])} "
        f"({format_token_count(advisor['cached_input_tokens'])} cached) · "
        f"Advisor share {share:.2f}%"
    )


def format_usage_report(state: dict[str, Any], session_name: str | None = None) -> str:
    main_section = state.get("main", {})
    advisor_section = state.get("advisor", {})
    main = normalize_token_usage(main_section.get("totals"))
    advisor = normalize_token_usage(advisor_section.get("totals"))
    combined = main["total_tokens"] + advisor["total_tokens"]
    share = (100.0 * advisor["total_tokens"] / combined) if combined else 0.0
    ratio = (100.0 * advisor["total_tokens"] / main["total_tokens"]) if main["total_tokens"] else 0.0

    def usage_lines(label: str, usage: dict[str, int]) -> list[str]:
        uncached = max(0, usage["input_tokens"] - usage["cached_input_tokens"])
        return [
            label,
            f"  Total: {usage['total_tokens']:,}",
            f"  Input: {usage['input_tokens']:,}",
            f"  Cached input: {usage['cached_input_tokens']:,}",
            f"  Uncached input: {uncached:,}",
            f"  Output: {usage['output_tokens']:,}",
            f"  Reasoning output: {usage['reasoning_output_tokens']:,}",
        ]

    heading = "Advisor usage — latest task"
    if session_name:
        heading += f" ({session_name})"
    lines = [heading, ""]
    lines.extend(usage_lines("Main agent", main))
    lines.extend(["", *usage_lines("Advisor", advisor), "", "Comparison"])
    lines.extend([
        f"  Advisor share of combined tokens: {share:.2f}%",
        f"  Advisor/Main ratio: {ratio:.2f}%",
        "",
        "Advisor activity",
        f"  Model: {advisor_section.get('model') or 'unknown'}",
        f"  Reasoning: {advisor_section.get('reasoning_effort') or 'unknown'}",
        f"  Invocations: {int(advisor_section.get('invocations', 0))}",
        f"  Successful reviews: {int(advisor_section.get('successful_reviews', 0))}",
        f"  Silent reviews: {int(advisor_section.get('silent_reviews', 0))}",
        f"  Failed reviews: {int(advisor_section.get('failed_reviews', 0))}",
        f"  Visible advisories: {int(advisor_section.get('visible_advisories', 0))}",
    ])
    severity = advisor_section.get("severity", {})
    lines.append(
        "  Severity: "
        f"{int(severity.get('blocker', 0))} blockers, "
        f"{int(severity.get('concern', 0))} concerns, "
        f"{int(severity.get('nit', 0))} nits"
    )
    lines.extend([
        "",
        f"Main model requests observed: {int(main_section.get('requests', 0))}",
        "Token totals are model-processed usage; reasoning output is already included in output.",
    ])
    return "\n".join(lines)


class FileLock(AbstractContextManager["FileLock"]):
    def __init__(self, path: Path, timeout: float = 600.0, stale_after: float = 900.0):
        self.path = path
        self.timeout = timeout
        self.stale_after = stale_after
        self.acquired = False

    def __enter__(self) -> "FileLock":
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()}\n{time.time()}\n".encode("ascii"))
                os.close(fd)
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > self.stale_after:
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for advisor session lock: {self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False


def _delivery_file(session: Path) -> Path:
    return session / "deliveries.json"


def append_delivery(
    session: Path,
    notes: list[dict[str, str]] | None = None,
    warning: str | None = None,
    update_id: str | None = None,
) -> None:
    if not notes and not warning:
        return
    with FileLock(session / "deliveries.lock", timeout=10, stale_after=30):
        deliveries = load_json(_delivery_file(session), [])
        if not isinstance(deliveries, list):
            deliveries = []
        deliveries.append({
            "id": update_id or f"delivery-{time.time_ns()}",
            "notes": [note for note in (notes or []) if isinstance(note, dict)],
            "warning": warning,
            "created_at": time.time(),
        })
        save_json(_delivery_file(session), deliveries[-256:])


def drain_deliveries(session: Path) -> tuple[list[dict[str, str]], list[str]]:
    with FileLock(session / "deliveries.lock", timeout=10, stale_after=30):
        deliveries = load_json(_delivery_file(session), [])
        if not isinstance(deliveries, list):
            deliveries = []
        save_json(_delivery_file(session), [])
    notes: list[dict[str, str]] = []
    warnings: list[str] = []
    for delivery in deliveries:
        if not isinstance(delivery, dict):
            continue
        notes.extend(note for note in delivery.get("notes", []) if isinstance(note, dict))
        warning = delivery.get("warning")
        if isinstance(warning, str) and warning.strip():
            warnings.append(warning.strip())
    return notes, warnings


def normalize_note(note: str) -> str:
    value = unicodedata.normalize("NFKC", note.lower())
    chars = [character if character.isalnum() else " " for character in value]
    return " ".join("".join(chars).split())


def default_guard_state() -> dict[str, Any]:
    return {"seen": [], "delivered": {}, "deferred": [], "updates": {}}


def _state_file(session: Path) -> Path:
    return session / "guard.json"


def _result_file(session: Path, update_id: str) -> Path:
    return session / "updates" / f"{update_id}.json"


def reset_guard(session: Path) -> None:
    save_json(_state_file(session), default_guard_state())


def _load_guard(session: Path) -> dict[str, Any]:
    state = load_json(_state_file(session), default_guard_state())
    if not isinstance(state, dict):
        state = default_guard_state()
    for key, default in default_guard_state().items():
        state.setdefault(key, default)
    return state


def _append_update_result(session: Path, update_id: str, advice: dict[str, str]) -> None:
    path = _result_file(session, update_id)
    result = load_json(path, {"notes": [], "advisor_spoke": False})
    if not isinstance(result, dict) or not isinstance(result.get("notes"), list):
        result = {"notes": [], "advisor_spoke": False}
    result["notes"].append(advice)
    save_json(path, result)


def _mark_update_spoke(session: Path, update_id: str) -> None:
    path = _result_file(session, update_id)
    result = load_json(path, {"notes": [], "advisor_spoke": False})
    if not isinstance(result, dict) or not isinstance(result.get("notes"), list):
        result = {"notes": [], "advisor_spoke": False}
    result["advisor_spoke"] = True
    save_json(path, result)


def start_update(session: Path, update_id: str) -> None:
    path = _result_file(session, update_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, {"notes": [], "advisor_spoke": False})


def read_update_result(session: Path, update_id: str) -> list[dict[str, str]]:
    result = load_json(_result_file(session, update_id), {"notes": []})
    notes = result.get("notes", []) if isinstance(result, dict) else []
    return [note for note in notes if isinstance(note, dict)]


def update_had_advice(session: Path, update_id: str) -> bool:
    result = load_json(_result_file(session, update_id), {})
    return bool(result.get("advisor_spoke")) if isinstance(result, dict) else False


def record_advice(
    session: Path,
    update_id: str,
    note: str,
    severity: str | None = None,
    in_progress: bool = False,
) -> str:
    _mark_update_spoke(session, update_id)
    severity = severity if severity in SEVERITY_RANK else None
    note = note.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    normalized = normalize_note(note)
    delivery_key = " ".join(note.strip().split())
    state = _load_guard(session)

    previous_rank = state["delivered"].get(delivery_key)
    deferred_rank = max(
        (SEVERITY_RANK.get(item.get("severity", "nit"), 0)
         for item in state["deferred"] if item.get("key") == delivery_key),
        default=None,
    )
    current_rank = SEVERITY_RANK[severity or "nit"]
    if ((previous_rank is not None and current_rank <= previous_rank)
            or (deferred_rank is not None and current_rank <= deferred_rank)):
        return "Duplicate advice ignored."

    advice = {"note": note.strip(), "normalized": normalized, "key": delivery_key}
    if severity is not None:
        advice["severity"] = severity

    if in_progress and severity != "blocker":
        pending = next((item for item in state["deferred"] if item.get("key") == delivery_key), None)
        if pending is None:
            state["deferred"].append(advice)
        elif current_rank > SEVERITY_RANK.get(pending.get("severity", "nit"), 0):
            if severity is None:
                pending.pop("severity", None)
            else:
                pending["severity"] = severity
        save_json(_state_file(session), state)
        return (
            "Deferred — primary is mid-turn; this note will be delivered automatically when "
            "the turn completes. Do not re-raise the same point."
        )

    state["delivered"][delivery_key] = current_rank
    update = state["updates"].setdefault(update_id, {"consumed": False})
    suppressed = normalized in SUPPRESSED_PHRASES or normalized in state["seen"] or update.get("consumed")
    if suppressed:
        save_json(_state_file(session), state)
        return "Recorded."
    state["seen"].append(normalized)
    state["seen"] = state["seen"][-SEEN_LIMIT:]
    update["consumed"] = True
    save_json(_state_file(session), state)
    _append_update_result(session, update_id, advice)
    return "Recorded."


def flush_deferred(session: Path, update_id: str) -> list[dict[str, str]]:
    state = _load_guard(session)
    deferred = list(state.get("deferred", []))
    state["deferred"] = []
    for advice in deferred:
        normalized = advice.get("normalized", normalize_note(advice.get("note", "")))
        delivery_key = advice.get("key", " ".join(advice.get("note", "").strip().split()))
        rank = SEVERITY_RANK.get(advice.get("severity", "nit"), 0)
        if rank <= state["delivered"].get(delivery_key, -1):
            continue
        state["delivered"][delivery_key] = rank
        update = state["updates"].setdefault(update_id, {"consumed": False})
        if normalized in SUPPRESSED_PHRASES or normalized in state["seen"] or update.get("consumed"):
            continue
        state["seen"].append(normalized)
        state["seen"] = state["seen"][-SEEN_LIMIT:]
        update["consumed"] = True
        _append_update_result(session, update_id, advice)
    save_json(_state_file(session), state)
    return deferred


def advisory_xml(note: str, severity: str | None = None) -> str:
    escaped_note = html.escape(note.strip(), quote=False)
    severity_attribute = f' severity="{html.escape(severity, quote=True)}"' if severity else ""
    return (
        f'<advisory{severity_attribute} guidance="weigh, don\'t blindly obey">\n'
        f"{escaped_note}\n"
        "</advisory>"
    )


def format_advisories(notes: list[dict[str, str]]) -> str:
    return "\n".join(advisory_xml(note["note"], note.get("severity")) for note in notes)
