# -*- coding: utf-8 -*-
"""发布核验器 `verify/release_watch.py` 的离线守门。

为什么要这条护栏：显示名 2026-09-20 起改为 **Cat Chat**，仓库名 2026-09-21 起改为 `cat-chat`
（EXE 名、清单名与清单里的 `repository` 则作为**协议常量**永久保留 `naiba` 系），而发布核验器原本
把「头条必须以 `Naiba Chat` 开头」写死——首次以 Cat Chat 发布时，**核验器会把一次完全正常的发布
判成异常**（`_watch_275.py` 那类临时脚本同理，所以判据要收进版本控制的测试里）。

这里钉住三组判据：① 头条品牌前缀支持两个已知名；② 清单 `repository` 恒为旧值（与核验器自己的
`REPO` 刻意不同名）；③ 5 项资产齐全（漏 `naiba-chat.exe` 会让全部旧客户端断更）。
反方向同样钉住——核验不能因此变松：非法前缀、空说明、缺字段、类型不对、缺资产一律必须判失败，
否则「头条写错了」「漏传了旧名 exe」这类真事故会被静默放过。
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


class ManifestRepositoryConstantTest(unittest.TestCase):
    """清单 `repository` 是协议常量：恒为旧值，**与核验器自己的仓库名刻意不同**。

    仓库 2026-09-21 更名为 `cat-chat`，但旧客户端逐字比对清单里的 `repository`——改值即让全部
    历史版本用户永久失去自动更新。这里钉住的正是「别顺手把两个名字对齐」。
    """

    def setUp(self) -> None:
        self.output = io.StringIO()

    def _check(self, manifest: dict) -> bool:
        with contextlib.redirect_stdout(self.output):
            return rw.check_manifest(manifest)

    def test_repo_constant_is_the_new_name_but_manifest_stays_old(self) -> None:
        self.assertEqual("yc883123/cat-chat", rw.REPO)
        self.assertEqual("yc883123/naiba-chat", rw.MANIFEST_REPOSITORY)
        self.assertNotEqual(rw.REPO, rw.MANIFEST_REPOSITORY,
                            "两个名字必须不同：一个是门面，一个是协议常量")

    def test_old_value_accepted(self) -> None:
        self.assertTrue(self._check(_manifest()))

    def test_new_value_rejected(self) -> None:
        """把 repository 改成新仓库名是最典型的「顺手对齐」事故，必须判失败。"""
        self.assertFalse(self._check(_manifest(repository="yc883123/cat-chat")))
        self.assertIn("清单 repository 必须恒为", self.output.getvalue())

    def test_missing_or_wrong_type_rejected(self) -> None:
        manifest = _manifest()
        del manifest["repository"]
        self.assertFalse(self._check(manifest))
        self.assertFalse(self._check(_manifest(repository=None)))


class AssetSetContractTest(unittest.TestCase):
    """5 项资产硬校验：漏 `naiba-chat.exe` 是最致命的一类发布事故。

    原实现直接 `assets["naiba-chat.exe"]`——缺资产时抛 `KeyError`，等于把「判失败」变成
    「崩给你看」，而且缺项连一句可读结论都不留。
    """

    FULL = {
        "naiba-chat.exe": {"size": 1},
        "cat-chat.exe": {"size": 1},
        "cat-chat-2.7.7-beta-windows-x64.zip": {"size": 1},
        "naiba-chat-2.7.7-beta-windows-x64.zip": {"size": 1},
        "naiba-chat-update.json": {"size": 1},
    }

    def setUp(self) -> None:
        self.output = io.StringIO()

    def _check(self, assets: dict) -> bool:
        with contextlib.redirect_stdout(self.output):
            return rw.check_asset_set(assets)

    def _without(self, *names: str) -> dict:
        return {k: v for k, v in self.FULL.items() if k not in names}

    def test_full_set_accepted(self) -> None:
        self.assertTrue(self._check(self.FULL))
        self.assertIn("5 项资产齐全", self.output.getvalue())

    def test_mandatory_assets_are_frozen(self) -> None:
        self.assertEqual(("naiba-chat.exe", "cat-chat.exe", "naiba-chat-update.json"),
                         rw.MANDATORY_ASSETS)

    def test_each_missing_asset_is_rejected_with_a_readable_reason(self) -> None:
        for name in ("naiba-chat.exe", "cat-chat.exe", "naiba-chat-update.json"):
            with self.subTest(missing=name):
                self.output.seek(0)
                self.output.truncate()
                self.assertFalse(self._check(self._without(name)))
                self.assertIn(f"缺少必需资产 {name}", self.output.getvalue())

    def test_each_missing_zip_is_rejected(self) -> None:
        for name in ("cat-chat-2.7.7-beta-windows-x64.zip",
                     "naiba-chat-2.7.7-beta-windows-x64.zip"):
            with self.subTest(missing=name):
                self.output.seek(0)
                self.output.truncate()
                self.assertFalse(self._check(self._without(name)))
                self.assertIn("缺少", self.output.getvalue())

    def test_similarly_named_zip_does_not_count(self) -> None:
        """判据是「前缀 + 后缀都命中」，`naiba-chat.exe` 之类别名蒙不过去。"""
        assets = self._without("naiba-chat-2.7.7-beta-windows-x64.zip")
        assets["naiba-chat-2.7.7-beta-windows-x64.msi"] = {"size": 1}
        self.assertFalse(self._check(assets))

    def test_extra_assets_do_not_fail_the_check(self) -> None:
        assets = dict(self.FULL, **{"extra-notes.txt": {"size": 1}})
        self.assertTrue(self._check(assets))


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
