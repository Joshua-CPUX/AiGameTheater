#!/usr/bin/env python3
"""AI 游戏剧场 · 控制台后端（零第三方依赖，仅用标准库）。

提供静态文件服务 + JSON API，把 run_game 的能力暴露成 Web UI：
  - 游戏列表 / 阵容 / 配置参数（动态读取 games/*/config.yaml）
  - 模型密钥管理（.secrets.json，不入库）
  - 后台开跑 + 实时进度（tail 日志）
  - 历史对局回放（集成 viewer）

启动：python app.py            默认 http://localhost:8770
      python app.py --port 9000
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote

ROOT = Path(__file__).resolve().parent
GAMES_DIR = ROOT / "games"
OUT_DIR = ROOT / "out"
WEB_DIR = ROOT / "web"
VIEWER_DIR = ROOT / "viewer"
SECRETS_FILE = ROOT / ".secrets.json"

# 受支持的 API Key 环境变量（标签 -> 变量名）
KEY_FIELDS = [
    ("OpenAI (GPT-5)",        "OPENAI_API_KEY"),
    ("Anthropic (Claude)",    "ANTHROPIC_API_KEY"),
    ("Google (Gemini)",       "GOOGLE_API_KEY"),
    ("DeepSeek 官方",         "DEEPSEEK_API_KEY"),
    ("SiliconFlow (DeepSeek-V3.2, cheap 模式)", "SILICONFLOW_API_KEY"),
    ("xAI (Grok)",            "XAI_API_KEY"),
    ("Moonshot (Kimi)",       "MOONSHOT_API_KEY"),
    ("阿里 DashScope (Qwen)", "DASHSCOPE_API_KEY"),
]

# ---------------------------------------------------------------- 任务管理
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_") + f"{int(time.time() * 1000) % 10000:04d}"


def _load_secrets_to_env() -> None:
    """把 .secrets.json 里的 Key 灌进环境变量，供 run_game 的 make_client 读取。"""
    if not SECRETS_FILE.exists():
        return
    try:
        data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        for k, v in data.items():
            if v:
                os.environ[k] = str(v)
    except Exception:
        pass


def _save_secrets(data: dict) -> None:
    """合并写入：只更新本次提交的非空字段，保留已存在的其他 Key。"""
    existing = {}
    if SECRETS_FILE.exists():
        try:
            existing = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    for k, v in data.items():
        if v:  # 非空才更新；空字符串表示"不修改"
            existing[k] = str(v)
    SECRETS_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def _list_games() -> list[dict]:
    out = []
    for d in sorted(GAMES_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        cfg_path = d / "config.yaml"
        if not cfg_path.exists():
            continue
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        params = {k: v for k, v in cfg.items() if k not in ("name", "cast")}
        out.append({
            "dir": d.name,
            "name": cfg.get("name", d.name),
            "params": params,
            "cast": cfg.get("cast", []),
        })
    return out


def _list_personas() -> list[dict]:
    import yaml
    path = ROOT / "core" / "personas.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("personas", [])


def _find_log_in_dir(job_dir: Path) -> Path | None:
    """对局日志落在 out_dir/{stamp}_{game}.jsonl，目录里通常只有一个。"""
    if not job_dir.exists():
        return None
    logs = sorted(job_dir.glob("*.jsonl"))
    return logs[-1] if logs else None


def _tail_log_summary(log_path: Path | None) -> dict:
    """读取日志尾部，返回进度摘要（步数/最后事件/是否结束）。"""
    if not log_path or not log_path.exists():
        return {"ready": False}
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {"ready": False}
    lines = [l for l in lines if l.strip()]
    actions = 0
    last = None
    meta = None
    end = None
    for l in lines:
        try:
            e = json.loads(l)
        except Exception:
            continue
        if e.get("type") == "meta":
            meta = e
        elif e.get("type") == "action":
            actions += 1
            last = e
        elif e.get("type") == "game_end":
            end = e
    return {
        "ready": True,
        "total_events": len(lines),
        "steps": actions,
        "round": last.get("round") if last else None,
        "last_player": last.get("player") if last else None,
        "finished": end is not None,
        "winner": end.get("winner") if end else None,
        "cost": end.get("cost") if end else None,
        "game_name": meta.get("game_name") if meta else None,
        "log_url": f"/out/{log_path.relative_to(OUT_DIR).as_posix()}",
    }


def _run_job(job_id: str, game: str, mode: str, seed, params: dict, cast: list) -> None:
    """在后台线程里跑一局。"""
    from core.runner import run_game
    job_dir = OUT_DIR / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    override = dict(params)
    if cast:
        override["cast"] = cast
    with _jobs_lock:
        _jobs[job_id]["out_dir"] = str(job_dir)
    try:
        _load_secrets_to_env()
        summary = run_game(game, mode=mode, seed=seed, out_dir=str(job_dir),
                           config_override=override or None)
        log_path = _find_log_in_dir(job_dir)
        with _jobs_lock:
            _jobs[job_id].update({
                "status": "done",
                "summary": summary,
                "log_url": f"/out/{log_path.relative_to(OUT_DIR).as_posix()}"
                           if log_path else None,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            })
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id].update({
                "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(),
            })


def _list_logs() -> list[dict]:
    """递归扫描 out/ 下所有 jsonl（含 samples / jobs）。"""
    if not OUT_DIR.exists():
        return []
    logs = []
    for p in sorted(OUT_DIR.rglob("*.jsonl"), key=lambda x: x.stat().st_mtime,
                    reverse=True):
        rel = p.relative_to(OUT_DIR).as_posix()
        # 从文件名提取游戏名：stamp_game.jsonl
        m = re.match(r"\d{8}_\d{6}_(.+)\.jsonl$", p.name)
        game = m.group(1) if m else p.stem
        logs.append({
            "name": p.name,
            "url": f"/out/{rel}",
            "game": game,
            "mtime": datetime.fromtimestamp(p.stat().st_mtime)
                     .strftime("%Y-%m-%d %H:%M"),
            "size_kb": round(p.stat().st_size / 1024, 1),
        })
    return logs


# ---------------------------------------------------------------- HTTP 处理
class Handler(BaseHTTPRequestHandler):
    server_version = "AiGameTheater/1.0"

    # ---- 工具 ----
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, code=200, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, fs_path: Path):
        # 路径穿越防护：必须在允许的根目录内
        fs_path = fs_path.resolve()
        try:
            fs_path.relative_to(ROOT)
        except ValueError:
            self._send_text("Forbidden", 403)
            return
        if not fs_path.exists() or not fs_path.is_file():
            self._send_text("Not Found", 404)
            return
        ctype, _ = mimetypes.guess_type(str(fs_path))
        self._send_text(fs_path.read_bytes(), 200, ctype or "application/octet-stream")

    def _read_body_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def log_message(self, *args):
        pass  # 静默默认日志，由前端控制台输出

    # ---- 路由 ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/" or path == "/app" or path == "/console":
            return self._send_static(WEB_DIR / "index.html")
        if path == "/api/games":
            return self._send_json({"games": _list_games()})
        if path == "/api/personas":
            return self._send_json({"personas": _list_personas()})
        if path == "/api/secrets":
            return self._send_json(self._secrets_view())
        if path == "/api/logs":
            return self._send_json({"logs": _list_logs()})
        if path == "/api/health":
            return self._send_json({"ok": True, "time": datetime.now().isoformat()})

        m = re.match(r"^/api/jobs/([^/]+)$", path)
        if m:
            return self._send_json(self._job_view(m.group(1)))

        # 静态资源：/web/... /viewer/... /out/...
        for prefix, root_dir in (("/web/", WEB_DIR), ("/viewer/", VIEWER_DIR),
                                 ("/out/", OUT_DIR)):
            if path.startswith(prefix):
                rel = path[len(prefix):]
                return self._send_static(root_dir / rel)
        self._send_text("Not Found", 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/secrets":
                data = self._read_body_json()
                _save_secrets(data)
                _load_secrets_to_env()
                return self._send_json({"ok": True})
            if path == "/api/run":
                return self._handle_run(self._read_body_json())
            self._send_text("Not Found", 404)
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 400)

    # ---- 业务 ----
    def _secrets_view(self) -> dict:
        existing = {}
        if SECRETS_FILE.exists():
            try:
                existing = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        keys = []
        for label, var in KEY_FIELDS:
            val = existing.get(var) or os.environ.get(var, "")
            keys.append({
                "label": label,
                "var": var,
                "set": bool(val),
                "value": "",  # 永不回显明文，安全
            })
        return {"keys": keys}

    def _handle_run(self, body: dict):
        game = body.get("game")
        mode = body.get("mode", "mock")
        seed = body.get("seed")
        params = body.get("params", {}) or {}
        cast = body.get("cast", []) or []
        if not game:
            return self._send_json({"error": "缺少 game 参数"}, 400)
        if mode not in ("mock", "cheap", "official"):
            return self._send_json({"error": "mode 必须是 mock/cheap/official"}, 400)
        if cast and len(cast) < 2:
            return self._send_json({"error": "阵容至少 2 人"}, 400)
        if mode != "mock":
            _load_secrets_to_env()
        seed_int = int(seed) if seed not in (None, "") else None
        job_id = _new_job_id()
        with _jobs_lock:
            _jobs[job_id] = {
                "job_id": job_id,
                "status": "running",
                "game": game,
                "mode": mode,
                "seed": seed_int,
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }
        t = threading.Thread(target=_run_job,
                             args=(job_id, game, mode, seed_int, params, cast),
                             daemon=True)
        t.start()
        return self._send_json({"job_id": job_id})

    def _job_view(self, job_id: str) -> dict:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job:
                return {"error": "任务不存在", "job_id": job_id}
            job = dict(job)
        # 实时进度：tail 日志
        out_dir = Path(job.get("out_dir", "")) if job.get("out_dir") else None
        log_path = _find_log_in_dir(out_dir) if out_dir else None
        progress = _tail_log_summary(log_path)
        job["progress"] = progress
        return job


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 游戏剧场 · 控制台")
    parser.add_argument("--port", type=int, default=8770, help="监听端口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    args = parser.parse_args()

    _load_secrets_to_env()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "jobs").mkdir(parents=True, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"🎮 AI 游戏剧场 控制台已启动：{url}")
    print(f"   按 Ctrl+C 停止。日志/素材输出在 out/ 目录。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.shutdown()


if __name__ == "__main__":
    main()
