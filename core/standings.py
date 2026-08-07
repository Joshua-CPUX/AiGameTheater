"""赛季积分榜（FR-3.3）：扫描 out/ 下全部对局日志，跨局累计各 AI 战绩。

用法：python standings.py [out目录]
产出：standings.json（视频结尾积分画面数据）+ 控制台表格。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .logger import read_events


def aggregate(out_dir: str | Path) -> list[dict]:
    """汇总所有对局，返回按胜场排序的积分榜。"""
    board: dict[str, dict] = {}
    for log_file in sorted(Path(out_dir).glob("*.jsonl")):
        try:
            events = read_events(log_file)
        except (json.JSONDecodeError, OSError):
            continue
        end = next((e for e in reversed(events) if e.get("type") == "game_end"),
                   None)
        meta = next((e for e in events if e.get("type") == "meta"), None)
        if not end or not meta:
            continue
        players = meta.get("players", [])
        for p in players:
            entry = board.setdefault(
                p["id"], {"id": p["id"], "name": p["name"],
                          "color": p.get("color"), "emoji": p.get("emoji"),
                          "games": 0, "wins": 0, "total_score": 0.0})
            entry["games"] += 1
        winner = end.get("winner")
        if winner and winner in board:
            board[winner]["wins"] += 1
        for pid, r in (end.get("settlement") or {}).items():
            if pid in board:
                board[pid]["total_score"] += r.get("net", 0)
    result = sorted(board.values(),
                    key=lambda e: (-e["wins"], -e["total_score"]))
    for rank, e in enumerate(result, 1):
        e["rank"] = rank
        e["win_rate"] = round(e["wins"] / e["games"], 3) if e["games"] else 0
        e["total_score"] = round(e["total_score"], 2)
    return result


def main() -> None:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
    board = aggregate(out_dir)
    if not board:
        print("没有找到有效对局日志")
        return
    print(f"{'名次':<4}{'AI':<12}{'场次':<6}{'胜场':<6}{'胜率':<8}{'总分':<8}")
    for e in board:
        print(f"{e['rank']:<6}{e['name']:<12}{e['games']:<8}{e['wins']:<8}"
              f"{e['win_rate']:<10.1%}{e['total_score']:<10}")
    out_file = out_dir / "standings.json"
    out_file.write_text(json.dumps(board, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n积分榜数据已写入: {out_file}")


if __name__ == "__main__":
    main()
