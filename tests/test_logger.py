"""事件日志与字幕导出测试（FR-4）。"""
import json

from core.logger import EventLogger, read_events, export_srt


class TestEventLogger:
    def test_seq_increments(self, tmp_path):
        with EventLogger(tmp_path / "g.jsonl") as log:
            e1 = log.log({"type": "meta"})
            e2 = log.log({"type": "action"})
        assert e1["seq"] == 1 and e2["seq"] == 2
        assert "ts" in e1

    def test_jsonl_roundtrip(self, tmp_path):
        p = tmp_path / "g.jsonl"
        with EventLogger(p) as log:
            log.log({"type": "meta", "game": "x"})
            log.log({"type": "action", "player": "GPT", "speech": "我出 0.1"})
        events = read_events(p)
        assert len(events) == 2
        assert events[1]["speech"] == "我出 0.1"
        # 中文不被转义破坏
        raw = p.read_text(encoding="utf-8")
        assert "我出 0.1" in raw


class TestSrt:
    def _events(self):
        return [
            {"seq": 1, "type": "meta"},
            {"seq": 2, "type": "action", "player": "GPT",
             "speech": "我出 0.1。", "thought": "先试探一下。"},
            {"seq": 3, "type": "action", "player": "Grok",
             "speech": "就这？我跟。", "thought": ""},
            {"seq": 4, "type": "game_end", "winner": "gpt"},
        ]

    def test_dual_tracks(self, tmp_path):
        speech, thought = export_srt(self._events(), tmp_path / "out")
        s = speech.read_text(encoding="utf-8")
        t = thought.read_text(encoding="utf-8")
        assert "【GPT】我出 0.1。" in s
        assert "【Grok】就这？我跟。" in s
        assert "【GPT·内心】先试探一下。" in t
        # Grok 没有内心独白，不进入内心轨
        assert "Grok" not in t
        # SRT 时间轴格式
        assert "00:00:00,000 --> " in s

    def test_timing_monotonic(self, tmp_path):
        speech, _ = export_srt(self._events(), tmp_path / "out")
        lines = [l for l in speech.read_text(encoding="utf-8").splitlines()
                 if "-->" in l]
        assert len(lines) == 2
        # 第二条的开始时间 >= 第一条的结束时间
        end1 = lines[0].split(" --> ")[1]
        start2 = lines[1].split(" --> ")[0]
        assert start2 >= end1
