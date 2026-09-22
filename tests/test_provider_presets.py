# -*- coding: utf-8 -*-
"""供应商预设（「只填 API Key」接入模板）的表完整性 + 回填语义 + 前端接线守门。

背景：设置 → 模型 →「添加 API」原先是一张空表单，API URL / 请求格式 / 模型名全要自己填，
新用户第一步就卡住。现在后端有一张 15 条的预设表（大厂 8 + 中转 2 + 自定义 + 本地 4），
前端两个弹层（设置弹层与首启引导向导）共用同一份名单：选中卡片即回填连接字段，
标准用户只剩 API Key 一个空。

关键不变量：
1. 名单只有一个来源（naiba/llm/provider_presets.py），前端经 /api/provider-presets 取；
2. 每条预设字段齐全且自洽：kind 与 request_format 同词表、在线必须有 key_url、hint 三段式
   （①这是什么站 → ②怎么注册/充值 → ③Key 在哪创建），且 hint 里出现的远端域名与 key_url 一致；
3. 回填语义：预设值只是默认值，用户显式提交的非空值优先；未知 preset_id 必须报错；
4. 「打开注册页」只吃 preset_id、地址来自白名单（不接受任意 URL）；
5. 前端两处（设置弹层 / 首启向导）都接了网格与引导条，且没有留下下拉版死控件。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.config import ConfigStore, ONLINE_REQUEST_FORMATS, VALID_LOCAL_BACKENDS  # noqa: E402
from naiba.llm.provider_presets import (  # noqa: E402
    PROVIDER_PRESETS, apply_preset_values, provider_preset, provider_preset_key_url,
    provider_presets_payload,
)

REQUIRED_FIELDS = ("id", "kind", "name", "abbr", "base_url", "request_format", "model", "key_required", "key_url", "hint")
# 名单顺序即界面顺序：大厂 → 中转 → 自定义 → 本地（在线在前）。
EXPECTED_ONLINE_IDS = ("deepseek", "kimi", "zhipu", "qwen", "ark", "openai", "claude", "gemini", "te", "bailan", "custom")
EXPECTED_LOCAL_IDS = ("ollama", "lm_studio", "llama_cpp", "unsloth")


class PresetTableTests(unittest.TestCase):
    def test_fifteen_presets_in_declared_order(self):
        ids = [item["id"] for item in PROVIDER_PRESETS]
        self.assertEqual(len(ids), 15, f"预设条数应为 15：{ids}")
        self.assertEqual(len(set(ids)), len(ids), "预设 id 必须唯一")
        self.assertEqual(ids, list(EXPECTED_ONLINE_IDS) + list(EXPECTED_LOCAL_IDS))

    def test_every_preset_has_all_fields(self):
        for item in PROVIDER_PRESETS:
            with self.subTest(preset=item.get("id")):
                for field in REQUIRED_FIELDS:
                    self.assertIn(field, item, f"{item.get('id')} 缺字段 {field}")
                self.assertIsInstance(item["key_required"], bool)
                self.assertTrue(str(item["name"]).strip())
                self.assertTrue(str(item["hint"]).strip())
                # 图标字符块：1~3 个字符（首字母色块，不引品牌图片）。
                self.assertTrue(1 <= len(str(item["abbr"])) <= 3, f"{item['id']} 的 abbr 长度异常")

    def test_kind_and_format_use_the_shared_vocabulary(self):
        for item in PROVIDER_PRESETS:
            with self.subTest(preset=item["id"]):
                if item["kind"] == "local":
                    self.assertIn(item["request_format"], VALID_LOCAL_BACKENDS)
                    self.assertFalse(item["key_required"], "本地后端免 Key")
                else:
                    self.assertEqual(item["kind"], "online")
                    self.assertIn(item["request_format"], ONLINE_REQUEST_FORMATS)
                # 请求格式必须能在设置弹层的下拉里选到（前端按同一套格式名渲染）。
                self.assertIn(f'value="{item["request_format"]}"', (ROOT / "public/index.html").read_text(encoding="utf-8"))

    def test_online_presets_expose_a_key_page_and_three_step_hint(self):
        for item in PROVIDER_PRESETS:
            if not item["key_url"]:
                # 只有「自定义」与本地后端可以没有注册页。
                with self.subTest(preset=item["id"]):
                    self.assertTrue(item["kind"] == "local" or item["id"] == "custom")
                continue
            with self.subTest(preset=item["id"]):
                parsed = urlparse(item["key_url"])
                self.assertEqual(parsed.scheme, "https")
                self.assertTrue(parsed.netloc)
                # 三段式：这是什么站 → 怎么注册/充值 → Key 在哪创建。
                for marker in ("①", "②", "③"):
                    self.assertIn(marker, item["hint"], f"{item['id']} 的 hint 缺 {marker}")
                # hint 里出现的域名必须就是按钮要打开的站点（不让用户手抄网址）。
                self.assertIn(parsed.netloc, item["hint"], f"{item['id']} 的 hint 里没写域名 {parsed.netloc}")

    def test_transit_presets_match_the_measured_cards(self):
        """两条中转的参数照抄冻结版实测可用卡片（TE 默认 openai_chat，按用户决定）。"""
        te = provider_preset("te")
        self.assertEqual(te["base_url"], "https://teynex.com")
        self.assertEqual(te["request_format"], "openai_chat")
        self.assertEqual(te["model"], "kimi-k3")
        self.assertIn("max_tokens", te["hint"], "TE 按 max_tokens 预扣费的风险要写进引导")
        self.assertIn("codex_responses", te["hint"], "GPT-5/Codex 系模型的切法要写进引导")
        bailan = provider_preset("bailan")
        self.assertEqual(bailan["base_url"], "https://api.bailan.store")
        self.assertEqual(bailan["request_format"], "openai_chat")
        self.assertEqual(bailan["model"], "grok-4.6")

    def test_ark_preset_prefills_coding_plan_endpoint(self):
        """方舟预设预填 Coding Plan 端点（订阅制）；按量端点与计费区分必须写进引导。"""
        ark = provider_preset("ark")
        self.assertEqual(ark["base_url"], "https://ark.cn-beijing.volces.com/api/coding/v3")
        self.assertEqual(ark["request_format"], "openai_chat")
        self.assertEqual(ark["model"], "doubao-seed-code")
        # 官方警告：Coding Plan 端点与按量端点用错会按量扣费——引导里必须能对出两个 URL。
        self.assertIn("ark.cn-beijing.volces.com/api/v3", ark["hint"], "按量端点切换要写进引导")
        self.assertIn("按量扣费", ark["hint"], "计费区分警告要写进引导")

    def test_custom_preset_leaves_the_address_blank(self):
        custom = provider_preset("custom")
        self.assertEqual(custom["base_url"], "")
        self.assertEqual(custom["model"], "")
        self.assertTrue(custom["key_required"])

    def test_payload_is_a_read_only_copy_online_first(self):
        payload = provider_presets_payload()
        self.assertEqual([item["id"] for item in payload], [item["id"] for item in PROVIDER_PRESETS])
        payload[0]["name"] = "改坏了"
        self.assertNotEqual(PROVIDER_PRESETS[0]["name"], "改坏了", "导出的副本不能反向改到表里")

    def test_key_url_lookup_is_whitelisted(self):
        self.assertEqual(provider_preset_key_url("te"), "https://teynex.com/")
        for preset_id in ("custom", "ollama", "", "nope", "https://evil.example.com"):
            with self.subTest(preset_id=preset_id):
                self.assertEqual(provider_preset_key_url(preset_id), "")


class ApplyPresetTests(unittest.TestCase):
    def test_without_preset_id_is_passthrough(self):
        raw = {"name": "手填", "base_url": "https://x.example", "request_format": "openai_chat"}
        self.assertEqual(apply_preset_values(raw), raw)
        self.assertNotIn("preset_id", apply_preset_values(raw))

    def test_fills_only_empty_fields(self):
        merged = apply_preset_values({
            "preset_id": "te",
            "name": "",
            "base_url": "",
            "request_format": "",
            "model": "",
            "api_key": "sk-test",
        })
        self.assertEqual(merged["name"], "TE 中转")
        self.assertEqual(merged["base_url"], "https://teynex.com")
        self.assertEqual(merged["request_format"], "openai_chat")
        self.assertEqual(merged["model"], "kimi-k3")
        self.assertEqual(merged["api_key"], "sk-test")
        self.assertEqual(merged["preset_id"], "te")

    def test_user_values_win_over_preset(self):
        """标准用户直接用预设；中转玩家把 URL/格式改成 codex 组合时不能被预设覆盖。"""
        merged = apply_preset_values({
            "preset_id": "te",
            "base_url": "https://teynex.com/v1",
            "request_format": "codex_responses",
            "model": "gpt-5.6-terra",
            "api_key": "sk-test",
        })
        self.assertEqual(merged["base_url"], "https://teynex.com/v1")
        self.assertEqual(merged["request_format"], "codex_responses")
        self.assertEqual(merged["model"], "gpt-5.6-terra")

    def test_local_preset_sets_local_backend(self):
        merged = apply_preset_values({"preset_id": "ollama", "api_key": ""})
        self.assertEqual(merged["kind"], "local")
        self.assertEqual(merged["request_format"], "ollama")
        self.assertEqual(merged["local_backend"], "ollama")
        self.assertEqual(merged["base_url"], "http://127.0.0.1:11434/v1")

    def test_online_preset_never_sets_local_backend(self):
        merged = apply_preset_values({"preset_id": "deepseek"})
        self.assertNotIn("local_backend", merged)

    def test_custom_preset_does_not_fill_address(self):
        merged = apply_preset_values({"preset_id": "custom"})
        self.assertNotIn("base_url", merged)
        self.assertNotIn("model", merged)
        self.assertEqual(merged["name"], "自定义（OpenAI 兼容或其他）")

    def test_unknown_preset_raises(self):
        with self.assertRaises(ValueError):
            apply_preset_values({"preset_id": "no-such-vendor"})


class PresetRoundTripTests(unittest.TestCase):
    """预设 → 回填 → 落库：卡片要记住来源，且不覆盖用户手改过的值。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_with_preset_lands_full_card(self):
        store = ConfigStore(self.path)
        saved = store.upsert_provider(apply_preset_values({
            "preset_id": "te", "name": "", "base_url": "", "request_format": "", "model": "",
            "api_key": "sk-test",
        }))
        self.assertEqual(saved["name"], "TE 中转")
        self.assertEqual(saved["kind"], "online")
        self.assertEqual(saved["base_url"], "https://teynex.com")
        self.assertEqual(saved["request_format"], "openai_chat")
        self.assertEqual(saved["model"], "kimi-k3")
        self.assertTrue(saved["has_api_key"])
        reloaded = ConfigStore(self.path).data["providers"][0]
        self.assertEqual(reloaded["preset_id"], "te", "卡片要记住来源模板，编辑时才能反显")

    def test_editing_off_preset_clears_the_source(self):
        store = ConfigStore(self.path)
        saved = store.upsert_provider(apply_preset_values({"preset_id": "zhipu", "api_key": "sk-test"}))
        store.upsert_provider({
            "id": saved["id"], "name": "我的中转", "base_url": "https://my.example/v1",
            "request_format": "openai_chat", "model": "my-model", "api_key": "",
        })
        entry = ConfigStore(self.path).data["providers"][0]
        self.assertNotIn("preset_id", entry, "改成手填配置后不该再挂着模板来源")
        self.assertEqual(entry["base_url"], "https://my.example/v1")
        self.assertEqual(entry["api_key"], "sk-test", "空 Key 表示沿用，不能被清掉")


class BackendWiringTests(unittest.TestCase):
    def _source(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_presets_endpoint_and_open_route(self):
        http = self._source("naiba/http.py")
        self.assertIn("self.app.api_provider_presets()", http)
        self.assertIn('"/api/provider-presets"', http)
        # 「打开注册页」只接受 preset_id（地址由服务端白名单取），不吃前端传的 URL。
        self.assertIn('"/api/provider-presets/open"', http)
        self.assertIn("api_open_provider_key_url(str(body.get(\"preset_id\") or \"\"))", http)

    def test_app_methods_use_preset_table(self):
        app = self._source("naiba/app.py")
        self.assertIn("provider_presets_payload", app)
        self.assertIn("apply_preset_values(body)", app)
        self.assertIn("provider_preset_key_url(preset_id)", app)
        self.assertIn("webbrowser.open(url)", app)

    def test_open_endpoint_rejects_presets_without_key_page(self):
        """无注册页的预设（自定义/本地）必须被挡在 400，而不是打开一个空地址。"""
        from naiba.app import NaibaChatApp  # noqa: F401  (只确认方法可被组装根加载)
        app_source = self._source("naiba/app.py")
        body = app_source[app_source.index("def api_open_provider_key_url"):]
        body = body[: body.index("\n    def ")]
        self.assertIn("if not url:", body)
        self.assertIn("HTTPStatus.BAD_REQUEST", body)


class ProviderUpsertIdempotencyTests(unittest.TestCase):
    """§九.127：连点「保存设置」→ 后端「无 id 的新建」必须幂等，不能变成 N 张重复卡。

    事故链：慢上游探测期间按钮不禁用（前端，见 FrontendWiringTests）→ 连点 5 次发出
    5 个**无 id** 的 POST → 后端此前一律 `uuid4()` 新建 ⇒ 列表出现 5 张同样的卡片。
    前端防重入是主修，这里是第二层：即使请求真的并发到达，也只能落一条。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _values(self, **overrides):
        values = {
            "kind": "online",
            "name": "SillyDream",
            "base_url": "https://api.example/v1",
            "model": "gpt-4o-mini",
            "api_key": "sk-test",
            "request_format": "openai_chat",
        }
        values.update(overrides)
        return values

    def test_repeated_anonymous_save_reuses_the_same_entry(self):
        store = ConfigStore(self.path)
        first = store.upsert_model_profile(self._values())
        second = store.upsert_model_profile(self._values())
        self.assertEqual(second["id"], first["id"], "同一条配置连发两次必须落在同一个 id 上")
        self.assertEqual(second["model_key"], first["model_key"])
        self.assertEqual(len(ConfigStore(self.path).data["providers"]), 1, "列表里只能有一张卡")

    def test_concurrent_anonymous_saves_land_on_one_entry(self):
        store = ConfigStore(self.path)
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(store.upsert_model_profile(self._values())))
            for _ in range(5)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual({item["id"] for item in results}, {results[0]["id"]}, "并发新建必须收敛到同一个 id")
        self.assertEqual(len(ConfigStore(self.path).data["providers"]), 1)

    def test_different_api_key_still_creates_a_second_entry(self):
        store = ConfigStore(self.path)
        first = store.upsert_model_profile(self._values())
        second = store.upsert_model_profile(self._values(api_key="sk-other"))
        self.assertNotEqual(second["id"], first["id"], "多账号（不同 Key）是合理需求，必须允许并存")
        self.assertEqual(len(store.data["providers"]), 2)

    def test_different_model_or_url_is_not_merged(self):
        store = ConfigStore(self.path)
        first = store.upsert_model_profile(self._values())
        self.assertNotEqual(store.upsert_model_profile(self._values(model="gpt-4o"))["id"], first["id"])
        self.assertNotEqual(
            store.upsert_model_profile(self._values(base_url="https://other.example/v1"))["id"], first["id"]
        )
        self.assertEqual(len(store.data["providers"]), 3)

    def test_explicit_id_still_targets_that_entry(self):
        """带 id 的请求语义是「改这一条」，不能被兜底改成新建。"""
        store = ConfigStore(self.path)
        first = store.upsert_model_profile(self._values())
        updated = store.upsert_model_profile({**self._values(), "id": first["id"], "name": "改名了"})
        self.assertEqual(updated["id"], first["id"])
        self.assertEqual(updated["name"], "改名了")
        self.assertEqual(len(store.data["providers"]), 1)

    def test_empty_key_does_not_hitch_a_ride_on_a_keyed_entry(self):
        """空 Key 的新建不能"蹭"上已有条目：那会把两次不同保存伪装成同一条。"""
        store = ConfigStore(self.path)
        keyed = store.upsert_model_profile(self._values(api_key="sk-keep"))
        anonymous = store.upsert_model_profile(self._values(api_key=""))
        self.assertNotEqual(anonymous["id"], keyed["id"])
        entry = next(item for item in store.data["providers"] if item["id"] == keyed["id"])
        self.assertEqual(entry["api_key"], "sk-keep", "既有条目不能被空 Key 的新建改写")


class FrontendWiringTests(unittest.TestCase):
    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _settings(self) -> str:
        return (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")

    def _bind(self) -> str:
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def _onboarding(self) -> str:
        return (ROOT / "public/js/19-onboarding.js").read_text(encoding="utf-8")

    def _bootstrap(self) -> str:
        return (ROOT / "public/js/05-bootstrap.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_settings_dialog_uses_card_grid_not_a_select(self):
        index = self._index()
        self.assertIn('id="providerPresetGrid"', index)
        self.assertIn('id="providerPresetGuide"', index)
        self.assertIn('id="providerPresetKeyUrl"', index)
        # 预埋的下拉版死控件必须换掉（留着就是一个永远空着的选择框）。
        self.assertNotIn('id="providerPreset"', index)
        self.assertNotIn('id="providerPresetHint"', index)
        self.assertNotIn("select id=\"providerPreset\"", index)

    def test_onboarding_dialog_has_steps_grid_and_key_row(self):
        index = self._index()
        header = index[index.index('id="onboardingDialog"'):]
        header = header[: header.index("</dialog>")]
        for element_id in (
            "onboardingStepPreset", "onboardingStepKey", "onboardingPresetGrid", "onboardingPresetMore",
            "onboardingRecap", "onboardingGuide", "onboardingGuideText", "onboardingKeyUrl",
            "onboardingUrlField", "onboardingBaseUrl", "onboardingApiKey", "onboardingModel",
            "onboardingBack", "onboardingSkip", "onboardingTest", "onboardingSave", "onboardingError",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', header)
        # 模板网格在 Key 行之前：先选供应商，再粘 Key。
        self.assertLess(header.index('id="onboardingPresetGrid"'), header.index('id="onboardingKeyRow"'))

    def test_settings_helpers_are_shared_with_the_wizard(self):
        settings = self._settings()
        for symbol in (
            "export function providerPresets()",
            "export async function loadProviderPresets()",
            "export function providerPresetCardMarkup(",
            "export function renderProviderPresetGrid(",
            "export function markProviderPresetCards(",
            "export function fillProviderPresetGuide(",
            "export function setProviderPresetSelection(",
            "export function applyProviderPreset(",
            "export async function openProviderPresetKeyUrl(",
            "export function providerPresetMoreText(",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, settings)
        # 表单保存时带上来源模板，供服务端回填与卡片反显。
        self.assertIn("preset_id: state.providerPresetId", settings)
        # 打开设置弹层即渲染网格；编辑老卡按卡里记的来源反显。
        self.assertIn("renderProviderPresetGrid('providerPresetGrid'", settings)
        self.assertIn("setProviderPresetSelection(provider.preset_id || '')", settings)

    def test_wizard_module_is_new_and_wired(self):
        onboarding = self._onboarding()
        self.assertIn("export async function maybeShowOnboarding(", onboarding)
        self.assertIn("export function selectOnboardingPreset(", onboarding)
        self.assertIn("export async function saveOnboardingProvider(", onboarding)
        self.assertIn("export async function testOnboardingConnection(", onboarding)
        self.assertIn("export function dismissOnboarding(", onboarding)
        # 「可用」判据 = 本地后端或配过 Key 的在线 API（只建卡没 Key 不算）。
        self.assertIn("has_api_key", onboarding)
        # 跳过状态记 localStorage，且名单拿不到时不弹空窗。
        self.assertIn("naibaOnboardingDismissed", onboarding)
        self.assertIn("localStorage.setItem(ONBOARDING_DISMISS_KEY", onboarding)
        self.assertLess(onboarding.index("await loadProviderPresets()"), onboarding.index("dialog.showModal()"))

    def test_bindings_delegate_and_close_remembers(self):
        bind = self._bind()
        self.assertIn("$('#providerPresetGrid').addEventListener('click'", bind)
        self.assertIn("applyProviderPreset(card.dataset.providerPreset)", bind)
        self.assertIn("$('#providerPresetKeyUrl').addEventListener('click'", bind)
        self.assertIn("$('#onboardingPresetGrid').addEventListener('click'", bind)
        self.assertIn("selectOnboardingPreset(card.dataset.providerPreset)", bind)
        self.assertIn("$('#onboardingBack').addEventListener('click', resetOnboarding)", bind)
        self.assertIn("$('#onboardingSkip').addEventListener('click'", bind)
        self.assertIn("$('#onboardingDialog').addEventListener('close', dismissOnboarding)", bind)
        # 向导模块必须真的被 entry 引到，否则打包后整段逻辑不存在。
        self.assertIn('from "./19-onboarding.js"', bind)

    def test_bootstrap_loads_presets_and_maybe_shows_wizard(self):
        bootstrap = self._bootstrap()
        self.assertIn("await loadProviderPresets()", bootstrap)
        self.assertIn("await maybeShowOnboarding()", bootstrap)
        self.assertIn('from "./19-onboarding.js"', bootstrap)

    def test_css_defines_grid_guide_and_responsive_rules(self):
        css = self._css()
        for selector in (
            ".provider-preset-grid {",
            ".provider-preset-card {",
            ".provider-preset-card.is-active",
            ".provider-preset-abbr {",
            ".provider-preset-guide {",
            ".onboarding-preset-grid {",
            ".onboarding-step.is-active",
            "#onboardingKeyRow[hidden]",
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, css)
        grid = css[css.index(".provider-preset-grid {"):]
        grid = grid[: grid.index("}")]
        self.assertIn("repeat(3, minmax(0, 1fr))", grid, "预设卡一行最多三张")
        self.assertIn(".provider-preset-grid, .onboarding-preset-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }", css)
        self.assertIn(".onboarding-dialog {", css)
        self.assertIn("color-mix(in srgb, var(--preset-accent", css, "色块要与主题面/字混色，亮暗主题都得能读")

    def test_save_provider_has_reentry_guard_and_feedback(self):
        """§九.127：设置弹层的「保存设置」必须与首启向导同款防重入 + 慢请求提示。

        事故形态：慢上游探测（`/api/providers/models`）期间按钮不禁用、一句提示都没有
        ⇒ 用户以为没反应、连点 5 次 ⇒ 5 张重复卡。这里钉住四件事：
        重入闸（回车提交绕不过禁用，所以要显式闸）、保存中态、finally 恢复、错误可见。
        """
        settings = self._settings()
        body = settings[settings.index("export async function saveProvider"):]
        body = body[: body.index("\n}")]
        self.assertIn("providerSaveInFlight", body, "必须有重入闸：disabled 挡不住输入框回车提交")
        self.assertIn("button.disabled = true", body)
        self.assertIn("保存中…", body, "与首启向导对齐的保存中态")
        self.assertIn("finally", body, "必须 finally 恢复按钮，否则失败后按钮永久禁用")
        self.assertIn("正在获取模型上下文参数", body, "慢探测阶段要有提示，否则体感是「点了没反应」")
        self.assertIn("toast(`保存失败", body, "弹窗被并发的另一次保存关掉时，错误必须换个地方可见")
        self.assertIn("syncSavedProvider(saved)", body, "两处共用同一份刷新函数")
        # 刷新失败不能把已经保存成功这件事变成「没反应」：刷新调用必须包在 try 里。
        refresh = body.index("syncSavedProvider(saved)")
        self.assertIn("try {", body[:refresh], "刷新（syncSavedProvider）必须包 try/catch")

    def test_index_html_still_has_no_duplicate_ids(self):
        ids = re.findall(r'\sid="([^"]+)"', self._index())
        duplicates = {value for value in ids if ids.count(value) > 1}
        self.assertFalse(duplicates, f"index.html 存在重复 id：{sorted(duplicates)}")

    def test_update_json_still_valid(self):
        """顺手守住清单 JSON（发布时必改的文件之一）。"""
        for name in ("naiba-chat-update.json", "release_notes.json"):
            with self.subTest(name=name):
                json.loads((ROOT / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
