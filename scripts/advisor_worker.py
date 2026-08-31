from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from advisor_common import (
    FileLock,
    append_delivery,
    empty_token_usage,
    flush_deferred,
    load_config,
    load_json,
    normalize_token_usage,
    read_update_result,
    record_advisor_invocation,
    save_json,
    start_update,
    token_usage_delta,
    update_had_advice,
)
from advisor_devtools_proxy import shutdown_broker
from advisor_hook import (
    build_system_prompt,
    log_error,
    mcp_overrides,
    normalized_queue_state,
    read_transcript_delta,
    requested_advisor_tools,
)
from advisor_process import advisor_process_group_kwargs, terminate_process_tree


def app_server_usage(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return normalize_token_usage({
        "input_tokens": source.get("inputTokens", source.get("input_tokens", 0)),
        "cached_input_tokens": source.get("cachedInputTokens", source.get("cached_input_tokens", 0)),
        "output_tokens": source.get("outputTokens", source.get("output_tokens", 0)),
        "reasoning_output_tokens": source.get(
            "reasoningOutputTokens", source.get("reasoning_output_tokens", 0)
        ),
        "total_tokens": source.get("totalTokens", source.get("total_tokens", 0)),
    })


class AppServerError(RuntimeError):
    pass


class AppServerUnresponsive(AppServerError):
    pass


APP_SERVER_HEALTH_PROBE_IDLE_SECONDS = 30.0
APP_SERVER_HEALTH_PROBE_TIMEOUT_SECONDS = 30.0
MAX_MISSED_HEALTH_PROBES = 3
MAX_REVIEW_ATTEMPTS = 3


class AppServerClient:
    def __init__(
        self,
        command: list[str],
        cwd: Path,
        environment: dict[str, str],
        log_path: Path,
        progress_callback: Callable[[], None] | None = None,
    ):
        self.command = command
        self.cwd = cwd
        self.environment = environment
        self.log_path = log_path
        self.process: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.request_id = 0
        self.thread_id: str | None = None
        self.latest_usage = empty_token_usage()
        self.completed_items: list[dict[str, Any]] = []
        self.last_turn: dict[str, Any] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self.progress_callback = progress_callback
        self.delivery_transcript: str | None = None
        self.delivery_is_root: bool | None = None

    def _progress(self) -> None:
        if self.progress_callback is not None:
            self.progress_callback()

    def start(self) -> None:
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            env=self.environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            **advisor_process_group_kwargs(),
        )
        self._reader = threading.Thread(target=self._read_stdout, name="advisor-app-server-out", daemon=True)
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name="advisor-app-server-err", daemon=True
        )
        self._reader.start()
        self._stderr_reader.start()
        response = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_advisor",
                    "title": "Codex Advisor",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "optOutNotificationMethods": ["item/agentMessage/delta"],
                },
            },
            timeout=30,
        )
        if "result" not in response:
            raise AppServerError(f"app-server initialize failed: {response}")
        self.notify("initialized", {})

    def _append_log(self, text: str) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(text.rstrip() + "\n")
        except OSError:
            pass

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._append_log(f"app-server non-JSON stdout: {line.rstrip()}")
                continue
            if isinstance(message, dict):
                self.messages.put(message)

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            self._append_log(f"app-server stderr: {line.rstrip()}")

    def _send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.poll() is not None or self.process.stdin is None:
            raise AppServerError("app-server is not running")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def _next_id(self) -> int:
        self.request_id += 1
        return self.request_id

    def _handle_server_request(self, message: dict[str, Any]) -> bool:
        if "id" not in message or "method" not in message:
            return False
        method = str(message.get("method"))
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            warning = (
                f"Advisor requested an interactive approval through {method} after Auto-review; "
                "the headless advisor client declined it."
            )
            self._append_log(warning)
            append_delivery(
                self.log_path.parent,
                warning=warning,
                transcript=self.delivery_transcript,
                is_root=self.delivery_is_root,
            )
            self._send({"id": message["id"], "result": {"decision": "decline"}})
        elif method == "item/permissions/requestApproval":
            warning = (
                "Advisor requested interactive permissions after Auto-review; "
                "the headless advisor client granted none."
            )
            self._append_log(warning)
            append_delivery(
                self.log_path.parent,
                warning=warning,
                transcript=self.delivery_transcript,
                is_root=self.delivery_is_root,
            )
            self._send({"id": message["id"], "result": {"permissions": {}, "scope": "turn"}})
        else:
            self._send({
                "id": message["id"],
                "error": {"code": -32601, "message": f"Advisor client cannot service {method}"},
            })
        return True

    def _observe(self, message: dict[str, Any]) -> None:
        if message.get("method") == "thread/tokenUsage/updated":
            params = message.get("params", {})
            token_usage = params.get("tokenUsage", {}) if isinstance(params, dict) else {}
            if isinstance(token_usage, dict):
                self.latest_usage = app_server_usage(token_usage.get("total"))
        elif message.get("method") == "item/completed":
            params = message.get("params", {})
            item = params.get("item") if isinstance(params, dict) else None
            if isinstance(item, dict):
                self.completed_items.append(item)
        elif message.get("method") == "turn/completed":
            params = message.get("params", {})
            turn = params.get("turn") if isinstance(params, dict) else None
            if isinstance(turn, dict):
                self.last_turn = turn

    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        request_id = self._next_id()
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            self._progress()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerUnresponsive(f"timed out waiting for {method}")
            try:
                message = self.messages.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if self.process is None or self.process.poll() is not None:
                    raise AppServerError(f"app-server exited while waiting for {method}")
                continue
            self._observe(message)
            if self._handle_server_request(message):
                continue
            if message.get("id") == request_id:
                if message.get("error"):
                    raise AppServerError(f"{method} failed: {message['error']}")
                return message

    def open_thread(
        self,
        stored_thread_id: str | None,
        model: str,
        reasoning: str,
        cwd: Path,
        instructions: str,
    ) -> str:
        common = {
            "model": model,
            "cwd": str(cwd),
            "approvalPolicy": "on-request",
            "approvalsReviewer": "auto_review",
            "sandbox": "read-only",
            "baseInstructions": instructions,
            "config": {"model_reasoning_effort": reasoning},
        }
        if stored_thread_id:
            try:
                response = self.request(
                    "thread/resume", {"threadId": stored_thread_id, **common}, timeout=60
                )
            except AppServerError:
                stored_thread_id = None
        if not stored_thread_id:
            response = self.request(
                "thread/start",
                {**common, "ephemeral": False, "serviceName": "codex_advisor"},
                timeout=60,
            )
        thread = response.get("result", {}).get("thread", {})
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise AppServerError(f"thread open returned no id: {response}")
        self.thread_id = thread_id
        return thread_id

    def run_turn(self, update: str, reasoning: str) -> dict[str, int]:
        if not self.thread_id:
            raise AppServerError("advisor thread is not open")
        before = self.latest_usage
        request_id = self._next_id()
        self._send({
            "method": "turn/start",
            "id": request_id,
            "params": {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": update}],
                "effort": reasoning,
                "approvalPolicy": "on-request",
                "sandboxPolicy": {"type": "readOnly", "access": {"type": "fullAccess"}},
            },
        })
        turn_id: str | None = None
        last_protocol_activity = time.monotonic()
        health_probe_id: int | None = None
        health_probe_sent_at = 0.0
        health_probe_ids: set[int] = set()
        missed_health_probes = 0
        while True:
            self._progress()
            now = time.monotonic()
            if health_probe_id is None:
                if now - last_protocol_activity >= APP_SERVER_HEALTH_PROBE_IDLE_SECONDS:
                    health_probe_id = self._next_id()
                    health_probe_ids.add(health_probe_id)
                    health_probe_sent_at = now
                    self._send({
                        "method": "thread/read",
                        "id": health_probe_id,
                        "params": {"threadId": self.thread_id, "includeTurns": False},
                    })
            elif now - health_probe_sent_at >= APP_SERVER_HEALTH_PROBE_TIMEOUT_SECONDS:
                missed_health_probes += 1
                if missed_health_probes >= MAX_MISSED_HEALTH_PROBES:
                    raise AppServerUnresponsive(
                        "app-server control plane did not answer health probes"
                    )
                health_probe_id = self._next_id()
                health_probe_ids.add(health_probe_id)
                health_probe_sent_at = now
                self._send({
                    "method": "thread/read",
                    "id": health_probe_id,
                    "params": {"threadId": self.thread_id, "includeTurns": False},
                })
            try:
                message = self.messages.get(timeout=1.0)
            except queue.Empty:
                if self.process is None or self.process.poll() is not None:
                    raise AppServerError("app-server exited during advisor turn")
                if self._reader is not None and not self._reader.is_alive():
                    raise AppServerError("app-server protocol output closed during advisor turn")
                continue
            is_health_response = message.get("id") in health_probe_ids
            last_protocol_activity = time.monotonic()
            health_probe_id = None
            health_probe_ids.clear()
            missed_health_probes = 0
            self._observe(message)
            if self._handle_server_request(message):
                continue
            if is_health_response:
                continue
            if message.get("id") == request_id:
                result_turn = message.get("result", {}).get("turn", {})
                if message.get("error"):
                    raise AppServerError(f"turn/start failed: {message['error']}")
                if isinstance(result_turn, dict):
                    turn_id = result_turn.get("id") or turn_id
                continue
            if message.get("method") != "turn/completed":
                continue
            params = message.get("params", {})
            completed_turn = params.get("turn", {}) if isinstance(params, dict) else {}
            completed_id = completed_turn.get("id") if isinstance(completed_turn, dict) else None
            if turn_id and completed_id and completed_id != turn_id:
                continue
            status = completed_turn.get("status") if isinstance(completed_turn, dict) else None
            if status not in {"completed", "Completed"}:
                raise AppServerError(f"advisor turn ended with status {status}: {completed_turn}")
            return token_usage_delta(self.latest_usage, before)

    def close(self) -> None:
        if self.process is None:
            return
        process = self.process
        self.process = None
        terminate_process_tree(process)


def queue_path(session: Path) -> Path:
    return session / "queue.json"


def worker_path(session: Path) -> Path:
    return session / "worker.json"


def runtime_path(session: Path) -> Path:
    return session / "runtime.json"


def update_worker_state(session: Path, **values: Any) -> None:
    with FileLock(session / "worker-state.lock", timeout=10, stale_after=30):
        state = load_json(worker_path(session), {})
        if not isinstance(state, dict):
            state = {}
        state.update(values)
        state["heartbeat"] = time.time()
        save_json(worker_path(session), state)


def app_server_command(session: Path, cwd: Path, runtime_file: Path, allow_shell: bool) -> list[str]:
    command = ["codex", "app-server", "--stdio"]
    overrides = [
        "notify=[]",
        "features.apps=false",
        "features.goals=false",
        "features.hooks=false",
        "features.multi_agent=false",
        "features.plugins=false",
        "features.remote_plugin=false",
        "features.plugin_sharing=false",
        f"features.shell_tool={'true' if allow_shell else 'false'}",
        "tools.view_image=false",
        'web_search="disabled"',
    ]
    requested = requested_advisor_tools(cwd)
    overrides.extend(mcp_overrides(session, cwd, runtime_file=runtime_file))
    for override in overrides:
        command.extend(["-c", override])
    return command


def app_server_environment(session: Path) -> dict[str, str]:
    environment = os.environ.copy()
    source_home = Path(environment.get("CODEX_HOME") or (Path.home() / ".codex"))
    nested_home = session / "codex-home"
    nested_home.mkdir(parents=True, exist_ok=True)
    source_auth = source_home / "auth.json"
    nested_auth = nested_home / "auth.json"
    if source_auth.is_file():
        if nested_auth.exists() and not os.path.samefile(source_auth, nested_auth):
            nested_auth.unlink()
        if not nested_auth.exists():
            try:
                os.link(source_auth, nested_auth)
            except OSError as error:
                raise AppServerError(
                    "Advisor could not securely share the existing Codex login with its "
                    "isolated app-server. Re-enable Advisor or restart Codex after checking "
                    f"access to {source_auth}."
                ) from error
    environment["CODEX_ADVISOR_NESTED"] = "1"
    environment["CODEX_HOME"] = str(nested_home)
    return environment


class AdvisorWorker:
    def __init__(self, session: Path):
        self.session = session
        self.app: AppServerClient | None = None
        self.app_key: tuple[str, str, tuple[str, ...], str] | None = None
        self.stopping = False

    def close_app(self, retire_devtools: bool = False) -> None:
        if self.app is not None:
            self.app.close()
            self.app = None
        self.app_key = None
        if retire_devtools:
            shutdown_broker(self.session / "devtools-broker.json")

    def ensure_app(self, cwd: Path, config: dict[str, Any], system_prompt: str) -> AppServerClient:
        tools = tuple(sorted(requested_advisor_tools(cwd)))
        key = (str(config["model"]), str(config["reasoning_effort"]), tools, system_prompt)
        if self.app is not None and self.app_key == key:
            return self.app
        self.close_app()
        active_file = self.session / "active-update.json"
        command = app_server_command(self.session, cwd, active_file, "bash" in tools)
        environment = app_server_environment(self.session)
        app = AppServerClient(command, cwd, environment, self.session / "advisor.log")
        app.progress_callback = lambda: update_worker_state(self.session, status="reviewing")
        app.start()
        runtime = load_json(runtime_path(self.session), {})
        stored_thread = runtime.get("thread_id") if isinstance(runtime, dict) else None
        stored_usage = runtime.get("advisor_thread_usage") if isinstance(runtime, dict) else None
        if stored_thread and isinstance(stored_usage, dict):
            app.latest_usage = normalize_token_usage(stored_usage)
        thread_id = app.open_thread(
            stored_thread if isinstance(stored_thread, str) else None,
            str(config["model"]),
            str(config["reasoning_effort"]),
            cwd,
            system_prompt,
        )
        if not isinstance(runtime, dict):
            runtime = {}
        runtime["thread_id"] = thread_id
        runtime["advisor_thread_usage"] = app.latest_usage
        save_json(runtime_path(self.session), runtime)
        self.app = app
        self.app_key = key
        update_worker_state(self.session, app_server_pid=app.process.pid if app.process else None)
        return app

    def process_batch(self, state: dict[str, Any], lane_key: str | None = None) -> None:
        transcript_raw = state.get("transcript")
        cwd_raw = state.get("cwd")
        if not transcript_raw or not cwd_raw:
            return
        transcript = Path(str(transcript_raw))
        transcript_key = lane_key or str(transcript.resolve())
        is_root = bool(state.get("is_root", False))
        cwd = Path(str(cwd_raw)).resolve()
        processed_cursor = int(state.get("processed_cursor", 0))
        delta, new_cursor = read_transcript_delta(transcript, processed_cursor)
        generation = int(state.get("generation", 0))
        in_progress = str(state.get("latest_event")) != "Stop"
        if not delta:
            if not in_progress:
                update_id = uuid.uuid4().hex
                start_update(self.session, update_id)
                flush_deferred(self.session, update_id)
                append_delivery(
                    self.session,
                    notes=read_update_result(self.session, update_id),
                    update_id=update_id,
                    transcript=transcript_key,
                    is_root=is_root,
                )
            self.finish_batch(generation, new_cursor, None, lane_key)
            return

        update_id = uuid.uuid4().hex
        start_update(self.session, update_id)
        save_json(self.session / "active-update.json", {
            "update_id": update_id,
            "in_progress": in_progress,
            "generation": generation,
            "cursor": new_cursor,
            "transcript": transcript_key,
            "is_root": is_root,
        })
        update = "### Session update\n\n" + delta
        if in_progress:
            update += "\n\n---\n\n[in progress — more steps follow]"
        config = load_config()
        system_prompt = build_system_prompt(cwd)
        final_error: Exception | None = None
        succeeded = False
        usage = empty_token_usage()
        attempts_started = 0
        for attempt in range(1, MAX_REVIEW_ATTEMPTS + 1):
            attempts_started = attempt
            update_worker_state(
                self.session,
                status="reviewing",
                review_attempt=attempt,
                review_attempts=MAX_REVIEW_ATTEMPTS,
                review_started_at=time.time(),
                active_generation=generation,
            )
            try:
                app = self.ensure_app(cwd, config, system_prompt)
                app.delivery_transcript = transcript_key
                app.delivery_is_root = is_root
                usage = app.run_turn(
                    update,
                    str(config["reasoning_effort"]),
                )
                runtime = load_json(runtime_path(self.session), {})
                if not isinstance(runtime, dict):
                    runtime = {}
                runtime["thread_id"] = app.thread_id
                runtime["advisor_thread_usage"] = app.latest_usage
                save_json(runtime_path(self.session), runtime)
                succeeded = True
                final_error = None
                break
            except (AppServerError, OSError) as error:
                final_error = error
                log_error(self.session, f"persistent advisor attempt {attempt}/3 failed: {error}")
                self.close_app(retire_devtools=isinstance(error, AppServerUnresponsive))
                runtime = load_json(runtime_path(self.session), {})
                if not isinstance(runtime, dict):
                    runtime = {}
                runtime["thread_id"] = None
                save_json(runtime_path(self.session), runtime)
                if attempt < MAX_REVIEW_ATTEMPTS:
                    time.sleep(attempt)

        record_advisor_invocation(
            self.session,
            usage,
            succeeded,
            update_had_advice(self.session, update_id),
            config,
        )
        if not succeeded:
            append_delivery(
                self.session,
                warning=(
                    f"Advisor unavailable after {attempts_started} persistent-worker "
                    f"transport attempt{'s' if attempts_started != 1 else ''}; "
                    f"this transcript batch was dropped. {final_error}"
                ),
                update_id=update_id,
                transcript=transcript_key,
                is_root=is_root,
            )
        if not in_progress:
            flush_deferred(self.session, update_id)
        notes = read_update_result(self.session, update_id)
        append_delivery(
            self.session,
            notes=notes,
            update_id=update_id,
            transcript=transcript_key,
            is_root=is_root,
        )
        self.finish_batch(generation, new_cursor, str(final_error) if final_error else None, lane_key)

    def finish_batch(
        self,
        generation: int,
        cursor: int,
        error: str | None,
        lane_key: str | None = None,
    ) -> None:
        with FileLock(self.session / "queue.lock", timeout=10, stale_after=30):
            raw_state = load_json(queue_path(self.session), {})
            if lane_key is None and isinstance(raw_state, dict) and not isinstance(raw_state.get("lanes"), dict):
                raw_state["processed_cursor"] = max(int(raw_state.get("processed_cursor", 0)), cursor)
                raw_state["processed_generation"] = max(int(raw_state.get("processed_generation", 0)), generation)
                raw_state["last_error"] = error
                raw_state["last_completed_at"] = time.time()
                save_json(queue_path(self.session), raw_state)
                return
            state = normalized_queue_state(raw_state)
            lane = state["lanes"].get(lane_key)
            if isinstance(lane, dict):
                lane["processed_cursor"] = max(int(lane.get("processed_cursor", 0)), cursor)
                lane["processed_generation"] = max(int(lane.get("processed_generation", 0)), generation)
                lane["last_error"] = error
                lane["last_completed_at"] = time.time()
            save_json(queue_path(self.session), state)

    @staticmethod
    def next_pending_lane(state: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for lane_key, lane in state.get("lanes", {}).items():
            if not isinstance(lane, dict):
                continue
            desired = int(lane.get("desired_cursor", 0) or 0)
            processed = int(lane.get("processed_cursor", 0) or 0)
            generation = int(lane.get("generation", 0) or 0)
            processed_generation = int(lane.get("processed_generation", 0) or 0)
            terminal = str(lane.get("latest_event")) == "Stop" and generation > processed_generation
            if desired > processed or terminal:
                candidates.append((generation, str(lane_key), dict(lane)))
        if not candidates:
            return None
        _, lane_key, lane = min(candidates, key=lambda item: (item[0], item[1]))
        return lane_key, lane

    def run(self) -> int:
        update_worker_state(
            self.session,
            pid=os.getpid(),
            status="running",
            started_at=time.time(),
            app_server_pid=None,
        )
        last_work = time.monotonic()
        try:
            while not self.stopping:
                config = load_config()
                if not config.get("enabled", True):
                    break
                with FileLock(self.session / "queue.lock", timeout=10, stale_after=30):
                    state = normalized_queue_state(load_json(queue_path(self.session), {}))
                if state.get("shutdown"):
                    break
                pending = self.next_pending_lane(state)
                if pending is not None:
                    coalesce = max(0, int(config.get("coalesce_milliseconds", 350))) / 1000.0
                    if coalesce:
                        time.sleep(coalesce)
                    with FileLock(self.session / "queue.lock", timeout=10, stale_after=30):
                        state = normalized_queue_state(load_json(queue_path(self.session), state))
                    pending = self.next_pending_lane(state)
                    if pending is None:
                        continue
                    lane_key, lane = pending
                    update_worker_state(self.session, status="reviewing")
                    try:
                        self.process_batch(lane, lane_key)
                    except Exception as error:
                        log_error(self.session, f"advisor worker batch failed unexpectedly: {error}")
                        self.close_app(retire_devtools=True)
                        append_delivery(
                            self.session,
                            warning=f"Advisor worker recovered from an unexpected batch failure: {error}",
                            transcript=lane_key,
                            is_root=bool(lane.get("is_root", False)),
                        )
                        update_worker_state(
                            self.session,
                            status="failed",
                            last_error=str(error),
                            review_attempt=None,
                            active_generation=None,
                        )
                        raise
                    update_worker_state(
                        self.session,
                        status="running",
                        review_attempt=None,
                        active_generation=None,
                    )
                    last_work = time.monotonic()
                    continue
                if time.monotonic() - last_work >= float(config.get("worker_idle_seconds", 3600)):
                    break
                update_worker_state(self.session, status="idle")
                time.sleep(0.25)
        finally:
            self.close_app(retire_devtools=True)
            update_worker_state(
                self.session,
                status="stopped",
                stopped_at=time.time(),
                app_server_pid=None,
            )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    options = parser.parse_args()
    return AdvisorWorker(Path(options.session_dir).resolve()).run()


if __name__ == "__main__":
    raise SystemExit(main())
