"""llm.py 的 JSON 提取与成本统计测试（不联网）。"""
import pytest

from core.llm import extract_json, LLMError, Usage, CostTracker


class TestExtractJson:
    def test_clean_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fence(self):
        text = '好的，这是我的决策：\n```json\n{"thought": "x", "action": {"type": "pass"}}\n```'
        assert extract_json(text)["action"]["type"] == "pass"

    def test_surrounding_text(self):
        text = '我想了想…… {"thought": "t", "speech": "s", "action": {"type": "bid", "amount": 0.1}} 就这样。'
        assert extract_json(text)["action"]["amount"] == 0.1

    def test_nested_braces(self):
        text = '{"action": {"type": "bid", "detail": {"note": "{嵌套}"}}}'
        assert extract_json(text)["action"]["detail"]["note"] == "{嵌套}"

    def test_no_json_raises(self):
        with pytest.raises(LLMError):
            extract_json("完全没有 JSON 的回复")

    def test_empty_raises(self):
        with pytest.raises(LLMError):
            extract_json("")


class TestCost:
    def test_usage_cost(self):
        u = Usage()
        u.add(1_000_000, 500_000)  # 1M 输入 + 0.5M 输出
        # deepseek-chat: $0.30/M in, $0.90/M out → 0.30 + 0.45 = 0.75
        assert u.cost_usd("deepseek-chat") == pytest.approx(0.75)

    def test_unknown_model_uses_default(self):
        u = Usage()
        u.add(1_000_000, 0)
        assert u.cost_usd("some-new-model") == pytest.approx(3.00)

    def test_tracker_report(self):
        t = CostTracker()
        t.record("gpt-5", 1000, 500)
        report = t.report()
        assert "gpt-5" in report and "合计" in report
        d = t.to_dict()
        assert d["gpt-5"]["calls"] == 1
