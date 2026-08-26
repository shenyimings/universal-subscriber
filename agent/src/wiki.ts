/** wiki 文件层：frontmatter 解析、pending 扫描、页面验证。
 *
 * index.md 重建仍由 Python 侧负责（subscriber wiki --reindex），
 * 这里只做 agent 循环需要的最小读写与确定性检查。
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { parse as parseYaml, stringify as stringifyYaml } from "yaml";

// 与 src/subscriber/wiki.py 的 CATEGORIES 保持同步
export const CATEGORIES = [
	"agent-engineering",
	"ai-security",
	"blockchain-security",
	"llm-systems",
	"program-analysis",
];

/** 单个页面的软上限；已有超限页面只许瘦身/拆分，不许继续膨胀。 */
export const MAX_PAGE_CHARS = 40_000;

const FRONT_RE = /^---\n([\s\S]*?)\n---\n?/;

export function parseFront(text: string): { meta: Record<string, any>; body: string } {
	const m = FRONT_RE.exec(text);
	if (!m) return { meta: {}, body: text };
	let meta: Record<string, any>;
	try {
		meta = parseYaml(m[1]) ?? {};
	} catch {
		return { meta: {}, body: text };
	}
	return { meta, body: text.slice(m[0].length) };
}

export function dumpFront(meta: Record<string, any>, body: string): string {
	return `---\n${stringifyYaml(meta).trim()}\n---\n${body}`;
}

/** 未编译的归档源，按 frontmatter date 升序（与 Python pending_sources 一致）。 */
export function pendingSources(wikiDir: string): string[] {
	const sourcesDir = path.join(wikiDir, "sources");
	if (!fs.existsSync(sourcesDir)) return [];
	const files: { date: string; file: string }[] = [];
	for (const file of walkMd(sourcesDir).sort()) {
		const { meta } = parseFront(fs.readFileSync(file, "utf-8"));
		if (meta.compiled === false) files.push({ date: String(meta.date ?? ""), file });
	}
	files.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
	return files.map((f) => f.file);
}

function walkMd(dir: string): string[] {
	const out: string[] = [];
	for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
		const p = path.join(dir, entry.name);
		if (entry.isDirectory()) out.push(...walkMd(p));
		else if (entry.name.endsWith(".md")) out.push(p);
	}
	return out;
}

export function markCompiled(sourcePath: string, pages: string[] = []): void {
	const text = fs.readFileSync(sourcePath, "utf-8");
	const { meta, body } = parseFront(text);
	meta.compiled = true;
	if (pages.length) {
		const prev = Array.isArray(meta.pages) ? meta.pages.map(String) : [];
		meta.pages = [...new Set([...prev, ...pages])].sort();
	}
	fs.writeFileSync(sourcePath, dumpFront(meta, body));
}

const SOURCE_SECTION = "## 来源";

/** 在页面末尾的「## 来源」段登记本次编译用到的归档源（代码侧维护，模型不写）。 */
export function appendSourceRef(wikiDir: string, pageFile: string, sourcePath: string): void {
	const rel = `../${path.relative(wikiDir, sourcePath).split(path.sep).join("/")}`;
	const p = path.join(wikiDir, "pages", pageFile);
	if (!fs.existsSync(p)) return;
	let text = fs.readFileSync(p, "utf-8");
	if (text.includes(rel)) return;
	const { meta } = parseFront(fs.readFileSync(sourcePath, "utf-8"));
	let line = `- [${meta.title ?? path.basename(sourcePath, ".md")}](${rel})`;
	const tail = [meta.source, meta.date].filter(Boolean).join("，");
	if (tail) line += `（${tail}）`;
	const i = text.lastIndexOf(SOURCE_SECTION);
	if (i >= 0) {
		const sect = text.slice(i + SOURCE_SECTION.length);
		const next = /^#{1,6} /m.exec(sect);
		const cut = next ? next.index : sect.length;
		text =
			text.slice(0, i + SOURCE_SECTION.length) +
			`${sect.slice(0, cut).trimEnd()}\n${line}\n` +
			sect.slice(cut);
	} else {
		text = `${text.trimEnd()}\n\n${SOURCE_SECTION}\n\n${line}\n`;
	}
	fs.writeFileSync(p, text);
}

export function appendLog(wikiDir: string, action: string, detail: string): void {
	const today = new Date().toISOString().slice(0, 10);
	fs.appendFileSync(path.join(wikiDir, "log.md"), `## [${today}] ${action} | ${detail}\n`);
}

/** 与 src/subscriber/images.py 的 IMG_DIR 保持一致。 */
export const IMG_DIR = "imgs";
export const FILE_NAME_RE = /^[a-z0-9][a-z0-9-]*\.md$/;
const WIKILINK_RE = /(!?)\[\[([^\]|]+)\]\]/g;
const TAG_RE = /^[a-z0-9][a-z0-9-]*$/;
/** 图片嵌入 ![[name.ext]]：文件必须真的躺在 wiki/imgs/ 里（见 save_image）。 */
export const IMAGE_EMBED_RE = /^[a-z0-9][a-z0-9-]*\.(png|jpg|webp|gif)$/;

/** 归档源里的图片段标题，与 fetch.image_section 一致。 */
export const IMAGE_SECTION = "## 图片";

/**
 * 把源正文和它末尾的图片段拆开。
 * 正文会按 max_chars 截断，而图片段正好在末尾——不先摘出来，模型就永远看不到
 * 原图链接（实测：12k 字符的源截到 6k，图片段整段消失）。
 */
export function splitImageSection(content: string): { text: string; images: string } {
	const at = content.lastIndexOf(`\n${IMAGE_SECTION}`);
	if (at < 0) return { text: content.trim(), images: "" };
	return { text: content.slice(0, at).trim(), images: content.slice(at + 1).trim() };
}

/** 页面内容的确定性检查，返回违规描述（空数组 = 通过）。 */
export function validatePage(file: string, content: string, wikiDir: string): string[] {
	const problems: string[] = [];
	if (!FILE_NAME_RE.test(file)) {
		problems.push(`${file}: 文件名必须是小写英文加连字符的 .md`);
	}
	if (!FRONT_RE.test(content)) {
		problems.push(`${file}: 缺少 YAML frontmatter`);
		return problems;
	}
	const { meta, body } = parseFront(content);
	if (!String(meta.description ?? "").trim()) {
		problems.push(`${file}: frontmatter 缺少 description`);
	}
	if (!CATEGORIES.includes(meta.category)) {
		problems.push(`${file}: category 必须是 ${CATEGORIES.join(" / ")} 之一`);
	}
	const tags = Array.isArray(meta.tags) ? meta.tags : [];
	if (tags.length < 2 || tags.length > 4 || !tags.every((t) => TAG_RE.test(String(t)))) {
		problems.push(`${file}: tags 必须是 2-4 个小写英文连字符标签`);
	}
	if (!body.trim()) {
		problems.push(`${file}: 正文为空`);
	}
	for (const m of body.matchAll(WIKILINK_RE)) {
		const target = m[2];
		if (m[1] === "!") {
			if (!IMAGE_EMBED_RE.test(target)) {
				problems.push(`${file}: 图片 ![[${target}]] 必须是小写英文连字符加 png/jpg/webp/gif`);
			} else if (!fs.existsSync(path.join(wikiDir, IMG_DIR, target))) {
				problems.push(`${file}: 图片 ![[${target}]] 不在 ${IMG_DIR}/ 里，先用 save_image 存下来`);
			}
			continue;
		}
		if (target.startsWith("#")) continue; // Obsidian 页内锚点链接
		if (!/^[a-z0-9][a-z0-9-]*$/.test(target)) {
			problems.push(`${file}: wikilink [[${target}]] 必须是小写英文连字符的页面名`);
		}
	}
	return problems;
}
