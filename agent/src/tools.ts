/** wiki 编译 agent 的工具集。写入类工具在代码侧做硬校验，不信任模型自觉。 */
import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { promisify } from "node:util";
import type { AgentTool } from "@earendil-works/pi-agent-core";
import { Type } from "typebox";
import { CATEGORIES, FILE_NAME_RE, MAX_PAGE_CHARS, validatePage } from "./wiki.ts";

const execFileAsync = promisify(execFile);

const FETCH_URL_MAX_CHARS = 8_000;

export interface CompileCtx {
	root: string; // subscriber 仓库根目录
	wikiDir: string;
	/** 本次被写过的页面 -> 首次触碰前的原始内容（新页面为 ""）。用于回滚。 */
	touched: Map<string, string>;
	finished: boolean;
	summary: string;
}

function pagePath(ctx: CompileCtx, file: string): string {
	if (!FILE_NAME_RE.test(file)) {
		throw new Error(`非法文件名 ${file}：必须是小写英文加连字符的 .md，不含路径`);
	}
	return path.join(ctx.wikiDir, "pages", file);
}

function recordTouch(ctx: CompileCtx, file: string, originalBefore: string): void {
	if (!ctx.touched.has(file)) ctx.touched.set(file, originalBefore);
}

function text(s: string) {
	return { content: [{ type: "text" as const, text: s }] };
}

export function makeTools(ctx: CompileCtx): AgentTool<any>[] {
	const listIndex: AgentTool<any> = {
		name: "list_index",
		label: "List index",
		description: `按分类查看现有页面索引（每条：[[页面名]] \`tag\` — 简介）。分类：${CATEGORIES.join(", ")}`,
		parameters: Type.Object({
			category: Type.String({ description: "分类名" }),
		}),
		execute: async (_id, params) => {
			if (!CATEGORIES.includes(params.category)) {
				throw new Error(`未知分类 ${params.category}，可选：${CATEGORIES.join(", ")}`);
			}
			const index = fs.readFileSync(path.join(ctx.wikiDir, "index.md"), "utf-8");
			const m = index.split(new RegExp(`^## ${params.category}$`, "m"))[1];
			const section = m ? m.split(/^## /m)[0].trim() : "";
			return text(section || "(该分类暂无页面)");
		},
	};

	const readPage: AgentTool<any> = {
		name: "read_page",
		label: "Read page",
		description: "读取一个 wiki 页面的完整内容",
		parameters: Type.Object({
			file: Type.String({ description: "页面文件名，如 llm-agent-harness.md" }),
		}),
		execute: async (_id, params) => {
			const p = pagePath(ctx, params.file);
			if (!fs.existsSync(p)) throw new Error(`页面不存在：${params.file}`);
			return text(fs.readFileSync(p, "utf-8"));
		},
	};

	const editPage: AgentTool<any> = {
		name: "edit_page",
		label: "Edit page",
		description:
			"对已有页面做精确字符串替换（old_string 必须在页面中唯一出现）。更新大页面时用它做增量修改，不要整页重写",
		parameters: Type.Object({
			file: Type.String({ description: "页面文件名" }),
			old_string: Type.String({ description: "被替换的原文，必须唯一" }),
			new_string: Type.String({ description: "替换后的内容" }),
		}),
		execute: async (_id, params) => {
			const p = pagePath(ctx, params.file);
			if (!fs.existsSync(p)) throw new Error(`页面不存在：${params.file}`);
			const before = fs.readFileSync(p, "utf-8");
			const count = before.split(params.old_string).length - 1;
			if (count === 0) throw new Error("old_string 在页面中不存在，请先 read_page 核对原文");
			if (count > 1) throw new Error(`old_string 出现 ${count} 次，请扩大上下文使其唯一`);
			const after = before.replace(params.old_string, params.new_string);
			recordTouch(ctx, params.file, before);
			fs.writeFileSync(p, after);
			const problems = validatePage(params.file, after);
			return text(problems.length ? `已替换，但存在问题：\n${problems.join("\n")}` : "已替换。");
		},
	};

	const writePage: AgentTool<any> = {
		name: "write_page",
		label: "Write page",
		description:
			"新建页面，或整页重写小页面。内容必须含 frontmatter（description、category、tags）。超过大小上限的已有页面会被拒绝，只能 edit_page 瘦身或拆分",
		parameters: Type.Object({
			file: Type.String({ description: "页面文件名，小写英文加连字符" }),
			content: Type.String({ description: "页面完整内容" }),
		}),
		execute: async (_id, params) => {
			const p = pagePath(ctx, params.file);
			const originalBefore = fs.existsSync(p) ? fs.readFileSync(p, "utf-8") : "";
			if (originalBefore.length > MAX_PAGE_CHARS) {
				throw new Error(
					`${params.file} 已超过 ${MAX_PAGE_CHARS} 字符上限，禁止整页重写；请用 edit_page 增量修改，或拆分出新页面`,
				);
			}
			if (params.content.length > MAX_PAGE_CHARS && params.content.length > originalBefore.length) {
				throw new Error(`内容超过 ${MAX_PAGE_CHARS} 字符上限，请精简或拆分`);
			}
			const problems = validatePage(params.file, params.content);
			if (problems.length) throw new Error(`页面校验未通过：\n${problems.join("\n")}`);
			recordTouch(ctx, params.file, originalBefore);
			fs.writeFileSync(p, params.content);
			return text(originalBefore ? "已重写。" : "已创建。");
		},
	};

	const fetchUrl: AgentTool<any> = {
		name: "fetch_url",
		label: "Fetch URL",
		description: "抓取一个网页并抽取正文。源内容太薄（只有链接）时用它补全上下文",
		parameters: Type.Object({
			url: Type.String({ description: "http(s) 链接" }),
		}),
		execute: async (_id, params, signal) => {
			const { stdout } = await execFileAsync(
				"uv",
				["run", "python", "scripts/extract_url.py", params.url],
				{ cwd: ctx.root, timeout: 60_000, maxBuffer: 4 * 1024 * 1024, signal },
			);
			const body = stdout.trim();
			if (body.length > FETCH_URL_MAX_CHARS) {
				return text(`${body.slice(0, FETCH_URL_MAX_CHARS)}\n\n（正文过长，已截断）`);
			}
			return text(body || "(正文为空)");
		},
	};

	const finish: AgentTool<any> = {
		name: "finish",
		label: "Finish",
		description:
			"沉淀完成后调用，结束本次编译。若这篇源没有值得长期沉淀的内容，不动任何页面直接 finish 也是正确选择",
		parameters: Type.Object({
			summary: Type.String({ description: "一句话：本文沉淀到了哪些页面/为何不沉淀" }),
		}),
		execute: async (_id, params) => {
			ctx.finished = true;
			ctx.summary = params.summary;
			return { ...text("已记录，编译结束。"), terminate: true };
		},
	};

	return [listIndex, readPage, editPage, writePage, fetchUrl, finish];
}
