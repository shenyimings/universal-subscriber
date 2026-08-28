/** 页面的章节视图：把「字符偏移寻址」换成「语义单元寻址」。
 *
 * 按字符 offset 跳读 + 按 old_string 锚点写入有两个结构性毛病：安全的锚点随页面
 * 变大而变少，最省事的锚点永远是页尾那个唯一的「## 来源」，页面于是退化成按时间
 * 堆叠的日志；而 old_string 又依赖上一轮 read_page 的原文，一旦被上下文裁剪掉就
 * 只能重读。改成「第 N 节」寻址后两者都消失：定位与页面大小无关，也不需要原文。
 *
 * 切分粒度是 H2：H3 留在所属 H2 里，frontmatter 不属于任何一节。
 */

const FRONT_RE = /^---\n[\s\S]*?\n---\n?/;
const H2_RE = /^## .*$/gm;
const FENCE_RE = /^(?:```|~~~)/;

/** 代码维护、模型不许写的段落（见 appendSourceRef 与 fetch.image_section）。 */
export const RESERVED_TITLES = ["## 来源", "## 图片"];
export const INTRO_TITLE = "（前言）";

export interface Section {
	/** 1-based，outline 里给模型看的就是它 */
	index: number;
	/** 标题行原文，前言为 INTRO_TITLE */
	title: string;
	/** 含标题行的整节原文，首尾相接可拼回页面 */
	text: string;
	start: number;
	end: number;
}

export function isReserved(title: string): boolean {
	return RESERVED_TITLES.includes(title.trim());
}

/** 落在代码块里的 `## ` 是伪标题，切在那儿会把 fence 劈成两半。 */
function fencedRanges(content: string): [number, number][] {
	const out: [number, number][] = [];
	let open = -1;
	let at = 0;
	for (const line of content.split("\n")) {
		if (FENCE_RE.test(line)) {
			if (open < 0) open = at;
			else {
				out.push([open, at + line.length]);
				open = -1;
			}
		}
		at += line.length + 1;
	}
	if (open >= 0) out.push([open, content.length]);
	return out;
}

export function splitSections(content: string): Section[] {
	const front = FRONT_RE.exec(content);
	const bodyStart = front ? front[0].length : 0;
	const fenced = fencedRanges(content);
	const heads: { at: number; title: string }[] = [];
	for (const m of content.matchAll(H2_RE)) {
		const at = m.index;
		if (at < bodyStart) continue;
		if (fenced.some(([a, b]) => at >= a && at < b)) continue;
		heads.push({ at, title: m[0].trim() });
	}

	const secs: Section[] = [];
	const push = (title: string, start: number, end: number) => {
		secs.push({ index: secs.length + 1, title, text: content.slice(start, end), start, end });
	};
	const introEnd = heads.length ? heads[0].at : content.length;
	if (content.slice(bodyStart, introEnd).trim()) push(INTRO_TITLE, bodyStart, introEnd);
	for (const [i, h] of heads.entries()) {
		push(h.title, h.at, i + 1 < heads.length ? heads[i + 1].at : content.length);
	}
	return secs;
}

function firstSentence(text: string, title: string): string {
	for (const line of text.split("\n")) {
		const s = line.trim();
		if (!s || s === title || s.startsWith("#")) continue;
		return s.length > 60 ? `${s.slice(0, 60)}…` : s;
	}
	return "";
}

/** 给模型的页面地图：序号 + 标题 + 字符数 + 首句。比读整页便宜两个数量级。 */
export function formatOutline(content: string): string {
	const secs = splitSections(content);
	if (!secs.length) return "（页面没有正文章节）";
	const lines = [`页面共 ${content.length} 字符，${secs.length} 节：`];
	for (const s of secs) {
		const flag = isReserved(s.title) ? "（保留段，代码维护，不要写入）" : "";
		const hint = firstSentence(s.text, s.title);
		lines.push(`${s.index}. ${s.title}  ${s.text.length} 字符${flag}${hint ? `  — ${hint}` : ""}`);
	}
	return lines.join("\n");
}

function pick(content: string, index: number): Section {
	const secs = splitSections(content);
	const s = secs.find((x) => x.index === index);
	if (!s) throw new Error(`没有第 ${index} 节：本页共 ${secs.length} 节，请先用 outline_page 核对序号`);
	return s;
}

export function getSection(content: string, index: number): Section {
	return pick(content, index);
}

/**
 * 用 newText 整体替换第 index 节（newText 含标题行），其余字节原样不动。
 * 模型给的整节内容常常不带结尾空行，直接拼回去会让下一节的 `## 标题` 贴着正文，
 * 所以这里补齐节间的空行。
 */
export function replaceSection(content: string, index: number, newText: string): string {
	const s = pick(content, index);
	const last = s.end >= content.length;
	const body = newText.replace(/\s*$/, "");
	return content.slice(0, s.start) + body + (last ? "\n" : "\n\n") + content.slice(s.end);
}

/**
 * 在第 after 节之后插入一个新 H2 节。
 * 保留段必须留在页尾，插入位置会被夹到它们之前，返回实际落点。
 */
export function insertSection(
	content: string,
	after: number,
	title: string,
	body: string,
): { content: string; at: number } {
	const secs = splitSections(content);
	if (!secs.length) {
		const front = FRONT_RE.exec(content);
		const at = front ? front[0].length : 0;
		return {
			content: `${content.slice(0, at)}## ${title}\n\n${body.trim()}\n${content.slice(at)}`,
			at: 0,
		};
	}
	const firstReserved = secs.find((s) => isReserved(s.title));
	const maxAfter = firstReserved ? firstReserved.index - 1 : secs.length;
	const at = Math.max(1, Math.min(after, maxAfter));
	const anchor = secs.find((s) => s.index === at) ?? secs[0];
	const block = `## ${title}\n\n${body.trim()}\n\n`;
	const head = content.slice(0, anchor.end).replace(/\s*$/, "\n\n");
	return { content: head + block + content.slice(anchor.end), at };
}
