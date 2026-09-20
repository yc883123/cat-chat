# -*- coding: utf-8 -*-
"""显示名品牌守门：产品一律叫 `Cat Chat`，内部身份一律保留 `naiba` 系。

背景：产品从 Naiba Chat 改显示名为 Cat Chat（见「项目维护说明 §2.1 命名契约」）。
这次只改人看得见的称呼——浏览器标签、桌面窗口标题、托盘悬停标题、原生对话框标题、
启动横幅与 CLI 帮助、界面提示文案；仓库名、包名、EXE 名、更新资产、数据位置、
AppUserModelID、浏览器存储键、协议标识一律不动。

本文件守两件事，缺一不可：

1. **显示文案必须已经是 `Cat Chat`**（且旧称呼不得残留在当前界面入口里）；
2. **机器依赖的内部标识必须还是原值**——这是同名回归最容易踩的地方：
   有人「顺手全替换」就会把 localStorage 键名一起改掉，老用户偏好全部丢失；
   把托盘内部 name 改成 cat-chat 会让系统把托盘图标当成另一个应用；
   把 `server_version` 改掉会动到 HTTP 协议标识。

刻意不做的事：不启动真实服务、不读真实配置、不联网、不按固定行号断言、
不把「全仓库搜不到 naiba」当判据（仓库名、包名、协议标识本来就该留着）。
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BRAND = "Cat Chat"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _attr_calls(tree: ast.AST, attr_name: str, base_name: str | None = None) -> list[ast.Call]:
    """收集 `某对象.attr(...)` 形式的调用；给了 base_name 就只认该名字的直接属性调用。"""
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != attr_name:
            continue
        if base_name is not None:
            base = func.value
            if not (isinstance(base, ast.Name) and base.id == base_name):
                continue
        found.append(node)
    return found


def _const(node: ast.AST | None) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _raise_pairs(tree: ast.AST) -> list[tuple[str | None, object]]:
    """收集 (异常类型名, 首个参数常量)——用来确认报错文案改了、异常类型没改。"""
    pairs: list[tuple[str | None, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        func = node.exc.func
        name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
        pairs.append((name, _const(node.exc.args[0] if node.exc.args else None)))
    return pairs


class FrontendDisplayBrandingTests(unittest.TestCase):
    """浏览器里能看到的称呼。"""

    def test_page_title(self):
        self.assertIn("<title>Cat Chat</title>", _read("public/index.html"))

    def test_welcome_wizard_heading(self):
        html = _read("public/index.html")
        self.assertIn("<h2>欢迎使用 Cat Chat</h2>", html)
        self.assertNotIn("欢迎使用 Naiba Chat", html)

    def test_appearance_section_sentence(self):
        self.assertIn("偏好会保存到当前 Cat Chat 服务。", _read("public/index.html"))

    def test_datadir_placeholder_uses_brand_but_keeps_original_format(self):
        html = _read("public/index.html")
        line = next(l for l in html.splitlines() if 'id="dataDir"' in l)
        self.assertIn("CatChatData", line)
        # 只换品牌词：placeholder 里原有的「盘符 + 两个反斜杠」写法保持原样，不夹带格式修正。
        self.assertEqual(line.count("\\"), 2, "placeholder 的反斜杠写法被改动了，本次只应换品牌词")

    def test_migration_hint_both_entries(self):
        self.assertIn("迁移后需完全退出并重启 Cat Chat 生效。", _read("public/index.html"))
        self.assertIn("请完全退出并重新启动 Cat Chat 生效。", _read("public/js/15-bind-events.js"))

    def test_bootstrap_lan_access_hint(self):
        js = _read("public/js/05-bootstrap.js")
        # 两处：按钮禁用时的状态说明 + toast
        self.assertEqual(js.count("手机访问已启用，请完全退出并重新启动 Cat Chat"), 2)
        self.assertIn("手机访问已启用，请完全退出并重新启动 Cat Chat。", js)
        # 「完全退出并」是操作要求，不是品牌词，不许被顺手删掉
        self.assertNotIn("naiba-chat", js)

    def test_settings_vision_hint(self):
        self.assertIn("Cat Chat 会把用户图片直接交给该模型。", _read("public/js/09-settings.js"))
        self.assertNotIn("Naiba-chat", _read("public/js/09-settings.js"))

    def test_no_stale_display_branding_left_in_current_frontend(self):
        for relative in (
            "public/index.html",
            "public/js/05-bootstrap.js",
            "public/js/09-settings.js",
            "public/js/15-bind-events.js",
        ):
            text = _read(relative)
            for stale in ("Naiba Chat", "NaibaChat", "Naiba-chat", "NaibaChatData"):
                self.assertNotIn(stale, text, f"{relative} 里还残留旧显示名 {stale!r}")

    def test_actual_targets_keep_old_identifier(self):
        """这两处指的是真东西的名字，不是产品称呼——改了会说错话。"""
        html = _read("public/index.html")
        # 防火墙放行对象是实际 EXE 名，EXE 仍叫 naiba-chat
        self.assertIn("Windows 防火墙需要允许 naiba-chat 或当前端口通过", html)
        # 更新仓库仍是 naiba-chat
        self.assertIn("更新来自 yc883123/naiba-chat", html)
        self.assertIn("https://github.com/yc883123/naiba-chat/releases", _read("public/js/07-models-agents.js"))


class FrontendStorageKeyTests(unittest.TestCase):
    """浏览器存储键是用户偏好的落脚点，改名时必须一个字都不动。"""

    KEPT_KEYS = (
        "naibaChatAppearance",
        "naibaChatToken",
        "naibaChatSidebarW",
        "naibaChatFilePanelW",
        "naibaChatSidebarCollapsed",
        "naibaChatSkillIds",
        "naibaChatSkillMode",
        "naibaChatAutoSkills",
        "naibaChatInteractionMode",
        "naibaOnboardingDismissed",
        "naiba.agentToolTemplates",
    )

    def test_kept_keys_still_declared(self):
        blob = "\n".join(_read(p) for p in (
            "public/index.html",
            "public/js/01-core.js",
            "public/js/05-bootstrap.js",
            "public/js/06-tasks-plans.js",
            "public/js/08-conversations.js",
            "public/js/09-settings.js",
            "public/js/14-file-panel.js",
            "public/js/15-bind-events.js",
            "public/js/19-onboarding.js",
        ))
        for key in self.KEPT_KEYS:
            self.assertIn(key, blob, f"存储键 {key!r} 不见了——改名不得动存储键")

    def test_no_cat_prefixed_replacement_key(self):
        """不许为改名新建一套 cat 前缀的键，否则等于把老用户设置全部作废。"""
        for relative in (
            "public/index.html",
            "public/js/01-core.js",
            "public/js/05-bootstrap.js",
            "public/js/09-settings.js",
            "public/js/14-file-panel.js",
            "public/js/15-bind-events.js",
            "public/js/19-onboarding.js",
        ):
            self.assertNotIn("catChat", _read(relative))
            self.assertNotIn("cat-chat", _read(relative))

    def test_legacy_lan_keys_kept_for_compat_read(self):
        core = _read("public/js/01-core.js")
        for legacy in ("lanSkillToken", "lanSkillIds", "lanAutoSkills"):
            self.assertIn(legacy, core, f"兼容读取用的 {legacy!r} 被删了")


class DesktopShellBrandingTests(unittest.TestCase):
    """桌面壳：显示标题换品牌，系统身份保持原值（用 AST 区分两者，不靠行号）。"""

    @classmethod
    def setUpClass(cls):
        cls.source = _read("launcher.py")
        cls.tree = ast.parse(cls.source)

    def test_tray_internal_name_kept_visible_title_rebranded(self):
        calls = _attr_calls(self.tree, "Icon", base_name="pystray")
        self.assertEqual(len(calls), 1, "托盘创建点应恰好一处")
        args = calls[0].args
        self.assertGreaterEqual(len(args), 3, "pystray.Icon(name, image, title, menu) 参数变少了")
        self.assertEqual(_const(args[0]), "naiba-chat", "托盘内部 name 是系统标识，必须保留")
        self.assertEqual(_const(args[2]), BRAND, "托盘悬停标题应为显示名")

    def test_window_title_rebranded(self):
        calls = _attr_calls(self.tree, "create_window", base_name="webview")
        self.assertEqual(len(calls), 1)
        self.assertEqual(_const(calls[0].args[0]), BRAND, "桌面窗口标题应为显示名")

    def test_startup_error_messagebox_title_rebranded(self):
        calls = _attr_calls(self.tree, "MessageBoxW")
        self.assertEqual(len(calls), 1)
        self.assertEqual(_const(calls[0].args[2]), "Cat Chat 启动失败")

    def test_app_user_model_id_kept(self):
        calls = _attr_calls(self.tree, "SetCurrentProcessExplicitAppUserModelID")
        self.assertEqual(len(calls), 1)
        self.assertEqual(_const(calls[0].args[0]), "naiba.chat", "AppUserModelID 改了会被系统当成另一个应用")

    def test_webview_profile_still_persistent(self):
        """持久化 profile 一松手，用户的前端偏好每次退出都会被清空。"""
        self.assertIn('start_kwargs["private_mode"] = False', self.source)
        self.assertIn('start_kwargs["storage_path"]', self.source)
        self.assertIn('app_dir / "webview"', self.source)


class BackendBrandingTests(unittest.TestCase):
    """后端的用户可见文案：原生目录选择器、实例锁提示、CLI、MCP 注册错误。"""

    def test_workspace_directory_picker_title(self):
        source = _read("naiba/app.py")
        self.assertIn('title="选择 Cat Chat 工作区目录"', source)
        self.assertNotIn("选择 NaibaChat 工作区目录", source)

    def test_instance_lock_message_is_runtime_error(self):
        source = _read("naiba/http.py")
        pairs = _raise_pairs(ast.parse(source))
        self.assertIn(("RuntimeError", "Cat Chat 已经在运行，请勿重复启动"), pairs)
        self.assertFalse(
            [p for p in pairs if p[1] == "naiba-chat 已经在运行，请勿重复启动"],
            "旧的实例锁文案还在",
        )

    def test_cli_description_and_banner(self):
        source = _read("naiba/http.py")
        self.assertIn('argparse.ArgumentParser(description="Cat Chat 局域网对话服务")', source)
        self.assertIn('print("\\nCat Chat 已启动")', source)

    def test_mcp_register_error_rebranded(self):
        source = _read("naiba/tools/providers/core.py")
        self.assertIn('"当前 Cat Chat 版本不支持自动注册 MCP"', source)
        self.assertNotIn("当前 NaibaChat 版本不支持自动注册 MCP", source)

    def test_server_version_is_protocol_identity_and_kept(self):
        import naiba.http  # noqa: PLC0415 —— 只在需要时导入，避免拖慢其它用例

        self.assertEqual(naiba.http.RequestHandler.server_version, "naiba-chat/1.0")

    @unittest.skipUnless(
        (ROOT / "start.bat").is_file(),
        "start.bat 是本机 Windows 启动脚本，已被 .gitignore 排除、不随仓库分发；"
        "CI/新克隆上没有这个文件，跳过而不是判红",
    )
    def test_start_script_banner(self):
        bat = _read("start.bat")
        self.assertIn("title Cat Chat local server", bat)
        self.assertIn("echo   Cat Chat server starting...", bat)
        self.assertNotIn("naiba-chat", bat)


class UpdateChannelTests(unittest.TestCase):
    """更新通路是机器依赖的，改名不得碰。"""

    def test_updater_constants_unchanged(self):
        from naiba import updater  # noqa: PLC0415

        self.assertEqual(updater.REPOSITORY, "yc883123/naiba-chat")
        self.assertEqual(updater.MANIFEST_ASSET, "naiba-chat-update.json")
        self.assertEqual(updater.EXECUTABLE_ASSET, "naiba-chat.exe")

    def test_updater_still_rejects_foreign_repo_by_old_name(self):
        from naiba import updater  # noqa: PLC0415

        self.assertIn("不是受支持的 naiba-chat Git 仓库", Path(updater.__file__).read_text(encoding="utf-8"))


class CurrentDocsBrandingTests(unittest.TestCase):
    """三份当前文档：自称新名，并向前兼容地说明「原名」与「不用搬数据」。"""

    def test_readme(self):
        readme = _read("README.md")
        self.assertTrue(readme.startswith("# Cat Chat "), "README 首行标题应已是新显示名")
        self.assertIn("原名 Naiba Chat", readme)
        self.assertIn("无需重新配置或搬迁数据", readme)
        # 下载文件名与仓库地址仍是旧标识
        self.assertIn("naiba-chat-", readme)
        self.assertIn("github.com/yc883123/naiba-chat", readme)

    def test_changelog(self):
        self.assertIn("Cat Chat（原 Naiba Chat）", _read("CHANGELOG.md"))

    def test_maintenance_doc_naming_contract(self):
        doc = _read("项目维护说明（修改代码前必读）.md")
        self.assertIn("### 2.1 命名契约（显示名 vs 内部标识）", doc)
        self.assertIn("**Cat Chat**（原名 Naiba Chat）", doc)
        self.assertIn("tests/test_display_branding.py", doc)


if __name__ == "__main__":
    unittest.main()
