"""Unit tests for privacy, integrity, replay, and evidence outcomes."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from lightpanda_evidence import record, replay, _load


class FakeBrowser:
    instances = []
    fail_start = False
    def __init__(self, url):
        if self.fail_start:
            raise OSError("SECRET_NOT_FOR_LOGS")
        self.url = url
        self.query = ""
        self.closed = False
        self.version = {"product": "fake-1"}
        self.binary_sha256 = "0123456789"
        self.instances.append(self)

    def action(self, action):
        typ = action["type"]
        if typ == "fill":
            self.query = action["value"]
            return {"type": typ, "result": {"selector": action["selector"], "value": self.query}}
        if typ == "click":
            return {"type": typ, "result": {"clicked": action["selector"]}}
        if typ == "extract":
            return {"type": typ, "result": [self.query + "-result"]}
        if typ == "evaluate":
            return {"type": typ, "result": action["expression"]}
        if typ == "navigate":
            self.url = action["url"]
            return {"type": typ, "result": {"href": self.url}}
        if typ == "text":
            raise ValueError("SECRET_FROM_BROWSER")
        raise ValueError("Not implemented")

    def close(self):
        self.closed = True


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "records"
        FakeBrowser.instances.clear()
        FakeBrowser.fail_start = False

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_and_replay_pass(self):
        actions = [
            {"type": "fill", "selector": "#q", "value": "{{query}}"},
            {"type": "click", "selector": "#go"},
            {"type": "extract", "selector": "#items li"},
        ]
        original = record("https://example.org/", actions, {"query": "PRIVATE_SEARCH"},
                          root=self.root, factory=FakeBrowser)
        self.assertEqual(original["outcome"], "PASS")
        self.assertEqual(original["required_replay_inputs"], ["query"])
        disk = (self.root / (original["receipt_id"] + ".json")).read_text()
        self.assertNotIn("PRIVATE_SEARCH", disk)
        self.assertNotIn("PRIVATE_SEARCH-result", disk)
        self.assertEqual(oct(self.root.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((self.root / (original["receipt_id"] + ".json")).stat().st_mode & 0o777), "0o600")
        result = replay(original["receipt_id"], {"query": "PRIVATE_SEARCH"},
                        root=self.root, factory=FakeBrowser)
        self.assertEqual(result["outcome"], "PASS")
        self.assertEqual(result["parent_receipt_id"], original["receipt_id"])
        self.assertTrue(all(step["matches_original"] for step in result["steps"]))
        self.assertTrue(all(x.closed for x in FakeBrowser.instances))

    def test_mismatch_and_missing_inputs(self):
        original = record("https://example.org/", [
            {"type": "fill", "selector": "#q", "value": "{{query}}"}
        ], {"query": "first"}, root=self.root, factory=FakeBrowser)
        missing = replay(original["receipt_id"], {}, root=self.root, factory=FakeBrowser)
        self.assertEqual(missing["outcome"], "BLOCKED")
        self.assertEqual(missing["reason"], "missing_replay_inputs")
        mismatch = replay(original["receipt_id"], {"query": "changed"},
                          root=self.root, factory=FakeBrowser)
        self.assertEqual(mismatch["outcome"], "FAIL")
        self.assertEqual(mismatch["reason"], "observation_mismatch")

    def test_raw_values_redacted_and_replay_requires_input(self):
        original = record("https://example.org/", [
            {"type": "fill", "selector": "#q", "value": "SUPER_SECRET_VALUE"},
            {"type": "evaluate", "expression": "'SENSITIVE_EXPRESSION'"},
        ], root=self.root, factory=FakeBrowser)
        self.assertEqual(original["outcome"], "PASS")
        raw = (self.root / (original["receipt_id"] + ".json")).read_text()
        self.assertNotIn("SUPER_SECRET_VALUE", raw)
        self.assertNotIn("SENSITIVE_EXPRESSION", raw)
        self.assertIn("redacted_step_1", raw)
        self.assertIn("redacted_step_2", raw)
        result = replay(original["receipt_id"], root=self.root, factory=FakeBrowser)
        self.assertEqual(result["outcome"], "BLOCKED")

    def test_url_query_not_stored(self):
        original = record("https://example.org/?token=SUPER_SECRET_QUERY", [
            {"type": "click", "selector": "#go"}
        ], root=self.root, factory=FakeBrowser)
        raw = (self.root / (original["receipt_id"] + ".json")).read_text()
        self.assertNotIn("SUPER_SECRET_QUERY", raw)
        self.assertIn("start_url", original["required_replay_inputs"])

    def test_failure_and_unavailable(self):
        r = record("https://example.org/", [{"type": "text", "selector": "#q"}],
                   root=self.root, factory=FakeBrowser)
        self.assertEqual(r["outcome"], "FAIL")
        self.assertNotIn("SECRET_FROM_BROWSER", json.dumps(r))
        FakeBrowser.fail_start = True
        r = record("https://example.org/", [{"type": "click", "selector": "#q"}],
                   root=self.root, factory=FakeBrowser)
        self.assertEqual(r["outcome"], "BLOCKED")
        self.assertNotIn("SECRET_NOT_FOR_LOGS", json.dumps(r))

    def test_integrity_enforced(self):
        original = record("https://example.org/", [{"type": "click", "selector": "#go"}],
                          root=self.root, factory=FakeBrowser)
        path = self.root / (original["receipt_id"] + ".json")
        contents = path.read_text()
        path.write_text(contents.replace("#go", "#unsafe"))
        with self.assertRaisesRegex(ValueError, "integrity"):
            replay(original["receipt_id"], root=self.root, factory=FakeBrowser)
        with self.assertRaises(ValueError):
            _load("../../etc/passwd", root=self.root)


if __name__ == "__main__":
    unittest.main()
