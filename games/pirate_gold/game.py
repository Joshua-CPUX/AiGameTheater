"""海盗分金（Pirate Gold）—— 第二游戏（v0.3，FR-6）。

规则：N 个海盗按资历排序瓜分 100 金币。资历最老者提出分配方案，全体海盗
（含提案者）表决；赞成票达到半数则方案通过并按方案分金，游戏结束；
否则提案者被扔进大海喂鲨鱼，由下一位资历者继续提案。最后剩下的海盗独吞全部金币。

内容钩子：博弈论经典"逆向归纳"有标准答案（1 号拿 97），但 AI 往往不按剧本走——
它们会讲人情、会报复、会赌气，理性与"人性"的冲突就是戏剧。
"""
from __future__ import annotations

import copy
import math

RULES_TEXT = """【游戏规则：海盗分金】
- 你们 {n} 个海盗按资历排序瓜分 {gold} 枚金币，当前资历顺序：{order}。
- 资历最老的在场海盗提出分配方案，然后全体在场海盗（含提案者）投票。
- 赞成票达到在场人数的一半（含一半）即通过，按方案分金币，游戏结束。
- 若被否决，提案者被扔进大海喂鲨鱼（淘汰，一分钱拿不到），由下一位继续提案。
- 每个人的目标：第一，活下去；第二，拿到尽量多的金币。"""

PROPOSE_TEMPLATE = """{rules}

【你的身份】
你是 {name}。{personality}

【当前局面】
- 你是当前提案者（资历最老的在场海盗）。
- 在场海盗（按资历）：{alive}
- 已被喂鲨鱼的：{eliminated}

{history}

请提出你的分配方案（总额必须恰好等于 {gold}，只能分给在场海盗，可以给自己 0 或全部）。
严格返回 JSON：
{{
  "thought": "你的真实盘算（观众可见，其他玩家不可见，100字以内）",
  "speech": "你的拉票演说（50字以内）",
  "action": {{"type": "propose", "allocation": {{"玩家id": 金币数, ...}}}}
}}
在场玩家 id：{alive_ids}"""

VOTE_TEMPLATE = """{rules}

【你的身份】
你是 {name}。{personality}

【当前局面】
- 提案者：{proposer}
- 分配方案：{proposal}
- 你将分到：{my_share} 枚金币
- 在场海盗：{alive}
- 已被喂鲨鱼的：{eliminated}

{history}

请投票。注意：如果否决，{proposer} 将喂鲨鱼，由下一个海盗重新提案——你也许能分到更多，也许轮到别人提案你一分没有。
严格返回 JSON：
{{
  "thought": "你的真实盘算（100字以内）",
  "speech": "你公开说的话（50字以内）",
  "action": {{"type": "vote", "choice": "yes" 或 "no"}}
}}"""


class IllegalAction(Exception):
    pass


def initial_state(config: dict) -> dict:
    order = [p["id"] for p in config["players"]]
    return {
        "game": "pirate_gold",
        "gold": int(config.get("gold", 100)),
        "order": order,               # 资历顺序（含已淘汰）
        "alive": list(order),
        "eliminated": [],
        "proposer_idx": 0,
        "phase": "propose",           # propose | vote
        "current_proposal": None,
        "votes": {},
        "round_no": 1,
        "allocation_final": None,
        "finished": False,
        "winner": None,
    }


def active_players(state: dict) -> list[str]:
    if state["phase"] == "propose":
        return [state["alive"][state["proposer_idx"]]]
    voted = set(state["votes"])
    remaining = [p for p in state["alive"] if p not in voted]
    return remaining[:1]


def visible_state(state: dict, player_id: str) -> dict:
    return copy.deepcopy(state)


def _proposer(state: dict) -> str:
    return state["alive"][state["proposer_idx"]]


def get_prompt(state: dict, player_id: str, persona: dict, memory: list) -> str:
    rules = RULES_TEXT.format(n=len(state["order"]), gold=state["gold"],
                              order=" > ".join(state["alive"]))
    history = ""
    if memory:
        history = "【最近发生的事】\n" + "\n".join(memory[-6:])
    common = dict(rules=rules, name=persona["name"],
                  personality=persona["personality"], gold=state["gold"],
                  alive="、".join(state["alive"]),
                  alive_ids="、".join(state["alive"]),
                  eliminated="、".join(state["eliminated"]) or "暂无",
                  history=history)
    if state["phase"] == "propose":
        return PROPOSE_TEMPLATE.format(**common)
    proposal = "、".join(f"{pid} {amt} 枚"
                         for pid, amt in state["current_proposal"].items())
    return VOTE_TEMPLATE.format(proposer=_proposer(state), proposal=proposal,
                                my_share=state["current_proposal"].get(player_id, 0),
                                **common)


def legal_actions(state: dict, player_id: str) -> list[dict]:
    if state["phase"] == "vote":
        return [{"type": "vote", "choice": "yes"}, {"type": "vote", "choice": "no"}]
    # 提案：均分 或 独吞（mock 用）
    n = len(state["alive"])
    equal = {pid: state["gold"] // n for pid in state["alive"]}
    equal[state["alive"][0]] += state["gold"] - sum(equal.values())
    greedy = {pid: 0 for pid in state["alive"]}
    greedy[player_id] = state["gold"]
    return [{"type": "propose", "allocation": equal},
            {"type": "propose", "allocation": greedy}]


def apply(state: dict, player_id: str, action: dict) -> tuple[dict, dict]:
    state = copy.deepcopy(state)
    extra: dict = {}

    if action.get("type") == "propose":
        if state["phase"] != "propose" or player_id != _proposer(state):
            raise IllegalAction("现在不该你提案")
        alloc = action.get("allocation")
        if not isinstance(alloc, dict):
            raise IllegalAction("allocation 必须是 {玩家id: 金币数} 的对象")
        try:
            alloc = {str(k): int(v) for k, v in alloc.items()}
        except (TypeError, ValueError):
            raise IllegalAction("金币数必须是整数")
        unknown = set(alloc) - set(state["alive"])
        if unknown:
            raise IllegalAction(f"不能分给已淘汰或不存在的玩家: {unknown}")
        if any(v < 0 for v in alloc.values()):
            raise IllegalAction("金币数不能为负")
        if sum(alloc.values()) != state["gold"]:
            raise IllegalAction(
                f"分配总额 {sum(alloc.values())} 必须恰好等于 {state['gold']}")
        state["current_proposal"] = alloc
        state["votes"] = {}
        state["phase"] = "vote"
        mine = alloc.get(player_id, 0)
        if mine >= state["gold"] * 0.9:
            extra["highlight"] = f"{player_id} 提案独吞 {mine} 枚！"

    elif action.get("type") == "vote":
        if state["phase"] != "vote":
            raise IllegalAction("当前不是投票阶段")
        if player_id in state["votes"]:
            raise IllegalAction("你已经投过票了")
        choice = action.get("choice")
        if choice not in ("yes", "no"):
            raise IllegalAction("choice 必须是 yes 或 no")
        state["votes"][player_id] = choice

        if len(state["votes"]) == len(state["alive"]):
            yes = sum(1 for v in state["votes"].values() if v == "yes")
            passed = yes * 2 >= len(state["alive"])
            proposer = _proposer(state)
            if passed:
                state["allocation_final"] = state["current_proposal"]
                state["finished"] = True
                state["winner"] = max(state["allocation_final"],
                                      key=state["allocation_final"].get)
                extra["highlight"] = f"方案通过！{yes} 票赞成"
            else:
                state["eliminated"].append(proposer)
                state["alive"].remove(proposer)
                extra["highlight"] = f"方案被否决，{proposer} 被扔进大海喂鲨鱼！"
                state["phase"] = "propose"
                state["current_proposal"] = None
                state["votes"] = {}
                state["round_no"] += 1
                if len(state["alive"]) == 1:
                    last = state["alive"][0]
                    state["allocation_final"] = {last: state["gold"]}
                    state["finished"] = True
                    state["winner"] = last
                    extra["highlight"] = f"{last} 成为最后的幸存者，独吞 {state['gold']} 枚金币！"
    else:
        raise IllegalAction(f"未知动作类型: {action.get('type')!r}")
    return state, extra


def is_terminal(state: dict) -> str | None:
    return state["winner"] if state["finished"] else None


def display_state(state: dict) -> dict:
    return {
        "round_no": state["round_no"],
        "gold": state["gold"],
        "proposer": _proposer(state) if state["alive"] else None,
        "current_proposal": state["current_proposal"],
        "votes": state["votes"],
        "alive": list(state["alive"]),
        "eliminated": list(state["eliminated"]),
        "players": {pid: {"gold": (state["current_proposal"] or {}).get(pid, 0),
                          "alive": pid in state["alive"]}
                    for pid in state["order"]},
    }


def settlement(state: dict) -> dict:
    alloc = state["allocation_final"] or {}
    return {pid: {"paid": 0, "prize": alloc.get(pid, 0),
                  "net": alloc.get(pid, 0),
                  "alive": pid in state["alive"]}
            for pid in state["order"]}


def game_meta(config: dict, cast: list[dict]) -> dict:
    return {
        "game": "pirate_gold",
        "game_name": config.get("name", "海盗分金"),
        "stage": {"type": "proposal_vote",
                  "proposal_field": "current_proposal",
                  "votes_field": "votes",
                  "proposer_field": "proposer",
                  "unit": "金"},
        "players": [{"id": c["id"], "name": c["name"],
                     "color": c["color"], "emoji": c["emoji"]} for c in cast],
    }
