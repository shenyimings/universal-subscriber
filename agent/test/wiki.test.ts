import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import {
	appendSourceRef,
	dumpFront,
	markCompiled,
	parseFront,
	pendingSources,
	validatePage,
} from "../src/wiki.ts";

const PAGE = `---
description: 测试页
category: llm-systems
tags:
- fuzzing
- llm-agent
---
正文，引用 [[llm-agent-harness]]。
`;

test("parseFront/dumpFront 往返", () => {
	const { meta, body } = parseFront(PAGE);
	assert.equal(meta.category, "llm-systems");
	assert.deepEqual(meta.tags, ["fuzzing", "llm-agent"]);
	const { meta: meta2 } = parseFront(dumpFront(meta, body));
	assert.deepEqual(meta2, meta);
});

test("validatePage 通过合规页面", () => {
	assert.deepEqual(validatePage("test-page.md", PAGE), []);
});

test("validatePage 抓住违规", () => {
	const bad = `---
description: ''
category: nonsense
tags:
- only-one
---
正文 [[中文链接]]
`;
	const problems = validatePage("Bad Name.md", bad);
	assert.equal(problems.length, 5);
	assert.match(problems.join("\n"), /文件名/);
	assert.match(problems.join("\n"), /description/);
	assert.match(problems.join("\n"), /category/);
	assert.match(problems.join("\n"), /tags/);
	assert.match(problems.join("\n"), /wikilink/);
});

test("validatePage 拒绝缺 frontmatter", () => {
	assert.match(validatePage("a.md", "没有 frontmatter")[0], /frontmatter/);
});

test("pendingSources 按 date 排序且跳过已编译", () => {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wiki-"));
	const src = path.join(dir, "sources", "2026", "07");
	fs.mkdirSync(src, { recursive: true });
	const mk = (name: string, date: string, compiled: boolean) =>
		fs.writeFileSync(
			path.join(src, name),
			`---\ntitle: ${name}\ndate: '${date}'\ncompiled: ${compiled}\n---\n正文`,
		);
	mk("b.md", "2026-07-02", false);
	mk("a.md", "2026-07-05", false);
	mk("c.md", "2026-07-01", true);
	const got = pendingSources(dir).map((p) => path.basename(p));
	assert.deepEqual(got, ["b.md", "a.md"]);

	markCompiled(path.join(src, "b.md"));
	assert.deepEqual(pendingSources(dir).map((p) => path.basename(p)), ["a.md"]);
	const { meta, body } = parseFront(fs.readFileSync(path.join(src, "b.md"), "utf-8"));
	assert.equal(meta.compiled, true);
	assert.equal(body, "正文");
	fs.rmSync(dir, { recursive: true });
});

test("markCompiled 合并 pages 反向链接", () => {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wiki-"));
	const src = path.join(dir, "s.md");
	fs.writeFileSync(src, "---\ntitle: t\ncompiled: false\npages:\n- old.md\n---\n正文");
	markCompiled(src, ["new.md", "old.md"]);
	const { meta } = parseFront(fs.readFileSync(src, "utf-8"));
	assert.deepEqual(meta.pages, ["new.md", "old.md"]);
	fs.rmSync(dir, { recursive: true });
});

test("appendSourceRef 建段、去重、追加", () => {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wiki-"));
	fs.mkdirSync(path.join(dir, "pages"), { recursive: true });
	const srcDir = path.join(dir, "sources", "2026", "07");
	fs.mkdirSync(srcDir, { recursive: true });
	const s1 = path.join(srcDir, "one.md");
	const s2 = path.join(srcDir, "two.md");
	fs.writeFileSync(s1, "---\ntitle: 第一篇\nsource: Blog A\ndate: '2026-07-01'\n---\n正文");
	fs.writeFileSync(s2, "---\ntitle: 第二篇\n---\n正文");
	fs.writeFileSync(path.join(dir, "pages", "p.md"), `${PAGE}\n## 相关页面\n\n- [[llm-agent-harness]]\n`);

	appendSourceRef(dir, "p.md", s1);
	appendSourceRef(dir, "p.md", s1); // 重复登记应被去重
	appendSourceRef(dir, "p.md", s2);
	const text = fs.readFileSync(path.join(dir, "pages", "p.md"), "utf-8");
	assert.equal(text.split("## 来源").length, 2);
	assert.equal(text.split("../sources/2026/07/one.md").length, 2);
	assert.match(text, /- \[第一篇\]\(\.\.\/sources\/2026\/07\/one\.md\)（Blog A，2026-07-01）/);
	assert.match(text, /- \[第二篇\]\(\.\.\/sources\/2026\/07\/two\.md\)\n/);
	fs.rmSync(dir, { recursive: true });
});
