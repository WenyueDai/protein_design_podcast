"""
Tests for src/collectors/rss.py — the 403-on-browser-UA fallback and the
per-source diagnostics used to tell "blocked" apart from "genuinely nothing new".

Run:  python -m unittest discover -s tests -v      (from openclaw-knowledge-radio/)
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.collectors import rss as R  # noqa: E402

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test Feed</title>
<item><title>{title}</title><link>{url}</link><pubDate>{date}</pubDate><description>{summary}</description></item>
</channel></rss>"""


class _Resp:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            err = requests.HTTPError(f"{self.status_code} error")
            err.response = self
            raise err


def _feed_bytes(title="A protein design paper", url="https://example.com/a", hours_ago=1, summary="abstract"):
    date = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return RSS_XML.format(title=title, url=url, date=date, summary=summary).encode("utf-8")


class TestBrowserFallback(unittest.TestCase):
    def test_403_retries_with_browser_ua_and_succeeds(self):
        calls = []

        def fake_get(url, timeout, headers):
            calls.append(headers.get("User-Agent", ""))
            if len(calls) == 1:
                return _Resp(403, b"blocked")
            return _Resp(200, _feed_bytes())

        src = {"name": "Endpoints News", "url": "https://endpts.com/feed/", "bucket": "news"}
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        diagnostics = {}
        with patch.object(R, "_requests") as mock_requests:
            mock_requests.get.side_effect = fake_get
            import requests as real_requests
            mock_requests.RequestException = real_requests.RequestException
            items = R._fetch_source(src, cutoff, now, diagnostics=diagnostics)

        self.assertEqual(len(calls), 2)
        self.assertIn("feedbot", calls[0].lower())
        self.assertNotIn("feedbot", calls[1].lower())
        self.assertEqual(len(items), 1)
        self.assertEqual(diagnostics["Endpoints News"]["http_status"], 200)
        self.assertEqual(diagnostics["Endpoints News"]["kept"], 1)

    def test_non_403_error_is_not_retried_with_browser_ua(self):
        calls = []

        def fake_get(url, timeout, headers):
            calls.append(headers)
            return _Resp(500, b"server error")

        src = {"name": "Some Journal", "url": "https://example.com/rss", "bucket": "journal"}
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        diagnostics = {}
        with patch.object(R, "_requests") as mock_requests:
            mock_requests.get.side_effect = fake_get
            import requests as real_requests
            mock_requests.RequestException = real_requests.RequestException
            items = R._fetch_source(src, cutoff, now, diagnostics=diagnostics)

        self.assertEqual(len(calls), 1)
        self.assertEqual(items, [])
        self.assertEqual(diagnostics["Some Journal"]["http_status"], 500)
        self.assertIsNotNone(diagnostics["Some Journal"]["error"])


class TestDiagnostics(unittest.TestCase):
    def test_records_raw_vs_kept_and_newest_entry_when_all_filtered_out(self):
        """A source whose only entry is outside the lookback window should show up in
        diagnostics as raw_entries=1, kept=0 with the entry's real date — not as a
        silent zero indistinguishable from a fetch failure."""
        old_bytes = _feed_bytes(hours_ago=48)  # older than a 24h window
        src = {"name": "Structure", "url": "https://www.cell.com/structure/inpress.rss", "bucket": "protein"}
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        diagnostics = {}
        with patch.object(R, "_requests") as mock_requests:
            mock_requests.get.return_value = _Resp(200, old_bytes)
            import requests as real_requests
            mock_requests.RequestException = real_requests.RequestException
            items = R._fetch_source(src, cutoff, now, diagnostics=diagnostics)

        self.assertEqual(items, [])
        d = diagnostics["Structure"]
        self.assertEqual(d["raw_entries"], 1)
        self.assertEqual(d["kept"], 0)
        self.assertIsNone(d["error"])
        self.assertIsNotNone(d["newest_entry"])

    def test_collect_rss_items_populates_diagnostics_per_source(self):
        src = {"name": "A Feed", "url": "https://example.com/rss", "bucket": "protein"}
        now = datetime.now(timezone.utc)
        with patch.object(R, "_requests") as mock_requests:
            mock_requests.get.return_value = _Resp(200, _feed_bytes())
            import requests as real_requests
            mock_requests.RequestException = real_requests.RequestException
            diagnostics = {}
            items = R.collect_rss_items(
                [src], tz=timezone.utc, lookback_hours=24, now_ref=now, diagnostics=diagnostics,
            )

        self.assertEqual(len(items), 1)
        self.assertIn("A Feed", diagnostics)
        self.assertEqual(diagnostics["A Feed"]["kept"], 1)


if __name__ == "__main__":
    unittest.main()
