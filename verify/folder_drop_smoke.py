# -*- coding: utf-8 -*-
"""「拖文件夹进输入区」端到端冒烟（自编排：隔离源码实例 + 真 HTTP + 真无头 Edge）。

为什么必须真后端（§九.92）：文件夹 chip / 索引清单 / 工作区外确认框全走真 HTTP——
静态 public/ 服务下 `/api/files/folder-index` 一律 404，而"被拦下来了"和"点一下没反应"
在截图上长得一模一样，断言会全绿却什么都没验。

两层断言：
A. HTTP 层（本文件，urllib 真发请求）：索引扫描的真实计数与截断原因、工作区外 403 的结构、
   `allow_outside=1` 之后同会话放行、非法路径的 400；
B. 浏览器层（folder_drop_smoke.cjs）：气泡只列前 10 项且可展开、拖入只出一个占位 chip、
   文件面板目录行的「＋」、工作区外先弹确认框（不允许 → 不读盘；允许 → 建 chip 且不再问）、
   零页面错误、零未解释 4xx。

用法：.venv\\Scripts\\python.exe verify\\folder_drop_smoke.py
（收尾要删隔离目录，若被沙箱批量删除护栏拦下：前面加 CODEBUDDY_SAFE_DELETE_ENABLED=0）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # A 层要 import naiba.*

PORT = int(os.environ.get("NAIBA_FOLDER_PORT", "8812"))
BASE = f"http://127.0.0.1:{PORT}"
TMP_ROOT = ROOT / "verify" / "_tmp_folder_drop"
SERVER_LOG = ROOT / "verify" / "_folder_drop_smoke_server.log"

# ---- 夹具口径（断言里逐字出现，改一处要同步改 cjs）----
INSIDE_NAME = "素材夹"
INSIDE_FILES = 5
INSIDE_IMAGES = 3
SEED_NAME = "图集"
SEED_FILES = 40          # 40 个文件 + max_entries=30 ⇒ 必然触发 count 截断
SEED_IMAGES = 12
SEED_MAX_ENTRIES = 30
SEED_TITLE = "文件夹气泡冒烟"
OUTSIDE_NAME = "外部夹"
OUTSIDE_FILES = 3

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + ("" if ok or not detail else f"  -> {detail}"))
    if not ok:
        failures.append(label)


def wait_until(predicate, timeout: float = 45.0, interval: float = 0.3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - 启动期连接失败是预期分支
            pass
        time.sleep(interval)
    return False


def request(method: str, path: str):
    """返回 (status, bytes)；4xx/5xx 也当正常返回（断言看 status）。"""
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310 - 固定本机地址
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        return 0, str(exc).encode("utf-8")


def json_of(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def q(path: str) -> str:
    return urllib.parse.quote(str(path), safe="")


def build_fixtures() -> dict[str, Path]:
    """搭三份夹具：工作区内 / 播种用的图集 / 工作区外。"""
    workspace = TMP_ROOT / "workspace"
    inside = workspace / INSIDE_NAME
    inside.mkdir(parents=True)
    for index in range(1, INSIDE_FILES + 1):
        suffix = ".png" if index <= INSIDE_IMAGES else ".txt"
        (inside / f"inside{index:02d}{suffix}").write_bytes(b"x")

    seed = TMP_ROOT / "seed" / SEED_NAME
    seed.mkdir(parents=True)
    for index in range(1, SEED_FILES + 1):
        suffix = ".png" if index <= SEED_IMAGES else ".txt"
        (seed / f"entry{index:02d}{suffix}").write_bytes(b"x")

    outside = TMP_ROOT / "outside" / OUTSIDE_NAME
    outside.mkdir(parents=True)
    for index in range(1, OUTSIDE_FILES + 1):
        (outside / f"out{index:02d}.png").write_bytes(b"x")
    return {"workspace": workspace, "inside": inside, "seed": seed, "outside": outside}


def seed_conversation(seed: Path) -> tuple[str, str, str]:
    """播种三个会话：气泡用 / A 层闸门用 / 跨会话对照用。服务启动前执行。

    为什么必须分开：工作区外的确认记录挂在会话上（`app._confirmed_folders`）。A 层要亲手
    走一遍「先 403、再允许」，如果拿气泡那个会话去走，浏览器层再拖同一个目录就**不会再问**
    ——冒烟会失败在"确认框没出现"上，而真实产品行为其实是对的（这正是本文件第一次跑出来的
    失败）。夹具之间不许互相污染状态。

    刻意不手搓索引 JSON：气泡要显示的总数/图片数/截断自述必须与后端 `folder_index` 一致。
    """
    from naiba.core.conv_files import folder_index
    from naiba.core.messages import MetadataKeys
    from naiba.storage.store import ChatStorage

    index = folder_index(seed, max_entries=SEED_MAX_ENTRIES)
    assert index["truncated"] and index["truncated_reason"] == "count", index
    storage = ChatStorage(TMP_ROOT / "data" / "chat.db")
    conversation = storage.create_conversation(title=SEED_TITLE, permission_mode="auto")
    cid = str(conversation["id"])
    storage.add_message(
        cid,
        "user",
        f"看看这个文件夹里有什么：{seed}",
        {MetadataKeys.FOLDER_INDEXES: [index], "attachments": []},
    )
    storage.add_message(cid, "assistant", "收到，我按清单来。", {})
    storage.update_conversation_settings(cid, title=SEED_TITLE)   # add_message 会用首条消息改标题
    gate = str(storage.create_conversation(title="闸门实验", permission_mode="auto")["id"])
    other = str(storage.create_conversation(title="跨会话对照", permission_mode="auto")["id"])
    print(f"已播种会话 {cid}：total={index['total']} entries={len(index['entries'])} truncated={index['truncated']}")
    return cid, gate, other


def http_layer(fixtures: dict[str, Path], cids: tuple[str, str, str]) -> None:
    """A. HTTP 层：索引口径 + 工作区外闸门（**只用 gate / other 两个会话**，别碰气泡那个）。"""
    print("---- A. HTTP 层 ----")
    _bubble_cid, gate, other = cids
    inside, outside = fixtures["inside"], fixtures["outside"]

    status, raw = request("GET", f"/api/files/folder-index?path={q(inside)}")
    payload = json_of(raw)
    check("工作区内目录直接放行（200）", status == 200, f"{status} {raw[:160]!r}")
    check(f"计数是真实值：{INSIDE_FILES} 项 / {INSIDE_IMAGES} 图",
          payload.get("total") == INSIDE_FILES and payload.get("image_count") == INSIDE_IMAGES, str(payload)[:200])
    check("条目是相对路径（不是绝对路径）",
          all(not Path(entry["rel"]).is_absolute() for entry in payload.get("entries") or []),
          str(payload.get("entries"))[:200])

    status, raw = request("GET", f"/api/files/folder-index?path={q(outside)}&conversation_id={gate}")
    payload = json_of(raw)
    check("工作区外目录先回 403 + needs_confirm",
          status == 403 and payload.get("needs_confirm") is True, f"{status} {payload}")
    check("403 里带上路径（前端才能回填确认框）", payload.get("path") == str(outside), str(payload.get("path")))

    status, raw = request("GET", f"/api/files/folder-index?path={q(outside)}")
    check("会话内已被拦过的目录，在没有会话时同样要拦", status == 403, f"{status}")

    status, raw = request("GET", f"/api/files/folder-index?path={q(outside)}&conversation_id={gate}&allow_outside=1")
    payload = json_of(raw)
    check("点过「允许」之后放行（200）", status == 200 and payload.get("total") == OUTSIDE_FILES,
          f"{status} {str(payload)[:160]}")

    status, raw = request("GET", f"/api/files/folder-index?path={q(outside)}&conversation_id={gate}")
    check("确认按会话记住：同会话再问一次就放行", status == 200, f"{status}")

    status, raw = request("GET", f"/api/files/folder-index?path={q(outside)}&conversation_id={other}")
    check("确认不跨会话泄漏", status == 403, f"{status}")

    status, raw = request("GET", f"/api/files/folder-index?path={q(fixtures['seed'] / 'entry01.png')}&conversation_id={gate}")
    check("文件（不是目录）被拒（400 + 中文文案）",
          status == 400 and "目录" in json_of(raw).get("error", ""), f"{status} {json_of(raw)}")
    status, raw = request("GET", f"/api/files/folder-index?path={q(TMP_ROOT / '不存在')}&conversation_id={gate}")
    check("不存在的路径被拒（400）", status == 400, f"{status} {json_of(raw)}")
    status, raw = request("GET", f"/api/files/folder-index?path={q(outside)}&conversation_id=conv-not-exist")
    check("不存在的会话 → 404（与 403 区分开）", status == 404, f"{status} {json_of(raw)}")


def browser_layer() -> int:
    """B. 浏览器层。"""
    print("---- B. 浏览器层 ----")
    env = dict(os.environ)
    env.update({
        "NAIBA_SMOKE_BASE": BASE,
        "NAIBA_FOLDER_CONV_TITLE": SEED_TITLE,
        "NAIBA_FOLDER_INSIDE": str(TMP_ROOT / "workspace" / INSIDE_NAME),
        "NAIBA_FOLDER_OUTSIDE": str(TMP_ROOT / "outside" / OUTSIDE_NAME),
        "NAIBA_FOLDER_SEED": str(TMP_ROOT / "seed" / SEED_NAME),
        "NAIBA_FOLDER_PICK": INSIDE_NAME,
        "NAIBA_SMOKE_SHOTS": str(ROOT / "verify"),
        "NODE_PATH": str(ROOT / "node_modules"),
    })
    try:
        proc = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "folder_drop_smoke.cjs")],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("找不到 node：本机 node 装在 D:\\Apps\\NodeJS，需先加进 PATH")
        return 1
    print(proc.stdout or "", end="")
    if proc.stderr.strip():
        print("--- node stderr ---")
        print(proc.stderr)
    return proc.returncode


def main() -> int:
    # 每轮全新隔离根：启动前删才是最可靠的"干净"（跑完再删可能被沙箱批量删除护栏拦下）。
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    (TMP_ROOT / "data").mkdir(parents=True, exist_ok=True)
    fixtures = build_fixtures()
    cids = seed_conversation(fixtures["seed"])

    log = SERVER_LOG.open("w", encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "NAIBA_TMP_ROOT": str(TMP_ROOT),
        "NAIBA_TMP_PORT": str(PORT),
    })
    server = subprocess.Popen(  # noqa: S603 - 固定 argv
        [sys.executable, str(ROOT / "verify" / "_serve_tmp.py")],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        if not wait_until(lambda: request("GET", "/api/health")[0] == 200):
            print(f"隔离实例未就绪（日志见 {SERVER_LOG}）")
            return 1
        print(f"隔离实例就绪 {BASE}（data_dir={TMP_ROOT / 'data'}）")

        http_layer(fixtures, cids)
        node_code = browser_layer()
        if node_code != 0:
            failures.append("浏览器层断言未全绿")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        log.close()
        shutil.rmtree(TMP_ROOT, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
