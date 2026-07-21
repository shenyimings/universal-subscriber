"""Tests for fetch logic — HTTP calls are mocked."""

from unittest.mock import MagicMock, patch

import pytest

from subscriber.fetch import (
    Update,
    fetch_and_extract,
    fetch_rss,
    fetch_page,
    fetch_inbox,
    fetch_source,
)


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Test Feed</title>
  <item>
    <title>Post A</title>
    <link>https://example.com/a</link>
    <guid>a</guid>
    <description>summary A</description>
  </item>
  <item>
    <title>Post B</title>
    <link>https://example.com/b</link>
    <guid>b</guid>
    <description>summary B</description>
  </item>
  <item>
    <title>Post C</title>
    <link>https://example.com/c</link>
    <guid>c</guid>
    <description>summary C</description>
  </item>
</channel>
</rss>"""


@pytest.fixture
def state(tmp_path):
    from subscriber.state import State
    return State(tmp_path / "test.db")


class TestFetchRss:
    @patch("subscriber.fetch.fetch_and_extract", side_effect=Exception("no network in tests"))
    @patch("subscriber.fetch.http_get")
    def test_new_items_returned(self, mock_get, mock_extract, state):
        mock_get.side_effect = lambda url: SAMPLE_RSS
        source = {"name": "test", "type": "rss", "url": "https://example.com/feed"}
        updates = fetch_rss(source, state, max_items=5)
        assert len(updates) == 3
        assert all(u.kind == "article" for u in updates)
        assert updates[0].title == "Post A"

    @patch("subscriber.fetch.fetch_and_extract", side_effect=Exception("no network in tests"))
    @patch("subscriber.fetch.http_get")
    def test_max_items_limit(self, mock_get, mock_extract, state):
        mock_get.side_effect = lambda url: SAMPLE_RSS
        source = {"name": "test", "type": "rss", "url": "https://example.com/feed"}
        updates = fetch_rss(source, state, max_items=1)
        assert len(updates) == 1
        # all 3 items should still be marked as seen
        assert state.is_seen("test", "a")
        assert state.is_seen("test", "b")
        assert state.is_seen("test", "c")

    @patch("subscriber.fetch.fetch_and_extract", side_effect=Exception("no network in tests"))
    @patch("subscriber.fetch.http_get")
    def test_already_seen_skipped(self, mock_get, mock_extract, state):
        state.mark_seen("test", "a")
        state.mark_seen("test", "b")
        mock_get.side_effect = lambda url: SAMPLE_RSS
        source = {"name": "test", "type": "rss", "url": "https://example.com/feed"}
        updates = fetch_rss(source, state, max_items=5)
        assert len(updates) == 1
        assert updates[0].title == "Post C"

    @patch("subscriber.fetch.fetch_and_extract", return_value="full article text")
    @patch("subscriber.fetch.http_get")
    def test_entry_link_extracted(self, mock_get, mock_extract, state):
        mock_get.side_effect = lambda url: SAMPLE_RSS
        source = {"name": "test", "type": "rss", "url": "https://example.com/feed"}
        updates = fetch_rss(source, state, max_items=1)
        assert updates[0].content == "full article text"
        mock_extract.assert_called_once_with("https://example.com/a")


class TestFetchPage:
    @patch("subscriber.fetch.fetch_and_extract", return_value="page content v1")
    def test_first_run_baseline(self, mock_extract, state):
        source = {"name": "pg", "type": "page", "url": "https://example.com"}
        updates = fetch_page(source, state)
        assert updates == []
        assert state.get_snapshot("pg") == "page content v1"

    @patch("subscriber.fetch.fetch_and_extract")
    def test_no_change(self, mock_extract, state):
        mock_extract.return_value = "same"
        source = {"name": "pg", "type": "page", "url": "https://example.com"}
        fetch_page(source, state)  # baseline
        updates = fetch_page(source, state)
        assert updates == []

    @patch("subscriber.fetch.fetch_and_extract")
    def test_change_detected(self, mock_extract, state):
        source = {"name": "pg", "type": "page", "url": "https://example.com"}
        mock_extract.return_value = "old content"
        fetch_page(source, state)
        mock_extract.return_value = "new content"
        updates = fetch_page(source, state)
        assert len(updates) == 1
        assert updates[0].kind == "page_change"
        assert "+new content" in updates[0].content

    @patch("subscriber.fetch.fetch_and_extract", return_value=None)
    def test_empty_extract_raises(self, mock_extract, state):
        source = {"name": "pg", "type": "page", "url": "https://example.com"}
        with pytest.raises(RuntimeError, match="正文提取为空"):
            fetch_page(source, state)


def _make_msg(message_id, subject="Test", preview="body text", labels=None):
    msg = MagicMock()
    msg.message_id = message_id
    msg.subject = subject
    msg.preview = preview
    msg.labels = labels or ["received", "unread"]
    return msg


class TestFetchInbox:
    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch._agentmail_client")
    def test_new_messages_returned(self, mock_cls, state):
        client = mock_cls.return_value  # _agentmail_client(key) returns this
        resp = MagicMock()
        resp.messages = [_make_msg("m1", "Hello", preview="some content")]
        client.inboxes.messages.list.return_value = resp
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=5)
        assert len(updates) == 1
        assert updates[0].title == "Hello"
        assert updates[0].content == "some content"
        assert state.is_seen("inbox", "m1")

    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch._agentmail_client")
    def test_sent_messages_skipped(self, mock_cls, state):
        client = mock_cls.return_value  # _agentmail_client(key) returns this
        resp = MagicMock()
        resp.messages = [_make_msg("m1", labels=["sent"])]
        client.inboxes.messages.list.return_value = resp
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=5)
        assert len(updates) == 0

    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch._agentmail_client")
    def test_already_seen_skipped(self, mock_cls, state):
        state.mark_seen("inbox", "m1")
        client = mock_cls.return_value  # _agentmail_client(key) returns this
        resp = MagicMock()
        resp.messages = [_make_msg("m1")]
        client.inboxes.messages.list.return_value = resp
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=5)
        assert len(updates) == 0

    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch.fetch_and_extract", return_value="extracted article")
    @patch("subscriber.fetch._agentmail_client")
    def test_url_only_body_fetched(self, mock_cls, mock_extract, state):
        client = mock_cls.return_value  # _agentmail_client(key) returns this
        resp = MagicMock()
        resp.messages = [_make_msg("m1", "Link", preview="https://example.com/article")]
        client.inboxes.messages.list.return_value = resp
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=5)
        assert len(updates) == 1
        assert updates[0].content == "extracted article"
        assert updates[0].link == "https://example.com/article"

    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch._agentmail_client")
    def test_max_items_limit(self, mock_cls, state):
        client = mock_cls.return_value  # _agentmail_client(key) returns this
        resp = MagicMock()
        resp.messages = [_make_msg(f"m{i}") for i in range(5)]
        client.inboxes.messages.list.return_value = resp
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=2)
        assert len(updates) == 2
        # all 5 should be marked seen
        for i in range(5):
            assert state.is_seen("inbox", f"m{i}")


def _blank_pdf_bytes() -> bytes:
    """A structurally valid but textless PDF, for extractor plumbing tests."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestFetchAndExtractPdf:
    @patch("subscriber.fetch.requests.get")
    def test_pdf_response_uses_pdf_extractor(self, mock_get):
        from subscriber.fetch import extract_pdf_text

        pdf_bytes = _blank_pdf_bytes()
        resp = MagicMock()
        resp.headers = {"content-type": "application/pdf"}
        resp.content = pdf_bytes
        mock_get.return_value = resp
        with patch("subscriber.fetch.extract_pdf_text", return_value="pdf text") as mock_pdf:
            result = fetch_and_extract("https://arxiv.org/pdf/2607.05168")
        mock_pdf.assert_called_once_with(pdf_bytes)
        assert result == "pdf text"

    @patch("subscriber.fetch.requests.get")
    def test_html_response_uses_html_extractor(self, mock_get):
        resp = MagicMock()
        resp.headers = {"content-type": "text/html"}
        resp.content = b"<html></html>"
        resp.text = "<html><body>hi</body></html>"
        mock_get.return_value = resp
        with patch("subscriber.fetch.extract_text", return_value="hi") as mock_html:
            result = fetch_and_extract("https://example.com/a")
        mock_html.assert_called_once()
        assert result == "hi"

    def test_extract_pdf_text_reads_real_pdf(self):
        from subscriber.fetch import extract_pdf_text

        # A writer-only PDF (no text layer) should return None, not raise.
        assert extract_pdf_text(_blank_pdf_bytes()) is None


class TestFetchSource:
    @patch("subscriber.fetch.fetch_rss", return_value=[])
    def test_dispatches_rss(self, mock_rss, state):
        source = {"name": "x", "type": "rss", "url": "u"}
        fetch_source(source, state, 5)
        mock_rss.assert_called_once()

    @patch("subscriber.fetch.fetch_page", return_value=[])
    def test_dispatches_page(self, mock_page, state):
        source = {"name": "x", "type": "page", "url": "u"}
        fetch_source(source, state, 5)
        mock_page.assert_called_once()

    @patch("subscriber.fetch.fetch_page", return_value=[])
    def test_dispatches_browser(self, mock_page, state):
        source = {"name": "x", "type": "browser", "url": "u"}
        fetch_source(source, state, 5)
        mock_page.assert_called_once()

    @patch("subscriber.fetch.fetch_inbox", return_value=[])
    def test_dispatches_inbox(self, mock_inbox, state):
        source = {"name": "x", "type": "inbox", "inbox_id": "t@agentmail.to"}
        fetch_source(source, state, 5)
        mock_inbox.assert_called_once()

    def test_unknown_type_raises(self, state):
        with pytest.raises(ValueError, match="未知 source type"):
            fetch_source({"name": "x", "type": "???", "url": "u"}, state, 5)
