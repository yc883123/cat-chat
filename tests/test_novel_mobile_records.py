"""执行真实前端函数，覆盖删除明细、跨作品回包和导出坏章阻断。"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NovelRecordFeedbackTests(unittest.TestCase):
    def test_real_frontend_delete_and_export_behaviour(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("需要 Node 执行实际前端函数")
        source = (ROOT / "public/js/25-novel.js").read_text(encoding="utf-8")

        def declaration(name):
            match = re.search(rf"^(?:async )?function {name}\(", source, re.M)
            self.assertIsNotNone(match, name)
            following = re.search(r"^(?:async )?function \w+\(", source[match.end():], re.M)
            end = match.end() + following.start() if following else len(source)
            return source[match.start():end]

        functions = "\n".join(declaration(name) for name in (
            "deleteManageSelection", "reconcileSelectedVersions", "exportBook",
        ))
        runner = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const functions = JSON.parse(fs.readFileSync(0, 'utf8'));
function fixture(keys = ['outline:o1', 'outline:o2']) {
  const calls = [], notices = [], error = {textContent:''};
  const novel = {bookId:'book-a', book:{revision:3, title:'测试作品'}, chapterNo:0,
    selectedVersions:{elements:0,outline:1,script:0}, manuscriptChoice:new Map(),
    expanded:new Set(),expandedTouched:new Set(),manuscriptBodies:new Map(),
    manage:{selected:new Set(keys),records:{manuscripts:[]},deleting:false,deleteFailures:new Map()}};
  const ctx = {novel, Map, Set, console,window:{confirm:()=>true},$ :()=>error,
    renderManage:()=>{},loadManageRecords:async()=>{calls.push('refresh')},
    refreshStageTasks:async()=>{},openChapter:async()=>{},renderAll:()=>{},
    toast:message=>notices.push(message),fail:(prefix,e)=>notices.push(prefix+':'+e.message),
    api:async()=>{throw Error('未设置接口')},setSaveState:(...args)=>calls.push(args),
    manuscriptsPayload:()=>({}),URLSearchParams,URL:{createObjectURL:()=>{throw Error('错误地创建下载')}},
    Blob:function(){throw Error('错误地创建文件')},document:{},setTimeout:()=>{}};
  vm.createContext(ctx);vm.runInContext(functions,ctx);
  return {ctx,novel,calls,notices,error};
}
(async()=>{
  let f=fixture();
  f.ctx.api=async()=>({book:{revision:4,outline_versions:[{version:2}]},
    succeeded:[{type:'outline',id:'o1'}],failed:[{type:'outline',id:'o2',error:'磁盘拒绝写入'}]});
  await f.ctx.deleteManageSelection();
  assert.deepEqual([...f.novel.manage.selected],['outline:o2']);
  assert.equal(f.novel.manage.deleteFailures.get('outline:o2'),'磁盘拒绝写入');
  assert.match(f.error.textContent,/部分成功 1 \/ 2/);
  assert.equal(f.novel.selectedVersions.outline,0);
  assert.equal(f.novel.manage.deleting,false);

  f=fixture();f.ctx.api=async()=>{throw Error('权限不足')};
  await f.ctx.deleteManageSelection();
  assert.equal(f.novel.manage.selected.size,2);
  assert.equal(f.novel.manage.deleteFailures.size,2);
  assert.match(f.error.textContent,/失败 0 \/ 2/);
  assert.ok(!f.notices.some(text=>text.includes('已删除 2')));

  f=fixture();let release;
  f.ctx.api=()=>new Promise(resolve=>release=resolve);
  let pending=f.ctx.deleteManageSelection();
  f.novel.bookId='book-b';const newerBook={revision:99};f.novel.book=newerBook;
  f.novel.manage.selected=new Set(['outline:newer']);
  release({book:{revision:4},succeeded:[{type:'outline',id:'o1'},{type:'outline',id:'o2'}]});
  await pending;
  assert.equal(f.novel.book,newerBook);
  assert.deepEqual([...f.novel.manage.selected],['outline:newer']);
  assert.equal(f.calls.length,0);

  f=fixture();let requests=0;
  f.ctx.api=()=>{requests++;return new Promise(resolve=>release=resolve)};
  pending=f.ctx.deleteManageSelection();await f.ctx.deleteManageSelection();
  assert.equal(requests,1);
  release({succeeded:[{type:'outline',id:'o1'},{type:'outline',id:'o2'}]});await pending;
  assert.equal(f.novel.manage.selected.size,0);

  f=fixture(['task:t1','task:t2']);f.ctx.api=async()=>({ok:true,removed:2});
  await f.ctx.deleteManageSelection();assert.equal(f.novel.manage.selected.size,0);
  assert.match(f.notices.at(-1),/已删除 2/);
  f=fixture(['task:t1','task:t2']);f.ctx.api=async()=>({succeeded:[{id:'t1'}],failed:[{id:'t2',error:'任务仍在运行'}]});
  await f.ctx.deleteManageSelection();assert.deepEqual([...f.novel.manage.selected],['task:t2']);
  assert.match(f.error.textContent,/部分成功 1 \/ 2/);

  f=fixture();f.ctx.api=async()=>({ok:true});
  await f.ctx.deleteManageSelection();assert.equal(f.novel.manage.selected.size,2);
  assert.match(f.error.textContent,/失败 0 \/ 2/);

  f=fixture();const broken=Error('作品章节预检失败');
  broken.payload={issues:[{chapter_no:158,reason:'正文损坏'},{chapter_no:163,reason:'稿件缺失'}]};
  f.ctx.api=async()=>{throw broken};await f.ctx.exportBook();
  assert.equal(f.calls[0][0],'导出已停止');
  assert.match(f.calls[0][1],/第 158 章：正文损坏\n第 163 章：稿件缺失/);

  f=fixture();f.novel.book={outline_versions:[{version:4}],script_versions:[]};
  f.novel.selectedVersions={elements:0,outline:4,script:2};f.ctx.reconcileSelectedVersions();
  assert.equal(f.novel.selectedVersions.outline,4);assert.equal(f.novel.selectedVersions.script,0);
  f=fixture(['outline:o1']);f.ctx.api=async()=>({succeeded:[{type:'outline',id:'o1',warning:'旧正文文件清理失败'}]});
  await f.ctx.deleteManageSelection();assert.equal(f.novel.manage.selected.size,0);
  assert.match(f.error.textContent,/记录已删除，清理提醒：\n旧正文文件清理失败/);
  f=fixture(['outline:o1']);f.novel.manuscriptChoice.set(158,'ms-keep');
  f.ctx.api=async()=>({succeeded:[{type:'outline',id:'o1'}]});
  f.ctx.loadManageRecords=async()=>{throw Error('刷新网络中断')};
  await f.ctx.deleteManageSelection();
  assert.equal(f.novel.manuscriptChoice.get(158),'ms-keep');
  assert.match(f.error.textContent,/已确认删除 1 \/ 1，刷新记录失败/);
  console.log('novel records: 11 behaviour cases passed');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result = subprocess.run(
            [node, "-e", runner], input=json.dumps(functions), capture_output=True,
            text=True, encoding="utf-8", cwd=ROOT, timeout=25,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("11 behaviour cases passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
