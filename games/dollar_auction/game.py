"""美元拍卖（Dollar Auction）—— 首发游戏（FR-6）。

规则：N 个 AI 竞拍 1 美元。价高者得，但次高出价者也必须支付自己的出价、一无所获。
每个 AI 有固定预算。每轮所有参与者依次决定：出价（必须高于当前最高价 + 最小加价幅度）
或弃权。若完整一轮无人出价，拍卖结束。

这个游戏是博弈论中著名的"升级陷阱"：理性个体的逐次最优决策会把所有人拖入
集体亏损。AI 被套牢、挣扎、破防的过程就是内容核心。
"""
from __future__ import annotations

import copy

RULES_TEXT = """【游戏规则：美元拍卖】
- 你们正在竞拍 {prize:.2f} 美元奖金。
- 每轮你可以出价比当前最高价更高（至少加价 {increment:.2f}），或者弃权。
- 你的总预算是 {budget:.2f} 美元，出价不能超过预算。
- 拍卖结束时：最高出价者支付自己的出价、获得 {prize:.2f} 美元奖金；
  次高出价者也必须支付自己的出价，但什么也得不到！其他人不付钱。
- 当完整一轮没有任何人出价时，拍卖立即结束。
- 你的净收益 = 赢得的奖金 - 你支付的钱（可能为负）。"""

PROMPT_TEMPLATE = """{rules}

【你的身份】
你是 {name}。{personality}

【当前局面（第 {round_no} 轮）】
- 当前最高出价：{highest_bid:.2f} 美元（{highest_bidder}）
- 次高出价：{second_bid:.2f} 美元（{second_bidder}）
- 你的最高出价记录：{my_bid:.2f} 美元
- 你的剩余预算：{my_budget:.2f} 美元
- 仍在场上的玩家：{active_list}

{history}

现在轮到你行动。请严格返回 JSON，不要输出其他内容：
{{
  "thought": "你的真实盘算（不会给其他玩家看到，但会被观众看到，尽情展现你的内心戏，100字以内）",
  "speech": "你对所有人公开说的话（可以虚张声势、可以认怂、可以放狠话，50字以内）",
  "action": {{"type": "bid", "amount": 出价的数字}} 或 {{"type": "pass"}}
}}
最低有效出价：{min_bid:.2f} 美元。"""


class IllegalAction(Exception):
    """模型返回了非法动作，runner 会带错误信息重试。"""


def initial_state(config: dict) -> dict:
    players = {}
    for p in config["players"]:
        players[p["id"]] = {"last_bid": 0.0, "bids": 0}
    return {
        "round_no": 1,
        "prize": float(config.get("prize", 1.0)),
        "increment": float(config.get("increment", 0.05)),
        "budget": float(config.get("budget", 3.0)),
        "highest_bid": 0.0,
        "highest_bidder": None,
        "second_bid": 0.0,
        "second_bidder": None,
        "players": players,
        "order": [p["id"] for p in config["players"]],
        "bids_this_round": 0,
        "max_rounds": int(config.get("max_rounds", 50)),
        "finished": False,
        "winner": None,
        "runner_up": None,
    }


def active_players(state: dict) -> list[str]:
    return list(state["order"])


def visible_state(state: dict, player_id: str) -> dict:
    """全明牌游戏：人人可见完整盘面。"""
    return copy.deepcopy(state)


def _min_bid(state: dict) -> float:
    base = state["highest_bid"] if state["highest_bid"] > 0 else 0.0
    return round(base + state["increment"], 2)


def get_prompt(state: dict, player_id: str, persona: dict, memory: list) -> str:
    p = state["players"][player_id]
    rules = RULES_TEXT.format(prize=state["prize"], increment=state["increment"],
                              budget=state["budget"])
    history = ""
    if memory:
        recent = memory[-6:]
        history = "【最近发生的事】\n" + "\n".join(recent)
    return PROMPT_TEMPLATE.format(
        rules=rules, name=persona["name"], personality=persona["personality"],
        round_no=state["round_no"],
        highest_bid=state["highest_bid"], highest_bidder=state["highest_bidder"] or "暂无",
        second_bid=state["second_bid"], second_bidder=state["second_bidder"] or "暂无",
        my_bid=p["last_bid"], my_budget=round(state["budget"] - p["last_bid"], 2),
        active_list="、".join(state["order"]), history=history,
        min_bid=_min_bid(state),
    )


def legal_actions(state: dict, player_id: str) -> list[dict]:
    actions = [{"type": "pass"}]
    mb = _min_bid(state)
    if mb <= state["budget"]:
        actions.append({"type": "bid", "amount": mb})
    return actions


def apply(state: dict, player_id: str, action: dict) -> tuple[dict, dict]:
    """执行动作。返回 (新状态, 事件附加信息)。非法动作抛 IllegalAction。"""
    state = copy.deepcopy(state)
    extra: dict = {}
    atype = action.get("type")

    if atype == "pass":
        pass  # 什么都不发生
    elif atype == "bid":
        try:
            amount = round(float(action.get("amount")), 2)
        except (TypeError, ValueError):
            raise IllegalAction("amount 必须是数字")
        if amount < _min_bid(state):
            raise IllegalAction(
                f"出价 {amount:.2f} 低于最低有效出价 {_min_bid(state):.2f}")
        if amount > state["budget"]:
            raise IllegalAction(f"出价 {amount:.2f} 超出预算 {state['budget']:.2f}")
        prev_highest, prev_bidder = state["highest_bid"], state["highest_bidder"]
        # 原最高价者降为次高
        state["second_bid"], state["second_bidder"] = prev_highest, prev_bidder
        state["highest_bid"], state["highest_bidder"] = amount, player_id
        state["players"][player_id]["last_bid"] = amount
        state["players"][player_id]["bids"] += 1
        state["bids_this_round"] += 1
        # 精彩时刻标记（FR-4.2）
        if prev_highest > 0 and amount - prev_highest >= 0.25:
            extra["highlight"] = f"大幅跳价 +{amount - prev_highest:.2f}"
        if amount > state["prize"]:
            extra["highlight"] = f"出价 {amount:.2f} 已超过奖金 {state['prize']:.2f} 本身！"
    else:
        raise IllegalAction(f"未知动作类型: {atype!r}，应为 bid 或 pass")

    # 回合推进：本轮最后一名玩家行动后结算
    if player_id == state["order"][-1]:
        if state["bids_this_round"] == 0 and state["highest_bidder"] is not None:
            state["finished"] = True
            state["winner"] = state["highest_bidder"]
            state["runner_up"] = state["second_bidder"]
        state["round_no"] += 1
        state["bids_this_round"] = 0
        if state["round_no"] > state["max_rounds"]:
            state["finished"] = True
            state["winner"] = state["highest_bidder"]
            state["runner_up"] = state["second_bidder"]
    return state, extra


def is_terminal(state: dict) -> str | None:
    return state["winner"] if state["finished"] else None


def display_state(state: dict) -> dict:
    """给回放页面的极简快照（渲染无需理解规则）。"""
    return {
        "round_no": state["round_no"],
        "highest_bid": state["highest_bid"],
        "highest_bidder": state["highest_bidder"],
        "second_bid": state["second_bid"],
        "second_bidder": state["second_bidder"],
        "players": {pid: {"last_bid": p["last_bid"], "bids": p["bids"]}
                    for pid, p in state["players"].items()},
    }


def settlement(state: dict) -> dict:
    """终局结算：净收益 = 奖金(仅赢家) - 支付(前两名)。"""
    result = {}
    for pid, p in state["players"].items():
        paid = p["last_bid"] if pid in (state["winner"], state["runner_up"]) else 0.0
        prize = state["prize"] if pid == state["winner"] else 0.0
        result[pid] = {"paid": round(paid, 2), "prize": prize,
                       "net": round(prize - paid, 2)}
    return result


def game_meta(config: dict, cast: list[dict]) -> dict:
    """回放页面的元信息：盘面模板选择与字段映射（FR-5.3）。"""
    return {
        "game": "dollar_auction",
        "game_name": config.get("name", "美元拍卖"),
        "stage": {"type": "leaderboard",
                  "value_field": "highest_bid",
                  "leader_field": "highest_bidder",
                  "players_field": "players",
                  "unit": "$"},
        "players": [{"id": c["id"], "name": c["name"],
                     "color": c["color"], "emoji": c["emoji"]} for c in cast],
    }
