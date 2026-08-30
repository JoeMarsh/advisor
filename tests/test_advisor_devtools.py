from __future__ import annotations

import os
import json
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
from advisor_devtools_proxy import ensure_broker, shutdown_broker  # noqa: E402


class AdvisorDevtoolsTests(unittest.TestCase):
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

    def test_worker_timeout_retires_app_server_and_broker(self) -> None:
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
            app.run_turn.side_effect = advisor_worker.AppServerTurnTimeout("expired")
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
                    "timeout_seconds": 0.01,
                }),
                patch.object(advisor_worker, "build_system_prompt", return_value="instructions"),
                patch.object(advisor_worker, "shutdown_broker", return_value=True) as shutdown,
                patch.object(advisor_worker.time, "sleep", return_value=None),
            ):
                worker.process_batch(state)
            self.assertGreaterEqual(shutdown.call_count, 1)
            shutdown.assert_any_call(session / "devtools-broker.json")


if __name__ == "__main__":
    unittest.main()
