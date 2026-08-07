"""独牢游戏规则测试（接口契约 + 多阶段流程 + 隐藏信息过滤 + 终局分支）。"""
import pytest

from core.logger import read_events
from core.runner import run_game
from games.solitary_cell import game


@pytest.fixture
def config():
    return {"seed": 0, "n_colors": 7, "budget": 6, "max_rounds": 12,
            "players": [{"id": x} for x in "abcdefg"]}


@pytest.fixture
def state(config):
    return game.initial_state(config)


def _run_full_round(state, guess_map):
    """跑完一整轮：talk 阶段全部 pass，guess 阶段按 guess_map 猜。"""
    for _ in range(len(state["alive"])):
        pid = game.active_players(state)[0]
        state, _ = game.apply(state, pid, {"type": "pass"})
    assert state["phase"] == "guess"
    for _ in range(len(state["alive"])):
        pid = game.active_players(state)[0]
        state, _ = game.apply(state, pid, {"type": "guess", "color": guess_map[pid]})
    return state


# ---------------------------------------------------------------- 契约

class TestContract:
    def test_interface_exists(self):
        for fn in ("initial_state", "active_players", "visible_state", "get_prompt",
                   "apply", "legal_actions", "is_terminal", "display_state",
                   "settlement", "game_meta"):
            assert callable(getattr(game, fn)), f"缺少接口函数 {fn}"

    def test_initial_state(self, state):
        assert state["phase"] == "talk"
        assert len(state["alive"]) == 7
        assert state["trickster"] in state["order"]
        assert state["consecutive_safe"] == 0
        assert all(b == 6 for b in state["budget"].values())

    def test_active_players_first(self, state):
        assert game.active_players(state) == ["a"]

    def test_prompt_contains_role_and_colors(self, state):
        trick = state["trickster"]
        # 欺诈师提示词暴露自己颜色
        p = game.get_prompt(state, trick, {"name": "X", "personality": "y"}, [])
        assert state["colors"][trick] in p and "欺诈师" in p
        # 普通玩家提示词不暴露自己颜色，但暴露他人颜色
        other = next(p for p in state["order"] if p != trick)
        po = game.get_prompt(state, other, {"name": "X", "personality": "y"}, [])
        assert state["colors"][other] not in po.split("你看见的他人项圈颜色")[1].split("\n")[0]
        assert "普通囚犯" in po
        for third in state["order"]:
            if third != other:
                assert state["colors"][third] in po

    def test_display_state_shape(self, state):
        d = game.display_state(state)
        assert d["phase"] == "talk"
        assert "players" in d and "trickster" in d
        assert d["players"]["a"]["color_hex"]


# ---------------------------------------------------------------- 隐藏信息过滤

class TestVisibility:
    def test_own_color_hidden_for_normal(self, state):
        trick = state["trickster"]
        other = next(p for p in state["order"] if p != trick)
        v = game.visible_state(state, other)
        assert v["colors"][other] == "？"
        assert "trickster" not in v              # 身份对所有人隐藏
        for third in state["order"]:
            if third != other:
                assert v["colors"][third] == state["colors"][third]

    def test_trickster_sees_own_color(self, state):
        trick = state["trickster"]
        v = game.visible_state(state, trick)
        assert v["colors"][trick] == state["colors"][trick]

    def test_private_log_only_visible_to_ends(self, state):
        s = state
        s, _ = game.apply(s, "a", {"type": "broadcast", "message": "公开hi"})
        s, _ = game.apply(s, "b", {"type": "private_chat", "target": "c", "message": "悄悄话"})
        v_c = game.visible_state(s, "c")
        v_d = game.visible_state(s, "d")
        assert any(m["text"] == "悄悄话" for m in v_c["private_log"])
        assert all(m["text"] != "悄悄话" for m in v_d["private_log"])
        # 公开喊话所有人可见
        assert any(m["text"] == "公开hi" for m in v_d["public_log"])


# ---------------------------------------------------------------- 沟通阶段规则

class TestTalk:
    def test_out_of_turn_rejected(self, state):
        with pytest.raises(game.IllegalAction, match="还没轮到你"):
            game.apply(state, "b", {"type": "pass"})

    def test_broadcast_costs_two(self, state):
        state["budget"]["a"] = 1
        with pytest.raises(game.IllegalAction, match="沟通机会不足"):
            game.apply(state, "a", {"type": "broadcast", "message": "x"})
        s, _ = game.apply(state, "a",
                          {"type": "private_chat", "target": "b", "message": "y"})
        assert s["budget"]["a"] == 0

    def test_broadcast_empty_rejected(self, state):
        with pytest.raises(game.IllegalAction, match="不能为空"):
            game.apply(state, "a", {"type": "broadcast", "message": "  "})

    def test_private_chat_invalid_target(self, state):
        with pytest.raises(game.IllegalAction, match="私聊对象无效"):
            game.apply(state, "a", {"type": "private_chat", "target": "a", "message": "x"})
        with pytest.raises(game.IllegalAction, match="私聊对象无效"):
            game.apply(state, "a", {"type": "private_chat", "target": "zzz", "message": "x"})

    def test_talk_advances_to_guess(self, state):
        s = state
        for pid in "abcdefg":
            assert game.active_players(s)[0] == pid
            s, _ = game.apply(s, pid, {"type": "pass"})
        assert s["phase"] == "guess"
        assert s["turn_idx"] == 0

    def test_case_insensitive_target(self, state):
        # 模型可能输出大写 id，应能解析
        s, _ = game.apply(state, "a",
                          {"type": "private_chat", "target": "B", "message": "hi"})
        assert s["private_log"][-1]["to"] == "b"


# ---------------------------------------------------------------- 猜测阶段与结算

class TestGuess:
    def _to_guess(self, state):
        for _ in range(len(state["alive"])):
            pid = game.active_players(state)[0]
            state, _ = game.apply(state, pid, {"type": "pass"})
        return state

    def test_invalid_color_rejected(self, state):
        s = self._to_guess(state)
        with pytest.raises(game.IllegalAction, match="颜色无效"):
            game.apply(s, "a", {"type": "guess", "color": "粉色"})

    def test_guess_phase_only_guess(self, state):
        s = self._to_guess(state)
        with pytest.raises(game.IllegalAction, match="应为 guess"):
            game.apply(s, "a", {"type": "pass"})

    def test_double_guess_rejected(self, state):
        s = self._to_guess(state)
        s, _ = game.apply(s, "a", {"type": "guess", "color": s["color_names"][0]})
        # a 猜过后轮次推进到 b；a 再次行动属于越权（轮次机制阻止重复猜测）
        assert game.active_players(s)[0] == "b"
        with pytest.raises(game.IllegalAction, match="还没轮到你"):
            game.apply(s, "a", {"type": "guess", "color": s["color_names"][0]})

    def test_already_guessed_defensive_guard(self, state):
        # 构造异常状态：a 仍是当前玩家却已在 guesses 中，验证防御守卫
        s = self._to_guess(state)
        s["guesses"]["a"] = s["color_names"][0]
        with pytest.raises(game.IllegalAction, match="已经猜过"):
            game.apply(s, "a", {"type": "guess", "color": s["color_names"][0]})

    def test_wrong_guess_eliminates_and_resets_streak(self, state):
        # 先两轮全员猜对
        s = state
        for _ in range(2):
            s = _run_full_round(s, {pid: s["colors"][pid] for pid in s["alive"]})
        assert s["consecutive_safe"] == 2
        # 第三轮让首位猜错
        guess = {pid: s["colors"][pid] for pid in s["alive"]}
        victim = s["alive"][0]
        victim_color = s["colors"][victim]
        guess[victim] = next(c for c in s["color_names"] if c != guess[victim])
        s, extra = _run_full_round_return_extra(s, guess)
        assert victim in s["eliminated"]
        assert s["consecutive_safe"] == 0
        assert extra["highlight"]
        assert extra["reveal"][victim] == victim_color

    def test_perfect_ending_after_four_safe_rounds(self, state):
        s = state
        for _ in range(4):
            if game.is_terminal(s):
                break
            s = _run_full_round(s, {pid: s["colors"][pid] for pid in s["alive"]})
        assert game.is_terminal(s) == "全员通关"
        assert s["win_reason"] == "coop"

    def test_last_survivor_wins(self):
        cfg = {"seed": 0, "players": [{"id": x} for x in "ab"]}
        s = game.initial_state(cfg)
        guess = {"a": s["colors"]["a"],
                 "b": next(c for c in s["color_names"] if c != s["colors"]["b"])}
        s = _run_full_round(s, guess)
        assert game.is_terminal(s) == "a"
        assert s["win_reason"] == "last_survivor"

    def test_mass_death_trickster_wins(self):
        cfg = {"seed": 0, "players": [{"id": x} for x in "ab"]}
        s = game.initial_state(cfg)
        guess = {"a": next(c for c in s["color_names"] if c != s["colors"]["a"]),
                 "b": next(c for c in s["color_names"] if c != s["colors"]["b"])}
        s = _run_full_round(s, guess)
        assert s["win_reason"] == "mass_death"
        assert s["winner"] == s["trickster"]
        assert s["alive"] == []

    def test_trickster_timeout(self, state):
        s = game.initial_state({"seed": 0, "n_colors": 7,
                                "players": [{"id": x} for x in "abcdefg"]})
        s["max_rounds"] = 2
        s["round_no"] = 2  # 本轮结算后 round_no>=max_rounds
        guess = {pid: s["colors"][pid] for pid in s["alive"]}
        victim = s["alive"][0]
        guess[victim] = next(c for c in s["color_names"] if c != guess[victim])
        s = _run_full_round(s, guess)
        assert s["win_reason"] == "trickster_timeout"
        assert s["winner"] == s["trickster"]

    def test_trickster_legal_action_is_correct_color(self, state):
        trick = state["trickster"]
        # talk 阶段不返回 guess
        assert all(a["type"] != "guess" for a in game.legal_actions(state, trick))
        state["phase"] = "guess"
        assert game.legal_actions(state, trick) == [
            {"type": "guess", "color": state["colors"][trick]}]

    def test_settlement_coop(self, state):
        s = state
        for _ in range(4):
            if game.is_terminal(s):
                break
            s = _run_full_round(s, {pid: s["colors"][pid] for pid in s["alive"]})
        sett = game.settlement(s)
        assert sett[s["trickster"]]["net"] == -8
        for pid in s["order"]:
            if pid != s["trickster"]:
                assert sett[pid]["net"] == 8


# ---------------------------------------------------------------- 工具：捕获 extra

def _run_full_round_return_extra(state, guess_map):
    for _ in range(len(state["alive"])):
        pid = game.active_players(state)[0]
        state, _ = game.apply(state, pid, {"type": "pass"})
    extra = {}
    for _ in range(len(state["alive"])):
        pid = game.active_players(state)[0]
        state, extra = game.apply(state, pid, {"type": "guess", "color": guess_map[pid]})
    return state, extra


# ---------------------------------------------------------------- Mock 端到端

def test_mock_e2e(tmp_path):
    summary = run_game("solitary_cell", mode="mock", seed=42, out_dir=tmp_path)
    assert summary["winner"]
    events = read_events(summary["log"])
    assert events[0]["type"] == "meta"
    assert events[0]["stage"]["type"] == "cell_grid"
    assert len(events[0]["players"]) == 7
    assert events[-1]["type"] == "game_end"
    assert "settlement" in events[-1]
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(events) + 1))
    # 每个 action 事件字段完整
    actions = [e for e in events if e["type"] == "action"]
    assert actions
    for e in actions:
        for key in ("player", "player_id", "thought", "speech", "action", "state_after"):
            assert key in e
    # 字幕生成
    assert open(summary["speech_srt"], encoding="utf-8").read()
    assert "·内心】" in open(summary["thought_srt"], encoding="utf-8").read()


def test_mock_is_deterministic_with_seed(tmp_path):
    import json
    s1 = run_game("solitary_cell", mode="mock", seed=7, out_dir=tmp_path)
    s2 = run_game("solitary_cell", mode="mock", seed=7, out_dir=tmp_path)
    e1 = [(e["type"], e.get("player_id"), json.dumps(e.get("action"), sort_keys=True))
          for e in read_events(s1["log"]) if e["type"] == "action"]
    e2 = [(e["type"], e.get("player_id"), json.dumps(e.get("action"), sort_keys=True))
          for e in read_events(s2["log"]) if e["type"] == "action"]
    assert e1 == e2
