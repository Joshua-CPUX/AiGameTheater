"""多厂商 LLM 适配层（协议驱动，不含任何硬编码厂商信息）。

统一接口：LLMClient.complete_json(system, user) -> dict
- 优先使用各厂商原生结构化输出能力（FR-2.2）
- 失败自动重试，429 指数退避
- 全程累计 token 消耗与成本（FR-2.4）
- API Key / base_url / model 全部由调用方传入，本模块只负责协议适配和调用
"""
from __future__ import annotations

import json
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

DEFAULT_PRICING = {"input": 3.00, "output": 15.00}  # 美元 / 百万 token


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    retries: int = 0
    pricing: dict = field(default_factory=lambda: dict(DEFAULT_PRICING))

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1

    def cost_usd(self) -> float:
        return (self.input_tokens * self.pricing.get("input", 3.0)
                + self.output_tokens * self.pricing.get("output", 15.0)) / 1_000_000


@dataclass
class CostTracker:
    by_model: dict = field(default_factory=dict)

    def record(self, model: str, input_tokens: int, output_tokens: int,
               pricing: dict | None = None) -> None:
        u = self.by_model.setdefault(model, Usage())
        if pricing:
            u.pricing = pricing
        u.add(input_tokens, output_tokens)

    def report(self) -> str:
        lines = ["===== 本局成本报告 ====="]
        total = 0.0
        for model, u in self.by_model.items():
            cost = u.cost_usd()
            total += cost
            lines.append(f"{model}: {u.calls} 次调用, "
                         f"输入 {u.input_tokens} tok / 输出 {u.output_tokens} tok, "
                         f"约 ${cost:.4f}")
        lines.append(f"合计: 约 ${total:.4f}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {m: {"calls": u.calls, "input": u.input_tokens,
                    "output": u.output_tokens, "cost_usd": round(u.cost_usd(), 4)}
                for m, u in self.by_model.items()}


# ---------------------------------------------------------------- 客户端

# 协议 -> 默认完整端点 URL（仅当用户未填 base_url 时兜底）
# 用户填写的 base_url 应为完整 API 端点地址，程序不再拼接路径
_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "google": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
}


class LLMClient:
    """统一 LLM 调用客户端。

    所有连接信息由 provider 配置传入，本类只做协议适配 + HTTP 调用。
    protocol: "openai" | "anthropic" | "google"
    """

    def __init__(self, protocol: str, model: str, api_key: str,
                 base_url: str | None = None, temperature: float = 0.8,
                 max_retries: int = 3, tracker: CostTracker | None = None,
                 pricing: dict | None = None):
        self.protocol = protocol
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_retries = max_retries
        self.tracker = tracker or CostTracker()
        self.pricing = pricing or dict(DEFAULT_PRICING)
        self.base_url = base_url or _DEFAULT_BASE_URLS.get(protocol, "")
        if not self.api_key:
            raise LLMError(f"缺少 API Key（protocol={protocol}, model={model}）")
        if not self.base_url:
            raise LLMError(f"缺少 base_url（protocol={protocol}）")

    # ---- 主接口 ----

    def complete_json(self, system: str, user: str) -> dict:
        """调用模型并返回解析后的 JSON 对象。失败抛出 LLMError。"""
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                text, usage = self._raw_call(system, user)
                if usage:
                    self.tracker.record(self.model, usage[0], usage[1], self.pricing)
                return extract_json(text)
            except LLMError as e:
                last_err = e
            except requests.RequestException as e:
                last_err = LLMError(f"network: {e}")
            self.tracker.by_model.setdefault(self.model, Usage()).retries += 1
            time.sleep(min(2 ** attempt, 8))  # 指数退避
        raise LLMError(f"模型 {self.model} 连续 {self.max_retries} 次失败: {last_err}")

    # ---- 协议适配 ----

    def _raw_call(self, system: str, user: str) -> tuple[str, tuple[int, int] | None]:
        if self.protocol == "openai":
            return self._call_openai(system, user)
        if self.protocol == "anthropic":
            return self._call_anthropic(system, user)
        if self.protocol == "google":
            return self._call_google(system, user)
        raise LLMError(f"unknown protocol: {self.protocol}")

    def _call_openai(self, system: str, user: str) -> tuple[str, tuple[int, int] | None]:
        resp = requests.post(
            self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": self.temperature,
                # 原生结构化输出（FR-2.2）：OpenAI / DeepSeek 等兼容服务均支持
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
            self.base_url,
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
        url = self.base_url.replace("{model}", self.model)
        resp = requests.post(
            url,
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

def make_client(provider: dict, tracker: CostTracker | None = None) -> LLMClient:
    """根据用户配置的 provider 字典创建 LLMClient。

    provider 结构:
      {
        "id":       "openai-gpt5",         # 唯一标识
        "name":     "OpenAI GPT-5",        # 显示名
        "protocol": "openai",              # openai | anthropic | google
        "base_url": "https://...",         # API 地址
        "model":    "gpt-5",               # 模型 ID
        "api_key":  "sk-...",              # 密钥
        "pricing":  {"input": 5.0, "output": 30.0}  # 可选，美元/百万token
      }
    """
    return LLMClient(
        protocol=provider["protocol"],
        model=provider["model"],
        api_key=provider.get("api_key", ""),
        base_url=provider.get("base_url"),
        tracker=tracker,
        pricing=provider.get("pricing"),
    )
