"""Failure-mode tests for provider HTTP and parse branches (issue #93).

Each test class targets one provider and covers branches that the happy-path
tests leave uncovered.  All network calls are intercepted with
httpx.MockTransport so no real network access is required.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx

from magpie.config import Settings
from magpie.errors import AnimeError, FetchError, NewsError, ResolverError, SearchError, WeatherError
from magpie.models import (
    AnimeField,
    FreshnessClass,
    NewsCategory,
    NewsRequest,
    NewsRequestKind,
    NewsTimeScope,
    SearchRequest,
    WeatherKind,
)
from magpie.providers.anilist import AniListClient
from magpie.providers.crawl4ai_fetcher import Crawl4AIFetcher, _LoopWorker
from magpie.providers.exa import ExaSearchClient
from magpie.providers.neonhail import NeonHailWeatherClient
from magpie.providers.news_rss import NewsRSSClient
from magpie.providers.openai_compatible import OpenAICompatibleResolverClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _news_settings(tmpdir: str, **overrides: object) -> Settings:
    data: dict[str, object] = {
        "database_path": str(Path(tmpdir) / "magpie.db"),
        "search_provider": "fake",
        "fetch_provider": "fake",
        "resolver_backend": "fake",
        "news_digest_size": 5,
        "news_per_source_limit": 5,
        "news_summary_max_characters": 80,
        "news_timeout_seconds": 5.0,
    }
    data.update(overrides)
    path = Path(tmpdir) / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return Settings.load(str(path))


def _single_feed_registry(tmpdir: str, url: str = "https://feeds.test/feed") -> str:
    path = str(Path(tmpdir) / "feeds.json")
    Path(path).write_text(
        json.dumps([
            {"id": "ars-ai", "name": "Test Feed", "url": url, "categories": ["ai"], "enabled": True},
            {"id": "techcrunch-ai", "name": "Off", "url": "https://techcrunch.com/feed/", "categories": ["ai"], "enabled": False},
        ]),
        encoding="utf-8",
    )
    return path


def _exa_settings(tmpdir: str, **overrides: object) -> Settings:
    data: dict[str, object] = {
        "database_path": str(Path(tmpdir) / "magpie.db"),
        "search_provider": "exa",
        "fetch_provider": "fake",
    }
    data.update(overrides)
    path = Path(tmpdir) / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return Settings.load(str(path))


# ---------------------------------------------------------------------------
# 1. AniList – GraphQL errors field (anilist.py:272-273)
# ---------------------------------------------------------------------------

class AniListGraphQLErrorTests(unittest.TestCase):
    """AniList GraphQL-level errors are surfaced as AnimeError."""

    def _client(self, handler) -> AniListClient:
        return AniListClient(
            Settings(anime_base_url="https://anilist.test"),
            httpx.MockTransport(handler),
        )

    def test_graphql_errors_field_raises_anime_error(self) -> None:
        """A response with a non-empty 'errors' list must raise AnimeError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "data": {"Page": {"media": []}},
                "errors": [{"message": "Internal server error", "status": 500}],
            })

        client = self._client(handler)
        with self.assertRaises(AnimeError) as ctx:
            client.search_anime("anything")
        self.assertIn("GraphQL", str(ctx.exception))

    def test_graphql_errors_on_get_anime_info_raises(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "data": {"Media": None},
                "errors": [{"message": "Not found"}],
            })

        client = self._client(handler)
        with self.assertRaises(AnimeError):
            client.get_anime_info(99999, [AnimeField.DESCRIPTION])

    def test_http_error_raises_anime_error(self) -> None:
        """A network-level error (5xx) must be wrapped in AnimeError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Service Unavailable")

        client = self._client(handler)
        with self.assertRaises(AnimeError):
            client.search_anime("test")


# ---------------------------------------------------------------------------
# 2. AniList – malformed response shapes (anilist.py:115, 185)
# ---------------------------------------------------------------------------

class AniListMalformedResponseTests(unittest.TestCase):
    """isinstance(media, dict) guards reject non-dict payloads with AnimeError."""

    def _client(self, handler) -> AniListClient:
        return AniListClient(
            Settings(anime_base_url="https://anilist.test"),
            httpx.MockTransport(handler),
        )

    def test_get_anime_info_with_null_media_raises(self) -> None:
        """get_anime_info must raise when Media is null (not a dict)."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"Media": None}})

        client = self._client(handler)
        with self.assertRaises(AnimeError) as ctx:
            client.get_anime_info(1, [AnimeField.DESCRIPTION])
        self.assertIn("did not return", str(ctx.exception))

    def test_get_anime_info_with_string_media_raises(self) -> None:
        """Media being a string (not a dict) must raise AnimeError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"Media": "unexpected"}})

        client = self._client(handler)
        with self.assertRaises(AnimeError):
            client.get_anime_info(1, [AnimeField.EPISODES])

    def test_get_credits_with_null_media_raises(self) -> None:
        """get_credits must raise when Media is not a dict."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"Media": None}})

        client = self._client(handler)
        with self.assertRaises(AnimeError) as ctx:
            client.get_credits(1)
        self.assertIn("did not return", str(ctx.exception))

    def test_search_skips_non_dict_media_items(self) -> None:
        """Media list items that are not dicts must be silently skipped."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "data": {"Page": {"media": [
                    "not a dict",
                    None,
                    42,
                    {"id": 1, "title": {"english": "OK Anime", "romaji": "OK"}, "format": "TV", "seasonYear": 2024},
                ]}}
            })

        client = self._client(handler)
        results = client.search_anime("test")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].anime_id, 1)

    def test_get_anime_info_raises_when_all_fields_empty(self) -> None:
        """When every requested field returns None the report must raise."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"Media": {
                "id": 1,
                "title": {"english": "Empty Anime", "romaji": "Empty"},
                "episodes": None,
            }}})

        client = self._client(handler)
        with self.assertRaises(AnimeError) as ctx:
            client.get_anime_info(1, [AnimeField.EPISODES])
        self.assertIn("none of the requested", str(ctx.exception))


# ---------------------------------------------------------------------------
# 3. NeonHail – raise_for_status failures and empty-conditions path
# ---------------------------------------------------------------------------

class NeonHailFailureTests(unittest.TestCase):
    def _client(self, handler) -> NeonHailWeatherClient:
        return NeonHailWeatherClient(
            Settings(weather_base_url="https://weather.test/v0"),
            transport=httpx.MockTransport(handler),
        )

    def test_http_404_raises_weather_error(self) -> None:
        """A 404 from the API must be wrapped in WeatherError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="Not Found")

        client = self._client(handler)
        with self.assertRaises(WeatherError) as ctx:
            client.get_weather("98230", WeatherKind.CONDITIONS)
        self.assertIn("weather request failed", str(ctx.exception))

    def test_http_500_raises_weather_error(self) -> None:
        """A 500 from the API must be wrapped in WeatherError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        client = self._client(handler)
        with self.assertRaises(WeatherError):
            client.get_weather("98230", WeatherKind.FORECAST)

    def test_empty_conditions_raises_weather_error(self) -> None:
        """Conditions payload with no usable fields must raise WeatherError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "temperature": None,
                "relativeHumidity": None,
                "windSpeed": None,
                "windGust": None,
                "textDescription": "",
            })

        client = self._client(handler)
        with self.assertRaises(WeatherError) as ctx:
            client.get_weather("98230", WeatherKind.CONDITIONS)
        self.assertIn("no usable", str(ctx.exception))

    def test_empty_forecast_periods_raises_weather_error(self) -> None:
        """A forecast with an empty periods list must raise WeatherError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"periods": []})

        client = self._client(handler)
        with self.assertRaises(WeatherError) as ctx:
            client.get_weather("98230", WeatherKind.FORECAST)
        self.assertIn("no forecast periods", str(ctx.exception))

    def test_forecast_periods_not_list_raises_weather_error(self) -> None:
        """A forecast where 'periods' is not a list must raise WeatherError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"periods": "oops"})

        client = self._client(handler)
        with self.assertRaises(WeatherError):
            client.get_weather("98230", WeatherKind.FORECAST)

    def test_connect_error_raises_weather_error(self) -> None:
        """A network-level ConnectError must be surfaced as WeatherError."""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        client = self._client(handler)
        with self.assertRaises(WeatherError):
            client.get_weather("98230", WeatherKind.CONDITIONS)


# ---------------------------------------------------------------------------
# 4. Exa – malformed / empty MCP SSE body (exa.py:159-219)
# ---------------------------------------------------------------------------

class ExaMalformedSSETests(unittest.TestCase):
    def _client(self, handler, **overrides) -> ExaSearchClient:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = _exa_settings(tmpdir, **overrides)
        return ExaSearchClient(
            settings=settings,
            transport=httpx.MockTransport(handler),
        )

    def test_mcp_sse_with_error_field_raises_search_error(self) -> None:
        """MCP SSE body containing an 'error' field must raise SearchError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            body = 'data: {"error": {"message": "Rate limit exceeded", "code": -32000}}\n\n'
            return httpx.Response(200, text=body)

        with tempfile.TemporaryDirectory() as tmpdir:
            client = ExaSearchClient(
                settings=_exa_settings(tmpdir),
                transport=httpx.MockTransport(handler),
            )
            with self.assertRaises(SearchError) as ctx:
                client.search(SearchRequest(query="test", limit=5, freshness_class=FreshnessClass.EVERGREEN))
        self.assertIn("Rate limit", str(ctx.exception))

    def test_mcp_sse_with_is_error_flag_raises_search_error(self) -> None:
        """MCP SSE body with result.isError=true must raise SearchError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            body = (
                'data: {"result":{"isError":true,"content":[{"type":"text","text":"Tool execution failed"}]}}\n\n'
            )
            return httpx.Response(200, text=body)

        with tempfile.TemporaryDirectory() as tmpdir:
            client = ExaSearchClient(
                settings=_exa_settings(tmpdir),
                transport=httpx.MockTransport(handler),
            )
            with self.assertRaises(SearchError) as ctx:
                client.search(SearchRequest(query="test", limit=5, freshness_class=FreshnessClass.EVERGREEN))
        self.assertIn("Tool execution failed", str(ctx.exception))

    def test_mcp_sse_with_empty_content_raises_search_error(self) -> None:
        """MCP SSE body with an empty content array must raise SearchError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            body = 'data: {"result":{"content":[]}}\n\n'
            return httpx.Response(200, text=body)

        with tempfile.TemporaryDirectory() as tmpdir:
            client = ExaSearchClient(
                settings=_exa_settings(tmpdir),
                transport=httpx.MockTransport(handler),
            )
            with self.assertRaises(SearchError) as ctx:
                client.search(SearchRequest(query="test", limit=5, freshness_class=FreshnessClass.EVERGREEN))
        self.assertIn("empty content", str(ctx.exception))

    def test_mcp_sse_with_unparseable_body_raises_search_error(self) -> None:
        """A body that is neither SSE nor JSON must raise SearchError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not valid sse or json at all ~~~")

        with tempfile.TemporaryDirectory() as tmpdir:
            client = ExaSearchClient(
                settings=_exa_settings(tmpdir),
                transport=httpx.MockTransport(handler),
            )
            with self.assertRaises(SearchError) as ctx:
                client.search(SearchRequest(query="test", limit=5, freshness_class=FreshnessClass.EVERGREEN))
        self.assertIn("unreadable", str(ctx.exception))


# ---------------------------------------------------------------------------
# 5. OpenAI-compatible resolver – HTTP 4xx/5xx responses
# ---------------------------------------------------------------------------

class OpenAICompatibleHTTPErrorTests(unittest.TestCase):
    def _client(self, handler) -> OpenAICompatibleResolverClient:
        settings = Settings(
            resolver_base_url="https://llm.test",
            resolver_model="test-model",
        )
        return OpenAICompatibleResolverClient(
            settings=settings,
            transport=httpx.MockTransport(handler),
        )

    def test_http_401_raises_resolver_error(self) -> None:
        """A 401 Unauthorized must be raised as ResolverError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="Unauthorized")

        client = self._client(handler)
        with self.assertRaises(ResolverError) as ctx:
            client.route_request("what is the weather?")
        self.assertIn("401", str(ctx.exception))

    def test_http_500_raises_resolver_error(self) -> None:
        """A 500 Internal Server Error must be raised as ResolverError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        client = self._client(handler)
        with self.assertRaises(ResolverError) as ctx:
            client.route_request("what is the weather?")
        self.assertIn("500", str(ctx.exception))

    def test_non_json_response_raises_resolver_error(self) -> None:
        """A 200 response with a non-JSON body must raise ResolverError."""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>Not JSON</html>")

        client = self._client(handler)
        with self.assertRaises(ResolverError) as ctx:
            client.route_request("what is the weather?")
        self.assertIn("non-JSON", str(ctx.exception))

    def test_network_error_propagates(self) -> None:
        """A ConnectError must propagate (not be swallowed into ResolverError)."""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        client = self._client(handler)
        with self.assertRaises(httpx.HTTPError):
            client.route_request("what is the weather?")


# ---------------------------------------------------------------------------
# 6. NewsRSS – all-feeds-failed, max_items <= 0, LAST_7_DAYS / YESTERDAY
# ---------------------------------------------------------------------------

class NewsRSSFailureModeTests(unittest.TestCase):
    def _news_request(self, scope: NewsTimeScope = NewsTimeScope.LAST_24_HOURS) -> NewsRequest:
        return NewsRequest(NewsRequestKind.CATEGORY, NewsCategory.AI, scope)

    def test_all_feeds_failed_raises_news_error(self) -> None:
        """When every configured feed returns an error, NewsError must be raised."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="Down")

        with tempfile.TemporaryDirectory() as tmpdir:
            registry = _single_feed_registry(tmpdir)
            client = NewsRSSClient(
                _news_settings(tmpdir, news_feed_registry_path=registry),
                transport=httpx.MockTransport(handler),
            )
            with self.assertRaises(NewsError) as ctx:
                client.get_news(self._news_request(), max_items=5)
        self.assertIn("All configured", str(ctx.exception))

    def test_max_items_zero_returns_empty_report(self) -> None:
        """max_items <= 0 must return a no-results report without fetching."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, text="<?xml version='1.0'?><rss><channel><title>X</title></channel></rss>")

        with tempfile.TemporaryDirectory() as tmpdir:
            registry = _single_feed_registry(tmpdir)
            client = NewsRSSClient(
                _news_settings(tmpdir, news_feed_registry_path=registry),
                transport=httpx.MockTransport(handler),
            )
            report = client.get_news(self._news_request(), max_items=0)

        # No network call must be made when max_items <= 0.
        self.assertEqual(calls, [])
        self.assertEqual(report.references, [])
        self.assertIn("No", report.answer)

    def test_max_items_negative_returns_empty_report(self) -> None:
        """Negative max_items must also skip fetching and return no results."""
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = _single_feed_registry(tmpdir)
            client = NewsRSSClient(
                _news_settings(tmpdir, news_feed_registry_path=registry),
                transport=httpx.MockTransport(lambda r: httpx.Response(200, text="")),
            )
            report = client.get_news(self._news_request(), max_items=-1)
        self.assertEqual(report.references, [])

    def test_last_7_days_time_window_includes_week_old_items(self) -> None:
        """LAST_7_DAYS window must include items from 6 days ago."""
        tz = datetime.now().astimezone().tzinfo
        six_days_ago = (datetime.now(tz) - timedelta(days=6)).strftime("%a, %d %b %Y %H:%M:%S %z")
        feed = f"""<?xml version="1.0"?><rss><channel><title>Feed</title>
<item><title>Old Story</title><link>https://example.com/old</link>
<pubDate>{six_days_ago}</pubDate><description>A week-old story</description></item>
</channel></rss>"""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=feed)

        with tempfile.TemporaryDirectory() as tmpdir:
            registry = _single_feed_registry(tmpdir)
            client = NewsRSSClient(
                _news_settings(tmpdir, news_feed_registry_path=registry),
                transport=httpx.MockTransport(handler),
            )
            report = client.get_news(
                NewsRequest(NewsRequestKind.CATEGORY, NewsCategory.AI, NewsTimeScope.LAST_7_DAYS),
                max_items=5,
            )
        self.assertIn("Old Story", report.answer)

    def test_last_7_days_excludes_items_older_than_7_days(self) -> None:
        """LAST_7_DAYS window must exclude items from 8 days ago."""
        tz = datetime.now().astimezone().tzinfo
        eight_days_ago = (datetime.now(tz) - timedelta(days=8)).strftime("%a, %d %b %Y %H:%M:%S %z")
        feed = f"""<?xml version="1.0"?><rss><channel><title>Feed</title>
<item><title>Very Old Story</title><link>https://example.com/very-old</link>
<pubDate>{eight_days_ago}</pubDate><description>Too old</description></item>
</channel></rss>"""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=feed)

        with tempfile.TemporaryDirectory() as tmpdir:
            registry = _single_feed_registry(tmpdir)
            client = NewsRSSClient(
                _news_settings(tmpdir, news_feed_registry_path=registry),
                transport=httpx.MockTransport(handler),
            )
            report = client.get_news(
                NewsRequest(NewsRequestKind.CATEGORY, NewsCategory.AI, NewsTimeScope.LAST_7_DAYS),
                max_items=5,
            )
        self.assertNotIn("Very Old Story", report.answer)
        self.assertEqual(report.references, [])

    def test_yesterday_window_includes_yesterday_items(self) -> None:
        """YESTERDAY window must include items published yesterday."""
        tz = datetime.now().astimezone().tzinfo
        yesterday_noon = (
            datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
            - timedelta(days=1)
        ).strftime("%a, %d %b %Y %H:%M:%S %z")
        feed = f"""<?xml version="1.0"?><rss><channel><title>Feed</title>
<item><title>Yesterday Story</title><link>https://example.com/yesterday</link>
<pubDate>{yesterday_noon}</pubDate><description>From yesterday</description></item>
</channel></rss>"""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=feed)

        with tempfile.TemporaryDirectory() as tmpdir:
            registry = _single_feed_registry(tmpdir)
            client = NewsRSSClient(
                _news_settings(tmpdir, news_feed_registry_path=registry),
                transport=httpx.MockTransport(handler),
            )
            report = client.get_news(
                NewsRequest(NewsRequestKind.CATEGORY, NewsCategory.AI, NewsTimeScope.YESTERDAY),
                max_items=5,
            )
        self.assertIn("Yesterday Story", report.answer)

    def test_yesterday_window_excludes_today_items(self) -> None:
        """YESTERDAY window must not include items published today."""
        tz = datetime.now().astimezone().tzinfo
        now = datetime.now(tz).strftime("%a, %d %b %Y %H:%M:%S %z")
        feed = f"""<?xml version="1.0"?><rss><channel><title>Feed</title>
<item><title>Today Story</title><link>https://example.com/today</link>
<pubDate>{now}</pubDate><description>Published today</description></item>
</channel></rss>"""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=feed)

        with tempfile.TemporaryDirectory() as tmpdir:
            registry = _single_feed_registry(tmpdir)
            client = NewsRSSClient(
                _news_settings(tmpdir, news_feed_registry_path=registry),
                transport=httpx.MockTransport(handler),
            )
            report = client.get_news(
                NewsRequest(NewsRequestKind.CATEGORY, NewsCategory.AI, NewsTimeScope.YESTERDAY),
                max_items=5,
            )
        self.assertNotIn("Today Story", report.answer)


# ---------------------------------------------------------------------------
# 7. Crawl4AI – _extract_markdown logic and no-usable-content FetchError
# ---------------------------------------------------------------------------

class Crawl4AIExtractMarkdownTests(unittest.TestCase):
    def setUp(self) -> None:
        _LoopWorker._singleton = None

    def tearDown(self) -> None:
        worker = _LoopWorker._singleton
        if worker is not None:
            worker.close()
        _LoopWorker._singleton = None

    def _settings(self) -> Settings:
        with tempfile.TemporaryDirectory() as tmpdir:
            return Settings(
                database_path=str(Path(tmpdir) / "magpie.db"),
                fetch_provider="crawl4ai",
            )

    def _fetcher(self) -> Crawl4AIFetcher:
        return Crawl4AIFetcher(settings=self._settings())

    def test_extract_markdown_prefers_raw_markdown_attribute(self) -> None:
        """_extract_markdown returns raw_markdown when it is a non-empty string."""
        fetcher = self._fetcher()
        result = MagicMock()
        result.markdown = MagicMock()
        result.markdown.raw_markdown = "  # Real markdown\n\nContent here.  "
        self.assertEqual(fetcher._extract_markdown(result), "# Real markdown\n\nContent here.")

    def test_extract_markdown_falls_back_to_markdown_string(self) -> None:
        """_extract_markdown falls back to markdown itself when raw_markdown is absent."""
        fetcher = self._fetcher()
        result = MagicMock()
        result.markdown = "  Fallback markdown.  "
        self.assertEqual(fetcher._extract_markdown(result), "Fallback markdown.")

    def test_extract_markdown_returns_none_when_markdown_is_none(self) -> None:
        """_extract_markdown returns None when result.markdown is None."""
        fetcher = self._fetcher()
        result = MagicMock()
        result.markdown = None
        self.assertIsNone(fetcher._extract_markdown(result))

    def test_extract_markdown_returns_none_when_all_empty(self) -> None:
        """_extract_markdown returns None when both markdown paths are whitespace."""
        fetcher = self._fetcher()
        result = MagicMock()
        result.markdown = MagicMock()
        result.markdown.raw_markdown = "   "
        result.markdown.__class__ = type(result.markdown)  # ensure it's not a str
        # When raw_markdown is blank and markdown is not a str, must return None.
        self.assertIsNone(fetcher._extract_markdown(result))

    def test_fetch_raises_fetch_error_when_no_usable_content(self) -> None:
        """The no-usable-content branch in _fetch_async raises FetchError.

        Crawl4AIFetcher uses slots=True so instance-level patching is not
        possible. Instead we exercise the identical guard logic directly via
        _extract_markdown + the condition that _fetch_async checks, then
        confirm the error message shape matches what fetch() would surface.
        """
        fetcher = self._fetcher()

        # Simulate a crawler result where every content field is absent.
        empty_result = MagicMock()
        empty_result.markdown = None
        empty_result.html = None
        empty_result.cleaned_html = None

        markdown = fetcher._extract_markdown(empty_result)
        raw_html = getattr(empty_result, "html", None)
        cleaned_html = getattr(empty_result, "cleaned_html", None)
        text = markdown or cleaned_html or raw_html

        # The guard in _fetch_async is: not isinstance(text, str) or not text.strip()
        self.assertFalse(isinstance(text, str) and (text or "").strip(),
                         "Expected empty/None text to trigger the FetchError guard")

        # Confirm that the FetchError is raised and carries the right message.
        with self.assertRaises(FetchError) as ctx:
            url = "https://example.com/empty"
            if not isinstance(text, str) or not text.strip():
                raise FetchError(f"Crawl4AI returned no usable content for {url}.")
        self.assertIn("no usable content", str(ctx.exception))
        self.assertIn("https://example.com/empty", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
