"""Tests for the wiki compiler — LLM calls are mocked."""

from unittest.mock import MagicMock, patch

from subscriber.wiki import (
    _chat,
    _parse_plan,
    compile_source,
    dump_front,
    lint_wiki,
    parse_front,
    pending_sources,
    rebuild_index,
)

LLM_CFG = {"base_url": "https://api.test", "model": "test", "api_key_env": "TEST_KEY"}
PROMPTS = {
    "persona": "test reader",
    "wiki_plan": "{persona}|{index}|{title}|{source}|{url}|{summary}|{content}",
    "wiki_page": "{persona}|{file}|{focus}|{existing}|{title}|{url}|{date}|{summary}|{content}",
}


def _write_source(wiki_dir, name="a.md", compiled=False, title="T", day="2026-07-01"):
    d = wiki_dir / "sources" / "2026" / "07"
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(
        f"---\ntitle: {title}\nsource: Src\nurl: https://x.com/a\n"
        f"date: '{day}'\ncompiled: {str(compiled).lower()}\n---\n\n"
        "## 摘要\n\n摘要文本\n\n## 原文\n\n正文文本\n"
    )
    return path


def _write_page(wiki_dir, stem, description="desc", body="", category=None, tags=None):
    d = wiki_dir / "pages"
    d.mkdir(parents=True, exist_ok=True)
    front = f"description: {description}\n"
    if category:
        front += f"category: {category}\n"
    if tags:
        front += "tags:\n" + "".join(f"- {t}\n" for t in tags)
    (d / f"{stem}.md").write_text(f"---\n{front}---\n{body}\n")


class TestChatModelSelection:
    @patch("subscriber.wiki._client")
    def test_uses_wiki_model_when_set(self, mock_client_fn):
        client = mock_client_fn.return_value
        client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="ok"))]
        )
        cfg = {"model": "flash", "wiki_model": "pro"}
        _chat(cfg, "prompt")
        assert client.chat.completions.create.call_args.kwargs["model"] == "pro"

    @patch("subscriber.wiki._client")
    def test_falls_back_to_model_when_wiki_model_unset(self, mock_client_fn):
        client = mock_client_fn.return_value
        client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="ok"))]
        )
        cfg = {"model": "flash"}
        _chat(cfg, "prompt")
        assert client.chat.completions.create.call_args.kwargs["model"] == "flash"


class TestFrontmatter:
    def test_roundtrip(self):
        meta, body = parse_front(dump_front({"a": 1, "中": "文"}, "\nbody\n"))
        assert meta == {"a": 1, "中": "文"}
        assert body == "\nbody\n"

    def test_no_frontmatter(self):
        meta, body = parse_front("plain text")
        assert meta == {} and body == "plain text"


class TestPendingSources:
    def test_filters_and_orders_by_date(self, tmp_path):
        _write_source(tmp_path, "new.md", day="2026-07-02")
        _write_source(tmp_path, "old.md", day="2026-06-01")
        _write_source(tmp_path, "done.md", compiled=True)
        names = [p.name for p in pending_sources(tmp_path)]
        assert names == ["old.md", "new.md"]


class TestParsePlan:
    def test_fenced_json(self):
        plan = _parse_plan('```json\n[{"file": "a.md", "action": "create", "focus": "f"}]\n```')
        assert plan == [
            {"file": "a.md", "action": "create", "focus": "f", "category": "", "tags": []}
        ]

    def test_rejects_bad_files_and_caps_at_three(self):
        items = [{"file": f"p{i}.md", "action": "update", "focus": ""} for i in range(5)]
        items.insert(0, {"file": "../evil.md", "action": "update", "focus": ""})
        items.insert(0, {"file": "no-extension", "action": "update", "focus": ""})
        import json
        plan = _parse_plan(json.dumps(items))
        assert [p["file"] for p in plan] == ["p0.md", "p1.md", "p2.md"]

    def test_garbage_returns_empty(self):
        assert _parse_plan("sorry, no idea") == []
        assert _parse_plan("[not json]") == []

    def test_category_and_tags_validated(self):
        plan = _parse_plan(
            '[{"file": "a.md", "action": "create", "focus": "f",'
            ' "category": "ai-security", "tags": ["Fuzzing", " llm-agent "]}]'
        )
        assert plan[0]["category"] == "ai-security"
        assert plan[0]["tags"] == ["fuzzing", "llm-agent"]

    def test_unknown_category_and_bad_tags_dropped(self):
        plan = _parse_plan(
            '[{"file": "a.md", "action": "create", "focus": "f",'
            ' "category": "made-up", "tags": "not-a-list"}]'
        )
        assert plan[0]["category"] == ""
        assert plan[0]["tags"] == []


class TestCompileSource:
    @patch("subscriber.wiki._chat")
    def test_creates_page_and_marks_compiled(self, mock_chat, tmp_path):
        src = _write_source(tmp_path)
        mock_chat.side_effect = [
            '[{"file": "topic.md", "action": "create", "focus": "要点",'
            ' "category": "ai-security", "tags": ["fuzzing"]}]',
            "---\ndescription: 主题页\n---\n\n知识内容 [[other]]\n",
        ]
        touched = compile_source(src, tmp_path, LLM_CFG, PROMPTS, 6000)
        assert touched == ["topic.md"]
        page_meta, page_body = parse_front((tmp_path / "pages" / "topic.md").read_text())
        assert page_meta["description"] == "主题页"
        assert page_meta["category"] == "ai-security"
        assert page_meta["tags"] == ["fuzzing"]
        assert "updated" in page_meta
        assert "知识内容" in page_body
        src_meta, _ = parse_front(src.read_text())
        assert src_meta["compiled"] is True
        assert "ingest | T" in (tmp_path / "log.md").read_text()

    @patch("subscriber.wiki._chat")
    def test_empty_plan_still_marks_compiled(self, mock_chat, tmp_path):
        src = _write_source(tmp_path)
        mock_chat.return_value = "[]"
        assert compile_source(src, tmp_path, LLM_CFG, PROMPTS, 6000) == []
        src_meta, _ = parse_front(src.read_text())
        assert src_meta["compiled"] is True

    @patch("subscriber.wiki._chat")
    def test_missing_description_backfilled_from_focus(self, mock_chat, tmp_path):
        src = _write_source(tmp_path)
        mock_chat.side_effect = [
            '[{"file": "t.md", "action": "create", "focus": "焦点"}]',
            "无 frontmatter 的正文",
        ]
        compile_source(src, tmp_path, LLM_CFG, PROMPTS, 6000)
        meta, _ = parse_front((tmp_path / "pages" / "t.md").read_text())
        assert meta["description"] == "焦点"


class TestIndexAndLint:
    def test_rebuild_index_groups_by_category_with_tags(self, tmp_path):
        _write_page(tmp_path, "alpha", description="第一页",
                    category="ai-security", tags=["fuzzing", "llm-agent"])
        _write_page(tmp_path, "beta", description="第二页")
        rebuild_index(tmp_path)
        index = (tmp_path / "index.md").read_text()
        assert "## ai-security" in index
        assert "- [[alpha]] `fuzzing` `llm-agent` — 第一页" in index
        assert "## uncategorized" in index
        assert "- [[beta]] — 第二页" in index
        assert index.index("## ai-security") < index.index("## uncategorized")

    def test_category_index_filters_by_category(self, tmp_path):
        from subscriber.wiki import category_index
        _write_page(tmp_path, "alpha", description="第一页", category="ai-security")
        _write_page(tmp_path, "beta", description="第二页", category="llm-systems")
        listing = category_index(tmp_path, "ai-security")
        assert "[[alpha]]" in listing and "[[beta]]" not in listing
        assert category_index(tmp_path, "program-analysis") == "(该分类暂无页面)"

    def test_lint_reports_broken_link_orphan_and_backlog(self, tmp_path):
        _write_page(tmp_path, "a", body="link to [[missing]]")
        _write_page(tmp_path, "b", body="no links here")
        _write_source(tmp_path)
        issues = "\n".join(lint_wiki(tmp_path))
        assert "[[missing]]" in issues
        assert "孤儿页" in issues and "pages/b.md" in issues
        assert "待编译" in issues

    def test_lint_clean_wiki(self, tmp_path):
        _write_page(tmp_path, "a", body="see [[b]]")
        _write_page(tmp_path, "b", body="see [[a]]")
        assert lint_wiki(tmp_path) == []
