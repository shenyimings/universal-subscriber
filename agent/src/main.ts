/** wiki 编译 agent：逐条把 pending 归档源沉淀进 wiki/pages/。
 *
 * 外循环（本文件，确定性代码）：挑选 pending 源、跑内循环、用验证器裁决、
 * 标记 compiled、重建索引。内循环（pi Agent）：模型自主读索引/读页/增量编辑/
 * 抓链接，直到调用 finish。终止由代码侧验证器决定，不采信模型自评。
 *
 * 用法：node src/main.ts [--limit N] [--max-turns N] [--dry-run] [--verbose]
 *      [--file <相对 wiki 目录的 pending 源路径>]
 */
import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { promisify } from "node:util";
import { Agent } from "@earendil-works/pi-agent-core";
import { createModels } from "@earendil-works/pi-ai";
import { deepseekProvider } from "@earendil-works/pi-ai/providers/deepseek";
import { parse as parseYaml } from "yaml";
import { pruneContext } from "./context.ts";
import { type CompileCtx, IMAGE_BUDGET, makeTools, UV_BIN } from "./tools.ts";
import { rollbackTouched, verifyTouched } from "./verify.ts";
import {
	appendLog,
	appendSourceRef,
	CATEGORIES,
	markCompiled,
	MAX_PAGE_CHARS,
	parseFront,
	pendingSources,
	splitImageSection,
} from "./wiki.ts";

const execFileAsync = promisify(execFile);

const ROOT = path.resolve(import.meta.dirname, "../..");
const MAX_REPAIR_ROUNDS = 2;

function arg(name: string, fallback: number): number {
	const i = process.argv.indexOf(name);
	return i >= 0 ? Number(process.argv[i + 1]) : fallback;
}

function argStr(name: string, fallback: string): string {
	const i = process.argv.indexOf(name);
	return i >= 0 ? process.argv[i + 1] : fallback;
}

interface SourceInput {
	title: string;
	url: string;
	date: string;
	summary: string;
	content: string;
	/** 末尾的「## 图片」段，绕过正文截断单独传给模型 */
	images: string;
}

function readSource(sourcePath: string, maxChars: number): SourceInput {
	const { meta, body } = parseFront(fs.readFileSync(sourcePath, "utf-8"));
	let summary = "";
	let content = body.trim();
	if (body.includes("## 原文")) {
		const [head, ...rest] = body.split("## 原文");
		summary = head.replace("## 摘要", "").trim();
		content = rest.join("## 原文").trim();
	}
	const { text, images } = splitImageSection(content);
	return {
		title: String(meta.title ?? path.basename(sourcePath, ".md")),
		url: String(meta.url ?? ""),
		date: String(meta.date ?? ""),
		summary,
		content: text.slice(0, maxChars),
		images,
	};
}

async function compileSource(
	sourcePath: string,
	systemPrompt: string,
	model: any,
	maxChars: number,
	maxTurns: number,
	wikiDir: string,
	configPath: string,
	verbose: boolean,
): Promise<{ ok: boolean; ctx: CompileCtx; turns: number; cost: number; tokens: number }> {
	const ctx: CompileCtx = {
		root: ROOT,
		wikiDir,
		configPath,
		touched: new Map<string, string>(),
		edits: 0,
		savedImages: [],
		finished: false,
		summary: "",
	};
	const agent = new Agent({
		initialState: { systemPrompt, model, tools: makeTools(ctx) },
		transformContext: async (messages) => pruneContext(messages),
		afterToolCall: async ({ toolCall, result, isError }) => {
			if (isError) {
				const first = result.content?.find((c: any) => c.type === "text") as any;
				console.error(`    ✗ ${toolCall.name}: ${String(first?.text ?? "").split("\n")[0]}`);
			}
		},
	});

	let turns = 0;
	let turnAllowance = maxTurns;
	let cost = 0;
	let tokens = 0;
	agent.subscribe(async (event) => {
		if (event.type === "turn_start" && ++turns > turnAllowance) {
			console.error(`  达到 ${turnAllowance} 轮上限，中止`);
			agent.abort();
		}
		if (event.type === "tool_execution_start") {
			console.error(`  [turn ${turns}] ${event.toolName} ${verbose ? JSON.stringify(event.args ?? {}).slice(0, 300) : ""}`);
		}
		if (verbose && event.type === "message_end" && event.message.role === "assistant") {
			const said = (event.message.content ?? [])
				.filter((c: any) => c.type === "text")
				.map((c: any) => c.text.trim())
				.join("\n")
				.trim();
			if (said) console.error(`  [turn ${turns}] 说：${said}`);
			if (event.message.stopReason) console.error(`  [turn ${turns}] stopReason=${event.message.stopReason}`);
		}
		if (event.type === "message_end" && event.message.role === "assistant") {
			cost += event.message.usage?.cost?.total ?? 0;
			tokens += event.message.usage?.totalTokens ?? 0;
		}
	});

	const src = readSource(sourcePath, maxChars);
	const prompt = [
		`新文章：${src.title}（${src.url}，${src.date}）`,
		`摘要：${src.summary}`,
		`正文：`,
		src.content,
		src.images,
	].join("\n");

	try {
		await agent.prompt(prompt);
		for (let round = 0; round < MAX_REPAIR_ROUNDS && ctx.finished; round++) {
			const problems = verifyTouched(ctx.wikiDir, ctx.touched);
			if (!problems.length) break;
			console.error(`  验证器打回（第 ${round + 1} 轮）：${problems.length} 个问题`);
			ctx.finished = false;
			turnAllowance = turns + 8; // 修复轮需要自己的预算，否则打回即必死
			await agent.prompt(`验证器发现以下问题，请修复后再次调用 finish：\n${problems.join("\n")}`);
		}
	} catch (e) {
		console.error(`  agent 异常：${e instanceof Error ? e.message : e}`);
	}

	const ok = ctx.finished && verifyTouched(ctx.wikiDir, ctx.touched).length === 0;
	return { ok, ctx, turns, cost, tokens };
}

async function main(): Promise<void> {
	const limit = arg("--limit", 3);
	const maxTurns = arg("--max-turns", 24);
	const configPath = path.resolve(argStr("--config", path.join(ROOT, "config.yaml")));
	const dryRun = process.argv.includes("--dry-run");
	const verbose = process.argv.includes("--verbose");
	const onlyFile = argStr("--file", "");

	const envPath = path.join(ROOT, ".env");
	if (fs.existsSync(envPath)) process.loadEnvFile(envPath);

	const cfg = parseYaml(fs.readFileSync(configPath, "utf-8"));
	const prompts = parseYaml(fs.readFileSync(path.join(ROOT, "prompts.yaml"), "utf-8"));
	const wikiDir = path.resolve(path.dirname(configPath), cfg.wiki?.dir ?? "wiki");
	const maxChars = cfg.limits?.max_chars_per_item ?? 6000;
	const models = createModels();
	models.setProvider(deepseekProvider());
	const model = models.getModel("deepseek", cfg.llm.wiki_model ?? cfg.llm.model);
	if (!model) throw new Error(`pi-ai 不认识模型 ${cfg.llm.wiki_model ?? cfg.llm.model}`);

	const systemPrompt = String(prompts.wiki_agent)
		.replaceAll("{persona}", String(prompts.persona).trim())
		.replaceAll("{categories}", CATEGORIES.join(", "))
		.replaceAll("{max_page_chars}", String(MAX_PAGE_CHARS))
		.replaceAll("{image_budget}", String(IMAGE_BUDGET));

	const pending = pendingSources(wikiDir);
	let batch = pending.slice(0, limit);
	if (onlyFile) {
		const target = path.resolve(wikiDir, onlyFile);
		if (!pending.includes(target)) {
			throw new Error(`--file 未命中 pending 源：${onlyFile}（不存在，或已 compiled）`);
		}
		batch = [target];
	}
	console.error(`[wiki-agent] backlog ${pending.length} 篇，本次处理 ${batch.length} 篇`);
	if (dryRun) {
		for (const p of batch) console.error(`  ${path.relative(wikiDir, p)}`);
		return;
	}

	let totalCost = 0;
	let totalTokens = 0;
	for (const [i, sourcePath] of batch.entries()) {
		const name = path.basename(sourcePath);
		console.error(`[wiki-agent ${i + 1}/${batch.length}] ${name}`);
		const { ok, ctx, turns, cost, tokens } = await compileSource(
			sourcePath, systemPrompt, model, maxChars, maxTurns, wikiDir, configPath, verbose,
		);
		totalCost += cost;
		totalTokens += tokens;
		const pages = [...ctx.touched.keys()];
		if (ok) {
			for (const f of pages) appendSourceRef(wikiDir, f, sourcePath);
			markCompiled(sourcePath, pages);
			appendLog(wikiDir, "ingest", `${readSource(sourcePath, 80).title} -> ${pages.join(", ") || "(无沉淀)"}`);
			try {
				await execFileAsync(
					UV_BIN,
					["run", "subscriber", "wiki", "--reindex", "--config", configPath],
					{ cwd: ROOT },
				);
			} catch (e) {
				// reindex 失败不该毁掉整批：索引下次运行会重建
				console.error(`  reindex 失败：${e instanceof Error ? e.message : e}`);
			}
			console.error(`  完成：${ctx.summary}（${turns} 轮，${tokens} tokens，$${cost.toFixed(4)}）`);
		} else {
			rollbackTouched(ctx.wikiDir, ctx.touched, ctx.savedImages);
			console.error(
				`  失败，回滚 ${pages.length} 个页面并保持 pending（${turns} 轮，${tokens} tokens，$${cost.toFixed(4)}）`,
			);
		}
	}
	console.error(`[wiki-agent] 合计 ${totalTokens} tokens，$${totalCost.toFixed(4)}`);
}

main().catch((e) => {
	console.error(e);
	process.exit(1);
});
