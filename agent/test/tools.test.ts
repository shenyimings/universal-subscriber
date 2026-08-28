import * as assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as http from "node:http";
import { test } from "node:test";
import { type CompileCtx, IMAGE_BUDGET, makeTools } from "../src/tools.ts";
import { rollbackTouched } from "../src/verify.ts";

function tmpCtx(): CompileCtx {
	const wikiDir = fs.mkdtempSync(path.join(os.tmpdir(), "tools-"));
	fs.mkdirSync(path.join(wikiDir, "pages"), { recursive: true });
	return {
		root: wikiDir,
		wikiDir,
		configPath: path.join(wikiDir, "config.yaml"),
		touched: new Map(),
		edits: 0,
		savedImages: [],
		finished: false,
		summary: "",
	};
}

function tool(ctx: CompileCtx, name: string) {
	const t = makeTools(ctx).find((t) => t.name === name);
	assert.ok(t);
	return t;
}

async function run(ctx: CompileCtx, name: string, params: any): Promise<string> {
	const result = await tool(ctx, name).execute("id", params, undefined as any, undefined);
	return (result.content[0] as { type: "text"; text: string }).text;
}

test("read_page 小页面整页返回", async () => {
	const ctx = tmpCtx();
	fs.writeFileSync(path.join(ctx.wikiDir, "pages", "small.md"), "短内容");
	assert.equal(await run(ctx, "read_page", { file: "small.md" }), "短内容");
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

test("read_page 大页面不再整页倒出，先给章节大纲", async () => {
	const ctx = tmpCtx();
	const section = (t: string) => `## ${t}\n${"正文".repeat(5000)}\n`;
	const big = `# 大页面\n${section("甲")}${section("乙")}${section("丙")}`;
	fs.writeFileSync(path.join(ctx.wikiDir, "pages", "big.md"), big);

	const first = await run(ctx, "read_page", { file: "big.md" });
	assert.match(first, new RegExp(`页面共 ${big.length} 字符`));
	assert.match(first, /2\. ## 甲/);
	assert.match(first, /read_page/); // 指路：按节读
	assert.ok(first.length < big.length / 10);
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

test("read_page 按 section 序号只返回那一节", async () => {
	const ctx = tmpCtx();
	fs.writeFileSync(
		path.join(ctx.wikiDir, "pages", "p.md"),
		"前言\n\n## 甲\n\n甲的正文\n\n## 乙\n\n乙的正文\n",
	);
	const out = await run(ctx, "read_page", { file: "p.md", section: 2 });
	assert.match(out, /第 2 节/);
	assert.match(out, /甲的正文/);
	assert.doesNotMatch(out, /乙的正文/);
	await assert.rejects(run(ctx, "read_page", { file: "p.md", section: 9 }), /没有第 9 节/);
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

test("outline_page 便宜地给出页面地图", async () => {
	const ctx = tmpCtx();
	fs.writeFileSync(
		path.join(ctx.wikiDir, "pages", "p.md"),
		"前言\n\n## 甲\n\n甲的正文\n\n## 来源\n\n- x\n",
	);
	const out = await run(ctx, "outline_page", { file: "p.md" });
	assert.match(out, /1\. （前言）/);
	assert.match(out, /2\. ## 甲/);
	assert.match(out, /3\. ## 来源.*保留段/);
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

const PAGE = `---
description: 测试页
category: llm-systems
tags:
- fuzzing
- llm-agent
---
前言正文。

## 甲

甲的正文。

## 来源

- [x](../sources/x.md)
`;

test("edit_section 按序号整节替换，不需要 old_string 锚点", async () => {
	const ctx = tmpCtx();
	const p = path.join(ctx.wikiDir, "pages", "p.md");
	fs.writeFileSync(p, PAGE);

	const out = await run(ctx, "edit_section", {
		file: "p.md",
		section: 2,
		content: "## 甲\n\n甲的正文。补充了新知识。\n",
	});
	assert.match(out, /已更新第 2 节/);
	const after = fs.readFileSync(p, "utf-8");
	assert.match(after, /补充了新知识/);
	assert.match(after, /前言正文。/);
	assert.match(after, /- \[x\]\(\.\.\/sources\/x\.md\)/); // 来源段原样保留
	assert.equal(ctx.touched.get("p.md"), PAGE);
	assert.equal(ctx.edits, 1);
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

test("edit_section 拒绝写保留段，也拒绝把普通节改成保留段", async () => {
	const ctx = tmpCtx();
	fs.writeFileSync(path.join(ctx.wikiDir, "pages", "p.md"), PAGE);
	await assert.rejects(
		run(ctx, "edit_section", { file: "p.md", section: 3, content: "## 来源\n\n乱写\n" }),
		/保留段/,
	);
	await assert.rejects(
		run(ctx, "edit_section", { file: "p.md", section: 2, content: "## 来源\n\n乱写\n" }),
		/保留段/,
	);
	assert.equal(ctx.touched.size, 0);
	assert.equal(ctx.edits, 0);
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

test("insert_section 插入新节，落点被夹在保留段之前", async () => {
	const ctx = tmpCtx();
	const p = path.join(ctx.wikiDir, "pages", "p.md");
	fs.writeFileSync(p, PAGE);

	const out = await run(ctx, "insert_section", {
		file: "p.md",
		after: 3, // 「## 来源」是第 3 节
		title: "乙",
		content: "乙的正文。",
	});
	assert.match(out, /已插入/);
	const after = fs.readFileSync(p, "utf-8");
	assert.ok(after.indexOf("## 乙") < after.indexOf("## 来源"));
	await assert.rejects(
		run(ctx, "insert_section", { file: "p.md", after: 1, title: "来源", content: "x" }),
		/保留段/,
	);
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

test("edit_section 拒绝让超限页面继续膨胀", async () => {
	const ctx = tmpCtx();
	const big = `---\ndescription: 大页\ncategory: llm-systems\ntags:\n- fuzzing\n- llm-agent\n---\n## 甲\n${"填".repeat(41000)}\n`;
	fs.writeFileSync(path.join(ctx.wikiDir, "pages", "big.md"), big);
	await assert.rejects(
		run(ctx, "edit_section", { file: "big.md", section: 1, content: `## 甲\n${"填".repeat(41100)}\n` }),
		/不能再增长/,
	);
	assert.equal(ctx.touched.size, 0);
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

test("write_page 禁止覆盖已有页面，但允许重写本次新建的页面", async () => {
	const ctx = tmpCtx();
	fs.writeFileSync(path.join(ctx.wikiDir, "pages", "old.md"), PAGE);
	await assert.rejects(
		run(ctx, "write_page", { file: "old.md", content: PAGE.replace("甲的正文。", "洗掉") }),
		/禁止整页覆盖/,
	);
	assert.equal(fs.readFileSync(path.join(ctx.wikiDir, "pages", "old.md"), "utf-8"), PAGE);

	await run(ctx, "write_page", { file: "fresh.md", content: PAGE });
	const again = await run(ctx, "write_page", { file: "fresh.md", content: PAGE.replace("前言正文。", "改过") });
	assert.match(again, /已重写/);
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

test("grep_pages 做字面全文匹配，返回页面、行号与片段", async () => {
	const ctx = tmpCtx();
	fs.writeFileSync(path.join(ctx.wikiDir, "pages", "a.md"), "前言\n\n## 甲\n\n讲了 MTP 推测解码。\n");
	fs.writeFileSync(path.join(ctx.wikiDir, "pages", "b.md"), "无关内容\n");

	const hit = await run(ctx, "grep_pages", { pattern: "推测解码" });
	assert.match(hit, /a\.md 第 5 行/);
	assert.match(hit, /行号不是章节序号/);
	assert.match(hit, /MTP 推测解码/);
	assert.doesNotMatch(hit, /b\.md/);

	assert.match(await run(ctx, "grep_pages", { pattern: "从未写过的词" }), /无命中/);
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

test("edit_page 拒绝让超限页面继续膨胀，允许瘦身", async () => {
	const ctx = tmpCtx();
	const big = `---\ndescription: 大页\ncategory: llm-systems\ntags:\n- fuzzing\n- llm-agent\n---\n唯一锚点\n${"填".repeat(41000)}`;
	fs.writeFileSync(path.join(ctx.wikiDir, "pages", "big.md"), big);

	await assert.rejects(
		run(ctx, "edit_page", { file: "big.md", old_string: "唯一锚点", new_string: "唯一锚点加了更多内容" }),
		/不能再增长/,
	);
	// 增长被拒后页面未被改动、未记入 touched
	assert.equal(fs.readFileSync(path.join(ctx.wikiDir, "pages", "big.md"), "utf-8"), big);
	assert.equal(ctx.touched.size, 0);

	const shrunk = await run(ctx, "edit_page", { file: "big.md", old_string: "唯一锚点", new_string: "锚" });
	assert.match(shrunk, /已替换/);
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

test("改动预算：临近上限提示，用完后拒绝", async () => {
	const ctx = tmpCtx();
	const page = (i: number) =>
		`---\ndescription: 页${i}\ncategory: llm-systems\ntags:\n- fuzzing\n- llm-agent\n---\n正文\n`;
	for (let i = 1; i <= 8; i++) {
		const r = await run(ctx, "write_page", { file: `p${i}.md`, content: page(i) });
		if (i < 8) assert.doesNotMatch(r, /改动预算/);
		else assert.match(r, /改动预算 8\/10/);
	}
	await run(ctx, "write_page", { file: "p9.md", content: page(9) });
	await run(ctx, "write_page", { file: "p10.md", content: page(10) });
	await assert.rejects(
		run(ctx, "write_page", { file: "p11.md", content: page(11) }),
		/预算.*已用完.*finish/,
	);
	await assert.rejects(
		run(ctx, "edit_page", { file: "p1.md", old_string: "正文", new_string: "改" }),
		/预算.*已用完/,
	);
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

test("list_index 读分类切片索引，缺失分类返回空提示", async () => {
	const ctx = tmpCtx();
	fs.mkdirSync(path.join(ctx.wikiDir, "index"), { recursive: true });
	fs.writeFileSync(
		path.join(ctx.wikiDir, "index", "ai-security.md"),
		"# ai-security\n\n1 个页面。本分类标签：`fuzzing`\n\n- [[alpha]] `fuzzing` — 第一页\n",
	);

	const listing = await run(ctx, "list_index", { category: "ai-security" });
	assert.match(listing, /\[\[alpha\]\]/);
	assert.match(listing, /本分类标签/);

	assert.equal(await run(ctx, "list_index", { category: "llm-systems" }), "(该分类暂无页面)");
	await assert.rejects(run(ctx, "list_index", { category: "nope" }), /未知分类/);
	fs.rmSync(ctx.wikiDir, { recursive: true });
});

/** save_image 走的是 uv -> scripts/save_image.py，这里连真实进程一起验。 */
test("save_image 存进 wiki/imgs 并返回 ![[...]] 写法", async () => {
	const png = Buffer.from("89504e470d0a1a0a0000000d49484452", "hex");
	const server = http.createServer((_req, res) => {
		res.writeHead(200, { "content-type": "image/png" });
		res.end(png);
	});
	await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
	const { port } = server.address() as { port: number };

	const ctx = tmpCtx();
	ctx.root = path.resolve(import.meta.dirname, "..", "..");
	try {
		const out = await run(ctx, "save_image", {
			url: `http://127.0.0.1:${port}/fig.png`,
			name: "harness-arch",
		});
		assert.match(out, /!\[\[harness-arch\.png\]\]/);
		assert.deepEqual(fs.readFileSync(path.join(ctx.wikiDir, "imgs", "harness-arch.png")), png);
	} finally {
		server.close();
		fs.rmSync(ctx.wikiDir, { recursive: true });
	}
});

test("save_image 拒绝非图片响应", async () => {
	const server = http.createServer((_req, res) => {
		res.writeHead(200, { "content-type": "text/html" });
		res.end("<html>404</html>");
	});
	await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
	const { port } = server.address() as { port: number };

	const ctx = tmpCtx();
	ctx.root = path.resolve(import.meta.dirname, "..", "..");
	try {
		await assert.rejects(
			run(ctx, "save_image", { url: `http://127.0.0.1:${port}/x`, name: "nope" }),
			/不是图片/,
		);
		assert.ok(!fs.existsSync(path.join(ctx.wikiDir, "imgs")));
	} finally {
		server.close();
		fs.rmSync(ctx.wikiDir, { recursive: true });
	}
});

test("save_image 有硬预算，超了就拒绝", async () => {
	const png = Buffer.from("89504e470d0a1a0a0000000d49484452", "hex");
	const server = http.createServer((_req, res) => {
		res.writeHead(200, { "content-type": "image/png" });
		res.end(png);
	});
	await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
	const { port } = server.address() as { port: number };

	const ctx = tmpCtx();
	ctx.root = path.resolve(import.meta.dirname, "..", "..");
	try {
		for (let i = 0; i < IMAGE_BUDGET; i++) {
			await run(ctx, "save_image", { url: `http://127.0.0.1:${port}/${i}.png`, name: `fig-${i}` });
		}
		await assert.rejects(
			run(ctx, "save_image", { url: `http://127.0.0.1:${port}/x.png`, name: "fig-x" }),
			/预算/,
		);
		assert.equal(fs.readdirSync(path.join(ctx.wikiDir, "imgs")).length, IMAGE_BUDGET);
	} finally {
		server.close();
		fs.rmSync(ctx.wikiDir, { recursive: true });
	}
});

test("回滚删掉本次存下的图，不在 wiki 里留孤儿文件", async () => {
	const ctx = tmpCtx();
	fs.mkdirSync(path.join(ctx.wikiDir, "imgs"), { recursive: true });
	const kept = path.join(ctx.wikiDir, "imgs", "old.png");
	const fresh = path.join(ctx.wikiDir, "imgs", "new.png");
	fs.writeFileSync(kept, "早就存在");
	fs.writeFileSync(fresh, "本次新存");
	ctx.savedImages.push("new.png");

	rollbackTouched(ctx.wikiDir, ctx.touched, ctx.savedImages);
	assert.ok(fs.existsSync(kept));
	assert.ok(!fs.existsSync(fresh));
	fs.rmSync(ctx.wikiDir, { recursive: true });
});
