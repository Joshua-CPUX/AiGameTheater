#!/usr/bin/env python3
"""AI 游戏剧场 · 对局启动器

用法：
    python run_game.py --game dollar_auction --mode mock       # 零成本试跑

注意：非 mock 模式需要 provider 配置，请通过 Web 控制台运行：
    python app.py
"""
import argparse

from core.runner import run_game


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 游戏剧场")
    parser.add_argument("--game", required=True, help="游戏名（games/ 下的文件夹名）")
    parser.add_argument("--mode", choices=["mock"], default="mock",
                        help="mock=假数据试跑（非 mock 模式请用 Web 控制台）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（复现用）")
    parser.add_argument("--out", default=None, help="输出目录（默认 out/）")
    args = parser.parse_args()

    summary = run_game(args.game, mode=args.mode, seed=args.seed, out_dir=args.out)
    print(f"字幕: {summary['speech_srt']} / {summary['thought_srt']}")


if __name__ == "__main__":
    main()
