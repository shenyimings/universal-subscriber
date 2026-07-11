"""Tests for archiving kept articles into wiki/sources/."""

from datetime import date

import yaml

from subscriber.archive import archive_update, slugify
from subscriber.fetch import Update


def _make_update(kind="article", title="Some Title", link="https://x.com/a"):
    return Update(source="Src Blog", title=title, link=link, content="full text", kind=kind)


class TestSlugify:
    def test_basic(self):
        assert slugify("Hello, World!") == "hello-world"

    def test_chinese_kept(self):
        assert slugify("智能合约安全") == "智能合约安全"

    def test_empty_falls_back(self):
        assert slugify("!!!") == "untitled"


class TestArchiveUpdate:
    def test_writes_file_with_frontmatter(self, tmp_path):
        path = archive_update(_make_update(), "中文摘要", tmp_path)
        assert path is not None and path.exists()
        text = path.read_text()
        meta = yaml.safe_load(text.split("---")[1])
        assert meta["title"] == "Some Title"
        assert meta["url"] == "https://x.com/a"
        assert meta["compiled"] is False
        assert "## 摘要" in text and "中文摘要" in text
        assert "## 原文" in text and "full text" in text

    def test_page_change_not_archived(self, tmp_path):
        assert archive_update(_make_update(kind="page_change"), "s", tmp_path) is None

    def test_dedupe_by_existing_path(self, tmp_path):
        p1 = archive_update(_make_update(), "first", tmp_path)
        p2 = archive_update(_make_update(), "second", tmp_path)
        assert p1 == p2
        assert "first" in p1.read_text()  # not overwritten

    def test_day_override_sets_dir_and_date(self, tmp_path):
        path = archive_update(_make_update(), "s", tmp_path, day=date(2025, 3, 7))
        assert "2025/03" in str(path)
        assert "date: '2025-03-07'" in path.read_text()
