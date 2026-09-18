# -*- coding: utf-8 -*-
"""冒烟：「会话区字体」（设置 → 外观 → 会话字体：字号 13-18 + 字体族点选列表）自编排入口。

用**独立数据目录**与独立端口起源码 server（复用 public/ 真实资源），跑完即清理：
播种 1 个在线 API + 1 个带问答的会话，浏览器侧做真实交互（真拖滑杆、真点 radio、
真在下拉里选字体、真敲文本框），断言「面板控件 → CSS 变量 → 消息正文计算样式」整条链路。

断言重点（都是"设置看着存了、但用户看不到效果"的静默失败面）：
- 默认值必须与历史硬编码值等价（15px / 界面字体），老用户视觉零变化；
- 拖滑杆**即时**改消息区字号，且**不落库**（绝不一像素一个 API）；
- 点选列表的选项、顺序与「（未安装）」标注**与 JS 常量/现场 canvas 度量逐项一致**
  （只比对 DOM 文案 = 自己验自己）；
- 选预置字体 → 正文真的换渲染字体（比对 canvas 度量，不只读声明）；
- 占位值 `__pick__` 永不落库；选预置键不抹掉用户手敲过的字体串；
- 「保存外观」写库 → 清掉 localStorage 后刷新仍生效（证明服务端才是真相来源）；
- 刷新后首帧就是正确字号（localStorage 缓存，移动端不闪）；
- 只改主题时字号字体不被重置；
- 手机竖屏下控件可达可操作、字号与字体同样生效并能落库。

用法：.venv\\Scripts\\python.exe verify\\chat_font_smoke.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # 直接跑本脚本时也能 import naiba
PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"
ISOLATED_ROOT = Path(os.environ.get("NAIBA_SMOKE_ROOT", ROOT / "verify" / "chat_font_tmp"))
DATA_DIR = ISOLATED_ROOT / "data"
PROVIDER = {"id": "font-smoke-a", "name": "字体冒烟 API", "model": "font-smoke-default"}
BOUND_MODEL = "font-smoke-pro"
TITLE = "会话字体冒烟"
Q1 = "会话字体冒烟：请回一句话"
A1 = "这是用于测量字号与字体族的 AI 回复正文。The quick brown fox jumps over the lazy dog."


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


def seed() -> None:
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
        "base_url": "https://example.invalid/v1",
        "api_key": "sk-smoke",
        "request_format": "openai_chat",
        "model": PROVIDER["model"],
    })
    # 只有一个会话：启动时会自动打开它，冒烟不必驱动侧栏（虚拟列表 + 默认折叠，点开条目反而脆弱）。
    conversation = storage.create_conversation(
        TITLE,
        model_key=f"online:{PROVIDER['id']}",
        model_name=BOUND_MODEL,
        workspace_dir=str(ISOLATED_ROOT / "workspace"),
    )
    storage.add_message(conversation["id"], "user", Q1)
    storage.add_message(conversation["id"], "assistant", A1)


def read_saved_appearance() -> dict:
    """读隔离实例落盘的 config.json（"真的写库了"的最终证据）。"""
    import json

    path = ISOLATED_ROOT / "config.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("appearance") or {}


def main() -> int:
    global ISOLATED_ROOT, DATA_DIR
    isolated = tempfile.TemporaryDirectory(prefix="chat_font_smoke_", dir=ROOT / "verify")
    ISOLATED_ROOT = Path(isolated.name)
    DATA_DIR = ISOLATED_ROOT / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    code = 1
    server: subprocess.Popen | None = None
    log = (ROOT / "verify" / "chat_font_smoke_server.log").open("w", encoding="utf-8")
    try:
        seed()
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
        node_env["NODE_PATH"] = str(Path.home() / "node_modules")
        node_env["NAIBA_SMOKE_BASE"] = BASE
        node_env["NAIBA_SMOKE_ANSWER"] = A1
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "chat_font_smoke.cjs")],
            cwd=str(ROOT), env=node_env, check=False,
        )
        code = node.returncode
        saved = read_saved_appearance()
        # 最后一步（手机竖屏）会再存一次「点选 lxgw + 13px」，所以这里钉的是那一版：
        # 字族是预置键（不是 __pick__ 占位）、字号落在契约区间、手敲过的串仍被保留
        # （选预置键时不传 custom 字段，后端因此原样留着）。
        size = saved.get("chat_font_size")
        ok = (saved.get("chat_font_family") == "lxgw"
              and saved.get("chat_font_family_custom") == "LXGW WenKai"
              and isinstance(size, int) and 13 <= size <= 18)
        print(f"{'PASS' if ok else 'FAIL'}  隔离实例 config.json 已落盘：{saved}")
        if not ok:
            code = 1
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
        print(f"已清理独立测试目录（开发 config.json 未修改）"
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
