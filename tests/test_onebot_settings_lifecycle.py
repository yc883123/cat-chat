"""执行 QQ 设置真实模块，覆盖首次失败恢复与跨打开周期的异步响应。"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class OnebotSettingsLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 执行前端模块")
    def test_recovery_binding_identity_and_stale_mutations(self):
        script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('public/js/30-onebot-settings.js', 'utf8')
  .replace(/^import .*\r?\n/gm, '').replace(/^export /gm, '');
const defer = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const tick = () => new Promise(resolve => setImmediate(resolve));
const config = { enabled: true, listen_host: '127.0.0.1', listen_port: 18899,
  path: '/saved/ws', has_access_token: true, allowed_user_ids: ['20001'],
  allow_groups: true, allowed_group_ids: ['30001'] };
const a = { channel_account_id: '10001', scope: 'private', qq_user_id: '20001', qq_group_id: '', conversation_id: 'ca' };
const b = { ...a, qq_user_id: '20002', conversation_id: 'cb' };
const status = (cfg = config, bindings = [a, b]) => ({ config: cfg, bindings, status: { enabled: cfg.enabled, listening: true } });
function harness(api) {
  const elements = new Map();
  const $ = key => {
    if (!elements.has(key)) elements.set(key, {
      value: '', checked: false, disabled: true, hidden: false, open: true, textContent: '', innerHTML: '',
      classList: { toggle() {} },
      replaceChildren(...options) { this.options = options; this.value = options[0]?.value || ''; },
      add(option) { this.options.push(option); },
    });
    return elements.get(key);
  };
  const ctx = { $, api, Option: function(label, value) { this.label = label; this.value = value; },
    state: { bootstrap: { model_profiles: [], agents: [], settings: {} } },
    escapeHtml: String, copyText: async () => {}, setTimeout: () => 1, clearTimeout() {} };
  vm.createContext(ctx); vm.runInContext(source, ctx);
  return { ctx, $ };
}
(async () => {
  // 首次 403/临时网络错误后，首次成功仍须回填配置与会话；之后轮询保留草稿。
  for (const code of [403, 500]) {
    let reads = 0, conversationReads = 0;
    const { ctx, $ } = harness(async path => {
      if (path === '/api/onebot/status') {
        if (++reads === 1) throw Object.assign(new Error('首次读取失败'), { status: code });
        return status();
      }
      conversationReads += 1;
      return { conversations: [{ id: 'ca', title: '已有会话' }] };
    });
    await ctx.openOnebotSettings();
    assert.equal($('#onebotConfigFields').disabled, true);
    await ctx.refreshStatus();
    assert.equal($('#onebotListenHost').value, '127.0.0.1');
    assert.equal($('#onebotListenPort').value, 18899);
    assert.equal($('#onebotEnabled').checked, true);
    assert.equal($('#onebotTokenStatus').textContent, '已配置');
    assert.equal($('#onebotAllowedUsers').value, '20001');
    assert.equal($('#onebotAllowedGroups').value, '30001');
    assert.equal(conversationReads, 1);
    assert.equal($('#onebotConversationId').options.length, 2);
    assert.equal($('#onebotConfigFields').disabled, false);
    assert.equal($('#onebotFeedback').textContent, '');
    $('#onebotAllowedUsers').value = '未保存的草稿';
    await ctx.refreshStatus();
    assert.equal($('#onebotAllowedUsers').value, '未保存的草稿');
  }

  // 会话列表单独失败不能让后续轮询永久放弃加载。
  {
    let conversations = 0;
    const { ctx, $ } = harness(async path => {
      if (path === '/api/onebot/status') return status();
      if (++conversations === 1) throw new Error('会话读取失败');
      return { conversations: [{ id: 'ca', title: '恢复的会话' }] };
    });
    await ctx.openOnebotSettings(); await ctx.refreshStatus();
    assert.equal(conversations, 2);
    assert.equal($('#onebotConversationId').options[1].value, 'ca');
  }

  // 刷新后的绑定数组与 DOM 同步；旧按钮身份不能误删新的同序号绑定。
  {
    const gate = defer(), reached = defer();
    let reads = 0, delay = false;
    const deleted = [];
    const { ctx, $ } = harness(async (path, options) => {
      if (path === '/api/onebot/status') return status(config, ++reads === 1 ? [a, b] : [b]);
      if (path === '/api/conversations' && delay) { reached.resolve(); return gate.promise; }
      if (path === '/api/onebot/unbind') { deleted.push(options.body); return { removed: true }; }
      return { conversations: [] };
    });
    await ctx.openOnebotSettings();
    const oldIdentity = ctx.bindingIdentity(a);
    delay = true;
    const pending = ctx.refreshStatus({ loadConversations: true });
    await reached.promise;
    assert.equal($('#onebotBindings').innerHTML.includes('QQ 20001'), false);
    assert.equal($('#onebotBindings').innerHTML.includes('QQ 20002'), true);
    await ctx.unbindConversation(oldIdentity);
    assert.equal(deleted.length, 0);
    await ctx.unbindConversation(ctx.bindingIdentity(b));
    assert.equal(deleted.length, 1);
    assert.equal(deleted[0].qq_user_id, '20002');
    gate.resolve({ conversations: [] }); await pending;
  }

  // 旧保存响应不能覆盖重新打开后读到的配置，也不能启用尚未授权的新表单。
  {
    const save = defer(), newStatus = defer();
    let reopening = false;
    const { ctx, $ } = harness(async path => {
      if (path === '/api/onebot/status') return reopening ? newStatus.promise : status();
      if (path === '/api/settings') return save.promise;
      return { conversations: [] };
    });
    await ctx.openOnebotSettings();
    $('#onebotAccessToken').value = 'secret-not-retained';
    const pending = ctx.saveConfig({ preventDefault() {} });
    ctx.closeOnebotSettings();
    assert.equal($('#onebotAccessToken').value, '');
    reopening = true;
    const reopened = ctx.openOnebotSettings();
    save.resolve({ settings: { onebot: { ...config, listen_port: 19999 } } });
    await pending;
    assert.equal($('#onebotConfigFields').disabled, true);
    assert.notEqual($('#onebotListenPort').value, 19999);
    newStatus.resolve(status({ ...config, listen_port: 17777 }));
    await reopened; await tick();
    assert.equal($('#onebotListenPort').value, 17777);
    assert.equal($('#onebotConfigFields').disabled, false);
    assert.notEqual($('#onebotFeedback').textContent, 'QQ 渠道设置已保存');
  }

  // 旧解绑完成后不能覆盖新页面反馈或绑定状态；旧 finally 不得越过新权限失败。
  {
    const action = defer();
    let reopened = false;
    const { ctx, $ } = harness(async path => {
      if (path === '/api/onebot/status') {
        if (reopened) throw Object.assign(new Error('当前不允许管理'), { status: 403 });
        return status();
      }
      if (path === '/api/onebot/unbind') return action.promise;
      return { conversations: [] };
    });
    await ctx.openOnebotSettings();
    const pending = ctx.unbindConversation(ctx.bindingIdentity(a));
    ctx.closeOnebotSettings(); reopened = true;
    await ctx.openOnebotSettings();
    action.resolve({ removed: true }); await pending; await tick();
    assert.equal($('#onebotConfigFields').disabled, true);
    assert.equal($('#onebotBindFields').disabled, true);
    assert.match($('#onebotFeedback').textContent, /当前不允许管理/);
    assert.equal($('#onebotFeedback').textContent.includes('已解除绑定'), false);
  }
})();
"""
        result = subprocess.run(
            [shutil.which("node"), "-"], input=script, cwd=ROOT,
            text=True, encoding="utf-8", capture_output=True, timeout=25,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
