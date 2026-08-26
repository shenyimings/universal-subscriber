"""Tests for fetch logic — HTTP calls are mocked."""

import json
from unittest.mock import MagicMock, patch

import pytest

from subscriber.fetch import (
    MAX_IMAGES,
    Update,
    _pick_link,
    capture_note,
    extract_images,
    is_capture_url,
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
        mock_extract.assert_called_once_with("https://example.com/a", with_images=True)


class TestFetchPage:
    @patch("subscriber.fetch.fetch_and_extract", return_value="page content v1")
    def test_first_run_baseline(self, mock_extract, state):
        source = {"name": "pg", "type": "page", "url": "https://example.com"}
        updates = fetch_page(source, state)
        assert updates == []
        assert state.get_snapshot("pg") == "page content v1"
        # snapshots are diffed; rotating image URLs would look like a change
        mock_extract.assert_called_once_with("https://example.com")

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


IMG_PAGE = """<html><body>
<header><img src="/static/logo.svg" alt="站标"></header>
<img class="logo__img" src="/img/tob.png" alt="Trail of Bits Logo">
<article>
  <p>正文第一段，足够长以便被抽取器认定为正文，讲的是 harness 的实现方式与细节。</p>
  <figure>
    <img src="/media/fig1.png" alt="架构图">
    <figcaption>图1 架构</figcaption>
  </figure>
  <p>正文第二段，同样足够长，继续讲解解释器的求值流程与工具调用的处理办法。</p>
  <img data-src="https://cdn.example.com/fig2.jpg" src="data:image/gif;base64,R0lGOD" alt="流程">
  <img src="/media/fig1.png" alt="重复的架构图">
  <img src="/i/avatar-2026.png" alt="作者头像">
  <img src="/px.gif" width="1" height="1">
</article></body></html>"""


class TestExtractImages:
    def test_absolutized_against_the_page(self):
        urls = [u for u, _ in extract_images(IMG_PAGE, "https://example.com/post/a")]
        assert "https://example.com/media/fig1.png" in urls

    def test_alt_text_kept(self):
        assert dict(extract_images(IMG_PAGE, "https://example.com/a"))[
            "https://example.com/media/fig1.png"
        ] == "架构图"

    def test_lazy_data_src_wins_over_placeholder(self):
        """知乎/公众号 put a data: placeholder in src and the real image in
        data-src; taking src would archive a 1px gif."""
        urls = [u for u, _ in extract_images(IMG_PAGE, "https://example.com/a")]
        assert "https://cdn.example.com/fig2.jpg" in urls
        assert not any(u.startswith("data:") for u in urls)

    def test_chrome_dropped(self):
        """svg logos, avatars and tracking pixels are never article content."""
        urls = [u for u, _ in extract_images(IMG_PAGE, "https://example.com/a")]
        assert not any("logo" in u or "avatar" in u or "px.gif" in u for u in urls)

    def test_chrome_named_only_in_alt_or_class_dropped(self):
        """Trail of Bits serves its site logo from /img/tob.png — the URL is
        innocent, the alt and the class are not."""
        urls = [u for u, _ in extract_images(IMG_PAGE, "https://example.com/a")]
        assert "https://example.com/img/tob.png" not in urls

    def test_deduped_keeping_first_alt(self):
        pairs = extract_images(IMG_PAGE, "https://example.com/a")
        fig1 = [p for p in pairs if p[0].endswith("fig1.png")]
        assert len(fig1) == 1 and fig1[0][1] == "架构图"

    def test_capped(self):
        many = "<article>" + "".join(
            f'<img src="https://cdn.example.com/{i}.png">' for i in range(MAX_IMAGES + 10)
        ) + "</article>"
        assert len(extract_images(many, "https://example.com/a")) == MAX_IMAGES

    def test_no_images(self):
        assert extract_images("<p>只有文字</p>", "https://example.com/a") == []


class TestFetchAndExtractImages:
    @patch("subscriber.fetch.extract_text", return_value="正文")
    @patch("subscriber.fetch.requests.get")
    def test_image_section_appended(self, mock_get, mock_extract):
        mock_get.return_value = _stream_response("text/html", IMG_PAGE.encode())
        out = fetch_and_extract("https://example.com/a", with_images=True)
        assert out.startswith("正文")
        assert "## 图片" in out
        assert "![架构图](https://example.com/media/fig1.png)" in out

    @patch("subscriber.fetch.extract_text", return_value="正文")
    @patch("subscriber.fetch.requests.get")
    def test_images_off_by_default(self, mock_get, mock_extract):
        """page sources diff their snapshots; rotating image URLs would show
        up as spurious changes every run."""
        mock_get.return_value = _stream_response("text/html", IMG_PAGE.encode())
        assert fetch_and_extract("https://example.com/a") == "正文"

    @patch("subscriber.fetch.extract_text", return_value="正文")
    @patch("subscriber.fetch.requests.get")
    def test_no_section_when_page_has_no_images(self, mock_get, mock_extract):
        mock_get.return_value = _stream_response("text/html", "<p>只有文字</p>".encode())
        assert fetch_and_extract("https://example.com/a", with_images=True) == "正文"


class TestPickLink:
    def test_scheme_relative_normalized(self):
        assert _pick_link("see //example.com/a") == "https://example.com/a"

    def test_trailing_markdown_punctuation_dropped(self):
        assert _pick_link("[x](https://example.com/a.pdf)") == "https://example.com/a.pdf"

    def test_images_skipped(self):
        body = "![](https://pica.zhimg.com/v2-abc_1440w.jpg)\n正文 https://example.com/post"
        assert _pick_link(body) == "https://example.com/post"

    def test_image_declared_in_query_skipped(self):
        body = "https://pbs.twimg.com/media/HN?format=jpg&name=large https://example.com/post"
        assert _pick_link(body) == "https://example.com/post"

    def test_no_link(self):
        assert _pick_link("纯文字，没有链接") == ""


def _make_msg(message_id, subject="Test", body="body text", labels=None):
    """A list-item message. `preview` is deliberately truncated: that is what
    the real API returns there, and reading it instead of the full body is
    what made a pasted article arrive as a one-line teaser."""
    msg = MagicMock()
    msg.message_id = message_id
    msg.subject = subject
    msg.preview = body[:20]
    msg.text = body
    msg.labels = labels or ["received", "unread"]
    return msg


def _wire(client, messages):
    """Point both messages.list and messages.get at the same fake mails."""
    resp = MagicMock()
    resp.messages = messages
    client.inboxes.messages.list.return_value = resp
    by_id = {m.message_id: m for m in messages}
    client.inboxes.messages.get.side_effect = lambda inbox_id, mid: by_id[mid]


class TestFetchInbox:
    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch._agentmail_client")
    def test_new_messages_returned(self, mock_cls, state):
        client = mock_cls.return_value  # _agentmail_client(key) returns this
        _wire(client, [_make_msg("m1", "Hello", body="some content")])
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
        _wire(client, [_make_msg("m1", labels=["sent"])])
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=5)
        assert len(updates) == 0

    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch._agentmail_client")
    def test_already_seen_skipped(self, mock_cls, state):
        state.mark_seen("inbox", "m1")
        client = mock_cls.return_value  # _agentmail_client(key) returns this
        _wire(client, [_make_msg("m1")])
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=5)
        assert len(updates) == 0

    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch.fetch_and_extract", return_value="extracted article")
    @patch("subscriber.fetch._agentmail_client")
    def test_url_only_body_fetched(self, mock_cls, mock_extract, state):
        client = mock_cls.return_value  # _agentmail_client(key) returns this
        _wire(client, [_make_msg("m1", "Link", body="https://example.com/article")])
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=5)
        assert len(updates) == 1
        assert updates[0].content == "extracted article"
        assert updates[0].link == "https://example.com/article"

    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch.fetch_and_extract", return_value="extracted article")
    @patch("subscriber.fetch._agentmail_client")
    def test_protocol_relative_link_followed(self, mock_cls, mock_extract, state):
        client = mock_cls.return_value
        _wire(client, [_make_msg("m1", "Link", body="//example.com/article")])
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=5)
        assert updates[0].link == "https://example.com/article"
        assert updates[0].content == "extracted article"
        mock_extract.assert_called_once_with("https://example.com/article", with_images=True)

    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch.fetch_and_extract", return_value="extracted article")
    @patch("subscriber.fetch._agentmail_client")
    def test_pasted_article_body_kept(self, mock_cls, mock_extract, state):
        """A long body is the article itself — the link in it is metadata,
        not something to go fetch and overwrite the body with."""
        client = mock_cls.return_value
        body = "原文链接 https://example.com/article\n\n" + "正文" * 400
        _wire(client, [_make_msg("m1", "Pasted", body=body)])
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=5)
        assert updates[0].content == body
        assert updates[0].link == "https://example.com/article"
        mock_extract.assert_not_called()

    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch._agentmail_client")
    def test_max_items_limit(self, mock_cls, state):
        client = mock_cls.return_value  # _agentmail_client(key) returns this
        _wire(client, [_make_msg(f"m{i}") for i in range(5)])
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=2)
        assert len(updates) == 2
        # all 5 should be marked seen
        for i in range(5):
            assert state.is_seen("inbox", f"m{i}")


XHS_NOTE = {
    "platform": "xiaohongshu",
    "title": "字节 Agent 面经",
    "text": "一面问了 harness 的实现。",
    "author": "某同学",
    "time": None,
    "tags": ["面经", "agent"],
    "images": ["https://sns-img.xhscdn.com/a.jpg", "https://sns-img.xhscdn.com/b.jpg"],
    "ocr": ["第一张图上的题目", "第二张图上的题目"],
    "url": "https://www.xiaohongshu.com/explore/abc?xsec_token=T",
}


class TestIsCaptureUrl:
    def test_xiaohongshu_hosts(self):
        assert is_capture_url("https://www.xiaohongshu.com/explore/abc?xsec_token=T")
        assert is_capture_url("http://xhslink.com/a/xyz")

    def test_everything_else(self):
        assert not is_capture_url("https://example.com/a")
        assert not is_capture_url("")


class TestCaptureNote:
    @patch("subscriber.fetch.subprocess.run")
    def test_runs_capture_with_ocr_and_json(self, mock_run):
        """小红书 posts keep the substance inside screenshots, so the OCR pass
        is the point — without it the note is a caption and a tag list."""
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(XHS_NOTE))
        capture_note(XHS_NOTE["url"])
        cmd = mock_run.call_args[0][0]
        assert "--ocr" in cmd and "--json" in cmd
        assert XHS_NOTE["url"] in cmd
        assert cmd[0].endswith("capture.py")

    @patch("subscriber.fetch.subprocess.run")
    def test_title_body_and_ocr_rendered(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(XHS_NOTE))
        out = capture_note(XHS_NOTE["url"])
        assert "字节 Agent 面经" in out
        assert "一面问了 harness 的实现。" in out
        assert "第一张图上的题目" in out and "第二张图上的题目" in out

    @patch("subscriber.fetch.subprocess.run")
    def test_images_land_in_the_image_section(self, mock_run):
        """Same `## 图片` block as any other article, so the compile agent has
        one shape to recognise."""
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(XHS_NOTE))
        out = capture_note(XHS_NOTE["url"])
        body, _, imgs = out.partition("## 图片")
        assert "![](https://sns-img.xhscdn.com/a.jpg)" in imgs
        assert "![](https://sns-img.xhscdn.com/b.jpg)" in imgs

    @patch("subscriber.fetch.subprocess.run")
    def test_capture_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="note is empty")
        with pytest.raises(RuntimeError, match="note is empty"):
            capture_note(XHS_NOTE["url"])


class TestInboxCaptureRouting:
    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch.capture_note", return_value="小红书笔记正文")
    @patch("subscriber.fetch.fetch_and_extract", return_value="trafilatura 的结果")
    @patch("subscriber.fetch._agentmail_client")
    def test_xhs_link_goes_through_capture(self, mock_cls, mock_extract, mock_capture, state):
        client = mock_cls.return_value
        _wire(client, [_make_msg("m1", "面经", body=XHS_NOTE["url"])])
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=5)
        assert updates[0].content == "小红书笔记正文"
        mock_capture.assert_called_once_with(XHS_NOTE["url"])
        mock_extract.assert_not_called()

    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch.capture_note", side_effect=RuntimeError("xsec_token 没了"))
    @patch("subscriber.fetch.fetch_and_extract", return_value="trafilatura 的结果")
    @patch("subscriber.fetch._agentmail_client")
    def test_capture_failure_falls_back(self, mock_cls, mock_extract, mock_capture, state):
        """A dead note or a stripped xsec_token must not lose the mail."""
        client = mock_cls.return_value
        _wire(client, [_make_msg("m1", "面经", body=XHS_NOTE["url"])])
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        updates = fetch_inbox(source, state, max_items=5)
        assert updates[0].content == "trafilatura 的结果"

    @patch.dict("os.environ", {"AGENTMAIL_API_KEY": "fake"})
    @patch("subscriber.fetch.capture_note")
    @patch("subscriber.fetch._agentmail_client")
    def test_pasted_body_never_captured(self, mock_cls, mock_capture, state):
        """The link is only worth following when the mail is just the link."""
        client = mock_cls.return_value
        body = XHS_NOTE["url"] + "\n\n" + "正文" * 400
        _wire(client, [_make_msg("m1", "面经", body=body)])
        source = {"name": "inbox", "type": "inbox", "inbox_id": "test@agentmail.to"}
        fetch_inbox(source, state, max_items=5)
        mock_capture.assert_not_called()


def _blank_pdf_bytes() -> bytes:
    """A structurally valid but textless PDF, for extractor plumbing tests."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _stream_response(content_type: str, body: bytes) -> MagicMock:
    resp = MagicMock()
    resp.headers = {"content-type": content_type}
    resp.encoding = "utf-8"
    resp.iter_content.return_value = iter([body])
    return resp


class TestFetchAndExtractPdf:
    @patch("subscriber.fetch.requests.get")
    def test_pdf_response_uses_pdf_extractor(self, mock_get):
        from subscriber.fetch import extract_pdf_text

        pdf_bytes = _blank_pdf_bytes()
        mock_get.return_value = _stream_response("application/pdf", pdf_bytes)
        with patch("subscriber.fetch.extract_pdf_text", return_value="pdf text") as mock_pdf:
            result = fetch_and_extract("https://arxiv.org/pdf/2607.05168")
        mock_pdf.assert_called_once_with(pdf_bytes)
        assert result == "pdf text"

    @patch("subscriber.fetch.requests.get")
    def test_html_response_uses_html_extractor(self, mock_get):
        mock_get.return_value = _stream_response(
            "text/html", b"<html><body>hi</body></html>"
        )
        with patch("subscriber.fetch.extract_text", return_value="hi") as mock_html:
            result = fetch_and_extract("https://example.com/a")
        mock_html.assert_called_once()
        assert result == "hi"

    @patch("subscriber.fetch.requests.get")
    def test_video_response_skipped_without_reading_body(self, mock_get):
        resp = _stream_response("video/mp4", b"\x00" * 1024)
        mock_get.return_value = resp
        assert fetch_and_extract("https://video.twimg.com/x.mp4") is None
        resp.iter_content.assert_not_called()

    @patch("subscriber.fetch.requests.get")
    def test_oversized_response_abandoned(self, mock_get):
        from subscriber.fetch import MAX_DOWNLOAD_BYTES

        chunk = b"x" * (1024 * 1024)
        resp = MagicMock()
        resp.headers = {"content-type": "text/html"}
        resp.encoding = "utf-8"
        resp.iter_content.return_value = iter([chunk] * (MAX_DOWNLOAD_BYTES // len(chunk) + 2))
        mock_get.return_value = resp
        assert fetch_and_extract("https://example.com/huge") is None

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
