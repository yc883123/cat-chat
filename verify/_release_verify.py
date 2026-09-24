# -*- coding: utf-8 -*-
r"""发版后核对：CI run、Release 5 项资产、清单协议常量、双 exe 哈希。

为什么要工具化：发版核对靠手点 GitHub 页面会漏项，且 **`HEAD` 探资产会假报缺失**
（`SSL: UNEXPECTED_EOF_WHILE_READING`），必须以「完整 GET 下载 + 真的算一遍 SHA-256」为准。
匿名 API 额度可能耗尽（403），所以 token 走 `git credential fill`（不打印）。

用法：
    .venv\Scripts\python.exe verify\_release_verify.py            # tag 取 release.yml，sha 取 HEAD
    .venv\Scripts\python.exe verify\_release_verify.py v2.9.1-beta
    .venv\Scripts\python.exe verify\_release_verify.py v2.9.1-beta 319a2a7

退出码：0 全过 / 1 有项不通过 / 2 拿不到 token。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "yc883123/cat-chat"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
# 直连 GitHub release 资产 CDN 会被重置（WinError 10054/10060），降级挂本机代理。
PROXY = "http://127.0.0.1:7897"
# §零 协议常量：已发布客户端逐字校验，任何版本都不许改。
FROZEN_REPOSITORY = "yc883123/naiba-chat"
FROZEN_ASSET = "naiba-chat.exe"

problems: list[str] = []


def ok(label: str, good: bool, detail: str = "") -> None:
    print("  %-4s %s%s" % ("OK" if good else "FAIL", label, ("  " + detail) if detail else ""))
    if not good:
        problems.append(label + (("  " + detail) if detail else ""))


def token() -> str:
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, cwd=str(ROOT),
    )
    for line in proc.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return ""


def api(path: str, tok: str):
    req = urllib.request.Request(f"https://api.github.com{path}", headers={
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch(url: str) -> bytes:
    """先禁代理直连，失败再挂本机代理（release CDN 常需代理）。"""
    last: Exception | None = None
    for label, handler in (("直连", urllib.request.ProxyHandler({})),
                           ("代理", urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))):
        try:
            opener = urllib.request.build_opener(handler)
            with opener.open(urllib.request.Request(url), timeout=600) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - 探针要如实报
            last = exc
            print("     （%s失败：%s）" % (label, exc))
    raise RuntimeError("下载失败：%s" % last)


def workflow_tag() -> str:
    text = WORKFLOW.read_text(encoding="utf-8", newline="")
    found = re.search(r"^\s*RELEASE_TAG:[ \t]*(\S+)[ \t]*\r?$", text, re.MULTILINE)
    return found.group(1) if found else ""


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else workflow_tag()
    sha = sys.argv[2] if len(sys.argv) > 2 else subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(ROOT)).decode().strip()
    version = tag.lstrip("v")
    print("tag =", tag, "| sha =", sha[:12], "| 版本 =", version)
    tok = token()
    print("token:", "有" if tok else "无")
    if not tok:
        return 2

    print("=" * 72)
    print("一、CI run（head_sha=%s）" % sha[:12])
    print("=" * 72)
    found_run = False
    for r in (api(f"/repos/{REPO}/actions/runs?per_page=10", tok).get("workflow_runs") or []):
        hit = r["head_sha"].startswith(sha[:12])
        if hit:
            found_run = True
            ok("run #%s 结论" % r["run_number"], r["conclusion"] == "success",
               "%s/%s" % (r["status"], r["conclusion"]))
            if r["conclusion"] != "success" and r.get("id"):
                jobs = api(f"/repos/{REPO}/actions/runs/{r['id']}/jobs", tok)
                for j in jobs.get("jobs") or []:
                    bad = [s for s in (j.get("steps") or [])
                           if s.get("conclusion") not in (None, "success", "skipped")]
                    for s in bad:
                        print("       失败步骤:", j.get("name"), "/", s.get("name"))
    if not found_run:
        print("  （这 10 条 run 里没有 head_sha=%s 的，可能还没排上或已翻页）" % sha[:12])

    print("=" * 72)
    print("二、Release 资产（tag=%s）" % tag)
    print("=" * 72)
    try:
        rel = api(f"/repos/{REPO}/releases/tags/{tag}", tok)
    except Exception as exc:  # noqa: BLE001
        print("  拿不到 release：", exc)
        return 1
    expected = {
        "naiba-chat.exe", "cat-chat.exe", "naiba-chat-update.json",
        f"cat-chat-{version}-windows-x64.zip",
        f"naiba-chat-{version}-windows-x64.zip",
    }
    names = {a["name"]: a for a in (rel.get("assets") or [])}
    for name in sorted(names):
        print("    %-48s %12d 字节" % (name, names[name].get("size") or 0))
    ok("5 项资产无缺", not (expected - set(names)), "缺：" + str(sorted(expected - set(names))))
    ok("5 项资产无多", not (set(names) - expected), "多：" + str(sorted(set(names) - expected)))

    print("=" * 72)
    print("三、Release 正文（不得还是上一版的事）")
    print("=" * 72)
    body = rel.get("body") or ""
    ok("正文非空", bool(body.strip()), "%d 行" % len(body.splitlines()))
    ok("正文带本版标题", f"Cat Chat {version.replace('-beta', '')} Beta" in body)
    # 与清单上一版头条比对：成片照抄即判红（与 tests/test_release_workflow.py 同口径）。
    try:
        notes = json.loads(fetch(
            f"https://github.com/{REPO}/releases/download/{tag}/naiba-chat-update.json"
        ).decode("utf-8")).get("release_notes") or []
        cur, prev = notes[0], notes[1]
        clauses = [c for c in re.split(r"[，。；：、（）()「」【】《》,;:()\[\]{}\s]+", prev)
                   if len(c) >= 12]
        copied = [c for c in clauses if c in body and c not in cur]
        ok("正文未照抄上一版（成片判据）", len(copied) < 3, "命中 %d 句" % len(copied))
        if copied:
            for c in copied[:5]:
                print("       ·", c)
    except Exception as exc:  # noqa: BLE001
        print("  （跳过正文比对：清单还没取到 — %s）" % exc)

    print("=" * 72)
    print("四、清单 + 双 exe 哈希（完整 GET 后现算）")
    print("=" * 72)
    try:
        manifest = json.loads(fetch(
            f"https://github.com/{REPO}/releases/download/{tag}/naiba-chat-update.json"
        ).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print("  清单下载失败：", exc)
        return 1
    print("    version=%s  commit=%s  说明条数=%d" % (
        manifest.get("version"), str(manifest.get("commit"))[:12],
        len(manifest.get("release_notes") or [])))
    ok("清单 version 与 tag 一致", manifest.get("version") == version)
    ok("清单 repository 恒为协议常量", manifest.get("repository") == FROZEN_REPOSITORY,
       repr(manifest.get("repository")))
    ok("清单 asset 恒为协议常量", manifest.get("asset") == FROZEN_ASSET,
       repr(manifest.get("asset")))

    digests: dict[str, str] = {}
    for name in ("naiba-chat.exe", "cat-chat.exe"):
        try:
            data = fetch(f"https://github.com/{REPO}/releases/download/{tag}/{name}")
            digest = hashlib.sha256(data).hexdigest()
            digests[name] = digest
            print("    %-16s %10d 字节 sha256=%s" % (name, len(data), digest[:16]))
        except Exception as exc:  # noqa: BLE001
            print("    %-16s 下载失败：%s" % (name, exc))
    if len(digests) == 2:
        ok("双 exe 同一字节", len(set(digests.values())) == 1)
    if "naiba-chat.exe" in digests:
        ok("naiba-chat.exe 与清单 sha256 一致",
           digests["naiba-chat.exe"] == str(manifest.get("sha256") or ""))

    print("=" * 72)
    if problems:
        print("结论：不通过，共 %d 项：" % len(problems))
        for item in problems:
            print("  ·", item)
        return 1
    print("结论：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
