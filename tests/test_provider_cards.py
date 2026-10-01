# -*- coding: utf-8 -*-
"""API 供应商设置页（卡片列表 + 点开才弹出的设置弹层）的接线守门。

背景：原先「当前供应商」下拉 + 添加/删除按钮 + 常驻表单三件套挤在同一页，
一次只能看一个供应商、新增必须先点「添加」再填表。现在改为**卡片网格**：
一行最多三张卡、最后一张固定是「添加 API」卡片、每张卡右上角 × 删除，
点卡片才弹出该供应商的设置弹层（**字段格式与文案保持原样**，只换显示容器）。

关键不变量：
1. 卡片容器 `#providerCards`；卡片带 `data-provider-card`，删除按钮 `data-provider-delete`，
   末尾添加卡 `data-provider-add` 且**必须排在所有供应商卡片之后**；
2. 顶部「在线 API / 本地 API」分类 tab（`data-provider-kind`）与过滤语义不变；
3. 设置项整体搬进顶层 `<dialog id="providerDialog">`，字段 id / 标签 / 控件逐字不变；
4. 旧控件（`#providerSelect`/`#addProvider`/`#deleteProvider`/`#editProvider`/`#providerEmpty`）
   必须连绑定一起消失，不留死引用；
5. 网格一行最多三张（`repeat(3, minmax(0, 1fr))`），窄屏降级 2 列 / 1 列。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LEGACY_SELECTORS = (
    "#providerSelect",
    "#addProvider",
    "#deleteProvider",
    "#editProvider",
    "#providerEmpty",
)

FORM_FIELD_IDS = (
    "providerName",
    "providerBaseUrl",
    "providerApiKey",
    "providerFormat",
    "providerModel",
    "providerModelCustom",
    "providerContextWindow",
    "providerMaxOutputTokens",
    "providerTemperature",
    "providerReasoningEffort",
    "providerSupportsImages",
    "providerError",
    "testProvider",
    "unloadProviderModel",
    "cancelProvider",
    "saveProvider",
)

FORM_LABELS = (
    "供应商名称",
    "API URL",
    "API Key",
    "请求格式",
    "模型名称",
    "上下文窗口 Tokens（可选）",
    "最大输出 Tokens（可选）",
    "温度（可选）",
    "思维强度",
    "视觉输入能力",
)
# 定价已迁出本表单（2026-10-01）：单价改在「用量统计 → 右上角 费用单价」里按
# 「供应商 → 模型」管理，写库走 POST /api/providers/pricing。表单里既没有字段，
# 提交体也不带 pricing 键（不带键时后端不动旧值，见 config.upsert_model_profile）。
REMOVED_PRICING_IDS = (
    "providerPricingField",
    "providerPriceInput",
    "providerPriceCachedInput",
    "providerPriceOutput",
    "providerPriceCurrency",
)


class ProviderCardsMarkupTests(unittest.TestCase):
    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _settings(self) -> str:
        return (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")

    def _bind(self) -> str:
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_models_panel_only_renders_cards(self) -> None:
        """API 供应商面板里只剩 tab + 卡片容器，表单不再常驻。"""
        index = self._index()
        start = index.index('data-settings-panel="models"')
        end = index.index('data-settings-panel="agent"')
        panel = index[start:end]
        self.assertIn('id="providerCards"', panel)
        self.assertNotIn('id="providerForm"', panel, "设置表单不应再常驻在设置页里")
        self.assertNotIn("provider-current", panel)

    def test_scope_tabs_unchanged(self) -> None:
        index = self._index()
        self.assertIn('class="provider-scope-tabs"', index)
        self.assertIn('data-provider-kind="online"', index)
        self.assertIn('data-provider-kind="local"', index)
        css = self._css()
        self.assertIn(".provider-scope-tabs {", css)

    def test_form_moved_into_dialog_with_same_fields(self) -> None:
        """字段格式不变：整体搬进 providerDialog，id 与标签逐字保留。"""
        index = self._index()
        self.assertIn('<dialog id="providerDialog" class="provider-dialog">', index)
        self.assertLess(
            index.index('id="providerDialog"'),
            index.index('id="providerForm"'),
            "表单必须位于供应商设置弹层内部",
        )
        for field_id in FORM_FIELD_IDS:
            with self.subTest(field=field_id):
                self.assertIn(f'id="{field_id}"', index)
        for label in FORM_LABELS:
            with self.subTest(label=label):
                self.assertIn(label, index)

    def test_actions_pinned_to_dialog_footer(self) -> None:
        """错误提示与按钮行固定在弹层底部，字段区独立滚动（不用滚到底才能保存）。"""
        index = self._index()
        self.assertIn('class="provider-form-footer"', index)
        self.assertLess(index.index('class="provider-form-body"'), index.index('class="provider-form-footer"'))
        self.assertLess(index.index('class="provider-form-footer"'), index.index('id="saveProvider"'))
        css = self._css()
        footer = css[css.index(".provider-form-footer {"):]
        footer = footer[: footer.index("}")]
        self.assertIn("border-top", footer)

    def test_legacy_controls_removed_everywhere(self) -> None:
        """旧下拉/按钮必须连引用一起删除（死引用会静默失效或抛错）。"""
        sources = {
            "public/index.html": self._index(),
            "public/js/09-settings.js": self._settings(),
            "public/js/15-bind-events.js": self._bind(),
        }
        for name, source in sources.items():
            for selector in LEGACY_SELECTORS:
                with self.subTest(file=name, selector=selector):
                    self.assertNotIn(selector, source)

    def test_card_markup_and_add_card_last(self) -> None:
        source = self._settings()
        for snippet in (
            'class="provider-card',
            'data-provider-card="${id}"',
            'data-provider-delete="${id}"',
            "data-provider-add",
            "provider-card-name",
            "provider-card-tag",
            "provider-card-badge",
            "is-default",
            "PROVIDER_FORMAT_LABELS",
            "添加 API",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, source)
        # 「添加 API」卡片拼接在所有供应商卡片之后。
        self.assertLess(
            source.index("providers.map(providerCardMarkup).join('')"),
            source.index("provider-card-add"),
            "「添加 API」卡片必须排在最后一张",
        )
        # 卡片压成两行（名称 + 脚行），模型名不再上卡片，对应的死样式也要删掉。
        self.assertNotIn("provider-card-model", source)
        self.assertNotIn(".provider-card-model", self._css())
        card = source[source.index("function providerCardMarkup("):]
        card = card[: card.index("\n}")]
        self.assertLess(card.index("provider-card-name"), card.index("provider-card-foot"))

    def test_render_does_not_auto_open_form(self) -> None:
        """列表渲染不再顺手打开表单（改为点卡片才开）。"""
        source = self._settings()
        body = source[source.index("export function renderProviders()"):]
        body = body[: body.index("\n}")]
        self.assertNotIn("showProviderForm(", body)

    def test_card_click_opens_dialog_with_that_provider(self) -> None:
        source = self._settings()
        self.assertIn("export function openProviderCard(", source)
        self.assertIn("export function showProviderForm(", source)
        body = source[source.index("export function openProviderCard("):]
        body = body[: body.index("\n}")]
        self.assertIn("providerProfiles().find", body)
        self.assertIn("showProviderForm(provider)", body)

    def test_delete_confirms_before_removing(self) -> None:
        source = self._settings()
        body = source[source.index("export async function deleteProvider("):]
        body = body[: body.index("\n}")]
        self.assertIn("confirm(", body)
        self.assertIn("/api/providers/", body)
        self.assertIn("{ method: 'DELETE' }", body)

    def test_bindings_delegate_from_card_container(self) -> None:
        bind = self._bind()
        self.assertIn("$('#providerCards').addEventListener('click'", bind)
        self.assertIn("$('#providerCards').addEventListener('keydown'", bind)
        self.assertIn("data-provider-delete", bind)
        self.assertIn("data-provider-add", bind)
        self.assertIn("data-provider-card", bind)
        self.assertIn("openProviderCard(", bind)
        self.assertIn("deleteProvider(", bind)
        # Esc / 右上角关闭 / 取消 / 保存成功统一走 close 事件复位。
        self.assertIn("$('#providerDialog').addEventListener('close'", bind)
        self.assertIn("$('#providerForm').addEventListener('submit'", bind)

    def test_css_three_columns_and_responsive(self) -> None:
        css = self._css()
        cards = css[css.index(".provider-cards {"):]
        cards = cards[: cards.index("}")]
        self.assertIn("repeat(3, minmax(0, 1fr))", cards, "一行最多三张卡片")
        self.assertIn("repeat(2, minmax(0, 1fr))", css, "窄屏应降级为两列")
        card = css[css.index(".provider-card {"):]
        card = card[: card.index("}")]
        self.assertIn("min-height: 88px", card, "卡片压成两行后应更矮（原 116px）")
        delete_rule = css[css.index(".provider-card-delete {"):]
        delete_rule = delete_rule[: delete_rule.index("}")]
        self.assertIn("position: absolute", delete_rule, "× 必须固定在卡片右上角")
        self.assertIn("top: 8px", delete_rule)
        self.assertIn("right: 8px", delete_rule)
        dialog = css[css.index(".provider-dialog {"):]
        dialog = dialog[: dialog.index("}")]
        self.assertIn("max-height", dialog, "弹层内部需可滚动，不能溢出视口")

    def test_pricing_fields_moved_out_of_provider_form(self) -> None:
        """单价字段与「改价即整表单提交」的老路径必须彻底消失（含死样式与表单键）。"""
        sources = {
            "public/index.html": self._index(),
            "public/js/09-settings.js": self._settings(),
            "public/js/15-bind-events.js": self._bind(),
            "public/styles.css": self._css(),
        }
        for name, source in sources.items():
            for corpse in REMOVED_PRICING_IDS + ("provider-pricing-grid",):
                with self.subTest(file=name, corpse=corpse):
                    self.assertNotIn(corpse, source)
        # 表单值里也不许再带 pricing 键：带上就等于「拿连接配置表单写价格」，
        # 一旦 model 或 api_key 为空就会把定价连人带价一起覆盖。
        # 先剥掉行注释再查：注释里点名 /api/providers/pricing 是正当的，真正的风险
        # 只在返回对象真的带上 pricing 键；剥注释后判定才既精确又不会误伤文案。
        body = self._settings()
        body = body[body.index("export function providerFormValue()"):]
        body = body[: body.index("\n}")]
        code = "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())
        self.assertNotIn("pricing", code)

    def test_index_html_still_has_no_duplicate_ids(self) -> None:
        """顺手守住重复 id（教训 §九.45）。"""
        ids = re.findall(r'\sid="([^"]+)"', self._index())
        duplicates = {value for value in ids if ids.count(value) > 1}
        self.assertFalse(duplicates, f"index.html 存在重复 id：{sorted(duplicates)}")


if __name__ == "__main__":
    unittest.main()
