/** LLM 提供方选择：config.yaml 的 llm.provider + llm.profiles，与 digest.py 的
 * llm_settings 同构（改一边记得改另一边）。没有 profiles 的旧式扁平配置原样返回。
 */
import * as os from "node:os";
import type { ThinkingLevel } from "@earendil-works/pi-agent-core";
import { type Api, createModels, type Model } from "@earendil-works/pi-ai";
import { deepseekProvider } from "@earendil-works/pi-ai/providers/deepseek";
import { zaiCodingCnProvider } from "@earendil-works/pi-ai/providers/zai-coding-cn";

export interface LlmSettings {
	provider?: string;
	base_url?: string;
	api_key_env?: string;
	model: string;
	wiki_model?: string;
	reasoning_effort?: ThinkingLevel;
}

export function llmSettings(cfg: any): LlmSettings {
	const llm = cfg.llm;
	if (!llm.profiles) return llm;
	const profile = llm.profiles[llm.provider];
	if (!profile) throw new Error(`llm.profiles 里没有 ${llm.provider}`);
	return { provider: llm.provider, ...profile };
}

/** 与 pi-ai 新版 getPiUserAgent() 一致。GLM coding-plan 的 key 限定 Pi Coding Agent
 * 使用，pi 对 zai-coding-cn 请求打上的客户端标记只有这一个头；本地锁定的
 * pi-ai 0.80.7 还不带它，所以自己补。 */
export function piUserAgent(): string {
	return `pi (${os.platform()} ${os.release()}; ${os.arch()})`;
}

// pi-ai 0.80.7 的 zai-coding-cn 目录只到 glm-5.2；glm-5.3 的参数取自 0.85.1 目录。
const GLM_53_PATCH = {
	name: "GLM-5.3",
	thinkingLevelMap: { off: null, minimal: null, low: "low", medium: null, high: "high", xhigh: null, max: "max" },
};

export function resolveModel(llm: LlmSettings): Model<Api> {
	const id = llm.wiki_model ?? llm.model;
	const provider = llm.provider ?? "deepseek";
	const models = createModels();
	let model: Model<Api> | undefined;
	if (provider === "deepseek") {
		models.setProvider(deepseekProvider());
		model = models.getModel("deepseek", id);
	} else if (provider === "zai-coding-cn") {
		models.setProvider(zaiCodingCnProvider());
		model = models.getModel("zai-coding-cn", id);
		if (!model && id === "glm-5.3") {
			const base = models.getModel("zai-coding-cn", "glm-5.2");
			if (base) {
				model = { ...base, id, ...GLM_53_PATCH, compat: { ...base.compat, maxTokensField: "max_tokens" } } as Model<Api>;
			}
		}
		if (model) model = { ...model, headers: { ...model.headers, "User-Agent": piUserAgent() } };
	} else {
		throw new Error(`不支持的 llm.provider：${provider}`);
	}
	if (!model) throw new Error(`pi-ai 不认识模型 ${provider}/${id}`);
	if (llm.base_url) model = { ...model, baseUrl: llm.base_url };
	return model;
}
