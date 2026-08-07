# AI 游戏剧场（AiGameTheater）

一个人、一台电脑，持续产出"一群 AI 大模型玩博弈游戏"的短视频。

**对局即素材**：每跑一局游戏，自动产出事件日志（JSONL）+ 双轨字幕（SRT）+ 可录制回放，拿到手直接进剪映。

## 快速开始

```bash
pip install -r requirements.txt

# 1. 零成本试跑（不调用任何 API，验证流程）
python run_game.py --game dollar_auction --mode mock

# 2. 廉价模型预演（需要 DEEPSEEK_API_KEY）
export DEEPSEEK_API_KEY=sk-xxx
python run_game.py --game dollar_auction --mode cheap

# 3. 正式阵容录制（按 core/personas.yaml 绑定的模型，需配置对应 Key）
python run_game.py --game dollar_auction --mode official

# 4. 赛季积分榜（汇总 out/ 下全部对局）
python -m core.standings out
```

## 内置游戏

| 游戏 | 目录 | 内容钩子 | 盘面模板 |
|------|------|---------|---------|
| 美元拍卖 | `games/dollar_auction/` | 为什么 AI 会花 3 美元抢 1 美元（升级陷阱） | leaderboard |
| 海盗分金 | `games/pirate_gold/` | 提案被否就喂鲨鱼；AI 不按博弈论最优解出牌 | proposal_vote |
| 最后通牒 | `games/ultimatum/` | AI 会为了尊严掀桌子吗 | leaderboard |
| 吹牛骰子 | `games/perudo/` | 全程说谎与读谎（隐藏信息，1 点万能） | leaderboard |

## 制作一期视频

1. `--mode cheap` 预演 1–3 局，确认流程与戏剧性；
2. `--mode official` 正式跑局（可多跑几局备选，单局约 $3–8）；
3. 浏览器打开 `viewer/index.html`，把 `out/` 里的 `.jsonl` 拖进去；
4. 点播放（自动导演：高光自动放慢放大 + 音效；可按"只播高光"录精华版）；
5. 竖屏短视频点"竖屏"切 9:16；要叠加解说画面点"OBS 抠像"（绿幕模式）；
6. OBS 录屏 → 剪映导入视频 + `*_speech.srt` / `*_thought.srt` 双轨字幕 → 文字转语音 → 加音乐 → 导出。

全程后期约 15–30 分钟，剪辑只做取舍，不做创作。

## 目录结构

```
core/               共享运行器（llm 适配 / 对局循环 / 日志字幕 / 班底人设 / 积分榜）
games/<游戏名>/      一个游戏一个文件夹：game.py + config.yaml
viewer/index.html   回放页面（纯 HTML 单文件，双击即用）
tests/              单元测试（pytest，60 个用例）
out/                对局产出（日志 + 字幕 + 积分榜）
docs/               需求文档与系统设计
```

## 添加新游戏（目标：1 天）

1. 复制任一 `games/xxx/` 为 `games/<新游戏>/`；
2. 实现接口契约：`initial_state` / `active_players` / `visible_state` / `get_prompt` / `apply` / `is_terminal`（外加 `legal_actions` / `display_state` / `settlement` / `game_meta`）；
3. `game_meta()` 里选盘面模板（`leaderboard` 排行榜式 / `proposal_vote` 提案表决式）并映射 `display_state` 字段；
4. `--mode mock` 零成本跑通 → 写测试 → 上线。

隐藏信息游戏（如吹牛骰子）在 `visible_state()` 里裁剪他人可见信息即可，日志与回放自动兼容。

## 测试

```bash
python -m pytest tests/ -v     # 60 个用例
```

## 环境变量（API Key 仅从环境变量读取）

| 变量 | 用途 |
|------|------|
| `OPENAI_API_KEY` | GPT 班底 |
| `ANTHROPIC_API_KEY` | Claude 班底 |
| `GOOGLE_API_KEY` | Gemini 班底 |
| `DEEPSEEK_API_KEY` | DeepSeek 班底 / cheap 模式 |
| `XAI_API_KEY` | Grok 班底 |
| `MOONSHOT_API_KEY` | Kimi 班底 |
| `DASHSCOPE_API_KEY` | Qwen 班底 |

缺哪个 Key，对应班底成员就在游戏 `config.yaml` 的 `cast` 里暂时移除即可。
