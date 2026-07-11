"""Tests for the wiki compiler — LLM calls are mocked."""

from unittest.mock import patch

from subscriber.wiki import (
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


def _write_page(wiki_dir, stem, description="desc", body=""):
    d = wiki_dir / "pages"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.md").write_text(f"---\ndescription: {description}\n---\n{body}\n")


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
        assert plan == [{"file": "a.md", "action": "create", "focus": "f"}]

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


class TestCompileSource:
    @patch("subscriber.wiki._chat")
    def test_creates_page_and_marks_compiled(self, mock_chat, tmp_path):
        src = _write_source(tmp_path)
        mock_chat.side_effect = [
            '[{"file": "topic.md", "action": "create", "focus": "要点"}]',
            "---\ndescription: 主题页\n---\n\n知识内容 [[other]]\n",
        ]
        touched = compile_source(src, tmp_path, LLM_CFG, PROMPTS, 6000)
        assert touched == ["topic.md"]
        page_meta, page_body = parse_front((tmp_path / "pages" / "topic.md").read_text())
        assert page_meta["description"] == "主题页"
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
    def test_rebuild_index_lists_pages(self, tmp_path):
        _write_page(tmp_path, "alpha", description="第一页")
        rebuild_index(tmp_path)
        index = (tmp_path / "index.md").read_text()
        assert "[[alpha]] — 第一页" in index

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
