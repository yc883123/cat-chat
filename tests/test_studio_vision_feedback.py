"""实际执行识图前端函数，验证迟到回包隔离、来源摘要和并发输入保留。"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StudioVisionFeedbackTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行前端逻辑")
    def test_late_results_keep_request_context_and_user_edits(self):
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('public/js/22-studio.js', 'utf8');
const begin = source.indexOf('function studioImageKey(images)');
const end = source.indexOf('// 生成任务：提交 / 订阅 / 取消 / 重试', begin);
assert.ok(begin >= 0 && end > begin);
const section = source.slice(begin, end);
function fixture() {
  let resolve;
  const pending = new Promise(done => { resolve = done; });
  const messages = [], sent = [], focuses = [], panes = [];
  const input = { value: '原始输入', dispatchEvent(){}, focus(){ focuses.push(true); } };
  const nodes = new Map([
    ['#studioProviderSelect', { value: 'online:model-A' }],
    ['#studioModelSelect', { value: 'vision-A' }],
    ['#studioUserRequest', input],
  ]);
  const studio = {
    images: [{ path: 'uploads/a.png', name: '图片A' }], visionBusy: false, visionSummary: null,
    detailToken: 1, kind: 'builtin', itemId: 'vi-004-cmd-reverse', customId: '', page: 'library', open: true,
  };
  const context = vm.createContext({
    studio, $: selector => nodes.get(selector), Event: class Event {},
    api: async (url, options) => { sent.push(options.body); return pending; },
    renderImagePreview(){}, toast: text => messages.push(text),
    setStudioPane: pane => panes.push(pane), visionFailureDetails: () => '',
  });
  vm.runInContext(section, context);
  return { studio, nodes, input, messages, sent, focuses, panes, resolve,
    start: () => vm.runInContext('analyzeStudioImages()', context),
  };
}
const success = { text: '【图1】橘猫坐在窗边', analyzed: 1, failed: 0, fallback_used: 0 };
(async () => {
  const edit = fixture();
  const running = edit.start();
  edit.input.value = '等待期间新写的文字';
  edit.resolve(success);
  await running;
  assert.equal(edit.sent[0].model_name, 'vision-A');
  assert.ok(edit.input.value.startsWith('等待期间新写的文字\n\n'));
  assert.ok(edit.input.value.includes('橘猫坐在窗边'));
  assert.match(edit.studio.visionSummary.text, /发起模型 vision-A：1 张/);
  assert.doesNotMatch(edit.studio.visionSummary.text, /当前模型/);
  assert.equal(edit.studio.visionBusy, false);

  for (const [label, change, expected] of [
    ['模板令牌变化', state => { state.studio.detailToken += 1; }, /模板已切换/],
    ['模板身份变化', state => { state.studio.itemId = 'vi-111-i2t-enhance'; }, /模板已切换/],
    ['模型名称变化', state => { state.nodes.get('#studioModelSelect').value = 'vision-B'; }, /模型已更换/],
    ['供应商变化', state => { state.nodes.get('#studioProviderSelect').value = 'online:model-B'; }, /模型已更换/],
    ['图片变化', state => { state.studio.images[0].path = 'uploads/b.png'; }, /参考图片已更换/],
    ['离开功能页', state => { state.studio.page = 'comfyui'; }, null],
    ['关闭工作台', state => { state.studio.open = false; }, null],
  ]) {
    const state = fixture();
    const pending = state.start();
    change(state);
    state.input.value = `${label}后的新输入`;
    state.resolve(success);
    await pending;
    assert.equal(state.input.value, `${label}后的新输入`, label);
    assert.equal(state.studio.visionSummary, null, label);
    assert.equal(state.focuses.length, 0, label);
    assert.equal(state.panes.length, 0, label);
    assert.equal(state.studio.visionBusy, false, label);
    if (expected) assert.match(state.messages.at(-1), expected, label);
  }

  const fallback = fixture();
  fallback.studio.images.push({ path: 'uploads/b.png', name: '图片B' });
  const fallbackRequest = fallback.start();
  fallback.resolve({ text: '图1与图2的描述', analyzed: 2, failed: 0, fallback_used: 1 });
  await fallbackRequest;
  assert.match(fallback.studio.visionSummary.text, /发起模型 vision-A：1 张；视觉 API：1 张/);
  fallback.nodes.get('#studioModelSelect').value = 'vision-B';
  assert.match(fallback.studio.visionSummary.text, /vision-A/);
  assert.doesNotMatch(fallback.studio.visionSummary.text, /vision-B|当前模型/);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-"], input=script, cwd=ROOT, text=True,
            encoding="utf-8", capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
