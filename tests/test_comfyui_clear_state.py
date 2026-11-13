"""执行真实前端函数，覆盖大范围清除、请求代次与状态回包归属。"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
class ClearAndKvmemStateTests(unittest.TestCase):
    def test_clear_chunking_missing_confirmation_and_navigation(self):
        script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('public/js/24-comfyui.js', 'utf8');
function extract(name) {
  const start = source.search(new RegExp('^(?:async )?function ' + name + '\\(', 'm'));
  assert.ok(start >= 0, name);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}
const functions = ['isTaskActive', 'isBatchTaskRow', 'clearableTaskIds', 'renderClearBar',
  'enterClearMode', 'exitClearMode', 'toggleClearSelectAll', 'onTaskClearToggle', 'deleteSelectedTasks'].map(extract).join('\n');
function setup(rows, api) {
  const context = { comfyui: { tasks: rows, deleting: new Set(), clearMode: false,
    clearSelection: new Set(), clearEpoch: 0, clearRequest: null },
    $: () => null, window: { confirm: () => true }, URLSearchParams, api,
    renderTasks() {}, toast() {}, refreshTasks: async () => {}, removed: [],
    forgetHistoryRows(_kind, ids) {
      this.removed.push(...ids);
      this.comfyui.tasks = this.comfyui.tasks.filter(row => !ids.includes(row.id));
    } };
  context.forgetHistoryRows = context.forgetHistoryRows.bind(context);
  vm.createContext(context); vm.runInContext(functions, context); return context;
}
(async () => {
  const rows = Array.from({ length: 450 }, (_, i) => ({ id: 'cf-' + i, status: 'completed' }));
  rows.push({ id: 'cf-active', status: 'running' }, { id: 'cf-batch', status: 'completed', batch_id: 'batch' });
  const store = new Map(rows.map(row => [row.id, row]));
  const calls = [];
  const s = setup(rows, async (url, options) => {
    if (options?.method === 'DELETE') {
      const ids = options.body.task_ids; calls.push([...ids]); assert.ok(ids.length <= 200);
      assert.equal(options.body.cancel_active, false); assert.equal(options.body.exclude_batch, true);
      for (const id of ids) if (!['cf-200','cf-201'].includes(id)) store.delete(id);
      return { deleted: ids.filter(id => !store.has(id)).length,
        skipped: ids.filter(id => id === 'cf-200'),
        failed: ids.filter(id => id === 'cf-201').map(id => ({ id, reason: '保留对账' })) };
    }
    const ids = new URL('http://fixture' + url).searchParams.get('ids').split(',');
    assert.ok(ids.length <= 100);
    return { tasks: ids.filter(id => store.has(id)).map(id => store.get(id)), missing_ids: ids.filter(id => !store.has(id)) };
  });
  s.enterClearMode(); s.toggleClearSelectAll();
  assert.equal(s.comfyui.clearSelection.size, 450);
  await s.deleteSelectedTasks();
  assert.deepEqual(calls.map(ids => ids.length), [200,200,50]);
  assert.equal(s.removed.length, 448);
  assert.deepEqual([...s.comfyui.clearSelection].sort(), ['cf-200','cf-201']);
  assert.ok(s.comfyui.tasks.some(row => row.id === 'cf-active'));
  assert.ok(s.comfyui.tasks.some(row => row.id === 'cf-batch'));

  const uncertain = setup([{ id: 'cf-a', status: 'completed' }], async (_url, options) => {
    if (options?.method === 'DELETE') return { deleted: 1, skipped: [], failed: [] };
    throw Error('lookup unavailable');
  });
  uncertain.enterClearMode(); uncertain.toggleClearSelectAll(); await uncertain.deleteSelectedTasks();
  assert.equal(uncertain.removed.length, 0, '未确认删除身份不能仅凭计数隐藏记录');
  assert.equal(uncertain.comfyui.clearSelection.size, 1);
  assert.equal(uncertain.comfyui.clearRequest, null);

  let resolve;
  const switching = setup([{ id: 'cf-a', status: 'completed' }, { id: 'cf-b', status: 'completed' }],
    (url, options) => options?.method === 'DELETE'
      ? new Promise(done => { resolve = done; }) : Promise.resolve({ missing_ids: ['cf-a'] }));
  switching.enterClearMode(); switching.comfyui.clearSelection.add('cf-a');
  const pending = switching.deleteSelectedTasks();
  const request = switching.comfyui.clearRequest;
  switching.exitClearMode(); switching.enterClearMode();
  assert.equal(switching.comfyui.clearRequest, request, '退出不能解除在途请求锁');
  switching.toggleClearSelectAll(); assert.equal(switching.comfyui.clearSelection.size, 0);
  switching.comfyui.clearSelection.add('cf-b');
  resolve({ deleted: 1, skipped: [], failed: [] }); await pending;
  assert.deepEqual([...switching.comfyui.clearSelection], ['cf-b'], '旧回包不覆盖新模式选择');
  assert.equal(switching.comfyui.clearMode, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_kvmem_old_response_and_error_cannot_overwrite_new_provider(self):
        script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('public/js/07-models-agents.js', 'utf8');
function extract(name) {
  const start = source.search(new RegExp('^(?:export )?(?:async )?function ' + name + '\\(', 'm'));
  return source.slice(start, source.indexOf('\n}', start) + 2).replace(/^export /, '');
}
const nodes = new Map(), pending = [];
const s = { kvmemState: { providerId: '', payload: null, busy: false, request: 0 }, current: { id: 'A' },
  currentKvmemProvider() { return s.current; }, kvmemLabel: value => value,
  $(key) { if (!nodes.has(key)) nodes.set(key, { dataset: {}, setAttribute() {} }); return nodes.get(key); },
  toast() { throw Error('迟到失败不得提示'); },
  api(url) { return new Promise((resolve, reject) => pending.push({ url, resolve, reject })); } };
vm.createContext(s); vm.runInContext(['renderKvmemControl','refreshKvmemStatus'].map(extract).join('\n'), s);
(async () => {
  const a = s.refreshKvmemStatus(); s.current = { id: 'B' }; const b = s.refreshKvmemStatus();
  assert.equal(s.kvmemState.payload, null, '切换后不展示旧状态');
  pending[1].resolve({ status: 'ready', model: 'B-model' }); await b;
  pending[0].resolve({ status: 'stopped', model: 'A-model' }); assert.equal(await a, null);
  assert.equal(s.kvmemState.payload.model, 'B-model'); assert.equal(nodes.get('#kvmemStart').disabled, true);
  const old = s.refreshKvmemStatus(); const fresh = s.refreshKvmemStatus();
  pending[3].resolve({ status: 'running', model: 'B-new' }); await fresh;
  pending[2].reject(Error('old error')); assert.equal(await old, null);
  assert.equal(s.kvmemState.payload.model, 'B-new');
  const leaving = s.refreshKvmemStatus(); s.current = null; s.renderKvmemControl();
  pending[4].resolve({ status: 'stopped', model: 'B-old' }); assert.equal(await leaving, null);
  assert.equal(s.kvmemState.payload, null); assert.equal(nodes.get('#kvmemControl').hidden, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
