"""Mock 模式端到端测试（FR-1.5）：零 API 成本跑完整局，验证日志与产出物。"""
import json

from core.logger import read_events
from core.runner import run_game


def test_mock_game_runs_to_completion(tmp_path):
    summary = run_game("dollar_auction", mode="mock", seed=42, out_dir=tmp_path)

    # 对局正常结束，产生了赢家
    assert summary["winner"] in {"gpt", "claude", "gemini", "deepseek",
                                 "grok", "kimi", "qwen"}

    events = read_events(summary["log"])
    # 首个事件是 meta，且带盘面模板配置（FR-5.3）
    assert events[0]["type"] == "meta"
    assert events[0]["stage"]["type"] == "leaderboard"
    assert len(events[0]["players"]) == 7
    # 末尾是 game_end，含结算（成本为零）
    assert events[-1]["type"] == "game_end"
    assert "settlement" in events[-1]
    assert events[-1]["cost"] == {}
    # seq 全局连续递增（FR-4.1）
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(events) + 1))
    # 每个 action 事件都有完整的回放所需字段
    actions = [e for e in events if e["type"] == "action"]
    assert len(actions) > 0
    for e in actions:
        for key in ("player", "player_id", "thought", "speech",
                    "action", "state_after", "color", "emoji"):
            assert key in e, f"action 事件缺少 {key}"
        assert "highest_bid" in e["state_after"]
    # 字幕文件已生成（FR-4.3）
    speech = open(summary["speech_srt"], encoding="utf-8").read()
    thought = open(summary["thought_srt"], encoding="utf-8").read()
    assert "【" in speech and "·内心】" in thought


def test_mock_is_deterministic_with_seed(tmp_path):
    s1 = run_game("dollar_auction", mode="mock", seed=7, out_dir=tmp_path)
    s2 = run_game("dollar_auction", mode="mock", seed=7, out_dir=tmp_path)
    e1 = [(e["type"], e.get("player_id"), json.dumps(e.get("action"), sort_keys=True))
          for e in read_events(s1["log"]) if e["type"] == "action"]
    e2 = [(e["type"], e.get("player_id"), json.dumps(e.get("action"), sort_keys=True))
          for e in read_events(s2["log"]) if e["type"] == "action"]
    assert e1 == e2


def test_unknown_cast_member_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="personas.yaml"):
        run_game("dollar_auction", mode="mock", out_dir=tmp_path,
                 config_override={"cast": ["gpt", "不存在的AI"]})
