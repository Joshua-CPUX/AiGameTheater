"""多厂商 LLM 适配层。

统一接口：LLMClient.complete_json(system, user) -> dict
- 优先使用各厂商原生结构化输出能力（FR-2.2）
- 失败自动重试，429 指数退避
- 全程累计 token 消耗与成本（FR-2.4）
- API Key 仅从环境变量读取（FR-2.3）
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

import requests

# ---------------------------------------------------------------- JSON 提取

class LLMError(Exception):
    """调用或解析失败，调用方可重试。"""


def extract_json(text: str) -> dict:
    """从模型输出中稳健提取第一个 JSON 对象。

    容忍：markdown 代码围栏、前后多余文字、嵌套对象。
    """
    if not text:
        raise LLMError("empty response")
    # 去掉 markdown 围栏
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    for cand in candidates:
        start = cand.find("{")
        while start != -1:
            depth = 0
            for i in range(start, len(cand)):
                if cand[i] == "{":
                    depth += 1
                elif cand[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(cand[start:i + 1])
                        except json.JSONDecodeError:
                            break
            start = cand.find("{", start + 1)
    raise LLMError(f"no valid JSON in response: {text[:200]!r}")


# ---------------------------------------------------------------- 成本

# 价格表：美元 / 百万 token（2026-08 口径，可按季度更新，FR-2.4）
PRICING = {
    "gpt-5":        {"input": 5.00, "output": 30.00},
    "claude-sonnet": {"input": 3.00, "output": 15.00},
    "gemini-pro":   {"input": 2.50, "output": 15.00},
    "deepseek-chat": {"input": 0.30, "output": 0.90},
    "deepseek-ai/DeepSeek-V3.2": {"input": 0.28, "output": 0.42},  # SiliconFlow 托管价（约 ¥2/¥3 每 M）
    "grok":         {"input": 3.00, "output": 15.00},
    "kimi":         {"input": 0.60, "output": 2.50},
    "qwen-max":     {"input": 0.40, "output": 1.20},
    "default":      {"input": 3.00, "output": 15.00},
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    retries: int = 0

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1

    def cost_usd(self, model: str) -> float:
        price = PRICING.get(model, PRICING["default"])
        return (self.input_tokens * price["input"]
                + self.output_tokens * price["output"]) / 1_000_000


@dataclass
class CostTracker:
    by_model: dict = field(default_factory=dict)

    def record(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.by_model.setdefault(model, Usage()).add(input_tokens, output_tokens)

    def report(self) -> str:
        lines = ["===== 本局成本报告 ====="]
        total = 0.0
        for model, u in self.by_model.items():
            cost = u.cost_usd(model)
            total += cost
            lines.append(f"{model}: {u.calls} 次调用, "
                         f"输入 {u.input_tokens} tok / 输出 {u.output_tokens} tok, "
                         f"约 ${cost:.4f}")
        lines.append(f"合计: 约 ${total:.4f}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {m: {"calls": u.calls, "input": u.input_tokens,
                    "output": u.output_tokens, "cost_usd": round(u.cost_usd(m), 4)}
                for m, u in self.by_model.items()}


# ---------------------------------------------------------------- 客户端

class LLMClient:
    """统一 LLM 调用客户端。

    vendor: "openai_compatible" | "anthropic" | "google"
    """

    def __init__(self, vendor: str, model: str, api_key: str | None = None,
                 base_url: str | None = None, temperature: float = 0.8,
                 max_retries: int = 3, tracker: CostTracker | None = None):
        self.vendor = vendor
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries
        self.tracker = tracker or CostTracker()
        self.api_key = api_key or self._key_from_env()
        self.base_url = base_url or self._default_base_url()
        if not self.api_key:
            raise LLMError(f"缺少 API Key：请在环境变量 {self._env_key_name()} 中配置")

    # ---- 配置辅助 ----

    def _env_key_name(self) -> str:
        return {"openai_compatible": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "google": "GOOGLE_API_KEY"}[self.vendor]

    def _key_from_env(self) -> str | None:
        return os.environ.get(self._env_key_name())

    def _default_base_url(self) -> str:
        return {"openai_compatible": "https://api.openai.com/v1",
                "anthropic": "https://api.anthropic.com",
                "google": "https://generativelanguage.googleapis.com"}[self.vendor]

    # ---- 主接口 ----

    def complete_json(self, system: str, user: str) -> dict:
        """调用模型并返回解析后的 JSON 对象。失败抛出 LLMError。"""
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                text, usage = self._raw_call(system, user)
                if usage:
                    self.tracker.record(self.model, usage[0], usage[1])
                return extract_json(text)
            except LLMError as e:
                last_err = e
            except requests.RequestException as e:
                last_err = LLMError(f"network: {e}")
            self.tracker.by_model.setdefault(self.model, Usage()).retries += 1
            time.sleep(min(2 ** attempt, 8))  # 指数退避
        raise LLMError(f"模型 {self.model} 连续 {self.max_retries} 次失败: {last_err}")

    # ---- 厂商适配 ----

    def _raw_call(self, system: str, user: str) -> tuple[str, tuple[int, int] | None]:
        if self.vendor == "openai_compatible":
            return self._call_openai(system, user)
        if self.vendor == "anthropic":
            return self._call_anthropic(system, user)
        if self.vendor == "google":
            return self._call_google(system, user)
        raise LLMError(f"unknown vendor: {self.vendor}")

    def _call_openai(self, system: str, user: str) -> tuple[str, tuple[int, int] | None]:
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": self.temperature,
                # 原生结构化输出（FR-2.2）：OpenAI / DeepSeek 均支持
                "response_format": {"type": "json_object"},
            },
            timeout=120,
        )
        if resp.status_code == 429:
            raise LLMError("rate limited (429)")
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        return (data["choices"][0]["message"]["content"],
                (usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)))

    def _call_anthropic(self, system: str, user: str) -> tuple[str, tuple[int, int] | None]:
        # Anthropic 原生结构化输出：强制 tool_use
        schema = {
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
                "speech": {"type": "string"},
                "action": {"type": "object"},
            },
            "required": ["thought", "speech", "action"],
        }
        resp = requests.post(
            f"{self.base_url}/v1/messages",
            headers={"x-api-key": self.api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": self.model,
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "tools": [{"name": "respond", "description": "返回你的决策",
                           "input_schema": schema}],
                "tool_choice": {"type": "tool", "name": "respond"},
            },
            timeout=120,
        )
        if resp.status_code == 429:
            raise LLMError("rate limited (429)")
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                return json.dumps(block["input"]), (usage.get("input_tokens", 0),
                                                    usage.get("output_tokens", 0))
        raise LLMError("anthropic response missing tool_use block")

    def _call_google(self, system: str, user: str) -> tuple[str, tuple[int, int] | None]:
        resp = requests.post(
            f"{self.base_url}/v1beta/models/{self.model}:generateContent",
            params={"key": self.api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": self.temperature,
                    # 原生结构化输出（FR-2.2）
                    "responseMimeType": "application/json",
                },
            },
            timeout=120,
        )
        if resp.status_code == 429:
            raise LLMError("rate limited (429)")
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        usage = data.get("usageMetadata") or {}
        return text, (usage.get("promptTokenCount", 0),
                      usage.get("candidatesTokenCount", 0))


# ---------------------------------------------------------------- 工厂

# 人设模型名 -> (vendor, model, base_url)；base_url 为 None 时用厂商默认
MODEL_REGISTRY = {
    "gpt-5":         ("openai_compatible", "gpt-5", None),
    "claude-sonnet": ("anthropic", "claude-sonnet-4-5", None),
    "gemini-pro":    ("google", "gemini-2.5-pro", None),
    "deepseek-chat": ("openai_compatible", "deepseek-chat",
                      "https://api.deepseek.com/v1"),
    "grok":          ("openai_compatible", "grok-4", "https://api.x.ai/v1"),
    "kimi":          ("openai_compatible", "kimi-k2", "https://api.moonshot.cn/v1"),
    "qwen-max":      ("openai_compatible", "qwen-max",
                      "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    # SiliconFlow 托管的 DeepSeek-V3.2（国内可达，cheap 模式默认）
    "deepseek-sf":   ("openai_compatible", "deepseek-ai/DeepSeek-V3.2",
                      "https://api.siliconflow.cn/v1"),
}

CHEAP_MODEL = "deepseek-sf"  # --cheap 模式全员替换（FR-2.5）


def make_client(persona_model: str, cheap: bool = False,
                tracker: CostTracker | None = None) -> LLMClient:
    model_key = CHEAP_MODEL if cheap else persona_model
    if model_key not in MODEL_REGISTRY:
        raise LLMError(f"未注册的模型: {model_key}（可选：{list(MODEL_REGISTRY)}）")
    vendor, model, base_url = MODEL_REGISTRY[model_key]
    # DeepSeek 等第三方 OpenAI 兼容服务走各自的环境变量
    api_key = None
    if base_url and "deepseek" in base_url:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
    elif base_url and "siliconflow" in base_url:
        api_key = os.environ.get("SILICONFLOW_API_KEY")
    elif base_url and "x.ai" in base_url:
        api_key = os.environ.get("XAI_API_KEY")
    elif base_url and "moonshot" in base_url:
        api_key = os.environ.get("MOONSHOT_API_KEY")
    elif base_url and "dashscope" in base_url:
        api_key = os.environ.get("DASHSCOPE_API_KEY")
    return LLMClient(vendor=vendor, model=model, api_key=api_key,
                     base_url=base_url, tracker=tracker)
