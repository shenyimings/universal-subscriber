/** 上下文裁剪：把总上下文压在健康区间。
 *
 * read_page / fetch_url / list_index 的结果是上下文里最大的块，且都可以随时
 * 重新调用取回，所以超预算时从最旧的开始替换成占位符；编辑类结果很小，保留。
 * 作为 pi Agent 的 transformContext 挂载，每次 LLM 调用前生效。
 */
import type { AgentMessage } from "@earendil-works/pi-agent-core";

/** 上下文字符预算；超出时裁剪旧的大块工具结果。 */
export const CONTEXT_BUDGET_CHARS = 120_000;

const PRUNABLE_TOOLS = new Set(["read_page", "fetch_url", "list_index"]);
const PRUNED_NOTE = "（该工具结果已因上下文裁剪移除；如仍需要，请重新调用该工具）";

function messageChars(m: AgentMessage): number {
	const content = (m as any).content;
	if (typeof content === "string") return content.length;
	if (Array.isArray(content)) {
		return content.reduce(
			(n: number, c: any) => n + (typeof c.text === "string" ? c.text.length : 0),
			0,
		);
	}
	return 0;
}

export function pruneContext(
	messages: AgentMessage[],
	budget: number = CONTEXT_BUDGET_CHARS,
): AgentMessage[] {
	let total = messages.reduce((n, m) => n + messageChars(m), 0);
	if (total <= budget) return messages;
	const out = [...messages];
	for (let i = 0; i < out.length && total > budget; i++) {
		const m = out[i] as any;
		if (m.role !== "toolResult" || !PRUNABLE_TOOLS.has(m.toolName)) continue;
		const size = messageChars(m);
		if (size <= PRUNED_NOTE.length) continue; // 已裁剪过或本来就小
		out[i] = { ...m, content: [{ type: "text", text: PRUNED_NOTE }] };
		total -= size - PRUNED_NOTE.length;
	}
	return out;
}
