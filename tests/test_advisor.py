from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from advisor_common import (  # noqa: E402
    add_token_usage,
    advisory_xml,
    compact_usage_footer,
    empty_token_usage,
    flush_deferred,
    format_usage_report,
    installed_plugin_data_root,
    load_usage_state,
    normalize_note,
    normalize_token_usage,
    read_update_result,
    record_advice,
    record_advisor_invocation,
    record_visible_advisories,
    reset_guard,
    start_update,
    update_had_advice,
    update_main_usage,
)
from advisor_hook import (  # noqa: E402
    MAX_UPDATE_CHARS,
    build_system_prompt,
    hook_output,
    mcp_overrides,
    read_transcript_delta,
)
from advisor_worker import (  # noqa: E402
    AppServerClient,
    app_server_command,
    app_server_environment,
    app_server_usage,
)
from advisor_prompt_submit import control_arguments, feed_hook_output, parse_advisor_invocation  # noqa: E402


class AdvisorCommonTests(unittest.TestCase):
    def test_cached_plugin_resolves_its_installed_marketplace_data(self) -> None:
        codex_home = Path("C:/Users/example/.codex")
        plugin = codex_home / "plugins" / "cache" / "playground" / "advisor" / "0.1.0"
        self.assertEqual(
            installed_plugin_data_root(plugin, codex_home),
            codex_home / "plugins" / "data" / "advisor-playground",
        )

    def test_source_plugin_has_no_inferred_installed_data_root(self) -> None:
        codex_home = Path("C:/Users/example/.codex")
        self.assertIsNone(installed_plugin_data_root(Path("C:/src/advisor"), codex_home))

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.session = Path(self.temporary.name)
        reset_guard(self.session)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_normalization_matches_upstream_examples(self) -> None:
        self.assertEqual(normalize_note("  *STOP.*  "), "stop")
        self.assertEqual(normalize_note("No issue; continue."), "no issue continue")
        self.assertEqual(normalize_note("Ångström"), "ångström")

    def test_noise_does_not_consume_update_budget(self) -> None:
        start_update(self.session, "u")
        self.assertEqual(record_advice(self.session, "u", "Stop.", "blocker"), "Recorded.")
        self.assertEqual(record_advice(self.session, "u", "The write can lose buffered bytes.", "concern"), "Recorded.")
        self.assertEqual(len(read_update_result(self.session, "u")), 1)

    def test_one_accepted_note_per_update(self) -> None:
        start_update(self.session, "u")
        self.assertEqual(record_advice(self.session, "u", "First concrete risk.", "concern"), "Recorded.")
        self.assertEqual(record_advice(self.session, "u", "Second concrete risk.", "concern"), "Recorded.")
        self.assertEqual(len(read_update_result(self.session, "u")), 1)

    def test_mid_turn_nonblocker_is_immediate(self) -> None:
        start_update(self.session, "wip")
        response = record_advice(self.session, "wip", "Verify the persisted cursor.", "concern", True)
        self.assertEqual(response, "Recorded.")
        self.assertEqual(read_update_result(self.session, "wip")[0]["note"], "Verify the persisted cursor.")

    def test_mid_turn_blocker_is_immediate(self) -> None:
        start_update(self.session, "wip")
        self.assertEqual(record_advice(self.session, "wip", "A destructive command targets the workspace root.", "blocker", True), "Recorded.")
        self.assertEqual(read_update_result(self.session, "wip")[0]["severity"], "blocker")

    def test_xml_matches_upstream_shape_and_escaping(self) -> None:
        self.assertEqual(
            advisory_xml("Use A < B & C", "concern"),
            '<advisory severity="concern" guidance="weigh, don\'t blindly obey">\nUse A &lt; B &amp; C\n</advisory>',
        )
        self.assertEqual(
            advisory_xml("Plain nit"),
            '<advisory guidance="weigh, don\'t blindly obey">\nPlain nit\n</advisory>',
        )


class TranscriptCursorTests(unittest.TestCase):
    def test_cursor_reads_complete_lines_once_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transcript.jsonl"
            rows = [
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Do it"}]}},
                {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "one"}},
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            first, cursor = read_transcript_delta(path, 0)
            self.assertLess(first.index("[user]"), first.index("[tool call: exec]"))
            second, same_cursor = read_transcript_delta(path, cursor)
            self.assertEqual(second, "")
            self.assertEqual(cursor, same_cursor)

    def test_large_backlog_keeps_only_a_bounded_recent_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "rollout.jsonl"
            entries = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": value * 40_000}],
                    },
                }
                for value in ("a", "b", "c")
            ]
            transcript.write_text(
                "".join(json.dumps(entry) + "\n" for entry in entries),
                encoding="utf-8",
            )
            delta, cursor = read_transcript_delta(transcript, 0)
            size = transcript.stat().st_size
        prefix = "[earlier delta truncated]\n"
        self.assertTrue(delta.startswith(prefix))
        self.assertLessEqual(len(delta), MAX_UPDATE_CHARS + len(prefix))
        self.assertIn("c" * 100, delta)
        self.assertEqual(cursor, size)


class PromptSubmitAdapterTests(unittest.TestCase):
    def test_windows_hook_commands_avoid_codex_embedded_quote_bug(self) -> None:
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        commands = [
            hook["commandWindows"]
            for groups in hooks["hooks"].values()
            for group in groups
            for hook in group["hooks"]
            if hook.get("type") == "command"
        ]
        self.assertTrue(commands)
        self.assertTrue((ROOT / "scripts" / "advisor_python.cmd").is_file())
        for command in commands:
            self.assertNotIn('"', command)
            self.assertIn("advisor_python.cmd", command)

    @unittest.skipUnless(os.name == "nt", "Windows hook launcher regression")
    def test_windows_prompt_hook_launcher_executes(self) -> None:
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["commandWindows"]
        with tempfile.TemporaryDirectory() as data_dir:
            environment = os.environ.copy()
            environment["PLUGIN_ROOT"] = str(ROOT)
            environment["PLUGIN_DATA"] = data_dir
            completed = subprocess.run(
                [environment.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command],
                input=json.dumps({"prompt": "$advisor status"}),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=30,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(
            output["hookSpecificOutput"]["hookEventName"],
            "UserPromptSubmit",
        )

    def test_bare_picker_invocation_maps_to_toggle(self) -> None:
        prompt = r"[$advisor:advisor](C:\Users\me\.codex\plugins\cache\playground\advisor\1\skills\advisor\SKILL.md)"
        remainder = parse_advisor_invocation(prompt)
        self.assertEqual(remainder, "")
        self.assertEqual(control_arguments(remainder or ""), (["toggle"], None))

    def test_picker_arguments_are_preserved(self) -> None:
        prompt = r"[$advisor:advisor](C:\cache\skills\advisor\SKILL.md) model gpt-5.6-luna max"
        remainder = parse_advisor_invocation(prompt)
        self.assertEqual(remainder, "model gpt-5.6-luna max")
        self.assertEqual(
            control_arguments(remainder or ""),
            (["configure", "--model", "gpt-5.6-luna", "--reasoning", "max"], None),
        )

    def test_literal_forms_are_supported(self) -> None:
        self.assertEqual(parse_advisor_invocation("/advisor status"), "status")
        self.assertEqual(parse_advisor_invocation("$advisor off"), "off")

    def test_incidental_mentions_are_not_commands(self) -> None:
        self.assertIsNone(parse_advisor_invocation("Please explain /advisor status"))


class UsageTests(unittest.TestCase):
    def test_app_server_usage_is_normalized(self) -> None:
        self.assertEqual(app_server_usage({
            "inputTokens": 100, "cachedInputTokens": 80,
            "outputTokens": 20, "reasoningOutputTokens": 5,
        }), {
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
            "total_tokens": 120,
        })

    def test_main_cumulative_events_are_not_double_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "session"
            session.mkdir()
            transcript = Path(directory) / "rollout.jsonl"

            def token_event(total: int, cached: int, output: int) -> dict[str, object]:
                return {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": total - output,
                                "cached_input_tokens": cached,
                                "output_tokens": output,
                                "reasoning_output_tokens": 2,
                                "total_tokens": total,
                            },
                            "model_context_window": 200000,
                        },
                    },
                }

            rows = [token_event(100, 70, 10), token_event(100, 70, 10), token_event(160, 110, 15)]
            transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            state = update_main_usage(session, transcript)
            self.assertEqual(state["main"]["totals"]["total_tokens"], 160)
            self.assertEqual(state["main"]["requests"], 2)

            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(token_event(160, 110, 15)) + "\n")
                stream.write(json.dumps(token_event(220, 150, 20)) + "\n")
            state = update_main_usage(session, transcript)
            self.assertEqual(state["main"]["totals"]["total_tokens"], 220)
            self.assertEqual(state["main"]["requests"], 3)

    def test_activity_and_reports_include_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            start_update(session, "u")
            record_advice(session, "u", "Inspect the cursor.", "concern")
            self.assertTrue(update_had_advice(session, "u"))
            usage = normalize_token_usage({
                "input_tokens": 80, "cached_input_tokens": 60, "output_tokens": 20,
            })
            state = record_advisor_invocation(
                session, usage, True, True,
                {"model": "gpt-5.6-luna", "reasoning_effort": "max"},
            )
            state = record_visible_advisories(session, [{"note": "Inspect the cursor.", "severity": "concern"}])
            footer = compact_usage_footer(state)
            report = format_usage_report(state, "example")
            self.assertIn("Advisor share", footer)
            self.assertIn("gpt-5.6-luna", report)
            self.assertIn("Visible advisories: 1", report)
            self.assertIn("1 concerns", report)

    def test_main_usage_tracks_multiple_transcripts_without_recounting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "session"
            session.mkdir()

            def write_usage(path: Path, total: int) -> None:
                path.write_text(json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"total_token_usage": {
                        "input_tokens": total - 10,
                        "output_tokens": 10,
                        "total_tokens": total,
                    }}},
                }) + "\n", encoding="utf-8")

            root = Path(directory) / "root.jsonl"
            child = Path(directory) / "child.jsonl"
            write_usage(root, 100)
            write_usage(child, 40)
            update_main_usage(session, root)
            state = update_main_usage(session, child)
            state = update_main_usage(session, root)
            state = update_main_usage(session, child)
            self.assertEqual(state["main"]["totals"]["total_tokens"], 140)
            self.assertEqual(state["main"]["requests"], 2)
            self.assertEqual(len(state["main"]["streams"]), 2)

    def test_silent_review_is_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            state = record_advisor_invocation(
                session, empty_token_usage(), True, False,
                {"model": "gpt-5.6-luna", "reasoning_effort": "max"},
            )
            self.assertEqual(state["advisor"]["silent_reviews"], 1)


class McpBridgeTests(unittest.TestCase):
    def test_persistent_advisor_uses_upstream_prompt_as_base_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = AppServerClient([], Path(directory), {}, Path(directory) / "log")
            captured: dict[str, object] = {}

            def request(method: str, params: dict[str, object], timeout: float) -> dict[str, object]:
                captured.update({"method": method, "params": params, "timeout": timeout})
                return {"result": {"thread": {"id": "advisor-thread"}}}

            client.request = request  # type: ignore[method-assign]
            thread_id = client.open_thread(
                None, "gpt-5.6-luna", "max", Path(directory), "UPSTREAM SYSTEM PROMPT"
            )
        self.assertEqual(thread_id, "advisor-thread")
        self.assertEqual(captured["method"], "thread/start")
        params = captured["params"]
        assert isinstance(params, dict)
        self.assertEqual(params["baseInstructions"], "UPSTREAM SYSTEM PROMPT")
        self.assertEqual(params["approvalsReviewer"], "auto_review")
        self.assertEqual(params["sandbox"], "read-only")

    def test_codex_transport_suffix_requires_advise_tool_delivery(self) -> None:
        upstream = (ROOT / "prompts" / "system.md").read_text(encoding="utf-8").rstrip()
        prompt = build_system_prompt(ROOT)
        self.assertTrue(prompt.startswith(upstream))
        self.assertIn("For every non-silent result, MUST call `advise` exactly once", prompt)
        self.assertIn("Do not emit advice as assistant commentary or final text", prompt)

    def test_worker_uses_app_server_not_per_update_exec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = app_server_command(root / "session", root, root / "active.json", False)
        self.assertEqual(command[:3], ["codex", "app-server", "--stdio"])
        self.assertNotIn("exec", command)
        self.assertIn("features.shell_tool=false", command)
        self.assertIn("notify=[]", command)

    def test_app_server_uses_an_isolated_nested_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_home = root / "source-home"
            source_home.mkdir()
            source_auth = source_home / "auth.json"
            source_auth.write_text('{"test":"credential"}', encoding="utf-8")
            session = root / "session"
            previous_home = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = str(source_home)
            try:
                environment = app_server_environment(session)
            finally:
                if previous_home is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = previous_home
            nested_auth = session / "codex-home" / "auth.json"
            self.assertTrue(os.path.samefile(source_auth, nested_auth))
            self.assertEqual(environment["CODEX_HOME"], str(session / "codex-home"))
            self.assertEqual(environment["CODEX_ADVISOR_NESTED"], "1")

    def test_unpaired_surrogate_is_sanitized_before_advice_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            start_update(session, "u")
            self.assertEqual(
                record_advice(session, "u", f"bad {chr(0xDC9D)} note", "concern"),
                "Recorded.",
            )
            note = read_update_result(session, "u")[0]["note"]
        self.assertEqual(note, "bad \ufffd note")

    def test_nested_advisor_preapproves_only_its_read_only_tools(self) -> None:
        overrides = mcp_overrides(Path("session"), Path("cwd"), "u", False)
        self.assertIn(
            'mcp_servers.advisor.enabled_tools=["advise","read","grep","glob"]',
            overrides,
        )
        self.assertIn(
            'mcp_servers.advisor.default_tools_approval_mode="approve"',
            overrides,
        )

    def test_stdio_mcp_lists_tools_and_records_advice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "session"
            cwd = Path(directory) / "cwd"
            session.mkdir()
            cwd.mkdir()
            (cwd / "unicode.txt").write_text("before → after\n", encoding="utf-8")
            reset_guard(session)
            start_update(session, "u")
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "advise", "arguments": {"note": "Preserve the committed cursor.", "severity": "concern"}},
                },
                {
                    "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "read", "arguments": {"path": "unicode.txt"}},
                },
            ]
            environment = os.environ.copy()
            environment["PLUGIN_ROOT"] = str(ROOT)
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "advisor_mcp.py"),
                 "--session-dir", str(session), "--cwd", str(cwd), "--update-id", "u"],
                input="\n".join(json.dumps(item) for item in requests) + "\n",
                text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment, timeout=10, check=True,
            )
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            tools = next(response["result"]["tools"] for response in responses if response.get("id") == 2)
            self.assertEqual([tool["name"] for tool in tools], ["advise", "read", "grep", "glob"])
            self.assertEqual(next(response for response in responses if response.get("id") == 3)["result"]["content"][0]["text"], "Recorded.")
            self.assertEqual(next(response for response in responses if response.get("id") == 4)["result"]["content"][0]["text"], "1: before → after")
            self.assertEqual(read_update_result(session, "u")[0]["severity"], "concern")

    def test_persistent_mcp_reads_the_active_update_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "session"
            cwd = Path(directory) / "cwd"
            session.mkdir()
            cwd.mkdir()
            active = session / "active-update.json"
            reset_guard(session)
            start_update(session, "active-u")
            active.write_text(json.dumps({
                "update_id": "active-u",
                "in_progress": False,
            }), encoding="utf-8")
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {
                        "name": "advise",
                        "arguments": {"note": "Keep the singleton cursor ordered.", "severity": "concern"},
                    },
                },
            ]
            environment = os.environ.copy()
            environment["PLUGIN_ROOT"] = str(ROOT)
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "advisor_mcp.py"),
                 "--session-dir", str(session), "--cwd", str(cwd),
                 "--runtime-file", str(active)],
                input="\n".join(json.dumps(item) for item in requests) + "\n",
                text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment, timeout=10, check=True,
            )
            self.assertEqual(
                read_update_result(session, "active-u")[0]["note"],
                "Keep the singleton cursor ordered.",
            )

    def test_stop_blocker_continues_once(self) -> None:
        notes = [{"note": "Run the required test.", "severity": "blocker"}]
        first = hook_output("Stop", notes, {})
        self.assertEqual(first["decision"], "block")
        self.assertIn("<advisory", first["systemMessage"])
        second = hook_output("Stop", notes, {"stop_hook_active": True})
        self.assertNotIn("decision", second)
        self.assertIn("systemMessage", second)

    def test_subagent_stop_blocker_continues_only_that_subagent_once(self) -> None:
        notes = [{"note": "Run the subagent's focused check.", "severity": "blocker"}]
        first = hook_output("SubagentStop", notes, {})
        self.assertEqual(first["decision"], "block")
        second = hook_output("SubagentStop", notes, {"stop_hook_active": True})
        self.assertNotIn("decision", second)

    def test_mid_task_advice_is_visible_to_its_originating_agent_without_forced_echo(self) -> None:
        output = hook_output(
            "PostToolUse",
            [{"note": "Inspect the failed assertion.", "severity": "concern"}],
            {},
            "Advisor usage · Main 100 · Advisor 10",
        )
        content = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Advisor usage", output["systemMessage"])
        self.assertNotIn("Advisor usage", content)
        self.assertIn("your own transcript", content)
        self.assertIn("do not copy it into another agent's context", content)
        self.assertIn("Inspect the failed assertion.", content)

    def test_feed_control_targets_current_session(self) -> None:
        output = feed_hook_output("01a-test")
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("show_advisor_feed", context)
        self.assertIn("session_id: 01a-test", context)


if __name__ == "__main__":
    unittest.main()
