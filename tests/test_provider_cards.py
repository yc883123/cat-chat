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
import shutil
import subprocess
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


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行供应商目录行为测试")
class ProviderCatalogBehaviorTests(unittest.TestCase):
    """把实际生产函数放进 VM，用本地 Promise 控制保存、删除与目录请求时序。"""

    HARNESS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// 沿用 test_provider_copy.py 的顶层函数提取方式；函数体与常量均来自生产文件。
const core = fs.readFileSync('public/js/01-core.js', 'utf8');
const models = fs.readFileSync('public/js/07-models-agents.js', 'utf8');
const settings = fs.readFileSync('public/js/09-settings.js', 'utf8');
function actualFunction(source, name) {
  const match = new RegExp(`^(?:export )?(?:async )?function ${name}\\(`, 'm').exec(source);
  assert.ok(match, `missing production function ${name}`);
  const closing = source.indexOf('\n}', match.index);
  assert.ok(closing > match.index, `missing end of production function ${name}`);
  return source.slice(match.index, closing + 2).replace(/^export /, '');
}
function actualConst(source, name) {
  const match = new RegExp(`^const ${name} = [^;]+;`, 'm').exec(source);
  assert.ok(match, `missing production const ${name}`);
  return match[0];
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
const flush = () => new Promise(setImmediate);
const plain = value => JSON.parse(JSON.stringify(value));
const original = { id: 'edited/id', model_key: 'online:edited', name: 'Original', base_url: 'https://old.invalid/v1' };
const other = { id: 'other', model_key: 'online:other', name: 'Other' };
const otherCatalog = [{ id: 'other-model' }];
const state = {
  bootstrap: { providers: [original, other], model_profiles: [original, other], retained: 'local-state' },
  providerModelCatalogs: { [original.model_key]: [{ id: 'cached-old' }], [other.model_key]: otherCatalog },
};
const controls = new Map([
  ['#visionProvider', { dataset: { populated: 'yes' } }],
  ['#providerId', { value: '' }],
]);
const requests = [], warnings = [], ui = [], confirmations = [];
const context = vm.createContext({
  state,
  console: { warn: (...args) => warnings.push(args) },
  $: selector => controls.get(selector) || null,
  confirm: message => { confirmations.push(message); return true; },
  api: (url, options = {}) => {
    assert.ok(['/api/providers/models', '/api/bootstrap', `/api/providers/${encodeURIComponent(original.id)}`].includes(url),
      `unexpected local API request ${url}`);
    const pending = deferred();
    requests.push({ url, options, ...pending });
    return pending.promise;
  },
  populateVisionSettings: () => ui.push('vision'),
  populateModels: () => ui.push('models'),
  refreshUsagePricingBadge: () => ui.push('pricing'),
  renderProviders: () => ui.push('cards'),
  cancelProviderEdit: () => ui.push('cancel'),
  toast: message => ui.push(message),
});
vm.runInContext([
  actualConst(core, 'providersChangedHooks'),
  ...['onProvidersChanged', 'notifyProvidersChanged'].map(name => actualFunction(core, name)),
  ...['catalogGenerations', 'catalogRequests'].map(name => actualConst(models, name)),
  ...['invalidateModelCatalogs', 'loadModelCatalog'].map(name => actualFunction(models, name)),
  ...['providerProfiles', 'syncSavedProvider', 'deleteProvider'].map(name => actualFunction(settings, name)),
].join('\n'), context, { timeout: 1000 });
const { catalogGenerations, catalogRequests } = vm.runInContext('({ catalogGenerations, catalogRequests })', context);
function request(index, url = '/api/providers/models', method = 'POST') {
  const result = requests[index];
  assert.ok(result, `missing local request ${index}`);
  assert.equal(result.url, url);
  assert.equal(result.options.method || 'GET', method);
  return result;
}
"""

    def run_case(self, body: str) -> None:
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-"],
            input=self.HARNESS + "\n" + body,
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_save_updates_both_bootstrap_lists_and_invalidates_before_notification(self) -> None:
        for mode in ("add", "same_key", "renamed_key"):
            with self.subTest(mode=mode):
                self.run_case("const mode = " + repr(mode) + ";\n" + r"""
const saved = { ...original, name: 'Saved', base_url: 'https://new.invalid/v1',
  ...(mode === 'add' ? { id: 'new', model_key: 'online:new' } : {}),
  ...(mode === 'renamed_key' ? { model_key: 'local:edited' } : {}) };
const affected = mode === 'add' ? [saved.model_key] : [...new Set([original.model_key, saved.model_key])];
const oldLoads = affected.map(key => context.loadModelCatalog(key, { force: true }));
for (const key of affected) state.providerModelCatalogs[key] = [{ id: 'stale' }];
const unrelatedLoad = context.loadModelCatalog(other.model_key, { force: true });
const unrelatedRequest = catalogRequests.get(other.model_key);
const unrelatedGeneration = catalogGenerations.get(other.model_key);
const snapshots = [];
context.onProvidersChanged(keys => snapshots.push({
  keys: plain(keys),
  providers: plain(state.bootstrap.providers), profiles: plain(state.bootstrap.model_profiles),
  invalidated: affected.every(key => !Object.hasOwn(state.providerModelCatalogs, key) && !catalogRequests.has(key)),
}));
context.syncSavedProvider(saved);
assert.equal(snapshots.length, 1);
assert.deepEqual(snapshots[0].keys, affected);
assert.equal(snapshots[0].invalidated, true, '通知之前必须失效目录与在途请求');
for (const list of [snapshots[0].providers, snapshots[0].profiles]) {
  assert.deepEqual(list.find(item => item.id === saved.id), saved, '通知时必须已更新 bootstrap');
  assert.equal(list.length, mode === 'add' ? 3 : 2);
  assert.equal(list.filter(item => item.id === saved.id).length, 1);
  assert.deepEqual(list.find(item => item.id === other.id), other);
}
assert.strictEqual(state.providerModelCatalogs[other.model_key], otherCatalog);
assert.strictEqual(catalogRequests.get(other.model_key), unrelatedRequest);
assert.equal(catalogGenerations.get(other.model_key), unrelatedGeneration);
assert.equal(state.bootstrap.retained, 'local-state');
assert.equal(controls.get('#visionProvider').dataset.populated, undefined);
assert.deepEqual(ui, ['vision', 'models', 'pricing']);
for (let i = 0; i < affected.length; i++) request(i).resolve({ models: [{ id: 'late-old' }] });
for (const result of await Promise.all(oldLoads)) assert.deepEqual(plain(result), []);
assert.ok(affected.every(key => !Object.hasOwn(state.providerModelCatalogs, key)));
request(affected.length).resolve({ models: otherCatalog });
assert.strictEqual(await unrelatedLoad, otherCatalog);
""")

    def test_catalog_reuses_cache_and_inflight_request_but_force_starts_new_generation(self) -> None:
        self.run_case(r"""
assert.strictEqual(await context.loadModelCatalog(other.model_key), otherCatalog);
assert.equal(requests.length, 0, '其他 API 的有效缓存不应重拉');
const key = 'online:uncached';
const first = context.loadModelCatalog(key);
const duplicate = context.loadModelCatalog(key);
assert.equal(requests.length, 1, '同代次在途请求应复用');
assert.deepEqual(plain(request(0).options.body), { model_key: key });
const values = { model_key: key, base_url: 'https://fixture.invalid/v1', api_key: 'local-fixture' };
const forced = context.loadModelCatalog(key, { force: true, values });
assert.equal(requests.length, 2);
assert.strictEqual(request(1).options.body, values);
const fresh = [{ id: 'fresh' }];
request(1).resolve({ models: fresh });
assert.strictEqual(await forced, fresh);
request(0).resolve({ models: [{ id: 'obsolete' }] });
assert.strictEqual(await first, fresh);
assert.strictEqual(await duplicate, fresh);
assert.strictEqual(await context.loadModelCatalog(key), fresh);
assert.equal(requests.length, 2);
assert.strictEqual(state.providerModelCatalogs[other.model_key], otherCatalog);
""")

    def test_edit_same_key_stale_success_or_failure_follows_new_request(self) -> None:
        for old_fails in (False, True):
            for old_finishes_first in (False, True):
                with self.subTest(old_fails=old_fails, old_finishes_first=old_finishes_first):
                    self.run_case(
                        f"const oldFails = {str(old_fails).lower()}, oldFirst = {str(old_finishes_first).lower()};\n"
                        + r"""
const key = original.model_key;
const oldLoad = context.loadModelCatalog(key, { force: true });
let newLoad;
context.onProvidersChanged(() => { newLoad = context.loadModelCatalog(key); });
context.syncSavedProvider({ ...original, base_url: 'https://edited.invalid/v1' });
assert.equal(requests.length, 2, '同 key 编辑后回调必须发出新请求');
const currentRequest = catalogRequests.get(key);
const currentGeneration = catalogGenerations.get(key);
const finishOld = () => oldFails
  ? request(0).reject(new Error('obsolete connection failed'))
  : request(0).resolve({ models: [{ id: 'obsolete' }] });
const fresh = [{ id: 'new-connection-model' }];
if (oldFirst) {
  finishOld();
  await flush();
  assert.equal(Object.hasOwn(state.providerModelCatalogs, key), false);
  assert.strictEqual(catalogRequests.get(key), currentRequest, '旧 finally 不得删除新在途请求');
  assert.equal(catalogGenerations.get(key), currentGeneration);
  const joined = context.loadModelCatalog(key);
  assert.equal(requests.length, 2, '旧请求完成后仍应复用新请求');
  request(1).resolve({ models: fresh });
  assert.strictEqual(await joined, fresh);
} else {
  request(1).resolve({ models: fresh });
  assert.strictEqual(await newLoad, fresh);
  finishOld();
}
assert.strictEqual(await oldLoad, fresh, '旧请求的成功或失败都应跟随新目录');
assert.strictEqual(await newLoad, fresh);
assert.strictEqual(state.providerModelCatalogs[key], fresh);
assert.equal(catalogRequests.has(key), false);
assert.strictEqual(state.providerModelCatalogs[other.model_key], otherCatalog);
""")

    def test_invalidated_failure_without_replacement_does_not_resurrect_catalog(self) -> None:
        self.run_case(r"""
const key = original.model_key;
const pending = context.loadModelCatalog(key, { force: true });
context.invalidateModelCatalogs([key]);
request(0).reject(new Error('invalidated request failed'));
assert.deepEqual(plain(await pending), []);
assert.equal(Object.hasOwn(state.providerModelCatalogs, key), false);
assert.equal(catalogRequests.has(key), false);
assert.strictEqual(state.providerModelCatalogs[other.model_key], otherCatalog);
""")

    def test_current_failure_still_rejects_and_allows_retry(self) -> None:
        self.run_case(r"""
const key = 'online:uncached';
const pending = context.loadModelCatalog(key);
const error = new Error('current connection failed');
const rejected = assert.rejects(pending, value => value === error);
request(0).reject(error);
await rejected;
assert.equal(catalogRequests.has(key), false);
assert.equal(Object.hasOwn(state.providerModelCatalogs, key), false);
const retry = context.loadModelCatalog(key);
request(1).resolve({ models: [{ id: 'retry-model' }] });
assert.deepEqual(await retry, [{ id: 'retry-model' }]);
""")

    def test_delete_refreshes_bootstrap_and_invalidates_before_notifying(self) -> None:
        for editing_deleted_provider in (False, True):
            with self.subTest(editing_deleted_provider=editing_deleted_provider):
                self.run_case(
                    f"const editingDeleted = {str(editing_deleted_provider).lower()};\n" + r"""
const key = original.model_key;
controls.get('#providerId').value = editingDeleted ? original.id : other.id;
const oldLoad = context.loadModelCatalog(key, { force: true });
const snapshots = [];
context.onProvidersChanged(keys => snapshots.push({
  keys: plain(keys), providers: plain(state.bootstrap.providers), profiles: plain(state.bootstrap.model_profiles),
  cached: Object.hasOwn(state.providerModelCatalogs, key), inFlight: catalogRequests.has(key),
}));
const deleting = context.deleteProvider(original.id);
assert.equal(confirmations.length, 1);
assert.ok(confirmations[0].includes(original.name));
request(1, `/api/providers/${encodeURIComponent(original.id)}`, 'DELETE').resolve({});
await flush();
assert.equal(snapshots.length, 0, 'bootstrap 返回之前不应通知');
assert.ok(Object.hasOwn(state.providerModelCatalogs, key));
request(2, '/api/bootstrap', 'GET').resolve({ providers: [other], model_profiles: [other] });
await deleting;
assert.deepEqual(snapshots, [{ keys: [key], providers: [other], profiles: [other], cached: false, inFlight: false }]);
assert.strictEqual(state.providerModelCatalogs[other.model_key], otherCatalog);
assert.equal(state.bootstrap.retained, 'local-state');
assert.equal(controls.get('#visionProvider').dataset.populated, undefined);
assert.deepEqual(ui, [editingDeleted ? 'cancel' : 'cards', 'models', 'vision', '供应商已删除']);
request(0).resolve({ models: [{ id: 'deleted-api-late-model' }] });
assert.deepEqual(plain(await oldLoad), []);
assert.equal(Object.hasOwn(state.providerModelCatalogs, key), false);
""")

    def test_cancelled_or_unknown_delete_leaves_catalog_and_hooks_untouched(self) -> None:
        self.run_case(r"""
let notified = 0;
context.onProvidersChanged(() => { notified++; });
const before = JSON.stringify(state);
await context.deleteProvider('missing');
assert.equal(confirmations.length, 0);
context.confirm = () => false;
await context.deleteProvider(original.id);
assert.equal(JSON.stringify(state), before);
assert.equal(requests.length, 0);
assert.equal(notified, 0);
assert.deepEqual(ui, []);
""")

    def test_notifications_deduplicate_keys_isolate_callbacks_and_allow_unsubscribe(self) -> None:
        self.run_case(r"""
const calls = [];
const syncError = new Error('synchronous hook failure');
const asyncError = new Error('asynchronous hook failure');
const offSync = context.onProvidersChanged(() => { throw syncError; });
const offAsync = context.onProvidersChanged(() => Promise.reject(asyncError));
const listener = keys => calls.push(plain(keys));
const offListener = context.onProvidersChanged(listener);
context.onProvidersChanged(listener);
const offLast = context.onProvidersChanged(keys => calls.push(['last', ...keys]));
assert.doesNotThrow(() => context.notifyProvidersChanged(['online:a', '', null, 'online:a', 'local:b', undefined]));
await flush();
assert.deepEqual(calls, [['online:a', 'local:b'], ['last', 'online:a', 'local:b']]);
assert.equal(warnings.length, 2);
assert.ok(warnings.some(args => args.includes(syncError)));
assert.ok(warnings.some(args => args.includes(asyncError)));
offSync(); offAsync(); offListener();
context.notifyProvidersChanged(['online:c']);
assert.deepEqual(calls.at(-1), ['last', 'online:c']);
assert.equal(calls.length, 3);
offLast();
context.notifyProvidersChanged(['online:d']);
assert.equal(calls.length, 3);
assert.equal(requests.length, 0);
""")


if __name__ == "__main__":
    unittest.main()
