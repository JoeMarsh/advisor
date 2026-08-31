import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginPackageTests(unittest.TestCase):
    def test_manifest_registers_bundled_mcp_servers(self):
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")

    def test_mcp_file_registers_feed_server(self):
        config = json.loads((ROOT / ".mcp.json").read_text())
        self.assertEqual(set(config), {"mcpServers"})
        self.assertEqual(set(config["mcpServers"]), {"advisor-feed"})
        server = config["mcpServers"]["advisor-feed"]
        self.assertEqual(server["command"], "node")
        self.assertEqual(
            server["args"], ["scripts/advisor_feed_mcp.mjs"]
        )
        self.assertEqual(server["default_tools_approval_mode"], "auto")

    def test_manifest_uses_at_most_three_starter_prompts(self):
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        self.assertLessEqual(len(manifest["interface"]["defaultPrompt"]), 3)


if __name__ == "__main__":
    unittest.main()
