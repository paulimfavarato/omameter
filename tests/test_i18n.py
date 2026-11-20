"""Daemon-side i18n: catalog checks, language switching, and the dashboard edge."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from backend import i18n
from backend.daemon import OmameterDaemon
from backend.http_client import ProxyConfig
from backend.models import MetricLine, ResetItem, title_names_window
from backend.pace import QuotaNotificationEvaluator, milestone_title_and_body
from backend.priority import _reset_item_view, format_line_amount
from backend.settings import PriorityLimits
from backend.store import OmameterStore
from backend.vault import CredentialVault
from scripts import check_i18n
from tests.fakes import FakeClock, InlineExecutor

NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


class LanguageTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        i18n.set_language("en")


class CatalogCheckTests(unittest.TestCase):
    def test_catalogs_and_code_agree(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = check_i18n.main()
        self.assertEqual(code, 0, err.getvalue())


class TranslateTests(LanguageTestCase):
    def test_english_by_default_and_placeholders_fill(self) -> None:
        i18n.set_language("en")
        self.assertEqual(i18n.tr("toast.accountAdded", "Added %1.", "Claude"), "Added Claude.")

    def test_portuguese_and_fallback_chain(self) -> None:
        self.assertTrue(i18n.set_language("pt-BR"))
        self.assertEqual(i18n.tr("toast.copied", "Copied to clipboard."), "Copiado para a área de transferência.")
        # A key missing from every catalog falls back to the caller's English.
        self.assertEqual(i18n.tr("no.such.key", "Fallback %1", 7), "Fallback 7")
        # An unknown regional language falls back to English.
        i18n.set_language("pt-PT")
        self.assertEqual(i18n.tr("toast.copied", "x"), "Copied to clipboard.")

    def test_invalid_language_codes_mean_english(self) -> None:
        i18n.set_language("../../etc/passwd")
        self.assertEqual(i18n.language(), "en")

    def test_data_phrases_keep_english_and_translate_otherwise(self) -> None:
        self.assertEqual(i18n.unit_label("credits"), "credits")
        self.assertEqual(i18n.metric_title("Weekly"), "Weekly")
        i18n.set_language("pt-BR")
        self.assertEqual(i18n.metric_title("Weekly"), "Semanal")
        self.assertEqual(i18n.metric_title("Sonnet"), "Sonnet")  # product names pass through
        self.assertEqual(i18n.unit_label("in 30 days"), "em 30 dias")
        for text in (
            "Grok team accounts have no personal quota.",
            "Paused",
            "Not usable right now",
            "Wait until the earlier reset is used.",
        ):
            self.assertNotEqual(i18n.data_text(text), text, text)


class DashboardEdgeTests(LanguageTestCase):
    def test_amounts_resets_and_notifications_follow_language(self) -> None:
        line = MetricLine(key="session", title="Session", line_type="progress", used=25.0, limit=100.0)
        self.assertEqual(format_line_amount(line), "75% left")
        item = ResetItem(id="r1", provider_kind="claude", kind="full", title="Full Reset",
                         usable_now=False, unavailable_reason="Paused")
        i18n.set_language("pt-BR")
        self.assertEqual(format_line_amount(line), "75% restante")
        view = _reset_item_view(item)
        self.assertEqual((view["title"], view["unavailableReason"]), ("Reset Completo", "Pausado"))
        self.assertEqual(item.title, "Full Reset", "the snapshot itself stays English")
        title, _ = milestone_title_and_body("critical", PriorityLimits.from_settings({}))
        self.assertEqual(title, "Crítico")

    def test_title_names_window_uses_the_english_title(self) -> None:
        self.assertTrue(title_names_window("Weekly", "7d"))
        self.assertTrue(title_names_window("Gemini 5h", "5h"))
        self.assertFalse(title_names_window("Gemini", "7d"))


class SetLanguageCommandTests(LanguageTestCase):
    def test_set_language_reemits_state_only_on_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            out = io.StringIO()
            daemon = OmameterDaemon(
                store=OmameterStore(config_dir=tmp / "cfg", cache_dir=tmp / "cache"),
                vault=CredentialVault(fallback_path=tmp / "vault.json", use_secret_tool=False),
                proxy=ProxyConfig(enabled=False, url=""),
                notifier=QuotaNotificationEvaluator(post_fn=lambda *a: True),
                clock=FakeClock(NOW),
                executor=InlineExecutor(),
                out=out,
            )

            def events():
                lines = [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]
                out.truncate(0)
                out.seek(0)
                return lines

            daemon.handle_command({"cmd": "set_language", "language": "pt-BR", "id": "a"})
            first = events()
            self.assertEqual([e["type"] for e in first], ["state", "result"])
            self.assertEqual(i18n.language(), "pt-BR")
            daemon.handle_command({"cmd": "set_language", "language": "pt-BR", "id": "b"})
            self.assertEqual([e["type"] for e in events()], ["result"])
            daemon.handle_command({"cmd": "no_such_command", "id": "c"})
            self.assertEqual(events()[-1]["error"], "Comando desconhecido: no_such_command")


if __name__ == "__main__":
    unittest.main()
