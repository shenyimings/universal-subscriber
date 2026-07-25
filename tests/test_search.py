"""Tests for the qmd-backed layered wiki search — the qmd CLI is mocked."""

import json
from unittest.mock import patch

import pytest

from subscriber.search import (
    PAGES_COLLECTION,
    SOURCES_COLLECTION,
    _rel,
    format_results,
    search_wiki,
    setup_collections,
)


def _write_page(wiki_dir, stem, description="简介", category="ai-security"):
    pages = wiki_dir / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / f"{stem}.md").write_text(
        f"---\ndescription: {description}\ncategory: {category}\ntags:\n- fuzzing\n---\n正文\n"
    )


def _write_source(wiki_dir, rel, compiled=True, pages=("alpha.md",), title="源标题"):
    path = wiki_dir / "sources" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    front = [
        "---",
        f"title: {title}",
        "url: https://example.com/a",
        "date: '2026-07-01'",
        f"compiled: {'true' if compiled else 'false'}",
    ]
    if compiled and pages:
        front.append("pages:")
        front += [f"- {p}" for p in pages]
    front.append("---")
    path.write_text("\n".join(front) + "\n\n## 原文\n正文\n")


def _hit(collection, rel, score, snippet="片段"):
    return {"file": f"qmd://{collection}/{rel}", "score": score, "snippet": snippet}


def _fake_qmd(pages_hits=(), sources_hits=()):
    """Stub for search._run: dispatch on the -c collection in the argv."""
    calls = []

    def run(args):
        calls.append(args)
        collection = args[args.index("-c") + 1] if "-c" in args else ""
        if collection == PAGES_COLLECTION:
            return json.dumps(list(pages_hits))
        if collection == SOURCES_COLLECTION:
            return json.dumps(list(sources_hits))
        return ""

    run.calls = calls
    return run


class TestLayering:
    def test_strong_page_hit_does_not_touch_sources(self, tmp_path):
        _write_page(tmp_path, "alpha", description="第一页")
        fake = _fake_qmd(pages_hits=[_hit(PAGES_COLLECTION, "alpha.md", 0.9)])
        with patch("subscriber.search._run", fake):
            result = search_wiki(tmp_path, "关键词")

        assert result["deep"] is False
        assert result["sources"] == []
        assert result["pages"][0]["page"] == "alpha.md"
        assert result["pages"][0]["category"] == "ai-security"
        assert result["pages"][0]["description"] == "第一页"
        assert all(SOURCES_COLLECTION not in args for args in fake.calls)

    def test_weak_page_hit_escalates_to_sources(self, tmp_path):
        _write_page(tmp_path, "alpha")
        _write_source(tmp_path, "2026/07/a.md", pages=["beta.md"])
        fake = _fake_qmd(
            pages_hits=[_hit(PAGES_COLLECTION, "alpha.md", 0.2)],
            sources_hits=[_hit(SOURCES_COLLECTION, "2026/07/a.md", 0.8)],
        )
        with patch("subscriber.search._run", fake):
            result = search_wiki(tmp_path, "关键词")

        assert result["deep"] is True
        src = result["sources"][0]
        assert src["source"] == "2026/07/a.md"
        assert src["compiled"] is True
        assert src["title"] == "源标题"
        # 回溯：该源沉淀进的页面没被 pages 层捞到，值得提示
        assert src["pages"] == ["beta.md"]

    def test_no_page_hit_escalates(self, tmp_path):
        _write_source(tmp_path, "2026/07/a.md", compiled=False, pages=())
        fake = _fake_qmd(sources_hits=[_hit(SOURCES_COLLECTION, "2026/07/a.md", 0.7)])
        with patch("subscriber.search._run", fake):
            result = search_wiki(tmp_path, "关键词")

        assert result["pages"] == []
        assert result["deep"] is True
        assert result["sources"][0]["compiled"] is False
        assert result["sources"][0]["pages"] == []

    def test_deep_forces_escalation_despite_strong_hit(self, tmp_path):
        _write_page(tmp_path, "alpha")
        _write_source(tmp_path, "2026/07/a.md")
        fake = _fake_qmd(
            pages_hits=[_hit(PAGES_COLLECTION, "alpha.md", 0.95)],
            sources_hits=[_hit(SOURCES_COLLECTION, "2026/07/a.md", 0.4)],
        )
        with patch("subscriber.search._run", fake):
            result = search_wiki(tmp_path, "关键词", deep=True)

        assert result["deep"] is True
        # 该源沉淀进 alpha.md，而 alpha.md 已在 pages 层命中，不重复提示
        assert result["sources"][0]["pages"] == []

    def test_mode_selects_qmd_subcommand(self, tmp_path):
        fake = _fake_qmd()
        with patch("subscriber.search._run", fake):
            search_wiki(tmp_path, "关键词", mode="semantic", limit=3)
        assert fake.calls[0][:2] == ["vsearch", "关键词"]
        assert fake.calls[0][fake.calls[0].index("-n") + 1] == "3"

        fake = _fake_qmd()
        with patch("subscriber.search._run", fake):
            search_wiki(tmp_path, "关键词", mode="hybrid")
        assert fake.calls[0][0] == "query"

    def test_unknown_mode_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            search_wiki(tmp_path, "关键词", mode="magic")

    def test_missing_page_file_does_not_break(self, tmp_path):
        """qmd 索引可能比磁盘旧；命中已删除的页面时不应炸。"""
        fake = _fake_qmd(pages_hits=[_hit(PAGES_COLLECTION, "gone.md", 0.9)])
        with patch("subscriber.search._run", fake):
            result = search_wiki(tmp_path, "关键词")
        assert result["pages"][0] == {
            "page": "gone.md", "score": 0.9, "category": "", "description": "",
            "snippet": "片段",
        }

    def test_malformed_json_yields_no_hits(self, tmp_path):
        with patch("subscriber.search._run", lambda args: "not json"):
            result = search_wiki(tmp_path, "关键词")
        assert result["pages"] == [] and result["sources"] == []


class TestHelpers:
    def test_rel_strips_collection_prefix(self):
        assert _rel(f"qmd://{PAGES_COLLECTION}/a.md", PAGES_COLLECTION) == "a.md"
        assert _rel("a.md", PAGES_COLLECTION) == "a.md"

    def test_setup_registers_missing_collections_only(self, tmp_path):
        calls = []

        def run(args):
            calls.append(args)
            return f"{PAGES_COLLECTION} (qmd://{PAGES_COLLECTION}/)" if args[0] == "collection" and args[1] == "list" else ""

        with patch("subscriber.search._run", run):
            setup_collections(tmp_path)

        added = [a for a in calls if a[:2] == ["collection", "add"]]
        assert len(added) == 1
        assert added[0][-1] == SOURCES_COLLECTION
        assert ["update"] in calls

    def test_format_results_lists_pages_then_sources(self, tmp_path):
        text = format_results({
            "pages": [{"page": "alpha.md", "score": 0.42, "category": "ai-security",
                       "description": "第一页", "snippet": ""}],
            "sources": [{"source": "2026/07/a.md", "score": 0.8, "title": "源标题",
                         "url": "", "compiled": False, "pages": [], "snippet": ""}],
            "deep": True,
        })
        assert "pages/alpha.md  0.42  [ai-security]" in text
        assert "第一页" in text
        assert "sources/2026/07/a.md  0.80  未编译" in text
        assert text.index("## pages") < text.index("## sources")

    def test_format_results_omits_sources_when_not_escalated(self):
        text = format_results({"pages": [], "sources": [], "deep": False})
        assert text == "## pages 无命中"
