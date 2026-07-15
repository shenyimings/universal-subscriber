import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import {
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
