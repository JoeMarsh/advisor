from __future__ import annotations

import os
import json
import queue
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import advisor_hook  # noqa: E402
import advisor_worker  # noqa: E402
from advisor_common import drain_deliveries, read_update_result, start_update  # noqa: E402
from advisor_devtools_proxy import ensure_broker, shutdown_broker  # noqa: E402


class AdvisorDevtoolsTests(unittest.TestCase):
    def test_turn_captures_last_nonempty_plain_agent_message(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = advisor_worker.AppServerClient([], root, {}, root / "log")
            client.thread_id = "advisor-thread"
            client.process = MagicMock()
            client.process.poll.return_value = None
            client._reader = MagicMock()
            client._reader.is_alive.return_value = True
            client._send = MagicMock()  # type: ignore[method-assign]
            client.messages.put({"id": 1, "result": {"turn": {"id": "turn-1"}}})
            client.messages.put({
                "method": "item/completed",
                "params": {"item": {
                    "type": "agentMessage",
                    "text": "[blocker] Preserve authoritative grounding.",
                    "phase": "commentary",
                }},
            })
            client.messages.put({
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": "", "phase": "final_answer"}},
            })
            client.messages.put({
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "completed"}},
            })
            client.run_turn("review", "max")
        self.assertEqual(
            client.last_agent_message,
            "[blocker] Preserve authoritative grounding.",
        )

    def test_plain_blocker_falls_back_into_normal_delivery_result(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            session = Path(raw)
            start_update(session, "u")
            recorded = advisor_worker.record_plain_advisory_fallback(
                session,
                "u",
                "[blocker] Authoritative pose does not affect grounding.",
                False,
            )
            notes = read_update_result(session, "u")
        self.assertTrue(recorded)
        self.assertEqual(notes[0]["severity"], "blocker")
        self.assertEqual(notes[0]["note"], "Authoritative pose does not affect grounding.")

    def test_plain_silence_phrase_stays_silent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            session = Path(raw)
            start_update(session, "u")
            recorded = advisor_worker.record_plain_advisory_fallback(
                session, "u", "No concerns", True
            )
            notes = read_update_result(session, "u")
        self.assertFalse(recorded)
        self.assertEqual(notes, [])

    def test_process_batch_delivers_plain_agent_message_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session = root / "session"
            session.mkdir()
            transcript = root / "rollout.jsonl"
            transcript.write_text(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            }) + "\n", encoding="utf-8")
            worker = advisor_worker.AdvisorWorker(session)
            app = MagicMock()
            app.run_turn.return_value = advisor_worker.empty_token_usage()
            app.last_agent_message = "[concern] Verify the authoritative contact geometry."
            app.thread_id = "advisor-thread"
            app.latest_usage = advisor_worker.empty_token_usage()
            worker.ensure_app = MagicMock(return_value=app)  # type: ignore[method-assign]
            state = {
                "transcript": str(transcript),
                "cwd": str(root),
                "processed_cursor": 0,
                "generation": 1,
                "latest_event": "Stop",
                "is_root": True,
            }
            with (
                patch.object(advisor_worker, "load_config", return_value={
                    "enabled": True,
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                }),
                patch.object(advisor_worker, "build_system_prompt", return_value="instructions"),
            ):
                worker.process_batch(state)
            notes, warnings = drain_deliveries(session, str(transcript.resolve()), True)
            usage = json.loads((session / "usage.json").read_text(encoding="utf-8"))
        self.assertEqual(warnings, [])
        self.assertEqual(notes[0]["severity"], "concern")
        self.assertEqual(notes[0]["note"], "Verify the authoritative contact geometry.")
        self.assertEqual(usage["advisor"]["silent_reviews"], 0)

    def test_long_turn_uses_health_probe_without_wall_clock_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = advisor_worker.AppServerClient([], root, {}, root / "log")
            client.thread_id = "advisor-thread"
            client.process = MagicMock()
            client.process.poll.return_value = None
            client._reader = MagicMock()
            client._reader.is_alive.return_value = True
            sent: list[dict[str, object]] = []
            client._send = sent.append  # type: ignore[method-assign]
            client.messages.put({"id": 1, "result": {"turn": {"id": "turn-1"}}})
            client.messages.put({"id": 2, "result": {"thread": {"status": "active"}}})
            client.messages.put({
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-1", "status": "completed"}},
            })
            with patch.object(advisor_worker, "APP_SERVER_HEALTH_PROBE_IDLE_SECONDS", 0):
                client.run_turn("review", "max")
        self.assertTrue(any(message.get("method") == "thread/read" for message in sent))
        self.assertFalse(any(message.get("method") == "turn/interrupt" for message in sent))

    def test_unresponsive_control_plane_fails_after_health_probes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = advisor_worker.AppServerClient([], root, {}, root / "log")
            client.thread_id = "advisor-thread"
            client.process = MagicMock()
            client.process.poll.return_value = None
            client._reader = MagicMock()
            client._reader.is_alive.return_value = True
            sent: list[dict[str, object]] = []
            client._send = sent.append  # type: ignore[method-assign]
            client.messages.get = MagicMock(side_effect=queue.Empty())  # type: ignore[method-assign]
            with (
                patch.object(advisor_worker, "APP_SERVER_HEALTH_PROBE_IDLE_SECONDS", 0),
                patch.object(advisor_worker, "APP_SERVER_HEALTH_PROBE_TIMEOUT_SECONDS", 0),
                patch.object(advisor_worker, "MAX_MISSED_HEALTH_PROBES", 2),
            ):
                with self.assertRaises(advisor_worker.AppServerUnresponsive):
                    client.run_turn("review", "max")
        probes = [message for message in sent if message.get("method") == "thread/read"]
        self.assertEqual(len(probes), 2)
        self.assertFalse(any(message.get("method") == "turn/interrupt" for message in sent))

    def test_parses_only_advisor_tool_lists(self) -> None:
        text = """
other:
  tools:
    - ignored
advisors:
  - name: default
    model: gpt-5.6-luna:max
    tools:
      - read
      - lsp
      - debug # execution is approval-gated
next: value
"""
        self.assertEqual(
            advisor_hook.parse_watchdog_advisor_tools(text),
            {"read", "lsp", "debug"},
        )

    def test_devtools_are_not_added_without_watchdog_request(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with patch.object(advisor_hook, "devtools_mcp_script", return_value=root / "server.mjs"):
                overrides = advisor_hook.mcp_overrides(root / "session", root, "update", False)
        self.assertFalse(any("godot-rust-devtools" in value for value in overrides))

    def test_watchdog_lsp_and_debug_enable_exact_tool_groups(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "WATCHDOG.yml").write_text(
                "advisors:\n  - name: default\n    tools:\n      - lsp\n      - debug\n",
                encoding="utf-8",
            )
            server = root / "mcp-server.mjs"
            server.write_text("", encoding="utf-8")
            state_file = root / "session" / "devtools-broker.json"
            with (
                patch.dict(os.environ, {"ADVISOR_DEVTOOLS_MCP": str(server)}),
                patch.object(advisor_hook, "ensure_broker", return_value=state_file),
            ):
                overrides = advisor_hook.mcp_overrides(root / "session", root, "update", False)
        enabled = next(value for value in overrides if "godot-rust-devtools.enabled_tools=" in value)
        for tool in advisor_hook.DEVTOOLS_TOOL_GROUPS["lsp"] + advisor_hook.DEVTOOLS_TOOL_GROUPS["debug"]:
            self.assertIn(f'"{tool}"', enabled)
        self.assertIn(
            'mcp_servers.godot-rust-devtools.default_tools_approval_mode="writes"',
            overrides,
        )
        self.assertTrue(any("advisor_devtools_proxy.py" in value for value in overrides))
        self.assertFalse(any(value == 'mcp_servers.godot-rust-devtools.command="node"' for value in overrides))

    def test_devtools_broker_is_singleton_and_proxy_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session = root / "session"
            server = root / "server.mjs"
            server.write_text(
                """
import readline from "node:readline";
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", line => {
  const request = JSON.parse(line);
  if (!("id" in request)) return;
  const result = request.method === "tools/list" ? { tools: [] } : {
    protocolVersion: "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "fake", version: "1" }
  };
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: request.id, result }) + "\\n");
});
""".strip(),
                encoding="utf-8",
            )
            state_file = session / "devtools-broker.json"
            try:
                first = ensure_broker(state_file, root, server, idle_timeout=30)
                first_state = json.loads(first.read_text(encoding="utf-8"))
                second = ensure_broker(state_file, root, server, idle_timeout=30)
                second_state = json.loads(second.read_text(encoding="utf-8"))
                self.assertEqual(first_state["pid"], second_state["pid"])
                self.assertEqual(first_state["node_pid"], second_state["node_pid"])

                proxy = SCRIPTS / "advisor_devtools_proxy.py"
                messages = "\n".join([
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                    "",
                ])
                completed = subprocess.run(
                    [sys.executable, str(proxy), "--state-file", str(state_file)],
                    input=messages,
                    text=True,
                    encoding="utf-8",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                responses = [json.loads(line) for line in completed.stdout.splitlines()]
                self.assertEqual([response["id"] for response in responses], [1, 2])
            finally:
                shutdown_broker(state_file)

    def test_persistent_thread_enables_auto_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = advisor_worker.AppServerClient([], root, {}, root / "log")
            captured: dict[str, object] = {}

            def request(method: str, params: dict[str, object], timeout: float) -> dict[str, object]:
                captured.update(params)
                return {"result": {"thread": {"id": "advisor-thread"}}}

            client.request = request  # type: ignore[method-assign]
            client.open_thread(
                None, "gpt-5.6-luna", "max", root, "instructions"
            )
        self.assertEqual(captured["approvalPolicy"], "on-request")
        self.assertEqual(captured["approvalsReviewer"], "auto_review")
        self.assertEqual(captured["sandbox"], "read-only")

    def test_unresponsive_worker_retires_app_server_and_broker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session = root / "session"
            session.mkdir()
            transcript = root / "rollout.jsonl"
            transcript.write_text(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "review"}],
                },
            }) + "\n", encoding="utf-8")
            worker = advisor_worker.AdvisorWorker(session)
            app = MagicMock()
            app.run_turn.side_effect = advisor_worker.AppServerUnresponsive("health probes failed")
            worker.ensure_app = MagicMock(return_value=app)  # type: ignore[method-assign]
            state = {
                "transcript": str(transcript),
                "cwd": str(root),
                "processed_cursor": 0,
                "generation": 1,
                "latest_event": "PostToolUse",
            }
            with (
                patch.object(advisor_worker, "load_config", return_value={
                    "enabled": True,
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                }),
                patch.object(advisor_worker, "build_system_prompt", return_value="instructions"),
                patch.object(advisor_worker, "shutdown_broker", return_value=True) as shutdown,
                patch.object(advisor_worker.time, "sleep", return_value=None),
            ):
                worker.process_batch(state)
            self.assertGreaterEqual(shutdown.call_count, 1)
            shutdown.assert_any_call(session / "devtools-broker.json")
            self.assertEqual(app.run_turn.call_count, 3)
            notes, warnings = drain_deliveries(session)
            self.assertEqual(notes, [])
            self.assertIn("after 3 persistent-worker transport attempts", warnings[0])

    def test_retry_can_recover_after_transport_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session = root / "session"
            session.mkdir()
            transcript = root / "rollout.jsonl"
            transcript.write_text(json.dumps({
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "review"}],
                },
            }) + "\n", encoding="utf-8")
            worker = advisor_worker.AdvisorWorker(session)
            first = MagicMock()
            first.run_turn.side_effect = advisor_worker.AppServerError("disconnect")
            second = MagicMock()
            second.run_turn.return_value = advisor_worker.empty_token_usage()
            second.thread_id = "advisor-thread"
            second.latest_usage = advisor_worker.empty_token_usage()
            worker.ensure_app = MagicMock(side_effect=[first, second])  # type: ignore[method-assign]
            state = {
                "transcript": str(transcript),
                "cwd": str(root),
                "processed_cursor": 0,
                "generation": 1,
                "latest_event": "PostToolUse",
            }
            with (
                patch.object(advisor_worker, "load_config", return_value={
                    "enabled": True,
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                }),
                patch.object(advisor_worker, "build_system_prompt", return_value="instructions"),
                patch.object(advisor_worker.time, "sleep", return_value=None),
            ):
                worker.process_batch(state)
            usage = json.loads((session / "usage.json").read_text(encoding="utf-8"))
        self.assertEqual(first.run_turn.call_count, 1)
        self.assertEqual(second.run_turn.call_count, 1)
        self.assertEqual(usage["advisor"]["successful_reviews"], 1)


if __name__ == "__main__":
    unittest.main()
