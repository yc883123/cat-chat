# -*- coding: utf-8 -*-
"""守门：「插话直达」开关（设置 → 运行设置 → 插话）。

需求（2026-10-01 定稿）：开关打开后，运行中输入插话**不必再点「引导」**，直接就发给 AI
（下一步生效），像 Codex 那样。四条不可退让的口径：

1. 偏好键 ``settings.interject_direct_send``：**布尔、默认 False**——默认必须维持旧版
   两段式（入队 → 用户显式点「引导」）；且只收真布尔，JSON 里的字符串 ``"false"`` 必须
   显式报错，不能被 ``bool()`` 判成 True（那会变成「开关反着来」，最难查的一类失灵）。
2. 服务端 settings 是唯一事实来源：经 ``update_settings`` 落盘、``public()`` 随 bootstrap
   下发（前端读 ``state.bootstrap.settings.interject_direct_send``），不自造 localStorage 影子。
3. 前端实现口径：入队成功后**复用 `guideInterjection`**，不准另打接口或自己拼引导请求——
   拒掉待确认工具卡 / `user_guidance` 事件 / 队列行转「已引导」全在那一条路径上。
4. 兜底：自动引导失败时该行留在队列（仍为待引导），可手动引导 / 取回 / 删除，字不会丢。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.messages import MetadataKeys  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402

AGENT = {"id": "", "name": "Chat", "system_prompt": "", "skill_ids": []}
RUNTIME_PANEL_ATTR = 'data-settings-panel="runtime"'


def _config_app():
    """建一个隔离实例，返回 (app, tmp)。配置写在临时目录里，不污染真实数据目录。"""
    from naiba.app import NaibaChatApp
    from naiba.paths import PathContext

    tmp = tempfile.TemporaryDirectory(prefix="naiba_ijd_")
    root = Path(tmp.name)
    return NaibaChatApp(paths=PathContext.local(root, root / "config.json")), tmp


class InterjectDirectSendSettingsTests(unittest.TestCase):
    """偏好键本身：默认关闭、可落盘、下发、只收布尔。"""

    def setUp(self) -> None:
        self.app, self._tmp = _config_app()
        self.addCleanup(self._tmp.cleanup)

    def test_default_is_off_and_published_in_bootstrap_payload(self) -> None:
        public = self.app.config.public()
        self.assertIn("interject_direct_send", public, "偏好键没有随 bootstrap 下发")
        self.assertIs(public["interject_direct_send"], False, "默认必须是关闭（维持两段式旧行为）")
        # 默认值也必须在"新装配置文件"里真的写出来，而不是只活在 public() 的兜底里。
        saved = json.loads((Path(self._tmp.name) / "config.json").read_text(encoding="utf-8"))
        self.assertIs(saved.get("interject_direct_send"), False)

    def test_toggle_persists_across_reload(self) -> None:
        self.app.config.update_settings({"interject_direct_send": True})
        self.assertIs(self.app.config.public()["interject_direct_send"], True)
        # 重新加载配置文件（模拟重启）：开关值必须还在。
        from naiba.config import ConfigStore

        reloaded = ConfigStore(Path(self._tmp.name) / "config.json")
        self.assertIs(reloaded.public()["interject_direct_send"], True)
        # 关回去同样要落盘。
        self.app.config.update_settings({"interject_direct_send": False})
        self.assertIs(self.app.config.public()["interject_direct_send"], False)

    def test_rejects_non_boolean(self) -> None:
        """字符串 / 数字 / None 一律显式报错：静默接受 "false" 会让开关反着走。"""
        for bad in ("true", "false", 1, 0, None, []):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.app.config.update_settings({"interject_direct_send": bad})
        self.assertIs(self.app.config.public()["interject_direct_send"], False)


class InterjectImmediateGuideTests(unittest.TestCase):
    """「入队 → 立即引导」这条时序（开关打开时前端连打两次请求）必须产出可被 agent 取走的行。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="naiba_ijd_queue_")
        self.addCleanup(self.tmp.cleanup)
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation_id = str(self.storage.create_conversation()["id"])
        run = self.storage.create_run(self.conversation_id, "回答", AGENT, {}, kind="chat")
        self.run_id = str(run["id"])

    def _metadata(self, message_id: str) -> dict:
        with self.storage._connect() as db:  # noqa: SLF001 - 公开读接口不回传 metadata
            row = db.execute("SELECT metadata FROM messages WHERE id = ?", (message_id,)).fetchone()
        return json.loads(row["metadata"] or "{}")

    def test_enqueue_then_immediate_guide_is_pullable(self) -> None:
        saved = self.storage.add_run_interjection(self.conversation_id, self.run_id, "改成竖屏")
        self.assertEqual(self.storage.list_run_interjections(self.run_id), [])
        guided = self.storage.guide_run_interjection(self.conversation_id, self.run_id, saved["id"])
        self.assertTrue(guided["metadata"][MetadataKeys.INTERJECTION_GUIDED])
        pullable = self.storage.list_run_interjections(self.run_id)
        self.assertEqual([item["id"] for item in pullable], [saved["id"]])
        self.assertEqual(pullable[0]["content"], "改成竖屏")

    def test_guided_row_keeps_flag_visible_for_panel(self) -> None:
        """开关打开时面板仍要能画出行（本人指定：保留「已引导」短暂显示）：
        guided 且未消费 = 既不在可见消息流里、又还在可拉取队列里。"""
        saved = self.storage.add_run_interjection(self.conversation_id, self.run_id, "换成 4K")
        self.storage.guide_run_interjection(self.conversation_id, self.run_id, saved["id"])
        metadata = self._metadata(saved["id"])
        self.assertTrue(metadata[MetadataKeys.INTERJECTION])
        self.assertTrue(metadata[MetadataKeys.INTERJECTION_GUIDED])
        self.assertFalse(metadata.get(MetadataKeys.INTERJECTION_CONSUMED))
        self.assertEqual(len(self.storage.list_run_interjections(self.run_id)), 1)

    def test_guide_after_run_ended_is_refused(self) -> None:
        """兜底口径：Run 已结束再自动引导会失败（LookupError）——这条失败必须可被前端捕获，
        以便走「已进入插话队列：直达发送未成功」那条提示，而不是把内容丢掉。"""
        saved = self.storage.add_run_interjection(self.conversation_id, self.run_id, "别删我")
        self.storage.update_background_task(self.run_id, status="completed", finished=True)
        with self.assertRaises(LookupError):
            self.storage.guide_run_interjection(self.conversation_id, self.run_id, saved["id"])
        # 行还在库里、仍是待引导：前端「取回 / 删除」两个出口照旧可用。
        metadata = self._metadata(saved["id"])
        self.assertTrue(metadata[MetadataKeys.INTERJECTION])
        self.assertFalse(metadata.get(MetadataKeys.INTERJECTION_GUIDED))


class InterjectDirectSendFrontendTests(unittest.TestCase):
    """前端静态结构：设置项、即时保存、入队后自动引导，三处都不许退化。"""

    def setUp(self) -> None:
        self.index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.settings = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        self.bind = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")
        self.interjections = (ROOT / "public/js/18-interjections.js").read_text(encoding="utf-8")

    def test_checkbox_lives_in_runtime_panel(self) -> None:
        self.assertIn('id="interjectDirectSend"', self.index, "设置页缺少开关控件")
        start = self.index.index(RUNTIME_PANEL_ATTR)
        panel = self.index[start:]
        panel = panel[: panel.index('data-settings-panel="connections"')]
        self.assertIn('id="interjectDirectSend"', panel, "开关必须落在「运行设置」面板内")
        self.assertIn("插话直达", panel, "开关标题不见了")
        self.assertIn("type=\"checkbox\"", panel, "开关必须是勾选框")

    def test_settings_populates_from_server_payload(self) -> None:
        self.assertIn("settings.interject_direct_send", self.settings, "回填没有读服务端偏好")
        self.assertIn("$('#interjectDirectSend').checked", self.settings)

    def test_save_is_immediate_and_rolls_back_on_failure(self) -> None:
        self.assertIn("export async function saveInterjectDirectSend(", self.settings)
        block = self.settings[self.settings.index("export async function saveInterjectDirectSend("):]
        block = block[: block.index("\n}\n")]
        self.assertIn("body: { interject_direct_send: value }", block, "没有走服务端 settings 单项提交")
        self.assertIn("checked = !value", block, "写失败没有回滚勾选框（会变成「看着开了其实没开」）")
        # 即时生效：绑定在 change 上，不进「保存参数」的 payload。
        self.assertIn("$('#interjectDirectSend')?.addEventListener('change'", self.bind)
        self.assertIn("saveInterjectDirectSend(event.target.checked)", self.bind)
        runtime_payload = self.settings[self.settings.index("export async function saveRuntimeSettings("):]
        runtime_payload = runtime_payload[: runtime_payload.index("toast('运行参数已保存')")]
        self.assertNotIn("interject_direct_send", runtime_payload, "开关不该被「保存参数」再写一遍")

    def test_send_path_reuses_guide_interjection(self) -> None:
        self.assertIn("function interjectDirectSendEnabled()", self.interjections)
        self.assertIn("state.bootstrap?.settings?.interject_direct_send", self.interjections)
        block = self.interjections[self.interjections.index("export async function sendRunInterjection("):]
        block = block[: block.index("export async function reuseInterjection(")]
        self.assertIn("interjectDirectSendEnabled()", block, "发送路径没有读开关")
        self.assertIn("await guideInterjection(message.id)", block, "入队后没有自动引导")
        # 默认（开关关）那条提示必须保留：旧行为不能被顺手改掉。
        self.assertIn("已加入插话队列", block, "关闭开关时的入队提示不见了")
        # 失败兜底：引导失败要给出可重试的说明，而不是谎报已发送。
        self.assertIn("直达发送未成功", block, "自动引导失败没有兜底提示")


if __name__ == "__main__":
    unittest.main()
