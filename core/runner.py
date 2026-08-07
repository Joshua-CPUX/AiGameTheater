"""通用对局运行器（FR-1）。

职责：加载游戏与阵容 → 调度回合 → 组装提示词 → 调模型 → 校验动作 → 写日志 → 导出字幕。
游戏逻辑全部在游戏模块的接口函数中，runner 不含任何具体游戏规则。
"""
from __future__ import annotations

import importlib.util
import random
import sys
from datetime import datetime
from pathlib import Path

import yaml

from .llm import LLMError, make_client, CostTracker
from .logger import EventLogger, export_srt, read_events

ROOT = Path(__file__).resolve().parent.parent

SYSTEM_PROMPT = (
    "你是一个参与博弈游戏的 AI 玩家。请完全代入给你的人设进行游戏。"
    "你必须严格按要求返回 JSON（thought / speech / action 三个字段），"
    "不要输出任何 JSON 以外的内容。"
)

# Mock 模式的兜底台词（零成本跑流程用）
MOCK_THOUGHTS = ["（mock）试探一下其他人的底线。", "（mock）先苟住，观察局势。",
                 "（mock）这个位置我不能再退了。", "（mock）他们肯定没想到我会这么做。"]
MOCK_SPEECHES = ["（mock）随便出一手。", "（mock）我跟。", "（mock）这把我势在必得。",
                 "（mock）你们随意。"]


def load_personas() -> dict:
    with open(ROOT / "core" / "personas.yaml", encoding="utf-8") as f:
        return {p["id"]: p for p in yaml.safe_load(f)["personas"]}


def load_game(game_name: str):
    game_dir = ROOT / "games" / game_name
    spec = importlib.util.spec_from_file_location(
        f"games.{game_name}.game", game_dir / "game.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with open(game_dir / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return module, config


def build_cast(config: dict, personas: dict) -> list[dict]:
    cast = []
    for pid in config["cast"]:
        if pid not in personas:
            raise ValueError(f"阵容中的 {pid!r} 不在 personas.yaml 里")
        cast.append(personas[pid])
    return cast


def run_game(game_name: str, mode: str = "mock", seed: int | None = None,
             out_dir: str | Path | None = None,
             config_override: dict | None = None) -> dict:
    """跑一局游戏。mode: mock / cheap / official。返回对局摘要。"""
    rng = random.Random(seed)
    game, config = load_game(game_name)
    if config_override:
        config.update(config_override)
    personas = load_personas()
    cast = build_cast(config, personas)

    # 游戏内部只认 players 列表（FR-1.1 接口契约）
    game_config = dict(config)
    game_config["players"] = [{"id": c["id"]} for c in cast]

    out_dir = Path(out_dir or ROOT / "out")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_prefix = out_dir / f"{stamp}_{game_name}"
    tracker = CostTracker()

    # 真实模式：为每个演员建立专属客户端（绑定各自模型，FR-3.1）
    clients = {}
    if mode != "mock":
        cheap = mode == "cheap"
        for c in cast:
            clients[c["id"]] = make_client(c["model"], cheap=cheap, tracker=tracker)

    persona_by_id = {c["id"]: c for c in cast}
    memory: dict[str, list[str]] = {c["id"]: [] for c in cast}

    with EventLogger(f"{out_prefix}.jsonl") as logger:
        logger.log({"type": "meta", "mode": mode, "seed": seed,
                    **game.game_meta(game_config, cast)})

        state = game.initial_state(game_config)
        steps, max_steps = 0, 2000  # 安全上限，防死循环

        while not game.is_terminal(state) and steps < max_steps:
            for player_id in game.active_players(state):
                if game.is_terminal(state):
                    break
                steps += 1
                persona = persona_by_id[player_id]
                state, event = _take_turn(
                    game, state, player_id, persona, memory,
                    clients.get(player_id), mode, rng, logger)
            # 防止极端情况下的无限循环

        winner = game.is_terminal(state)
        end_event = {"type": "game_end", "winner": winner,
                     "rounds": state.get("round_no", 0) - 1}
        if hasattr(game, "settlement"):
            end_event["settlement"] = game.settlement(state)
        end_event["cost"] = tracker.to_dict()
        logger.log(end_event)

    speech_srt, thought_srt = export_srt(read_events(f"{out_prefix}.jsonl"),
                                         out_prefix)

    summary = {"log": f"{out_prefix}.jsonl",
               "speech_srt": str(speech_srt), "thought_srt": str(thought_srt),
               "winner": winner, "steps": steps}
    print(tracker.report())
    print(f"赢家: {winner} | 步数: {steps}")
    print(f"日志: {summary['log']}")
    return summary


def _take_turn(game, state, player_id, persona, memory, client, mode, rng, logger):
    """执行一名玩家的一次行动（含重试与兜底，FR-1.4）。返回 (新状态, 事件)。"""
    if mode == "mock":
        action = rng.choice(game.legal_actions(state, player_id))
        thought, speech = rng.choice(MOCK_THOUGHTS), rng.choice(MOCK_SPEECHES)
        state, extra = game.apply(state, player_id, action)
        return state, _log_action(logger, game, state, player_id, persona,
                                  thought, speech, action, extra)

    prompt = game.get_prompt(state, player_id, persona, memory[player_id])
    last_err = None
    for attempt in range(3):
        try:
            resp = client.complete_json(SYSTEM_PROMPT, prompt)
            _validate_response(resp)
            action = resp["action"]
            state, extra = game.apply(state, player_id, action)
            event = _log_action(logger, game, state, player_id, persona,
                                resp["thought"], resp["speech"], action, extra)
            _update_memory(memory, player_id, persona, state, action)
            return state, event
        except (LLMError, game.IllegalAction, KeyError) as e:
            last_err = e
            prompt += f"\n\n【系统提示】你上一次的响应无效：{e}。请重新返回合法 JSON。"

    # 连续 3 次失败：随机合法动作兜底并标记（FR-1.4）
    action = rng.choice(game.legal_actions(state, player_id))
    state, extra = game.apply(state, player_id, action)
    extra["fallback"] = str(last_err)
    return state, _log_action(logger, game, state, player_id, persona,
                              "（系统兜底：该玩家未能给出有效决策）",
                              "（弃权/随机行动）", action, extra)


def _validate_response(resp: dict) -> None:
    for key in ("thought", "speech", "action"):
        if key not in resp:
            raise KeyError(f"响应缺少字段 {key!r}")
    if not isinstance(resp["action"], dict) or "type" not in resp["action"]:
        raise KeyError("action 必须是含 type 字段的对象")


def _log_action(logger, game, state, player_id, persona,
                thought, speech, action, extra) -> dict:
    event = {"type": "action", "round": state.get("round_no"),
             "player": persona["name"], "player_id": player_id,
             "color": persona["color"], "emoji": persona["emoji"],
             "thought": thought, "speech": speech, "action": action,
             "state_after": game.display_state(state)}
    if extra.get("highlight"):
        event["highlight"] = True
        event["note"] = extra["highlight"]
    if extra.get("fallback"):
        event["fallback"] = extra["fallback"]
    return logger.log(event)


def _update_memory(memory, player_id, persona, state, action) -> None:
    """向所有玩家写入公开记忆（内心独白不进记忆，FR-2.2 信息隔离）。"""
    round_no = state.get("round_no", "?")
    desc = f"第{round_no}轮 {persona['name']} 行动: {action.get('type')}"
    if action.get("type") == "bid":
        desc += f" {action.get('amount')}"
    for pid in memory:
        memory[pid].append(desc)
        if len(memory[pid]) > 20:  # 滑动窗口，控制 token 预算
            memory[pid] = memory[pid][-20:]
