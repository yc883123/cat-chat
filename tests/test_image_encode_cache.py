# -*- coding: utf-8 -*-
"""图片编码记忆（进程内 LRU）守门。

背景（维护说明 §九.152）：`build_model_history` 每轮都要把历史里每张图重新
「读盘 → PIL 解码 → 缩到 1600px → JPEG 多档试压 → base64」。服务端的前缀/KV 缓存省不掉
这段**客户端**的活——真实出图 3.9MB PNG 实测单张中位 77ms，12 张一轮 ≈0.92s 的开口延迟。
编码是确定性的（同文件 ⇒ 同字节，正是前缀缓存成立的前提），所以按
(绝对路径, 文件大小, mtime_ns) 记一次即可。

保护对象：
1. **确实省掉了重复编码**：同一张图第二次调用不再进编码器；
2. **绝不发旧图**：文件被替换（同路径、内容变、mtime 变）必须重编码；
3. **返回值是副本**：调用方改结果不能污染缓存（否则会改变发给模型的字节）；
4. **上限有界且按 LRU 淘汰**：`image_encode_cache_mb`（默认 512MB，0 = 关闭）；
5. **字节不变**：开/关记忆产出的历史逐字节相同——这句承诺必须有守门；
6. **配置与接线**：默认值/校验/暴露、三个 build 调用点同口径传参、设置页可调且有说明。
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from naiba import config as config_mod  # noqa: E402
from naiba.core import history as history_mod  # noqa: E402
from naiba.core.history import (  # noqa: E402
    IMAGE_ENCODE_CACHE_MB_DEFAULT,
    IMAGE_ENCODE_CACHE_MB_MAX,
    build_model_history,
    encode_image_for_model,
    image_encode_cache_stats,
    set_image_encode_cache_limit_mb,
)

ROOT = Path(__file__).resolve().parents[1]


def _png(path: Path, color: tuple[int, int, int], size: int = 64) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="PNG")
    path.write_bytes(buffer.getvalue())


class _EncodedCallCounter:
    """数「进过编码器几次」（缓存命中就不该进）。"""

    def __init__(self) -> None:
        self.count = 0

    def __enter__(self):
        self._real = history_mod._jpeg_for_model
        counter = self

        def spy(image, target_bytes=history_mod.MODEL_IMAGE_TARGET_BYTES):
            counter.count += 1
            return counter._real(image, target_bytes)

        self._patch = mock.patch.object(history_mod, "_jpeg_for_model", spy)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


class CacheHitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        set_image_encode_cache_limit_mb(IMAGE_ENCODE_CACHE_MB_DEFAULT)
        history_mod._IMAGE_ENCODE_CACHE.clear()
        self.addCleanup(set_image_encode_cache_limit_mb, IMAGE_ENCODE_CACHE_MB_DEFAULT)

    def test_second_call_does_not_reencode(self) -> None:
        path = self.dir / "a.png"
        _png(path, (10, 20, 30))
        with _EncodedCallCounter() as counter:
            first = encode_image_for_model(str(path))
            second = encode_image_for_model(str(path))
            third = encode_image_for_model(str(path))
        self.assertIsNotNone(first)
        self.assertEqual(counter.count, 1, "同一张图只该进一次编码器")
        self.assertEqual(first, second)
        self.assertEqual(first, third)

    def test_replaced_file_is_reencoded_and_never_serves_stale_bytes(self) -> None:
        path = self.dir / "a.png"
        _png(path, (255, 0, 0))
        first = encode_image_for_model(str(path))
        _png(path, (0, 0, 255), size=128)  # 同路径、内容与大小都变
        with _EncodedCallCounter() as counter:
            second = encode_image_for_model(str(path))
        self.assertEqual(counter.count, 1, "文件变了必须重编码")
        self.assertNotEqual(first["data"], second["data"], "绝不能把旧图的字节发出去")

    def test_touched_file_is_reencoded(self) -> None:
        """mtime 变（内容没变）也重编码：宁可多算一次，也不冒"发旧图"的风险。"""
        path = self.dir / "a.png"
        _png(path, (7, 7, 7))
        encode_image_for_model(str(path))
        stat = path.stat()
        import os

        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        with _EncodedCallCounter() as counter:
            encode_image_for_model(str(path))
        self.assertEqual(counter.count, 1)

    def test_returned_part_is_a_copy(self) -> None:
        """调用方（历史构建/视觉装载）会用这个 dict，改了不能污染缓存——**命中路径也要给副本**。

        注意两条路径都要测：未命中那次返回的是刚构造的对象（`put` 自己存副本即可挡住），
        而命中那次若直接返回缓存本体，调用方一改就改变了**之后每一轮发给模型的字节**。
        （本用例第一版只测了未命中路径，变异核对"get 返回本体"时没红，才发现测错了路径。）
        """
        path = self.dir / "a.png"
        _png(path, (1, 2, 3))
        first = encode_image_for_model(str(path))       # 未命中
        first["data"] = "改坏未命中那次"
        second = encode_image_for_model(str(path))      # 命中
        original = second["data"]
        second["data"] = "改坏命中那次"
        second["name"] = "改了"
        third = encode_image_for_model(str(path))       # 再命中
        self.assertEqual(third["data"], original, "命中路径返回的必须是副本")
        self.assertEqual(third["name"], "a.png")
        self.assertNotEqual(third["data"], "改坏未命中那次")

    def test_stats_track_hits_and_misses(self) -> None:
        path = self.dir / "a.png"
        _png(path, (4, 5, 6))
        encode_image_for_model(str(path))
        encode_image_for_model(str(path))
        stats = image_encode_cache_stats()
        self.assertEqual(stats["entries"], 1)
        self.assertGreater(stats["bytes"], 0)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)


class BudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.addCleanup(set_image_encode_cache_limit_mb, IMAGE_ENCODE_CACHE_MB_DEFAULT)

    def test_disabled_means_always_reencode(self) -> None:
        set_image_encode_cache_limit_mb(0)
        path = self.dir / "a.png"
        _png(path, (9, 9, 9))
        with _EncodedCallCounter() as counter:
            encode_image_for_model(str(path))
            encode_image_for_model(str(path))
        self.assertEqual(counter.count, 2, "0 = 关闭记忆，每轮照旧重编码")
        self.assertEqual(image_encode_cache_stats()["entries"], 0)

    def test_limit_evicts_oldest_and_keeps_recent(self) -> None:
        """预算 1MB 放 3 条 400KB 的条目 ⇒ 最旧的被淘汰（用缓存本体直接测，不依赖图片大小）。"""
        cache = history_mod._ImageEncodeCache()
        cache.set_limit_mb(1)
        payload = {"type": "image", "media_type": "image/jpeg", "data": "x" * 400_000, "name": "a"}
        cache.put(("k1", 1, 1), payload)
        cache.put(("k2", 1, 1), payload)
        cache.put(("k3", 1, 1), payload)
        stats = cache.stats()
        self.assertLessEqual(stats["bytes"], stats["limit_bytes"], "缓存字节不得超预算")
        self.assertEqual(stats["entries"], 2, "1MB 只放得下 2 条")
        self.assertIsNone(cache.get(("k1", 1, 1)), "最旧的（k1）必须被淘汰")
        self.assertIsNotNone(cache.get(("k3", 1, 1)), "最新的要留着")

    def test_hit_refreshes_lru_order(self) -> None:
        cache = history_mod._ImageEncodeCache()
        cache.set_limit_mb(1)
        payload = {"type": "image", "media_type": "image/jpeg", "data": "x" * 400_000, "name": "a"}
        cache.put(("k1", 1, 1), payload)
        cache.put(("k2", 1, 1), payload)
        cache.get(("k1", 1, 1))          # k1 变成最近使用
        cache.put(("k3", 1, 1), payload)  # 触发淘汰
        self.assertIsNotNone(cache.get(("k1", 1, 1)), "命中过的条目不该被先淘汰")
        self.assertIsNone(cache.get(("k2", 1, 1)), "该淘汰的是最久未用的 k2")

    def test_real_images_stay_within_budget(self) -> None:
        set_image_encode_cache_limit_mb(1)
        history_mod._IMAGE_ENCODE_CACHE.clear()
        for index in range(4):
            path = self.dir / f"big{index}.png"
            _png(path, (index * 40, 90, 20), size=512)
            encode_image_for_model(str(path))
            stats = image_encode_cache_stats()
            self.assertLessEqual(stats["bytes"], stats["limit_bytes"], "真实图片路径同样不得超预算")

    def test_limit_is_clamped(self) -> None:
        set_image_encode_cache_limit_mb(IMAGE_ENCODE_CACHE_MB_MAX + 10000)
        self.assertEqual(image_encode_cache_stats()["limit_bytes"], IMAGE_ENCODE_CACHE_MB_MAX * 1024 * 1024)
        set_image_encode_cache_limit_mb(-5)
        self.assertEqual(image_encode_cache_stats()["limit_bytes"], 0, "负数按关闭处理")
        set_image_encode_cache_limit_mb("不是数字")
        self.assertEqual(image_encode_cache_stats()["limit_bytes"], IMAGE_ENCODE_CACHE_MB_DEFAULT * 1024 * 1024)


class ByteStabilityTests(unittest.TestCase):
    """开/关记忆产出的历史必须**逐字节相同**——这是"只影响速度"的书面承诺。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.addCleanup(set_image_encode_cache_limit_mb, IMAGE_ENCODE_CACHE_MB_DEFAULT)

    def test_history_bytes_identical_with_and_without_cache(self) -> None:
        paths = []
        for index in range(3):
            path = self.dir / f"shot{index}.png"
            _png(path, (index * 60, 30, 90), size=96)
            paths.append(str(path))
        messages = [{
            "id": "m1", "role": "user", "content": "看这三张",
            "metadata": {"attachments": [{"path": p, "name": Path(p).name} for p in paths]},
        }]
        set_image_encode_cache_limit_mb(0)
        history_mod._IMAGE_ENCODE_CACHE.clear()
        without = build_model_history(messages, image_encode_cache_mb=0)
        with_cache = build_model_history(messages, image_encode_cache_mb=64)
        self.assertEqual(
            json.dumps(without, ensure_ascii=False, sort_keys=True),
            json.dumps(with_cache, ensure_ascii=False, sort_keys=True),
            "记忆只能改变编码次数，不能改变发给模型的字节",
        )
        # 第二次（命中）也必须完全一致
        again = build_model_history(messages, image_encode_cache_mb=64)
        self.assertEqual(
            json.dumps(with_cache, ensure_ascii=False, sort_keys=True),
            json.dumps(again, ensure_ascii=False, sort_keys=True),
        )

    def test_build_model_history_configures_the_cache(self) -> None:
        """`build_model_history` 是该缓存的**唯一写入点**（三个调用点都经它）。"""
        build_model_history([], image_encode_cache_mb=128)
        self.assertEqual(image_encode_cache_stats()["limit_bytes"], 128 * 1024 * 1024)


class ConfigContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        from server import ConfigStore

        self.store = ConfigStore(self.path)

    def test_default_is_exposed_in_public_settings(self) -> None:
        self.assertEqual(self.store.data["image_encode_cache_mb"], IMAGE_ENCODE_CACHE_MB_DEFAULT)
        self.assertEqual(self.store.public()["image_encode_cache_mb"], IMAGE_ENCODE_CACHE_MB_DEFAULT)

    def test_persists_and_survives_restart(self) -> None:
        self.store.update_settings({"image_encode_cache_mb": 1024})
        self.assertEqual(self.store.data["image_encode_cache_mb"], 1024)
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["image_encode_cache_mb"], 1024
        )
        from server import ConfigStore

        self.assertEqual(ConfigStore(self.path).data["image_encode_cache_mb"], 1024)

    def test_zero_and_empty_and_bad_values(self) -> None:
        self.store.update_settings({"image_encode_cache_mb": 0})
        self.assertEqual(self.store.data["image_encode_cache_mb"], 0, "0 合法 = 关闭记忆")
        self.store.update_settings({"image_encode_cache_mb": ""})
        self.assertEqual(
            self.store.data["image_encode_cache_mb"], IMAGE_ENCODE_CACHE_MB_DEFAULT, "留空回落默认"
        )
        for bad in (IMAGE_ENCODE_CACHE_MB_MAX + 1, -1, "abc", 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    self.store.update_settings({"image_encode_cache_mb": bad})
                self.assertIn("图片编码记忆", str(ctx.exception))

    def test_options_helper_normalizes(self) -> None:
        self.store.update_settings({"image_encode_cache_mb": 256})
        self.assertEqual(self.store.image_encode_cache_options(), {"image_encode_cache_mb": 256})
        self.store.data["image_encode_cache_mb"] = "脏值"
        self.assertEqual(
            self.store.image_encode_cache_options(),
            {"image_encode_cache_mb": IMAGE_ENCODE_CACHE_MB_DEFAULT},
        )

    def test_defaults_match_history_module(self) -> None:
        self.assertEqual(config_mod.IMAGE_ENCODE_CACHE_MB_DEFAULT, IMAGE_ENCODE_CACHE_MB_DEFAULT)
        self.assertEqual(config_mod.IMAGE_ENCODE_CACHE_MB_MAX, IMAGE_ENCODE_CACHE_MB_MAX)


class WiringTests(unittest.TestCase):
    """接线级守门：三个调用点同口径传参 + 设置页可调且有说明。"""

    def test_three_build_call_sites_pass_the_option(self) -> None:
        for rel in ("naiba/run/chat.py", "naiba/subagent.py", "naiba/plans.py"):
            with self.subTest(file=rel):
                source = (ROOT / rel).read_text(encoding="utf-8")
                self.assertIn(
                    "image_encode_cache_options()",
                    source,
                    f"{rel} 必须传图片编码记忆口径（与思考回放同一纪律）",
                )
                self.assertIn("reasoning_replay_options()", source)

    def test_settings_page_exposes_the_field_with_a_purpose_hint(self) -> None:
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="imageEncodeCacheMb"', html, "运行设置里必须有这一项")
        start = html.index('id="imageEncodeCacheMb"')
        block = html[start - 200:start + 900]
        self.assertIn("图片编码记忆上限", block)
        self.assertIn("0 = 关闭记忆", block)
        self.assertIn("不影响发给模型的字节", block, "必须说清它不会打断前缀缓存")

    def test_settings_page_loads_and_saves_the_field(self) -> None:
        js = (ROOT / "public" / "js" / "09-settings.js").read_text(encoding="utf-8")
        self.assertIn("$('#imageEncodeCacheMb').value = Number(settings.image_encode_cache_mb ?? 512)", js)
        self.assertIn("image_encode_cache_mb: imageCacheRaw === '' ? 512 : Number(imageCacheRaw)", js)
        self.assertIn("const imageCacheRaw", js)


if __name__ == "__main__":
    unittest.main()
