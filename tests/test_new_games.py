"""海盗分金 / 最后通牒 / 吹牛骰子 规则测试 + 全游戏 Mock 端到端。"""
import pytest

from core.logger import read_events
from core.runner import run_game
from games.pirate_gold import game as pirate
from games.ultimatum import game as ulti
from games.perudo import game as perudo


# ---------------------------------------------------------------- 海盗分金

@pytest.fixture
def pstate():
    return pirate.initial_state({"gold": 100, "players": [{"id": x} for x in "abcde"]})


class TestPirateGold:
    def _propose_and_vote(self, state, alloc, votes):
        proposer = state["alive"][0]
        state, _ = pirate.apply(state, proposer,
                                {"type": "propose", "allocation": alloc})
        for pid in state["alive"]:
            state, extra = pirate.apply(state, pid,
                                        {"type": "vote", "choice": votes[pid]})
        return state, extra

    def test_proposal_must_sum_to_gold(self, pstate):
        with pytest.raises(pirate.IllegalAction, match="恰好等于"):
            pirate.apply(pstate, "a",
                         {"type": "propose", "allocation": {"a": 50, "b": 40}})

    def test_cannot_allocate_to_dead(self, pstate):
        with pytest.raises(pirate.IllegalAction, match="已淘汰"):
            pirate.apply(pstate, "a",
                         {"type": "propose", "allocation": {"a": 60, "zzz": 40}})

    def test_pass_with_half_votes(self, pstate):
        # 5 人 3 票赞成 → 通过
        alloc = {"a": 97, "b": 0, "c": 1, "d": 2, "e": 0}
        votes = {"a": "yes", "b": "no", "c": "yes", "d": "yes", "e": "no"}
        state, extra = self._propose_and_vote(pstate, alloc, votes)
        assert pirate.is_terminal(state) == "a"
        assert state["allocation_final"] == alloc
        s = pirate.settlement(state)
        assert s["a"]["net"] == 97 and s["e"]["net"] == 0

    def test_rejection_feeds_sharks(self, pstate):
        alloc = {"a": 100, "b": 0, "c": 0, "d": 0, "e": 0}
        votes = {"a": "yes", "b": "no", "c": "no", "d": "no", "e": "no"}
        state, extra = self._propose_and_vote(pstate, alloc, votes)
        assert "喂鲨鱼" in extra["highlight"]
        assert "a" in state["eliminated"] and "a" not in state["alive"]
        assert state["alive"][0] == "b"  # 下一位提案
        assert pirate.is_terminal(state) is None

    def test_half_votes_pass_at_two_players(self, pstate):
        """剩 2 人时提案者自投即达半数，方案通过（半数为过的正确行为）。"""
        state = pstate
        for _ in range(3):  # 连否三轮，剩 d、e
            proposer = state["alive"][0]
            alloc = {proposer: 100, **{p: 0 for p in state["alive"] if p != proposer}}
            state, _ = pirate.apply(state, proposer,
                                    {"type": "propose", "allocation": alloc})
            for pid in list(state["alive"]):
                choice = "yes" if pid == proposer else "no"
                state, _ = pirate.apply(state, pid,
                                        {"type": "vote", "choice": choice})
        # 第 4 轮：d 提案独吞，d 赞成 e 反对 → 1/2 达半数通过
        state, _ = pirate.apply(state, "d",
                                {"type": "propose", "allocation": {"d": 100, "e": 0}})
        state, _ = pirate.apply(state, "d", {"type": "vote", "choice": "yes"})
        state, extra = pirate.apply(state, "e", {"type": "vote", "choice": "no"})
        assert pirate.is_terminal(state) == "d"
        assert state["allocation_final"] == {"d": 100, "e": 0}

    def test_last_survivor_safety_branch(self, pstate):
        """兜底分支：场上只剩 1 人时直接独吞。"""
        state = pstate
        state["alive"] = ["e"]
        state["eliminated"] = ["a", "b", "c", "d"]
        state["phase"] = "propose"
        state, _ = pirate.apply(state, "e",
                                {"type": "propose", "allocation": {"e": 100}})
        state, extra = pirate.apply(state, "e", {"type": "vote", "choice": "yes"})
        assert pirate.is_terminal(state) == "e"

    def test_double_vote_rejected(self, pstate):
        state, _ = pirate.apply(
            pstate, "a", {"type": "propose",
                          "allocation": {"a": 20, "b": 20, "c": 20, "d": 20, "e": 20}})
        state, _ = pirate.apply(state, "a", {"type": "vote", "choice": "yes"})
        with pytest.raises(pirate.IllegalAction, match="已经投过票"):
            pirate.apply(state, "a", {"type": "vote", "choice": "no"})


# ---------------------------------------------------------------- 最后通牒

@pytest.fixture
def ustate():
    return ulti.initial_state({"pot": 100, "rounds": 4, "seed": 0,
                               "players": [{"id": x} for x in "abc"]})


class TestUltimatum:
    def test_pair_rotation(self, ustate):
        assert ulti.active_players(ustate) == ["a"]  # 第 1 轮 a 提议

    def test_accept_splits(self, ustate):
        state, _ = ulti.apply(ustate, "a", {"type": "offer", "amount": 30})
        assert state["responder"] == "b"
        state, _ = ulti.apply(state, "b", {"type": "respond", "choice": "accept"})
        assert state["earnings"]["b"] == 30
        assert state["earnings"]["a"] == 70
        assert state["round_no"] == 2

    def test_reject_zeroes_both(self, ustate):
        state, _ = ulti.apply(ustate, "a", {"type": "offer", "amount": 1})
        state, extra = ulti.apply(state, "b", {"type": "respond", "choice": "reject"})
        assert "掀桌子" in extra["highlight"]
        assert state["earnings"]["a"] == 0 and state["earnings"]["b"] == 0

    def test_offer_out_of_range(self, ustate):
        with pytest.raises(ulti.IllegalAction):
            ulti.apply(ustate, "a", {"type": "offer", "amount": 101})

    def test_low_offer_highlight(self, ustate):
        _, extra = ulti.apply(ustate, "a", {"type": "offer", "amount": 10})
        assert "羞辱性报价" in extra["highlight"]

    def test_game_ends_after_rounds(self, ustate):
        state = ustate
        while not ulti.is_terminal(state):
            pid = ulti.active_players(state)[0]
            if state["phase"] == "offer":
                state, _ = ulti.apply(state, pid, {"type": "offer", "amount": 50})
            else:
                state, _ = ulti.apply(state, pid,
                                      {"type": "respond", "choice": "accept"})
        assert state["winner"] in "abc"
        total = sum(state["earnings"].values())
        assert total == 100 * 4  # 每轮分完 100，共 4 轮


# ---------------------------------------------------------------- 吹牛骰子

@pytest.fixture
def dstate():
    return perudo.initial_state({"dice_per_player": 3, "seed": 42,
                                 "players": [{"id": x} for x in "abc"]})


class TestPerudo:
    def test_hidden_information(self, dstate):
        """visible_state 必须隐藏他人骰子（FR-1.3 首个真实验证）。"""
        v = perudo.visible_state(dstate, "a")
        assert all(isinstance(d, int) for d in v["dice"]["a"])
        assert v["dice"]["b"] == ["?", "?", "?"]

    def test_bid_must_raise(self, dstate):
        state, _ = perudo.apply(dstate, "a", {"type": "bid", "quantity": 3, "face": 4})
        with pytest.raises(perudo.IllegalAction, match="加注"):
            perudo.apply(state, "b", {"type": "bid", "quantity": 2, "face": 6})
        with pytest.raises(perudo.IllegalAction, match="加注"):
            perudo.apply(state, "b", {"type": "bid", "quantity": 3, "face": 3})
        # 同数量更大点数合法
        state, _ = perudo.apply(state, "b", {"type": "bid", "quantity": 3, "face": 5})
        assert state["bid"]["face"] == 5

    def test_challenge_requires_bid(self, dstate):
        with pytest.raises(perudo.IllegalAction, match="无法质疑"):
            perudo.apply(dstate, "a", {"type": "challenge"})

    def test_challenge_resolution(self, dstate):
        # a 叫 99 个 6（必假），b 质疑必成功
        state, _ = perudo.apply(dstate, "a", {"type": "bid", "quantity": 99, "face": 6})
        state, extra = perudo.apply(state, "b", {"type": "challenge"})
        assert "质疑成功" in extra["highlight"]
        assert len(state["dice"]["a"]) == 2  # a 输一枚
        assert "reveal" in extra  # 开骰展示

    def test_ones_are_wild(self, dstate):
        state = dstate
        state["dice"] = {"a": [1, 1, 6], "b": [2, 2, 6], "c": [3, 4, 5]}
        assert perudo._count_face(state, 6) == 4   # 两个 6 + 两个野点 1
        assert perudo._count_face(state, 1) == 2   # 叫 1 点时只算真 1

    def test_out_of_turn_rejected(self, dstate):
        with pytest.raises(perudo.IllegalAction, match="没轮到你"):
            perudo.apply(dstate, "b", {"type": "bid", "quantity": 1, "face": 2})

    def test_elimination_and_winner(self):
        state = perudo.initial_state({"dice_per_player": 1, "seed": 1,
                                      "players": [{"id": x} for x in "ab"]})
        # 每人 1 骰：a 叫 2 个 6（必假），b 质疑 → a 输光淘汰
        state, _ = perudo.apply(state, "a", {"type": "bid", "quantity": 2, "face": 6})
        state, extra = perudo.apply(state, "b", {"type": "challenge"})
        assert "淘汰" in extra["highlight"]
        assert perudo.is_terminal(state) == "b"


# ---------------------------------------------------------------- 全游戏 Mock 端到端

@pytest.mark.parametrize("game_name", ["dollar_auction", "pirate_gold",
                                       "ultimatum", "perudo"])
def test_all_games_mock_e2e(tmp_path, game_name):
    """四个游戏 Mock 模式全部零成本跑通（FR-1.5 回归）。"""
    summary = run_game(game_name, mode="mock", seed=42, out_dir=tmp_path)
    assert summary["winner"]
    events = read_events(summary["log"])
    assert events[0]["type"] == "meta"
    assert events[-1]["type"] == "game_end"
    assert events[0]["stage"]["type"] in ("leaderboard", "proposal_vote")
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(events) + 1))
