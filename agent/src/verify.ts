/** 编译结果的确定性门禁：验证本次触碰过的页面，失败时回滚到原状。
 *
 * touched 是 file -> 首次触碰前的原始内容（新页面为 ""），由工具在写盘前记录。
 * 终止是否被接受由 verifyTouched 裁决；不接受就 rollbackTouched，
 * 否则源仍是 pending 却留下半成品编辑，下一轮会在改过的页面上重复合并。
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { IMG_DIR, MAX_PAGE_CHARS, validatePage } from "./wiki.ts";

export function verifyTouched(wikiDir: string, touched: Map<string, string>): string[] {
	const problems: string[] = [];
	for (const [file, original] of touched) {
		const p = path.join(wikiDir, "pages", file);
		if (!fs.existsSync(p)) continue;
		const content = fs.readFileSync(p, "utf-8");
		problems.push(...validatePage(file, content, wikiDir));
		if (content.length > MAX_PAGE_CHARS && content.length > original.length) {
			problems.push(`${file}: 页面 ${content.length} 字符，超过 ${MAX_PAGE_CHARS} 上限且仍在膨胀，请拆分`);
		}
	}
	return problems;
}

export function rollbackTouched(
	wikiDir: string,
	touched: Map<string, string>,
	savedImages: string[] = [],
): void {
	for (const file of savedImages) {
		const p = path.join(wikiDir, IMG_DIR, file);
		if (fs.existsSync(p)) fs.rmSync(p);
	}
	for (const [file, original] of touched) {
		const p = path.join(wikiDir, "pages", file);
		if (original === "") {
			if (fs.existsSync(p)) fs.rmSync(p); // 本次新建的页面，删除
		} else {
			fs.writeFileSync(p, original); // 恢复原内容
		}
	}
}
