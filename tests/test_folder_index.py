# -*- coding: utf-8 -*-
"""护栏：拖文件夹进输入区 —— 路径索引链路（见 `计划-端口加固与文件夹拖拽.md` B 部分）。

缺陷形态（本文件钉的就是这几条）：
1. 一个素材目录能拖出几百个文件：若按普通附件走，输入区被缩略图刷屏、上传链路还会把
   整目录内容搬一遍——用户要的是"让模型知道这目录里有什么"，不是"在这翻图"；
2. 索引清单要拼进模型上下文，**没有任何闸门**就等于一次拖入吃掉几万字符（深度 / 条数 / 超时
   三重闸门缺一不可）；
3. 工作区外的目录如果在用户没表态时被静默扫描，那是越权读盘（口径二：确认一次、按会话记住）；
4. 计数被截断连坐：气泡上的"共 128 项"必须数**真实**值，否则用户以为文件丢了。

覆盖：`core/conv_files.folder_index` 的扫描语义与截断原因、`app.folder_index` 的路径闸门、
`/api/files/folder-index` 的 403 结构、模型侧清单与 `compose_user_content` 的字节稳定性、
落库 metadata 与历史重放的一致性、以及前后端共用的键名。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.app import NaibaChatApp  # noqa: E402
from naiba.core.attachments import (  # noqa: E402
    FOLDER_ONLY_NOTICE,
    compose_user_content,
    folder_index_lines,
)
from naiba.core.conv_files import (  # noqa: E402
    FOLDER_INDEX_MAX_DEPTH,
    FOLDER_INDEX_MAX_ENTRIES,
    FolderConfirmRequired,
    folder_index,
)
from naiba.core.history import build_model_history  # noqa: E402
from naiba.core.messages import MESSAGE_METADATA_KEYS, MetadataKeys  # noqa: E402
from naiba.http import AppHTTPServer, RequestHandler  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402


def _make_tree(root: Path) -> None:
    """搭一棵"三层 + 一个深井"的目录树（每层的预期命中写在断言里）。"""
    (root / "a.png").write_bytes(b"png")
    (root / "b.txt").write_text("hello", encoding="utf-8")
    (root / ".hidden.png").write_bytes(b"png")          # 点号开头：一律跳过
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "c.jpg").write_bytes(b"jpg")
    (root / "sub" / "deep").mkdir()
    (root / "sub" / "deep" / "d.webp").write_bytes(b"webp")
    (root / "sub" / "deep" / "deeper").mkdir()
    (root / "sub" / "deep" / "deeper" / "e.png").write_bytes(b"png")   # 超出深度闸门


class FolderIndexScanTests(unittest.TestCase):
    """`folder_index` 的扫描语义：相对路径、计数真实、三重闸门。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_folderindex_")
        # 必须 resolve()：CI runner 的 TEMP 是 8.3 短路径（`...\RUNNER~1\...`），
        # 而产品侧会把路径 resolve() 之后再比对——测试侧不 resolve 就会「本地全绿、CI 全红」
        # （与 tests/test_chat_background.py / test_file_references.py 同因同解）。
        self.root = Path(self.tmp.name).resolve() / "素材4"
        self.root.mkdir()
        _make_tree(self.root)
        self.addCleanup(self.tmp.cleanup)

    def _rels(self, result: dict) -> list[str]:
        return [entry["rel"] for entry in result["entries"]]

    def test_snapshot_shape(self) -> None:
        result = folder_index(self.root)
        self.assertEqual(result["name"], "素材4")
        self.assertEqual(result["path"], str(self.root))
        self.assertEqual(result["max_depth"], FOLDER_INDEX_MAX_DEPTH)
        self.assertEqual(result["max_entries"], FOLDER_INDEX_MAX_ENTRIES)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["truncated_reason"], "")

    def test_counts_are_real_and_hidden_entries_skipped(self) -> None:
        result = folder_index(self.root)
        # 命中：a.png / b.txt / sub/c.jpg / sub/deep/d.webp（e.png 超出深度，见下一个用例）
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["image_count"], 3, "png/jpg/webp 都算图片")
        self.assertEqual(result["dir_count"], 3, "sub / deep / deeper 三个目录都计数")
        rels = self._rels(result)
        self.assertNotIn(".hidden.png", rels, "点号开头的条目对「这目录里有什么」毫无信息量")
        self.assertFalse([rel for rel in rels if rel.startswith(".git")])

    def test_rel_paths_are_relative_and_forward_slashed(self) -> None:
        rels = self._rels(folder_index(self.root))
        self.assertEqual(sorted(rels), ["a.png", "b.txt", "sub/c.jpg", "sub/deep/d.webp"])
        self.assertFalse([rel for rel in rels if "\\" in rel], "模型侧要的是可拼接的 POSIX 相对路径")
        self.assertFalse([rel for rel in rels if Path(rel).is_absolute()])

    def test_depth_gate_stops_descending(self) -> None:
        """深井必须被深度闸门挡住：`deeper/` 自己是目录（计数），但不下去。"""
        result = folder_index(self.root)
        self.assertNotIn("sub/deep/deeper/e.png", self._rels(result))
        self.assertFalse(result["truncated"], "被深度闸门挡住不等于截断——条数预算没花完")

    def test_entry_cap_truncates_but_keeps_real_counts(self) -> None:
        result = folder_index(self.root, max_entries=2)
        self.assertEqual(len(result["entries"]), 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncated_reason"], "count")
        self.assertEqual(result["total"], 4, "计数是真实值：条数被截断不能让「共 N 项」跟着缩水")
        self.assertEqual(result["image_count"], 3)

    def test_timeout_sets_reason_and_keeps_going_no_further(self) -> None:
        """超时闸门：网络盘/机械盘上扫不完时宁可给半份清单，也不能把发送卡死。"""
        ticks = iter([0.0, 1e9, 1e9, 1e9])

        def _monotonic():
            return next(ticks, 1e9)

        with mock.patch("naiba.core.conv_files.time.monotonic", side_effect=_monotonic):
            result = folder_index(self.root)
        self.assertEqual(result["truncated_reason"], "timeout")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["entries"], [], "首次进入循环即超时 ⇒ 一条也没扫到")

    def test_repeated_scan_is_deterministic(self) -> None:
        """清单顺序不能依赖文件系统枚举顺序（否则每次发送都可能改动前缀缓存）。"""
        first = self._rels(folder_index(self.root))
        second = self._rels(folder_index(self.root))
        self.assertEqual(first, second)


class _AppGate:
    """只借 `NaibaChatApp` 上的文件夹闸门逻辑，不构造真 App（避免拉起 MCP / 后台任务）。

    这里刻意用**未绑定方法**而不是复制一份逻辑：测的就是线上那份实现。
    """

    folder_index = NaibaChatApp.folder_index
    folder_indexes_for_send = NaibaChatApp.folder_indexes_for_send
    # 线上是 staticmethod：取出来已是普通函数，这里必须重新包一层才不会被当成实例方法
    _folder_key = staticmethod(NaibaChatApp._folder_key)
    _confirmed_folder_paths = NaibaChatApp._confirmed_folder_paths

    def __init__(self, workspace: Path, conversations: dict | None = None) -> None:
        self.config = SimpleNamespace(resolve_workspace_dir=lambda raw=None: Path(workspace))
        self.storage = SimpleNamespace(get_conversation=lambda cid: (conversations or {}).get(cid))
        self._confirmed_folders: dict[str, set[str]] = {}


class FolderIndexGateTests(unittest.TestCase):
    """口径二：工作区内直接放行；工作区外必须**先确认**，且确认按会话隔离。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_foldergate_")
        # resolve() 同 FolderIndexScanTests：闸门把工作区与目标都 resolve() 过（`_conv_file_target`），
        # 夹具不 resolve 的话，CI 的 8.3 短 TEMP 会让「403 里带上的路径」与夹具字符串对不上。
        base = Path(self.tmp.name).resolve()
        self.workspace = base / "work"
        self.workspace.mkdir()
        (self.workspace / "inside").mkdir()
        (self.workspace / "inside" / "p.png").write_bytes(b"png")
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "q.png").write_bytes(b"png")
        self.addCleanup(self.tmp.cleanup)
        self.app = _AppGate(self.workspace, {"conv-a": {"id": "conv-a"}, "conv-b": {"id": "conv-b"}})

    def test_inside_workspace_passes_without_confirm(self) -> None:
        result = self.app.folder_index(str(self.workspace / "inside"), "conv-a")
        self.assertEqual(result["total"], 1)
        self.assertEqual(self.app._confirmed_folders, {}, "工作区内不该产生任何确认记录")

    def test_outside_workspace_requires_confirm(self) -> None:
        with self.assertRaises(FolderConfirmRequired) as ctx:
            self.app.folder_index(str(self.outside), "conv-a")
        self.assertEqual(ctx.exception.path, str(self.outside), "要带上路径，前端才能弹框并回填")

    def test_outside_workspace_allowed_once_then_remembered(self) -> None:
        result = self.app.folder_index(str(self.outside), "conv-a", allow_outside=True)
        self.assertEqual(result["total"], 1)
        # 第二次不给 allow_outside：同一会话内必须直接放行（用户不必反复点同意）
        again = self.app.folder_index(str(self.outside), "conv-a")
        self.assertEqual(again["total"], 1)

    def test_confirmation_is_scoped_to_conversation(self) -> None:
        self.app.folder_index(str(self.outside), "conv-a", allow_outside=True)
        with self.assertRaises(FolderConfirmRequired):
            self.app.folder_index(str(self.outside), "conv-b")

    def test_confirmation_key_ignores_case_and_trailing_slash(self) -> None:
        self.app.folder_index(str(self.outside), "conv-a", allow_outside=True)
        variant = f"{str(self.outside).replace(chr(92), '/')}/"
        self.app.folder_index(variant, "conv-a")  # 不抛即通过
        self.assertEqual(self.app._folder_key(self.outside), str(self.outside).replace("\\", "/").lower())

    def test_missing_or_non_directory_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.app.folder_index(str(self.workspace / "nope"), "conv-a")
        with self.assertRaises(ValueError):
            self.app.folder_index(str(self.workspace / "inside" / "p.png"), "conv-a")
        with self.assertRaises(ValueError):
            self.app.folder_index("", "conv-a")

    def test_unknown_conversation_is_lookup_error(self) -> None:
        with self.assertRaises(LookupError):
            self.app.folder_index(str(self.workspace / "inside"), "conv-missing")

    def test_send_snapshot_converts_confirm_required_to_value_error(self) -> None:
        """发送那一刻再抛 403 已经没意义了：必须变成一句能显示给用户的 ValueError。"""
        with self.assertRaises(ValueError) as ctx:
            self.app.folder_indexes_for_send("conv-a", [{"path": str(self.outside)}])
        self.assertIn(str(self.outside), str(ctx.exception))

    def test_send_snapshot_skips_blank_paths_and_keeps_order(self) -> None:
        result = self.app.folder_indexes_for_send(
            "conv-a",
            [{"path": str(self.workspace / "inside")}, {"path": "   "}, {"path": ""}],
        )
        self.assertEqual([item["name"] for item in result], ["inside"])


class _StubHttpApp:
    """给 HTTP 路由用：只实现 `/api/files/folder-index` 会碰到的那一个方法。"""

    def __init__(self, behavior) -> None:
        self._behavior = behavior
        self.calls: list[tuple] = []

    def folder_index(self, raw_path, conversation_id="", allow_outside=False):
        self.calls.append((raw_path, conversation_id, allow_outside))
        return self._behavior(raw_path, conversation_id, allow_outside)


class FolderIndexRouteTests(unittest.TestCase):
    """HTTP 层：403 + `needs_confirm`，而不是一句干巴巴的错误。"""

    def _serve(self, app) -> str:
        server = AppHTTPServer(("127.0.0.1", 0), RequestHandler, app)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def _stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_stop)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def _get(self, url: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_forbidden_carries_confirm_payload(self) -> None:
        def behavior(raw_path, conversation_id, allow_outside):
            raise FolderConfirmRequired(str(raw_path))

        app = _StubHttpApp(behavior)
        base = self._serve(app)
        status, payload = self._get(f"{base}/api/files/folder-index?path=D%3A%2Fx&conversation_id=conv-a")
        self.assertEqual(status, 403)
        self.assertTrue(payload["needs_confirm"])
        self.assertEqual(payload["path"], "D:/x", "回给前端的路径要原样可回填（query 解码后即为此形态）")
        self.assertEqual(app.calls, [("D:/x", "conv-a", False)])

    def test_allow_outside_query_is_forwarded(self) -> None:
        app = _StubHttpApp(lambda raw_path, conversation_id, allow_outside: {"allow": allow_outside})
        base = self._serve(app)
        status, payload = self._get(
            f"{base}/api/files/folder-index?path=D%3A%2Fx&allow_outside=1&conversation_id=conv-a"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["allow"])
        self.assertEqual(app.calls, [("D:/x", "conv-a", True)])

    def test_value_error_maps_to_400(self) -> None:
        def behavior(raw_path, conversation_id, allow_outside):
            raise ValueError("目录不存在或已被移动")

        base = self._serve(_StubHttpApp(behavior))
        status, payload = self._get(f"{base}/api/files/folder-index?path=D%3A%2Fx")
        self.assertEqual(status, 400)
        self.assertIn("目录不存在", payload["error"])


class FolderIndexPayloadTests(unittest.TestCase):
    """模型侧清单 + `compose_user_content`：老会话前缀必须逐字节不变。"""

    INDEX = {
        "name": "素材4",
        "path": "D:\\素材4",
        "total": 128,
        "image_count": 96,
        "dir_count": 2,
        "truncated": False,
        "entries": [
            {"rel": "a/图1.png", "kind": "image", "size": 10},
            {"rel": "b/图2.png", "kind": "image", "size": 20},
        ],
    }

    def test_lines_use_relative_paths_with_absolute_base(self) -> None:
        lines = folder_index_lines([self.INDEX])
        self.assertEqual(lines[0], "[文件夹] D:\\素材4（共 128 项，图片 96；下列路径均相对该文件夹）")
        self.assertEqual(lines[1:], ["- a/图1.png", "- b/图2.png"])

    def test_truncated_line_self_reports(self) -> None:
        lines = folder_index_lines([{**self.INDEX, "truncated": True}])
        self.assertTrue(lines[-1].startswith("- （仅列出前 2 项，另有 126 项未列出"))
        self.assertIn("可用工具按目录路径读取", lines[-1])

    def test_no_truncated_line_when_nothing_is_missing(self) -> None:
        lines = folder_index_lines([{**self.INDEX, "truncated": True, "total": 2}])
        self.assertFalse([line for line in lines if line.startswith("- （仅列出前")])

    def test_lines_skip_malformed_items(self) -> None:
        self.assertEqual(folder_index_lines([None, {}, {"path": "  "}, "x"]), [])
        self.assertEqual(folder_index_lines(None), [])

    def test_plain_message_is_byte_identical(self) -> None:
        self.assertEqual(compose_user_content("你好", []), "你好")
        self.assertEqual(compose_user_content("你好", [], folder_indexes=[]), "你好")
        self.assertEqual(compose_user_content("你好", [], folder_indexes=None), "你好")

    def test_folder_block_is_appended_after_uploads(self) -> None:
        uploads = [{"path": "D:\\素材4\\a.png"}]
        folder = {"path": "D:\\素材4", "total": 1, "image_count": 1, "entries": [{"rel": "a.png"}]}
        text = compose_user_content("看图", uploads, folder_indexes=[folder])
        self.assertEqual(
            text,
            "看图\n[用户上传文件：D:\\素材4\\a.png]\n"
            "[文件夹] D:\\素材4（共 1 项，图片 1；下列路径均相对该文件夹）\n- a.png",
        )
        # 不含文件夹的同一轮次必须逐字节不变（旧会话前缀缓存不受影响）
        self.assertEqual(compose_user_content("看图", uploads), "看图\n[用户上传文件：D:\\素材4\\a.png]")

    def test_folder_only_turn_gets_its_own_notice(self) -> None:
        folder = {"path": "D:\\素材4", "total": 0, "image_count": 0, "entries": []}
        text = compose_user_content("", [], folder_indexes=[folder])
        self.assertTrue(text.startswith(FOLDER_ONLY_NOTICE))
        self.assertIn("D:\\素材4", text)

    def test_history_replays_the_same_folder_block(self) -> None:
        folder = {"path": "D:\\素材4", "total": 1, "image_count": 1, "entries": [{"rel": "a.png"}]}
        rows = [
            {"role": "user", "content": "看图", "metadata": {MetadataKeys.FOLDER_INDEXES: [folder]}},
            {"role": "assistant", "content": "看到了", "metadata": {}},
        ]
        history = build_model_history(rows)
        self.assertEqual(
            history[0]["content"],
            compose_user_content("看图", [], folder_indexes=[folder]),
            "历史重放与 _run_chat 必须同一口径，否则每轮都重写前缀",
        )

    def test_history_without_folders_is_untouched(self) -> None:
        rows = [{"role": "user", "content": "你好", "metadata": {}}]
        self.assertEqual(build_model_history(rows)[0]["content"], "你好")


class FolderIndexStorageTests(unittest.TestCase):
    """落库：`create_chat_run` 写 metadata，分支要能把索引带过去。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_folderstore_")
        # 同因：paths 一律走 resolve()，夹具也保持规范形态（见 FolderIndexScanTests.setUp）
        self.storage = ChatStorage(Path(self.tmp.name).resolve() / "chat.db")
        self.conversation = self.storage.create_conversation(title="文件夹实验")
        self.cid = self.conversation["id"]
        self.addCleanup(self.tmp.cleanup)

    def test_create_chat_run_persists_folder_indexes(self) -> None:
        folder = {"path": "D:\\素材4", "total": 2, "image_count": 2, "entries": [{"rel": "a.png"}]}
        run, history = self.storage.create_chat_run(
            self.cid, "看图", [], {"id": "default"}, {"agent_id": "default"},
            "chat", folder_indexes=[folder],
        )
        stored = self.storage.get_conversation(self.cid)["messages"][-1]
        self.assertEqual(stored["metadata"][MetadataKeys.FOLDER_INDEXES], [folder])
        self.assertEqual(run["input_message_id"], stored["id"], "用户消息与所属 run 必须指向同一条")
        # 模型上下文也要读得到（落库与重放同源）
        self.assertIn("[文件夹] D:\\素材4", build_model_history(history)[0]["content"])

    def test_empty_folder_indexes_do_not_add_metadata(self) -> None:
        self.storage.create_chat_run(
            self.cid, "你好", [], {"id": "default"}, {"agent_id": "default"}, "chat",
        )
        stored = self.storage.get_conversation(self.cid)["messages"][-1]
        self.assertNotIn(MetadataKeys.FOLDER_INDEXES, stored["metadata"],
                         "没有文件夹的轮次不许留下空键（历史重放要逐字节一致）")


class FolderIndexContractTests(unittest.TestCase):
    """键名与前后端一致性：改名不会有人报错，但会让整条链路悄悄失效。"""

    def test_metadata_key_is_registered(self) -> None:
        self.assertEqual(MetadataKeys.FOLDER_INDEXES, "folder_indexes")
        self.assertIn("folder_indexes", MESSAGE_METADATA_KEYS)

    def test_key_name_is_shared_with_frontend(self) -> None:
        """后端键名与前端读 metadata 的地方必须逐字一致（改名不会有人报错，只会静默失效）。"""
        for rel in ("public/js/04-messages.js", "public/js/11-run-stream.js"):
            source = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("folder_indexes", source, f"{rel} 与后端的 metadata 键名已经分叉")
        # 上传侧不直接拼键名，靠 camelCase 助手转换——两个助手名也要在。
        upload = (ROOT / "public/js/10-upload.js").read_text(encoding="utf-8")
        self.assertIn("export function folderIndexMetadata", upload)
        self.assertIn("export function folderChipsFromIndexes", upload)

    def test_frontend_markup_previews_ten_entries(self) -> None:
        """B.4：气泡只展示前 10 项 + 可折叠，完整清单（最多 300）进模型。"""
        source = (ROOT / "public/js/03-media.js").read_text(encoding="utf-8")
        self.assertIn("FOLDER_INDEX_PREVIEW = 10", source)
        self.assertIn("folderIndexMarkup", source)
        self.assertIn("export function folderIndexMarkup", source)

    def test_render_pending_files_shows_one_chip_per_folder(self) -> None:
        """一个目录 = 一个占位，不许按目录内容展开（否则输入区被刷屏）。"""
        source = (ROOT / "public/js/10-upload.js").read_text(encoding="utf-8")
        self.assertIn("is-folder", source)
        self.assertIn("export function folderChips", source)

    def test_interjection_channel_refuses_folder_chips(self) -> None:
        """插话通道没有 metadata 注入点，带文件夹就必须拦住（见 run/interjection 契约）。"""
        source = (ROOT / "public/js/18-interjections.js").read_text(encoding="utf-8")
        self.assertIn("folderChips().length", source)


if __name__ == "__main__":
    unittest.main()
