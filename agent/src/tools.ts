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
/** read_page 单次返回的上限：大页面按切片迭代读取，控制每轮进入上下文的量。 */
const READ_PAGE_MAX_CHARS = 16_000;

/** 大页面的章节大纲：标题行 + 字符位置，供模型跳读相关切片。 */
function pageOutline(content: string): string {
	const lines: string[] = [];
	const re = /^#{1,3} .*$/gm;
	for (const m of content.matchAll(re)) {
		lines.push(`${m[0]} @${m.index}`);
	}
	return lines.join("\n") || "（无标题结构）";
}

export interface CompileCtx {
	root: string; // subscriber 仓库根目录
	wikiDir: string;
	/** 本次被写过的页面 -> 首次触碰前的原始内容（新页面为 ""）。用于回滚。 */
	touched: Map<string, string>;
	/** 本次成功的 edit_page/write_page 次数；预算在 harness 强制，不指望模型自律。 */
	edits: number;
	finished: boolean;
	summary: string;
}

/** 每篇源的改动预算：超过说明模型不肯收敛，强制它 finish。 */
export const EDIT_BUDGET = 10;

function spendEdit(ctx: CompileCtx): string {
	ctx.edits += 1;
	if (ctx.edits >= EDIT_BUDGET - 2) {
		return `（改动预算 ${ctx.edits}/${EDIT_BUDGET}：请收尾，完成必要改动后立即 finish）`;
	}
	return "";
}

function checkEditBudget(ctx: CompileCtx): void {
	if (ctx.edits >= EDIT_BUDGET) {
		throw new Error(`本次改动预算（${EDIT_BUDGET} 次）已用完，不能再改动页面，请立即调用 finish`);
	}
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
		description:
			"读取一个 wiki 页面。超过读取上限的大页面按切片返回：首次调用（不带 offset）返回章节大纲（含字符位置）和开头切片，之后按大纲用 offset 只读需要的部分，不要顺序读完整页",
		parameters: Type.Object({
			file: Type.String({ description: "页面文件名，如 llm-agent-harness.md" }),
			offset: Type.Optional(Type.Number({ description: "起始字符位置（大页面续读/跳读用）" })),
		}),
		execute: async (_id, params) => {
			const p = pagePath(ctx, params.file);
			if (!fs.existsSync(p)) throw new Error(`页面不存在：${params.file}`);
			const content = fs.readFileSync(p, "utf-8");
			if (content.length <= READ_PAGE_MAX_CHARS && !params.offset) return text(content);
			const offset = Math.max(0, Math.min(params.offset ?? 0, content.length));
			const end = Math.min(offset + READ_PAGE_MAX_CHARS, content.length);
			const head = `（页面共 ${content.length} 字符，本次返回第 ${offset}–${end} 字符${
				end < content.length ? `，续读用 offset=${end}` : "，已到末尾"
			}）`;
			const outline = offset === 0 ? `\n章节定位（标题 @字符位置）：\n${pageOutline(content)}\n` : "";
			return text(`${head}${outline}\n${content.slice(offset, end)}`);
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
			checkEditBudget(ctx);
			const p = pagePath(ctx, params.file);
			if (!fs.existsSync(p)) throw new Error(`页面不存在：${params.file}`);
			const before = fs.readFileSync(p, "utf-8");
			const count = before.split(params.old_string).length - 1;
			if (count === 0) throw new Error("old_string 在页面中不存在，请先 read_page 核对原文");
			if (count > 1) throw new Error(`old_string 出现 ${count} 次，请扩大上下文使其唯一`);
			const after = before.replace(params.old_string, params.new_string);
			if (before.length > MAX_PAGE_CHARS && after.length > before.length) {
				throw new Error(
					`${params.file} 已超过 ${MAX_PAGE_CHARS} 字符上限，不能再增长；` +
						"请把新内容放进拆分出的新页面（write_page），或先用 edit_page 删减/迁出旧内容再合并",
				);
			}
			recordTouch(ctx, params.file, before);
			fs.writeFileSync(p, after);
			const note = spendEdit(ctx);
			const problems = validatePage(params.file, after);
			return text(
				problems.length ? `已替换，但存在问题：\n${problems.join("\n")}${note}` : `已替换。${note}`,
			);
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
			checkEditBudget(ctx);
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
			const note = spendEdit(ctx);
			return text(`${originalBefore ? "已重写。" : "已创建。"}${note}`);
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
