# -*- coding: utf-8 -*-
r"""把线上 Release 的正文更新成 release.yml 里的最新 body（不等 CI 跑完就能生效）。

用途：2.9.0-beta 的 Release 正文是 2.8.8 的旧文案。推 master 后 CI 会用新 body 重新
发布一次，但那要等十几分钟；本脚本走 Release REST 的 PATCH，立刻改对。

用法：
    .venv\Scripts\python.exe verify\_patch_release_body.py [tag] [来源yml] [sha]

三个参数都可省：
    tag       默认 v2.9.1-beta
    来源yml   默认当前 .github/workflows/release.yml（要修历史版本时，传那一版的 yml 备份）
    sha       默认本地 HEAD；传历史版本时要给那一版的 commit（body 首行会带它）
token 走 `git credential fill`，不打印。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "yc883123/cat-chat"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
_INDENT = re.compile(r"^(\s*)([^\s#][^:]*):[ \t]*\|[-+]?[ \t]*$")


def block_scalar(text: str, key: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    for index, line in enumerate(lines):
        match = _INDENT.match(line)
        if match is None or match.group(2).strip() != key:
            continue
        base = len(match.group(1))
        out: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            candidate = lines[cursor]
            if not candidate.strip():
                out.append("")
                cursor += 1
                continue
            if len(candidate) - len(candidate.lstrip(" ")) <= base:
                break
            out.append(candidate)
            cursor += 1
        return "\n".join(out)
    return ""


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


def call(url: str, tok: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "v2.9.1-beta"
    source = Path(sys.argv[2]) if len(sys.argv) > 2 else WORKFLOW
    sha_override = sys.argv[3] if len(sys.argv) > 3 else ""
    tok = token()
    print("token:", "有" if tok else "无")
    if not tok:
        return 2

    if sha_override:
        sha = sha_override
    else:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT)).decode().strip()
    body = block_scalar(source.read_text(encoding="utf-8", newline=""), "body")
    body = body.replace("${{ github.sha }}", sha)
    if not body.strip():
        print("没取到 body，中止；来源 =", source)
        return 3
    print("来源 yml =", source.name, "| sha =", sha)
    print("新正文行数 =", len(body.splitlines()))

    release = call(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}", tok)
    rid = release.get("id")
    old = release.get("body") or ""
    print("release id =", rid, "| 旧正文行数 =", len(old.splitlines()))
    print("旧正文含 1872 :", "1872" in old)
    print("旧正文含 手机裸回车:", "手机裸回车" in old)

    updated = call(f"https://api.github.com/repos/{REPO}/releases/{rid}", tok,
                   method="PATCH", payload={"body": body})
    new = updated.get("body") or ""
    print("=" * 60)
    print("PATCH 完成")
    print("新正文含 1872      :", "1872" in new)
    print("新正文含 手机裸回车  :", "手机裸回车" in new)
    print("新正文含 1910      :", "1910" in new)
    print("新正文含 出厂就带 6 :", "出厂就带 6" in new)
    print("线上正文行数 =", len(new.splitlines()))
    print("html_url =", updated.get("html_url"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
