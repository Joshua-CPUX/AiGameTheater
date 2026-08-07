"""吹牛骰子（Perudo / Liar's Dice）—— v0.4。第一个含隐藏信息的游戏。

规则：每人 5 枚骰子（点数互相保密，1 点是万能的野点）。玩家轮流"叫点"：
声称全场至少有 Q 个 F 点（如"3 个 4"）。下一家要么加注（数量更多，或数量
相同但点数更大），要么质疑（"吹牛！"）。质疑则全场开骰：实际数量达到叫点
则质疑者输一枚骰子，否则叫点者输一枚。骰子输光淘汰，最后幸存者获胜。

内容钩子：全程在说谎与读谎之间走钢丝——AI 手里根本没几个 4，却要面不改色
地喊"6 个 4"。隐藏信息游戏，验证 visible_state 信息过滤。
"""
from __future__ import annotations

import copy
import random

RULES_TEXT = """【游戏规则：吹牛骰子】
- 每人 {dice_n} 枚骰子，点数互相保密；1 点是野点（可当作任何点数）。
- 轮流叫点："全场至少有 Q 个 F 点"。下一家必须加注（Q 更大，或 Q 相同但 F 更大），
  或者质疑上一家"吹牛"。
- 质疑后全场开骰：统计 F 点的真实数量（1 点也算作 F）。实际数量 ≥ Q 则质疑者
  输一枚骰子，否则被质疑者输一枚。骰子输光淘汰，最后有骰子的人获胜。"""

PROMPT_TEMPLATE = """{rules}

【你的身份】
你是 {name}。{personality}

【当前局面（第 {round_no} 轮）】
- 你的骰子：{my_dice}（只有你自己看得到）
- 各家剩余骰子数：{dice_counts}（全场共 {total_dice} 枚）
- 当前叫点：{current_bid}
- 上一家：{last_bidder}

{history}

轮到你。加注的合法条件：数量 > {cur_q}，或数量 = {cur_q} 且点数 > {cur_f}。
严格返回 JSON：
{{
  "thought": "你的真实盘算（观众可见，100字以内）",
  "speech": "你公开说的话（可以虚张声势，50字以内）",
  "action": {{"type": "bid", "quantity": 数量, "face": 点数}} 或 {{"type": "challenge"}}
}}"""


class IllegalAction(Exception):
    pass


def _roll(rng: random.Random, n: int) -> list[int]:
    return sorted(rng.randint(1, 6) for _ in range(n))


def initial_state(config: dict) -> dict:
    order = [p["id"] for p in config["players"]]
    rng = random.Random(config.get("seed", 0))
    dice_n = int(config.get("dice_per_player", 5))
    return {
        "game": "perudo",
        "dice_n": dice_n,
        "seed": int(config.get("seed", 0)),
        "order": order,
        "alive": list(order),
        "dice": {pid: _roll(rng, dice_n) for pid in order},
        "turn_idx": 0,
        "round_no": 1,
        "bid": None,          # {"quantity": q, "face": f, "bidder": pid}
        "finished": False,
        "winner": None,
    }


def _rng(state: dict) -> random.Random:
    return random.Random((state["seed"], state["round_no"]).__hash__())


def active_players(state: dict) -> list[str]:
    return [state["alive"][state["turn_idx"] % len(state["alive"])]]


def visible_state(state: dict, player_id: str) -> dict:
    """信息过滤（FR-1.3）：只能看到自己的骰子。"""
    s = copy.deepcopy(state)
    for pid in s["dice"]:
        if pid != player_id:
            s["dice"][pid] = ["?"] * len(s["dice"][pid])
    return s


def _count_face(state: dict, face: int) -> int:
    total = 0
    for dice in state["dice"].values():
        for d in dice:
            if d == face or (face != 1 and d == 1):
                total += 1
    return total


def get_prompt(state: dict, player_id: str, persona: dict, memory: list) -> str:
    rules = RULES_TEXT.format(dice_n=state["dice_n"])
    history = ""
    if memory:
        history = "【最近发生的事】\n" + "\n".join(memory[-6:])
    bid = state["bid"] or {"quantity": 0, "face": 0, "bidder": None}
    counts = "、".join(f"{pid}:{len(state['dice'][pid])}枚" for pid in state["alive"])
    return PROMPT_TEMPLATE.format(
        rules=rules, name=persona["name"], personality=persona["personality"],
        round_no=state["round_no"],
        my_dice=" ".join(map(str, state["dice"][player_id])),
        dice_counts=counts,
        total_dice=sum(len(d) for d in state["dice"].values()),
        current_bid=(f"{bid['quantity']} 个 {bid['face']} 点"
                     if bid["bidder"] else "还没有人叫点"),
        last_bidder=bid["bidder"] or "—",
        cur_q=bid["quantity"], cur_f=bid["face"])


def legal_actions(state: dict, player_id: str) -> list[dict]:
    actions = []
    bid = state["bid"]
    if bid:
        actions.append({"type": "challenge"})
        actions.append({"type": "bid", "quantity": bid["quantity"] + 1,
                        "face": bid["face"]})
        if bid["face"] < 6:
            actions.append({"type": "bid", "quantity": bid["quantity"],
                            "face": bid["face"] + 1})
    else:
        actions.append({"type": "bid", "quantity": 1, "face": 2})
        actions.append({"type": "bid", "quantity": 2, "face": 3})
    return actions


def apply(state: dict, player_id: str, action: dict) -> tuple[dict, dict]:
    state = copy.deepcopy(state)
    extra: dict = {}
    current = active_players(state)[0]
    if player_id != current:
        raise IllegalAction("还没轮到你")

    if action.get("type") == "bid":
        try:
            q, f = int(action["quantity"]), int(action["face"])
        except (KeyError, TypeError, ValueError):
            raise IllegalAction("bid 需要整数 quantity 和 face")
        if not 1 <= f <= 6 or q < 1:
            raise IllegalAction("点数须在 1-6，数量须 ≥1")
        bid = state["bid"]
        if bid and not (q > bid["quantity"] or (q == bid["quantity"] and f > bid["face"])):
            raise IllegalAction(
                f"必须加注：数量 > {bid['quantity']}，或数量相同且点数 > {bid['face']}")
        state["bid"] = {"quantity": q, "face": f, "bidder": player_id}
        state["turn_idx"] = (state["turn_idx"] + 1) % len(state["alive"])

    elif action.get("type") == "challenge":
        bid = state["bid"]
        if not bid:
            raise IllegalAction("还没有人叫点，无法质疑")
        actual = _count_face(state, bid["face"])
        bidder = bid["bidder"]
        if actual >= bid["quantity"]:
            loser = player_id  # 质疑失败
            extra["highlight"] = (
                f"质疑失败！实际有 {actual} 个 {bid['face']} 点"
                f"（≥{bid['quantity']}），{loser} 失去一枚骰子")
        else:
            loser = bidder     # 质疑成功
            extra["highlight"] = (
                f"质疑成功！实际只有 {actual} 个 {bid['face']} 点"
                f"（<{bid['quantity']}），{loser} 吹牛被抓，失去一枚骰子")
        extra["reveal"] = {pid: list(d) for pid, d in state["dice"].items()}
        state["dice"][loser].pop()
        if not state["dice"][loser]:
            state["alive"].remove(loser)
            extra["highlight"] += f"；{loser} 骰子输光，淘汰！"
        if len(state["alive"]) == 1:
            state["finished"] = True
            state["winner"] = state["alive"][0]
        else:
            # 新一轮：重摇骰子，输家先叫（若已淘汰则顺位）
            rng = _rng(state)
            state["dice"] = {pid: _roll(rng, len(state["dice"][pid]))
                             for pid in state["alive"]}
            state["bid"] = None
            state["round_no"] += 1
            start = loser if loser in state["alive"] else state["alive"][0]
            state["turn_idx"] = state["alive"].index(start)
    else:
        raise IllegalAction(f"未知动作类型: {action.get('type')!r}")
    return state, extra


def is_terminal(state: dict) -> str | None:
    return state["winner"] if state["finished"] else None


def display_state(state: dict) -> dict:
    bid = state["bid"] or {"quantity": 0, "face": 0, "bidder": None}
    return {
        "round_no": state["round_no"],
        "current_quantity": bid["quantity"],
        "current_face": bid["face"],
        "current_bidder": bid["bidder"],
        "players": {pid: {"dice": len(state["dice"].get(pid, [])),
                          "alive": pid in state["alive"]}
                    for pid in state["order"]},
    }


def settlement(state: dict) -> dict:
    return {pid: {"paid": 0, "prize": len(state["dice"].get(pid, [])),
                  "net": len(state["dice"].get(pid, [])),
                  "alive": pid in state["alive"]}
            for pid in state["order"]}


def game_meta(config: dict, cast: list[dict]) -> dict:
    return {
        "game": "perudo",
        "game_name": config.get("name", "吹牛骰子"),
        "stage": {"type": "leaderboard",
                  "value_field": "current_quantity",
                  "leader_field": "current_bidder",
                  "value_label": "当前叫点（个）",
                  "bar_field": "dice", "unit": "个"},
        "players": [{"id": c["id"], "name": c["name"],
                     "color": c["color"], "emoji": c["emoji"]} for c in cast],
    }
