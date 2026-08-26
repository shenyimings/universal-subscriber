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
	splitImageSection,
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

function wikiWithImage(name?: string): string {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "wiki-img-"));
	fs.mkdirSync(path.join(dir, "imgs"), { recursive: true });
	if (name) fs.writeFileSync(path.join(dir, "imgs", name), "fake bytes");
	return dir;
}

test("validatePage 通过合规页面", () => {
	assert.deepEqual(validatePage("test-page.md", PAGE, wikiWithImage()), []);
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
	const problems = validatePage("Bad Name.md", bad, wikiWithImage());
	assert.equal(problems.length, 5);
	assert.match(problems.join("\n"), /文件名/);
	assert.match(problems.join("\n"), /description/);
	assert.match(problems.join("\n"), /category/);
	assert.match(problems.join("\n"), /tags/);
	assert.match(problems.join("\n"), /wikilink/);
});

test("validatePage 拒绝缺 frontmatter", () => {
	assert.match(validatePage("a.md", "没有 frontmatter", wikiWithImage())[0], /frontmatter/);
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

test("validatePage 接受指向已存在图片的 ![[x]]", () => {
	const dir = wikiWithImage("harness-arch.png");
	const page = `${PAGE}\n![[harness-arch.png]]\n`;
	assert.deepEqual(validatePage("test-page.md", page, dir), []);
	fs.rmSync(dir, { recursive: true });
});

test("validatePage 拒绝 imgs/ 里不存在的图片", () => {
	const dir = wikiWithImage();
	const page = `${PAGE}\n![[missing.png]]\n`;
	const problems = validatePage("test-page.md", page, dir);
	assert.equal(problems.length, 1);
	assert.match(problems[0], /missing\.png/);
	assert.match(problems[0], /imgs/);
	fs.rmSync(dir, { recursive: true });
});

test("validatePage 拒绝不规范的图片名", () => {
	const dir = wikiWithImage("Harness Arch.PNG");
	const page = `${PAGE}\n![[Harness Arch.PNG]]\n`;
	const problems = validatePage("test-page.md", page, dir);
	assert.equal(problems.length, 1);
	assert.match(problems[0], /小写/);
	fs.rmSync(dir, { recursive: true });
});

test("图片嵌入不被当成普通 wikilink 校验", () => {
	// [[a.md]] 会因为带点被判非法页面名；![[a.png]] 是图片，走另一条规则
	const dir = wikiWithImage("fig-1.png");
	assert.deepEqual(validatePage("test-page.md", `${PAGE}\n![[fig-1.png]]\n`, dir), []);
	fs.rmSync(dir, { recursive: true });
});

test("splitImageSection 把图片段从正文里摘出来", () => {
	const { text, images } = splitImageSection(
		"正文一段\n\n## 图片\n\n![架构图](https://cdn.example.com/a.png)\n![](https://cdn.example.com/b.jpg)\n",
	);
	assert.equal(text, "正文一段");
	assert.match(images, /## 图片/);
	assert.match(images, /a\.png/);
	assert.match(images, /b\.jpg/);
});

test("splitImageSection 没有图片段时原样返回", () => {
	const { text, images } = splitImageSection("只有正文");
	assert.equal(text, "只有正文");
	assert.equal(images, "");
});

test("图片段不受正文截断影响", () => {
	// 正文被 max_chars 砍掉时，段在末尾的图片链接会一起消失——先摘再砍。
	const long = "正".repeat(9000);
	const { text, images } = splitImageSection(`${long}\n\n## 图片\n\n![](https://cdn.example.com/a.png)\n`);
	const sliced = text.slice(0, 6000);
	assert.equal(sliced.length, 6000);
	assert.match(images, /a\.png/);
});
