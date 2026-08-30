from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from advisor_common import (  # noqa: E402
    append_delivery,
    drain_deliveries,
    record_advice,
    reset_guard,
    start_update,
)
from advisor_hook import (  # noqa: E402
    claim_delivery_waiter,
    complete_transcript_cursor,
    enqueue_update,
    ensure_worker,
    hook_output,
)
from advisor_worker import AdvisorWorker  # noqa: E402
from advisor_worker import app_server_command  # noqa: E402


class SingletonQueueTests(unittest.TestCase):
    def test_high_water_marks_coalesce_without_reordering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            session.mkdir()
            transcript = root / "rollout.jsonl"
            transcript.write_text('{"first":1}\n', encoding="utf-8")
            first_generation = enqueue_update(session, transcript, root, "PostToolUse")
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write('{"second":2}\n')
            second_generation = enqueue_update(session, transcript, root, "PostToolUse")
            state = json.loads((session / "queue.json").read_text(encoding="utf-8"))
            expected_cursor = transcript.stat().st_size
        self.assertGreater(second_generation, first_generation)
        self.assertEqual(state["desired_cursor"], expected_cursor)
        self.assertEqual(state["latest_event"], "PostToolUse")

    def test_partial_transcript_line_is_not_queued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes(b'{"complete":true}\n{"partial":')
            self.assertEqual(complete_transcript_cursor(path), len(b'{"complete":true}\n'))

    def test_stop_flushes_deferred_advice_without_new_transcript_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            session.mkdir()
            transcript = root / "rollout.jsonl"
            transcript.write_text("", encoding="utf-8")
            reset_guard(session)
            start_update(session, "wip")
            record_advice(session, "wip", "Verify the final cursor.", "concern", True)
            AdvisorWorker(session).process_batch({
                "transcript": str(transcript),
                "cwd": str(root),
                "processed_cursor": 0,
                "generation": 1,
                "latest_event": "Stop",
            })
            notes, warnings = drain_deliveries(session)
        self.assertEqual(notes[0]["note"], "Verify the final cursor.")
        self.assertEqual(warnings, [])

    def test_one_live_worker_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            with (
                patch("advisor_hook.live_worker_pid", return_value=4242),
                patch("advisor_hook.subprocess.Popen") as popen,
            ):
                self.assertEqual(ensure_worker(session), 4242)
            popen.assert_not_called()

    def test_delivery_waiter_is_singleton(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            self.assertTrue(claim_delivery_waiter(session, 60))
            with patch("advisor_hook.os.getpid", return_value=os.getpid() + 1):
                self.assertFalse(claim_delivery_waiter(session, 60))


class DeliveryVisibilityTests(unittest.TestCase):
    def test_completed_advice_is_drained_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            append_delivery(
                session,
                notes=[{"note": "Inspect the cursor.", "severity": "concern"}],
                update_id="u",
            )
            first_notes, first_warnings = drain_deliveries(session)
            second_notes, second_warnings = drain_deliveries(session)
        self.assertEqual(first_notes[0]["note"], "Inspect the cursor.")
        self.assertEqual(first_warnings, [])
        self.assertEqual(second_notes, [])
        self.assertEqual(second_warnings, [])

    def test_stop_always_surfaces_usage_even_when_review_is_silent(self) -> None:
        output = hook_output("Stop", [], {}, "Advisor usage · Main 100 · Advisor 10")
        self.assertEqual(output["systemMessage"], "Advisor usage · Main 100 · Advisor 10")

    def test_runtime_warning_is_visible_but_not_advice_context(self) -> None:
        output = hook_output(
            "PostToolUse",
            [],
            {},
            warnings=["Advisor unavailable after three attempts."],
        )
        self.assertIn("Advisor unavailable", output["systemMessage"])
        self.assertNotIn("hookSpecificOutput", output)

    def test_session_end_hook_is_installed(self) -> None:
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("SessionEnd", hooks["hooks"])
        handler = hooks["hooks"]["SessionEnd"][0]["hooks"][0]
        self.assertEqual(handler["timeout"], 3)

    def test_runtime_contains_no_per_update_codex_exec(self) -> None:
        hook_source = (ROOT / "scripts" / "advisor_hook.py").read_text(encoding="utf-8")
        worker_source = (ROOT / "scripts" / "advisor_worker.py").read_text(encoding="utf-8")
        self.assertNotIn('"codex", "exec"', hook_source)
        self.assertNotIn('"codex", "exec"', worker_source)
        self.assertIn('"codex", "app-server"', worker_source)

    def test_app_server_disables_inherited_mcp_servers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / ".codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                '[mcp_servers.godot]\ncommand = "godot"\n'
                '[mcp_servers.godot-rust-devtools]\ncommand = "node"\n'
                '[mcp_servers."search.example"]\ncommand = "search"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
                command = app_server_command(root / "session", root, root / "active.json", False)
        self.assertIn("mcp_servers.godot.enabled=false", command)
        self.assertIn("mcp_servers.godot-rust-devtools.enabled=false", command)
        self.assertIn('mcp_servers."search.example".enabled=false', command)
        self.assertTrue(any(value.startswith("mcp_servers.advisor.command=") for value in command))


if __name__ == "__main__":
    unittest.main()
