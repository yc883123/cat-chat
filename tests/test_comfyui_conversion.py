"""运行真正的 JS 请求构造器，验证 H3 模板选择及批量图片传递。"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ComfyuiConversionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 运行前端纯函数")
    def test_templates_images_and_material_boundaries(self):
        script = r"""
import assert from 'node:assert/strict';
import { buildBatchConvertRequest as build, BATCH_H3_REFERENCE_TEMPLATE as REF } from './public/js/24a-comfyui-convert.js';
const detail = {
  params: [
    { node_id: '1', field_name: 'image', media: 'image', label: '普通参考' },
    { node_id: '2', field_name: 'image', media: 'image', label: '尾帧 last_image',
      connections: [{ target_node_id: '20', target_field: 'end_frame' }] },
    { node_id: '3', field_name: 'video', field_type: 'video' },
    { node_id: '4', field_name: 'seconds', default: 8 },
  ],
  prompt: { '4': { inputs: { seconds: 9 } } },
};
const mapping = {
  media_ports: [
    { node_id: '2', field_name: 'image', checked: true },
    { node_id: '1', field_name: 'image', checked: false },
    { node_id: '1', field_name: 'image', checked: true },
  ],
  duration_slot: { node_id: '4', field_name: 'seconds' },
};
const item = { script: '尾帧落在图1的构图', duration: '6', files: {
  '0': { path: 'uploads/second.png', name: '第二张', thumb_path: 'uploads/second_thumb.webp' },
  '1': { path: 'uploads/first.png', name: '第一张' },
} };
const convert = { template: '', provider: 'online:test', model: 'local-vision' };
let body = build(detail, mapping, item, convert);
assert.equal(body.item_id, 'h3-007-h3-i2va');
assert.equal(body.custom_id, undefined);
assert.equal(body.model_key, convert.provider);
assert.equal(body.model_name, convert.model);
assert.deepEqual(body.images.map(image => image.path), ['uploads/second.png', 'uploads/first.png']);
assert.equal(body.images[0].thumb_path, 'uploads/second_thumb.webp');
assert.match(body.user_request, /目标时长：6 秒/);
assert.match(body.user_request, /尾帧落在图1/);
assert.match(body.user_request, /实际发送 2 张图片/);
assert.match(body.user_request, /图1：节点 2，字段 image，标签 尾帧 last_image，下游 20.end_frame/);
assert.match(body.user_request, /图2：节点 1，字段 image，标签 普通参考/);
assert.match(body.user_request, /用途未知时不得猜成首帧/);

body = build(detail, mapping, { ...item, duration: '' }, { ...convert, template: REF });
assert.equal(body.item_id, 'h3-014-h3-ref2va');
assert.match(body.user_request, /目标时长：9 秒/);
body = build(detail, mapping, { ...item, duration: '' }, convert, { '4|seconds': '10' });
assert.match(body.user_request, /目标时长：10 秒/);
body = build(detail, mapping, item, { ...convert, template: 'cu-custom' });
assert.equal(body.item_id, undefined);
assert.equal(body.custom_id, 'cu-custom');
assert.equal(body.images.length, 2);

const videoMapping = { media_ports: [{ node_id: '3', field_name: 'video', checked: true }] };
assert.throws(() => build(detail, videoMapping, item, { ...convert, template: REF }), /尚未发送视频或音频/);
body = build(detail, videoMapping, item, convert);
assert.equal(body.images, undefined);
assert.match(body.user_request, /视频和音频素材未发送/);
body = build(detail, { media_ports: [] }, item, convert);
assert.equal(body.images, undefined);
assert.match(body.user_request, /未提供关键帧/);

const tooMany = { media_ports: Array.from({ length: 7 }, () => ({ node_id: '1', field_name: 'image', checked: true })) };
const files = Object.fromEntries(Array.from({ length: 7 }, (_, index) => [String(index), { path: `uploads/${index}.png` }]));
assert.throws(() => build(detail, tooMany, { ...item, files }, convert), /最多分析 6 张图片/);
"""
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-"], input=script, cwd=ROOT,
            text=True, encoding="utf-8", capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
