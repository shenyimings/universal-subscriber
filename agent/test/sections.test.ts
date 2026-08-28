import * as assert from "node:assert/strict";
import { test } from "node:test";
import {
	formatOutline,
	insertSection,
	isReserved,
	replaceSection,
	splitSections,
} from "../src/sections.ts";

const PAGE = `---
description: 测试页
category: llm-systems
tags:
- a
- b
---
开头的一段前言。

## 背景

背景正文。

### 子标题

子标题正文，应当留在「背景」这一节里。

## 方法

方法正文。

## 来源

- [某篇源](../sources/2026/08/x.md)
`;

test("splitSections 按 H2 切分，frontmatter 之外的开头是前言，H3 不另起一节", () => {
	const secs = splitSections(PAGE);
	assert.deepEqual(
		secs.map((s) => s.title),
		["（前言）", "## 背景", "## 方法", "## 来源"],
	);
	assert.deepEqual(
		secs.map((s) => s.index),
		[1, 2, 3, 4],
	);
	assert.match(secs[0].text, /开头的一段前言/);
	assert.doesNotMatch(secs[0].text, /---/); // frontmatter 不属于任何一节
	assert.match(secs[1].text, /子标题正文/);
	assert.doesNotMatch(secs[2].text, /子标题正文/);
	// 各节首尾相接，拼回来正好是 frontmatter 之后的全部内容
	assert.equal(secs[0].start, PAGE.indexOf("开头的一段前言"));
	assert.equal(secs[secs.length - 1].end, PAGE.length);
	assert.equal(secs.map((s) => s.text).join(""), PAGE.slice(secs[0].start));
});

test("splitSections 忽略代码块里的伪标题", () => {
	const content = "前言\n\n## 真标题\n\n```md\n## 假标题\n```\n\n收尾\n";
	const secs = splitSections(content);
	assert.deepEqual(
		secs.map((s) => s.title),
		["（前言）", "## 真标题"],
	);
	assert.match(secs[1].text, /假标题/);
});

test("splitSections 处理无 frontmatter、无前言的页面", () => {
	const secs = splitSections("## 只有一节\n\n正文\n");
	assert.deepEqual(
		secs.map((s) => s.title),
		["## 只有一节"],
	);
	assert.equal(secs[0].index, 1);
});

test("formatOutline 给出序号、标题、字符数和首句", () => {
	const outline = formatOutline(PAGE);
	assert.match(outline, /1\. （前言）/);
	assert.match(outline, /2\. ## 背景/);
	assert.match(outline, /\d+ 字符/);
	assert.match(outline, /背景正文/); // 首句提示
	assert.match(outline, /4\. ## 来源.*保留段/s);
});

test("replaceSection 只改目标节，其余字节不动", () => {
	const out = replaceSection(PAGE, 2, "## 背景\n\n改写后的背景。\n\n");
	assert.match(out, /改写后的背景/);
	assert.doesNotMatch(out, /背景正文/);
	assert.match(out, /开头的一段前言/);
	assert.match(out, /方法正文/);
	assert.match(out, /^---\ndescription: 测试页/);
	// 改写后仍能被重新切分，节数不变
	assert.equal(splitSections(out).length, 4);
});

test("replaceSection 拒绝越界序号", () => {
	assert.throws(() => replaceSection(PAGE, 0, "x"), /序号/);
	assert.throws(() => replaceSection(PAGE, 9, "x"), /序号/);
});

test("insertSection 插到指定节之后", () => {
	const { content } = insertSection(PAGE, 2, "新增小节", "新增正文。");
	const titles = splitSections(content).map((s) => s.title);
	assert.deepEqual(titles, ["（前言）", "## 背景", "## 新增小节", "## 方法", "## 来源"]);
	assert.match(content, /## 新增小节\n\n新增正文。/);
	assert.match(content, /^---\ndescription/); // frontmatter 仍在最前
});

test("insertSection 不会插到保留段之后，位置被夹到保留段之前", () => {
	const { content, at } = insertSection(PAGE, 4, "新节", "正文");
	assert.equal(at, 3); // 「## 来源」是第 4 节，只能插到第 3 节后
	assert.deepEqual(
		splitSections(content).map((s) => s.title),
		["（前言）", "## 背景", "## 方法", "## 新节", "## 来源"],
	);
});

test("isReserved 认得代码维护的保留段", () => {
	assert.equal(isReserved("## 来源"), true);
	assert.equal(isReserved("## 图片"), true);
	assert.equal(isReserved("## 背景"), false);
	assert.equal(isReserved("（前言）"), false);
});

test("replaceSection 补齐节间空行，下一节标题不会贴着正文", () => {
	const out = replaceSection(PAGE, 2, "## 背景\n\n没有结尾空行的整节内容。");
	assert.match(out, /没有结尾空行的整节内容。\n\n## 方法/);
	// 末节替换只留一个换行，不在文件尾堆空行
	const tailReplaced = replaceSection(PAGE, 4, "## 来源\n\n- x");
	assert.ok(tailReplaced.endsWith("- x\n"));
});
