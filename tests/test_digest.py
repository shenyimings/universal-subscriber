"""Tests for digest building — LLM calls are mocked."""

from unittest.mock import MagicMock, patch

from subscriber.digest import build_digest, summarize
from subscriber.fetch import Update


def _make_update(source="src", title="Title", kind="article"):
    return Update(source=source, title=title, link="https://x.com", content="body", kind=kind)


LLM_CFG = {"base_url": "https://api.test", "model": "test", "api_key_env": "TEST_KEY"}
PROMPTS = {
    "persona": "test reader",
    "article": "{persona}\n{source}\n{title}\n{link}\n{content}",
    "page": "{persona}\n{title}\n{link}\n{content}",
}


def _mock_openai_response(text):
    choice = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class TestSummarize:
    @patch.dict("os.environ", {"TEST_KEY": "fake"})
    @patch("subscriber.digest.OpenAI")
    def test_returns_summary(self, mock_cls):
        client = mock_cls.return_value
        client.chat.completions.create.return_value = _mock_openai_response("摘要内容")
        result = summarize(_make_update(), LLM_CFG, PROMPTS, 6000)
        assert result == "摘要内容"

    @patch.dict("os.environ", {"TEST_KEY": "fake"})
    @patch("subscriber.digest.OpenAI")
    def test_skip_returns_none(self, mock_cls):
        client = mock_cls.return_value
        client.chat.completions.create.return_value = _mock_openai_response("SKIP\n无关内容")
        result = summarize(_make_update(), LLM_CFG, PROMPTS, 6000)
        assert result is None

    @patch.dict("os.environ", {"TEST_KEY": "fake"})
    @patch("subscriber.digest.OpenAI")
    def test_content_truncated(self, mock_cls):
        client = mock_cls.return_value
        client.chat.completions.create.return_value = _mock_openai_response("ok")
        u = _make_update()
        u.content = "x" * 100
        summarize(u, LLM_CFG, PROMPTS, 10)
        call_args = client.chat.completions.create.call_args
        prompt_text = call_args.kwargs["messages"][0]["content"]
        assert "x" * 10 in prompt_text
        assert "x" * 100 not in prompt_text


class TestBuildDigest:
    @patch("subscriber.digest.summarize")
    def test_all_skipped_returns_none(self, mock_sum):
        mock_sum.return_value = None
        result = build_digest([_make_update()], LLM_CFG, PROMPTS, 6000)
        assert result is None

    @patch("subscriber.digest.summarize")
    def test_digest_contains_summary(self, mock_sum):
        mock_sum.return_value = "摘要内容"
        result = build_digest([_make_update()], LLM_CFG, PROMPTS, 6000)
        assert "摘要内容" in result
        assert "■ src" in result

    @patch("subscriber.digest.summarize")
    def test_multiple_sources_grouped(self, mock_sum):
        mock_sum.return_value = "ok"
        updates = [_make_update(source="A"), _make_update(source="B")]
        result = build_digest(updates, LLM_CFG, PROMPTS, 6000)
        assert "■ A" in result
        assert "■ B" in result
