import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { rollbackTouched, verifyTouched } from "../src/verify.ts";

const GOOD = `---
description: 测试页
category: llm-systems
tags:
- fuzzing
- llm-agent
---
正文
`;

function tmpWiki(): { dir: string; pages: string } {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "verify-"));
	const pages = path.join(dir, "pages");
	fs.mkdirSync(pages, { recursive: true });
	return { dir, pages };
}

test("verifyTouched 放行合规、抓住违规", () => {
	const { dir, pages } = tmpWiki();
	fs.writeFileSync(path.join(pages, "ok.md"), GOOD);
	fs.writeFileSync(path.join(pages, "bad.md"), "没有 frontmatter");
	const touched = new Map([
		["ok.md", ""],
		["bad.md", ""],
	]);
	const problems = verifyTouched(dir, touched);
	assert.ok(problems.every((p) => p.startsWith("bad.md")));
	assert.ok(problems.length >= 1);
	fs.rmSync(dir, { recursive: true });
});

test("rollbackTouched 恢复改动、删除新建页", () => {
	const { dir, pages } = tmpWiki();
	const original = GOOD;
	fs.writeFileSync(path.join(pages, "edited.md"), original);
	const touched = new Map([
		["edited.md", original], // 已有页：改动后应被还原
		["created.md", ""], // 本次新建：应被删除
	]);
	// 模拟 agent 的写盘：改动已有页 + 创建新页
	fs.writeFileSync(path.join(pages, "edited.md"), `${original}\n被 agent 追加的半成品`);
	fs.writeFileSync(path.join(pages, "created.md"), "半成品新页面");

	rollbackTouched(dir, touched);

	assert.equal(fs.readFileSync(path.join(pages, "edited.md"), "utf-8"), original);
	assert.equal(fs.existsSync(path.join(pages, "created.md")), false);
	fs.rmSync(dir, { recursive: true });
});
