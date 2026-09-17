# -*- coding: utf-8 -*-
"""冻结版实跑自检：确认第一批三项（v18 迁移 + 搜索/分支链/删除撤销）真的进了 exe。

用法：`dist\\naiba-chat.exe --run-skill-script <本文件绝对路径>`
只断言运行期可达的东西（冻结版没有可读源码），不读源码文本——源码级断言在
`tests/test_message_search_api.py` / `tests/test_branch_chain.py` / `tests/test_message_delete.py` 里。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ok = True


def check(label: str, condition: bool, detail: str = "") -> None:
    global ok
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition or not detail else f'  -> {detail}'}")
    if not condition:
        ok = False


from naiba.storage.store import CURRENT_SCHEMA_VERSION, MIGRATIONS, ChatStorage  # noqa: E402

check("CURRENT_SCHEMA_VERSION == 18", CURRENT_SCHEMA_VERSION == 18, str(CURRENT_SCHEMA_VERSION))
check("MIGRATIONS 含 v18", 18 in MIGRATIONS)
for name in ("search_messages", "snippet_around", "branch_chain", "delete_message", "restore_messages"):
    check(f"ChatStorage.{name} 存在", hasattr(ChatStorage, name))
check("MESSAGE_DELETE_MODES == ('single','turn')",
      tuple(getattr(ChatStorage, "MESSAGE_DELETE_MODES", ())) == ("single", "turn"),
      str(getattr(ChatStorage, "MESSAGE_DELETE_MODES", None)))

# 端到端：真建库（触发 v18 迁移）→ 删一条 → 撤销 → 逐字节还原
with tempfile.TemporaryDirectory(prefix="frozen_batch1_") as tmp:
    storage = ChatStorage(Path(tmp) / "chat.db")
    conversation = storage.create_conversation(title="冻结探针")
    cid = conversation["id"]
    first = storage.add_message(cid, "user", "冻结第一问")["id"]
    reply = storage.add_message(cid, "assistant", "冻结第一答", {"reasoning": "r"})["id"]
    storage.add_message(cid, "user", "冻结第二问")

    # 分支两列确实落地
    branch = storage.branch_conversation(cid, first)["conversation"]
    check("branch_conversation 写入来源两列",
          branch["branched_from_id"] == cid and branch["branch_message_id"] == first)
    check("branch_chain 三角色可用",
          storage.branch_chain(branch["id"])["role"] == "branch"
          and storage.branch_chain(cid)["role"] == "source")

    # 搜索
    hits = storage.search_messages("冻结第一答")
    check("search_messages 命中且片段为原文子串",
          hits["total_hits"] == 1 and "冻结第一答" in hits["hits"][0]["snippet"],
          str(hits)[:200])

    # 删除 + 撤销逐字节还原
    before = [f"{m['id']}|{m['role']}|{m['content']}|{m['metadata']}" for m in storage.get_conversation(cid)["messages"]]
    removed = storage.delete_message(cid, reply, "single")
    check("删除后少一条", len(storage.get_conversation(cid)["messages"]) == len(before) - 1)
    storage.restore_messages(cid, removed["removed"])
    after = [f"{m['id']}|{m['role']}|{m['content']}|{m['metadata']}" for m in storage.get_conversation(cid)["messages"]]
    check("撤销后逐字节还原", before == after)

    # turn 语义 + session 行拒绝
    try:
        storage.delete_message(cid, first, "turn")
        check("turn 删除可用", len(storage.get_conversation(cid)["messages"]) == 1)
    except Exception as exc:  # noqa: BLE001
        check("turn 删除可用", False, repr(exc))

# 前端/后端资源完整性：能 import 到这儿就说明 PYZ 完整。
# 注意**不能**断言 `naiba/__file__` 指向的目录存在——冻结版把源码打进 PYZ，
# `_MEIPASS\naiba` 是虚拟路径，盘上没有可读的 `.py`（维护说明书明写过）。
try:
    import naiba.http as http_module  # noqa: E402
    from naiba.app import NaibaChatApp  # noqa: E402

    check("naiba.app / naiba.http 均可 import（PYZ 完整）", True)
    for name in ("api_search_messages", "api_branch_chain", "api_delete_message", "api_restore_messages"):
        check(f"NaibaChatApp.{name} 存在", hasattr(NaibaChatApp, name))
except Exception as exc:  # noqa: BLE001
    check("naiba.app / naiba.http 均可 import（PYZ 完整）", False, repr(exc))

# 打包进 exe 的 index.html 必须带本批新增的 DOM 钩子（源码模式读 public/ 看不出打包缺失）。
try:
    from naiba.paths import default_path_context  # noqa: E402

    paths = default_path_context()
    index = (Path(paths.public_dir) / "index.html").read_text(encoding="utf-8")
    for marker in ("workspaceSearchRow", "branchChainPanel", "messageDeleteDialog", "undoBar", "messageDeleteTurn"):
        check(f"打包资源含 {marker}", marker in index)
except Exception as exc:  # noqa: BLE001
    check("可读到打包内的 index.html", False, repr(exc))

print("\n全部通过" if ok else "\n存在未通过项")
sys.exit(0 if ok else 1)
