/** wiki 编译 agent 的工具集。写入类工具在代码侧做硬校验，不信任模型自觉。 */
import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { promisify } from "node:util";
import type { AgentTool } from "@earendil-works/pi-agent-core";
import { Type } from "typebox";
import {
	formatOutline,
	getSection,
	insertSection as spliceSection,
	isReserved,
	replaceSection,
	splitSections,
} from "./sections.ts";
import { CATEGORIES, FILE_NAME_RE, MAX_PAGE_CHARS, validatePage } from "./wiki.ts";

const execFileAsync = promisify(execFile);

/** systemd 环境的 PATH 不含 ~/.local/bin，spawn "uv" 会 ENOENT，这里解析绝对路径。 */
export const UV_BIN = (() => {
	const local = path.join(os.homedir(), ".local", "bin", "uv");
	return fs.existsSync(local) ? local : "uv";
})();

const FETCH_URL_MAX_CHARS = 8_000;
/** 超过这个大小的页面不整页返回，只给章节大纲，让模型按节取用。 */
const READ_PAGE_MAX_CHARS = 16_000;
/** grep_pages 单次最多返回几行命中：去重判断看的是有没有，不是全部。 */
const GREP_MAX_HITS = 40;
const GREP_SNIPPET_CHARS = 160;

export interface CompileCtx {
	root: string; // subscriber 仓库根目录
	wikiDir: string;
	/** config.yaml 路径，search_wiki 转交给 subscriber search */
	configPath: string;
	/** 本次被写过的页面 -> 首次触碰前的原始内容（新页面为 ""）。用于回滚。 */
	touched: Map<string, string>;
	/** 本次成功的 edit_page/write_page 次数；预算在 harness 强制，不指望模型自律。 */
	edits: number;
	/** 本次 save_image 存进 imgs/ 的文件名；回滚时删掉，别在 wiki 里留孤儿图。 */
	savedImages: string[];
	finished: boolean;
	summary: string;
}

/** 每篇源的改动预算：超过说明模型不肯收敛，强制它 finish。 */
export const EDIT_BUDGET = 10;
/** 每篇源最多存几张图。一篇文章值得留下的图极少，多了就是在搬运插图。 */
export const IMAGE_BUDGET = 3;

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

function readPageFile(ctx: CompileCtx, file: string): string {
	const p = pagePath(ctx, file);
	if (!fs.existsSync(p)) throw new Error(`页面不存在：${file}`);
	return fs.readFileSync(p, "utf-8");
}

/**
 * 写盘的唯一入口：棘轮检查（超限页只许瘦身）、记录回滚点、落盘、跑页面校验。
 * 所有写入类工具都走这里，别在工具里各写一遍。
 */
function commitPage(ctx: CompileCtx, file: string, before: string, after: string, done: string) {
	if (before.length > MAX_PAGE_CHARS && after.length > before.length) {
		throw new Error(
			`${file} 已超过 ${MAX_PAGE_CHARS} 字符上限，不能再增长；` +
				"请把新内容放进拆分出的新页面（write_page），或先删减/迁出旧内容再合并",
		);
	}
	recordTouch(ctx, file, before);
	fs.writeFileSync(pagePath(ctx, file), after);
	const note = spendEdit(ctx);
	const problems = validatePage(file, after, ctx.wikiDir);
	return text(problems.length ? `${done}，但存在问题：\n${problems.join("\n")}${note}` : `${done}。${note}`);
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
			// 分类切片索引由 subscriber wiki --reindex 维护，只读它，不再切整库 index.md
			const p = path.join(ctx.wikiDir, "index", `${params.category}.md`);
			if (!fs.existsSync(p)) return text("(该分类暂无页面)");
			return text(fs.readFileSync(p, "utf-8").trim());
		},
	};

	const outlinePage: AgentTool<any> = {
		name: "outline_page",
		label: "Outline page",
		description:
			"列出一个页面的章节地图：每节的序号、标题、字符数、首句。极便宜，选目标页面时先对候选页各拉一遍，再决定读哪一节",
		parameters: Type.Object({
			file: Type.String({ description: "页面文件名，如 llm-agent-harness.md" }),
		}),
		execute: async (_id, params) => text(formatOutline(readPageFile(ctx, params.file))),
	};

	const readPage: AgentTool<any> = {
		name: "read_page",
		label: "Read page",
		description:
			"读取页面。带 section 时只返回该序号的那一节（序号来自 outline_page）；不带 section 时小页面整页返回，大页面只返回章节大纲——不要试图整页读完，按节取用",
		parameters: Type.Object({
			file: Type.String({ description: "页面文件名，如 llm-agent-harness.md" }),
			section: Type.Optional(Type.Number({ description: "章节序号（来自 outline_page）" })),
		}),
		execute: async (_id, params) => {
			const content = readPageFile(ctx, params.file);
			if (params.section !== undefined) {
				const s = getSection(content, params.section);
				return text(`（第 ${s.index} 节，共 ${s.text.length} 字符）\n${s.text}`);
			}
			if (content.length <= READ_PAGE_MAX_CHARS) return text(content);
			return text(
				`${formatOutline(content)}\n\n（页面较大，未整页返回。用 read_page 带 section 序号读需要的那几节，用 edit_section 就地改写）`,
			);
		},
	};

	const editPage: AgentTool<any> = {
		name: "edit_page",
		label: "Edit page",
		description:
			"对已有页面做精确字符串替换（old_string 必须唯一）。只用于改 frontmatter 或某句话这类小修补；整节的合并改写用 edit_section",
		parameters: Type.Object({
			file: Type.String({ description: "页面文件名" }),
			old_string: Type.String({ description: "被替换的原文，必须唯一" }),
			new_string: Type.String({ description: "替换后的内容" }),
		}),
		execute: async (_id, params) => {
			checkEditBudget(ctx);
			const before = readPageFile(ctx, params.file);
			const count = before.split(params.old_string).length - 1;
			if (count === 0) throw new Error("old_string 在页面中不存在，请先 read_page 核对原文");
			if (count > 1) throw new Error(`old_string 出现 ${count} 次，请扩大上下文使其唯一`);
			const after = before.replace(params.old_string, params.new_string);
			return commitPage(ctx, params.file, before, after, "已替换");
		},
	};

	const editSection: AgentTool<any> = {
		name: "edit_section",
		label: "Edit section",
		description:
			"按章节序号整节改写（序号来自 outline_page）。合并新知识的默认写法：先 read_page 读那一节，再把改好的整节内容写回。不需要 old_string 锚点，定位成本与页面大小无关",
		parameters: Type.Object({
			file: Type.String({ description: "页面文件名" }),
			section: Type.Number({ description: "章节序号（来自 outline_page）" }),
			content: Type.String({ description: "该节的新全文，含 `## 标题` 行" }),
		}),
		execute: async (_id, params) => {
			checkEditBudget(ctx);
			const before = readPageFile(ctx, params.file);
			const target = getSection(before, params.section);
			if (isReserved(target.title)) {
				throw new Error(`第 ${params.section} 节「${target.title}」是代码维护的保留段，不要写入`);
			}
			const firstLine = params.content.trim().split("\n")[0] ?? "";
			if (isReserved(firstLine)) {
				throw new Error(`不能把第 ${params.section} 节改成保留段「${firstLine}」`);
			}
			const after = replaceSection(before, params.section, params.content);
			return commitPage(ctx, params.file, before, after, `已更新第 ${params.section} 节`);
		},
	};

	const insertSection: AgentTool<any> = {
		name: "insert_section",
		label: "Insert section",
		description:
			"在指定章节之后插入一个新的二级小节。新知识不属于任何现有章节时用它，别再往页尾堆——页尾是代码维护的「## 来源」段",
		parameters: Type.Object({
			file: Type.String({ description: "页面文件名" }),
			after: Type.Number({ description: "插到第几节之后（序号来自 outline_page）" }),
			title: Type.String({ description: "新小节标题，不带 ## " }),
			content: Type.String({ description: "新小节正文，不含标题行" }),
		}),
		execute: async (_id, params) => {
			checkEditBudget(ctx);
			if (isReserved(`## ${params.title.trim()}`)) {
				throw new Error(`「${params.title}」是代码维护的保留段，不能新建`);
			}
			const before = readPageFile(ctx, params.file);
			const { content: after, at } = spliceSection(before, params.after, params.title, params.content);
			const note = at === params.after ? "" : `（落点已调整到第 ${at} 节之后，保留段必须留在页尾）`;
			return commitPage(ctx, params.file, before, after, `已插入「${params.title}」${note}`);
		},
	};

	const writePage: AgentTool<any> = {
		name: "write_page",
		label: "Write page",
		description:
			"新建页面。内容必须含 frontmatter（description、category、tags）。已有页面禁止整页覆盖，只能用 edit_section / edit_page 增量修改",
		parameters: Type.Object({
			file: Type.String({ description: "页面文件名，小写英文加连字符" }),
			content: Type.String({ description: "页面完整内容" }),
		}),
		execute: async (_id, params) => {
			checkEditBudget(ctx);
			const p = pagePath(ctx, params.file);
			const exists = fs.existsSync(p);
			// 本次新建的页面（touched 里原始内容为 ""）允许重写，历史页面一律不许：
			// 模型重新生成全文时对没读到的部分没有保留义务，这是整页被洗掉的根因。
			if (exists && ctx.touched.get(params.file) !== "") {
				throw new Error(
					`${params.file} 已存在，禁止整页覆盖；请用 outline_page 看结构，再用 edit_section 改写相关章节或 insert_section 新增一节`,
				);
			}
			const originalBefore = exists ? fs.readFileSync(p, "utf-8") : "";
			if (params.content.length > MAX_PAGE_CHARS) {
				throw new Error(`内容超过 ${MAX_PAGE_CHARS} 字符上限，请精简或拆分`);
			}
			const problems = validatePage(params.file, params.content, ctx.wikiDir);
			if (problems.length) throw new Error(`页面校验未通过：\n${problems.join("\n")}`);
			recordTouch(ctx, params.file, originalBefore);
			fs.writeFileSync(p, params.content);
			const note = spendEdit(ctx);
			return text(`${exists ? "已重写。" : "已创建。"}${note}`);
		},
	};

	const grepPages: AgentTool<any> = {
		name: "grep_pages",
		label: "Grep pages",
		description:
			"在所有页面正文里做字面全文匹配，返回命中的页面、行号与片段。合并前先用它查这件事是不是已经写过了——索引里的一句简介看不出正文写了什么",
		parameters: Type.Object({
			pattern: Type.String({ description: "字面匹配串（不是正则），如 推测解码" }),
		}),
		execute: async (_id, params) => {
			const needle = params.pattern.trim().toLowerCase();
			if (!needle) throw new Error("pattern 不能为空");
			const dir = path.join(ctx.wikiDir, "pages");
			const hits: string[] = [];
			for (const file of fs.readdirSync(dir).sort()) {
				if (!file.endsWith(".md")) continue;
				const lines = fs.readFileSync(path.join(dir, file), "utf-8").split("\n");
				for (const [i, line] of lines.entries()) {
					if (!line.toLowerCase().includes(needle)) continue;
					const snippet = line.trim().slice(0, GREP_SNIPPET_CHARS);
					hits.push(`${file} 第 ${i + 1} 行: ${snippet}`);
					if (hits.length >= GREP_MAX_HITS) break;
				}
				if (hits.length >= GREP_MAX_HITS) break;
			}
			if (!hits.length) return text(`「${params.pattern}」在现有页面里无命中（字面匹配）。`);
			const more = hits.length >= GREP_MAX_HITS ? `\n（命中过多，只显示前 ${GREP_MAX_HITS} 行）` : "";
			return text(
				`「${params.pattern}」命中 ${hits.length} 行（行号不是章节序号；要改写请先 outline_page 取序号）：\n${hits.join("\n")}${more}`,
			);
		},
	};

	const searchWiki: AgentTool<any> = {
		name: "search_wiki",
		label: "Search wiki",
		description:
			"在 wiki 里按 pages -> sources 分层检索（BM25，弱命中时自动退语义）。用于找不到确切字面时定位相关页面；确认某个说法是否已写过，用 grep_pages 更准",
		parameters: Type.Object({
			query: Type.String({ description: "检索词" }),
		}),
		execute: async (_id, params, signal) => {
			try {
				const { stdout } = await execFileAsync(
					UV_BIN,
					["run", "subscriber", "search", params.query, "--config", ctx.configPath],
					{ cwd: ctx.root, timeout: 120_000, maxBuffer: 4 * 1024 * 1024, signal },
				);
				return text(stdout.trim() || "(无命中)");
			} catch (e) {
				// qmd 索引可能没建好；检索失败不该让整篇编译停摆，退回 grep_pages 即可
				return text(`检索不可用（${e instanceof Error ? e.message.split("\n")[0] : e}），请改用 grep_pages。`);
			}
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
				UV_BIN,
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

	const saveImage: AgentTool<any> = {
		name: "save_image",
		label: "Save Image",
		description:
			"把源里的一张图存进 wiki/imgs/ 并返回嵌入写法。只在图本身承载信息时才用（架构图、流程图、数据图表、评测结果表），装饰图、头像、封面一律不存",
		parameters: Type.Object({
			url: Type.String({ description: "源里 ## 图片 段给出的原图链接" }),
			name: Type.String({
				description: "文件名（不带扩展名）：小写英文加连字符，建议 <页面名>-<图说的是什么>",
			}),
		}),
		execute: async (_id, params, signal) => {
			if (ctx.savedImages.length >= IMAGE_BUDGET) {
				throw new Error(`本篇存图预算（${IMAGE_BUDGET} 张）已用完，用现有的图或不用图`);
			}
			const { stdout } = await execFileAsync(
				UV_BIN,
				["run", "python", "scripts/save_image.py", params.url, params.name, ctx.wikiDir],
				{ cwd: ctx.root, timeout: 60_000, signal },
			);
			const file = stdout.trim();
			if (!ctx.savedImages.includes(file)) ctx.savedImages.push(file);
			return text(`已存为 ${file}，在页面里用 ![[${file}]] 引用（写在它说明的那段正文旁边）。`);
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

	return [
		listIndex,
		grepPages,
		searchWiki,
		outlinePage,
		readPage,
		editSection,
		insertSection,
		editPage,
		writePage,
		fetchUrl,
		saveImage,
		finish,
	];
}
