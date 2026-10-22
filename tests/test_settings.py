import json
import tempfile
import unittest
from pathlib import Path
from settings import ROOT, load_config


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config.example.json").write_bytes((ROOT / "config.example.json").read_bytes())

    def test_fresh_checkout_uses_defaults(self):
        self.assertEqual(load_config(self.root)["model"], "qwen2.5:7b")

    def test_partial_local_config_preserves_defaults_and_file(self):
        local = self.root / "config.json"
        content = json.dumps({"model": "custom:7b"})
        local.write_text(content, encoding="utf-8-sig")
        config = load_config(self.root)
        self.assertEqual(config["model"], "custom:7b")
        self.assertEqual(config["language"], "pt")
        self.assertEqual(local.read_text(encoding="utf-8-sig"), content)

    def test_invalid_json_has_actionable_error(self):
        (self.root / "config.json").write_text('{bad', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, 'linha 1'):
            load_config(self.root)

    def test_invalid_server_rejected(self):
        (self.root / "config.json").write_text('{"mcp_servers": ["bad"]}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, 'mcp_servers'):
            load_config(self.root)


if __name__ == '__main__':
    unittest.main()
