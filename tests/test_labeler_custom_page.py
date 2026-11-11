"""护栏：贴签匠「预设打标 / 自定义打标」两页签与自定义档（system_prompt）。

自定义档口径（2026-11-07 用户拍板，取代 2026-11-01 交接的 custom_lora 方案）：
- 配置键 system_prompt：**键存在 = 自定义档**，整段手输提示词即全部规则，
  预设的忽略词/输出规则不叠加；预设档快照不落该键（空串省键，往返无损）。
- 输出形式/语言支持 auto（「以提示词为准」）：不追加规则；auto 只在自定义档可用。
- 指纹：system_prompt 只在非空时进指纹，预设任务指纹与旧版本逐字节一致。
- Skill 载入：与聊天同一份列表（/api/skills/scan），正文经 /api/skills/content 灌进提示词框。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from naiba.labeler.engines.provider import build_prompt
from naiba.labeler.jobs import LabelerTaskError, config_fingerprint, normalize_config
from naiba.labeler.presets import get_preset


class _StubConfig:
    def __init__(self, kind: str = "online"):
        self.kind = kind

    def profile(self, key):
        return {"kind": self.kind, "name": "stub", "model": "stub-model",
                "base_url": "http://127.0.0.1:9"}

    def runtime_guard_options(self):
        return {}


class _StubPaths:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)


class _StubApp:
    def __init__(self, data_dir: Path):
        self.paths = _StubPaths(data_dir)
        self.config = _StubConfig()


def _base_config(**overrides):
    config = {
        "engine": "provider",
        "output_mode": "keywords",
        "language": "en",
        "preset": "general",
        "model_key": "online:stub",
        "model_name": "stub-model",
        "confidence_threshold": 0.35,
        "max_tags": 24,
        "custom_prompt": "",
        "subject_filter": {"enabled": False, "keywords": [], "strength": "medium"},
        "concurrency": 1,
        "retry_limit": 1,
        "timeout_seconds": 90,
        "prompt_version": 2,
    }
    config.update(overrides)
    return config


def _legacy_fingerprint(config: dict) -> str:
    """旧版（4.1.2-beta）指纹算法：FINGERPRINT_KEYS 无 system_prompt。

    预设档指纹必须与旧版本逐字节一致（已有标注照常复用、不重复计费），
    这里按旧键集独立重算一遍作为对照基准。
    """
    keys = ("engine", "output_mode", "language", "preset", "model_key", "model_name",
            "confidence_threshold", "max_tags", "custom_prompt", "subject_filter",
            "prompt_version", "trigger_word")
    payload = {key: config.get(key) for key in keys}
    trigger = payload.get("trigger_word")
    if not (isinstance(trigger, dict) and trigger.get("enabled")
            and str(trigger.get("value") or "").strip()):
        payload.pop("trigger_word", None)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "cfg-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


class CustomPageConfigTests(unittest.TestCase):
    def setUp(self):
        self.app = _StubApp(Path(tempfile.gettempdir()))

    def test_custom_config_accepts_system_prompt_and_auto(self):
        config = normalize_config(self.app, _base_config(
            output_mode="auto", language="auto", system_prompt="输出 5-10 个英文标签。"))
        self.assertEqual(config["system_prompt"], "输出 5-10 个英文标签。")
        self.assertEqual(config["output_mode"], "auto")
        self.assertEqual(config["language"], "auto")

    def test_custom_config_concrete_output_mode_still_valid(self):
        config = normalize_config(self.app, _base_config(
            output_mode="description", language="zh",
            system_prompt="你是一个插画师助手。"))
        self.assertEqual(config["output_mode"], "description")
        self.assertEqual(config["language"], "zh")

    def test_auto_requires_system_prompt(self):
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(output_mode="auto"))
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(language="auto"))

    def test_empty_system_prompt_rejected(self):
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(output_mode="auto", system_prompt="   "))
        with self.assertRaises(LabelerTaskError):
            normalize_config(self.app, _base_config(system_prompt=""))

    def test_oversized_system_prompt_is_preserved_without_silent_truncation(self):
        prompt = "规" * 24001 + "尾部规则"
        accepted = normalize_config(self.app, _base_config(
            output_mode="auto", language="auto", system_prompt=prompt))
        self.assertEqual(accepted["system_prompt"], prompt)

    def test_preset_config_has_no_system_prompt_key(self):
        config = normalize_config(self.app, _base_config())
        self.assertNotIn("system_prompt", config)

    def test_preset_fingerprint_matches_legacy_algorithm(self):
        # 预设档指纹与旧版本逐字节一致：旧标注照常复用。
        config = normalize_config(self.app, _base_config())
        self.assertEqual(config["config_fingerprint"], _legacy_fingerprint(config))
        triggered = normalize_config(self.app, _base_config(
            trigger_word={"enabled": True, "value": "ohwx"}))
        self.assertEqual(triggered["config_fingerprint"], _legacy_fingerprint(triggered))

    def test_custom_fingerprint_distinguished_by_system_prompt(self):
        base = normalize_config(self.app, _base_config())
        custom = normalize_config(self.app, _base_config(
            output_mode="auto", language="auto", system_prompt="只描述动物。"))
        self.assertNotEqual(base["config_fingerprint"], custom["config_fingerprint"])
        self.assertEqual(custom["config_fingerprint"],
                         config_fingerprint(normalize_config(self.app, _base_config(
                             output_mode="auto", language="auto", system_prompt="只描述动物。"))))

    def test_preset_snapshot_roundtrip_omits_key(self):
        # 继续任务走 allow_legacy_language 重归一化：预设快照无键、往返不误判。
        first = normalize_config(self.app, _base_config())
        snapshot = {key: value for key, value in first.items() if key != "config_fingerprint"}
        second = normalize_config(self.app, snapshot, allow_legacy_language=True)
        self.assertNotIn("system_prompt", second)
        self.assertEqual(first["config_fingerprint"], second["config_fingerprint"])

    def test_custom_snapshot_roundtrip_keeps_prompt(self):
        first = normalize_config(self.app, _base_config(
            output_mode="auto", language="auto", system_prompt="输出 JSON。"))
        snapshot = {key: value for key, value in first.items() if key != "config_fingerprint"}
        second = normalize_config(self.app, snapshot, allow_legacy_language=True)
        self.assertEqual(second["system_prompt"], "输出 JSON。")
        self.assertEqual(first["config_fingerprint"], second["config_fingerprint"])


class CustomPromptBuildTests(unittest.TestCase):
    def setUp(self):
        self.preset = get_preset("character_lora")

    def test_custom_prompt_is_verbatim_with_auto(self):
        # 自定义档 + auto：手输正文原样返回，不叠 JSON 契约/预设忽略段/输出规则。
        prompt = build_prompt({
            "output_mode": "auto", "language": "auto",
            "system_prompt": "列出画面里的一切，但不要提天气。",
        }, self.preset)
        self.assertEqual(prompt, "列出画面里的一切，但不要提天气。")

    def test_custom_prompt_appends_explicit_rules_only(self):
        prompt = build_prompt({
            "output_mode": "description", "language": "en",
            "system_prompt": "描述这张图。",
        }, self.preset)
        self.assertTrue(prompt.startswith("描述这张图。"))
        self.assertIn("输出语言", prompt)
        self.assertIn("输出形式", prompt)
        # 预设的忽略段与 JSON 契约不叠加。
        self.assertNotIn("必须从标签和描述里省略", prompt)
        self.assertNotIn("你是图片标注助手", prompt)

    def test_custom_phrases_with_auto_language_is_neutral(self):
        prompt = build_prompt({
            "output_mode": "phrases", "language": "auto",
            "system_prompt": "短语化描述。",
        }, self.preset)
        self.assertIn("语言与上方提示词保持一致", prompt)

    def test_preset_path_unchanged(self):
        # 预设档维持既有形态：JSON 契约 + 生成模式忽略段，system_prompt 不参与。
        config = {
            "output_mode": "keywords", "language": "en", "max_tags": 24,
            "confidence_threshold": 0.35, "custom_prompt": "",
        }
        prompt = build_prompt(config, self.preset)
        self.assertIn("你是图片标注助手", prompt)
        self.assertIn("必须从标签和描述里省略", prompt)

    def test_write_txt_rule_added_for_both_prompt_paths(self):
        # 预设档与自定义手输档都要告诉模型用同一件写入工具。
        preset_prompt = build_prompt({
            "output_mode": "keywords", "language": "en", "max_tags": 24,
            "confidence_threshold": 0.35, "custom_prompt": "", "write_txt": True,
        }, self.preset)
        self.assertIn("write_caption_txt", preset_prompt)
        custom_prompt = build_prompt({
            "output_mode": "auto", "language": "auto",
            "system_prompt": "只描述动物。", "write_txt": True,
        }, self.preset)
        self.assertTrue(custom_prompt.startswith("只描述动物。"))
        self.assertIn("write_caption_txt", custom_prompt)
        # 关闭时不出现任何工具规则（原提示词逐字节不变）。
        self.assertNotIn("write_caption_txt", build_prompt({
            "output_mode": "auto", "language": "auto", "system_prompt": "只描述动物。",
        }, self.preset))


class CustomPageUiContractTests(unittest.TestCase):
    """两页签与自定义页的源码契约（页面结构 / 接线 / 显隐规则 / 后端路由）。"""

    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        self.html = (self.root / "public/index.html").read_text(encoding="utf-8")
        self.js = (self.root / "public/js/23-labeler.js").read_text(encoding="utf-8")

    def test_page_tabs_and_bodies_in_html(self):
        # 根节点挂 dataset 状态；方式与窄屏分档分别分组；两套 body 互斥。
        self.assertIn('id="studioPageLabeler" data-studio-page-view="labeler" data-labeler-page="preset"', self.html)
        self.assertIn('data-labeler-page-tab="preset">预设打标', self.html)
        self.assertIn('data-labeler-page-tab="custom">自定义打标', self.html)
        self.assertIn('class="wb-media-tabs" role="group" aria-label="打标方式"', self.html)
        self.assertIn('class="wb-media-tabs labeler-view-tabs"', self.html)
        self.assertIn('data-labeler-page-body="preset"', self.html)
        self.assertIn('data-labeler-page-body="custom" hidden', self.html)
        # 预设页字段在 preset body 内；自定义页字段在 custom body 内。
        self.assertLess(self.html.index('data-labeler-page-body="preset"'),
                        self.html.index('id="labelerPreset"'))
        self.assertLess(self.html.index('id="labelerPreset"'),
                        self.html.index('data-labeler-page-body="custom"'))
        self.assertIn('id="labelerCustomSystemPrompt"', self.html)
        self.assertIn('id="labelerCustomOutputMode"', self.html)
        self.assertIn('id="labelerCustomLanguage"', self.html)

    def test_custom_page_defaults_to_prompt_decides(self):
        # 输出形式/语言默认「以提示词为准」（auto）。
        self.assertIn('<option value="auto" selected>以提示词为准</option>', self.html)
        self.assertGreaterEqual(self.html.count('<option value="auto" selected>以提示词为准</option>'), 2)

    def test_skill_load_controls_in_html(self):
        # 选中即应用（用户 2026-11-07 拍板）：Skill 完整目录不灌提示词框，
        # 载入按钮全删，提示词框是纯手输区；只留「清空提示词」做重置。
        self.assertIn('id="labelerSkillSelect"', self.html)
        self.assertNotIn("labelerSkillLoadFull", self.html)
        self.assertNotIn("labelerSkillLoadBody", self.html)
        self.assertNotIn("载入正文", self.html)
        self.assertIn('id="labelerSkillClear"', self.html)
        self.assertIn('id="labelerSkillNote"', self.html)

    def test_rename_labeler_to_tieqianjiang_display_only(self):
        # 用户可见名改「贴签匠」；DOM id / 类名 / data-* 不动。
        self.assertIn("<span>贴签匠</span>", self.html)
        self.assertNotIn("<span>打标器</span>", self.html)
        self.assertIn('aria-label="贴签匠分档"', self.html)
        self.assertIn('id="labelerPanes"', self.html)
        self.assertIn('data-studio-page="labeler"', self.html)
        # 展示层按 module 映射新名，记录常量不重写。
        studio_js = (self.root / "public/js/22-studio.js").read_text(encoding="utf-8")
        self.assertIn('if (module === "labeler") return "贴签匠";', studio_js)

    def test_labeler_js_page_switch_and_custom_collect(self):
        self.assertIn("function currentLabelerPage()", self.js)
        self.assertIn("function setLabelerPage(page)", self.js)
        self.assertIn('page2.dataset.labelerPage = next', self.js)
        # 自定义档收集：规则来源二选一——选了 Skill 走对话链路（engine=chat + skill_id），
        # 没选就手输提示词；auto 默认 + 预设固定 general。
        self.assertIn('currentLabelerPage() === "custom"', self.js)
        self.assertIn('config.engine = "chat"', self.js)
        self.assertIn('config.skill_id = labeler.skillPick.id', self.js)
        self.assertIn('config.system_prompt = String($("#labelerCustomSystemPrompt")?.value || "").trim()', self.js)
        self.assertIn('config.output_mode = $("#labelerCustomOutputMode")?.value || "auto"', self.js)
        self.assertIn('config.preset = "general"', self.js)
        # 空规则在提交前拦截（没选 Skill 也没手输）。
        self.assertIn("自定义打标要先选一个 Skill", self.js)

    def test_skill_load_uses_chat_skill_surface(self):
        # 与聊天同一份接口且同方法：scan 走 POST；选中 Skill = 走对话链路（engine=chat）。
        # 2026-11-07 拍板：正文不再灌提示词框，bundle 内联方案废弃（端点保留不接前端），
        # 超限/截断类 UI 一律不存在（对话模块口径：不截断）。
        self.assertIn('api("/api/skills/scan", { method: "POST", body: {} })', self.js)
        self.assertIn("function applySelectedSkill()", self.js)
        self.assertIn("function skillPlaceholderOption()", self.js)
        self.assertIn("labeler.skillPick", self.js)
        self.assertIn("function syncCustomEngineUi()", self.js)
        self.assertIn('config.engine = "chat"', self.js)
        self.assertNotIn("systemPromptOverlimitNote", self.js)
        self.assertNotIn("LABELER_MAX_SYSTEM_PROMPT", self.js)
        self.assertNotIn("mode: \"bundle\"", self.js)
        self.assertIn("function clearSystemPrompt()", self.js)

    def test_skill_content_api_returns_both_flavors(self):
        # /api/skills/content 同时回完整全文与去头正文，口味由前端按钮决定。
        catalog_py = (self.root / "naiba/skills/catalog.py").read_text(encoding="utf-8")
        self.assertIn('"content": text', catalog_py)
        self.assertIn('"body": _strip_frontmatter(text)', catalog_py)
        self.assertIn("def _strip_frontmatter(text: str)", catalog_py)

    def test_panes_click_delegates_page_tabs_first(self):
        # 同一个 #labelerPanes 委托里页签优先于三档页签（互不串扰）。
        pane_index = self.js.index('$("#labelerPanes")?.addEventListener("click"')
        self.assertLess(pane_index, self.js.index('data-labeler-page-tab]', pane_index))
        self.assertLess(pane_index, self.js.index('data-labeler-pane]', pane_index))

    def test_page_tab_click_opens_config_pane(self):
        # 手机三档下两页签的内容只在「设置」档里：点页签必须同切设置档，
        # 否则在批次/结果档上点页签只有高亮变化、页面不切换（2026-11-10 反馈）。
        pane_index = self.js.index('$("#labelerPanes")?.addEventListener("click"')
        page_branch = self.js.index('data-labeler-page-tab]', pane_index)
        pane_branch = self.js.index('data-labeler-pane]', page_branch)
        self.assertIn('setLabelerPane("config")', self.js[page_branch:pane_branch])

    def test_mobile_page_tabs_fit_their_buttons(self):
        # 手机五枚按钮同排：「自定义打标」比等分格子宽，必须按内容给下限，
        # 否则文字越出按钮（2026-11-10 远端截图反馈）。
        css = (self.root / "public/styles.css").read_text(encoding="utf-8")
        self.assertIn('.labeler-panes .studio-pane-btn { flex: 1 1 auto; min-width: max-content; }', css)

    def test_styles_toggle_by_dataset(self):
        css = (self.root / "public/styles.css").read_text(encoding="utf-8")
        self.assertIn('#studioPageLabeler[data-labeler-page="preset"] [data-labeler-page-body="custom"] { display: none; }', css)
        self.assertIn('#studioPageLabeler[data-labeler-page="custom"] [data-labeler-page-body="preset"] { display: none; }', css)
        self.assertIn('#studioPageLabeler[data-labeler-page="custom"] [data-labeler-page-body="custom"]', css)
        # 自定义页隐藏预设语义的「补充要求」。
        self.assertIn('#studioPageLabeler[data-labeler-page="custom"] .labeler-advanced .labeler-prompt-field { display: none; }', css)

    def test_result_card_title_matches_export_naming(self):
        # 逐图结果卡标题与导出同口径（relative_path || name），能逐行对照导出
        # 文件；旧批次的 name 曾被服务端缓存名（naiba_chat_…）顶替过，不能当
        # 标题（2026-11-09 用户反馈）。标题与路径同一信息只留一层。
        self.assertIn('const displayName = item.relative_path || item.name || item.item_id;', self.js)
        self.assertIn("name.textContent = displayName;", self.js)
        self.assertIn("name.title = displayName;", self.js)
        self.assertNotIn("labeler-anno-path", self.js)
        css = (self.root / "public/styles.css").read_text(encoding="utf-8")
        self.assertNotIn(".labeler-anno-path", css)

    def test_upload_response_does_not_overwrite_display_name(self):
        # 上传响应不再把服务端缓存落盘名写回显示名：提交与展示都以原文件名
        # 走（否则结果区/提交批次里满是 naiba_chat_时间戳_…）。
        self.assertNotIn("row.name = String(payload.name)", self.js)

    def test_write_txt_switches_and_result_entries(self):
        # 2026-11-10 计划：两个页签各一个「写入本地 TXT」开关；结果区可查看/
        # 下载已写入的 TXT，并可打开本批次输出目录。
        self.assertIn('id="labelerWriteTxtPreset"', self.html)
        self.assertIn('id="labelerWriteTxtCustom"', self.html)
        self.assertIn('id="labelerOpenTxt"', self.html)
        self.assertIn('id="labelerTxtDialog"', self.html)
        self.assertIn('id="labelerTxtDialogBody"', self.html)
        self.assertIn('write_txt: Boolean($("#labelerWriteTxtPreset")?.checked)', self.js)
        self.assertIn('config.write_txt = Boolean($("#labelerWriteTxtCustom")?.checked)', self.js)
        self.assertIn("function viewTxt(", self.js)
        self.assertIn("function downloadTxt(", self.js)
        self.assertIn("function openTxtFolder(", self.js)
        self.assertIn("/txt/open", self.js)
        self.assertIn("data-labeler-txt-view", self.js)
        self.assertIn("data-labeler-txt-download", self.js)

    def test_write_txt_backend_routes(self):
        http_py = (self.root / "naiba/http.py").read_text(encoding="utf-8")
        service_py = (self.root / "naiba/labeler/service.py").read_text(encoding="utf-8")
        self.assertIn('path.endswith("/txt")', http_py)
        self.assertIn('"/txt/" in path', http_py)
        self.assertIn('path.endswith("/txt/open")', http_py)
        self.assertIn("def api_task_txt_files(", service_py)
        self.assertIn("def api_task_txt_content(", service_py)
        self.assertIn("def api_task_txt_open(", service_py)

    def test_backend_skill_content_route(self):
        http_py = (self.root / "naiba/http.py").read_text(encoding="utf-8")
        catalog_py = (self.root / "naiba/skills/catalog.py").read_text(encoding="utf-8")
        self.assertIn('"/api/skills/content"', http_py)
        self.assertIn("def skill_content(self, skill_id: str)", catalog_py)
        self.assertIn("read_skill_content(row[\"path\"])", catalog_py)
        # mode="bundle" 分发到完整目录文本包；脚本只报名单不收正文。
        self.assertIn("skill_bundle", http_py)
        self.assertIn("def skill_bundle(", catalog_py)
        self.assertIn('"script_names"', catalog_py)


if __name__ == "__main__":
    unittest.main()
