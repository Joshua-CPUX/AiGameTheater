"""美元拍卖游戏规则测试（FR-1.1 接口契约 + 规则正确性）。"""
import pytest

from games.dollar_auction import game


@pytest.fixture
def config():
    return {"name": "测试局", "prize": 1.0, "increment": 0.05, "budget": 3.0,
            "max_rounds": 50,
            "players": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}


@pytest.fixture
def state(config):
    return game.initial_state(config)


class TestContract:
    """5 个接口函数齐全且基本行为正确。"""

    def test_interface_exists(self):
        for fn in ("initial_state", "active_players", "visible_state",
                   "get_prompt", "apply", "legal_actions", "is_terminal",
                   "display_state", "game_meta", "settlement"):
            assert callable(getattr(game, fn)), f"缺少接口函数 {fn}"

    def test_initial_state(self, state):
        assert state["highest_bid"] == 0.0
        assert state["finished"] is False
        assert set(state["players"]) == {"a", "b", "c"}

    def test_active_players(self, state):
        assert game.active_players(state) == ["a", "b", "c"]

    def test_visible_state_is_full(self, state):
        assert game.visible_state(state, "a") == state

    def test_prompt_contains_context(self, state):
        persona = {"name": "测试AI", "personality": "暴躁"}
        p = game.get_prompt(state, "a", persona, [])
        assert "测试AI" in p and "暴躁" in p and "0.05" in p

    def test_display_state_shape(self, state):
        d = game.display_state(state)
        assert "highest_bid" in d and "players" in d


class TestRules:
    def test_legal_bid(self, state):
        state, _ = game.apply(state, "a", {"type": "bid", "amount": 0.05})
        assert state["highest_bid"] == 0.05
        assert state["highest_bidder"] == "a"

    def test_bid_below_increment_rejected(self, state):
        state, _ = game.apply(state, "a", {"type": "bid", "amount": 0.10})
        with pytest.raises(game.IllegalAction):
            game.apply(state, "b", {"type": "bid", "amount": 0.12})  # 低于 0.10+0.05

    def test_bid_over_budget_rejected(self, state):
        with pytest.raises(game.IllegalAction):
            game.apply(state, "a", {"type": "bid", "amount": 3.50})

    def test_bid_not_number_rejected(self, state):
        with pytest.raises(game.IllegalAction):
            game.apply(state, "a", {"type": "bid", "amount": "abc"})

    def test_unknown_action_rejected(self, state):
        with pytest.raises(game.IllegalAction):
            game.apply(state, "a", {"type": "explode"})

    def test_second_bidder_tracked(self, state):
        state, _ = game.apply(state, "a", {"type": "bid", "amount": 0.10})
        state, _ = game.apply(state, "b", {"type": "bid", "amount": 0.20})
        assert state["highest_bidder"] == "b"
        assert state["second_bidder"] == "a" and state["second_bid"] == 0.10

    def test_full_pass_round_ends_game(self, state):
        state, _ = game.apply(state, "a", {"type": "bid", "amount": 0.05})
        state, _ = game.apply(state, "b", {"type": "pass"})
        state, _ = game.apply(state, "c", {"type": "pass"})  # 本轮有出价，不结束
        assert game.is_terminal(state) is None
        state, _ = game.apply(state, "a", {"type": "pass"})
        state, _ = game.apply(state, "b", {"type": "pass"})
        state, _ = game.apply(state, "c", {"type": "pass"})  # 整轮无人出价 → 结束
        assert game.is_terminal(state) == "a"

    def test_first_round_all_pass_no_winner(self, state):
        """没人出过价时整轮弃权，不应产生赢家。"""
        for pid in ("a", "b", "c"):
            state, _ = game.apply(state, pid, {"type": "pass"})
        assert game.is_terminal(state) is None  # highest_bidder 为 None，不结束

    def test_highlight_on_big_jump(self, state):
        state, extra = game.apply(state, "a", {"type": "bid", "amount": 0.10})
        state, extra = game.apply(state, "b", {"type": "bid", "amount": 0.40})
        assert extra.get("highlight")

    def test_highlight_over_prize(self, state):
        state, _ = game.apply(state, "a", {"type": "bid", "amount": 0.95})
        state, extra = game.apply(state, "b", {"type": "bid", "amount": 1.10})
        assert "已超过奖金" in extra.get("highlight", "")

    def test_settlement_top_two_pay(self, config):
        state = game.initial_state(config)
        state, _ = game.apply(state, "a", {"type": "bid", "amount": 0.80})
        state, _ = game.apply(state, "b", {"type": "bid", "amount": 1.20})
        state, _ = game.apply(state, "c", {"type": "pass"})
        for pid in ("a", "b", "c"):
            state, _ = game.apply(state, pid, {"type": "pass"})
        assert game.is_terminal(state) == "b"
        s = game.settlement(state)
        assert s["b"]["net"] == pytest.approx(1.0 - 1.20)   # 赢家：奖金 - 支付
        assert s["a"]["net"] == pytest.approx(-0.80)         # 次高：纯亏
        assert s["c"]["net"] == pytest.approx(0.0)           # 围观者：不亏

    def test_legal_actions(self, state):
        acts = game.legal_actions(state, "a")
        assert {"type": "pass"} in acts
        assert {"type": "bid", "amount": 0.05} in acts
