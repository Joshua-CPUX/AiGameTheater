"""赛季积分榜测试（FR-3.3）。"""
from core.runner import run_game
from core.standings import aggregate


def test_standings_aggregate(tmp_path):
    # 跑两局（不同游戏），验证跨局累计
    run_game("dollar_auction", mode="mock", seed=1, out_dir=tmp_path)
    run_game("ultimatum", mode="mock", seed=2, out_dir=tmp_path)

    board = aggregate(tmp_path)
    assert board, "积分榜为空"
    # 班底成员都被统计
    ids = {e["id"] for e in board}
    assert "gpt" in ids
    # 每人至少 1 场；胜场不超过场次；按胜场降序
    for e in board:
        assert e["games"] >= 1
        assert 0 <= e["wins"] <= e["games"]
        assert 0 <= e["win_rate"] <= 1
        assert "rank" in e and "name" in e and "color" in e
    wins = [e["wins"] for e in board]
    assert wins == sorted(wins, reverse=True)
    # 两局总共产生恰好 2 个胜场
    assert sum(wins) == 2


def test_standings_empty_dir(tmp_path):
    assert aggregate(tmp_path) == []
