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
    complete_transcript_cursor,
    enqueue_update,
    ensure_worker,
    hook_output,
    live_worker_pid,
    wait_for_generation,
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
        lane = state["lanes"][str(transcript.resolve())]
        self.assertEqual(lane["desired_cursor"], expected_cursor)
        self.assertEqual(lane["latest_event"], "PostToolUse")

    def test_root_and_subagent_transcripts_keep_independent_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            session.mkdir()
            primary = root / "primary.jsonl"
            child = root / "child.jsonl"
            primary.write_text('{"primary":1}\n', encoding="utf-8")
            child.write_text('{"child":1}\n', encoding="utf-8")
            enqueue_update(session, primary, root, "PostToolUse")
            enqueue_update(session, child, root, "PostToolUse")
            state = json.loads((session / "queue.json").read_text(encoding="utf-8"))
        self.assertEqual(set(state["lanes"]), {str(primary.resolve()), str(child.resolve())})
        self.assertEqual(state["lanes"][str(primary.resolve())]["processed_cursor"], 0)
        self.assertEqual(state["lanes"][str(child.resolve())]["processed_cursor"], 0)

    def test_wait_targets_its_own_transcript_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            primary = str((session / "primary.jsonl").resolve())
            child = str((session / "child.jsonl").resolve())
            (session / "queue.json").write_text(json.dumps({
                "generation": 8,
                "shutdown": False,
                "lanes": {
                    primary: {"processed_generation": 3},
                    child: {"processed_generation": 8},
                },
            }), encoding="utf-8")
            with (
                patch("advisor_hook.live_worker_pid", return_value=1),
                patch("advisor_hook.time.monotonic", side_effect=[0.0, 0.0, 1.0]),
                patch("advisor_hook.time.sleep") as sleep,
            ):
                wait_for_generation(session, 5, 0.5, primary)
            sleep.assert_called_once_with(0.1)
            self.assertEqual(
                json.loads((session / "queue.json").read_text(encoding="utf-8"))["lanes"][primary]["processed_generation"],
                3,
            )

    def test_partial_transcript_line_is_not_queued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes(b'{"complete":true}\n{"partial":')
            self.assertEqual(complete_transcript_cursor(path), len(b'{"complete":true}\n'))

    def test_stop_surfaces_legacy_deferred_advice_without_new_transcript_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            session.mkdir()
            transcript = root / "rollout.jsonl"
            transcript.write_text("", encoding="utf-8")
            reset_guard(session)
            start_update(session, "wip")
            (session / "guard.json").write_text(json.dumps({
                "seen": [], "delivered": {}, "updates": {},
                "deferred": [{
                    "note": "Verify the final cursor.",
                    "normalized": "verify the final cursor",
                    "key": "Verify the final cursor.",
                    "severity": "concern",
                }],
            }), encoding="utf-8")
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

    def test_unexpected_batch_failure_preserves_unreviewed_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "session"
            session.mkdir()
            transcript = root / "rollout.jsonl"
            transcript.write_text("pending\n", encoding="utf-8")
            (session / "queue.json").write_text(json.dumps({
                "transcript": str(transcript),
                "cwd": str(root),
                "desired_cursor": transcript.stat().st_size,
                "processed_cursor": 0,
                "generation": 1,
                "processed_generation": 0,
                "latest_event": "PostToolUse",
                "shutdown": False,
            }), encoding="utf-8")
            worker = AdvisorWorker(session)
            with (
                patch("advisor_worker.load_config", return_value={
                    "enabled": True,
                    "coalesce_milliseconds": 0,
                    "worker_idle_seconds": 3600,
                }),
                patch.object(worker, "process_batch", side_effect=RuntimeError("boom")),
                self.assertRaises(RuntimeError),
            ):
                worker.run()
            state = json.loads((session / "queue.json").read_text(encoding="utf-8"))
            notes, warnings = drain_deliveries(session)
        self.assertEqual(state["processed_cursor"], 0)
        self.assertEqual(state["processed_generation"], 0)
        self.assertEqual(notes, [])
        self.assertIn("unexpected batch failure", warnings[0])

    def test_one_live_worker_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            with (
                patch("advisor_hook.live_worker_pid", return_value=4242),
                patch("advisor_hook.subprocess.Popen") as popen,
            ):
                self.assertEqual(ensure_worker(session), 4242)
            popen.assert_not_called()

    def test_stale_worker_is_terminated_and_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            (session / "worker.json").write_text(json.dumps({
                "pid": 4242,
                "status": "reviewing",
                "heartbeat": 1,
                "app_server_pid": 4343,
            }), encoding="utf-8")
            with (
                patch("advisor_hook.process_is_running", return_value=True),
                patch("advisor_hook.terminate_pid_tree") as terminate,
                patch("advisor_hook.time.time", return_value=100),
            ):
                self.assertIsNone(live_worker_pid(session))
            notes, warnings = drain_deliveries(session)
            state = json.loads((session / "worker.json").read_text(encoding="utf-8"))
        terminate.assert_called_once_with(4242)
        self.assertEqual(notes, [])
        self.assertIn("heartbeat expired", warnings[0])
        self.assertEqual(state["status"], "stale")
        self.assertIsNone(state["app_server_pid"])

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

    def test_post_tool_use_is_a_fast_synchronous_delivery_checkpoint(self) -> None:
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        handler = hooks["hooks"]["PostToolUse"][0]["hooks"][0]
        self.assertNotIn("async", handler)
        self.assertEqual(handler["timeout"], 30)

    def test_runtime_contains_no_per_update_codex_exec(self) -> None:
        hook_source = (ROOT / "scripts" / "advisor_hook.py").read_text(encoding="utf-8")
        worker_source = (ROOT / "scripts" / "advisor_worker.py").read_text(encoding="utf-8")
        self.assertNotIn('"codex", "exec"', hook_source)
        self.assertNotIn('"codex", "exec"', worker_source)
        self.assertIn('"codex", "app-server"', worker_source)

    def test_app_server_only_configures_explicit_advisor_servers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = app_server_command(root / "session", root, root / "active.json", False)
        self.assertFalse(any(value.startswith("mcp_servers.godot.enabled=") for value in command))
        self.assertFalse(any(value.startswith("mcp_servers.linear.enabled=") for value in command))
        self.assertTrue(any(value.startswith("mcp_servers.advisor.command=") for value in command))


if __name__ == "__main__":
    unittest.main()
