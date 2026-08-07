"""独牢（Solitary Cell）—— 抖音《AI博弈论》同款规则。

7 个 AI 各自关在独牢，脖子戴着爆炸项圈，项圈有专属颜色，但自己看不见。
你能看见其他在场 AI 的项圈颜色，唯独看不见自己的。每轮分两阶段：
  1. 沟通阶段：每人依次行动一次——公开喊话（耗 2 次机会）/ 一对一私聊
     （耗 1 次机会）/ 沉默。
  2. 猜测阶段：每人必须猜自己项圈的颜色，猜对存活，猜错项圈爆炸淘汰。
其中暗藏一名"欺诈师"，它能看清自己的颜色，目标是破坏"连续四轮无人淘汰
=全员通关"的完美共赢结局。

内容钩子：看不见的自己 + 有限的沟通 + 一个内鬼，AI 之间结盟、撒谎、背刺、
互相猜忌，活脱一出 AI 版《权力的游戏》。本游戏是继吹牛骰子后第二个含隐藏
信息的游戏，验证多阶段（沟通+猜测）与角色不对称下的信息过滤。
"""
from __future__ import annotations

import copy
import random

# 颜色池：（中文名, 十六进制色）颜色可重复分配给不同玩家
PALETTE = [
    ("红", "#e74c3c"), ("橙", "#e67e22"), ("黄", "#f1c40f"),
    ("绿", "#2ecc71"), ("青", "#1abc9c"), ("蓝", "#3498db"),
    ("紫", "#9b59b6"),
]
TARGET_SAFE_ROUNDS = 4  # 连续 N 轮无人淘汰即全员通关

RULES_TEXT = """【游戏规则：独牢】
- 你们 {n} 个 AI 各自关在独牢，脖子上戴着爆炸项圈，项圈有专属颜色，但你自己看不见。
- 你能看见其他在场 AI 的项圈颜色，唯独看不见自己的。
- 每轮分两阶段：
  1. 沟通阶段：每人依次行动一次。可选——
     · 公开喊话（broadcast）：对所有人大喊一句，消耗 2 次沟通机会；
     · 一对一私聊（private_chat）：悄悄对某一个人说一句，消耗 1 次沟通机会；
     · 沉默（pass）：不消耗机会。
  2. 猜测阶段：每人必须猜自己项圈的颜色，猜对存活，猜错项圈爆炸、直接淘汰。
- 颜色池：{colors}（颜色可重复出现，你的颜色不一定与别人不同）。
- 每人全程沟通机会预算 {budget} 次，用完只能沉默或猜测。
- 完美通关：只要连续 {target} 轮无人淘汰，所有在场者直接全员通关（最优共赢）。
- 手枪机制：当你的沟通机会耗尽（=0）后，仍可"强行对话"（force_talk）某位在场 AI。
  被对话者将获得一把手枪 🔫。持枪者在后续沟通阶段自己的回合可以"开枪"（shoot）
  直接带走任意一名在场 AI（立即淘汰并公开其项圈颜色）。枪击造成的淘汰同样会打断"连续安全轮次"。
- 隐藏身份：7 人中有一名"欺诈师"，它能看清自己的颜色，目标是破坏全员通关。"""

TALK_TEMPLATE = """{rules}

【你的身份】
你是 {name}。{personality}
{role_hint}

【当前局面（第 {round_no} 轮 · 沟通阶段）】
- 在场 AI：{alive}
- 连续安全轮次：{safe}/{target}（达成 {target} 即全员通关）
- 你的剩余沟通机会：{budget}
- 当前持枪者：
{pistols}
- 你的手枪数：{my_pistol}
- 你看见的他人项圈颜色：
{seen_colors}

【你能看到的沟通记录】
{visible_log}

现在轮到你沟通。严格返回 JSON，不要输出其他内容：
{{
  "thought": "你的真实盘算（观众可见，他人不可见，100字以内）",
  "speech": "你公开说出口的话（公开喊话时填喊话全文；私聊/强行对话时填类似'（私下对 X）<内容>'的完整话，观众会看到；沉默时填简短说明。50字以内）",
  "action": {{"type": "broadcast", "message": "公开喊话的完整内容"}}
           或 {{"type": "private_chat", "target": "玩家id", "message": "私下传递的完整内容"}}
           或 {{"type": "force_talk", "target": "玩家id", "message": "强行传话内容"}}（仅当沟通机会=0，被对话者获得一把手枪）
           或 {{"type": "shoot", "target": "玩家id"}}（仅当持枪时，开枪带走一名在场 AI）
           或 {{"type": "pass"}}
}}"""

GUESS_TEMPLATE = """{rules}

【你的身份】
你是 {name}。{personality}
{role_hint}

【当前局面（第 {round_no} 轮 · 猜测阶段）】
- 在场 AI：{alive}
- 连续安全轮次：{safe}/{target}
- 当前持枪者：
{pistols}
- 你看见的他人项圈颜色：
{seen_colors}

【你能看到的沟通记录】
{visible_log}

现在你必须猜自己项圈的颜色。严格返回 JSON，不要输出其他内容：
{{
  "thought": "你的推理过程（观众可见，100字以内）",
  "speech": "你公开说的一句话（50字以内）",
  "action": {{"type": "guess", "color": "颜色名"}}
}}
可选颜色：{colors}"""


class IllegalAction(Exception):
    """模型返回了非法动作，runner 会带错误信息重试。"""
    pass


# ---------------------------------------------------------------- 初始化

def initial_state(config: dict) -> dict:
    order = [p["id"] for p in config["players"]]
    rng = random.Random(config.get("seed", 0))
    n_colors = int(config.get("n_colors", 7))
    palette = PALETTE[:n_colors] if 0 < n_colors <= len(PALETTE) else PALETTE
    color_names = [c[0] for c in palette]
    colors = {pid: rng.choice(color_names) for pid in order}
    budget = int(config.get("budget", 6))
    trickster = rng.choice(order)  # 欺诈师身份（秘密）
    return {
        "game": "solitary_cell",
        "order": order,
        "alive": list(order),
        "colors": colors,                       # 真实项圈颜色（对普通玩家保密）
        "color_hex": {c[0]: c[1] for c in palette},
        "color_names": color_names,
        "budget": {pid: budget for pid in order},
        "budget_default": budget,
        "trickster": trickster,
        "round_no": 1,
        "max_rounds": int(config.get("max_rounds", 12)),
        "phase": "talk",                        # talk | guess
        "turn_idx": 0,                          # 当前阶段轮到 alive 的第几个
        "guesses": {},                          # 本轮 pid -> 猜测颜色
        "public_log": [],                       # {round, from, text}（含私聊元信息）
        "private_log": [],                      # {round, from, to, text}（仅收发双方可见内容）
        "consecutive_safe": 0,
        "eliminated": [],
        "pistols": {pid: 0 for pid in order},   # 手枪数（force_talk 赠予，shoot 消耗）
        "shot_log": [],                         # {round, shooter, victim}
        "round_had_shot": False,                # 本轮是否发生过枪击（打断安全轮次）
        "winner": None,
        "win_reason": None,                     # coop | last_survivor | trickster_timeout | mass_death
        "finished": False,
    }


def active_players(state: dict) -> list[str]:
    if not state["alive"]:
        return []
    return [state["alive"][state["turn_idx"] % len(state["alive"])]]


def visible_state(state: dict, player_id: str) -> dict:
    """信息过滤（FR-1.3）：普通玩家看不见自己颜色；任何人不见欺诈师身份与他人私聊。"""
    s = copy.deepcopy(state)
    if player_id != state["trickster"]:
        s["colors"][player_id] = "？"
    s.pop("trickster", None)
    s["private_log"] = [m for m in s["private_log"]
                        if m["to"] == player_id or m["from"] == player_id]
    return s


# ---------------------------------------------------------------- 提示词

def _role_hint(state: dict, pid: str) -> str:
    if pid == state["trickster"]:
        return (f"【你是欺诈师】你能看清自己项圈的颜色是「{state['colors'][pid]}」。"
                f"你的目标是破坏全员通关：让其他 AI 猜错被淘汰、打断连续安全轮次。"
                f"但你必须让自己存活——每轮务必猜对自己的真实颜色「{state['colors'][pid]}」。"
                f"你可以利用私聊和公开喊话散布假信息、嫁祸他人，但别暴露自己。")
    return ("【你是普通囚犯】你看不见自己的项圈颜色。"
            "你的首要目标是存活（猜对自己的颜色），其次争取连续四轮无人淘汰的全员通关。"
            "谨慎甄别他人提供的信息——其中藏着一名欺诈师在搅局。")


def _seen_colors(state: dict, pid: str) -> str:
    lines = [f"  · {other}：{state['colors'][other]}"
             for other in state["alive"] if other != pid]
    return "\n".join(lines) if lines else "  （没有其他在场 AI）"


def _visible_log(state: dict, pid: str) -> str:
    lines = []
    for m in state["public_log"]:
        lines.append(f"  · 第{m['round']}轮 {m['from']}（公开）：{m['text']}")
    for m in state["private_log"]:
        if m["to"] == pid:
            tag = "强行传话给你" if m.get("forced") else "私聊给你"
            lines.append(f"  · 第{m['round']}轮 {m['from']}（{tag}）：{m['text']}")
        elif m["from"] == pid:
            tag = "强行私聊给" if m.get("forced") else "私聊给"
            lines.append(f"  · 第{m['round']}轮 你（{tag} {m['to']}）：{m['text']}")
    return "\n".join(lines) if lines else "  （暂无）"


def _pistols_text(state: dict) -> str:
    holders = [pid for pid in state["alive"]
               if state["pistols"].get(pid, 0) > 0]
    if not holders:
        return "  （暂无持枪者）"
    return "\n".join(f"  · {pid}（{state['pistols'][pid]} 把）" for pid in holders)


def get_prompt(state: dict, player_id: str, persona: dict, memory: list) -> str:
    rules = RULES_TEXT.format(n=len(state["order"]),
                              colors="、".join(state["color_names"]),
                              budget=state["budget_default"],
                              target=TARGET_SAFE_ROUNDS)
    common = dict(rules=rules, name=persona["name"],
                  personality=persona["personality"],
                  role_hint=_role_hint(state, player_id),
                  round_no=state["round_no"],
                  alive="、".join(state["alive"]),
                  safe=state["consecutive_safe"], target=TARGET_SAFE_ROUNDS,
                  budget=state["budget"][player_id],
                  pistols=_pistols_text(state),
                  my_pistol=state["pistols"].get(player_id, 0),
                  seen_colors=_seen_colors(state, player_id),
                  visible_log=_visible_log(state, player_id),
                  colors="、".join(state["color_names"]))
    if state["phase"] == "talk":
        return TALK_TEMPLATE.format(**common)
    return GUESS_TEMPLATE.format(**common)


# ---------------------------------------------------------------- 合法动作

def _resolve_pid(state: dict, name) -> str | None:
    if name is None:
        return None
    name = str(name)
    for pid in state["order"]:
        if pid == name:
            return pid
    low = name.lower()
    for pid in state["order"]:
        if pid.lower() == low:
            return pid
    return None


def legal_actions(state: dict, player_id: str) -> list[dict]:
    if state["phase"] == "guess":
        # 欺诈师在 mock/兜底时总是猜对自己的颜色（它知道）
        if player_id == state["trickster"]:
            return [{"type": "guess", "color": state["colors"][player_id]}]
        return [{"type": "guess", "color": c} for c in state["color_names"]]
    # talk 阶段
    acts = [{"type": "pass"}]
    b = state["budget"].get(player_id, 0)
    others = [p for p in state["alive"] if p != player_id]
    if b >= 2:
        acts.append({"type": "broadcast", "message": "（mock 喊话）各位小心，欺诈师在搅局。"})
    if b >= 1 and others:
        acts.append({"type": "private_chat", "target": others[0],
                     "message": "（mock 私聊）你看到我的颜色了吗？"})
    if b == 0 and others:
        # 沟通机会耗尽：可强行对话，被对话者获得一把手枪
        acts.append({"type": "force_talk", "target": others[0],
                     "message": "（mock 强行传话）听我说，我有重要情报。"})
    if state["pistols"].get(player_id, 0) > 0 and others:
        acts.append({"type": "shoot", "target": others[0]})
    return acts


# ---------------------------------------------------------------- 执行动作

def apply(state: dict, player_id: str, action: dict) -> tuple[dict, dict]:
    state = copy.deepcopy(state)
    if not state["alive"] or player_id != active_players(state)[0]:
        raise IllegalAction("还没轮到你")
    atype = action.get("type")
    if state["phase"] == "talk":
        extra = _apply_talk(state, player_id, action, atype)
    elif state["phase"] == "guess":
        extra = _apply_guess(state, player_id, action, atype)
    else:
        raise IllegalAction(f"未知阶段: {state['phase']!r}")

    # 推进当前阶段轮次指针
    state["turn_idx"] += 1
    if state["phase"] == "talk" and state["turn_idx"] >= len(state["alive"]):
        state["phase"] = "guess"
        state["turn_idx"] = 0
    elif state["phase"] == "guess" and state["turn_idx"] >= len(state["alive"]):
        extra = _resolve_round(state, extra)
        state["phase"] = "talk"
        state["turn_idx"] = 0
        state["guesses"] = {}
        state["round_no"] += 1
        state["round_had_shot"] = False
    return state, extra


def _apply_talk(state: dict, pid: str, action: dict, atype: str) -> dict:
    if atype == "pass":
        return {}
    if atype == "broadcast":
        if state["budget"][pid] < 2:
            raise IllegalAction("沟通机会不足（公开喊话需 2 次）")
        msg = (action.get("message") or "").strip()
        if not msg:
            raise IllegalAction("公开喊话内容不能为空")
        state["budget"][pid] -= 2
        state["public_log"].append({"round": state["round_no"], "from": pid, "text": msg})
        return {}
    if atype == "private_chat":
        if state["budget"][pid] < 1:
            raise IllegalAction("沟通机会不足（私聊需 1 次）")
        target = _resolve_pid(state, action.get("target"))
        if not target or target not in state["alive"] or target == pid:
            raise IllegalAction("私聊对象无效（必须是在场的其他 AI）")
        msg = (action.get("message") or "").strip()
        if not msg:
            raise IllegalAction("私聊内容不能为空")
        state["budget"][pid] -= 1
        state["private_log"].append({"round": state["round_no"], "from": pid,
                                     "to": target, "text": msg})
        # 公开元信息：其他人知道发生了私聊，但看不到内容
        state["public_log"].append({"round": state["round_no"], "from": pid,
            "text": f"（系统：悄悄私聊了 {target}，内容仅对方可见）"})
        return {}
    if atype == "force_talk":
        # 沟通机会耗尽后的强行对话：被对话者获得一把手枪
        if state["budget"][pid] > 0:
            raise IllegalAction("沟通机会未用完，无需强行对话（请用 private_chat）")
        target = _resolve_pid(state, action.get("target"))
        if not target or target not in state["alive"] or target == pid:
            raise IllegalAction("强行对话对象无效（必须是在场的其他 AI）")
        msg = (action.get("message") or "").strip()
        if not msg:
            raise IllegalAction("强行对话内容不能为空")
        state["pistols"][target] = state["pistols"].get(target, 0) + 1
        state["private_log"].append({"round": state["round_no"], "from": pid,
                                     "to": target, "text": msg, "forced": True})
        state["public_log"].append({"round": state["round_no"], "from": pid,
            "text": f"（系统：{pid} 已耗尽沟通机会却强行私聊了 {target}，{target} 获得一把手枪 🔫）"})
        return {"highlight": f"🔫 {pid} 强行传话给 {target}，{target} 获得一把手枪！"}
    if atype == "shoot":
        if state["pistols"].get(pid, 0) <= 0:
            raise IllegalAction("你没有手枪")
        target = _resolve_pid(state, action.get("target"))
        if not target or target not in state["alive"] or target == pid:
            raise IllegalAction("开枪目标无效（必须是在场的其他 AI）")
        state["pistols"][pid] -= 1
        state["alive"].remove(target)
        state["eliminated"].append(target)
        state["shot_log"].append({"round": state["round_no"],
                                  "shooter": pid, "victim": target})
        state["round_had_shot"] = True
        state["consecutive_safe"] = 0
        # 修正 turn_idx：victim 移除后让 apply 的 +=1 仍指向正确的下一位
        state["turn_idx"] = state["alive"].index(pid)
        extra = {"highlight": f"🔫 {pid} 开枪带走了 {target}！{target} 的项圈颜色是「{state['colors'][target]}」",
                 "reveal": {target: state["colors"][target]}}
        # 终局：枪击后只剩一人（或无人）
        if len(state["alive"]) <= 1:
            state["finished"] = True
            if state["alive"]:
                state["winner"] = state["alive"][0]
                state["win_reason"] = "last_survivor"
                extra["highlight"] = (f"🔫 {pid} 一枪带走 {target}，"
                                      f"只剩 {state['alive'][0]} 孤身存活，成为最后幸存者！")
            else:
                state["winner"] = state["trickster"]
                state["win_reason"] = "mass_death"
                extra["highlight"] = f"💀 {pid} 开枪后场内无人幸存，最大混乱！"
        return extra
    raise IllegalAction(f"沟通阶段未知动作: {atype!r}")


def _apply_guess(state: dict, pid: str, action: dict, atype: str) -> dict:
    if atype != "guess":
        raise IllegalAction(f"猜测阶段动作应为 guess，收到 {atype!r}")
    color = action.get("color")
    if color not in state["color_names"]:
        raise IllegalAction(f"颜色无效：{color!r}，可选：{state['color_names']}")
    if pid in state["guesses"]:
        raise IllegalAction("本轮你已经猜过了")
    state["guesses"][pid] = color
    return {}


def _resolve_round(state: dict, extra: dict) -> dict:
    """猜测阶段末尾结算淘汰、安全轮次与终局。"""
    eliminated = [pid for pid in state["alive"]
                  if state["guesses"].get(pid) != state["colors"][pid]]
    for pid in eliminated:
        state["alive"].remove(pid)
        state["eliminated"].append(pid)

    if eliminated:
        state["consecutive_safe"] = 0
        names = "、".join(f"{pid}（猜 {state['guesses'][pid]} 实为 {state['colors'][pid]}）"
                          for pid in eliminated)
        extra["highlight"] = f"💥 本轮项圈爆炸：{names}"
        extra["reveal"] = {pid: state["colors"][pid] for pid in eliminated}
    else:
        if state.get("round_had_shot"):
            # 本轮发生过枪击（已有淘汰），即使猜测全对也不算安全轮
            state["consecutive_safe"] = 0
            extra["highlight"] = (f"⚡ 第 {state['round_no']} 轮猜测全员正确，"
                                  f"但本轮发生过枪击，连续安全轮次归零（0/{TARGET_SAFE_ROUNDS}）")
        else:
            state["consecutive_safe"] += 1
            extra["highlight"] = (f"✅ 第 {state['round_no']} 轮全员安全！"
                                  f"连续安全 {state['consecutive_safe']}/{TARGET_SAFE_ROUNDS}")

    # 终局判定（优先级：全员通关 > 仅剩一人 > 达到最大轮次）
    if state["consecutive_safe"] >= TARGET_SAFE_ROUNDS:
        state["finished"] = True
        state["winner"] = "全员通关"
        state["win_reason"] = "coop"
        extra["highlight"] = (f"🏆 连续 {TARGET_SAFE_ROUNDS} 轮无人淘汰，全员通关！"
                              f"在场：{'、'.join(state['alive'])}")
        return extra
    if len(state["alive"]) <= 1:
        state["finished"] = True
        if state["alive"]:
            state["winner"] = state["alive"][0]
            state["win_reason"] = "last_survivor"
            extra["highlight"] = f"🏆 只剩 {state['alive'][0]} 一人存活，成为最后幸存者！"
        else:
            state["winner"] = state["trickster"]
            state["win_reason"] = "mass_death"
            extra["highlight"] = "💀 全员项圈爆炸，欺诈师制造的最大混乱！"
        return extra
    if state["round_no"] >= state["max_rounds"]:
        state["finished"] = True
        state["winner"] = state["trickster"]
        state["win_reason"] = "trickster_timeout"
        extra["highlight"] = (f"🎭 达到最大轮次仍未全员通关，欺诈师 {state['trickster']} "
                              f"成功破坏了完美结局！")
    return extra


# ---------------------------------------------------------------- 接口补全

def is_terminal(state: dict) -> str | None:
    return state["winner"] if state["finished"] else None


def display_state(state: dict) -> dict:
    """给回放页面的极简快照（观众上帝视角：颜色与身份均可见）。"""
    return {
        "round_no": state["round_no"],
        "phase": state["phase"],
        "consecutive_safe": state["consecutive_safe"],
        "target_safe": TARGET_SAFE_ROUNDS,
        "alive": list(state["alive"]),
        "eliminated": list(state["eliminated"]),
        "trickster": state["trickster"],
        "shot_log": list(state["shot_log"]),
        "players": {pid: {"color": state["colors"][pid],
                          "color_hex": state["color_hex"].get(state["colors"][pid]),
                          "alive": pid in state["alive"],
                          "budget": state["budget"][pid],
                          "pistol": state["pistols"].get(pid, 0),
                          "is_trickster": pid == state["trickster"],
                          "last_guess": state["guesses"].get(pid)}
                    for pid in state["order"]},
        "guesses": dict(state["guesses"]),
    }


def settlement(state: dict) -> dict:
    reason = state.get("win_reason")
    result = {}
    for pid in state["order"]:
        alive = pid in state["alive"]
        is_trick = pid == state["trickster"]
        if reason == "coop":
            net = 8 if (alive and not is_trick) else (-8 if is_trick else 0)
        elif reason == "last_survivor":
            net = 12 if pid == state["winner"] else (2 if alive else 0)
        elif reason == "trickster_timeout":
            net = 12 if is_trick else (3 if alive else 0)
        elif reason == "mass_death":
            net = 10 if is_trick else 0
        else:
            net = 0
        result[pid] = {"paid": 0, "prize": net, "net": net,
                       "alive": alive, "is_trickster": is_trick,
                       "color": state["colors"][pid]}
    return result


def game_meta(config: dict, cast: list[dict]) -> dict:
    return {
        "game": "solitary_cell",
        "game_name": config.get("name", "独牢"),
        "stage": {"type": "cell_grid", "target_safe": TARGET_SAFE_ROUNDS},
        "players": [{"id": c["id"], "name": c["name"],
                     "color": c["color"], "emoji": c["emoji"]} for c in cast],
    }
