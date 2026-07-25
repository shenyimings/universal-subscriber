"""Tests for skill/wiki-knowledge-base/wiki-search, the copy that ships inside
the wiki repo. It is a standalone executable (no package import), so it is
driven as a subprocess against a stub `qmd` placed on PATH."""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "skill" / "wiki-knowledge-base" / "wiki-search"


def _make_wiki(tmp_path, name="wiki"):
    """A wiki with the script sitting where it does in the real repo."""
    wiki = tmp_path / name
    (wiki / "pages").mkdir(parents=True)
    (wiki / "sources" / "2026" / "07").mkdir(parents=True)
    skill_dir = wiki / "skill" / "wiki-knowledge-base"
    skill_dir.mkdir(parents=True)
    target = skill_dir / "wiki-search"
    target.write_text(SCRIPT.read_text())
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    return wiki


def _page(wiki, stem, description="简介", category="ai-security"):
    (wiki / "pages" / f"{stem}.md").write_text(
        f"---\ndescription: {description}\ncategory: {category}\ntags:\n- fuzzing\n---\n正文\n"
    )


def _source(wiki, rel, compiled=True, pages=("alpha.md",), indent="", title="源标题"):
    path = wiki / "sources" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    front = [
        "---",
        f'title: "{title}"',
        "url: https://example.com/a",
        f"compiled: {'true' if compiled else 'false'}",
    ]
    if pages:
        front.append("pages:")
        front += [f"{indent}- {p}" for p in pages]
    front.append("---")
    path.write_text("\n".join(front) + "\n\n## 原文\n正文\n")


def _stub_qmd(tmp_path, pages_hits=(), sources_hits=(), collections=None):
    """A fake qmd. Records every invocation to calls.log and answers searches
    from canned JSON keyed by the -c collection."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    payload = {
        "pages": list(pages_hits),
        "sources": list(sources_hits),
        # name -> registered path, i.e. what `collection show` reports
        "collections": collections or {},
        "log": str(tmp_path / "calls.log"),
    }
    (tmp_path / "stub.json").write_text(json.dumps(payload))
    qmd = bin_dir / "qmd"
    qmd.write_text(f'''#!/usr/bin/env python3
import json, sys
cfg = json.load(open({str(tmp_path / "stub.json")!r}))
argv = sys.argv[1:]
with open(cfg["log"], "a") as f:
    f.write(" ".join(argv) + "\\n")
if argv[:2] == ["collection", "show"]:
    path = cfg["collections"].get(argv[2])
    print(f"Collection not found: {{argv[2]}}" if path is None
          else f"Collection: {{argv[2]}}\\n  Path:     {{path}}\\n  Pattern:  **/*.md")
    sys.exit(0)
if argv[0] in ("search", "vsearch", "query"):
    col = argv[argv.index("-c") + 1]
    print(json.dumps(cfg["sources" if col.endswith("-sources") else "pages"]))
sys.exit(0)
''')
    qmd.chmod(qmd.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _hit(collection, rel, score):
    return {"file": f"qmd://{collection}/{rel}", "score": score, "snippet": "片段"}


def _run(wiki, bin_dir, *args, cwd=None):
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    proc = subprocess.run(
        [str(wiki / "skill" / "wiki-knowledge-base" / "wiki-search"), *args],
        capture_output=True, text=True, env=env, cwd=cwd or str(wiki.parent), timeout=60,
    )
    return proc


def _calls(tmp_path):
    log = tmp_path / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


class TestLayering:
    def test_strong_page_hit_does_not_touch_sources(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        _page(wiki, "alpha", description="第一页")
        bin_dir = _stub_qmd(
            tmp_path,
            pages_hits=[_hit("wiki-pages", "alpha.md", 0.9)],
            collections={"wiki-pages": str(wiki / "pages"),
                         "wiki-sources": str(wiki / "sources")},
        )
        out = _run(wiki, bin_dir, "关键词").stdout
        assert "pages/alpha.md  0.90  [ai-security]" in out
        assert "第一页" in out
        assert "sources" not in out
        assert not any("wiki-sources" in c and c.startswith("search") for c in _calls(tmp_path))

    def test_weak_page_hit_escalates_and_climbs_back(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        _page(wiki, "alpha")
        _source(wiki, "2026/07/a.md", pages=["beta.md"])
        bin_dir = _stub_qmd(
            tmp_path,
            pages_hits=[_hit("wiki-pages", "alpha.md", 0.2)],
            sources_hits=[_hit("wiki-sources", "2026/07/a.md", 0.8)],
            collections={"wiki-pages": str(wiki / "pages"),
                         "wiki-sources": str(wiki / "sources")},
        )
        out = _run(wiki, bin_dir, "关键词").stdout
        assert "sources/2026/07/a.md  0.80" in out
        assert "未编译" not in out
        assert "已沉淀进: beta.md" in out

    def test_uncompiled_source_is_flagged(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        _source(wiki, "2026/07/a.md", compiled=False, pages=())
        bin_dir = _stub_qmd(
            tmp_path,
            sources_hits=[_hit("wiki-sources", "2026/07/a.md", 0.7)],
            collections={"wiki-pages": str(wiki / "pages"),
                         "wiki-sources": str(wiki / "sources")},
        )
        out = _run(wiki, bin_dir, "关键词").stdout
        assert "## pages 无命中" in out
        assert "未编译" in out
        assert "已沉淀进" not in out

    def test_page_already_found_is_not_suggested_again(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        _page(wiki, "alpha")
        _source(wiki, "2026/07/a.md", pages=["alpha.md"])
        bin_dir = _stub_qmd(
            tmp_path,
            pages_hits=[_hit("wiki-pages", "alpha.md", 0.9)],
            sources_hits=[_hit("wiki-sources", "2026/07/a.md", 0.4)],
            collections={"wiki-pages": str(wiki / "pages"),
                         "wiki-sources": str(wiki / "sources")},
        )
        out = _run(wiki, bin_dir, "--deep", "关键词").stdout
        assert "sources/2026/07/a.md" in out
        assert "已沉淀进" not in out

    @pytest.mark.parametrize("flag,sub", [("--semantic", "vsearch"), ("--hybrid", "query")])
    def test_mode_flags_pick_the_qmd_subcommand(self, tmp_path, flag, sub):
        wiki = _make_wiki(tmp_path)
        bin_dir = _stub_qmd(tmp_path, collections={"wiki-pages": str(wiki / "pages"),
                                                   "wiki-sources": str(wiki / "sources")})
        _run(wiki, bin_dir, flag, "-n", "3", "关键词")
        searches = [c for c in _calls(tmp_path) if c.startswith(sub)]
        assert searches and "-n 3" in searches[0]


class TestPathHandling:
    def test_frontmatter_survives_slugged_paths_and_indented_lists(self, tmp_path):
        """qmd 压掉重复连字符；agent 侧写的 pages: 列表是缩进的。两者都得认。"""
        wiki = _make_wiki(tmp_path)
        _source(wiki, "2026/07/anthropic--advanced-tool--eafa6e2f.md",
                pages=["beta.md"], indent="  ", title="Advanced: tool use")
        bin_dir = _stub_qmd(
            tmp_path,
            sources_hits=[_hit("wiki-sources", "2026/07/anthropic-advanced-tool-eafa6e2f.md", 0.8)],
            collections={"wiki-pages": str(wiki / "pages"),
                         "wiki-sources": str(wiki / "sources")},
        )
        out = _run(wiki, bin_dir, "关键词").stdout
        assert "Advanced: tool use" in out
        assert "已沉淀进: beta.md" in out
        assert "未编译" not in out

    def test_registers_collections_when_absent(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        bin_dir = _stub_qmd(tmp_path, collections={})
        _run(wiki, bin_dir, "关键词")
        calls = _calls(tmp_path)
        assert f"collection add {wiki / 'pages'} --name wiki-pages" in calls
        assert f"collection add {wiki / 'sources'} --name wiki-sources" in calls
        assert "update" in calls
        assert not any(c.startswith("collection remove") for c in calls)

    def test_repoints_collections_after_the_repo_moved(self, tmp_path):
        """每次 git clone 落在新路径，旧的绝对路径必须被摘掉重挂。"""
        wiki = _make_wiki(tmp_path)
        bin_dir = _stub_qmd(tmp_path, collections={
            "wiki-pages": "/gone/wiki/pages", "wiki-sources": "/gone/wiki/sources"})
        _run(wiki, bin_dir, "关键词")
        calls = _calls(tmp_path)
        assert "collection remove wiki-pages" in calls
        assert f"collection add {wiki / 'pages'} --name wiki-pages" in calls
        assert calls.index("collection remove wiki-pages") < calls.index(
            f"collection add {wiki / 'pages'} --name wiki-pages")

    def test_up_to_date_collections_are_left_alone(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        bin_dir = _stub_qmd(tmp_path, collections={"wiki-pages": str(wiki / "pages"),
                                                   "wiki-sources": str(wiki / "sources")})
        _run(wiki, bin_dir, "关键词")
        calls = _calls(tmp_path)
        assert not any(c.startswith(("collection add", "collection remove")) for c in calls)
        assert "update" not in calls

    def test_collection_names_follow_the_wiki_directory(self, tmp_path):
        """同机多个 wiki 不该互相覆盖集合。"""
        wiki = _make_wiki(tmp_path, name="wiki-page")
        bin_dir = _stub_qmd(tmp_path, collections={})
        _run(wiki, bin_dir, "关键词")
        assert any("--name wiki-page-pages" in c for c in _calls(tmp_path))

    def test_runs_from_any_cwd(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        _page(wiki, "alpha")
        bin_dir = _stub_qmd(
            tmp_path,
            pages_hits=[_hit("wiki-pages", "alpha.md", 0.9)],
            collections={"wiki-pages": str(wiki / "pages"),
                         "wiki-sources": str(wiki / "sources")},
        )
        out = _run(wiki, bin_dir, "关键词", cwd=str(tmp_path.parent)).stdout
        assert "pages/alpha.md" in out

    def test_rejects_a_directory_that_is_not_a_wiki(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        shutil.rmtree(wiki / "sources")
        bin_dir = _stub_qmd(tmp_path, collections={})
        proc = _run(wiki, bin_dir, "关键词")
        assert proc.returncode != 0
        assert "不像一个 llm-wiki 仓库" in proc.stderr
