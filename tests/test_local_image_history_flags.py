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
    """chat 层落库：合并只增不减、不清空其它 metadata、缺 id 就跳过。"""

    class _Storage:
        def __init__(self, messages: list[dict]):
            self.messages = messages
            self.writes: list[tuple[str, dict]] = []

        def get_conversation(self, _conversation_id: str) -> dict:
            return {"messages": self.messages}

        def update_message_metadata(
            self, _conversation_id: str, message_id: str, metadata: dict
        ) -> bool:
            for message in self.messages:
                if message["id"] == message_id:
                    message["metadata"] = metadata
                    self.writes.append((message_id, metadata))
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

    def test_merges_monotonically_and_keeps_other_metadata(self) -> None:
        self.host._record_local_image_demotions(
            "c1", [{"message_id": "m1", "names": ["a.png"]}]
        )
        self.host._record_local_image_demotions(
            "c1", [{"message_id": "m1", "names": ["b.png", "a.png"]}]
        )
        self.assertEqual(self._flag(), ["a.png", "b.png"], "旗标只增不减，且不去重两次")
        self.assertEqual(
            self.messages[0]["metadata"].get("attachments"),
            [{"path": "a.png", "name": "a.png"}],
            "写旗标绝不能清空 attachments 等既有 metadata（update_message_metadata 是整块替换）",
        )

    def test_skips_entries_without_message_id(self) -> None:
        self.host._record_local_image_demotions("c1", [{"message_id": "", "names": ["a.png"]}])
        self.host._record_local_image_demotions("c1", [{"message_id": "m1", "names": []}])
        self.host._record_local_image_demotions("c1", [{"message_id": "ghost", "names": ["x.png"]}])
        self.assertEqual(self.storage.writes, [], "无 id / 无名字 / 消息不存在都不应写库")

    def test_write_failure_does_not_raise(self) -> None:
        def boom(*_args, **_kwargs):
            raise RuntimeError("库锁")

        self.storage.update_message_metadata = boom  # type: ignore[method-assign]
        self.host._record_local_image_demotions(
            "c1", [{"message_id": "m1", "names": ["a.png"]}]
        )
        self.assertEqual(self._flag(), [], "写库失败只记日志，本轮照常发请求")


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
