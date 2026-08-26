"""Tests for the wiki compiler — LLM calls are mocked."""

from unittest.mock import MagicMock, patch

from subscriber.wiki import (
    _apply_fix,
    _chat,
    _parse_plan,
    _strip_stale_markers,
    compile_source,
    dump_front,
    fix_wikilinks,
    lint_wiki,
    parse_front,
    pending_sources,
    rebuild_index,
)

LLM_CFG = {"base_url": "https://api.test", "model": "test", "api_key_env": "TEST_KEY"}
PROMPTS = {
    "persona": "test reader",
    "wiki_plan": "{persona}|{index}|{categories}|{title}|{source}|{url}|{summary}|{content}",
    "wiki_page": (
        "{persona}|{file}|{focus}|{index}|{category}|{existing}"
        "|{title}|{url}|{date}|{summary}|{content}"
    ),
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

    def test_mixed_quoted_and_bare_dates(self, tmp_path):
        """sources/ holds both `date: 2026-07-02` and `date: '2026-07-01'`;
        YAML hands back a date for one and a str for the other."""
        bare = _write_source(tmp_path, "bare.md", day="2026-07-02")
        bare.write_text(bare.read_text().replace("date: '2026-07-02'", "date: 2026-07-02"))
        _write_source(tmp_path, "quoted.md", day="2026-07-01")
        names = [p.name for p in pending_sources(tmp_path)]
        assert names == ["quoted.md", "bare.md"]


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
        assert src_meta["pages"] == ["topic.md"]
        assert "- [T](../sources/2026/07/a.md)（Src，2026-07-01）" in page_body
        assert "## 来源" in page_body
        assert "ingest | T" in (tmp_path / "log.md").read_text()

    @patch("subscriber.wiki._chat")
    def test_source_ref_deduped_on_recompile(self, mock_chat, tmp_path):
        src = _write_source(tmp_path)
        mock_chat.side_effect = [
            '[{"file": "topic.md", "action": "create", "focus": "要点",'
            ' "category": "ai-security", "tags": ["fuzzing"]}]',
            "---\ndescription: 主题页\n---\n\n知识内容\n",
        ]
        compile_source(src, tmp_path, LLM_CFG, PROMPTS, 6000)
        first = (tmp_path / "pages" / "topic.md").read_text()
        mock_chat.side_effect = [
            '[{"file": "topic.md", "action": "update", "focus": "要点",'
            ' "category": "ai-security", "tags": ["fuzzing"]}]',
            first,  # 模型按提示原样保留旧的来源段
        ]
        compile_source(src, tmp_path, LLM_CFG, PROMPTS, 6000)
        text = (tmp_path / "pages" / "topic.md").read_text()
        assert text.count("../sources/2026/07/a.md") == 1
        assert text.count("## 来源") == 1

    @patch("subscriber.wiki._chat")
    def test_manual_heading_numbers_stripped(self, mock_chat, tmp_path):
        src = _write_source(tmp_path)
        mock_chat.side_effect = [
            '[{"file": "t.md", "action": "create", "focus": "f"}]',
            "---\ndescription: d\n---\n# 1 是标题的一部分\n\n## 3. 关键推论\n\n"
            "### 3.1 界面 3.1 正文里的数字保留\n\n## 无编号标题\n",
        ]
        compile_source(src, tmp_path, LLM_CFG, PROMPTS, 6000)
        body = (tmp_path / "pages" / "t.md").read_text()
        assert "## 关键推论" in body
        assert "### 界面 3.1 正文里的数字保留" in body
        assert "# 1 是标题的一部分" in body  # H1 untouched
        assert "## 无编号标题" in body

    @patch("subscriber.wiki._chat")
    def test_page_prompt_gets_same_category_index(self, mock_chat, tmp_path):
        _write_page(tmp_path, "peer", description="同类页", category="ai-security")
        _write_page(tmp_path, "stranger", description="他类页", category="llm-systems")
        src = _write_source(tmp_path)
        mock_chat.side_effect = [
            '[{"file": "topic.md", "action": "create", "focus": "f",'
            ' "category": "ai-security", "tags": []}]',
            "---\ndescription: d\n---\n正文\n",
        ]
        compile_source(src, tmp_path, LLM_CFG, PROMPTS, 6000)
        page_prompt = mock_chat.call_args_list[1].args[1]
        assert "|ai-security|" in page_prompt
        assert "[[peer]]" in page_prompt and "[[stranger]]" not in page_prompt

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
        assert index.startswith("# Index\n")
        assert "## ai-security" in index
        assert "- [[alpha]] `fuzzing` `llm-agent` — 第一页" in index
        assert "## uncategorized" in index
        assert "- [[beta]] — 第二页" in index
        assert index.index("## ai-security") < index.index("## uncategorized")

    def test_rebuild_index_writes_category_slices(self, tmp_path):
        _write_page(tmp_path, "alpha", description="第一页",
                    category="ai-security", tags=["fuzzing", "llm-agent"])
        _write_page(tmp_path, "gamma", description="第三页",
                    category="ai-security", tags=["fuzzing"])
        _write_page(tmp_path, "beta", description="第二页")
        rebuild_index(tmp_path)

        slice_ = (tmp_path / "index" / "ai-security.md").read_text()
        assert slice_.startswith("# ai-security\n")
        assert "2 个页面。本分类标签：`fuzzing` `llm-agent`" in slice_
        assert "- [[alpha]] `fuzzing` `llm-agent` — 第一页" in slice_
        assert "[[beta]]" not in slice_
        assert (tmp_path / "index" / "uncategorized.md").exists()
        assert not (tmp_path / "index" / "llm-systems.md").exists()

    def test_rebuild_index_prunes_emptied_category_slices(self, tmp_path):
        _write_page(tmp_path, "alpha", category="ai-security")
        rebuild_index(tmp_path)
        assert (tmp_path / "index" / "ai-security.md").exists()

        (tmp_path / "pages" / "alpha.md").unlink()
        _write_page(tmp_path, "beta", category="llm-systems")
        rebuild_index(tmp_path)
        assert not (tmp_path / "index" / "ai-security.md").exists()
        assert (tmp_path / "index" / "llm-systems.md").exists()

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


class TestApplyFix:
    def test_rename_all_link_forms(self):
        text = "a [[old]] b [[old|别名]] c [[old#节]] d [[older]]"
        out = _apply_fix(text, "old", "rename", "new")
        assert out == "a [[new]] b [[new|别名]] c [[new#节]] d [[older]]"

    def test_drop_unwraps_and_clears_marker(self):
        text = "a [[gone]] b [[gone|别名]] c [[gone]]（未建） d"
        out = _apply_fix(text, "gone", "drop")
        assert out == "a gone b 别名 c gone d"

    def test_keep_marks_once_idempotent(self):
        text = "a [[future]] b [[future]]（未建）"
        out = _apply_fix(text, "future", "keep")
        assert out == "a [[future]]（未建） b [[future]]（未建）"
        assert _apply_fix(out, "future", "keep") == out

    def test_strip_stale_markers_only_for_existing(self):
        text = "[[built]]（未建） and [[still-missing]]（未建）"
        out = _strip_stale_markers(text, {"built"})
        assert out == "[[built]] and [[still-missing]]（未建）"


class TestFixWikilinks:
    PROMPTS = {"wiki_fix": "{pages}|{broken}|{total}|{broken_count}|{budget}"}

    @patch("subscriber.wiki._chat")
    def test_end_to_end_rename_drop_keep(self, mock_chat, tmp_path):
        _write_page(tmp_path, "harness-engineering", body="real page [[a]]")
        _write_page(
            tmp_path, "a",
            body="x [[llm-harness-engineering]] y [[tiny-detail]] z [[worth-building]]",
        )
        mock_chat.return_value = (
            '[{"target": "llm-harness-engineering", "action": "rename", "to": "harness-engineering"},'
            ' {"target": "tiny-detail", "action": "drop"},'
            ' {"target": "worth-building", "action": "keep"}]'
        )
        fix_wikilinks(tmp_path, LLM_CFG, self.PROMPTS)
        body = (tmp_path / "pages" / "a.md").read_text()
        assert "[[harness-engineering]]" in body
        assert "[[tiny-detail]]" not in body and "tiny-detail" in body
        assert "[[worth-building]]（未建）" in body
        assert "fix |" in (tmp_path / "log.md").read_text()

    @patch("subscriber.wiki._chat")
    def test_invalid_rename_target_skipped(self, mock_chat, tmp_path):
        _write_page(tmp_path, "a", body="[[missing]] [[a]]")
        mock_chat.return_value = (
            '[{"target": "missing", "action": "rename", "to": "nonexistent"}]'
        )
        fix_wikilinks(tmp_path, LLM_CFG, self.PROMPTS)
        assert "[[missing]]" in (tmp_path / "pages" / "a.md").read_text()

    @patch("subscriber.wiki._chat")
    def test_no_broken_links_no_llm_call(self, mock_chat, tmp_path):
        _write_page(tmp_path, "a", body="see [[a]]")
        fix_wikilinks(tmp_path, LLM_CFG, self.PROMPTS)
        mock_chat.assert_not_called()

    @patch("subscriber.wiki._chat")
    def test_budget_computed_from_total_links(self, mock_chat, tmp_path):
        body = " ".join("[[a]]" for _ in range(20)) + " [[missing]] [[missing]]"
        _write_page(tmp_path, "a", body=body)
        mock_chat.return_value = '[{"target": "missing", "action": "keep"}]'
        fix_wikilinks(tmp_path, LLM_CFG, self.PROMPTS)
        prompt = mock_chat.call_args.args[1]
        # 22 个链接 * 10% = 2 次保留配额
        assert prompt.endswith("|22|2|2")


class TestImageEmbedsInLint:
    def test_image_embed_is_not_a_broken_wikilink(self, tmp_path):
        """![[x.png]] is a figure in wiki/imgs, not a page that failed to
        exist; the lint must not report it, nor --fix rewrite it."""
        _write_page(tmp_path, "a", body="正文\n\n![[harness-arch.png]]\n")
        (tmp_path / "imgs").mkdir()
        (tmp_path / "imgs" / "harness-arch.png").write_bytes(b"x")
        issues = lint_wiki(tmp_path)
        assert not any("harness-arch" in i for i in issues)

    def test_missing_image_reported(self, tmp_path):
        _write_page(tmp_path, "a", body="正文\n\n![[gone.png]]\n")
        issues = lint_wiki(tmp_path)
        assert any("gone.png" in i and "imgs" in i for i in issues)
