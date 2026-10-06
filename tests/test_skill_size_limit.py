# -*- coding: utf-8 -*-
"""守门：「Skill 大小上限」设置项（skill_max_size_mb）全链路口径。

三条铁律：
1. 默认 0 = 内置默认，五处上限与引入该设置前**逐字一致**（老用户行为零变化）；
2. 设置 > 0 时 UI 导入（zip 包/文件夹/解压后）、HTTP 请求体（×1.5 派生）、
   AI 帮装（install_skill / unpack_skill_archive）统一放宽到该值；
3. 任何超限报错文案必须带「设置 → Skills 管理 调大『Skill 大小上限』」指引——
   用户撞墙时必须知道有出口、出口在哪。
"""

import base64
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.app import NaibaChatApp  # noqa: E402
from naiba.config import (  # noqa: E402
    SKILL_FOLDER_MAX_MB_DEFAULT,
    SKILL_MAX_SIZE_MB_MAX,
    SKILL_TOOL_MAX_MB_DEFAULT,
    SKILL_UNPACKED_MAX_MB_DEFAULT,
    SKILL_ZIP_MAX_MB_DEFAULT,
    clamp_skill_max_size_mb,
)
from naiba.http import skill_import_body_limit_mb  # noqa: E402
from naiba.skills.install import (  # noqa: E402
    MAX_TOTAL_SIZE,
    validate_and_extract_archive,
)
from naiba.paths import PathContext  # noqa: E402

SKILL_MD = "---\nname: size-limit-skill\ndescription: 大小上限守门技能\n---\n\n正文\n"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _zip_bytes(member_size: int) -> bytes:
    """打一个压缩包：单个 SKILL.md + 一个 member_size 字节的大文件。

    内容用「1MB 随机 + 其余补零」：纯零的压缩比远超 100x 会先误触 zip 炸弹
    防线（那也是要守的行为），混合内容把压缩比压到远低于 100x，
    才能测到「解压后体积」这条检查本身。
    """
    filler = b"\x00" * max(0, member_size - 1024 * 1024)
    content = os.urandom(min(member_size, 1024 * 1024)) + filler
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("skill/SKILL.md", SKILL_MD)
        archive.writestr("skill/big.bin", content)
    return buf.getvalue()


class ClampTests(unittest.TestCase):
    def test_clamp_edges(self):
        self.assertEqual(clamp_skill_max_size_mb(0), 0)
        self.assertEqual(clamp_skill_max_size_mb(300), 300)
        self.assertEqual(clamp_skill_max_size_mb(5), 10, "低于下限夹到 10")
        self.assertEqual(clamp_skill_max_size_mb(9999), SKILL_MAX_SIZE_MB_MAX)
        self.assertEqual(clamp_skill_max_size_mb("abc"), 0)
        self.assertEqual(clamp_skill_max_size_mb(None), 0)
        # bool 按 int 处理（True=1 → 夹到下限 10），与 clamp_chat_font_size 同款先例。
        self.assertEqual(clamp_skill_max_size_mb(True), 10)


class HttpBodyLimitTests(unittest.TestCase):
    def test_default_is_global_130mb(self):
        self.assertEqual(skill_import_body_limit_mb(0), 130)

    def test_derived_from_setting(self):
        self.assertEqual(skill_import_body_limit_mb(300), 450)
        self.assertEqual(skill_import_body_limit_mb(2048), 3072)

    def test_floor_at_global_default(self):
        self.assertEqual(skill_import_body_limit_mb(10), 130, "小设置不放宽请求体下限")


class ConfigStoreSkillLimitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.app = NaibaChatApp(paths=PathContext.local(root, root / "config.json"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_zero_and_builtin_limits_reach_public(self):
        self.assertEqual(self.app.config.get_skill_max_size_mb(), 0)
        limits = self.app.config.skill_size_limits()
        self.assertEqual(limits["zip_mb"], SKILL_ZIP_MAX_MB_DEFAULT)
        self.assertEqual(limits["folder_mb"], SKILL_FOLDER_MAX_MB_DEFAULT)
        self.assertEqual(limits["unpacked_mb"], SKILL_UNPACKED_MAX_MB_DEFAULT)
        self.assertEqual(limits["tool_mb"], SKILL_TOOL_MAX_MB_DEFAULT)
        self.assertEqual(limits["setting_mb"], 0)
        public = self.app.config.public()
        self.assertEqual(public["skill_size_limits"], limits, "public() 必须整体下发生效值")

    def test_update_settings_valid_values(self):
        for value in (0, 10, 300, SKILL_MAX_SIZE_MB_MAX):
            self.app.config.update_settings({"skill_max_size_mb": value})
            self.assertEqual(self.app.config.get_skill_max_size_mb(), value)

    def test_update_settings_rejects_invalid(self):
        for bad in (5, SKILL_MAX_SIZE_MB_MAX + 1, -1, "abc", True):
            with self.assertRaises(ValueError):
                self.app.config.update_settings({"skill_max_size_mb": bad})

    def test_setting_overrides_all_limits(self):
        self.app.config.update_settings({"skill_max_size_mb": 1024})
        limits = self.app.config.skill_size_limits()
        self.assertEqual(
            (limits["zip_mb"], limits["folder_mb"], limits["unpacked_mb"], limits["tool_mb"]),
            (1024, 1024, 1024, 1024),
        )
        self.assertEqual(limits["setting_mb"], 1024)

    def test_load_path_clamps_hand_edited_config(self):
        self.app.config.data["skill_max_size_mb"] = 99999
        self.assertEqual(self.app.config.get_skill_max_size_mb(), SKILL_MAX_SIZE_MB_MAX)


class InstallEndpointLimitTests(unittest.TestCase):
    """UI 导入端点：设置 > 0 时放宽，报错文案必须带设置指引。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        (root / "skills").mkdir()
        (root / "data" / "skills").mkdir(parents=True)
        self.app = NaibaChatApp(paths=PathContext.local(root, root / "config.json"))

    def tearDown(self):
        self._tmp.cleanup()

    def _set_limit(self, mb):
        self.app.config.update_settings({"skill_max_size_mb": mb})

    def test_folder_over_limit_rejected_with_guidance(self):
        self._set_limit(10)
        payload, status = self.app._install_folder(
            {"files": [{"path": "SKILL.md", "data": _b64(SKILL_MD.encode("utf-8"))},
                       {"path": "big.bin", "data": _b64(b"\x00" * (11 * 1024 * 1024))}]}
        )
        self.assertEqual(int(status), 413, payload)
        self.assertIn("10 MB", str(payload.get("error")))
        self.assertIn("Skill 大小上限", str(payload.get("error")))

    def test_folder_under_limit_accepted(self):
        self._set_limit(10)
        payload, status = self.app._install_folder(
            {"files": [{"path": "SKILL.md", "data": _b64(SKILL_MD.encode("utf-8"))}]}
        )
        self.assertEqual(int(status), 200, payload)

    def test_zip_body_over_limit_rejected_with_guidance(self):
        self._set_limit(10)
        payload, status = self.app._install_skill(
            {"name": "big.zip", "data": _b64(b"\x00" * (11 * 1024 * 1024))}
        )
        self.assertEqual(int(status), 413, payload)
        self.assertIn("10 MB", str(payload.get("error")))
        self.assertIn("Skill 大小上限", str(payload.get("error")))

    def test_zip_unpacked_over_limit_rejected_even_when_compressed_small(self):
        self._set_limit(10)
        payload, status = self.app._install_skill(
            {"name": "bomb.zip", "data": _b64(_zip_bytes(11 * 1024 * 1024))}
        )
        # zip 包本体（~1MB）远小于上限，撞的是「解压后体积」这条检查（400）。
        self.assertEqual(int(status), 400, payload)
        self.assertIn("解压后", str(payload.get("error")))
        self.assertIn("Skill 大小上限", str(payload.get("error")))

    def test_default_limits_reach_endpoints(self):
        """设置 0 时生效值必须等于内置默认（老用户行为零变化的直接断言）。"""
        limits = self.app.config.skill_size_limits()
        self.assertEqual(limits["zip_mb"], 80)
        self.assertEqual(limits["folder_mb"], 300)
        self.assertEqual(limits["unpacked_mb"], 500)
        self.assertEqual(limits["tool_mb"], 50)


class ToolChainLimitTests(unittest.TestCase):
    """AI 帮装链路（skills/install.py）：参数化上限，None = 内置 50MB。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        (root / "skills").mkdir()
        (root / "data" / "skills").mkdir(parents=True)
        self.app = NaibaChatApp(paths=PathContext.local(root, root / "config.json"))
        self.managed = self.app.config.resolve_managed_skills_dir().resolve()
        self.incoming = root / "workspace" / ".skill_incoming"

    def tearDown(self):
        self._tmp.cleanup()

    def test_builtin_default_matches_config_constant(self):
        self.assertEqual(MAX_TOTAL_SIZE, SKILL_TOOL_MAX_MB_DEFAULT * 1024 * 1024)

    def test_default_none_uses_builtin_limit(self):
        archive = Path(self._tmp.name) / "big.zip"
        # 60MB 解压后 > 内置默认 50MB；混合内容压缩后 ~1MB，不会误触 zip 炸弹防线。
        archive.write_bytes(_zip_bytes(60 * 1024 * 1024))
        result = validate_and_extract_archive(archive, self.incoming)
        self.assertFalse(result["success"])
        self.assertIn("50 MB", result["error"])
        self.assertIn("Skill 大小上限", result["error"])

    def test_custom_limit_relaxes_tool_chain(self):
        archive = Path(self._tmp.name) / "big.zip"
        archive.write_bytes(_zip_bytes(11 * 1024 * 1024))
        result = validate_and_extract_archive(archive, self.incoming, max_total_bytes=64 * 1024 * 1024)
        self.assertTrue(result["success"], result)

    def test_capability_passes_setting(self):
        runtime_limit = self.app.config.get_skill_max_size_mb()
        self.assertEqual(runtime_limit, 0)
        self.app.config.update_settings({"skill_max_size_mb": 300})
        # capability.CapabilityRuntime._skill_max_bytes：0 → None（内置默认），>0 → 字节。
        from naiba.capability import CapabilityRuntime

        runtime = CapabilityRuntime(self.app)
        self.app.config.update_settings({"skill_max_size_mb": 0})
        self.assertIsNone(runtime._skill_max_bytes())
        self.app.config.update_settings({"skill_max_size_mb": 300})
        self.assertEqual(runtime._skill_max_bytes(), 300 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
