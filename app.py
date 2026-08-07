#!/usr/bin/env python3
"""AI 游戏剧场 · 控制台后端（零第三方依赖，仅用标准库）。

提供静态文件服务 + JSON API，把 run_game 的能力暴露成 Web UI：
  - 游戏列表 / 阵容 / 配置参数（动态读取 games/*/config.yaml）
  - 模型密钥管理（.providers.json，不入库）
  - 后台开跑 + 实时进度（tail 日志）
  - 历史对局回放（集成 viewer）

启动：python app.py            默认 http://localhost:8770
      python app.py --port 9000
"""
from __future__ import annotations

import argparse
import json
import mimetypes
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
PROVIDERS_FILE = ROOT / ".providers.json"

# ---------------------------------------------------------------- 任务管理
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _new_job_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_") + f"{int(time.time() * 1000) % 10000:04d}"


# ---------------------------------------------------------------- Provider 管理

def _load_providers() -> list[dict]:
    """读取 .providers.json，返回 provider 列表。"""
    if not PROVIDERS_FILE.exists():
        return []
    try:
        data = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
        return data.get("providers", [])
    except Exception:
        return []


def _save_providers(providers: list[dict]) -> None:
    PROVIDERS_FILE.write_text(
        json.dumps({"providers": providers}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _mask_key(key: str) -> str:
    """脱敏显示 API Key。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:3] + "…" + key[-4:]


def _provider_public(p: dict) -> dict:
    """返回不含明文密钥的 provider 信息（给前端）。"""
    return {
        "id": p["id"],
        "name": p.get("name", ""),
        "protocol": p.get("protocol", ""),
        "base_url": p.get("base_url", ""),
        "model": p.get("model", ""),
        "api_key_set": bool(p.get("api_key")),
        "api_key_hint": _mask_key(p.get("api_key", "")),
        "pricing": p.get("pricing"),
    }


def _provider_full(p: dict) -> dict:
    """返回完整 provider 信息（含密钥，仅内部用）。"""
    return dict(p)


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
        params = {k: v for k, v in cfg.items() if k not in ("name", "cast", "rules")}
        out.append({
            "dir": d.name,
            "name": cfg.get("name", d.name),
            "rules": cfg.get("rules", "").strip(),
            "params": params,
            "cast": cfg.get("cast", []),
        })
    return out


def _load_persona_data() -> dict:
    """读取 personas.yaml 全部内容。"""
    import yaml
    path = ROOT / "core" / "personas.yaml"
    if not path.exists():
        return {"actors": [], "personas": [], "default_bindings": {}}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _save_persona_library(personas: list[dict]) -> None:
    """把人设库写回 personas.yaml（保留 actors / default_bindings 不变）。"""
    import yaml
    data = _load_persona_data()
    data["personas"] = personas
    path = ROOT / "core" / "personas.yaml"
    path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False,
                              sort_keys=False), encoding="utf-8")


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


def _run_job(job_id: str, game: str, mode: str, seed, params: dict,
             cast: list, provider_map: dict | None,
             persona_map: dict | None) -> None:
    """在后台线程里跑一局。provider_map: {actor_id: provider_id}。"""
    from core.runner import run_game
    job_dir = OUT_DIR / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    override = dict(params)
    if cast:
        override["cast"] = cast

    # 非 mock 模式：把 provider_id 翻译成完整 provider 配置
    full_provider_map = None
    if mode != "mock" and provider_map:
        all_providers = {p["id"]: p for p in _load_providers()}
        full_provider_map = {}
        for pid, prov_id in provider_map.items():
            if prov_id not in all_providers:
                with _jobs_lock:
                    _jobs[job_id].update({
                        "status": "error",
                        "error": f"Provider {prov_id!r} 不存在（演员 {pid}）",
                    })
                return
            full_provider_map[pid] = _provider_full(all_providers[prov_id])

    with _jobs_lock:
        _jobs[job_id]["out_dir"] = str(job_dir)
    try:
        summary = run_game(game, mode=mode, seed=seed, out_dir=str(job_dir),
                           config_override=override or None,
                           provider_map=full_provider_map,
                           persona_map=persona_map)
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
        body = fs_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
            data = _load_persona_data()
            return self._send_json({
                "actors": data.get("actors", []),
                "personas": data.get("personas", []),
                "default_bindings": data.get("default_bindings", {}),
            })
        if path == "/api/providers":
            return self._send_json({"providers": [_provider_public(p)
                                       for p in _load_providers()]})
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
            if path == "/api/providers":
                return self._handle_provider_save(self._read_body_json())
            if path == "/api/providers/delete":
                return self._handle_provider_delete(self._read_body_json())
            if path == "/api/providers/test":
                return self._handle_provider_test(self._read_body_json())
            if path == "/api/personas/save":
                return self._handle_persona_save(self._read_body_json())
            if path == "/api/personas/delete":
                return self._handle_persona_delete(self._read_body_json())
            if path == "/api/personas/bindings":
                return self._handle_persona_bindings(self._read_body_json())
            if path == "/api/logs/delete":
                return self._handle_log_delete(self._read_body_json())
            if path == "/api/logs/rename":
                return self._handle_log_rename(self._read_body_json())
            if path == "/api/run":
                return self._handle_run(self._read_body_json())
            self._send_text("Not Found", 404)
        except Exception as e:
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 400)

    # ---- 业务 ----

    def _handle_provider_save(self, body: dict):
        """创建或更新 provider。有 id 则更新，无 id 则新建。"""
        import uuid
        providers = _load_providers()
        pid = body.get("id", "").strip()
        name = (body.get("name") or "").strip()
        protocol = (body.get("protocol") or "").strip()
        base_url = (body.get("base_url") or "").strip()
        model = (body.get("model") or "").strip()
        api_key = (body.get("api_key") or "").strip()
        pricing = body.get("pricing")

        if not name:
            return self._send_json({"error": "名称不能为空"}, 400)
        if protocol not in ("openai", "anthropic", "google"):
            return self._send_json({"error": "协议类型必须是 openai / anthropic / google"}, 400)
        if not model:
            return self._send_json({"error": "模型 ID 不能为空"}, 400)

        # 新建
        if not pid:
            pid = "prov_" + uuid.uuid4().hex[:12]
            provider = {
                "id": pid, "name": name, "protocol": protocol,
                "base_url": base_url, "model": model, "api_key": api_key,
                "pricing": pricing,
            }
            providers.append(provider)
        else:
            # 更新
            found = None
            for p in providers:
                if p["id"] == pid:
                    found = p
                    break
            if not found:
                return self._send_json({"error": f"Provider {pid!r} 不存在"}, 404)
            found["name"] = name
            found["protocol"] = protocol
            found["base_url"] = base_url
            found["model"] = model
            if api_key:
                found["api_key"] = api_key  # 空字符串表示不修改
            if pricing is not None:
                found["pricing"] = pricing

        _save_providers(providers)
        return self._send_json({"ok": True, "id": pid})

    def _handle_provider_delete(self, body: dict):
        pid = body.get("id", "").strip()
        if not pid:
            return self._send_json({"error": "缺少 id"}, 400)
        providers = _load_providers()
        new_list = [p for p in providers if p["id"] != pid]
        if len(new_list) == len(providers):
            return self._send_json({"error": f"Provider {pid!r} 不存在"}, 404)
        _save_providers(new_list)
        return self._send_json({"ok": True})

    def _handle_provider_test(self, body: dict):
        """测试 Provider 连接：发一个最小请求验证连通性。"""
        from core.llm import LLMClient, LLMError, DEFAULT_PRICING
        protocol = (body.get("protocol") or "").strip()
        base_url = (body.get("base_url") or "").strip()
        model = (body.get("model") or "").strip()
        api_key = (body.get("api_key") or "").strip()
        pid = body.get("id", "").strip()

        # 仅传 id 时（从列表直接测试），从已保存配置加载所有字段
        if pid:
            for p in _load_providers():
                if p["id"] == pid:
                    if not protocol:
                        protocol = p.get("protocol", "")
                    if not base_url:
                        base_url = p.get("base_url", "")
                    if not model:
                        model = p.get("model", "")
                    if not api_key:
                        api_key = p.get("api_key", "")
                    break

        if protocol not in ("openai", "anthropic", "google"):
            return self._send_json({"ok": False, "error": "协议类型必须是 openai / anthropic / google"})
        if not model:
            return self._send_json({"ok": False, "error": "模型 ID 不能为空"})
        if not api_key:
            return self._send_json({"ok": False, "error": "API Key 不能为空"})

        try:
            client = LLMClient(
                protocol=protocol, model=model, api_key=api_key,
                base_url=base_url or None, max_retries=1,
            )
            # 发一个最小请求：要求模型回复 "OK"
            text, usage = client._raw_call(
                "You are a connectivity test. Reply with exactly: OK",
                "Reply with exactly one word: OK"
            )
            return self._send_json({
                "ok": True,
                "response": text[:200],
                "usage": {"input": usage[0], "output": usage[1]} if usage else None,
            })
        except LLMError as e:
            return self._send_json({"ok": False, "error": str(e)})
        except Exception as e:
            return self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"})

    # ---- 人设库 CRUD ----

    def _handle_persona_save(self, body: dict):
        """创建或更新人设。有 id 则更新，无 id 则新建。"""
        import uuid
        personas = _load_persona_data().get("personas", [])
        pid = (body.get("id") or "").strip()
        name = (body.get("name") or "").strip()
        personality = (body.get("personality") or "").strip()

        if not name:
            return self._send_json({"error": "人设名称不能为空"}, 400)
        if not personality:
            return self._send_json({"error": "人设描述不能为空"}, 400)

        if not pid:
            pid = "p_" + uuid.uuid4().hex[:10]
            personas.append({"id": pid, "name": name, "personality": personality})
        else:
            found = None
            for p in personas:
                if p["id"] == pid:
                    found = p
                    break
            if not found:
                return self._send_json({"error": f"人设 {pid!r} 不存在"}, 404)
            found["name"] = name
            found["personality"] = personality

        _save_persona_library(personas)
        return self._send_json({"ok": True, "id": pid})

    def _handle_persona_delete(self, body: dict):
        pid = (body.get("id") or "").strip()
        if not pid:
            return self._send_json({"error": "缺少 id"}, 400)
        data = _load_persona_data()
        personas = data.get("personas", [])
        new_list = [p for p in personas if p["id"] != pid]
        if len(new_list) == len(personas):
            return self._send_json({"error": f"人设 {pid!r} 不存在"}, 404)
        # 同时清理 default_bindings 中对此人设的引用
        bindings = data.get("default_bindings", {})
        bindings = {k: v for k, v in bindings.items() if v != pid}
        data["personas"] = new_list
        data["default_bindings"] = bindings
        import yaml
        path = ROOT / "core" / "personas.yaml"
        path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False,
                                  sort_keys=False), encoding="utf-8")
        return self._send_json({"ok": True})

    def _handle_persona_bindings(self, body: dict):
        """保存默认绑定（演员 → 人设）到 personas.yaml。"""
        import yaml
        bindings = body.get("bindings") or {}
        if not isinstance(bindings, dict):
            return self._send_json({"error": "bindings 必须是对象"}, 400)
        data = _load_persona_data()
        data["default_bindings"] = bindings
        path = ROOT / "core" / "personas.yaml"
        path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False,
                                  sort_keys=False), encoding="utf-8")
        return self._send_json({"ok": True})

    def _resolve_log_path(self, url: str) -> Path | None:
        """从 /out/... URL 解析出安全的文件路径，防止路径穿越。"""
        if not url or not url.startswith("/out/"):
            return None
        rel = url[len("/out/"):]
        fs_path = (OUT_DIR / rel).resolve()
        try:
            fs_path.relative_to(OUT_DIR.resolve())
        except ValueError:
            return None
        return fs_path if fs_path.exists() else None

    def _handle_log_delete(self, body: dict):
        url = (body.get("url") or "").strip()
        fs_path = self._resolve_log_path(url)
        if not fs_path:
            return self._send_json({"error": "文件不存在"}, 404)
        # 删除 jsonl 及同名的 srt / 草稿文件
        deleted = [fs_path.name]
        for ext in (".speech.srt", ".thought.srt"):
            sibling = fs_path.with_suffix(ext)
            if sibling.exists():
                sibling.unlink()
                deleted.append(sibling.name)
        fs_path.unlink()
        return self._send_json({"ok": True, "deleted": deleted})

    def _handle_log_rename(self, body: dict):
        url = (body.get("url") or "").strip()
        new_name = (body.get("new_name") or "").strip()
        if not new_name:
            return self._send_json({"error": "新文件名不能为空"}, 400)
        if not new_name.endswith(".jsonl"):
            new_name += ".jsonl"
        # 防止非法字符
        if re.search(r'[<>:"/\\|?*]', new_name):
            return self._send_json({"error": "文件名包含非法字符"}, 400)
        fs_path = self._resolve_log_path(url)
        if not fs_path:
            return self._send_json({"error": "文件不存在"}, 404)
        new_path = fs_path.parent / new_name
        if new_path.exists():
            return self._send_json({"error": "目标文件名已存在"}, 409)
        # 重命名 jsonl 及关联的 srt 文件
        old_stem = fs_path.stem
        fs_path.rename(new_path)
        for ext in (".speech.srt", ".thought.srt"):
            old_srt = fs_path.parent / (old_stem + ext)
            if old_srt.exists():
                new_srt = new_path.parent / (new_path.stem + ext)
                old_srt.rename(new_srt)
        new_url = f"/out/{new_path.relative_to(OUT_DIR).as_posix()}"
        return self._send_json({"ok": True, "new_url": new_url, "new_name": new_name})

    def _handle_run(self, body: dict):
        game = body.get("game")
        mode = body.get("mode", "mock")
        seed = body.get("seed")
        params = body.get("params", {}) or {}
        cast = body.get("cast", []) or []
        provider_map = body.get("provider_map") or None
        persona_map = body.get("persona_map") or None
        if not game:
            return self._send_json({"error": "缺少 game 参数"}, 400)
        if mode not in ("mock", "cheap", "official"):
            return self._send_json({"error": "mode 必须是 mock/cheap/official"}, 400)
        if cast and len(cast) < 2:
            return self._send_json({"error": "阵容至少 2 人"}, 400)
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
                             args=(job_id, game, mode, seed_int, params, cast,
                                   provider_map, persona_map),
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

    _load_providers()  # 确保文件可读
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
