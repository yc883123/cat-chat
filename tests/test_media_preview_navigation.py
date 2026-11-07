"""执行真实预览函数：批量分组、屏外占位、手势以及播放器释放。"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.dataset={};this.children=[];this.listeners=new Map();
    this.style={};this.hidden=false;this.className='';this.attributes={};this.pauseCalls=0;this.loadCalls=0;
    this.classList={add(){},remove(){},toggle(){}};}
  setAttribute(key,value){this.attributes[key]=String(value);if(key==='id')this.id=String(value);}
  getAttribute(key){if(key==='alt')return this.alt||'';if(key==='src')return this.src||'';
    if(key.startsWith('data-'))return this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]??null;
    return this.attributes[key]??null;}
  removeAttribute(key){delete this.attributes[key];if(key==='src')this.src='';}
  matches(selector){return selector.split(',').some(raw=>{const s=raw.trim();
    if(s.startsWith('#'))return this.id===s.slice(1);
    if(s.startsWith('.'))return this.className.split(/\s+/).includes(s.slice(1));
    const tag=s.match(/^[a-z]+/i)?.[0];if(tag&&this.tagName!==tag.toUpperCase())return false;
    const attrs=[...s.matchAll(/\[([^\]=]+)(?:=["']?([^\]"']*)["']?)?\]/g)];
    return Boolean(tag||attrs.length)&&attrs.every(([,key,value])=>key==='hidden'?this.hidden:
      this.getAttribute(key)!==null&&(value===undefined||this.getAttribute(key)===value));});}
  closest(selector){for(let item=this;item;item=item.parentElement)if(item.matches(selector))return item;return null;}
  append(...nodes){for(const node of nodes){node.remove();node.parentElement=this;this.children.push(node);}}
  prepend(node){node.remove();node.parentElement=this;this.children.unshift(node);}
  remove(){if(this.parentElement){const p=this.parentElement;p.children=p.children.filter(n=>n!==this);this.parentElement=null;}}
  querySelectorAll(selector){const rows=[];for(const child of this.children){if(child.matches(selector))rows.push(child);rows.push(...child.querySelectorAll(selector));}return rows;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  addEventListener(name,fn){const rows=this.listeners.get(name)||[];rows.push(fn);this.listeners.set(name,rows);}
  removeEventListener(name,fn){this.listeners.set(name,(this.listeners.get(name)||[]).filter(item=>item!==fn));}
  dispatch(name,event={}){event.target??=this;event.preventDefault??=()=>{};event.stopImmediatePropagation??=()=>{};
    for(const fn of [...(this.listeners.get(name)||[])])fn(event);}
  focus(){this.focused=true;}
  getClientRects(){return this.closest('[hidden]')?[]:[{}];}
  getBoundingClientRect(){return {left:0,top:0,width:600,height:400};}
  get isConnected(){return Boolean(this.closest('#document-body'));}
  pause(){this.pauseCalls++;}
  load(){this.loadCalls++;}
}
const body=new Element();body.id='document-body';
const document=new Element();document.body=body;document.createElement=tag=>new Element(tag);
document.querySelector=s=>body.matches(s)?body:body.querySelector(s);
document.querySelectorAll=s=>body.querySelectorAll(s);
const nodes={};for(const id of ['imageLightbox','imageLightboxImg','imageLightboxPrev','imageLightboxNext','imageLightboxCounter','imageLightboxDownload']){
  const n=new Element(id==='imageLightboxImg'?'img':'div');n.id=id;nodes[id]=n;body.append(n);}
nodes.imageLightbox.hidden=true;
const state={token:'test-token',bootstrap:{media_exts:{exts:{image:['.png','.jpg'],video:['.mp4'],audio:['.wav']}}}};
const messages=[],timers=[];const window={innerWidth:600,innerHeight:500,setTimeout:fn=>{timers.push(fn);return timers.length;}};
const context=vm.createContext({console,document,window,state,URL,Set,Map,
  $:s=>document.querySelector(s),toast:text=>messages.push(text),localFileUrl:path=>'/api/file?path='+encodeURIComponent(path),
  setTimeout:window.setTimeout,clearTimeout(){},setInterval(){},clearInterval(){}});
const clean=source=>source.replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');
vm.runInContext(clean(fs.readFileSync('public/js/03-media.js','utf8'))+`
globalThis.mediaApi={collectMediaPreviewItems,openImageLightbox,closeImageLightbox,stepImageLightbox,handleImageLightboxKey,initImageLightboxInteractions,mediaKind};`,context);
const api=context.mediaApi;
Object.assign(context,api,{attachmentThumbUrl:()=>'',saveLocalFile:()=>true});
vm.runInContext(clean(fs.readFileSync('public/js/24-comfyui.js','utf8'))+`
globalThis.previewApi={openMediaPreviewOverlay,renderOutputCard};`,context);
const preview=context.previewApi;
function scope(id){const element=new Element();element.id=id;body.append(element);return element;}
function output(parent,task,name,{placeholder=false,url=''}={}){
  const host=new Element();if(placeholder){host.dataset.batchPreviewTask=task;host.dataset.batchPreviewName=name;}
  parent.append(host);
  if(placeholder)return host;
  host.dataset.comfyuiPreviewKey=JSON.stringify([task,name]);host.dataset.comfyuiPreviewName=name;
  host.dataset.comfyuiPreviewKind=name.endsWith('.mp4')?'video':name.endsWith('.wav')?'audio':'image';
  host.dataset.comfyuiPreviewUrl=url||'/api/comfyui/tasks/'+task+'/files/'+name+'?token=test-token';
  const media=new Element(host.dataset.comfyuiPreviewKind==='image'?'img':host.dataset.comfyuiPreviewKind);
  if(media.tagName==='IMG'){media.dataset.largeUrl=host.dataset.comfyuiPreviewUrl;media.alt=name;}
  media.src=host.dataset.comfyuiPreviewUrl;host.append(media);return {host,media};
}
"""


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 验证真实预览逻辑")
class MediaPreviewNavigationTests(unittest.TestCase):
    def run_case(self, body: str) -> None:
        result = subprocess.run([shutil.which("node"), "-e", HARNESS + body], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_batch_uses_offscreen_placeholders_and_deduplicates_expanded_images(self):
        self.run_case(r"""
const batch=scope('comfyuiBatchBody');
output(batch,'task-a','a.png',{placeholder:true});
const first=output(batch,'task-a','a.png');
output(batch,'task-b','b.png',{placeholder:true});
output(batch,'task-c','c.png',{placeholder:true});
const expanded=output(batch,'task-a','a.png',{url:'/api/file?path=managed%2Fa.png'});
const hidden=new Element();hidden.hidden=true;batch.append(hidden);output(hidden,'task-hidden','hidden.png');
output(scope('messages'),'chat','chat.png');output(scope('comfyuiOutputGrid'),'single','single.png');
api.openImageLightbox(expanded.media.dataset.largeUrl,expanded.media);
assert.equal(nodes.imageLightboxCounter.textContent,'1 / 3');
assert.equal(nodes.imageLightboxImg.src,'/api/file?path=managed%2Fa.png');
assert.equal(nodes.imageLightboxPrev.hidden,false);
api.stepImageLightbox(1);assert.match(nodes.imageLightboxImg.src,/task-b\/files\/b.png/);
api.handleImageLightboxKey({key:'ArrowRight',preventDefault(){}});
assert.equal(nodes.imageLightboxCounter.textContent,'3 / 3');
assert.match(nodes.imageLightboxImg.src,/task-c\/files\/c.png/);
api.stepImageLightbox(1);assert.equal(nodes.imageLightboxCounter.textContent,'1 / 3');
api.closeImageLightbox();assert.equal(nodes.imageLightbox.hidden,true);
""")

    def test_pending_attachment_remains_single_image(self):
        self.run_case(r"""
const file=output(scope('pendingFiles'),'pending','pending.png');
output(scope('messages'),'chat','chat.png');
api.openImageLightbox(file.media.dataset.largeUrl,file.media);
assert.equal(nodes.imageLightboxPrev.hidden,true);assert.equal(api.stepImageLightbox(1),false);
""")

    def test_mobile_swipe_changes_image_but_zoomed_pan_does_not(self):
        self.run_case(r"""
const group=scope('comfyuiBatchBody'),a=output(group,'a','a.png');output(group,'b','b.png');
api.initImageLightboxInteractions();api.openImageLightbox(a.media.dataset.largeUrl,a.media);
const box=nodes.imageLightbox;
box.dispatch('touchstart',{touches:[{clientX:320,clientY:100}]});
box.dispatch('touchmove',{touches:[{clientX:80,clientY:110}]});
box.dispatch('touchend',{touches:[],changedTouches:[{clientX:80,clientY:110}]});
assert.equal(nodes.imageLightboxCounter.textContent,'2 / 2');
box.dispatch('wheel',{deltaY:-1,clientX:300,clientY:200});
box.dispatch('touchstart',{touches:[{clientX:320,clientY:100}]});
box.dispatch('touchmove',{touches:[{clientX:80,clientY:110}]});
box.dispatch('touchend',{touches:[],changedTouches:[{clientX:80,clientY:110}]});
assert.equal(nodes.imageLightboxCounter.textContent,'2 / 2','缩放后的平移不能翻图');
""")

    def test_video_overlay_navigation_releases_media_and_keeps_one_key_listener(self):
        self.run_case(r"""
const group=scope('comfyuiBatchBody'),a=output(group,'a','a.mp4');
output(group,'a','a.mp4',{placeholder:true});output(group,'b','b.mp4',{placeholder:true});
output(group,'image','image.png');output(group,'audio','audio.wav');
preview.openMediaPreviewOverlay(a.host.dataset.comfyuiPreviewUrl,'video',a.host);
let overlay=document.querySelector('.comfyui-media-preview-overlay');
assert.equal(overlay.querySelector('[data-media-preview-counter]').textContent.startsWith('1 / 2'),true);
const old=overlay.querySelector('video');
document.dispatch('keydown',{key:'ArrowRight'});
assert.equal(old.pauseCalls,1);assert.equal(old.src,'');assert.equal(old.loadCalls,1);
assert.match(overlay.querySelector('video').src,/b\/files\/b.mp4/);
const next=overlay.querySelector('video');document.dispatch('keydown',{key:'Escape'});
assert.equal(next.pauseCalls,1);assert.equal(next.src,'');assert.equal(document.querySelector('.comfyui-media-preview-overlay'),null);
assert.equal((document.listeners.get('keydown')||[]).length,0);
preview.openMediaPreviewOverlay(a.host.dataset.comfyuiPreviewUrl,'video',a.host);
const previousOverlay=document.querySelector('.comfyui-media-preview-overlay'),previousMedia=previousOverlay.querySelector('video');
preview.openMediaPreviewOverlay(a.host.dataset.comfyuiPreviewUrl,'video',a.host);
assert.equal(previousMedia.pauseCalls,1);assert.equal((document.listeners.get('keydown')||[]).length,1);
""")

    def test_output_video_has_explicit_preview_without_eager_loading(self):
        self.run_case(r"""
const group=scope('comfyuiOutputGrid');
const card=preview.renderOutputCard('task-video','movie.mp4','/managed/movie.mp4');group.append(card);
const video=card.querySelector('video');assert.equal(video.preload,'none');
const button=card.querySelector('[data-media-preview-open]');assert.equal(button.textContent,'放大预览');
button.dispatch('click',{stopPropagation(){}});assert.equal(video.pauseCalls,1);
assert.ok(document.querySelector('.comfyui-media-preview-overlay video')||document.querySelector('.comfyui-media-preview-overlay').querySelector('video'));
""")


if __name__ == "__main__":
    unittest.main()
