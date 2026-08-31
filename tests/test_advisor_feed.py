from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AdvisorFeedMcpTests(unittest.TestCase):
    def test_feed_server_exposes_read_only_ui_and_lane_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory)
            session_id = "01a-feed-test"
            key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
            session = data_root / "sessions" / key
            session.mkdir(parents=True)
            (session / "usage.json").write_text(json.dumps({
                "main": {"totals": {"total_tokens": 100}},
                "advisor": {
                    "totals": {"total_tokens": 25},
                    "invocations": 2,
                    "successful_reviews": 2,
                    "silent_reviews": 1,
                    "failed_reviews": 0,
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                },
            }), encoding="utf-8")
            (session / "worker.json").write_text(
                json.dumps({"status": "idle"}), encoding="utf-8"
            )
            (session / "feed.json").write_text(json.dumps([{
                "id": "u:note:0",
                "kind": "advice",
                "note": "Subagent-only correction.",
                "severity": "concern",
                "origin": "subagent",
                "transcript": str(session / "child.jsonl"),
                "created_at": 1,
            }]), encoding="utf-8")
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "show_advisor_feed",
                        "arguments": {"session_id": session_id},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "resources/read",
                    "params": {"uri": "ui://advisor/feed-v1.html"},
                },
            ]
            environment = os.environ.copy()
            environment["PLUGIN_DATA"] = str(data_root)
            completed = subprocess.run(
                ["node", str(ROOT / "scripts" / "advisor_feed_mcp.mjs")],
                cwd=ROOT,
                env=environment,
                input="\n".join(json.dumps(request) for request in requests) + "\n",
                text=True,
                encoding="utf-8",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = {item["id"]: item["result"] for item in map(json.loads, completed.stdout.splitlines())}
        show = next(tool for tool in responses[2]["tools"] if tool["name"] == "show_advisor_feed")
        self.assertTrue(show["annotations"]["readOnlyHint"])
        self.assertEqual(show["_meta"]["ui"]["resourceUri"], "ui://advisor/feed-v1.html")
        snapshot = responses[3]["structuredContent"]
        self.assertEqual(snapshot["feed"][0]["origin"], "subagent")
        self.assertEqual(snapshot["usage"]["advisor_main_ratio"], 25)
        resource = responses[4]["contents"][0]
        self.assertEqual(resource["mimeType"], "text/html;profile=mcp-app")
        self.assertIn("read_advisor_feed", resource["text"])


if __name__ == "__main__":
    unittest.main()
