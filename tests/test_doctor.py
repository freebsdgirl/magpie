from __future__ import annotations

import unittest

from magpie.config import RESOLVER_API_KEY_PLACEHOLDER, Settings
from magpie.doctor import run_doctor
from magpie.providers.fake import FakeFetcher, FakeNewsClient, FakeSearchClient


def _fake_clients() -> tuple:
    return FakeSearchClient(), FakeFetcher(), FakeNewsClient()


class DoctorWarningsTests(unittest.TestCase):
    def test_warns_when_resolver_api_key_is_placeholder(self) -> None:
        settings = Settings(
            resolver_backend="openai_compatible",
            resolver_api_key=RESOLVER_API_KEY_PLACEHOLDER,
        )
        search, fetcher, news = _fake_clients()
        report = run_doctor(settings, search, fetcher, news)
        warnings = report["warnings"]
        self.assertTrue(len(warnings) == 1, f"expected one warning, got {warnings}")
        self.assertIn("resolver_api_key", warnings[0])
        self.assertIn("placeholder", warnings[0])

    def test_no_warning_when_resolver_is_fake(self) -> None:
        settings = Settings(
            resolver_backend="fake",
            resolver_api_key=RESOLVER_API_KEY_PLACEHOLDER,
        )
        search, fetcher, news = _fake_clients()
        report = run_doctor(settings, search, fetcher, news)
        self.assertEqual(report["warnings"], [])

    def test_no_warning_when_api_key_is_set(self) -> None:
        settings = Settings(
            resolver_backend="openai_compatible",
            resolver_api_key="sk-real-key-12345",
        )
        search, fetcher, news = _fake_clients()
        report = run_doctor(settings, search, fetcher, news)
        self.assertEqual(report["warnings"], [])

    def test_warnings_do_not_affect_overall_status(self) -> None:
        settings = Settings(
            resolver_backend="openai_compatible",
            resolver_api_key=RESOLVER_API_KEY_PLACEHOLDER,
        )
        search, fetcher, news = _fake_clients()
        report = run_doctor(settings, search, fetcher, news)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(len(report["warnings"]) >= 1)


if __name__ == "__main__":
    unittest.main()
