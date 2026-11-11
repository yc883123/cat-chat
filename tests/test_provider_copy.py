"""执行真实供应商表单 JavaScript，守住副本身份、密钥与异步归属。"""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
NODE_DRIVER = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('public/js/09-settings.js', 'utf8');
const controls = new Map();
function $(selector) {
  if (!controls.has(selector)) {
    let value = '';
    controls.set(selector, {
      get value() { return value; }, set value(next) { value = String(next ?? ''); },
      textContent: '', type: 'text', open: false, disabled: false,
      focus() {}, showModal() { this.open = true; }, close() { this.open = false; },
    });
  }
  return controls.get(selector);
}
const profiles = [];
const requests = [];
const notices = [];
let identity = 0;
const context = {
  $, profiles, requests, console, clearTimeout,
  state: { providerEditing: false, providerPresetId: '' },
  crypto: { randomUUID: () => `independent-${++identity}` },
  providerProfiles: () => profiles,
  setProviderModelOptions: (_models, selected) => { $('#providerModel').value = selected; },
  setProviderEditMode: editing => { context.state.providerEditing = editing; },
  setProviderPresetSelection: preset => { context.state.providerPresetId = preset; },
  providerPresets: () => [{}], providerPresetById: () => ({}),
  renderProviderPresetGrid() {}, fillProviderPresetGuide() {}, refreshProviderPresetGrid() {},
  syncProviderKindOptions() {}, updateProviderFormatGuide() {}, updateProviderContextField() {},
  updateProviderKvmemFields() {}, updateProviderVisionHint() {}, updateUnloadModelButton() {},
  renderProviders() {},
  syncSavedProvider(saved) { profiles.push(saved); },
  toast(message) { notices.push(message); },
  api: async (url, options) => {
    requests.push({ url, body: JSON.parse(JSON.stringify(options?.body || {})) });
    return { ...options.body };
  },
};
vm.createContext(context);
function actualFunction(name) {
  const match = new RegExp(`^(?:export )?(?:async )?function ${name}\\(`, 'm').exec(source);
  assert.ok(match, `missing production function ${name}`);
  const end = source.indexOf('\n}', match.index) + 2;
  assert.ok(end > match.index);
  return source.slice(match.index, end).replace(/^export /, '');
}
const declarations = [
  'providerFormGeneration', 'providerCopyId', 'providerModelCheckGeneration',
  'providerModelCheckTimer', 'providerSaveInFlight',
].map(name => {
  const match = new RegExp(`^(?:export )?let ${name}(?: = [^;]+)?;`, 'm').exec(source);
  assert.ok(match, `missing production state ${name}`);
  return match[0].replace(/^export /, '');
});
vm.runInContext(declarations.join('\n') + '\n' + [
  'nextProviderCopyName', 'copyProvider', 'showProviderForm', 'providerFormValue',
  'saveProvider', 'cancelProviderEdit', 'toggleProviderKey',
].map(actualFunction).join('\n'), context);
const event = { preventDefault() {} };
const data = () => ({
  id: 'original', model_key: 'online:original', name: '测试供应商', kind: 'online',
  base_url: 'https://fixture.invalid/v1', request_format: 'openai_chat', model: 'fixture-model',
  api_key: 'source-secret-must-not-copy', has_api_key: true, is_default: true,
  context_window: 8192, context_window_source: 'provider_config', max_output_tokens: 2048,
  temperature: 0, reasoning_effort: 'high', supports_images_explicit: false,
  pricing: { input: 99 }, context_window_probed: 4096, status: 'running',
});
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
const cases = {
  async fields_and_identity() {
    const original = data(); profiles.push(original);
    const before = JSON.stringify(original);
    context.copyProvider(original.id);
    assert.equal($('#providerId').value, '');
    assert.equal($('#providerApiKey').value, '');
    assert.equal($('#providerKeyStatus').textContent, '未配置');
    assert.equal($('#providerName').value, '测试供应商1');
    assert.equal($('#providerTemperature').value, '0');
    assert.equal($('#providerSupportsImages').value, 'false');
    $('#providerName').value = original.name;
    $('#providerApiKey').value = original.api_key;
    await context.saveProvider(event);
    const saved = requests.find(row => row.url === '/api/providers').body;
    assert.ok(saved.id && saved.id !== original.id, '手工同名同Key也必须独立创建');
    for (const field of ['base_url', 'request_format', 'model', 'context_window', 'max_output_tokens', 'temperature', 'reasoning_effort']) {
      assert.equal(saved[field], original[field], field);
    }
    assert.equal(saved.supports_images, false);
    for (const field of ['model_key', 'is_default', 'pricing', 'context_window_probed', 'status', 'has_api_key']) {
      assert.equal(Object.hasOwn(saved, field), false, field);
    }
    assert.equal(JSON.stringify(original), before);
  },
  async names() {
    profiles.push({ name: '服务1' }, { name: '服务2' }, { name: '服务4' });
    assert.equal(context.nextProviderCopyName('服务'), '服务3');
    assert.equal(context.nextProviderCopyName('服务2'), '服务3');
    profiles.push({ name: '服务9007199254740991' }, { name: '服务9007199254740992' });
    assert.equal(context.nextProviderCopyName('服务9007199254740990'), '服务9007199254740993');
  },
  async local_and_probe() {
    profiles.push({ ...data(), kind: 'local', request_format: 'llama_cpp', local_backend: 'llama_cpp',
      service_manager: 'kvmem', service_manager_url: 'http://127.0.0.1:18199',
      context_window_source: 'local_probe' });
    context.copyProvider('original');
    assert.equal($('#providerContextWindow').value, '', '探测缓存不能固化成显式窗口');
    const values = context.providerFormValue();
    assert.equal(values.kind, 'local');
    assert.equal(values.local_backend, 'llama_cpp');
    assert.equal(values.service_manager, 'kvmem');
    assert.equal(values.service_manager_url, 'http://127.0.0.1:18199');
    assert.equal(values.api_key, '');
  },
  async late_secret() {
    profiles.push(data());
    const pending = deferred();
    context.api = () => pending.promise;
    context.showProviderForm(profiles[0]);
    const reveal = context.toggleProviderKey();
    context.copyProvider('original');
    pending.resolve({ api_key: 'source-secret-must-not-copy' });
    await reveal;
    assert.equal($('#providerApiKey').value, '');
    assert.equal($('#providerApiKey').type, 'password');
    assert.equal($('#providerKeyStatus').textContent, '未配置');
    assert.equal($('#providerId').value, '');
  },
  async cancelled_probe() {
    const original = data(); delete original.max_output_tokens; profiles.push(original);
    const pending = deferred();
    context.api = (url, options) => { requests.push({ url, body: options.body }); return pending.promise; };
    context.copyProvider('original');
    const saving = context.saveProvider(event);
    assert.equal(requests[0].url, '/api/providers/models');
    context.cancelProviderEdit();
    pending.resolve({ models: [{ id: original.model, max_output_tokens: 4096 }] });
    await saving;
    assert.equal(requests.length, 1, '预探测中取消不得继续创建副本');
    assert.equal(profiles.length, 1);
  },
  async late_save() {
    profiles.push(data(), { ...data(), id: 'second', name: '另一个源卡' });
    const pending = deferred();
    context.api = (url, options) => { requests.push({ url, body: options.body }); return pending.promise; };
    context.copyProvider('original');
    const saving = context.saveProvider(event);
    const submitted = { ...requests[0].body };
    context.cancelProviderEdit();
    context.copyProvider('second');
    $('#providerApiKey').value = 'second-draft-key';
    const title = $('#providerName').value;
    pending.resolve(submitted);
    await saving;
    assert.equal($('#providerDialog').open, true);
    assert.equal($('#providerName').value, title);
    assert.equal($('#providerId').value, '');
    assert.equal($('#providerApiKey').value, 'second-draft-key');
    assert.ok(profiles.some(profile => profile.id === submitted.id), '旧保存成功仍应同步列表');
  },
  async remote_fallback() {
    context.crypto = undefined;
    profiles.push(data());
    context.copyProvider('original');
    await context.saveProvider(event);
    context.copyProvider('original');
    await context.saveProvider(event);
    const ids = requests.filter(row => row.url === '/api/providers').map(row => row.body.id);
    assert.equal(ids.length, 2);
    assert.ok(ids.every(id => /^provider-copy-[a-z0-9]+-[a-z0-9]+$/.test(id)));
    assert.notEqual(ids[0], ids[1]);
    assert.ok(ids.every(id => id !== 'original'));
  },
};
Promise.resolve(cases[process.argv[1]]()).catch(error => { console.error(error); process.exitCode = 1; });
"""


@unittest.skipUnless(shutil.which("node"), "供应商复制行为测试需要 Node")
class ProviderCopyBehaviorTests(unittest.TestCase):
    def run_case(self, name):
        result = subprocess.run(
            ["node", "-e", NODE_DRIVER, name], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_copied_fields_keep_new_identity_and_do_not_mutate_source(self):
        self.run_case("fields_and_identity")

    def test_copy_names_skip_existing_and_support_large_suffixes(self):
        self.run_case("names")

    def test_local_connection_keeps_configuration_without_probe_cache(self):
        self.run_case("local_and_probe")

    def test_late_secret_cannot_populate_copy(self):
        self.run_case("late_secret")

    def test_cancelling_capability_probe_does_not_create_copy(self):
        self.run_case("cancelled_probe")

    def test_late_saved_copy_does_not_close_or_overwrite_new_form(self):
        self.run_case("late_save")

    def test_remote_http_without_random_uuid_gets_independent_copy_ids(self):
        self.run_case("remote_fallback")


if __name__ == "__main__":
    unittest.main()
