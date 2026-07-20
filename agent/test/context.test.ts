import * as assert from "node:assert/strict";
import { test } from "node:test";
import { pruneContext } from "../src/context.ts";

function toolResult(toolName: string, textLen: number): any {
	return {
		role: "toolResult",
		toolCallId: "id",
		toolName,
		content: [{ type: "text", text: "字".repeat(textLen) }],
		isError: false,
		timestamp: 0,
	};
}

test("预算内不动", () => {
	const messages = [toolResult("read_page", 100)];
	assert.equal(pruneContext(messages, 1000), messages);
});

test("超预算时裁剪最旧的大块工具结果，保留编辑结果", () => {
	const messages: any[] = [
		toolResult("read_page", 5000), // 最旧，应先被裁
		toolResult("edit_page", 5000), // 不可裁剪
		toolResult("read_page", 3000),
	];
	const out: any[] = pruneContext(messages, 9000);
	assert.match(out[0].content[0].text, /已因上下文裁剪移除/);
	assert.equal(out[1].content[0].text.length, 5000);
	assert.equal(out[2].content[0].text.length, 3000);
	// 原数组不被修改
	assert.equal(messages[0].content[0].text.length, 5000);
});

test("逐个裁剪直到回到预算内", () => {
	const messages: any[] = [
		toolResult("fetch_url", 6000),
		toolResult("read_page", 6000),
		toolResult("read_page", 2000),
	];
	const out: any[] = pruneContext(messages, 4000);
	assert.match(out[0].content[0].text, /已因上下文裁剪移除/);
	assert.match(out[1].content[0].text, /已因上下文裁剪移除/);
	assert.equal(out[2].content[0].text.length, 2000);
});
