#!/usr/bin/env python3
"""Check the plugin's JSON files and i18n catalogs.

- manifest.json, every i18n catalog and every test fixture parse as JSON;
- every catalog has exactly the keys of i18n/en.json, with the same
  placeholders (%1..%9) and the same plural shape (string vs. CLDR forms);
- every key the code asks for exists, and its English text equals the
  fallback written next to it:
    Python   tr("key", "fallback", ...)                     (backend/, bin/)
    QML/JS   t("key", "fb"), tn("key", n, "fb"),
             tr(strings, "key", "fb"), _trArg(strings, "key", "fb")
- every catalog key is used somewhere (keys built at runtime are covered by
  DYNAMIC_PREFIXES).

Exit code 0 when everything holds, 1 otherwise (problems on stderr).
Run from anywhere: `python3 scripts/check_i18n.py`.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
I18N_DIR = ROOT / "i18n"
REFERENCE = "en"

# Keys built at runtime: "card." + link id (AllUsageTab.qml) and the data
# phrases translated at the dashboard edge (backend/i18n.py).
DYNAMIC_PREFIXES = ("card.", "metric.", "unit.", "data.")
DYNAMIC_CARD_KEYS = {"card.statusPage", "card.usageDashboard", "card.apiKeys", "card.credits", "card.orgBilling"}

PLACEHOLDER_RE = re.compile(r"%[1-9]")
_JS_STR = r'"((?:[^"\\]|\\.)*)"'
JS_CALL_RES = (
    # t("key", "fb") / root.t(...) — key and optional literal fallback
    re.compile(r"(?<![\w$])t\(\s*" + _JS_STR + r"(?:\s*,\s*" + _JS_STR + r")?"),
    # tr(strings, "key", "fb") / _trArg(o.strings, "key", "fb") / _nameList(strings, "key", "fb", i)
    re.compile(r"(?<![\w$])(?:tr|_trArg|_nameList)\(\s*[\w.]+\s*,\s*" + _JS_STR + r"(?:\s*,\s*" + _JS_STR + r")?"),
    # model tables translated later with t(modelData.key, modelData.fb)
    re.compile(r"\bkey:\s*" + _JS_STR + r"\s*,\s*fb:\s*" + _JS_STR),
)
JS_PLURAL_RE = re.compile(r"(?<![\w$])tn\(\s*" + _JS_STR)


def load_json(path: Path, problems: List[str]) -> Optional[object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        problems.append(f"{path.relative_to(ROOT)}: invalid JSON ({err})")
        return None


def placeholders(value: object) -> Tuple[str, ...]:
    texts = list(value.values()) if isinstance(value, dict) else [value]
    found = set()
    for text in texts:
        found.update(PLACEHOLDER_RE.findall(str(text)))
    return tuple(sorted(found))


def english_text(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("other", ""))
    return str(value)


def python_calls(paths: Iterable[Path]) -> List[Tuple[str, str, Optional[str]]]:
    """(location, key, fallback) for every tr("key", "fallback") call."""
    calls = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
            if name != "tr" or not node.args:
                continue
            key_node = node.args[0]
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue
            fallback = None
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                fallback = node.args[1].value
            calls.append((f"{path.relative_to(ROOT)}:{node.lineno}", key_node.value, fallback))
    return calls


def _js_unescape(text: str) -> str:
    return json.loads('"' + text + '"')


def js_calls(paths: Iterable[Path]) -> List[Tuple[str, str, Optional[str]]]:
    calls = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("//"):
                continue
            where = f"{path.relative_to(ROOT)}:{lineno}"
            for pattern in JS_CALL_RES:
                for match in pattern.finditer(line):
                    fallback = match.group(2)
                    calls.append((where, _js_unescape(match.group(1)), None if fallback is None else _js_unescape(fallback)))
            for match in JS_PLURAL_RE.finditer(line):
                calls.append((where, _js_unescape(match.group(1)), None))
    return calls


def main() -> int:
    problems: List[str] = []

    for path in [ROOT / "manifest.json", *sorted((ROOT / "tests" / "fixtures").glob("*.json"))]:
        load_json(path, problems)

    catalogs: Dict[str, Dict[str, object]] = {}
    for path in sorted(I18N_DIR.glob("*.json")):
        data = load_json(path, problems)
        if not isinstance(data, dict):
            problems.append(f"{path.relative_to(ROOT)}: a catalog must be a JSON object")
            continue
        catalogs[path.stem] = data
    reference = catalogs.get(REFERENCE)
    if reference is None:
        problems.append(f"i18n/{REFERENCE}.json is missing")
        return report(problems)

    for code, catalog in catalogs.items():
        if code == REFERENCE:
            continue
        for key in sorted(set(reference) - set(catalog)):
            problems.append(f"i18n/{code}.json: missing key {key}")
        for key in sorted(set(catalog) - set(reference)):
            problems.append(f"i18n/{code}.json: extra key {key}")
        for key in sorted(set(reference) & set(catalog)):
            ref_value, value = reference[key], catalog[key]
            if isinstance(ref_value, dict) != isinstance(value, dict):
                problems.append(f"i18n/{code}.json: {key} must be {'plural forms' if isinstance(ref_value, dict) else 'a string'} like en.json")
            if placeholders(ref_value) != placeholders(value):
                problems.append(f"i18n/{code}.json: {key} placeholders {placeholders(value)} != en {placeholders(ref_value)}")
            if isinstance(value, dict) and "other" not in value:
                problems.append(f"i18n/{code}.json: {key} plural forms need an 'other' form")
            texts = value.values() if isinstance(value, dict) else [value]
            if any(not isinstance(t, str) or not t.strip() for t in texts):
                problems.append(f"i18n/{code}.json: {key} is empty")

    py_files = sorted((ROOT / "backend").rglob("*.py")) + [ROOT / "bin" / "omameter"]
    js_files = sorted(ROOT.glob("*.qml")) + sorted((ROOT / "ui").glob("*.qml")) + sorted(ROOT.glob("*.js"))
    used = set()
    for where, key, fallback in python_calls(py_files) + js_calls(js_files):
        if key.endswith("."):
            continue  # a prefix completed at runtime ("card." + link id)
        used.add(key)
        if key not in reference:
            problems.append(f"{where}: key {key} is not in i18n/{REFERENCE}.json")
            continue
        if fallback is not None and english_text(reference[key]) != fallback:
            problems.append(f"{where}: fallback {fallback!r} differs from en.json {english_text(reference[key])!r} for {key}")

    for key in sorted(set(reference) - used):
        if key in DYNAMIC_CARD_KEYS or (key.startswith(DYNAMIC_PREFIXES) and not key.startswith("card.")):
            continue
        problems.append(f"i18n/{REFERENCE}.json: key {key} is never used")

    return report(problems, len(reference), sorted(catalogs))


def report(problems: List[str], key_count: int = 0, codes: Optional[List[str]] = None) -> int:
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"check_i18n: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(f"check_i18n: OK ({key_count} keys, catalogs: {', '.join(codes or [])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
