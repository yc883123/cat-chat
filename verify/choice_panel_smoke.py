# -*- coding: utf-8 -*-
"""冒烟：前端「选择交互面板」（单选 / 多选 / 多组）的真前端验证入口。

自编排：**独立临时根**（`verify/choice_panel_smoke_*/`，跑完即删）+ **独立端口 8797** 起源码 server，
播种 3 个会话（两题 / 单题 / 长文本）+ 1 个在线 API，浏览器侧拦 `/api/providers/models`
（让「发送前置校验」成立）与 `POST /api/chat`（按场景回最小 NDJSON，不真调模型）。

判据分工（与 `regenerate_smoke.py` 同口径）：
- **面板行为全部由真前端决定**：题目/标识/进度/已选摘要/上一题/完成/折叠/草稿追加/内存键，
  都由 `verify/choice_panel_smoke.cjs` 在真实浏览器里点出来并断言；
- **后端只提供历史真值**：会话与带 `choice_groups` 的消息由 `ChatStorage` 播种，
  面板「从历史恢复」「提交后失效」都以历史接口的真值为准；
- **`POST /api/chat` 全程被拦截**，因此「点完成不发聊天请求」是真断言（拦截计数为 0）。

用法：`.venv\\Scripts\\python.exe verify\\choice_panel_smoke.py`
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # 直接跑本脚本时也能 import naiba
PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"
ISOLATED_ROOT = Path(os.environ.get("NAIBA_SMOKE_ROOT", ROOT / "verify" / "choice_panel_tmp"))
DATA_DIR = ISOLATED_ROOT / "data"
SHOTS_DIR = ROOT / "verify" / "choice_panel_shots"
PROVIDER = {"id": "smoke-choice", "name": "冒烟 API（选择面板）", "model": "smoke-default"}
BOUND_MODEL = "smoke-pro"
IMG_NAME = "smoke-choice-att.png"

# 1x1 透明 PNG：够让 `mediaKind` 判成图片、让浏览器真解码（避免 404 在控制台留 error）。
IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

TITLE_MAIN = "选择面板冒烟：两题"
TITLE_OTHER = "选择面板冒烟：单题"
TITLE_LONG = "选择面板冒烟：长文本"
U_MAIN = "第一问：这版内容怎么配"
U_OTHER = "B 会话：推送走哪条路"
U_LONG = "第三问：选一版封面"

# 两题：1 单选 + 1 多选（主流程；`choices` 字段按后端落库口径带第一组，metadata 才算权威）。
GROUPS_MAIN = [
    {"prompt": "视觉风格", "choices": ["写实摄影", "动漫插画"], "mode": "single", "source": "natural"},
    {"prompt": "文章形式（可多选）", "choices": ["教程", "评测", "案例拆解"], "mode": "multi", "source": "natural"},
]
# 单题：用来验「切换会话不串题」——题面与选项都和两题那份完全不同。
GROUPS_OTHER = [
    {"prompt": "推送渠道", "choices": ["邮件", "短信", "站内信"], "mode": "single", "source": "natural"},
]
# 长文本：题目 200+ 字、选项 400+ 字（原 40 字上限会把整组漏掉 —— 本次改造的放宽上限内）。
LONG_PROMPT = "请选择这一版内容的整体气质：" + "说明" * 90 + "（也可直接告诉我你的想法）"
LONG_CHOICES = ["偏理性：" + "细节" * 130, "偏感性：" + "氛围" * 130]
GROUPS_LONG = [
    {"prompt": LONG_PROMPT, "choices": LONG_CHOICES, "mode": "single", "source": "natural"},
]


def wait_health(deadline: float = 60.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 启动期连接失败是预期分支
            time.sleep(0.5)
    return False


def isolated_paths():
    from naiba.paths import PathContext

    paths = PathContext.local(ISOLATED_ROOT, ISOLATED_ROOT / "config.json")
    paths.public_dir = ROOT / "public"
    return paths


def _seed_conversation(storage, title: str, question: str, groups: list[dict]) -> tuple[str, str]:
    """播种一个「末条助手消息带 choice_groups」的会话，返回 (会话 id, 该消息 id)。"""
    conversation = storage.create_conversation(
        title,
        model_key=f"online:{PROVIDER['id']}",
        model_name=BOUND_MODEL,
        workspace_dir=str(ISOLATED_ROOT / "workspace"),
    )
    storage.add_message(conversation["id"], "user", question)
    metadata = {
        "choices": list(groups[0]["choices"]),          # 旧字段：后端落库时同源带出
        "choice_groups": groups,                         # 权威数据（历史读取优先用它）
    }
    message = storage.add_message(
        conversation["id"], "assistant",
        "先确认两个选择，然后我按你的选择继续。" if len(groups) > 1 else "先确认一个选择，然后我继续。",
        metadata,
    )
    return conversation["id"], str(message["id"])


def seed() -> dict:
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.storage.store import ChatStorage
    from types import SimpleNamespace

    paths = isolated_paths()
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({"host": "127.0.0.1", "port": PORT, "access_token": "",
                        "data_dir": str(DATA_DIR), "workspace_dir": str(ISOLATED_ROOT / "workspace"),
                        "skills_dirs": [str(ROOT / "skills")], "mcp_servers": []})
    config.save()
    storage = ChatStorage(DATA_DIR / "chat.db")
    app = SimpleNamespace(config=config, storage=storage)
    NaibaChatApp.api_upsert_model_profile(app, {
        "id": PROVIDER["id"],
        "kind": "online",
        "name": PROVIDER["name"],
        "base_url": "https://example.invalid/v1",   # 目录/模型请求都在浏览器侧被拦截，永不真连
        "api_key": "sk-smoke",
        "request_format": "openai_chat",
        "model": PROVIDER["model"],
    })
    uploads = DATA_DIR / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    # 附件夹具故意放在 uploads **之外**：让它走真实上传管线（/api/uploads → 分日目录 +
    # <stem>_thumb.webp）。若预先塞进 uploads，`store_uploaded_file` 的内容级去重会命中
    # 这份手写文件（它没有缩略图，`_thumb_path_for` 只能回空串），前端再按
    # `<stem>_thumb.webp` 推导 → 必然 404（控制台报错 + 破图兜底）。
    fixture_dir = ISOLATED_ROOT / "fixture"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / IMG_NAME).write_bytes(IMAGE_BYTES)
    (ISOLATED_ROOT / "workspace").mkdir(parents=True, exist_ok=True)

    # 播种顺序即「最近更新」顺序：最后播种的会排在会话列表第一位（启动时自动打开它）。
    long_id, long_mid = _seed_conversation(storage, TITLE_LONG, U_LONG, GROUPS_LONG)
    other_id, other_mid = _seed_conversation(storage, TITLE_OTHER, U_OTHER, GROUPS_OTHER)
    main_id, main_mid = _seed_conversation(storage, TITLE_MAIN, U_MAIN, GROUPS_MAIN)
    return {
        "bound_model": BOUND_MODEL,
        "attachment": str(fixture_dir / IMG_NAME),
        "long_prompt": LONG_PROMPT,
        "long_choices": LONG_CHOICES,
        "conversations": {
            # sidebar：侧栏条目上的文案。会话标题会在首条用户消息落库时被自动命名覆盖，
            # 因此点侧栏必须用「首条用户消息」，而不是 create_conversation 传的 title。
            "main": {"id": main_id, "title": TITLE_MAIN, "sidebar": U_MAIN,
                     "message_id": main_mid, "groups": GROUPS_MAIN},
            "other": {"id": other_id, "title": TITLE_OTHER, "sidebar": U_OTHER,
                      "message_id": other_mid, "groups": GROUPS_OTHER},
            "long": {"id": long_id, "title": TITLE_LONG, "sidebar": U_LONG,
                     "message_id": long_mid, "groups": GROUPS_LONG},
        },
    }


def main() -> int:
    global ISOLATED_ROOT, DATA_DIR
    isolated = tempfile.TemporaryDirectory(prefix="choice_panel_smoke_", dir=ROOT / "verify")
    ISOLATED_ROOT = Path(isolated.name)
    DATA_DIR = ISOLATED_ROOT / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    # 清掉上一轮的截图：失败时看截图判断卡在哪一步，残留旧图容易看成"这次的结果"。
    for stale in SHOTS_DIR.glob("*.png"):
        stale.unlink()
    code = 1
    server: subprocess.Popen | None = None
    log = (ROOT / "verify" / "choice_panel_smoke_server.log").open("w", encoding="utf-8")
    try:
        spec = seed()
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                    "NAIBA_SMOKE_ROOT": str(ISOLATED_ROOT)})
        server = subprocess.Popen(  # noqa: S603 - 固定 argv
            [sys.executable, str(Path(__file__).resolve()), "--serve"],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if not wait_health():
            print(f"server 未就绪（日志见 {log.name}）")
            return 1
        node_env = dict(os.environ)
        node_env["NODE_PATH"] = str(ROOT / "node_modules")
        node_env["NAIBA_SMOKE_BASE"] = BASE
        node_env["NAIBA_SMOKE_SPEC"] = json.dumps(spec, ensure_ascii=False)
        node_env["NAIBA_SMOKE_SHOTS"] = str(SHOTS_DIR)
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "choice_panel_smoke.cjs")],
            cwd=str(ROOT), env=node_env, check=False,
        )
        code = node.returncode
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        log.close()
        isolated.cleanup()
        print("已清理独立测试目录（开发 config.json 未修改）"
              f"；server 已退出（exit={server.returncode if server else 'n/a'}）")
    return code


if __name__ == "__main__":
    if "--serve" in sys.argv:
        from naiba.app import NaibaChatApp
        from naiba.http import AppHTTPServer, RequestHandler

        app = NaibaChatApp(paths=isolated_paths())
        server = AppHTTPServer(("127.0.0.1", PORT), RequestHandler, app)
        server.daemon_threads = True
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.server_close()
            app.stop()
        sys.exit(0)
    sys.exit(main())
