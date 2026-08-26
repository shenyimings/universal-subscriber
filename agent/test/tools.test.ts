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

test("read_page 大页面切片返回，首片带大纲，offset 续读", async () => {
	const ctx = tmpCtx();
	const section = `## 章节标题\n${"正文".repeat(5000)}\n`;
	const big = `# 大页面\n${section.repeat(3)}`;
	fs.writeFileSync(path.join(ctx.wikiDir, "pages", "big.md"), big);

	const first = await run(ctx, "read_page", { file: "big.md" });
	assert.match(first, new RegExp(`页面共 ${big.length} 字符`));
	assert.match(first, /章节定位/);
	assert.match(first, /## 章节标题 @\d+/);
	assert.ok(first.length < big.length);

	const m = /续读用 offset=(\d+)/.exec(first);
	assert.ok(m);
	const next = await run(ctx, "read_page", { file: "big.md", offset: Number(m[1]) });
	assert.match(next, new RegExp(`本次返回第 ${m[1]}–`));
	assert.doesNotMatch(next, /章节定位/);

	const tail = await run(ctx, "read_page", { file: "big.md", offset: big.length - 10 });
	assert.match(tail, /已到末尾/);
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
