# -*- coding: utf-8 -*-
"""契约：本地大脑的「图片已降级」旗标（降级不可逆）。

保护对象（维护说明 §九.146）：
1. **字节一致**：首次降级那轮线上改写的占位文本，与之后各轮旗标回放的占位文本逐字节相同
   （否则"记忆"本身就在改写历史，前缀照旧断）；
2. **一块占位**：同一条消息上「旗标回放的旧省略」与「本轮新省略」必须合并成一块占位，
   不能出现两份「已省略 N 张」；
3. **不再重编码**：已降级的图片此后不再 ``encode_image_for_model``（省每轮的读盘 + PIL 压缩）；
4. **不可逆**：后续历史被裁剪/回滚使保留窗口往回滑时，已降级图片不会又被装成真图；
5. **只在 local 生效**：在线大脑不回放旗标（切在线恢复原图，前缀断一次可接受）；
6. **内部键不外泄**：``_message_id`` / ``_local_images_capped`` 不进任何 wire 请求消息。

探针 ``verify/_probe_image_prefix.py`` 记录实测口径：只追加的稳态下两种实现的前缀一致，
旗标的稳态收益是编码次数（63→37）；前缀收益出现在历史被回滚时（1/5→2/5，随深度放大）。
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from naiba.core import history as history_mod  # noqa: E402
from naiba.core.contracts import MetadataKeys  # noqa: E402
from naiba.core.history import (  # noqa: E402
    HISTORY_LOCAL_IMAGES_KEY,
    HISTORY_MESSAGE_ID_KEY,
    build_model_history,
    is_local_image_omitted_marker,
    local_image_omitted_marker,
)
from naiba.llm.protocols import ProtocolMixins  # noqa: E402
from naiba.run.chat import ConversationRunMixin  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402
from naiba.vision.runtime import VisionRouter  # noqa: E402


class _ConfigStub:
    def __init__(self, vision: dict):
        self.data = {"vision": vision}


class _AppStub:
    def __init__(self, vision: dict):
        self.config = _ConfigStub(vision)


VISION_CONFIG = {
    "provider_model_key": "",
    "timeout_ms": 180000,
    "max_images": 4,
    "cache": True,
    "cache_ttl_seconds": 3600,
    "cache_max_entries": 200,
}

LOCAL_PROFILE = {"kind": "local", "model": "qwen3-vl", "request_format": "llama_cpp",
                 "supports_images": True}
ONLINE_PROFILE = {"kind": "online", "model": "gpt-4o", "request_format": "openai_chat",
                  "supports_images": True}


def setUpModule() -> None:
    """本文件钉的是**保留机制**的契约（默认已停用，见 §九.146 修订）。

    2026-10-01 用户实测纠正：本地会话内历史图片本来就在服务端 KV / 前缀缓存里，降级滑窗才是
    "12 张以后丢缓存"的元凶 ⇒ `LOCAL_IMAGE_CAP_ENABLED` 默认 False，本地大脑按"全部图片原样
    保留"走。机制代码与用例全部保留，本模块把开关打开来钉它自己的契约；
    **开关关掉时的行为**由 `CapSwitchedOffTests`（本文件末尾，显式关）覆盖，**出厂默认值本身**
    由 `tests/test_local_model_guard.py::LocalImageCapTests` 里那条**不打补丁**的用例钉住
    （变异核对：把默认改成 True ⇒ 它红）。
    """
    global _CAP_PATCHER
    _CAP_PATCHER = mock.patch.object(history_mod, "LOCAL_IMAGE_CAP_ENABLED", True)
    _CAP_PATCHER.start()


def tearDownModule() -> None:
    if _CAP_PATCHER is not None:
        _CAP_PATCHER.stop()


def _write_images(tmp: Path, count: int) -> list[str]:
    paths = []
    for index in range(count):
        path = tmp / f"probe{index}.png"
        image = Image.new("RGB", (16, 16), (index * 9 % 255, index * 5 % 255, 60))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        path.write_bytes(buffer.getvalue())
        paths.append(str(path))
    return paths


def _user_message(message_id: str, paths: list[str]) -> dict:
    return {
        "id": message_id,
        "role": "user",
        "content": f"看第 {message_id} 批图",
        "metadata": {
            "attachments": [{"path": path, "name": Path(path).name} for path in paths],
        },
    }


def _image_count(history: list[dict]) -> int:
    return sum(
        1
        for item in history
        for part in (item.get("content") or [])
        if isinstance(part, dict) and part.get("type") == "image"
    )


def _marker_texts(history: list[dict], index: int = 0) -> list[str]:
    return [
        str(part.get("text") or "")
        for part in (history[index].get("content") or [])
        if isinstance(part, dict) and part.get("type") == "text"
        and is_local_image_omitted_marker(str(part.get("text") or ""))
    ]


def _wire_bytes(item: dict) -> str:
    return json.dumps(item.get("content"), ensure_ascii=False, sort_keys=True)


_CAP_PATCHER = None


class CapSwitchedOffTests(unittest.TestCase):
    """开关关掉时的口径：本地大脑**不做任何图片处理**（用户 2026-10-01 要求；出厂默认即关）。

    1. 超限也全部保留（`prepare_history` 原样放行、不降级、不回传旗标）；
    2. 老会话里已经落了 `local_images_capped` 的消息**按真图还原**（旗标不再回放）——
       否则老会话会永久停在占位形态上；
    3. 在线大脑与文本大脑路径都不受影响。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = _write_images(Path(self.tmp.name), 4)
        self.router = VisionRouter(_AppStub(VISION_CONFIG))

    def test_local_brain_keeps_every_image_when_switch_off(self) -> None:
        """开关关：本地大脑不降级、不回传旗标、原样放行；**远超每请求上限的图片也全部保留**。

        6 条消息 × 3 张 = 18 张（> LOCAL_REQUEST_IMAGE_LIMIT=12）：这是用户要的口径。
        注意 `MODEL_IMAGE_HISTORY_LIMIT=3` 是**每条 user 消息**的封顶（另一套规则，未改动），
        所以喂 4 张会被它裁到 3 张——本用例按 3 张/条来构造。
        """
        per_message = self.paths[:3]
        messages = [_user_message(f"m{index}", per_message) for index in range(6)]
        history = build_model_history(messages, local_image_brain=True)
        self.assertEqual(_image_count(history), 18, "18 张全部要在上下文里（12 张上限已停用）")
        with mock.patch.object(history_mod, "LOCAL_IMAGE_CAP_ENABLED", False):
            kept, note, demotions = self.router.prepare_history(history, LOCAL_PROFILE)
        self.assertIs(kept, history, "原样放行：连对象都不换")
        self.assertEqual(_image_count(kept), 18)
        self.assertEqual(note, "")
        self.assertEqual(demotions, [])
        # 机制没被删掉：显式打开就照旧降级（这是"停用"与"删除"的区别）
        with mock.patch.object(VisionRouter, "LOCAL_REQUEST_IMAGE_LIMIT", 1):
            capped, _note, cap_demotions = self.router._cap_local_history_images(history)
        self.assertEqual(_image_count(capped), 1)
        self.assertTrue(cap_demotions)

    def test_flagged_old_messages_replay_real_images_when_switch_off(self) -> None:
        """开关关：老会话已落的旗标**不再回放**，那张图要回到上下文（否则永久停在占位形态）。"""
        messages = [_user_message("m1", self.paths[:2])]
        messages[0]["metadata"][MetadataKeys.LOCAL_IMAGES_CAPPED] = {
            "names": [Path(self.paths[0]).name]
        }
        with mock.patch.object(history_mod, "LOCAL_IMAGE_CAP_ENABLED", False):
            replayed = build_model_history(messages, local_image_brain=True)
        texts = [
            str(part.get("text") or "")
            for part in replayed[0]["content"]
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        self.assertEqual(_image_count(replayed), 2, "旗标里的那张图也要回到上下文")
        self.assertFalse(
            any(is_local_image_omitted_marker(text) for text in texts),
            f"默认不该再出现占位块：{texts}",
        )
        self.assertNotIn(HISTORY_LOCAL_IMAGES_KEY, replayed[0], "也不该再挂内部降级键")



class LocalImageFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = VisionRouter(_AppStub(VISION_CONFIG))
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = _write_images(Path(self.tmp.name), 6)
        self.addCleanup(self.tmp.cleanup)

    def _cap(self, history: list[dict], limit: int):
        with mock.patch.object(VisionRouter, "LOCAL_REQUEST_IMAGE_LIMIT", limit):
            return self.router._cap_local_history_images(history)

    def test_replay_is_byte_identical_to_the_demotion_turn(self) -> None:
        """核心契约：首降那轮的线上字节 == 之后各轮的旗标回放字节。"""
        messages = [_user_message("m1", self.paths[:3])]
        first = build_model_history(messages, local_image_brain=True)
        self.assertEqual(_image_count(first), 3)
        capped, _note, demotions = self._cap(first, limit=1)
        self.assertEqual(_image_count(capped), 1, "上限 1 张：另外 2 张要降级")
        self.assertEqual(len(demotions), 1)
        self.assertEqual(demotions[0]["message_id"], "m1")

        messages[0]["metadata"][MetadataKeys.LOCAL_IMAGES_CAPPED] = {
            "names": list(demotions[0]["names"])
        }
        replayed = build_model_history(messages, local_image_brain=True)
        self.assertEqual(
            _wire_bytes(replayed[0]), _wire_bytes(capped[0]),
            "旗标回放的字节必须与降级那一轮完全一致，否则前缀还是断在原地",
        )

    def test_old_and_new_demotions_merge_into_one_marker(self) -> None:
        """同一条消息上旧省略 + 新省略只能有一块占位（合并清单），不能两块。"""
        messages = [_user_message("m1", self.paths[:3])]
        first = build_model_history(messages, local_image_brain=True)
        capped, _note, demotions = self._cap(first, limit=2)
        messages[0]["metadata"][MetadataKeys.LOCAL_IMAGES_CAPPED] = {
            "names": list(demotions[0]["names"])
        }
        second = build_model_history(messages, local_image_brain=True)
        capped_again, _note2, demotions2 = self._cap(second, limit=1)
        markers = _marker_texts(capped_again)
        self.assertEqual(len(markers), 1, f"只能有一块占位，实际 {len(markers)} 块：{markers}")
        merged = list(dict.fromkeys(list(demotions[0]["names"]) + list(demotions2[0]["names"])))
        self.assertEqual(markers[0], local_image_omitted_marker(merged))
        self.assertIn(f"已省略 {len(merged)} 张", markers[0])

    def test_demoted_images_are_not_encoded_again(self) -> None:
        """成本契约：已降级的图片此后不再读盘 + PIL 编码。"""
        messages = [_user_message("m1", self.paths[:3])]
        first = build_model_history(messages, local_image_brain=True)
        _capped, _note, demotions = self._cap(first, limit=1)
        messages[0]["metadata"][MetadataKeys.LOCAL_IMAGES_CAPPED] = {
            "names": list(demotions[0]["names"])
        }
        calls: list[str] = []
        real = history_mod.encode_image_for_model

        def spy(source: str):
            calls.append(str(source))
            return real(source)

        with mock.patch.object(history_mod, "encode_image_for_model", spy):
            replayed = build_model_history(messages, local_image_brain=True)
        self.assertEqual(
            [Path(path).name for path in calls],
            [Path(self.paths[2]).name],
            "只剩最后 1 张真图要编码，被降级的 2 张绝不能再进编码器",
        )
        self.assertEqual(_image_count(replayed), 1)

    def test_slot_accounting_does_not_admit_a_fourth_attachment(self) -> None:
        """槽位口径：已降级的图照样占一个 `MODEL_IMAGE_HISTORY_LIMIT` 槽。

        否则「降级空出一个槽」会让第 4 张附件凭空补成历史里的真图——首降之后字节又变一次，
        前缀断在更早的位置（这种口径不写测试就一定会错，所以钉死）。
        """
        messages = [_user_message("m1", self.paths[:3])]
        first = build_model_history(messages, local_image_brain=True)
        self.assertEqual(_image_count(first), 3)
        _capped, _note, demotions = self._cap(first, limit=2)
        messages[0]["metadata"][MetadataKeys.LOCAL_IMAGES_CAPPED] = {
            "names": list(demotions[0]["names"])
        }
        messages[0]["metadata"]["attachments"].append(
            {"path": self.paths[3], "name": Path(self.paths[3]).name}
        )
        replayed = build_model_history(messages, local_image_brain=True)
        self.assertEqual(
            _image_count(replayed), 2,
            "旗标里的 paths[0] 仍占一个槽：只剩两张真图，第 4 张不得补位",
        )

    def test_same_file_attached_twice_replays_byte_identically(self) -> None:
        """同一张图在一条消息里附两次：回放必须逐字节等于首降那轮，且只跳该跳的那张。

        旧写法按"名字在清单里"匹配 ⇒ 两个同名上传**都被跳过**，本该保留的那张也消失：
        历史字节变了（前缀断）+ 那张图不可逆地没了，正好击穿本功能要建立的不变量。
        触发很现实：前端上传不去重、后端按内容去重返回同一个 path，拖两次就是两个同名附件。
        """
        same = self.paths[0]
        messages = [_user_message("m1", [same, same]), _user_message("m2", self.paths[1:2])]

        first = build_model_history(messages, local_image_brain=True)
        capped, _note, demotions = self._cap(first, limit=2)
        messages[0]["metadata"][MetadataKeys.LOCAL_IMAGES_CAPPED] = {
            "names": list(demotions[0]["names"])
        }
        replayed = build_model_history(messages, local_image_brain=True)

        self.assertEqual(demotions[0]["names"], [Path(same).name], "只省了先出现的那一次")
        self.assertEqual(_image_count(capped), 2)
        self.assertEqual(_wire_bytes(capped[0]), _wire_bytes(replayed[0]), "回放必须字节一致")
        self.assertEqual(_image_count(replayed), 2, "同名图片不得被一起跳过")

    def test_different_files_with_the_same_basename_replay_byte_identically(self) -> None:
        """两个不同目录下的同名文件：同样按"第几次出现"对应，不能按名字一刀切。"""
        left = Path(self.tmp.name) / "left"
        right = Path(self.tmp.name) / "right"
        left.mkdir(exist_ok=True)
        right.mkdir(exist_ok=True)
        paths = _write_images(left, 1) + _write_images(right, 1)
        self.assertEqual(Path(paths[0]).name, Path(paths[1]).name, "前置条件：两个文件同名")
        self.assertNotEqual(paths[0], paths[1], "前置条件：两个文件不同")
        messages = [_user_message("m1", paths), _user_message("m2", self.paths[1:2])]

        first = build_model_history(messages, local_image_brain=True)
        capped, _note, demotions = self._cap(first, limit=2)
        messages[0]["metadata"][MetadataKeys.LOCAL_IMAGES_CAPPED] = {
            "names": list(demotions[0]["names"])
        }
        replayed = build_model_history(messages, local_image_brain=True)

        self.assertEqual(demotions[0]["names"], [Path(paths[0]).name])
        self.assertEqual(_image_count(capped), 2)
        self.assertEqual(_wire_bytes(capped[0]), _wire_bytes(replayed[0]))
        self.assertEqual(_image_count(replayed), 2)

    def test_duplicate_names_are_counted_by_occurrence(self) -> None:
        """占位里的张数按**出现次数**报：同名两张都省了就说两张（json 里仍只列一次名字）。"""
        same = self.paths[0]
        messages = [_user_message("m1", [same, same, self.paths[1]])]
        first = build_model_history(messages, local_image_brain=True)
        capped, _note, _demotions = self._cap(first, limit=1)
        marker = _marker_texts(capped)[0]
        self.assertIn("已省略 2 张较早的图片", marker)
        self.assertEqual(marker.count(Path(same).name), 1, "json 里只列一次（去重展示）")

    def test_online_brain_does_not_replay_flags(self) -> None:
        """旗标只在 kind=local 回放：在线大脑照常带原图（切模型断一次前缀，可接受）。"""
        messages = [_user_message("m1", self.paths[:3])]
        first = build_model_history(messages, local_image_brain=True)
        _capped, _note, demotions = self._cap(first, limit=1)
        messages[0]["metadata"][MetadataKeys.LOCAL_IMAGES_CAPPED] = {
            "names": list(demotions[0]["names"])
        }
        online = build_model_history(messages, local_image_brain=False)
        self.assertEqual(_image_count(online), 3, "在线大脑应重新带上 3 张原图")
        self.assertEqual(_marker_texts(online), [], "在线大脑不该回放占位文本")

    def test_rollback_does_not_reopen_demoted_images(self) -> None:
        """不可逆契约：回滚/裁剪让窗口往回滑时，已降级图片不再被装回真图。"""
        messages = [
            _user_message("m1", self.paths[0:2]),
            _user_message("m2", self.paths[2:4]),
            _user_message("m3", self.paths[4:6]),
        ]
        history = build_model_history(messages, local_image_brain=True)
        _capped, _note, demotions = self._cap(history, limit=2)
        for entry in demotions:
            for message in messages:
                if message["id"] == entry["message_id"]:
                    message["metadata"][MetadataKeys.LOCAL_IMAGES_CAPPED] = {
                        "names": list(entry["names"])
                    }
        settled = build_model_history(messages, local_image_brain=True)
        # 回滚：删掉最后两条 user（保留窗口往回滑到 m1）。
        trimmed = build_model_history(messages[:1], local_image_brain=True)
        self.assertEqual(_wire_bytes(settled[0]), _wire_bytes(trimmed[0]),
                         "m1 已经降级过：历史变短也不许把它的图换回真图")
        self.assertEqual(_image_count(trimmed), 0, "m1 的两张图都在旗标里：只剩占位文本")

    def test_local_cap_skips_non_local_profile(self) -> None:
        """prepare_history：只有 kind=local 的多模态大脑走图片总量上限并回传旗标清单。"""
        history = build_model_history(
            [_user_message("m1", self.paths[:3])], local_image_brain=True
        )
        _capped, note, demotions = self.router.prepare_history(history, ONLINE_PROFILE)
        self.assertEqual((note, demotions), ("", []), "在线大脑不降级、不回传旗标")


class FlagPersistenceTests(unittest.TestCase):
    """chat 层落库：按键合并、不清空其它 metadata、缺 id 就跳过、失败只记日志。"""

    class _Storage:
        """**只**提供本函数用到的那一个写入接口。

        刻意不提供 ``get_conversation`` / ``update_message_metadata``：写旗标不再允许
        「先读整块 metadata 再整块写回」，那正是丢更新的来源（丢 ``session_start`` ⇒
        上下文被整段清空）。谁改回去，这里会直接 AttributeError 红掉。
        """

        def __init__(self, messages: list[dict]):
            self.messages = messages
            self.writes: list[tuple[str, dict]] = []

        def merge_message_metadata(
            self, _conversation_id: str, message_id: str, patch: dict
        ) -> bool:
            for message in self.messages:
                if message["id"] == message_id:
                    metadata = message.get("metadata")
                    if not isinstance(metadata, dict):
                        metadata = {}
                    metadata.update(patch)   # 模拟 SQLite 的 json_set：只改传进来的键
                    message["metadata"] = metadata
                    self.writes.append((message_id, patch))
                    return True
            return False

    class _Host(ConversationRunMixin):
        def __init__(self, storage) -> None:
            self.app = SimpleNamespace(storage=storage)

    def setUp(self) -> None:
        self.messages = [{
            "id": "m1",
            "role": "user",
            "content": "带图",
            "metadata": {"attachments": [{"path": "a.png", "name": "a.png"}]},
        }]
        self.storage = FlagPersistenceTests._Storage(self.messages)
        self.host = FlagPersistenceTests._Host(self.storage)

    def _flag(self) -> list[str]:
        return list(
            (self.messages[0]["metadata"].get(MetadataKeys.LOCAL_IMAGES_CAPPED) or {}).get("names")
            or []
        )

    def test_replaces_with_the_authoritative_list_and_keeps_other_metadata(self) -> None:
        """``names`` 是 vision 回传的**全量**清单 ⇒ 整键替换（旧省在前 + 本轮新增在后）。

        替换而不是增量合并有两个理由：① 重复调用天然幂等（增量合并会把张数累加，
        而重复项正是「同名图片省了几张」的回放匹配依据）；② 陈旧名字会被自然清掉。
        """
        self.host._record_local_image_demotions(
            "c1", [{"message_id": "m1", "names": ["a.png"]}]
        )
        self.assertEqual(self._flag(), ["a.png"])
        self.host._record_local_image_demotions(
            "c1", [{"message_id": "m1", "names": ["a.png", "b.png"]}]
        )
        self.assertEqual(self._flag(), ["a.png", "b.png"], "旧省在前、新增在后，顺序即回放顺序")
        # 幂等：同一份全量清单再来一次，旗标一字不变（增量合并会变成 4 项）
        self.host._record_local_image_demotions(
            "c1", [{"message_id": "m1", "names": ["a.png", "b.png"]}]
        )
        self.assertEqual(self._flag(), ["a.png", "b.png"], "重复记账不得把同一张图数两次")
        self.assertEqual(
            self.messages[0]["metadata"].get("attachments"),
            [{"path": "a.png", "name": "a.png"}],
            "写旗标绝不能清空 attachments 等既有 metadata（写入是按键合并，其他键由数据库保留）",
        )

    def test_keeps_duplicate_names_for_replay_matching(self) -> None:
        """同名图片省了两张 ⇒ 清单里必须留两项（回放靠它逐次跳过）。"""
        self.host._record_local_image_demotions(
            "c1", [{"message_id": "m1", "names": ["dup.png", "dup.png"]}]
        )
        self.assertEqual(self._flag(), ["dup.png", "dup.png"])

    def test_skips_entries_without_message_id(self) -> None:
        self.host._record_local_image_demotions("c1", [{"message_id": "", "names": ["a.png"]}])
        self.host._record_local_image_demotions("c1", [{"message_id": "m1", "names": []}])
        self.host._record_local_image_demotions("c1", [{"message_id": "ghost", "names": ["x.png"]}])
        self.assertEqual(self.storage.writes, [], "无 id / 无名字 / 消息不存在都不应写库")

    def test_write_failure_does_not_raise(self) -> None:
        """写库失败只记日志、绝不抛到外层「整轮视觉清洗失败」分支。"""
        def boom(*_args, **_kwargs):
            raise RuntimeError("库锁")

        self.storage.merge_message_metadata = boom  # type: ignore[method-assign]
        self.host._record_local_image_demotions(
            "c1", [{"message_id": "m1", "names": ["a.png"]}]
        )
        self.assertEqual(self._flag(), [], "写库失败只记日志，本轮照常发请求")
        self.assertIsInstance(
            self.messages[0]["metadata"], dict,
            "失败路径不得把 metadata 破坏成非 dict（那会让后续轮次整段降级）",
        )


class ConcurrentMetadataWritersTests(unittest.TestCase):
    """同一行有多个 metadata 写入方时**不得互相抹键**（图片降级旗标 / 会话边界）。

    病历：两侧都写「先读整块 metadata、改完再整块写回」，各自读到的都是**旧**整块 ⇒
    后写者把先写者刚落的键整块抹掉。丢 `session_start` 的代价不是"少个标记"：
    `build_model_history` 会清空整段上下文（用户视角＝模型突然失忆）。
    修法是把写入降成按键原子合并（单条 `json_set`）——数据库按当前值保留其他键。
    """

    class _Host(ConversationRunMixin):
        def __init__(self, storage) -> None:
            self.app = SimpleNamespace(storage=storage)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_flag_race_")
        self.addCleanup(self.tmp.cleanup)
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.cid = str(self.storage.create_conversation("并发写入")["id"])
        self.mid = str(self.storage.add_message(
            self.cid, "user", "带图",
            {MetadataKeys.ATTACHMENTS: [{"path": "a.png", "name": "a.png"}]},
        )["id"])
        self.host = self._Host(self.storage)

    def _metadata(self) -> dict:
        return dict((self.storage.get_conversation(self.cid)["messages"][0].get("metadata") or {}))

    def _flag_names(self) -> list[str]:
        return list((self._metadata().get(MetadataKeys.LOCAL_IMAGES_CAPPED) or {}).get("names") or [])

    def test_flag_writer_has_no_read_modify_write(self) -> None:
        """**结构守门**：旗标写入不得再出现「读整块 → 整块写回」这两步。

        这是丢更新的唯一来源：两个写入方各自读到的都是旧整块，后写者抹掉先写者的键。
        本用例在旧实现上必红（旧实现正是 `get_conversation` + `update_message_metadata`），
        是这条修复的判决性守卫；运行期还有 `FlagPersistenceTests._Storage` 只提供
        `merge_message_metadata` 作为第二道（改回去会 AttributeError）。
        """
        body = _read_source("naiba", "run", "chat.py").split(
            "def _record_local_image_demotions", 1
        )[1].split("\n    def ", 1)[0]
        self.assertIn("merge_message_metadata", body, "必须走按键原子合并")
        self.assertNotIn("get_conversation", body, "不许再先读整块会话（那是竞态窗口的一半）")
        self.assertNotIn("update_message_metadata", body, "整块替换会抹掉同行的其他键")

    def test_flag_and_session_start_share_one_row_without_losing_keys(self) -> None:
        """两个写入方共用一行：两种先后顺序都必须两键俱全。"""
        self.host._record_local_image_demotions(
            self.cid, [{"message_id": self.mid, "names": ["a.png"]}]
        )
        self.storage.set_session_start(self.cid, self.mid, note="并发")
        metadata = self._metadata()
        self.assertEqual(self._flag_names(), ["a.png"], "会话边界的写入不得抹掉旗标")
        self.assertIn(MetadataKeys.SESSION_START, metadata)
        self.assertIn(MetadataKeys.ATTACHMENTS, metadata)

    def test_clear_session_start_keeps_the_flag(self) -> None:
        """对称保证：撤销会话边界只摘自己那个键（`json_remove`），不整块写回。"""
        self.storage.set_session_start(self.cid, self.mid, note="先落边界")
        self.host._record_local_image_demotions(
            self.cid, [{"message_id": self.mid, "names": ["a.png"]}]
        )
        self.assertTrue(self.storage.clear_session_start(self.mid))
        metadata = self._metadata()
        self.assertNotIn(MetadataKeys.SESSION_START, metadata, "边界要被摘掉")
        self.assertEqual(self._flag_names(), ["a.png"], "摘边界不得顺手抹掉旗标")

    def test_no_whole_blob_metadata_write_on_shared_rows(self) -> None:
        """**结构守门**：共享行上的写入一律键级（`json_set`/`json_remove`），不得整块写回。

        允许整块写回的只剩 `update_message_metadata` 一处（"整块 metadata 都是自己算的"
        场景）；三个插话写入方（`set_interjection_guided` /
        `mark_run_interjections_consumed` / `stop_pending_interjections`）与
        `storage/job_media.py::write_back` 都必须按键写。判据是源码里整块替换语句的出现
        次数——旧实现 store.py 4 条、job_media.py 1 条调用。
        """
        store = _read_source("naiba", "storage", "store.py")
        self.assertEqual(
            store.count("UPDATE messages SET metadata = ? WHERE id = ?"), 1,
            "store.py 只允许 update_message_metadata 那一条整块替换（其余一律键级 json_set）",
        )
        self.assertIn("json_set(", store, "键级合并必须真的落到 SQL 上")
        media = _read_source("naiba", "storage", "job_media.py")
        self.assertIn("merge_message_metadata", media, "产物写回必须按键合并")
        self.assertNotIn(
            "update_message_metadata", media,
            "整块替换会抹掉同一助手消息上的 session_start（用户点过「新会话」时）",
        )

    def test_merge_message_metadata_semantics(self) -> None:
        """合并的契约：只改传进来的键、返回是否命中、非法键名直接报错。"""
        self.assertTrue(
            self.storage.merge_message_metadata(
                self.cid, self.mid, {MetadataKeys.LOCAL_IMAGES_CAPPED: {"names": ["a.png"]}}
            )
        )
        metadata = self._metadata()
        self.assertEqual((metadata.get(MetadataKeys.LOCAL_IMAGES_CAPPED) or {}).get("names"), ["a.png"])
        self.assertIn(MetadataKeys.ATTACHMENTS, metadata, "其他键按当前值原样保留")
        self.assertFalse(
            self.storage.merge_message_metadata(self.cid, "ghost", {MetadataKeys.LOCAL_IMAGES_CAPPED: {}}),
            "消息不存在时必须返回 False（调用方据此记日志），而不是静默成功",
        )
        with self.assertRaises(ValueError):
            self.storage.merge_message_metadata(self.cid, self.mid, {"$.bad path": 1})
        with self.assertRaises(ValueError):
            self.storage.merge_message_metadata(self.cid, self.mid, {"bad-key": 1})

    def test_merge_message_metadata_advances_conversation_updated_at(self) -> None:
        """前端靠 `conversations.updated_at` 轮询感知变化——合并也必须推进它。"""
        before = int(self.storage.get_conversation(self.cid, include_messages=False)["updated_at"] or 0)
        with mock.patch("naiba.storage.store.time.time", return_value=(before / 1000) + 60):
            self.storage.merge_message_metadata(
                self.cid, self.mid, {MetadataKeys.LOCAL_IMAGES_CAPPED: {"names": ["a.png"]}}
            )
        after = int(self.storage.get_conversation(self.cid, include_messages=False)["updated_at"] or 0)
        self.assertGreater(after, before)


class InternalKeysNeverLeakTests(unittest.TestCase):
    """守门：``_message_id`` / ``_local_images_capped`` 绝不进 wire 消息。"""
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        paths = _write_images(Path(self.tmp.name), 1)
        self.addCleanup(self.tmp.cleanup)
        self.history = build_model_history(
            [_user_message("m1", paths)], local_image_brain=True
        )
        self.assertIn(HISTORY_MESSAGE_ID_KEY, self.history[0], "前置条件：内部键确实挂在历史项上")

    def test_history_item_carries_the_message_id(self) -> None:
        self.assertEqual(self.history[0][HISTORY_MESSAGE_ID_KEY], "m1")

    def test_no_wire_builder_emits_internal_keys(self) -> None:
        for name in ("_openai_messages", "_responses_input", "_ollama_messages"):
            converted = getattr(ProtocolMixins, name)(self.history)
            self.assertFalse(_contains_internal_key(converted), f"{name} 泄漏了内部键：{converted}")
        for name in ("_gemini_message", "_claude_message"):
            for item in self.history:
                converted = getattr(ProtocolMixins, name)(item)
                self.assertFalse(
                    _contains_internal_key(converted), f"{name} 泄漏了内部键：{converted}"
                )
        _system, lm_messages = ProtocolMixins._lm_studio_messages(self.history)
        self.assertFalse(_contains_internal_key(lm_messages), "lm_studio 泄漏了内部键")


def _contains_internal_key(node) -> bool:
    keys = {HISTORY_MESSAGE_ID_KEY, HISTORY_LOCAL_IMAGES_KEY}
    if isinstance(node, dict):
        if keys & set(node):
            return True
        return any(_contains_internal_key(value) for value in node.values())
    if isinstance(node, (list, tuple)):
        return any(_contains_internal_key(value) for value in node)
    return False


class MarkerSingleSourceTests(unittest.TestCase):
    """占位文案只有一份实现：vision 的线上改写 == core.history 的构造函数。"""

    def test_vision_uses_the_shared_marker(self) -> None:
        router = VisionRouter(_AppStub(VISION_CONFIG))
        history = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "两张图"},
                {"type": "image", "media_type": "image/jpeg", "data": "YWJj", "name": "a.png"},
                {"type": "image", "media_type": "image/jpeg", "data": "YWJj", "name": "b.png"},
            ],
            HISTORY_MESSAGE_ID_KEY: "m1",
        }]
        with mock.patch.object(VisionRouter, "LOCAL_REQUEST_IMAGE_LIMIT", 1):
            capped, _note, demotions = router._cap_local_history_images(history)
        self.assertEqual(
            _marker_texts(capped), [local_image_omitted_marker(["a.png"])],
            "vision 不能自带一份文案，否则旗标回放与线上改写会字节不同",
        )
        self.assertEqual(demotions, [{"message_id": "m1", "names": ["a.png"]}])


ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE_DOC = ROOT / "项目维护说明（修改代码前必读）.md"


def _read_source(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def _assert_has(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} 缺少 {needle!r}")


class FlagPlumbingTests(unittest.TestCase):
    """接线契约：漏一处传参 / 漏一处共用文案，就等于同一会话出现两种历史字节。"""

    def test_all_live_call_sites_pass_the_flag(self) -> None:
        for parts in (
            ("naiba", "run", "chat.py"),
            ("naiba", "subagent.py"),
            ("naiba", "plans.py"),
        ):
            _assert_has(
                _read_source(*parts), "local_image_brain=local_brain(profile)",
                "图片旗标判据透传（" + "/".join(parts) + "）",
            )

    def test_marker_and_internal_keys_have_a_single_owner(self) -> None:
        history_source = _read_source("naiba", "core", "history.py")
        for token in (
            "HISTORY_MESSAGE_ID_KEY", "HISTORY_LOCAL_IMAGES_KEY",
            "MetadataKeys.LOCAL_IMAGES_CAPPED", "local_omitted_image_names",
        ):
            _assert_has(history_source, token, "core/history.py")
        vision_source = _read_source("naiba", "vision", "runtime.py")
        _assert_has(vision_source, "local_image_omitted_marker(", "vision 必须复用共用文案")
        _assert_has(vision_source, "is_local_image_omitted_marker(", "vision 必须合并旧的占位块")
        _assert_has(
            _read_source("naiba", "run", "chat.py"), "_record_local_image_demotions",
            "chat 层落库旗标",
        )

    def test_maintenance_doc_records_the_contract(self) -> None:
        doc = MAINTENANCE_DOC.read_text(encoding="utf-8")
        for token in ("local_images_capped", "local_image_omitted_marker", "九.146"):
            _assert_has(doc, token, "维护说明")


if __name__ == "__main__":
    unittest.main()
