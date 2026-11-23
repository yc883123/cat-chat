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

    def test_batch_clear_running_exclusion_deletion_and_epoch_lock(self):
        """批次档勾选式清除：在跑批次不可清、404 按已清除计、失败批次保留、在途请求锁。"""
        script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync('public/js/24-comfyui.js', 'utf8');
function extract(name) {
  const start = source.search(new RegExp('^(?:async )?function ' + name + '\\(', 'm'));
  assert.ok(start >= 0, name);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}
const functions = ['batchRowRunning', 'clearableBatchIds', 'renderBatchClearBar',
  'enterBatchClearMode', 'exitBatchClearMode', 'toggleBatchClearSelectAll',
  'onBatchClearToggle', 'deleteSelectedBatches'].map(extract).join('\n');
function setup(rows, api) {
  const context = { comfyui: { batchClearMode: false, batchClearSelection: new Set(),
      batchClearEpoch: 0, batchClearRequest: null },
    batch: { histories: rows, deletingBatches: new Set() },
    $: () => null, window: { confirm: () => true }, api, URLSearchParams, encodeURIComponent,
    renderBatchHistoryList() {}, renderBatchClearBar() {}, toast() {},
    loadBatchHistories: async () => {}, removed: [],
    forgetHistoryRows(kind, ids) {
      assert.equal(kind, 'batches');
      this.removed.push(...ids);
      this.batch.histories = this.batch.histories.filter(row => !ids.includes(row.id));
    } };
  context.forgetHistoryRows = context.forgetHistoryRows.bind(context);
  vm.createContext(context); vm.runInContext(functions, context); return context;
}
(async () => {
  // 在跑批次（running/queued 条目）与删除中的批次都不进可清除集合。
  const rows = [
    { id: 'wb-a', items: [{ status: 'completed' }, { status: 'running' }] },
    { id: 'wb-b', items: [{ status: 'completed' }] },
    { id: 'wb-c', items: [] },
    { id: 'wb-d', items: [{ status: 'queued' }] },
  ];
  const calls = [];
  const s = setup(rows, async (url, options) => {
    calls.push([url, options.method, options.body.delete_outputs]);
    assert.ok(options.method === 'DELETE' && options.body.delete_outputs === true);
    return { ok: true, deleted: 2, skipped: [], failed: [] };
  });
  s.batch.deletingBatches.add('wb-c');
  s.enterBatchClearMode();
  assert.equal(s.comfyui.batchClearMode, true);
  s.toggleBatchClearSelectAll();
  assert.deepEqual([...s.comfyui.batchClearSelection].sort(), ['wb-b']);
  await s.deleteSelectedBatches();
  assert.deepEqual(calls, [['/api/comfyui/batches/wb-b', 'DELETE', true]]);
  assert.deepEqual(s.removed, ['wb-b']);
  assert.equal(s.comfyui.batchClearMode, false, '清完可清批次自动退出清除模式');
  assert.equal(s.comfyui.batchClearRequest, null);

  // 404 按已清除计；409/失败批次保留在勾选集里等待重试。
  const rows2 = [{ id: 'wb-x', items: [] }, { id: 'wb-y', items: [] }, { id: 'wb-z', items: [] }];
  const s2 = setup(rows2, async (url) => {
    if (url.endsWith('wb-x')) { const e = new Error('批次不存在或已被删除'); e.status = 404; throw e; }
    if (url.endsWith('wb-y')) return { ok: true, deleted: 1, skipped: [], failed: [] };
    const e = new Error('有任务没能删掉，批次先保留'); e.status = 409; throw e;
  });
  s2.enterBatchClearMode(); s2.toggleBatchClearSelectAll();
  assert.deepEqual([...s2.comfyui.batchClearSelection].sort(), ['wb-x', 'wb-y', 'wb-z']);
  await s2.deleteSelectedBatches();
  assert.deepEqual(s2.removed.sort(), ['wb-x', 'wb-y']);
  assert.deepEqual([...s2.comfyui.batchClearSelection], ['wb-z']);
  assert.equal(s2.comfyui.batchClearMode, true, '还有可清批次时保持清除模式');

  // 全是在跑批次时进不了清除模式，提示讲清原因。
  let notice = '';
  const s3 = setup([{ id: 'wb-run', items: [{ status: 'running' }] }], async () => { throw new Error('not reached'); });
  s3.toast = (msg) => { notice = msg; };
  s3.enterBatchClearMode();
  assert.equal(s3.comfyui.batchClearMode, false);
  assert.ok(notice.includes('正在生成'));

  // epoch 锁与单次口径一致：退出/重进不解锁在途请求，旧回包不覆盖新模式。
  const s4 = setup([{ id: 'wb-a', items: [] }, { id: 'wb-b', items: [] }], async () => new Promise(() => {}));
  s4.enterBatchClearMode(); s4.comfyui.batchClearSelection.add('wb-a');
  const pending = s4.deleteSelectedBatches();
  const request = s4.comfyui.batchClearRequest;
  s4.exitBatchClearMode(); s4.enterBatchClearMode();
  assert.equal(s4.comfyui.batchClearRequest, request, '退出不能解除在途请求锁');
  s4.toggleBatchClearSelectAll(); assert.equal(s4.comfyui.batchClearSelection.size, 0);
  s4.comfyui.batchClearSelection.add('wb-b');
  assert.equal(s4.comfyui.batchClearMode, true);
  s4.exitBatchClearMode();
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
const s = { kvmemState: { providerId: '', payload: null, busy: false, request: 0, operation: null, operations: new Map() }, current: { id: 'A' },
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
