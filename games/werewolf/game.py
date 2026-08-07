"""狼人杀（Werewolf）—— 8人标准板（3狼1预1女1猎2民）。

夜晚：狼人击杀 → 预言家查验 → 女巫救/毒
白天：公布死讯 → (猎人开枪) → 依次发言 → 投票出局 → (猎人开枪)
胜利：狼人>=好人 → 狼人胜；狼人全灭 → 好人胜
"""
from __future__ import annotations

import copy
import random
from collections import Counter

RULES_TEXT = """【游戏规则：狼人杀 · 8人预女猎板】
- 8 名玩家分为两个阵营：狼人阵营（3狼）和好人阵营（1预言家、1女巫、1猎人、2平民）。
- 游戏分为夜晚和白天交替进行。

【夜晚阶段】
1. 狼人睁眼，共同选择一名非狼人玩家击杀。
2. 预言家睁眼，查验一名玩家的身份（得知是狼人或好人）。
3. 女巫睁眼，得知今晚被杀的玩家，可选择：使用解药救人、使用毒药毒杀一人、或不行动。
   （解药和毒药各一瓶，每夜最多用一瓶。第一晚可以自救，之后不可。）

【白天阶段】
1. 公布昨晚死亡名单（不公布死因和角色）。
2. 若猎人被狼人杀死或被投票出局（非被毒杀），可开枪带走一名玩家。
3. 所有存活玩家依次发言，讨论谁是狼人。
4. 发言结束后全体投票，得票最多者被出局。
5. 若被投票出局者是猎人，可开枪带走一名玩家。

【胜利条件】
- 狼人胜：存活的狼人数量 >= 存活的好人数量。
- 好人胜：所有狼人被淘汰。

【特殊规则】
- 猎人被女巫毒杀时无法开枪。
- 狼人互相知道身份。"""

ROLE_INFO = {
    "werewolf": "你是狼人。每晚与其他狼人共同选择一人击杀。你知道队友是谁。白天你需要伪装身份，混入好人中。",
    "seer": "你是预言家。每晚可以查验一名玩家的身份，得知是狼人还是好人。你的查验结果是好人阵营最重要的信息来源。",
    "witch": "你是女巫。你有一瓶解药和一瓶毒药。每晚最多用一瓶。解药可以救今晚被杀的人，毒药可以毒杀一人。第一晚可以自救，之后不可。",
    "hunter": "你是猎人。当你被狼人杀死或被投票出局时，可以开枪带走一名玩家。但被女巫毒杀时无法开枪。",
    "villager": "你是平民。你没有特殊能力，但你的投票和发言是好人阵营胜利的关键。",
}

ROLE_NAMES = {
    "werewolf": "狼人", "seer": "预言家", "witch": "女巫",
    "hunter": "猎人", "villager": "平民",
}
ROLE_COLORS = {
    "werewolf": "#e53e3e", "seer": "#4299e1", "witch": "#9f7aea",
    "hunter": "#ed8936", "villager": "#48bb78",
}

# ---- 提示词模板 ----
NIGHT_WOLF_TMPL = """{rules}

【你的身份】
你是 {name}（{role_name}）。{role_info}
{personality}

【当前局面】
- 第 {round} 轮 · 夜晚
- 存活玩家：{alive}
- 已淘汰：{dead}
- 你的狼人队友：{teammates}

{history}

请选择今晚要击杀的目标（只能选非狼人的存活玩家）。
严格返回 JSON：
{{
  "thought": "你的真实想法（100字以内）",
  "speech": "（夜晚不发言）",
  "action": {{"type": "kill", "target": "玩家id"}}
}}
可选目标：{target_ids}"""

NIGHT_SEER_TMPL = """{rules}

【你的身份】
你是 {name}（{role_name}）。{role_info}
{personality}

【当前局面】
- 第 {round} 轮 · 夜晚
- 存活玩家：{alive}
- 已淘汰：{dead}

{history}

请选择今晚要查验的玩家（不能查验自己）。
严格返回 JSON：
{{
  "thought": "你的真实想法（100字以内）",
  "speech": "（夜晚不发言）",
  "action": {{"type": "check", "target": "玩家id"}}
}}
可选目标：{target_ids}"""

NIGHT_WITCH_TMPL = """{rules}

【你的身份】
你是 {name}（{role_name}）。{role_info}
{personality}

【当前局面】
- 第 {round} 轮 · 夜晚
- 存活玩家：{alive}
- 已淘汰：{dead}
- 今晚被狼人袭击的玩家：{killed}
- 解药：{antidote_status}，毒药：{poison_status}

{history}

请决定今晚的行动。{options}
严格返回 JSON：
{{
  "thought": "你的真实想法（100字以内）",
  "speech": "（夜晚不发言）",
  "action": {action_format}
}}"""

HUNTER_SHOOT_TMPL = """{rules}

【你的身份】
你是 {name}（猎人）。{role_info}
{personality}

【当前局面】
- 你已经死亡，但你可以开枪带走一名存活玩家！
- 存活玩家：{alive}

{history}

请选择要开枪带走的玩家，或选择不开枪。
严格返回 JSON：
{{
  "thought": "你的真实想法（100字以内）",
  "speech": "你的遗言（50字以内）",
  "action": {{"type": "shoot", "target": "玩家id"}} 或 {{"type": "shoot_skip"}}
}}
可选目标：{target_ids}"""

DAY_SPEAK_TMPL = """{rules}

【你的身份】
你是 {name}（{role_name}）。{role_info}
{personality}

【当前局面】
- 第 {round} 轮 · 白天讨论
- 存活玩家：{alive}
- 已淘汰：{dead}

{history}

请发言。你可以：指控疑似狼人、为自己辩护、分享信息（如预言家可报查验结果）、或伪装身份。
注意：你的发言内容需要同时放在 "speech" 和 "action.message" 两个字段中。
严格返回 JSON：
{{
  "thought": "你的真实想法（100字以内）",
  "speech": "你的公开发言（100字以内）",
  "action": {{"type": "speak", "message": "和speech相同的内容"}}
}}"""

DAY_VOTE_TMPL = """{rules}

【你的身份】
你是 {name}（{role_name}）。{role_info}
{personality}

【当前局面】
- 第 {round} 轮 · 白天投票
- 存活玩家：{alive}
- 已淘汰：{dead}

{history}

请投票选出你认为的狼人。得票最多者将被出局（平票随机选一人）。
注意：投票阶段不再发言，你的投票理由请写在 thought 中。
严格返回 JSON：
{{
  "thought": "你的投票理由和真实想法（100字以内）",
  "speech": "（投票阶段不发言）",
  "action": {{"type": "vote", "target": "玩家id"}}
}}
可选目标：{target_ids}"""


class IllegalAction(Exception):
    pass


def _assign_roles(player_ids: list[str]) -> dict[str, str]:
    """随机分配角色：3狼1预1女1猎2民。"""
    pool = ["werewolf"] * 3 + ["seer", "witch", "hunter"] + ["villager"] * 2
    random.shuffle(pool)
    return {pid: pool[i] for i, pid in enumerate(player_ids)}


def initial_state(config: dict) -> dict:
    order = [p["id"] for p in config["players"]]
    roles = _assign_roles(order)
    return {
        "game": "werewolf",
        "round_no": 1,
        "phase": "night_wolf",
        "roles": roles,
        "seer_id": next(p for p, r in roles.items() if r == "seer"),
        "witch_id": next(p for p, r in roles.items() if r == "witch"),
        "hunter_id": next(p for p, r in roles.items() if r == "hunter"),
        "wolf_ids": [p for p, r in roles.items() if r == "werewolf"],
        "order": order,
        "alive": list(order),
        "dead": [],
        "death_cause": {},
        # 夜晚
        "wolf_votes": {},
        "wolf_kill_target": None,
        "seer_checks": [],
        "witch_antidote": True,
        "witch_poison": True,
        "witch_action": None,
        "witch_poison_target": None,
        "night_deaths": [],
        # 白天
        "day_spoken": [],
        "day_speeches": {},
        "day_votes": {},
        "vote_result": None,
        # 猎人
        "hunter_shoot_pending": False,
        "hunter_context": None,   # "night" | "vote"
        # 公共日志
        "public_log": [],
        # 结束
        "winner": None,
        "finished": False,
    }


def active_players(state: dict) -> list[str]:
    ph = state["phase"]
    if ph == "night_wolf":
        return [w for w in state["wolf_ids"] if w in state["alive"]]
    if ph == "night_seer":
        return [state["seer_id"]] if state["seer_id"] in state["alive"] else []
    if ph == "night_witch":
        wid = state["witch_id"]
        if wid in state["alive"] and (state["witch_antidote"] or state["witch_poison"]):
            return [wid]
        return []
    if ph == "hunter_shoot":
        return [state["hunter_id"]] if state["hunter_shoot_pending"] else []
    if ph == "day_speak":
        return [p for p in state["alive"] if p not in state["day_spoken"]]
    if ph == "day_vote":
        return [p for p in state["alive"] if p not in state["day_votes"]]
    return []


def visible_state(state: dict, player_id: str) -> dict:
    return copy.deepcopy(state)


# ---- 提示词构建 ----
def _build_history(state: dict, player_id: str) -> str:
    role = state["roles"][player_id]
    lines = []
    if state["public_log"]:
        lines.append("【公开信息】")
        for e in state["public_log"][-12:]:
            lines.append(f"  {e}")
    if role == "werewolf":
        tm = [w for w in state["wolf_ids"] if w != player_id]
        if tm:
            lines.append(f"\n【狼人队友】{', '.join(tm)}")
    if role == "seer" and state["seer_checks"]:
        lines.append("\n【你的查验记录】")
        for c in state["seer_checks"]:
            r = "狼人" if c["result"] == "werewolf" else "好人"
            lines.append(f"  {c['target']} → {r}")
    if role == "witch":
        lines.append(f"\n【药水】解药: {'有' if state['witch_antidote'] else '无'}, "
                     f"毒药: {'有' if state['witch_poison'] else '无'}")
    if state["day_speeches"]:
        lines.append("\n【今日发言】")
        for pid, sp in state["day_speeches"].items():
            lines.append(f"  {pid}: {sp}")
    if state["day_votes"]:
        lines.append("\n【已投票】")
        for pid, tgt in state["day_votes"].items():
            lines.append(f"  {pid} → {tgt}")
    return "\n".join(lines) if lines else ""


def get_prompt(state: dict, player_id: str, persona: dict, memory: list) -> str:
    role = state["roles"][player_id]
    ph = state["phase"]
    common = dict(
        rules=RULES_TEXT,
        name=persona["name"],
        personality=persona.get("personality", ""),
        role_name=ROLE_NAMES[role],
        role_info=ROLE_INFO[role],
        alive="、".join(state["alive"]),
        dead="、".join(state["dead"]) or "暂无",
        round=state["round_no"],
        history=_build_history(state, player_id),
    )
    if ph == "night_wolf":
        tm = [w for w in state["wolf_ids"] if w != player_id and w in state["alive"]]
        targets = [p for p in state["alive"] if p not in state["wolf_ids"]]
        return NIGHT_WOLF_TMPL.format(teammates="、".join(tm) or "无（你是唯一存活的狼人）",
                                       target_ids="、".join(targets), **common)
    if ph == "night_seer":
        targets = [p for p in state["alive"] if p != player_id]
        return NIGHT_SEER_TMPL.format(target_ids="、".join(targets), **common)
    if ph == "night_witch":
        killed = state.get("wolf_kill_target") or "无人被杀"
        opts = []
        af = None
        if state["witch_antidote"] and state.get("wolf_kill_target"):
            can_self = state["round_no"] == 1 or state["wolf_kill_target"] != player_id
            if can_self:
                opts.append("使用解药救人")
                af = '{"type": "witch_save"}'
        if state["witch_poison"]:
            pt = [p for p in state["alive"] if p != player_id]
            opts.append(f"使用毒药毒杀一人（可选: {'、'.join(pt)}）")
            af2 = '{"type": "witch_poison", "target": "玩家id"}'
            af = f'{af} 或 {af2}' if af else af2
        opts.append("不使用药水")
        af_final = af + ' 或 {"type": "witch_skip"}' if af else '{"type": "witch_skip"}'
        return NIGHT_WITCH_TMPL.format(
            killed=killed,
            antidote_status="有" if state["witch_antidote"] else "已用",
            poison_status="有" if state["witch_poison"] else "已用",
            options="；".join(opts),
            action_format=af_final,
            **common)
    if ph == "hunter_shoot":
        targets = list(state["alive"])
        return HUNTER_SHOOT_TMPL.format(target_ids="、".join(targets), **common)
    if ph == "day_speak":
        return DAY_SPEAK_TMPL.format(**common)
    if ph == "day_vote":
        targets = [p for p in state["alive"] if p != player_id]
        return DAY_VOTE_TMPL.format(target_ids="、".join(targets), **common)
    return f"未知阶段: {ph}"


def legal_actions(state: dict, player_id: str) -> list[dict]:
    ph = state["phase"]
    if ph == "night_wolf":
        return [{"type": "kill", "target": t}
                for t in state["alive"] if t not in state["wolf_ids"]]
    if ph == "night_seer":
        return [{"type": "check", "target": t}
                for t in state["alive"] if t != player_id]
    if ph == "night_witch":
        acts = []
        killed = state.get("wolf_kill_target")
        if state["witch_antidote"] and killed:
            can_self = state["round_no"] == 1 or killed != player_id
            if can_self:
                acts.append({"type": "witch_save"})
        if state["witch_poison"]:
            acts.extend({"type": "witch_poison", "target": t}
                         for t in state["alive"] if t != player_id)
        acts.append({"type": "witch_skip"})
        return acts
    if ph == "hunter_shoot":
        acts = [{"type": "shoot", "target": t} for t in state["alive"]]
        acts.append({"type": "shoot_skip"})
        return acts
    if ph == "day_speak":
        return [{"type": "speak"}]
    if ph == "day_vote":
        return [{"type": "vote", "target": t}
                for t in state["alive"] if t != player_id]
    return []


# ---- 动作执行 ----
def apply(state: dict, player_id: str, action: dict) -> tuple[dict, dict]:
    state = copy.deepcopy(state)
    extra: dict = {}
    t = action.get("type")

    if t == "kill":
        if state["phase"] != "night_wolf":
            raise IllegalAction("当前不是狼人行动阶段")
        if player_id not in state["wolf_ids"] or player_id not in state["alive"]:
            raise IllegalAction("你不是存活中的狼人")
        tgt = action.get("target")
        if tgt not in state["alive"] or tgt in state["wolf_ids"]:
            raise IllegalAction("不能选择该目标")
        state["wolf_votes"][player_id] = tgt
        alive_wolves = [w for w in state["wolf_ids"] if w in state["alive"]]
        if all(w in state["wolf_votes"] for w in alive_wolves):
            counts = Counter(state["wolf_votes"].values())
            mx = max(counts.values())
            cands = [p for p, v in counts.items() if v == mx]
            state["wolf_kill_target"] = random.choice(cands)
            state["wolf_votes"] = {}
            _advance_to_seer(state)

    elif t == "check":
        if state["phase"] != "night_seer":
            raise IllegalAction("当前不是预言家行动阶段")
        if player_id != state["seer_id"]:
            raise IllegalAction("你不是预言家")
        tgt = action.get("target")
        if tgt not in state["alive"] or tgt == player_id:
            raise IllegalAction("不能选择该目标")
        result = "werewolf" if state["roles"][tgt] == "werewolf" else "good"
        state["seer_checks"].append({"target": tgt, "result": result})
        extra["highlight"] = f"预言家查验 {tgt}: {'狼人' if result == 'werewolf' else '好人'}"
        _advance_to_witch(state)

    elif t == "witch_save":
        if state["phase"] != "night_witch":
            raise IllegalAction("当前不是女巫行动阶段")
        if player_id != state["witch_id"]:
            raise IllegalAction("你不是女巫")
        if not state["witch_antidote"]:
            raise IllegalAction("解药已用完")
        killed = state.get("wolf_kill_target")
        if not killed:
            raise IllegalAction("今晚无人被杀，无需使用解药")
        if state["round_no"] > 1 and killed == player_id:
            raise IllegalAction("非第一晚不能自救")
        state["witch_antidote"] = False
        state["witch_action"] = "save"
        extra["highlight"] = f"女巫使用解药救了 {killed}"
        _resolve_night(state)

    elif t == "witch_poison":
        if state["phase"] != "night_witch":
            raise IllegalAction("当前不是女巫行动阶段")
        if player_id != state["witch_id"]:
            raise IllegalAction("你不是女巫")
        if not state["witch_poison"]:
            raise IllegalAction("毒药已用完")
        tgt = action.get("target")
        if tgt not in state["alive"] or tgt == player_id:
            raise IllegalAction("不能选择该目标")
        state["witch_poison"] = False
        state["witch_action"] = "poison"
        state["witch_poison_target"] = tgt
        extra["highlight"] = f"女巫使用毒药毒杀 {tgt}"
        _resolve_night(state)

    elif t == "witch_skip":
        if state["phase"] != "night_witch":
            raise IllegalAction("当前不是女巫行动阶段")
        if player_id != state["witch_id"]:
            raise IllegalAction("你不是女巫")
        state["witch_action"] = "skip"
        _resolve_night(state)

    elif t == "shoot":
        if state["phase"] != "hunter_shoot":
            raise IllegalAction("当前不是猎人开枪阶段")
        if player_id != state["hunter_id"]:
            raise IllegalAction("你不是猎人")
        tgt = action.get("target")
        if tgt not in state["alive"]:
            raise IllegalAction("目标不在场")
        state["alive"].remove(tgt)
        state["dead"].append(tgt)
        state["death_cause"][tgt] = "hunter"
        state["hunter_shoot_pending"] = False
        extra["highlight"] = f"猎人开枪带走了 {tgt}！"
        state["public_log"].append(f"猎人开枪带走了 {tgt}")
        if not _check_win(state):
            _advance_after_hunter(state)

    elif t == "shoot_skip":
        if state["phase"] != "hunter_shoot":
            raise IllegalAction("当前不是猎人开枪阶段")
        if player_id != state["hunter_id"]:
            raise IllegalAction("你不是猎人")
        state["hunter_shoot_pending"] = False
        extra["highlight"] = "猎人选择不开枪"
        if not _check_win(state):
            _advance_after_hunter(state)

    elif t == "speak":
        if state["phase"] != "day_speak":
            raise IllegalAction("当前不是发言阶段")
        if player_id not in state["alive"]:
            raise IllegalAction("你已经淘汰")
        msg = action.get("message", "")
        state["day_speeches"][player_id] = msg
        state["day_spoken"].append(player_id)
        if all(p in state["day_spoken"] for p in state["alive"]):
            state["phase"] = "day_vote"

    elif t == "vote":
        if state["phase"] != "day_vote":
            raise IllegalAction("当前不是投票阶段")
        if player_id not in state["alive"]:
            raise IllegalAction("你已经淘汰")
        tgt = action.get("target")
        if tgt not in state["alive"] or tgt == player_id:
            raise IllegalAction("不能选择该目标")
        state["day_votes"][player_id] = tgt
        if all(p in state["day_votes"] for p in state["alive"]):
            _resolve_vote(state, extra)

    else:
        raise IllegalAction(f"未知动作类型: {t!r}")

    return state, extra


# ---- 阶段推进 ----
def _advance_to_seer(state):
    if state["seer_id"] in state["alive"]:
        state["phase"] = "night_seer"
    else:
        _advance_to_witch(state)


def _advance_to_witch(state):
    wid = state["witch_id"]
    if wid in state["alive"] and (state["witch_antidote"] or state["witch_poison"]):
        state["phase"] = "night_witch"
    else:
        _resolve_night(state)


def _resolve_night(state):
    """结算夜晚，确定死亡，推进到白天。"""
    deaths = []
    killed = state.get("wolf_kill_target")
    if killed and state.get("witch_action") != "save":
        deaths.append(killed)
        state["death_cause"][killed] = "wolf"
    if state.get("witch_action") == "poison":
        pt = state["witch_poison_target"]
        deaths.append(pt)
        state["death_cause"][pt] = "witch_poison"

    for pid in deaths:
        if pid in state["alive"]:
            state["alive"].remove(pid)
            state["dead"].append(pid)
    state["night_deaths"] = deaths

    if deaths:
        state["public_log"].append(f"第{state['round_no']}夜：{', '.join(deaths)} 死亡")
    else:
        state["public_log"].append(f"第{state['round_no']}夜：平安夜（无人死亡）")

    hunter_dead = state["hunter_id"] in deaths
    hunter_can_shoot = (hunter_dead and
                        state["death_cause"].get(state["hunter_id"]) != "witch_poison")
    if hunter_can_shoot:
        state["hunter_shoot_pending"] = True
        state["hunter_context"] = "night"
        state["phase"] = "hunter_shoot"
    else:
        if not _check_win(state):
            _start_day(state)


def _resolve_vote(state, extra):
    counts = Counter(state["day_votes"].values())
    mx = max(counts.values())
    cands = [p for p, v in counts.items() if v == mx]
    voted_out = cands[0] if len(cands) == 1 else random.choice(cands)

    state["alive"].remove(voted_out)
    state["dead"].append(voted_out)
    state["death_cause"][voted_out] = "vote"
    state["vote_result"] = voted_out
    detail = ", ".join(f"{v}->{t}" for v, t in state["day_votes"].items())
    state["public_log"].append(f"第{state['round_no']}天投票：{voted_out} 出局（{detail}）")
    extra["highlight"] = f"{voted_out} 被投票出局！"

    if voted_out == state["hunter_id"]:
        state["hunter_shoot_pending"] = True
        state["hunter_context"] = "vote"
        state["phase"] = "hunter_shoot"
    else:
        if not _check_win(state):
            _start_night(state)


def _advance_after_hunter(state):
    ctx = state.get("hunter_context", "night")
    state["hunter_context"] = None
    if ctx == "vote":
        _start_night(state)
    else:
        _start_day(state)


def _start_day(state):
    state["phase"] = "day_speak"
    state["day_spoken"] = []
    state["day_speeches"] = {}
    state["day_votes"] = {}
    state["vote_result"] = None


def _start_night(state):
    state["round_no"] += 1
    state["phase"] = "night_wolf"
    state["wolf_votes"] = {}
    state["wolf_kill_target"] = None
    state["witch_action"] = None
    state["witch_poison_target"] = None
    state["night_deaths"] = []
    state["day_spoken"] = []
    state["day_speeches"] = {}
    state["day_votes"] = {}
    state["vote_result"] = None


def _check_win(state) -> bool:
    wolves = [p for p in state["wolf_ids"] if p in state["alive"]]
    good = [p for p in state["alive"] if p not in state["wolf_ids"]]
    if not wolves:
        state["winner"] = "good"
        state["finished"] = True
        return True
    if len(wolves) >= len(good):
        state["winner"] = "werewolves"
        state["finished"] = True
        return True
    return False


def is_terminal(state: dict) -> str | None:
    return state["winner"] if state["finished"] else None


def display_state(state: dict) -> dict:
    return {
        "round_no": state["round_no"],
        "phase": state["phase"],
        "phase_label": "🌙 夜晚" if state["phase"].startswith("night") else
                       ("🔫 猎人开枪" if state["phase"] == "hunter_shoot" else "☀️ 白天"),
        "alive": list(state["alive"]),
        "dead": list(state["dead"]),
        "night_deaths": state.get("night_deaths", []),
        "day_votes": state.get("day_votes", {}),
        "vote_result": state.get("vote_result"),
        "safe_label": f"存活 {len(state['alive'])} / {len(state['order'])}",
        "players": {pid: {
            "alive": pid in state["alive"],
            "role": ROLE_NAMES[state["roles"][pid]],
            "color_hex": ROLE_COLORS[state["roles"][pid]],
            "color": ROLE_NAMES[state["roles"][pid]],
        } for pid in state["order"]},
    }


def settlement(state: dict) -> dict:
    w = state["winner"]
    return {pid: {
        "role": ROLE_NAMES[state["roles"][pid]],
        "alive": pid in state["alive"],
        "win": (pid in state["wolf_ids"] and w == "werewolves") or
               (pid not in state["wolf_ids"] and w == "good"),
    } for pid in state["order"]}


def game_meta(config: dict, cast: list[dict]) -> dict:
    return {
        "game": "werewolf",
        "game_name": config.get("name", "狼人杀"),
        "stage": {"type": "cell_grid", "target_safe": 0},
        "players": [{"id": c["id"], "name": c["name"],
                     "color": c["color"], "emoji": c["emoji"]} for c in cast],
    }
