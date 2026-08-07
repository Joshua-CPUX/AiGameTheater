"""提示词渲染冒烟测试：覆盖所有游戏、所有阶段的 get_prompt。

回归背景：perudo 的 get_prompt 曾漏传 history 变量，Mock 模式不调 get_prompt
所以测试没发现，真实 API 模式才暴露。此测试确保提示词模板永远可渲染。
"""
import pytest

from games.dollar_auction import game as dollar
from games.pirate_gold import game as pirate
from games.ultimatum import game as ulti
from games.perudo import game as perudo

PERSONA = {"name": "测试AI", "personality": "暴躁"}


def _two_players():
    return [{"id": "a"}, {"id": "b"}]


class TestDollarAuctionPrompt:
    def test_render(self):
        state = dollar.initial_state({"players": _two_players()})
        p = dollar.get_prompt(state, "a", PERSONA, ["第1轮 b 行动: bid 0.1"])
        assert "测试AI" in p and "最近发生的事" in p


class TestPirateGoldPrompt:
    def test_propose_phase(self):
        state = pirate.initial_state({"gold": 100, "players": _two_players()})
        p = pirate.get_prompt(state, "a", PERSONA, ["第1轮 x"])
        assert "提案" in p

    def test_vote_phase(self):
        state = pirate.initial_state({"gold": 100, "players": _two_players()})
        state, _ = pirate.apply(state, "a",
                                {"type": "propose", "allocation": {"a": 60, "b": 40}})
        p = pirate.get_prompt(state, "b", PERSONA, [])
        assert "40" in p  # b 能看到自己分到的金额


class TestUltimatumPrompt:
    def test_both_phases(self):
        state = ulti.initial_state({"pot": 100, "rounds": 2, "seed": 0,
                                    "players": _two_players()})
        p1 = ulti.get_prompt(state, "a", PERSONA, ["x"])
        assert "提议者" in p1
        state, _ = ulti.apply(state, "a", {"type": "offer", "amount": 30})
        p2 = ulti.get_prompt(state, "b", PERSONA, [])
        assert "30" in p2


class TestPerudoPrompt:
    def test_render_with_history(self):
        """回归：history 变量必须传入模板（真实模式曾因此 KeyError）。"""
        state = perudo.initial_state({"dice_per_player": 3, "seed": 1,
                                      "players": _two_players()})
        p = perudo.get_prompt(state, "a", PERSONA, ["第1轮 b 叫点 2 个 3"])
        assert "测试AI" in p and "最近发生的事" in p
        assert "还没有人叫点" in p

    def test_render_after_bid(self):
        state = perudo.initial_state({"dice_per_player": 3, "seed": 1,
                                      "players": _two_players()})
        state, _ = perudo.apply(state, "a", {"type": "bid", "quantity": 2, "face": 3})
        p = perudo.get_prompt(state, "b", PERSONA, [])
        assert "2 个 3 点" in p
        # b 的提示词里绝不能出现 a 的骰子（信息隔离抽检）
        a_dice = " ".join(map(str, state["dice"]["a"]))
        assert f"你的骰子：{a_dice}" not in p
