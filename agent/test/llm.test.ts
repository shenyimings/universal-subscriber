import * as assert from "node:assert/strict";
import { test } from "node:test";
import { llmSettings, resolveModel } from "../src/llm.ts";

test("llmSettings: flat config passes through, profiles flatten", () => {
	const flat = { model: "deepseek-v4-flash" };
	assert.equal(llmSettings({ llm: flat }), flat);
	const cfg = { llm: { provider: "b", profiles: { a: { model: "x" }, b: { model: "y" } } } };
	assert.deepEqual(llmSettings(cfg), { provider: "b", model: "y" });
	assert.throws(() => llmSettings({ llm: { provider: "c", profiles: {} } }));
});

test("resolveModel: glm-5.3 on zai-coding-cn carries pi User-Agent", () => {
	const m = resolveModel({ provider: "zai-coding-cn", model: "glm-5.3" });
	assert.equal(m.id, "glm-5.3");
	assert.match(String(m.headers?.["User-Agent"]), /^pi \(/);
	assert.equal(m.thinkingLevelMap?.high, "high");
});

test("resolveModel: deepseek stays the default provider", () => {
	const m = resolveModel({ model: "deepseek-v4-flash" });
	assert.equal(m.provider, "deepseek");
	assert.equal(m.headers?.["User-Agent"], undefined);
});
