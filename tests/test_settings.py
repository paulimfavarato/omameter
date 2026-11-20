"""backend/settings.py: derived from manifest.json, shared edge cases with Settings.js, linked thresholds."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend import settings as S
from backend.models import PriorityLimits
from backend.store import OmameterStore

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
CASES = json.loads((ROOT / "tests" / "fixtures" / "settings_cases.json").read_text(encoding="utf-8"))


class ManifestParityTests(unittest.TestCase):
    def test_defaults_come_from_the_schema(self) -> None:
        schema = MANIFEST["barWidget"]["schema"]
        self.assertEqual(S.DEFAULT_SETTINGS, {f["key"]: f["defaultValue"] for f in schema})
        self.assertEqual(list(S.DEFAULT_SETTINGS), [f["key"] for f in schema])

    def test_manifest_defaults_block_matches_schema(self) -> None:
        self.assertEqual(MANIFEST["barWidget"]["defaults"], S.DEFAULT_SETTINGS)

    def test_priority_limits_bounds_are_the_schema_bounds(self) -> None:
        fields = {f["key"]: f for f in MANIFEST["barWidget"]["schema"]}
        w, c = fields["warningThreshold"], fields["criticalThreshold"]
        self.assertEqual((PriorityLimits.WARNING_MIN, PriorityLimits.WARNING_MAX), (w["min"], w["max"]))
        self.assertEqual((PriorityLimits.CRITICAL_MIN, PriorityLimits.CRITICAL_MAX), (c["min"], c["max"]))
        self.assertEqual(PriorityLimits.STEP, w["step"])
        self.assertEqual(PriorityLimits.STEP, c["step"])
        self.assertEqual((PriorityLimits().warning, PriorityLimits().critical), (w["defaultValue"], c["defaultValue"]))

    def test_unreadable_manifest_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "manifest.json"
            with self.assertRaises(S.SettingsSchemaError):
                S.load_schema(missing)
            missing.write_text("{not json", encoding="utf-8")
            with self.assertRaises(S.SettingsSchemaError):
                S.load_schema(missing)
            missing.write_text(json.dumps({"barWidget": {"schema": []}}), encoding="utf-8")
            with self.assertRaises(S.SettingsSchemaError):
                S.load_schema(missing)
            bad_enum = {"key": "x", "type": "enum", "options": ["a"], "defaultValue": "b"}
            missing.write_text(json.dumps({"barWidget": {"schema": [bad_enum]}}), encoding="utf-8")
            with self.assertRaises(S.SettingsSchemaError):
                S.load_schema(missing)


class SharedCaseTests(unittest.TestCase):
    def check(self, got: dict, case: dict) -> None:
        for key, value in (case.get("expect") or {}).items():
            self.assertEqual(got[key], value, f"{case['name']}: {key}")
            self.assertIs(type(got[key]), type(value), f"{case['name']}: {key} type")
        for key in case.get("absent") or []:
            self.assertNotIn(key, got, case["name"])
        self.assertEqual(set(got), set(S.DEFAULT_SETTINGS), case["name"])

    def test_normalize_cases(self) -> None:
        for case in CASES["normalize"]:
            with self.subTest(case["name"]):
                self.check(S.normalize_settings(case["raw"]), case)

    def test_apply_patch_cases(self) -> None:
        for case in CASES["applyPatch"]:
            with self.subTest(case["name"]):
                self.check(S.apply_patch(case["current"], case["patch"]), case)


class StoreSettingsTests(unittest.TestCase):
    def test_update_settings_keeps_linked_thresholds_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OmameterStore(config_dir=Path(tmp) / "c", cache_dir=Path(tmp) / "k")
            self.assertEqual(store.settings, S.DEFAULT_SETTINGS)
            store.update_settings({"warningThreshold": 20, "criticalThreshold": 15})
            out = store.update_settings({"criticalThreshold": 20})
            self.assertEqual((out["warningThreshold"], out["criticalThreshold"]), (25, 20))
            out = store.update_settings({"warningThreshold": 20})
            self.assertEqual((out["warningThreshold"], out["criticalThreshold"]), (20, 15))
            reloaded = OmameterStore(config_dir=Path(tmp) / "c", cache_dir=Path(tmp) / "k")
            self.assertEqual(reloaded.settings, out)

    def test_load_normalizes_a_hand_edited_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "c"
            cfg.mkdir()
            (cfg / "settings.json").write_text(
                json.dumps({"maxBarItems": 42, "displayMode": "weird", "legacy": True}), encoding="utf-8"
            )
            store = OmameterStore(config_dir=cfg, cache_dir=Path(tmp) / "k")
            self.assertEqual(store.settings["maxBarItems"], 5)
            self.assertEqual(store.settings["displayMode"], "remaining")
            self.assertNotIn("legacy", store.settings)


if __name__ == "__main__":
    unittest.main()
