"""书工作台改版：执行实际分栏函数，守住菜单、动作栏与原控件身份。"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def declaration(source, name):
    match = re.search(rf"^(?:async )?function {name}\(", source, re.M)
    if not match:
        raise AssertionError(f"缺少函数 {name}")
    following = re.search(r"^(?:export )?(?:async )?function \w+\(", source[match.end():], re.M)
    end = match.end() + following.start() if following else len(source)
    return source[match.start():end]


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.ids = {}

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if attrs.get("id"):
            self.ids.setdefault(attrs["id"], []).append((attrs, list(self.stack)))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class WorkbenchBookStructureTests(unittest.TestCase):
    def test_original_controls_are_unique_and_in_functional_menus(self):
        doc = Elements()
        doc.feed((ROOT / "public/index.html").read_text(encoding="utf-8"))
        for prefix, names in {
            "novel": ["BooksBtn", "ImportBtn", "ManageBtn", "ExportBtn", "StageGenerate"],
            "writer": ["BooksBtn", "ImportBtn", "NewBookBtn", "NovelContinueBtn", "ManageBtn", "ExportBtn", "SetupGenerate", "DraftGenerate"],
        }.items():
            for suffix in names:
                with self.subTest(control=prefix + suffix):
                    self.assertEqual(len(doc.ids.get(prefix + suffix, [])), 1)
            for suffix in ["BookMenu", "MoreMenu", "ExportMenu"]:
                attrs, _ = doc.ids[prefix + suffix][0]
                self.assertEqual(attrs["role"], "menu")
                self.assertIn("popover", attrs)
            _, ancestors = doc.ids[prefix + "Stages"][0]
            self.assertTrue(any(tag == "header" and "data-wb-page-header" in attrs for tag, attrs in ancestors))
            _, ancestors = doc.ids[prefix + "BooksBtn"][0]
            self.assertTrue(any(attrs.get("id") == prefix + "BookMenu" for _, attrs in ancestors))

    def test_responsive_visibility_has_container_contract(self):
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        css = css.split("/* wb-books:start */")[1].split("/* wb-books:end */")[0]
        self.assertIn('[data-wb-size="medium"]', css)
        self.assertIn('[data-wb-size="narrow"]', css)
        self.assertIn('[aria-hidden="true"] { display: none; }', css)
        self.assertIn(".wb-book-stage-body", css)
        self.assertIn(".wb-book-action-dock", css)


class WorkbenchBookRuntimeTests(unittest.TestCase):
    def run_node(self, script, data):
        node = shutil.which("node")
        if not node:
            self.skipTest("需要 Node 执行真实前端函数")
        result = subprocess.run([node, "-e", script], input=json.dumps(data), text=True,
                                encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_pane_functions_follow_container_not_window(self):
        functions = {}
        for prefix, file in [("novel", "25-novel.js"), ("writer", "27-writer.js")]:
            source = (ROOT / "public/js" / file).read_text(encoding="utf-8")
            functions[prefix] = "\n".join(declaration(source, name) for name in (
                "paneLayoutSize", "isPaneLayout", "setPane", "applyPaneAccessibility"))
        self.run_node(r"""
const assert=require('node:assert/strict'), vm=require('node:vm');
const sources=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
for (const prefix of ['novel','writer']) {
  const first=prefix==='novel'?'chapters':'book', last=prefix==='novel'?'preview':'notes';
  const names=[first,'editor',last], root={width:1200,dataset:{wbSize:'wide'}};
  function element(name) {
    const attributes={};
    return {dataset:{[prefix+'Pane']:name},attributes,
      classList:{contains:key=>key===prefix+'-'+name||key===prefix+'-pane-btn',toggle:()=>{}},
      setAttribute:(key,value)=>attributes[key]=value, removeAttribute:key=>delete attributes[key]};
  }
  const panels=Object.fromEntries(names.map(name=>[name,element(name)]));
  const buttons=names.map(element), aside=element('aside');
  const columns={dataset:{},querySelectorAll:()=>Object.values(panels)};
  const state={pane:'editor'};
  const ctx={console,[prefix]:state,workbenchWidth:el=>el.width,
    window:{innerWidth:1440,matchMedia:()=>({matches:false})},
    $:selector=>selector.endsWith('Columns')?columns:selector.endsWith('AsideBtn')?aside:root,
    document:{querySelectorAll:()=>buttons,querySelector:selector=>panels[names.find(name=>selector.endsWith('-'+name))]}};
  vm.createContext(ctx);vm.runInContext(sources[prefix],ctx);
  const hidden=name=>panels[name].attributes['aria-hidden']==='true';
  ctx.applyPaneAccessibility();assert.ok(names.every(name=>!hidden(name)));
  root.width=640;ctx.applyPaneAccessibility();assert.equal(ctx.isPaneLayout(),true);
  assert.equal(hidden(first),true);assert.equal(hidden('editor'),false);assert.equal(hidden(last),true);
  ctx.setPane(last);assert.equal(state.pane,last);assert.equal(hidden(last),false);assert.equal(hidden('editor'),true);
  root.width=840;ctx.setPane('editor');assert.equal(ctx.isPaneLayout(),false);
  assert.equal(hidden(first),false);assert.equal(hidden('editor'),false);assert.equal(hidden(last),true);
  ctx.setPane(last);assert.equal(hidden(last),false);assert.equal(aside.attributes['aria-expanded'],'true');
  root.width=1200;ctx.applyPaneAccessibility();assert.ok(names.every(name=>!hidden(name)));
  root.width=0;root.dataset.wbSize='medium';ctx.setPane('invalid');
  assert.equal(state.pane,'editor');assert.equal(hidden(last),true);
}
""", functions)

    def test_action_docking_moves_original_buttons_once(self):
        source = (ROOT / "public/js/27-writer.js").read_text(encoding="utf-8")
        self.run_node(r"""
const assert=require('node:assert/strict'),vm=require('node:vm');
const source=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
class Element {
  constructor(tag='div',id='') {this.tag=tag;this.id=id;this.children=[];this.dataset={};this.className='';this.attributes={};this.classList={add:name=>this.className+=' '+name};}
  append(...children){for(const child of children){if(child.parentElement)child.parentElement.children=child.parentElement.children.filter(x=>x!==child);child.parentElement=this;this.children.push(child);}}
  prepend(child){this.append(child);this.children.unshift(this.children.pop());}
  setAttribute(k,v){this.attributes[k]=v;}
  closest(selector){return this.className.split(' ').includes(selector.slice(1))?this:this.parentElement?.closest(selector);}
  querySelectorAll(tag){return this.children.flatMap(x=>[...(x.tag===tag?[x]:[]),...x.querySelectorAll(tag)]);}
}
const ids={},panels={},bindings=[];
const config={setup:['writerSetupGenerate','writerSetupSave'],outline:['writerOutlineGenerate','writerOutlineSave','writerOutlineReview'],chapter:['writerSplitStart','writerSplitContinue'],draft:['writerDraftGenerate','writerDraftSave','writerDraftContinue']};
for (const [stage,names] of Object.entries(config)) {
  const panel=panels[stage]=new Element(),group=new Element(),actions=new Element();actions.className='writer-action-bar';
  panel.append(group);group.append(actions);
  for(const name of names){const btn=ids[name]=new Element('button',name);btn.handler={identity:name};actions.append(btn);}
}
const detachedSaveGroup=new Element();panels.draft.append(detachedSaveGroup);detachedSaveGroup.append(ids.writerDraftSave);
const lookup=selector=>{const el=ids[selector.slice(1)];if(!el)return null;let root=el;while(root.parentElement)root=root.parentElement;return Object.values(panels).includes(root)?el:null};
const ctx={$:lookup,document:{createElement:tag=>new Element(tag),querySelector:selector=>panels[selector.match(/="(\w+)"/)[1]]},bindWorkbenchMenu:(button,menu)=>bindings.push({button,menu})};
vm.createContext(ctx);vm.runInContext(source,ctx);ctx.prepareEditorLayout();
for(const [stage,names] of Object.entries(config)) {
  const panel=panels[stage];assert.equal(panel.children[0].className,'wb-book-stage-body');
  assert.ok(panel.children[1].className.includes('wb-book-action-dock'));
  assert.equal(ids[names[0]].parentElement,panel.children[1]);
  assert.equal(panel.querySelectorAll('button').filter(b=>b===ids[names[0]]).length,1);
  assert.equal(ids[names[0]].handler.identity,names[0]);
}
assert.equal(bindings.length,2);ctx.prepareEditorLayout();assert.equal(bindings.length,2);
assert.equal(ids.writerDraftContinue.parentElement,bindings.find(b=>b.menu.id==='writerStageMenu-draft').menu);
assert.equal(ids.writerDraftSave.parentElement,panels.draft.children[1]);
""", declaration(source, "prepareEditorLayout"))
