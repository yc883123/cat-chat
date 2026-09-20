# -*- coding: utf-8 -*-
"""发布核验器 `verify/release_watch.py::check_manifest` 的离线守门。

为什么要这条护栏：显示名 2026-09-20 起改为 **Cat Chat**（仓库、EXE、更新资产仍叫 naiba 系），
而发布核验器原本把「头条必须以 `Naiba Chat` 开头」写死——首次以 Cat Chat 发布时，**核验器会把
一次完全正常的发布判成异常**（`_watch_275.py` 那类临时脚本同理，所以判据要收进版本控制的测试里）。

改法是**支持两个已知品牌前缀**：当前显示名 `Cat Chat` 与历史名 `Naiba Chat` 都算正常。
这里同时钉住反方向——核验不能因此变松：非法前缀、空说明、缺字段、类型不对一律必须判失败，
否则「头条写错了」这类真事故会被静默放过。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_release_watch():
    """按路径加载 verify/release_watch.py（verify/ 不是包，不能 import verify.release_watch）。"""
    path = ROOT / "verify" / "release_watch.py"
    spec = importlib.util.spec_from_file_location("verify_release_watch_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rw = _load_release_watch()


def _manifest(**overrides) -> dict:
    base = {
        "repository": "yc883123/naiba-chat",
        "version": "2.7.6-beta",
        "commit": "a" * 40,
        "asset": "naiba-chat.exe",
        "sha256": "b" * 64,
        "release_notes": ["Cat Chat 2.7.6 Beta：显示名改为 Cat Chat（原 Naiba Chat）"],
    }
    base.update(overrides)
    return base


class CheckManifestBrandTest(unittest.TestCase):
    """头条品牌前缀：当前名与历史名都放行，其余一律拦下。"""

    def setUp(self) -> None:
        self.output = io.StringIO()

    def _check(self, manifest: dict, commit: str = "") -> bool:
        with contextlib.redirect_stdout(self.output):
            return rw.check_manifest(manifest, commit)

    def test_brand_prefixes_are_the_two_known_names(self) -> None:
        self.assertEqual(("Cat Chat", "Naiba Chat"), rw.BRAND_PREFIXES)

    def test_cat_chat_head_note_accepted(self) -> None:
        self.assertTrue(self._check(_manifest()))

    def test_historical_naiba_chat_head_note_accepted(self) -> None:
        """历史版本条目（改名前的发布）仍要能核验。"""
        notes = ["Naiba Chat 2.7.5 Beta：新增供应商预设与首启引导",
                 "Naiba Chat 2.7.3 Beta：手机端四连修"]
        self.assertTrue(self._check(_manifest(release_notes=notes)))

    def test_brand_only_needs_to_prefix_the_head_note(self) -> None:
        """第二条及以后是历史条目，品牌不参与判定——判定只看头条。"""
        notes = ["Cat Chat 2.7.6 Beta：显示名改名", "Naiba Chat 2.7.5 Beta：旧条目"]
        self.assertTrue(self._check(_manifest(release_notes=notes)))

    def test_unknown_brand_prefix_rejected(self) -> None:
        for head in ("NaibaChat 2.7.6 Beta：…", "CatChat 2.7.6 Beta：…",
                     "Chat Cat 2.7.6 Beta：…", "更新说明：…"):
            with self.subTest(head=head):
                self.output.seek(0)
                self.output.truncate()
                self.assertFalse(self._check(_manifest(release_notes=[head])))
                self.assertIn("release_notes 头条异常", self.output.getvalue())

    def test_brand_in_the_middle_is_not_enough(self) -> None:
        """判据是「前缀 ∈ 已知品牌」，不是「含品牌字样」。"""
        head = "某站转载 Naiba Chat 2.7.5 Beta 的说明"
        self.assertFalse(self._check(_manifest(release_notes=[head])))

    def test_empty_note_rejected(self) -> None:
        self.assertFalse(self._check(_manifest(release_notes=[""])))

    def test_empty_note_list_rejected(self) -> None:
        """空列表以前会抛 IndexError（把「问不到」变成崩溃），必须给出明确失败。"""
        self.assertFalse(self._check(_manifest(release_notes=[])))
        self.assertIn("release_notes 缺失或为空", self.output.getvalue())

    def test_missing_notes_key_rejected_without_crash(self) -> None:
        manifest = _manifest()
        del manifest["release_notes"]
        self.assertFalse(self._check(manifest))

    def test_wrong_types_rejected(self) -> None:
        for notes in ("Cat Chat 2.7.6 Beta：说明", {"a": 1}, [1, 2], [None]):
            with self.subTest(notes=notes):
                self.assertFalse(self._check(_manifest(release_notes=notes)))

    def test_missing_version_and_sha_do_not_crash(self) -> None:
        """发布事故也可能表现为字段缺失，报错要可读、不要抛异常。"""
        manifest = _manifest()
        del manifest["version"]
        del manifest["sha256"]
        self.assertTrue(self._check(manifest))


class CheckManifestCommitTest(unittest.TestCase):
    def test_commit_mismatch_rejected(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            ok = rw.check_manifest(_manifest(commit="c" * 40), "d" * 40)
        self.assertFalse(ok)

    def test_commit_match_accepted(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            ok = rw.check_manifest(_manifest(commit="c" * 40), "c" * 40)
        self.assertTrue(ok)

    def test_commit_omitted_skips_the_check(self) -> None:
        """不带 sha 时只核 manifest 自身（静态口核验会带，网页/手工核对常常不带）。"""
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(rw.check_manifest(_manifest(commit="c" * 40)))


class _StdoutShim(io.StringIO):
    """`_release_check.py` 顶层会调 `sys.stdout.reconfigure(encoding=…)`，
    而被 `redirect_stdout` 换掉的 StringIO 没有这个方法——不补一下，加载即 AttributeError。"""

    def reconfigure(self, **_kwargs) -> None:  # noqa: D102 - 只为吞掉调用
        pass


class ReleaseCheckBrandTest(unittest.TestCase):
    """`verify/_release_check.py` 的品牌口径也要能跟着显示名走（它是发版前的同步自检）。"""

    @classmethod
    def setUpClass(cls) -> None:
        path = ROOT / "verify" / "_release_check.py"
        spec = importlib.util.spec_from_file_location("verify_release_check_under_test", path)
        module = importlib.util.module_from_spec(spec)
        # 该脚本顶层就是一段自检并直接 print，跑一遍即可（它只读仓库文件）。
        with contextlib.redirect_stdout(_StdoutShim()):
            spec.loader.exec_module(module)
        cls.module = module

    def test_brand_hit_accepts_both_names(self) -> None:
        self.assertEqual("Cat Chat", self.module.brand_hit("# Cat Chat 2.7.6 Beta", "# {brand} 2.7.6 Beta"))
        self.assertEqual("Naiba Chat", self.module.brand_hit("# Naiba Chat 2.7.5 Beta", "# {brand} 2.7.5 Beta"))

    def test_brand_hit_rejects_partial_or_missing(self) -> None:
        for text in ("# CatChat 2.7.6 Beta", "# 说明 2.7.6 Beta", "", "# Cat Chat"):
            with self.subTest(text=text):
                self.assertEqual("", self.module.brand_hit(text, "# {brand} 2.7.6 Beta"))


if __name__ == "__main__":
    unittest.main()
