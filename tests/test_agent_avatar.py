# -*- coding: utf-8 -*-
"""Agent 自动 ID 与自定义头像的守门测试。

本轮三件事中的两件落在后端：
1. **不再让用户手填 Agent ID**：`ConfigStore.upsert_agent` 在 id 留空时分配
   `agent_<12 位 hex>` 形态的持久化唯一 id（跨重启不变、不与既有 id 冲突）；
2. **自定义头像**：`storage/avatars.py` 把上传图片**中心裁切为正方形**、缩到 256px、
   存 WebP（内容哈希命名，换图即换名且删除上一份），`/api/agents/avatar` 写入
   `agents[].avatar`，`/api/agents/avatar/<name>` 读取；
3. 头像字段必须在"表单保存不带头像"时被保留（否则每次改名都会清掉头像）。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.config import ConfigStore  # noqa: E402
from naiba.storage.avatars import (  # noqa: E402
    AVATAR_MAX_BYTES,
    AVATAR_SIZE,
    avatar_dir,
    is_avatar_filename,
    read_agent_avatar,
    store_agent_avatar,
)


def _png(size: tuple[int, int] = (800, 400), color: tuple[int, int, int] = (120, 80, 200)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class AgentAutoIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.store = ConfigStore(self.config_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_new_agent_gets_generated_unique_id(self) -> None:
        import re

        first = self.store.upsert_agent({"name": "甲", "system_prompt": "", "skill_ids": []})
        second = self.store.upsert_agent({"name": "乙", "system_prompt": "", "skill_ids": []})
        for agent in (first, second):
            with self.subTest(agent=agent["name"]):
                self.assertRegex(agent["id"], r"^agent_[0-9a-f]{12}$")
        self.assertNotEqual(first["id"], second["id"])

    def test_generated_id_is_persistent(self) -> None:
        created = self.store.upsert_agent({"name": "持久", "system_prompt": "", "skill_ids": []})
        reloaded = ConfigStore(self.config_path)
        ids = [agent["id"] for agent in reloaded.public_agents()]
        self.assertIn(created["id"], ids)
        again = reloaded.upsert_agent({"id": created["id"], "name": "改名", "system_prompt": "", "skill_ids": []})
        self.assertEqual(again["id"], created["id"])

    def test_explicit_id_still_accepted_for_api_compat(self) -> None:
        saved = self.store.upsert_agent({"id": "my_agent", "name": "手填", "system_prompt": "", "skill_ids": []})
        self.assertEqual(saved["id"], "my_agent")

    def test_invalid_explicit_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.upsert_agent({"id": "bad id!", "name": "非法", "system_prompt": "", "skill_ids": []})

    def test_avatar_preserved_when_form_omits_it(self) -> None:
        created = self.store.upsert_agent({"name": "带头像", "system_prompt": "", "skill_ids": []})
        self.store.upsert_agent({**created, "avatar": "abc_0123456789ab.webp"})
        # 表单保存只带 id/name/prompt/skills/scope —— 头像不能被清掉。
        updated = self.store.upsert_agent({
            "id": created["id"], "name": "改名", "system_prompt": "", "skill_ids": [],
        })
        self.assertEqual(updated["avatar"], "abc_0123456789ab.webp")
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        stored = next(item for item in saved["agents"] if item.get("id") == created["id"])
        self.assertEqual(stored["avatar"], "abc_0123456789ab.webp")


class AgentAvatarStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "data"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_center_crops_to_square_webp(self) -> None:
        from PIL import Image

        name = store_agent_avatar(self.data_dir, "agent_test", _png((800, 400)))
        data = read_agent_avatar(self.data_dir, name)
        self.assertIsNotNone(data)
        with Image.open(io.BytesIO(data)) as img:
            self.assertEqual(img.size, (AVATAR_SIZE, AVATAR_SIZE))
            self.assertEqual(img.format, "WEBP")

    def test_content_addressed_naming_and_old_file_removed(self) -> None:
        first = store_agent_avatar(self.data_dir, "agent_test", _png(color=(10, 20, 30)))
        second = store_agent_avatar(
            self.data_dir, "agent_test", _png(color=(200, 10, 10)), previous=first
        )
        self.assertNotEqual(first, second, "换图必须换文件名（否则浏览器缓存命中旧图）")
        self.assertFalse((avatar_dir(self.data_dir) / first).exists(), "旧头像文件应被删除")
        self.assertTrue((avatar_dir(self.data_dir) / second).exists())
        # 同样内容重复上传复用同一份文件名。
        again = store_agent_avatar(self.data_dir, "agent_test", _png(color=(200, 10, 10)))
        self.assertEqual(again, second)

    def test_rejects_non_image_and_oversized(self) -> None:
        with self.assertRaises(ValueError):
            store_agent_avatar(self.data_dir, "agent_test", b"not an image")
        with self.assertRaises(ValueError):
            store_agent_avatar(self.data_dir, "agent_test", b"x" * (AVATAR_MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            store_agent_avatar(self.data_dir, "agent_test", b"")

    def test_read_rejects_unsafe_names(self) -> None:
        for name in ("../config.json", "..\\config.json", "a/b.webp", "abc.webp", "", "x" * 200 + ".webp"):
            with self.subTest(name=name):
                self.assertFalse(is_avatar_filename(name))
                self.assertIsNone(read_agent_avatar(self.data_dir, name))


class AgentAvatarApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="naiba_agent_avatar_")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        from naiba.app import NaibaChatApp
        from naiba.paths import PathContext

        self.paths = PathContext.local(root, root / "config.json")
        self.app = NaibaChatApp(paths=self.paths)
        created = self.app.config.upsert_agent({"name": "头像接口", "system_prompt": "", "skill_ids": []})
        self.agent_id = str(created["id"])

    def test_set_and_read_avatar(self) -> None:
        payload, status = self.app.api_set_agent_avatar(self.agent_id, _png((300, 900)))
        self.assertEqual(int(status), 200)
        self.assertTrue(payload.get("ok"))
        name = str(payload["agent"]["avatar"])
        self.assertTrue(is_avatar_filename(name))
        # 落库
        agent = self.app.config.get_agent(self.agent_id)
        self.assertEqual(agent.get("avatar"), name)
        # 读取
        data, read_status = self.app.api_read_agent_avatar(name)
        self.assertEqual(int(read_status), 200)
        self.assertIsInstance(data, (bytes, bytearray))
        self.assertGreater(len(data), 0)

    def test_unknown_agent_and_bad_image(self) -> None:
        payload, status = self.app.api_set_agent_avatar("nope", _png())
        self.assertEqual(int(status), 404)
        self.assertIn("不存在", payload.get("error", ""))
        payload, status = self.app.api_set_agent_avatar(self.agent_id, b"broken")
        self.assertEqual(int(status), 400)
        self.assertIn("图片", payload.get("error", ""))

    def test_read_missing_avatar_404(self) -> None:
        payload, status = self.app.api_read_agent_avatar("agent_x_0123456789ab.webp")
        self.assertEqual(int(status), 404)


class AgentEmojiAvatarTests(unittest.TestCase):
    """内置 Agent 的 emoji 头像（avatar 字段的二义性）。

    `avatar` 字段要么是后端生成的文件名（`<id>_<hash>.webp`），要么是内置 Agent 的
    emoji 字形。两者混用就会出破图：`/api/agents/avatar/%F0%9F%93%98` 永远 404。
    所以后台必须拒绝把非文件名当文件读，前端必须分清两种取值（守门见下面的前端接线断言）。
    """

    def test_emoji_is_never_treated_as_an_avatar_filename(self) -> None:
        for value in ("📘", "🐱", "🎬", "✍️", "💻", "🧩", "abc.webp", "a_b.webp.bak"):
            with self.subTest(value=value):
                self.assertFalse(is_avatar_filename(value), "emoji / 非法名绝不能当头像文件读取")
        self.assertTrue(is_avatar_filename("abc_0123456789ab.webp"), "反向：正常文件名必须通过")

    def test_built_in_emoji_survives_upsert_and_public_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            # 表单保存不带头像键：内置 emoji 不能被顺手清掉。
            saved = store.upsert_agent({
                "id": "tutor", "name": "教程助手 Agent", "system_prompt": "改过", "skill_ids": [],
            })
            self.assertEqual(saved["avatar"], "📘", "表单保存不带头像时必须保留内置 emoji")
            agent = next(item for item in store.public_agents() if item["id"] == "tutor")
            self.assertEqual(agent["avatar"], "📘")

    def test_uploaded_avatar_replaces_emoji(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            saved = store.upsert_agent({
                "id": "tutor", "name": "教程助手 Agent", "system_prompt": "",
                "skill_ids": [], "avatar": "tutor_0123456789ab.webp",
            })
            self.assertEqual(saved["avatar"], "tutor_0123456789ab.webp", "上传头像必须能顶掉 emoji")

    def test_frontend_tells_emoji_and_filename_apart(self) -> None:
        core = (ROOT / "public/js/01-core.js").read_text(encoding="utf-8")
        for snippet in ("export function agentAvatarFile(", "export function agentAvatarEmoji(",
                        "export function agentAvatarSrc(", "AVATAR_FILE_RE", "AVATAR_EMOJI_MAX_LENGTH"):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, core, "头像取值判定必须集中定义在 01-core")
        self.assertIn(r"/^[A-Za-z0-9_-]+\.webp$/", core,
                      "文件名判定必须与后端 is_avatar_filename 同口径（只认 .webp + 含下划线）")

    def test_emoji_rendered_at_all_three_sites(self) -> None:
        """三处渲染点缺一不可：Agent 卡片、表单预览、助手消息圆标。"""
        settings = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        self.assertIn("agent-card-avatar-emoji", settings, "Agent 卡片要能画 emoji")
        messages = (ROOT / "public/js/04-messages.js").read_text(encoding="utf-8")
        self.assertIn("export function currentAgentAvatarEmoji(", messages)
        self.assertIn("message-avatar-emoji", messages, "助手消息圆标要能画 emoji")
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.assertIn('id="agentAvatarEmojiPreview"', index, "表单里要有 emoji 预览位")
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        for rule in (".agent-card-avatar-emoji {", ".message-avatar-emoji {",
                     ".agent-avatar-emoji-preview {", ".agent-avatar-emoji-preview[hidden] {"):
            with self.subTest(rule=rule):
                self.assertIn(rule, css)


if __name__ == "__main__":
    unittest.main()
