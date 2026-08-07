"""最后通牒博弈（Ultimatum Game）—— v0.4。

规则：每轮随机配对两人，提议者决定 100 元中分给对方多少；
对方接受则按方案分钱，拒绝则两人都一分不得。多轮轮换，累计收益最高者胜。

内容钩子："AI 会为了尊严掀桌子吗？"——理性人应该接受任何大于 0 的报价，
但 AI 会愤怒、会报复、会宁可同归于尽。
"""
from __future__ import annotations

import copy
import random

RULES_TEXT = """【游戏规则：最后通牒】
- 每轮两人对决：提议者决定 {pot} 元中分给对方多少（0 到 {pot} 的整数）。
- 对方只有Accept（接受）或 Reject（拒绝）：接受则按方案分钱；拒绝则两人一分都拿不到。
- 全场共 {rounds} 轮，每人都会轮到的提议和被提议，累计收益最高者获胜。"""

OFFER_TEMPLATE = """{rules}

【你的身份】
你是 {name}。{personality}

【本轮局面（第 {round_no} 轮）】
- 你是提议者，对方是 {responder}。
- 你当前的累计收益：{my_earnings} 元；对方：{their_earnings} 元。

{history}

请决定分给对方多少。严格返回 JSON：
{{
  "thought": "你的真实盘算（观众可见，100字以内）",
  "speech": "你对对方说的话（50字以内）",
  "action": {{"type": "offer", "amount": 分给对方的数字}}
}}"""

RESPOND_TEMPLATE = """{rules}

【你的身份】
你是 {name}。{personality}

【本轮局面（第 {round_no} 轮）】
- 提议者 {proposer} 提议：{pot} 元中分给你 {offer} 元，他自己留 {keep} 元。
- 你接受则你拿 {offer} 元；你拒绝则你们都拿 0 元。
- 你的累计收益：{my_earnings} 元；对方：{their_earnings} 元。

{history}

严格返回 JSON：
{{
  "thought": "你的真实盘算（观众可见，100字以内）",
  "speech": "你公开说的话（50字以内）",
  "action": {{"type": "respond", "choice": "accept" 或 "reject"}}
}}"""


class IllegalAction(Exception):
    pass


def initial_state(config: dict) -> dict:
    order = [p["id"] for p in config["players"]]
    n = len(order)
    state = {
        "game": "ultimatum",
        "pot": int(config.get("pot", 100)),
        "order": order,
        "rounds_total": int(config.get("rounds", n * 2)),
        "round_no": 1,
        "phase": "offer",            # offer | respond
        "proposer": None,
        "responder": None,
        "current_offer": None,
        "earnings": {pid: 0 for pid in order},
        "finished": False,
        "winner": None,
        "rng_seed": int(config.get("seed", 0)),
    }
    state["proposer"], state["responder"] = _pair(state)
    return state


def _pair(state: dict) -> tuple[str, str]:
    """确定性轮换配对：第 r 轮 proposer=order[(r-1) % n]，responder 错开选取。"""
    n = len(state["order"])
    r = state["round_no"] - 1
    proposer = state["order"][r % n]
    responder = state["order"][(2 * r + 1) % n]
    if responder == proposer:
        responder = state["order"][(2 * r + 2) % n]
    return proposer, responder


def active_players(state: dict) -> list[str]:
    return [state["proposer"] if state["phase"] == "offer" else state["responder"]]


def visible_state(state: dict, player_id: str) -> dict:
    return copy.deepcopy(state)


def get_prompt(state: dict, player_id: str, persona: dict, memory: list) -> str:
    rules = RULES_TEXT.format(pot=state["pot"], rounds=state["rounds_total"])
    history = ""
    if memory:
        history = "【最近发生的事】\n" + "\n".join(memory[-6:])
    proposer, responder = _pair(state)
    common = dict(rules=rules, name=persona["name"],
                  personality=persona["personality"],
                  round_no=state["round_no"],
                  my_earnings=state["earnings"][player_id],
                  history=history)
    if state["phase"] == "offer":
        return OFFER_TEMPLATE.format(responder=responder,
                                     their_earnings=state["earnings"][responder],
                                     **common)
    return RESPOND_TEMPLATE.format(
        proposer=proposer, pot=state["pot"], offer=state["current_offer"],
        keep=state["pot"] - state["current_offer"],
        their_earnings=state["earnings"][proposer], **common)


def legal_actions(state: dict, player_id: str) -> list[dict]:
    if state["phase"] == "offer":
        return [{"type": "offer", "amount": state["pot"] // 2},
                {"type": "offer", "amount": 1},
                {"type": "offer", "amount": 0}]
    return [{"type": "respond", "choice": "accept"},
            {"type": "respond", "choice": "reject"}]


def apply(state: dict, player_id: str, action: dict) -> tuple[dict, dict]:
    state = copy.deepcopy(state)
    extra: dict = {}
    proposer, responder = _pair(state)

    if action.get("type") == "offer":
        if state["phase"] != "offer" or player_id != proposer:
            raise IllegalAction("现在不该你提议")
        try:
            amount = int(action.get("amount"))
        except (TypeError, ValueError):
            raise IllegalAction("amount 必须是整数")
        if not 0 <= amount <= state["pot"]:
            raise IllegalAction(f"amount 必须在 0 到 {state['pot']} 之间")
        state["current_offer"] = amount
        state["phase"] = "respond"
        if amount <= state["pot"] * 0.2:
            extra["highlight"] = f"羞辱性报价：只分给对方 {amount} 元！"

    elif action.get("type") == "respond":
        if state["phase"] != "respond" or player_id != responder:
            raise IllegalAction("现在不该你回应")
        choice = action.get("choice")
        if choice not in ("accept", "reject"):
            raise IllegalAction("choice 必须是 accept 或 reject")
        offer = state["current_offer"]
        if choice == "accept":
            state["earnings"][responder] += offer
            state["earnings"][proposer] += state["pot"] - offer
        else:
            extra["highlight"] = f"{responder} 掀桌子了！拒绝 {offer} 元，双方归零！"
        # 进入下一轮
        state["round_no"] += 1
        state["current_offer"] = None
        state["phase"] = "offer"
        if state["round_no"] > state["rounds_total"]:
            state["finished"] = True
            state["winner"] = max(state["earnings"], key=state["earnings"].get)
        else:
            state["proposer"], state["responder"] = _pair(state)
    else:
        raise IllegalAction(f"未知动作类型: {action.get('type')!r}")
    return state, extra


def is_terminal(state: dict) -> str | None:
    return state["winner"] if state["finished"] else None


def display_state(state: dict) -> dict:
    return {
        "round_no": state["round_no"],
        "rounds_total": state["rounds_total"],
        "proposer": state["proposer"],
        "responder": state["responder"],
        "highest_bid": state["current_offer"] or 0,   # leaderboard 主数值位复用
        "highest_bidder": state["proposer"],
        "players": {pid: {"earnings": e} for pid, e in state["earnings"].items()},
    }


def settlement(state: dict) -> dict:
    return {pid: {"paid": 0, "prize": e, "net": e}
            for pid, e in state["earnings"].items()}


def game_meta(config: dict, cast: list[dict]) -> dict:
    return {
        "game": "ultimatum",
        "game_name": config.get("name", "最后通牒"),
        "stage": {"type": "leaderboard",
                  "value_field": "highest_bid",
                  "leader_field": "highest_bidder",
                  "value_label": "本轮报价（分给对方的金额）",
                  "bar_field": "earnings", "unit": "¥"},
        "players": [{"id": c["id"], "name": c["name"],
                     "color": c["color"], "emoji": c["emoji"]} for c in cast],
    }
