"""事件日志：JSONL 写入 + SRT 字幕导出（FR-4）。

日志是对局的唯一事实来源：回放页面、字幕、积分榜全部由它生成。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class EventLogger:
    """追加式 JSONL 事件日志。每个事件自动分配全局递增 seq。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self._fh = open(self.path, "w", encoding="utf-8")

    def log(self, event: dict) -> dict:
        """写入一个事件，自动补充 seq 与 ts。返回完整事件。"""
        self._seq += 1
        event = {"seq": self._seq,
                 "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                 **event}
        self._fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._fh.flush()  # 立即落盘，中断不丢素材
        return event

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def read_events(path: str | Path) -> list[dict]:
    """读取 JSONL 日志为事件列表（按 seq 排序）。"""
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    events.sort(key=lambda e: e.get("seq", 0))
    return events


# ---------------------------------------------------------------- SRT 导出

def _srt_ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def export_srt(events: list[dict], out_prefix: str | Path,
               seconds_per_event: float = 4.0) -> tuple[Path, Path]:
    """导出双轨 SRT 字幕（FR-4.3）。

    发言轨：<prefix>_speech.srt    —— 公开发言
    内心轨：<prefix>_thought.srt   —— 内心独白（剪映中可分配不同音色/样式）

    每条字幕时长按内容长度估算，方便直接对轨自动播放的回放录屏。
    """
    speech_path = Path(f"{out_prefix}_speech.srt")
    thought_path = Path(f"{out_prefix}_thought.srt")
    cursor = 0.0
    idx = 0
    speech_blocks, thought_blocks = [], []
    for e in events:
        if e.get("type") != "action":
            continue
        player = e.get("player", "?")
        speech = (e.get("speech") or "").strip()
        thought = (e.get("thought") or "").strip()
        duration = max(2.0, seconds_per_event + 0.05 * len(speech))
        start, end = cursor, cursor + duration
        cursor = end
        idx += 1
        if speech:
            speech_blocks.append(
                f"{idx}\n{_srt_ts(start)} --> {_srt_ts(end)}\n【{player}】{speech}\n")
        if thought:
            thought_blocks.append(
                f"{idx}\n{_srt_ts(start)} --> {_srt_ts(end)}\n【{player}·内心】{thought}\n")
    speech_path.write_text("\n".join(speech_blocks), encoding="utf-8")
    thought_path.write_text("\n".join(thought_blocks), encoding="utf-8")
    return speech_path, thought_path
