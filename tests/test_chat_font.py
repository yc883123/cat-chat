# -*- coding: utf-8 -*-
"""会话区字体设置（appearance.chat_font_*）的前端契约守门。

后端校验/归一化在 `tests/test_appearance.py`；这里钉的是"设置在浏览器里真的生效"
这条链路——它是纯源码断言（项目惯例：护栏要断言到可用性，不是"节点存在"）：

- `.message-body` 必须消费 `--msg-font-size/--msg-font-family`（还写死 px 就等于设置无效）；
- `:root` 的默认值必须与历史硬编码值**完全一致**（没设过的用户视觉零变化）；
- 变量只挂在 `:root` 上、只改这两个变量（挂 body 字号会连带侧栏/按钮一起变）；
- 只传 theme/skin 的老调用点不得把字号字体重置（"保存外观"会整体 POST
  `state.appearance`，漏一处的表现就是"改完主题，字体自己回去了"）；
- 滑块 `input` 路径绝不能落库（拖一次打几十个 API 是最典型的性能事故）。
"""

import sys
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.config import APPEARANCE_CHAT_FONT_FAMILIES  # noqa: E402


class ChatFontFrontendTests(unittest.TestCase):
    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def _core(self) -> str:
        return (ROOT / "public/js/01-core.js").read_text(encoding="utf-8")

    def _settings(self) -> str:
        return (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")

    def _bind(self) -> str:
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    @staticmethod
    def _rule(text: str, selector: str) -> str:
        """取出 `selector { ... }` 的声明体（selector 必须整段匹配，避免前缀命中）。"""
        start = text.index(f"{selector} {{")
        end = text.index("}", start)
        return text[start:end]

    # ---- 样式层 ----

    def test_root_keeps_historical_defaults(self):
        root = self._rule(self._css(), ":root")
        self.assertIn("--msg-font-size:15px", root.replace(" ", ""))
        self.assertIn("--msg-font-family:var(--font-sans)", root.replace(" ", ""))

    def test_message_body_consumes_the_variables(self):
        body = self._rule(self._css(), ".message-body")
        self.assertIn("font-size:var(--msg-font-size)", body.replace(" ", ""))
        self.assertIn("font-family:var(--msg-font-family)", body.replace(" ", ""))
        # 行高不被字体设置牵连（它有自己的设置项位置，现在仍是常量）。
        self.assertIn("line-height:1.7", body.replace(" ", ""))
        # 回归防线：绝不能再出现写死的正文字号。
        self.assertNotIn("font-size:15px", body.replace(" ", ""))

    def test_font_variables_are_not_folded_into_global_tokens(self):
        """--font-sans / body 字号不得被会话字体设置改写（会牵连侧栏、按钮、设置弹窗）。"""
        core = self._core()
        self.assertNotIn("--font-sans'", core)
        self.assertNotIn("root.style.fontSize", core)
        self.assertNotIn("style.setProperty('--font-sans'", core)

    # ---- 应用链路（01-core.js） ----

    def test_apply_appearance_writes_both_variables(self):
        core = self._core()
        self.assertIn("root.style.setProperty('--msg-font-size'", core)
        self.assertIn("root.style.setProperty('--msg-font-family'", core)
        self.assertIn("resolveMsgFontFamily", core)

    def test_font_family_map_covers_backend_enum(self):
        """前端栈表必须覆盖后端枚举的**每一个键**（漏一个 = 用户选了却回落到界面字体）。"""
        core = self._core()
        for key in sorted(APPEARANCE_CHAT_FONT_FAMILIES - {"custom"}):
            with self.subTest(key=key):
                self.assertIn(f"{key}:", core)
        self.assertIn("}, var(--font-sans)`", core)   # custom 的用户串必须带界面字体兜底

    def test_pick_list_covers_every_font_key(self):
        """点选列表的键集 = 后端枚举去掉结构三档；标签/探测名不进 HTML 也不进后端。"""
        core = self._core()
        block = core[core.index("export const CHAT_FONT_PICKS"):core.index("// ---- 字体安装探测")]
        pick_keys = set(re.findall(r"key: '([a-z_]+)'", block))
        self.assertEqual(pick_keys, set(APPEARANCE_CHAT_FONT_FAMILIES) - {"system", "serif", "rounded"})
        # 每一条都要有中文标签与探测名（custom 是手敲兜底，没有探测名）。
        for item in re.findall(r"\{ key: '[a-z_]+', label: '([^']+)', probe: '([^']*)' \}", block):
            with self.subTest(label=item[0]):
                self.assertTrue(item[0].strip())
        self.assertIn("{ key: 'custom', label: '手动填写…', probe: '' }", block)
        # 探测名必须真的是该栈的首选字体（否则"未安装"标注会张冠李戴）。
        for probe in ("Microsoft YaHei UI", "SimSun", "LXGW WenKai", "HarmonyOS Sans SC"):
            with self.subTest(probe=probe):
                self.assertIn(probe, core)

    def test_install_probe_is_measurement_based_and_cached(self):
        """安装探测只能靠 canvas 度量（queryLocalFonts 在手机上不可用），且必须缓存。"""
        core = self._core()
        probe = core[core.index("export function isFontInstalled"):core.index("const MSG_FONT_SIZE_MIN")]
        self.assertIn("measureText", probe)
        self.assertIn("fontInstalledCache.set(name, installed)", probe)
        self.assertIn("fontInstalledCache.has(name)", probe)
        # 注释里会解释「为什么不用 queryLocalFonts」——那只算文档，不算调用。
        # 判据要落在**可执行代码**上：先剔除整行注释，再断言没有任何引用。
        code_only = "\n".join(
            line for line in core.splitlines() if not line.lstrip().startswith("//")
        )
        self.assertNotIn("queryLocalFonts", code_only)

    def test_partial_update_keeps_previous_font(self):
        """只传 theme/skin 的调用点（面板、恢复默认、旧版服务端）不得重置字号字体。"""
        core = self._core()
        self.assertIn("numericOrNull(previous.chat_font_size)", core)
        self.assertIn("MSG_FONT_FAMILY_KEYS.has(previous.chat_font_family)", core)
        self.assertIn("previous.chat_font_family_custom", core)

    def test_bootstrap_sync_passes_font_keys(self):
        core = self._core()
        for key in ("chat_font_size", "chat_font_family", "chat_font_family_custom"):
            with self.subTest(key=key):
                self.assertIn(f"configured?.{key} ?? local.{key}", core)

    def test_local_cache_stores_font_keys(self):
        """首屏无闪烁靠它：localStorage 缓存里没有字号就会先画 15px 再跳变。"""
        core = self._core()
        read = core[core.index("function readStoredAppearance"):core.index("function resolvedTheme")]
        for key in ("chat_font_size", "chat_font_family", "chat_font_family_custom"):
            with self.subTest(key=key):
                self.assertIn(key, read)

    # ---- 面板与绑定 ----

    def test_index_exposes_the_controls(self):
        index = self._index()
        self.assertIn('id="chatFontSize"', index)
        self.assertIn('min="13" max="18"', index)
        self.assertIn('id="chatFontSizeValue"', index)
        self.assertIn('id="chatFontFamilyCustom"', index)
        self.assertIn('id="chatFontCustomRow"', index)
        # 第 4 项是「指定字体」占位值（点选列表），落库的永远是 select 里的真实键。
        for value in ("system", "serif", "rounded", "__pick__"):
            with self.subTest(value=value):
                self.assertIn(f'name="appearanceChatFont" value="{value}"', index)
        self.assertIn('id="chatFontPick"', index)
        self.assertIn('id="chatFontManualRow"', index)
        # 点选列表与「手动填写」是两层显隐：select 在 #chatFontCustomRow 里、文本框在手动行里。
        self.assertIn('<select id="chatFontPick"></select>', index)
        self.assertIn("指定字体", index)
        # 设置内搜索要能按「字体/字号」命中外观页。
        self.assertIn("字体 字号", index)

    def test_pick_options_are_built_from_the_constant(self):
        """选项必须由 CHAT_FONT_PICKS 现建（含未安装标注），且每次重建后保住已选值。"""
        settings = self._settings()
        build = settings[settings.index("export function buildChatFontPickOptions"):]
        build = build[:build.index("\nexport function", 1)]
        for needle in ("CHAT_FONT_PICKS", "#chatFontPick", "isFontInstalled(item.probe)",
                       "（未安装）", "const previous = pick.value", "pick.value = previous"):
            with self.subTest(needle=needle):
                self.assertIn(needle, build)
        # 建选项要早于回显：select 里还没有 option 时写 value 会被清空。
        populate = settings[settings.index("export function populateAppearanceSettings"):]
        populate = populate[:populate.index("\n}")]
        self.assertIn("buildChatFontPickOptions();", populate)
        self.assertLess(populate.index("buildChatFontPickOptions();"), populate.index("syncAppearanceControls();"))

    def test_settings_panel_syncs_every_control(self):
        settings = self._settings()
        sync = settings[settings.index("export function syncAppearanceControls"):]
        sync = sync[:sync.index("\nexport function", 1)]
        for needle in ("appearanceChatFont", "#chatFontSize", "#chatFontSizeValue",
                       "#chatFontFamilyCustom", "#chatFontCustomRow",
                       "#chatFontPick", "#chatFontManualRow"):
            with self.subTest(needle=needle):
                self.assertIn(needle, sync)
        # 两层显隐别揉在一起：radio 决定 select 行，select 的值决定手动行。
        self.assertIn("CHAT_FONT_BASE_FAMILIES.has(family)", sync)
        self.assertIn("manualRow.hidden = !(usingPick && family === 'custom')", sync)

    def test_pick_placeholder_never_reaches_the_backend(self):
        """最大事故面：__pick__ 落库会被后端白名单拒（表现为"保存失败"）。"""
        settings = self._settings()
        form = settings[settings.index("export function appearanceFormValues"):]
        form = form[:form.index("\n}")]
        self.assertIn("family.value === CHAT_FONT_PICK_VALUE", form)
        self.assertIn("pick ? pick.value : undefined", form)
        self.assertIn("const CHAT_FONT_PICK_VALUE = '__pick__';", settings)
        # 原始占位值只能是 DOM 值，绝不能被当字体键塞进请求体。
        self.assertNotIn("chat_font_family: family.value", form)
        # 只有手动填写时才带上用户串，否则不动它（用户打过的串要留住）。
        self.assertIn("fontFamily === 'custom' && customInput", form)

    def test_slider_input_previews_without_persisting(self):
        """拖动滑块/点选字体只做本地预览；落库统一由「保存外观」完成（别每像素打 API）。"""
        bind = self._bind()
        self.assertIn("$('#chatFontSize')?.addEventListener('input'", bind)
        self.assertIn("$('#chatFontPick')?.addEventListener('change'", bind)
        preview = bind[bind.index("const previewAppearance"):bind.index("$$('input[name=\"appearanceTheme\"")]
        self.assertNotIn("saveAppearance", preview)
        self.assertIn("applyAppearance(appearanceFormValues())", preview)

    def test_reset_covers_font_keys(self):
        bind = self._bind()
        reset = bind[bind.index("appearanceReset?.addEventListener"):]
        reset = reset[:reset.index("catch (error)")]
        for needle in ("chat_font_size: 15", "chat_font_family: 'system'", "chat_font_family_custom: ''"):
            with self.subTest(needle=needle):
                self.assertIn(needle, reset)

    def test_form_values_do_not_invent_defaults_for_missing_nodes(self):
        """旧 index.html 缺控件时必须返回 undefined（沿用当前值），不能回落成默认值。"""
        settings = self._settings()
        form = settings[settings.index("export function appearanceFormValues"):]
        form = form[:form.index("\n}")]
        self.assertIn("chat_font_size: slider ? Number(slider.value) : undefined", form)
        self.assertIn("family.value === CHAT_FONT_PICK_VALUE", form)


if __name__ == "__main__":
    unittest.main()
