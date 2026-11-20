"""Keeps tests/fixtures/dashboard.json (read by the Model.js tests) in step with the real builder."""

from __future__ import annotations

import json
import unittest

from fixtures.make_dashboard_fixture import FIXTURE_PATH, build_fixture, render_fixture


class TestDashboardFixture(unittest.TestCase):
    def test_fixture_matches_builder(self) -> None:
        self.assertTrue(FIXTURE_PATH.exists(), "run: python3 tests/fixtures/make_dashboard_fixture.py")
        self.assertEqual(
            FIXTURE_PATH.read_text(encoding="utf-8"),
            render_fixture(),
            "dashboard.json is stale; regenerate with: python3 tests/fixtures/make_dashboard_fixture.py",
        )

    def test_fixture_covers_every_note_kind_and_sentinel_field(self) -> None:
        dash = build_fixture()["dashboard"]
        prio = dash["priority"]
        rows = prio["pinned"] + prio["runningOut"] + prio["useItOrLoseIt"] + prio["usedUp"]
        self.assertEqual({r["noteKind"] for r in rows}, {"amount", "resetCompact", "reset", "resetRelative", "outIn"})
        burning = next(r for r in rows if r["priorityReason"] == "burningFast")
        self.assertTrue(burning["runoutAt"])
        for r in rows:
            for gone in ("priorityNote", "resetText", "compactResetText", "paceInline", "prioritySection"):
                self.assertNotIn(gone, r)

        cards = {c["kind"]: c for c in dash["allUsage"]}
        claude = cards["claude"]["accounts"][0]
        self.assertTrue(claude["isUnified"])
        self.assertTrue(claude["planMultiple"])
        self.assertEqual(claude["plan"], "")
        self.assertTrue(all(l["isAggregateName"] for l in claude["lines"]))
        codex = cards["codex"]["accounts"][0]
        self.assertEqual(codex["id"], "33333333-3333-4333-8333-333333333333")
        self.assertFalse(codex["planMultiple"])

        usage = next(l for l in cards["cursor"]["accounts"][0]["lines"] if l["key"] == "usage")
        models = usage["breakdown"]["models"]
        self.assertEqual([m["isOther"] for m in models], [False] * 5 + [True])

        link_ids = {l["id"] for l in cards["copilot"]["accounts"][0]["links"]}
        self.assertIn("orgBilling", link_ids)
        self.assertIn("statusPage", {l["id"] for l in cards["claude"]["links"]})
        for item in dash["bar"]["allItems"]:
            self.assertNotIn("resetText", item)
            self.assertIn("resetsAt", item)

        self.assertNotIn("generatedAt", dash)
        json.dumps(dash)  # serializable as NDJSON


if __name__ == "__main__":
    unittest.main()
