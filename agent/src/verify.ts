/** 编译结果的确定性门禁：验证本次触碰过的页面，失败时回滚到原状。
 *
 * touched 是 file -> 首次触碰前的原始内容（新页面为 ""），由工具在写盘前记录。
 * 终止是否被接受由 verifyTouched 裁决；不接受就 rollbackTouched，
 * 否则源仍是 pending 却留下半成品编辑，下一轮会在改过的页面上重复合并。
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { IMG_DIR, MAX_PAGE_CHARS, validatePage } from "./wiki.ts";

/** 触碰过的页面允许总量下降的比例：正常合并只会增长，大跌意味着内容被整页洗掉。 */
export const SHRINK_TOLERANCE = 0.05;

export function verifyTouched(wikiDir: string, touched: Map<string, string>): string[] {
	const problems: string[] = [];
	let originalTotal = 0;
	let currentTotal = 0;
	for (const [file, original] of touched) {
		const p = path.join(wikiDir, "pages", file);
		if (!fs.existsSync(p)) continue;
		const content = fs.readFileSync(p, "utf-8");
		originalTotal += original.length;
		currentTotal += content.length;
		problems.push(...validatePage(file, content, wikiDir));
		if (content.length > MAX_PAGE_CHARS && content.length > original.length) {
			problems.push(`${file}: 页面 ${content.length} 字符，超过 ${MAX_PAGE_CHARS} 上限且仍在膨胀，请拆分`);
		}
	}
	// 字符数守恒：按本次触碰的全部页面合计，拆分（内容迁到新页）因此仍能通过，
	// 而「整页重写时丢掉没读到的小节」这类静默毁内容会被挡下。
	const floor = originalTotal * (1 - SHRINK_TOLERANCE);
	if (originalTotal > 0 && currentTotal < floor) {
		problems.push(
			`本次触碰的页面总字符数从 ${originalTotal} 降到 ${currentTotal}（超过 ${
				SHRINK_TOLERANCE * 100
			}% 的下降），疑似整页重写丢失内容：合并知识只应增长，删减请说明并保持总量`,
		);
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
