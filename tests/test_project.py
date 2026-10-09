import json
import unittest
from pathlib import Path

import main


class ProjectSmokeTests(unittest.TestCase):
    def test_config_is_valid_and_plan_counts_match(self):
        cfg = main.load_config()
        self.assertGreater(cfg["schedule"]["posts_per_day"], 0)
        self.assertGreaterEqual(len(cfg["schedule"]["times"]), cfg["schedule"]["posts_per_day"])
        self.assertEqual(sum(cfg["daily_plan"].values()), cfg["schedule"]["posts_per_day"])

    def test_extract_json_handles_fenced_or_surrounded_text(self):
        parsed = main._extract_json('Result:\n{"text":"مرحبا", "caption":"#خير"}\nDone')
        self.assertEqual(parsed["text"], "مرحبا")

    def test_graph_error_parser_is_informative(self):
        class FakeResponse:
            ok = False
            def json(self):
                return {"error": {"message": "Invalid token", "code": 190}}
        with self.assertRaisesRegex(RuntimeError, "Invalid token"):
            main._fail(FakeResponse())


if __name__ == "__main__":
    unittest.main()
