# -*- coding: utf-8 -*-
r"""教程截图后端：在**冻结版 exe 里**起一个「全新安装观感」的隔离实例。

为什么要冻结版（不是 `_serve_tmp.py` 那种源码模式）：
  教程截图必须拍**用户拿到的那份**——`sys._MEIPASS/public` 的静态资源、冻结的 naiba 包。
  源码模式读的是工作树 `public/`，两者一旦不一致，教程就会骗人（§九.128 同因）。

隔离：独立 root（config / data / workspace），独立端口，**不碰开发数据目录、不抢实例锁**。
本脚本由 exe 自己执行（不初始化 GUI / HTTP 默认服务 / 单实例锁）：

    dist\naiba-chat.exe --run-skill-script verify\_tutorial_serve.py

环境变量：
  NAIBA_TUT_ROOT  隔离根目录（必给；不给则用临时目录）
  NAIBA_TUT_PORT  监听端口（默认 8799）
  NAIBA_TUT_FIXTURE 播种内容（见 FIXTURES；默认 fresh）
  NAIBA_TUT_PROVIDERS_FROM  从这份 config.json 里拷 `providers`（真跑对话用）
  NAIBA_TUT_PROVIDER_NAMES  只挑这几个（逗号分隔；不给=全要）

**脱敏纪律**：拷进来的供应商只许用**公开品牌名**（如 `deepseek` / `mimo`）。
别把这理解成「看着像中转站的就拦」——`TE 中转` / `摆烂中转` 是
`naiba/llm/provider_presets.py` 里的**出厂预设模板**，设置页里人人可见，不算泄密。
真正不许进图的是：① 用户自配的中转站显示名（`tedsres` / `GG公益` / `大肥鱼官方` 之类）；
② 不透明的 provider id；③ 本地绝对路径、真实 Key、真实会话标题。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PORT = int(os.environ.get("NAIBA_TUT_PORT", "8799"))
FIXTURE = (os.environ.get("NAIBA_TUT_FIXTURE") or "fresh").strip().lower()
ROOT = Path(os.environ.get("NAIBA_TUT_ROOT") or "").expanduser()
PROVIDERS_FROM = (os.environ.get("NAIBA_TUT_PROVIDERS_FROM") or "").strip()
PROVIDER_NAMES = [n.strip() for n in (os.environ.get("NAIBA_TUT_PROVIDER_NAMES") or "").split(",") if n.strip()]

# 示例 API：一律用**公开品牌名**，不得出现开发机上的私有供应商名。
PROVIDERS = [
    ("tut-deepseek", "DeepSeek", "openai_chat", "online", "deepseek-chat"),
    ("tut-kimi", "Kimi", "openai_chat", "online", "kimi-k2"),
    ("tut-ollama", "本地 Ollama", "ollama", "local", "qwen2.5:7b"),
]

# 演示会话（教程正文里出现的那几段），键即 NAIBA_TUT_FIXTURE 的取值。
FIXTURES: dict[str, list[dict]] = {
    "first-chat": [
        {
            "title": "第一次对话",
            "messages": [
                ("user", "你好，帮我用一句话说说你能做什么。"),
                ("assistant", "我是全能 Agent：问答、写作、整理资料，也能读写你指定的文件、跑命令。\n\n"
                              "要动文件或跑命令前，我会先说清要改哪些东西，等你点「允许」再动手。"),
            ],
        },
    ],
}

# 演示用 MCP 服务（NAIBA_TUT_SEED_MCP=1 时播种）。
#
# 为什么用「预置注册项」而不是真跑一遍安装：教程 2 的 07-mcp 要的是「已注册的服务器长什么样」。
# 真跑「设置本地 Comfy MCP」卡会让 AI 在**本机**跑 pip install（隔离实例之外的真实副作用），
# 而这一步的结果还取决于本机 ComfyUI 装在哪儿，不确定。
#
# 为什么用中性 id 就够：`renderMcp()` 只渲染「id · 状态」+ 一行明细，
# command / args / env **不进 DOM**（`public/js/09-settings.js:2799-2808`），
# 所以这里不会漏出本机路径；`mcp_servers` 也是惰性连接（状态 idle 的文案是
# 「仅在本轮激活的 Skill 需要 MCP 时连接」），启动时不会去 spawn 进程。
DEMO_MCP_SERVERS: list[dict] = [
    {"id": "comfy-mcp", "command": "comfy-mcp", "args": [], "env": {}, "enabled": True},
]


def packed_roots() -> tuple[Path, Path]:
    """冻结资源的 public / skills 目录（唯一真相取自 _MEIPASS）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        print("拒绝：本脚本只在冻结版下有正确的资源根（源码模式请用 _serve_tmp.py）",
              file=sys.stderr)
        raise SystemExit(2)
    return Path(meipass) / "public", Path(meipass) / "skills"


def isolated_paths():
    from naiba.paths import PathContext

    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        print("拒绝：本脚本只在冻结版下有正确的资源根（源码模式请用 _serve_tmp.py）",
              file=sys.stderr)
        raise SystemExit(2)
    public_dir = packed_roots()[0]
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    (ROOT / "workspace").mkdir(parents=True, exist_ok=True)
    paths = PathContext.local(ROOT, ROOT / "config.json")
    paths.public_dir = public_dir
    # **必须同时改 resource_dir**：随包 Skill 是从 `resource_dir/skills` 推导的
    # （`app.py` 里 `bundled_skills = self._paths.resource_dir / "skills"`）。
    # 只改 public_dir 的话，随包 Skill 一个都扫不到——顶栏显示 `0 Skill`。
    paths.resource_dir = Path(meipass)
    return paths


def provider_specs() -> list[dict]:
    """要播种的 API。

    默认用**公开品牌名**的演示夹具（不真连，只撑起界面）；
    给了 `NAIBA_TUT_PROVIDERS_FROM` 就从真实配置里照抄几条（教程要真跑对话时用）。
    """
    if not PROVIDERS_FROM:
        return [
            {"id": pid, "kind": kind, "name": name,
             "base_url": "https://example.invalid/v1",
             "api_key": "" if kind == "local" else "sk-tutorial-demo",
             "request_format": fmt, "model": model}
            for pid, name, fmt, kind, model in PROVIDERS
        ]
    raw = json.loads(Path(PROVIDERS_FROM).read_text(encoding="utf-8"))
    items = [dict(x) for x in (raw.get("providers") or [])]
    if PROVIDER_NAMES:
        items = [x for x in items if str(x.get("name") or "") in PROVIDER_NAMES]
    if not items:
        raise SystemExit("NAIBA_TUT_PROVIDERS_FROM 里没挑到供应商：%r" % (PROVIDER_NAMES,))
    # 脱敏：真实 provider id 会**露在界面上**——会话首轮那张折叠卡的表头是
    # 「首次请求上下文 · <Agent> · online:<provider id> · 工具 N 个」（见教程一/五/八的截图），
    # 而 id 是本机配置里的真实条目 id（形如 eb8dc7a70d7）。统一改写成中性序号；
    # 顶栏显示的是品牌名（name），改名不动它。
    for index, item in enumerate(items, start=1):
        item["id"] = "tut%d" % index
    return items


def seed() -> list[dict]:
    """播种「全新安装观感」的 API 与演示会话，返回播种进去的 provider 列表。

    **故意不写 `skills_dirs`**：随包 Skill 目录由 app 自己推导（`app.py` 里
    `bundled_skills` = `sys._MEIPASS/skills`）。冻结版的 `_MEIPASS` 是**每次启动都变的
    临时解压目录**，把它写进 config 会让下一次启动扫到一个已删除的路径——
    表现就是顶栏 `0 Skill`（Skill 一个都不见）。让 app 自己推导才是对的。
    """
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.storage.store import ChatStorage
    from types import SimpleNamespace

    paths = isolated_paths()
    config = ConfigStore(paths.config_path, paths=paths)
    config.data.update({
        "host": "127.0.0.1",
        "port": PORT,
        "access_token": "",
        "data_dir": str(paths.data_dir),
        "workspace_dir": str(ROOT / "workspace"),
        "mcp_servers": [dict(item) for item in DEMO_MCP_SERVERS]
        if os.environ.get("NAIBA_TUT_SEED_MCP") else [],
    })
    config.data.pop("skills_dirs", None)
    config.save()

    storage = ChatStorage(paths.data_dir / "chat.db")
    app = SimpleNamespace(config=config, storage=storage)
    specs = provider_specs()
    for spec in specs:
        NaibaChatApp.api_upsert_model_profile(app, spec)
    first_online = next((s for s in specs if s.get("kind") != "local"), specs[0])
    default_key = "%s:%s" % (
        "local" if first_online.get("kind") == "local" else "online", first_online.get("id"),
    )
    config.set_default_model_key(default_key)
    for item in FIXTURES.get(FIXTURE, []):
        conv = storage.create_conversation(
            item["title"],
            model_key=default_key,
            model_name=str(first_online.get("model") or ""),
            workspace_dir=str(ROOT / "workspace"),
        )
        for role, text in item["messages"]:
            storage.add_message(conv["id"], role, text)
    config.save()
    print("[tut] providers=%d default=%s" % (len(specs), default_key), flush=True)
    return specs


def _install_sqlite_trace() -> None:
    """NAIBA_TUT_VERBOSE=1 时给 sqlite 装一层取证：记录连接路径与失败的语句。

    存在理由：run 失败被上层吞成 SSE 事件，日志里看不到 traceback，
    「attempt to write a readonly database」这类错误必须看到是**哪条连接、哪句 SQL**。
    注意 `sqlite3.Cursor` 是 C 类型、方法不可赋值，只能走 `factory=` 注入子类。
    """
    import sqlite3
    import traceback

    real_connect = sqlite3.connect
    real_execute = sqlite3.Connection.execute

    def _caller() -> str:
        """触发失败的 store.py 调用点。

        只打 SQL 是不够的：`BEGIN IMMEDIATE` 在 store.py 有 8 处，分不清命中的是
        「已包重试」的 `append_run_event` / `update_job`，还是**没包重试**的其余写路径
        （§九.145 的修复范围只覆盖那两处）。
        """
        for frame in reversed(traceback.extract_stack()):
            if frame.filename.endswith("store.py"):
                return "%s:%d" % (Path(frame.filename).name, frame.lineno)
        return "?"

    class TracedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            try:
                return real_execute(self, sql, *args, **kwargs)
            except sqlite3.Error as exc:
                db = ""
                try:
                    db = real_execute(self, "PRAGMA database_list").fetchone()[2]
                except Exception:  # noqa: BLE001 - 取证失败不该改变行为
                    pass
                print("[sqlite] FAILED %s | db=%s | caller=%s | sql=%s" % (
                    exc, db, _caller(), " ".join(str(sql).split())[:160]),
                    file=sys.stderr, flush=True)
                raise

    def traced_connect(database, *args, **kwargs):
        kwargs.setdefault("factory", TracedConnection)
        conn = real_connect(database, *args, **kwargs)
        print("[sqlite] connect %r" % (database,), file=sys.stderr, flush=True)
        return conn

    sqlite3.connect = traced_connect


def main() -> None:
    from naiba.app import NaibaChatApp
    from naiba.http import AppHTTPServer, RequestHandler

    if os.environ.get("NAIBA_TUT_VERBOSE"):
        import logging
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                            format="%(levelname)s %(name)s %(message)s")
        _install_sqlite_trace()
    paths = isolated_paths()
    app = NaibaChatApp(paths=paths)
    print(f"[tut] port={PORT} root={ROOT} data_dir={paths.data_dir} fixture={FIXTURE}", flush=True)
    print(f"[tut] storage db={getattr(app.storage, 'db_path', '?')}", flush=True)
    server = AppHTTPServer(("127.0.0.1", PORT), RequestHandler, app)
    server.daemon_threads = True
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        app.stop()


if __name__ == "__main__":
    # `--seed-only`：只播种，不起服务（推荐用法：先跑一次它，再跑一次普通的起服务，
    # 避免播种用的 ChatStorage 连接与 app 自己那条连接同时挂在同一个库上）。
    if "--seed-only" in sys.argv:
        seed()
        print("[tut] seed ok", flush=True)
        raise SystemExit(0)
    if "--no-seed" not in sys.argv:
        seed()
    main()
