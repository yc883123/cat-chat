"""等 GitHub Actions 发版跑完，并校验 Release 资产是否自洽。

本机**没有 gh CLI**，所以直接打匿名 GitHub API（仓库公开、免 token，60 次/小时）。
一条命令走完「等 run → 看每步结论 → 核对 manifest → 核对 5 项资产 → 下载两个 exe 对哈希」。

**资产契约（仓库更名后为 5 项，缺一即失败）**：`naiba-chat.exe`（旧客户端自动更新链，永久）、
`cat-chat.exe`（与前者同字节，哈希必须相等且都等于清单值）、两个同名 zip、清单
`naiba-chat-update.json`（其 `repository` 字段恒为旧值，见 `MANIFEST_REPOSITORY`）。

**匿名额度是硬约束**：额度耗尽时 API 一律回 403（`X-RateLimit-Remaining: 0`）。
旧实现把它当网络抖动、每 15s 重试一次直到 20 分钟超时，最后打印「超时」并退出 1
——**发布明明是好的却报失败**（2026-09-17 实测踩到）。现在识别到额度耗尽会**立即降级**
到「静态口核验」：`git ls-remote --tags` 看 tag → `releases/latest/download/` 取 manifest
与 exe 对哈希，全程不需要 API。代价是拿不到逐步骤结论（要看得自己去 Actions 页面）。

用法：
  .venv/Scripts/python.exe verify/release_watch.py
      # 等本地 HEAD 那个 sha 的 run；成功后若知道 tag 会自动核对资产
  .venv/Scripts/python.exe verify/release_watch.py <sha> <tag>
      # 显式指定，如：… release_watch.py ed68fe55… v2.3.5-beta
      # **额度耗尽时也必须带 tag**（静态口只能按 tag 定位资产）

退出码：0 = 发布成功且资产自洽；1 = 失败/超时/资产不一致；
        2 = **无法核验**（额度耗尽又没带 tag，或网络中断）——**不等于发布失败**。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 仓库 2026-09-21 更名为 `cat-chat`（旧名 `naiba-chat` 由 GitHub 301 长期重定向，新旧都能取到数据）。
# 这里取**新名**：核验器的用途是「确认这次发布是好的」，用门面真名才顺带验到改名有没有生效。
REPO = "yc883123/cat-chat"
# 而清单里的 `repository` 是**协议常量**，必须与 `REPO` 不同名、永远写旧值——两者刻意分开，
# 免得后人「顺手对齐」，那会让全部旧客户端拒绝更新。
MANIFEST_REPOSITORY = "yc883123/naiba-chat"
API = f"https://api.github.com/repos/{REPO}"
# 不需要 API、不受额度限制的资产口：release 是 Latest 时直接按资产名取。
STATIC_BASE = f"https://github.com/{REPO}/releases/latest/download"
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "naiba-release-watch"}

# ======================= 发布资产契约（改名后为 5 项，缺一即发版失败） =======================
# 前三项是**更新协议**：`naiba-chat.exe` 服务旧客户端自动更新链、`naiba-chat-update.json` 是清单，
# 各自被历史版本逐字校验，**永久不可停发**；漏掉 `naiba-chat.exe` 会让全部旧客户端断更——这是本项目
# 最致命的一类发布事故，所以必须**硬失败**，不能因为「资产本来就没在预期清单里」而静默放过。
MANDATORY_ASSETS = ("naiba-chat.exe", "cat-chat.exe", "naiba-chat-update.json")
# 两个 exe 是同一字节（哈希必须相等，且都等于清单里的值）：前者给旧客户端，后者给人直接下载。
EXECUTABLE_ASSETS = ("naiba-chat.exe", "cat-chat.exe")
# 双名 zip：内容等价，只有包内 exe 的文件名不同。旧名 zip 与更新器无关（更新器从不消费 zip），
# 只为兼容既存的教程与口耳相传的下载链接。
ZIP_SUFFIX = "-windows-x64.zip"
ZIP_PREFIXES = ("cat-chat-", "naiba-chat-")
# ==========================================================================================

# `release_notes` 头条允许的品牌前缀：**产品显示名 2026-09-20 起为 Cat Chat**（原名 Naiba Chat）。
# 历史版本条目仍以 Naiba Chat 开头，所以两个前缀都算正常——但不能因此放宽到「任意字符串」，
# 否则「头条写错/换了别的说明」这类发布事故会被静默放过。判据是「前缀 ∈ 已知品牌」，
# 不是「含品牌字样」。
BRAND_PREFIXES = ("Cat Chat", "Naiba Chat")

# wait_for_run 的哨兵返回值：不是「超时没等到」，而是「根本问不到」——两者处置完全不同，
# 混成一个 None 就会把额度问题误报成发布失败。
API_UNAVAILABLE = object()


class RateLimited(Exception):
    """匿名 API 额度耗尽（403/429 + `X-RateLimit-Remaining: 0`，或错误体含 rate limit）。"""


class NetworkError(Exception):
    """重试若干次后仍取不到数据。本机到 GitHub 的 TLS 偶发 `UNEXPECTED_EOF_WHILE_READING`
    （额度探测、exe 下载都撞到过），属于**环境瞬时问题**，与发布成败无关。"""


RETRY_SLEEP = 5


def is_rate_limited(error: Exception) -> bool:
    if not isinstance(error, urllib.error.HTTPError) or error.code not in (403, 429):
        return False
    if error.headers.get("X-RateLimit-Remaining") == "0":
        return True
    try:
        body = error.read(400).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 读不到 body 不影响判定
        return False
    return "rate limit" in body.lower()


def fetch_bytes(url: str, timeout: int = 60, attempts: int = 4) -> bytes:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if is_rate_limited(error):
                raise RateLimited() from error
            last = error
        except Exception as error:  # noqa: BLE001 - URLError / SSLError / 超时
            last = error
        print(f"  取数失败（第 {attempt}/{attempts} 次）：{type(last).__name__}: {last}")
        if attempt < attempts:
            time.sleep(RETRY_SLEEP)
    raise NetworkError(str(last))


def get_json(url: str, timeout: int = 30):
    return json.loads(fetch_bytes(url, timeout=timeout).decode("utf-8"))


def local_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), check=True,
                          capture_output=True, text=True).stdout.strip()


def wait_for_run(sha: str, minutes: int = 20):
    deadline = time.time() + minutes * 60
    run = None
    while time.time() < deadline:
        try:
            data = get_json(f"{API}/actions/runs?per_page=10")
        except RateLimited:
            print("!! 匿名 API 额度已耗尽（403 rate limit）——不再空等，改走静态口核验。")
            return API_UNAVAILABLE
        except Exception as error:  # noqa: BLE001 - 网络抖动不影响主流程
            print("查询失败，稍后重试:", error)
            time.sleep(15)
            continue
        run = next((item for item in data.get("workflow_runs", [])
                    if item["head_sha"] == sha), None)
        if run is None:
            print(f"还没看到 sha={sha[:8]} 的 run…")
        else:
            print(f"#{run['run_number']} status={run['status']} conclusion={run['conclusion']}")
            if run["status"] == "completed":
                return run
        time.sleep(15)
    print("超时：没等到该 run")
    return run


def report_steps(run: dict) -> None:
    jobs = get_json(f"{API}/actions/runs/{run['id']}/jobs")
    for job in jobs.get("jobs", []):
        print(f"\njob: {job['name']} | {job['status']}/{job['conclusion']}")
        for step in job.get("steps", []):
            flag = {"success": "OK  ", "failure": "FAIL", "skipped": "skip"}.get(
                step.get("conclusion") or "", "..  ")
            print(f"  {flag} {step['name']}")


def hash_remote_exe(url: str, attempts: int = 4) -> str:
    """流式下载 exe 并算 SHA-256（算完即删，不留大文件）。

    84MB 下到一半断流很常见（TLS 偶发 EOF），所以**每次重试都从头重下**：断点续传会把
    「两份不同内容拼一起」算成一个假哈希，比重新下载危险得多。
    """
    target = ROOT / "verify" / "_release_exe_check.exe"
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        print("下载 exe 核对哈希（约 84MB，稍等）…" if attempt == 1
              else f"  重新下载 exe（第 {attempt}/{attempts} 次）…")
        digest = hashlib.sha256()
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=300) as response, \
                    target.open("wb") as handle:
                while chunk := response.read(1 << 20):
                    handle.write(chunk)
                    digest.update(chunk)
            return digest.hexdigest()
        except Exception as error:  # noqa: BLE001 - 中途断流也要整体重来
            last = error
            print(f"  下载中断：{type(error).__name__}: {error}")
            if attempt < attempts:
                time.sleep(RETRY_SLEEP)
    target.unlink(missing_ok=True)
    raise NetworkError(str(last))


def check_manifest(manifest: dict, commit: str = "") -> bool:
    """manifest 与本次提交是否自洽（两条核验路径共用）。"""
    ok = True
    notes = manifest.get("release_notes")
    count = len(notes) if isinstance(notes, list) else "(不是列表)"
    print(f"manifest: version={manifest.get('version')} "
          f"commit={str(manifest.get('commit'))[:12]} notes={count} 条")
    print("  sha256 =", manifest.get("sha256"))
    if manifest.get("repository") != MANIFEST_REPOSITORY:
        # 这是全项目后果最重的一个字段：旧客户端**逐字**比对它，改值即让全部历史版本用户
        # 永久失去自动更新。仓库更名后它**仍然**必须写旧值（REPO 是新门面名，两者刻意不同名）。
        print(f"  !! 清单 repository 必须恒为 {MANIFEST_REPOSITORY!r}（协议常量），"
              f"实际 {manifest.get('repository')!r}")
        ok = False
    if commit and manifest.get("commit") != commit:
        print("  !! manifest.commit 不是本次提交"); ok = False
    if not isinstance(notes, list) or not notes:
        print("  !! release_notes 缺失或为空"); ok = False
    elif not isinstance(notes[0], str) or not notes[0].startswith(BRAND_PREFIXES):
        print(f"  !! release_notes 头条异常（需以 {' / '.join(BRAND_PREFIXES)} 开头）")
        ok = False
    return ok


def check_hash(manifest: dict, exe_url: str, label: str = "exe") -> bool:
    """哈希必须实测比对：不一致时应用内「检查更新」会**静默拒绝安装**。"""
    actual = hash_remote_exe(exe_url)
    print(f"  {label} 实际 sha256 =", actual)
    if actual != manifest["sha256"]:
        print(f"  !! {label} 哈希与清单不一致 —— 应用内自动更新会拒绝安装")
        return False
    print(f"  {label} 哈希一致 → 「检查更新」可正常校验并安装")
    return True


def check_asset_set(assets: dict) -> bool:
    """5 项资产硬校验（缺一即失败）。

    为什么不能只靠后面那句 `assets["naiba-chat.exe"]`：缺资产时它会抛 `KeyError`，等于把
    「判失败」变成「崩给你看」；而漏传 `naiba-chat.exe` 正是最致命的一种发布事故。
    """
    ok = True
    for name in MANDATORY_ASSETS:
        if name not in assets:
            print(f"  !! 缺少必需资产 {name}")
            ok = False
    for prefix in ZIP_PREFIXES:
        if not any(n.startswith(prefix) and n.endswith(ZIP_SUFFIX) for n in assets):
            print(f"  !! 缺少 {prefix}*{ZIP_SUFFIX}")
            ok = False
    if ok:
        print("  OK  5 项资产齐全（双 exe + 双 zip + 清单）")
    return ok


def check_assets(tag: str, commit: str) -> bool:
    release = get_json(f"{API}/releases/tags/{tag}")
    assets = {a["name"]: a for a in release["assets"]}
    print(f"\n--- Release {release['tag_name']} | {release['name']} ---")
    print(f"prerelease={release['prerelease']}  assets="
          + ", ".join(f"{n}({a['size']:,}B)" for n, a in assets.items()))

    ok = check_asset_set(assets)
    if "naiba-chat-update.json" not in assets:
        # 清单都拿不到就没法往下核，但上面的缺项结论已经打出来了，不要返回 True。
        return False
    manifest = get_json(assets["naiba-chat-update.json"]["browser_download_url"])
    if not check_manifest(manifest, commit):
        ok = False
    # 两个 exe 各算一次：内容应当相同、且都等于清单值（同名复制，字节一致）。
    for name in EXECUTABLE_ASSETS:
        if name not in assets:
            continue
        if not check_hash(manifest, assets[name]["browser_download_url"], name):
            ok = False
    return ok


def remote_tags() -> dict:
    """`git ls-remote --tags origin` → {tag: sha}（走 git 协议，不消耗 API 额度）。"""
    out = subprocess.run(["git", "ls-remote", "--tags", "origin"], cwd=str(ROOT), check=True,
                         capture_output=True, text=True).stdout
    tags = {}
    for line in out.splitlines():
        if line.endswith("^{}"):  # annotated tag 的解引用行，跳过
            continue
        sha, ref = line.split("\t")
        tags[ref.replace("refs/tags/", "")] = sha
    return tags


def check_assets_static(tag: str, commit: str) -> bool:
    """额度耗尽时的兜底：tag 存在 → manifest 自洽 → exe 对哈希，全程不用 API。

    走 `releases/latest/download/<资产名>`，所以只有在本次发布被标记为 Latest 时成立
    （workflow 里 `make_latest: true`，正常成立）。
    """
    print(f"\n--- 静态口核验 tag={tag}（不需要 API）---")
    ok = True
    tags = remote_tags()
    if tag in tags:
        tag_sha = tags[tag]
        print(f"  OK  tag {tag} → {tag_sha[:12]}")
        if commit and tag_sha != commit:
            print(f"  !! tag 指向 {tag_sha[:12]}，不是本次提交 {commit[:12]}"); ok = False
    else:
        print(f"  !! 远端还没有 tag {tag}（最近 5 个：{', '.join(sorted(tags)[-5:])}）"); ok = False

    manifest = get_json(f"{STATIC_BASE}/naiba-chat-update.json")
    if not check_manifest(manifest, commit):
        ok = False
    # 两个 exe 都核：静态口没有资产列表，缺资产只能靠下载 404 暴露（这正是它的价值所在）。
    for name in EXECUTABLE_ASSETS:
        if not check_hash(manifest, f"{STATIC_BASE}/{name}", name):
            ok = False
    if not ok:
        print("\n静态核验未通过。注意：额度耗尽时拿不到逐步骤结论，需要的话去 Actions 页面看。")
    else:
        print("  注：静态口拿不到资产列表，5 项资产齐全与否请用带额度的完整核验确认。")
    return ok


def main() -> int:
    sha = sys.argv[1] if len(sys.argv) > 1 else local_head()
    tag = sys.argv[2] if len(sys.argv) > 2 else ""
    print(f"跟踪 sha={sha}")

    run = wait_for_run(sha)
    if run is API_UNAVAILABLE:
        # 额度耗尽 ≠ 发布失败：降级核验，绝不把「问不到」报成「没成功」。
        if not tag:
            print("额度耗尽且未指定 tag，静态口无法定位资产。")
            print("请带 tag 重跑：release_watch.py <sha> vX.Y.Z-beta")
            return 2
        try:
            return 0 if check_assets_static(tag, sha) else 1
        except NetworkError as error:
            print(f"\n!! 网络中断，没能核完：{error}")
            print("   这不是发布失败——稍后重跑本脚本即可（git ls-remote 与静态口都不吃额度）。")
            return 2
    if run is None:
        return 1
    print(f"\nrun #{run['run_number']} → {run['conclusion']}")
    print(run["html_url"])
    if run["conclusion"] != "success":
        try:
            report_steps(run)
        except (RateLimited, NetworkError) as error:
            print("步骤结论取不到（额度/网络）：", error)
        print("\n构建失败，看上面的 FAIL 步骤")
        return 1
    try:
        report_steps(run)
    except (RateLimited, NetworkError) as error:
        print("步骤结论取不到（额度/网络）：", error)
    if not tag:
        print("\n发布成功（未指定 tag，跳过资产核对）")
        return 0
    try:
        return 0 if check_assets(tag, sha) else 1
    except RateLimited:
        print("\n额度在核验途中耗尽 —— 改走静态口核验。")
        try:
            return 0 if check_assets_static(tag, sha) else 1
        except NetworkError as error:
            print(f"\n!! 网络中断，没能核完：{error}")
            return 2
    except NetworkError as error:
        print(f"\n!! 网络中断，没能核完：{error}（run 结论是 success，稍后重跑核验资产）")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
