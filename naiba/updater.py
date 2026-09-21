from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from naiba import net as net_io


# ============================ 更新协议常量（永不可改） ============================
# 下面三个常量连同清单里的 `repository` / `asset` 字段，是**已发布客户端逐字校验**的协议：
# 旧客户端只接受 `naiba-chat.exe` 这个资产名，只接受清单 `repository == "yc883123/naiba-chat"`。
# 仓库已更名为 `cat-chat`（2026-09-21），但 GitHub 对旧仓库名做长期 301 重定向，因此**改的是
# 「人可见链接」，不是这三个常量**：Release 必须继续同时上传 `naiba-chat.exe` 与清单
# `naiba-chat-update.json`，清单里的 `repository` 也必须继续写旧值。一旦停发或改值，
# 全部历史版本用户会永久失去自动更新，且**无法用版本号界定受影响范围**（用户可能跳过任意多个版本）。
# 唯一会破坏旧链路的操作是「在 `yc883123/naiba-chat` 这个旧名下新建仓库」——禁止。
REPOSITORY = "yc883123/naiba-chat"
MANIFEST_ASSET = "naiba-chat-update.json"
EXECUTABLE_ASSET = "naiba-chat.exe"
# ==================================================================================
# 内存/磁盘发布列表在此时长内视为新鲜，命中即复用，减少对 GitHub API 配额的无谓消耗。
CACHE_TTL_SECONDS = 6 * 3600
# 合成发布条目的 tag；安装该条目时始终走 releases/latest/download 静态直连，不占 API 配额。
LATEST_TAG = "latest"
MANUAL_RELEASE_VERSION = (2, 0, 0)
MANUAL_RELEASE_URL = f"https://github.com/{REPOSITORY}/releases"
DEFAULT_RELEASE_NOTES = [
    "修复 Windows 10053/10054/10061 瞬时连接错误，并在失败时显示供应商主机和排查提示。",
    "修复推理模型只返回 reasoning 时测试连接被误判失败的问题。",
    "新增 Ollama 原生 /api/chat 请求格式，并提供配置引导。",
    "恢复 Ollama 和 LM Studio 的手动模型卸载入口，可释放显存和内存。",
    "本地模型使用独立的长等待策略，不再受在线 API 的 180 秒超时与重试影响。",
    "修复更新说明长期显示旧内容的问题，发布流程改为读取统一说明文件。",
]


# 安装（换 exe）用的 PowerShell 脚本正文。放模块级常量而不是内联字符串，是为了让
# 守门测试与冻结版自检能在**运行期**读到它——本程序被 PyInstaller 打进 PYZ 后没有可读的
# 源码文件，只有运行期常量还拿得到（否则「更新后重启不显示窗口」这类文案缺陷无法在
# 打包产物上回归）。
#
# 两条 Start-Process 都必须以**普通窗口**启动：本程序是窗口化打包（spec `console=False`，
# 本来就没有控制台窗口要藏），而 `-WindowStyle Hidden` 会让新进程的主窗口一起处于隐藏状态
# ——更新后进程活着、托盘图标在，但界面不出来，用户只能到托盘双击「打开窗口」才能唤回。
APPLY_UPDATE_SCRIPT = """param([int]$ProcessId, [string]$Downloaded, [string]$Target, [string]$Backup)
$ErrorActionPreference = 'Stop'
Wait-Process -Id $ProcessId -Timeout 30 -ErrorAction SilentlyContinue
$deadline = (Get-Date).AddSeconds(60)
$installed = $false
while ((Get-Date) -lt $deadline) {
  try {
    if (Test-Path -LiteralPath $Target) { Copy-Item -LiteralPath $Target -Destination $Backup -Force }
    Copy-Item -LiteralPath $Downloaded -Destination $Target -Force
    $installed = $true
    break
  } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $installed) { exit 1 }
$env:PYINSTALLER_RESET_ENVIRONMENT = '1'
Get-ChildItem Env: | Where-Object { $_.Name -like '_PYI_*' } | ForEach-Object {
  Remove-Item -LiteralPath ("Env:" + $_.Name) -ErrorAction SilentlyContinue
}
$started = Start-Process -FilePath $Target -WorkingDirectory (Split-Path -Parent $Target) -PassThru
Start-Sleep -Seconds 3
if ($started.HasExited) {
  Start-Sleep -Seconds 2
  Start-Process -FilePath $Target -WorkingDirectory (Split-Path -Parent $Target)
}
Remove-Item -LiteralPath $Downloaded -Force -ErrorAction SilentlyContinue
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_body_to_notes(body: Any) -> list[str]:
    """把 GitHub Release body 转成结构化更新说明列表，供版本下拉展示。"""
    if not body:
        return []
    return [line.strip() for line in str(body).splitlines() if line.strip()]


class UpdateManager:
    def __init__(self, app_dir: Path, data_dir: Path):
        self.app_dir = app_dir.resolve()
        self.data_dir = data_dir.resolve()
        self.lock = threading.RLock()
        self.latest: dict[str, Any] | None = None
        self.releases: list[dict[str, Any]] = []
        self.releases_loaded_at = 0
        self.phase = "idle"
        self.error = ""
        self.checked_at = 0
        # True 表示 self.releases 来自磁盘缓存回退（非实时 API），更新决策须以直连清单为准。
        self._releases_from_cache = False
        self.manual_release_url = ""
        self.manual_update_available = False
        self.build = self._read_build_info()
        self.pending_verification = self.verify_pending()

    @staticmethod
    def _with_proxy_context(message: str) -> str:
        """在错误消息末尾附上当前实际生效的外部请求模式，便于用户定位网络问题。"""
        try:
            note = net_io.proxy_state().get("note") or ""
        except Exception:  # noqa: BLE001 - 诊断信息失败不影响主流程
            note = ""
        if not note:
            return message
        return f"{message}（外部请求：{note}）"

    def _run_git(self, *args: str, timeout: int = 30) -> str:
        attempts = 2 if args and args[0] in {"fetch", "pull", "ls-remote"} else 1
        for attempt in range(attempts):
            result = subprocess.run(
                ["git", *args],
                cwd=self.app_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                return result.stdout.strip()
            if attempt + 1 < attempts:
                time.sleep(1.5)
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"git {' '.join(args)} 执行失败")

    def _source_repository(self) -> bool:
        if getattr(sys, "frozen", False) or not (self.app_dir / ".git").exists():
            return False
        try:
            remote = self._run_git("remote", "get-url", "origin", timeout=5).lower()
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return False
        normalized = remote.removesuffix(".git").replace("\\", "/")
        # 仓库名 2026-09-21 起为 `cat-chat`（原名 `naiba-chat`，旧地址由 GitHub 301 长期重定向）。
        # 这里**必须同时接受两个名字**：老克隆的 remote 仍是旧名，新克隆的 remote 是新名，
        # 只认一个就会让另一半用户在源码模式点「检查更新」时被误报成「不是受支持的仓库」。
        # 注意与 REPOSITORY 常量区分——那个是更新协议的一部分，**永远保持旧值**（见模块头注释）。
        return bool(re.search(r"github\.com[/:]yc883123/(?:naiba-chat|cat-chat)$", normalized))

    def _read_build_info(self) -> dict[str, str]:
        if not getattr(sys, "frozen", False) and (self.app_dir / ".git").exists():
            try:
                commit = self._run_git("rev-parse", "HEAD", timeout=5).lower()
                return {"version": "source", "commit": commit}
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass
        resource_dir = Path(getattr(sys, "_MEIPASS", self.app_dir)).resolve()
        path = resource_dir / "build_info.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return {
                    "version": str(value.get("version") or "dev"),
                    "commit": str(value.get("commit") or ""),
                }
        except (OSError, json.JSONDecodeError):
            pass
        return {"version": "dev", "commit": ""}

    def _read_release_notes(self) -> list[str]:
        resource_dir = Path(getattr(sys, "_MEIPASS", self.app_dir)).resolve()
        path = resource_dir / "release_notes.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(value, dict):
                value = value.get("release_notes", [])
            if isinstance(value, list):
                notes = [str(note).strip() for note in value if str(note).strip()][:50]
                if notes:
                    return notes
        except (OSError, json.JSONDecodeError):
            pass
        return list(DEFAULT_RELEASE_NOTES)

    @staticmethod
    def _build_number(version: str) -> int | None:
        match = re.fullmatch(r"build-(\d+)", str(version or "").strip(), flags=re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _release_version_key(version: str) -> tuple[int, int, int, int, int] | None:
        """Parse stable and beta release labels without mixing them with legacy builds."""
        match = re.fullmatch(
            r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-._ ]?(beta|b)(?:[-._ ]?(\d+))?)?",
            str(version or "").strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        major, minor, patch = (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))
        is_beta = bool(match.group(4))
        beta_number = int(match.group(5) or 0) if is_beta else 0
        return (major, minor, patch, 0 if is_beta else 1, beta_number)

    @classmethod
    def _is_auto_update_version(cls, version: str) -> bool:
        key = cls._release_version_key(version)
        return key is not None and key[:3] > MANUAL_RELEASE_VERSION

    @classmethod
    def _is_manual_migration_version(cls, version: str) -> bool:
        key = cls._release_version_key(version)
        return key is not None and key[:3] == MANUAL_RELEASE_VERSION

    @classmethod
    def _normalize_releases(cls, releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        eligible = [r for r in releases if cls._is_auto_update_version(str(r.get("version") or ""))]
        eligible.sort(key=lambda r: (cls._release_version_key(str(r.get("version") or "")) or (0,0,0,0,0), str(r.get("published_at") or ""), str(r.get("tag") or "")), reverse=True)
        seen: set[str] = set(); result: list[dict[str, Any]] = []
        for release in eligible:
            identity = str(release.get("version") or "").lower()
            if identity in seen: continue
            seen.add(identity); result.append(release)
        return result

    @property
    def supported(self) -> bool:
        return bool(os.name == "nt" and (getattr(sys, "frozen", False) or self._source_repository()))

    @property
    def mode(self) -> str:
        return "executable" if getattr(sys, "frozen", False) else "source"

    def _source_dirty(self) -> bool:
        if self.mode != "source" or not self._source_repository():
            return False
        try:
            return bool(self._run_git("status", "--porcelain", "--untracked-files=normal", timeout=10))
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return True

    def status(self) -> dict[str, Any]:
        with self.lock:
            latest = dict(self.latest or {})
            current_commit = self.build.get("commit", "")
            update_available = bool(
                latest
                and latest.get("update_available", latest.get("commit") != current_commit)
            )
            return {
                "repository": REPOSITORY,
                "supported": self.supported,
                "mode": self.mode,
                "current_version": self.build.get("version", "dev"),
                "current_commit": current_commit,
                "phase": self.phase,
                "error": self.error,
                "checked_at": self.checked_at,
                "update_available": update_available,
                "latest_version": latest.get("version", ""),
                "latest_commit": latest.get("commit", ""),
                "release_notes": latest.get("release_notes", []),
                "published_at": latest.get("published_at", ""),
                "release_url": latest.get("release_url", ""),
                "manual_update_available": self.manual_update_available,
                "manual_release_url": self.manual_release_url or MANUAL_RELEASE_URL,
                "manual_update_message": ("2.0.0 属于大版本更新，请前往 GitHub Release 手动下载。" if self.manual_update_available else ""),
                "releases": list(self.releases),
                "pending_verification": self.pending_verification,
                "source_dirty": self._source_dirty(),
            }

    @staticmethod
    def _is_rate_limited(exc: urllib.error.HTTPError) -> bool:
        """识别 GitHub API 限流：剩余配额为 0、携带 Retry-After 或响应体提及 rate limit。"""
        if exc.code not in (403, 429):
            return False
        headers = getattr(exc, "headers", None) or getattr(exc, "hdrs", None) or {}
        try:
            if str(headers.get("X-RateLimit-Remaining", "")).strip() == "0":
                return True
            if headers.get("Retry-After") is not None:
                return True
            body = exc.read(4096).decode("utf-8", errors="ignore")
            return "rate limit" in body.lower()
        except Exception:
            return False

    @staticmethod
    def _http_error_message(exc: urllib.error.HTTPError) -> str:
        if exc.code == 404:
            return "尚无可用的自动更新版本"
        if UpdateManager._is_rate_limited(exc):
            return "GitHub 接口访问频率受限，请稍后重试"
        if exc.code == 403:
            return "GitHub 拒绝了请求（HTTP 403），请检查网络或代理设置"
        return f"检查更新失败：HTTP {exc.code}"

    @staticmethod
    def _request_json(url: str, timeout: int = 20) -> dict[str, Any] | list[Any]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "naiba-chat-updater",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with net_io.open(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8-sig"))
        if not isinstance(value, (dict, list)):
            raise RuntimeError("更新服务器返回了无效数据")
        return value

    @staticmethod
    def _validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
        repository = str(manifest.get("repository") or "")
        commit = str(manifest.get("commit") or "").lower()
        checksum = str(manifest.get("sha256") or "").lower()
        asset = str(manifest.get("asset") or "")
        if repository != REPOSITORY:
            raise RuntimeError("更新清单的仓库不匹配")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise RuntimeError("更新清单的提交版本无效")
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise RuntimeError("更新清单的校验值无效")
        if asset != EXECUTABLE_ASSET:
            raise RuntimeError("更新清单的程序文件名无效")
        release_notes = manifest.get("release_notes", [])
        if isinstance(release_notes, str):
            release_notes = [release_notes]
        if not isinstance(release_notes, list):
            release_notes = []
        release_notes = [str(note).strip() for note in release_notes if str(note).strip()][:50]
        return {
            "repository": repository,
            "commit": commit,
            "sha256": checksum,
            "asset": asset,
            "version": str(manifest.get("version") or commit[:7]),
            "release_notes": release_notes,
        }

    def _load_cached_releases(self, cache: Path) -> list[dict[str, Any]] | None:
        """读取并结构校验磁盘发布列表缓存；缺失、损坏、空或字段异常的缓存视为不存在。"""
        try:
            if not cache.is_file():
                return None
            value = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(value, list) or not value:
            return None
        releases: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                return None
            tag = str(item.get("tag") or "")
            version = str(item.get("version") or "")
            installable = item.get("installable")
            if not tag or not version or not isinstance(installable, bool):
                return None
            raw_notes = item.get("release_notes")
            if isinstance(raw_notes, str):
                release_notes = [line.strip() for line in raw_notes.splitlines() if line.strip()]
            elif isinstance(raw_notes, list):
                release_notes = [str(note).strip() for note in raw_notes if str(note).strip()]
            else:
                release_notes = []
            releases.append(
                {
                    "tag": tag,
                    "version": version,
                    "published_at": str(item.get("published_at") or ""),
                    "release_url": str(item.get("release_url") or ""),
                    "release_notes": release_notes[:50],
                    "installable": installable,
                    "current": False,
                }
            )
        return self._normalize_releases(releases) or None

    def _mark_current(self, releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current_version = self.build.get("version", "")
        current_commit = self.build.get("commit", "")
        for release in releases:
            release["current"] = bool(
                release.get("version") == current_version
                and release.get("commit")
                and release.get("commit") == current_commit
            )
        return releases

    def _fetch_releases(self, force: bool = False) -> list[dict[str, Any]]:
        """返回发布列表。优先复用 6 小时内的内存/磁盘缓存；API 失败时回退读取缓存。"""
        cache = self.data_dir / "update" / "releases.json"
        if not force and self.releases_loaded_at and time.time() - self.releases_loaded_at < CACHE_TTL_SECONDS:
            return self.releases
        if not force and not self.releases_loaded_at:
            # 启动场景：磁盘缓存较新则直接复用，避免无谓请求 API 消耗配额。
            try:
                cache_fresh = cache.is_file() and time.time() - cache.stat().st_mtime < CACHE_TTL_SECONDS
            except OSError:
                cache_fresh = False
            if cache_fresh:
                cached = self._load_cached_releases(cache)
                if cached is not None:
                    self.releases = self._mark_current(cached)
                    self.releases_loaded_at = int(time.time())
                    self._releases_from_cache = True
                    return self.releases
        try:
            url = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
            data = self._request_json(url)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
            # API 限流/网络抖动时回退读缓存，保留历史版本下拉项。
            cached = self._load_cached_releases(cache)
            if cached is not None:
                self.releases = self._mark_current(cached)
                self.releases_loaded_at = int(time.time())
                self._releases_from_cache = True
                return self.releases
            raise
        if not isinstance(data, list):
            raise RuntimeError("更新服务器返回了无效数据")
        releases: list[dict[str, Any]] = []
        for item in data:
            tag = str(item.get("tag_name") or "")
            if not tag:
                continue
            assets = item.get("assets") or []
            asset_names = {str(asset.get("name") or "") for asset in assets}
            releases.append(
                {
                    "tag": tag,
                    "version": tag.lstrip("v"),
                    "published_at": str(item.get("published_at") or ""),
                    "release_url": str(item.get("html_url") or ""),
                    "release_notes": _release_body_to_notes(item.get("body")),
                    "installable": MANIFEST_ASSET in asset_names and EXECUTABLE_ASSET in asset_names,
                    "current": False,
                }
            )
        releases = self._normalize_releases(releases)
        self._mark_current(releases)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(releases, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        self.releases = releases
        self.releases_loaded_at = int(time.time())
        self._releases_from_cache = False
        return releases

    def _release_from_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """依据已校验的 latest 清单生成合成的可安装发布条目，供下拉框一键安装。"""
        version = str(manifest.get("version") or "")
        return {
            "tag": LATEST_TAG,
            "version": version,
            "published_at": str(manifest.get("published_at") or ""),
            "release_url": str(manifest.get("release_url") or ""),
            "release_notes": manifest.get("release_notes", []),
            "installable": True,
            "current": version == self.build.get("version", "") and bool(manifest.get("commit")) and manifest.get("commit") == self.build.get("commit", ""),
        }

    def _manifest_base(self, tag: str) -> str:
        """版本下载基址；latest（或空）映射到 releases/latest 静态直连，不消耗 API 配额。"""
        if not tag or tag == LATEST_TAG:
            return f"https://github.com/{REPOSITORY}/releases/latest/download"
        return f"https://github.com/{REPOSITORY}/releases/download/{tag}"

    def _fetch_manifest_for_tag(self, tag: str) -> dict[str, Any]:
        base = self._manifest_base(tag)
        manifest = self._validate_manifest(self._request_json(f"{base}/{MANIFEST_ASSET}"))
        is_latest = not tag or tag == LATEST_TAG
        manifest.update(
            {
                "download_url": f"{base}/{EXECUTABLE_ASSET}",
                "published_at": "",
                "release_url": (
                    f"https://github.com/{REPOSITORY}/releases/latest"
                    if is_latest
                    else f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
                ),
                "tag": LATEST_TAG if is_latest else tag,
            }
        )
        return manifest

    def _write_pending(self, latest: dict[str, Any]) -> None:
        update_dir = self.data_dir / "update"
        update_dir.mkdir(parents=True, exist_ok=True)
        marker = update_dir / "pending-update.json"
        marker.write_text(
            json.dumps(
                {
                    "version": latest.get("version", ""),
                    "commit": latest.get("commit", ""),
                    "tag": latest.get("tag", ""),
                    "installed_at": int(time.time()),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def verify_pending(self) -> dict[str, Any]:
        marker = self.data_dir / "update" / "pending-update.json"
        if not marker.is_file():
            return {"pending": False}
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            marker.unlink(missing_ok=True)
            return {"pending": False}
        marker.unlink(missing_ok=True)
        target_commit = str(data.get("commit") or "")
        current_commit = self.build.get("commit", "")
        ok = bool(target_commit) and target_commit == current_commit
        return {
            "pending": True,
            "ok": ok,
            "target_version": str(data.get("version") or ""),
            "target_commit": target_commit,
            "current_commit": current_commit,
        }

    def check(self, force: bool = False) -> dict[str, Any]:
        with self.lock:
            if not force and self.checked_at and time.time() - self.checked_at < 6 * 3600:
                return self.status()
            self.phase = "checking"
            self.error = ""
            self.manual_update_available = False
            self.manual_release_url = ""
        try:
            if self.mode == "source":
                if not self._source_repository():
                    raise RuntimeError("当前目录不是受支持的 naiba-chat Git 仓库")
                current = self._run_git("rev-parse", "HEAD", timeout=10).lower()
                self._run_git("fetch", "--quiet", "origin", "master", timeout=60)
                remote_commit = self._run_git("rev-parse", "origin/master", timeout=10).lower()
                if not re.fullmatch(r"[0-9a-f]{40}", remote_commit):
                    raise RuntimeError("无法读取 origin/master 的提交版本")
                counts = self._run_git(
                    "rev-list", "--left-right", "--count", "HEAD...origin/master", timeout=10
                ).split()
                if len(counts) != 2:
                    raise RuntimeError("无法比较本地与远程版本")
                local_ahead, remote_ahead = (int(value) for value in counts)
                divergent = local_ahead > 0 and remote_ahead > 0
                update_available = remote_ahead > 0 and not divergent
                self.build = {"version": "source", "commit": current}
                with self.lock:
                    self.latest = {
                        "version": "origin/master",
                        "commit": remote_commit,
                        "release_url": f"https://github.com/{REPOSITORY}/commits/master",
                        "update_available": update_available,
                        "release_notes": self._read_release_notes(),
                    }
                    if divergent:
                        self.phase = "error"
                        self.error = "本地与 origin/master 已分叉，请在终端中手动处理"
                    else:
                        self.phase = "available" if update_available else "current"
                    self.checked_at = int(time.time())
                return self.status()
            try:
                releases = self._fetch_releases(force=force)
            except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
                # 列表接口不可用（含网关返回非 JSON 等）且无缓存可回退：以 latest 静态入口为准继续判断。
                releases = []
            list_from_api = not self._releases_from_cache
            installable = [release for release in releases if release["installable"]]
            manifest = None
            if list_from_api and installable:
                latest_meta = installable[0]
                try:
                    manifest = self._fetch_manifest_for_tag(latest_meta["tag"])
                    manifest["published_at"] = latest_meta["published_at"]
                    manifest["release_url"] = latest_meta["release_url"]
                except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
                    # 所选版本资产获取失败（网络类错误/内容异常）：改用 latest 静态直连清单兜底。
                    manifest = None
            if manifest is None:
                # 列表缺失/不可信，或首选版本清单拉取失败：以 /releases/latest/download 静态
                # 直连清单作为本次更新判断与默认安装的唯一权威来源（该入口不消耗 API 配额）。
                manifest = self._fetch_manifest_for_tag(LATEST_TAG)
            current_number = self._build_number(self.build.get("version", ""))
            latest_number = self._build_number(manifest.get("version", ""))
            current_release = self._release_version_key(self.build.get("version", ""))
            latest_release = self._release_version_key(manifest.get("version", ""))
            update_available = False
            if current_number is not None and latest_number is not None:
                # Build numbers prevent downgrades, while a republished build
                # with a different commit must still reach existing clients.
                update_available = (
                    latest_number > current_number
                    or (
                        latest_number == current_number
                        and manifest["commit"] != self.build.get("commit")
                    )
                )
            elif current_release is not None and latest_release is not None:
                update_available = (
                    latest_release > current_release
                    or (
                        latest_release == current_release
                        and manifest["commit"] != self.build.get("commit")
                    )
                )
            # 其它情形（当前构建版本不是可解析的发布版本，例如本地/dev 的 git hash）
            # 无法确认 manifest 是否更新，为免把新构建降级成清单里的旧版本，保持不更新。
            # 2.0.0 is a migration release and must be downloaded manually.
            # Any release older than it is ignored entirely.
            if self._is_manual_migration_version(str(manifest.get("version") or "")):
                self.manual_update_available = True
                self.manual_release_url = MANUAL_RELEASE_URL
                update_available = False
                manifest["manual_update"] = True
                manifest["release_url"] = MANUAL_RELEASE_URL
            elif not self._is_auto_update_version(str(manifest.get("version") or "")):
                update_available = False
            manifest["update_available"] = update_available
            # Once the manifest is authoritative, annotate matching release rows
            # using its commit so republished same-version builds are not shown
            # as the currently installed build.
            for release in self.releases:
                same_version = release.get("version") == self.build.get("version", "")
                republished = (
                    same_version
                    and manifest.get("version") == self.build.get("version", "")
                    and manifest.get("commit") != self.build.get("commit", "")
                )
                release["current"] = bool(same_version and not republished)
            with self.lock:
                self.latest = manifest
                if (
                    manifest.get("tag") == LATEST_TAG
                    and self._is_auto_update_version(str(manifest.get("version") or ""))
                    and not any(
                        release.get("installable") and release.get("version") == manifest.get("version")
                        for release in self.releases
                    )
                ):
                    # 列表来自回退缓存或整体缺失：合成一条可安装的 latest 条目置于顶部，
                    # 保证下拉框仍能一键安装最新版；下次 API 成功后以真实列表整体替换。
                    self.releases = [self._release_from_manifest(manifest), *self.releases]
                self.phase = "available" if update_available else "current"
                self.checked_at = int(time.time())
        except urllib.error.HTTPError as exc:
            with self.lock:
                self.phase = "error"
                self.error = self._http_error_message(exc)
                self.checked_at = int(time.time())
        except urllib.error.URLError:
            with self.lock:
                self.phase = "error"
                self.error = self._with_proxy_context("无法连接更新服务器，请检查网络后重试")
                self.checked_at = int(time.time())
        except Exception as exc:
            with self.lock:
                self.phase = "error"
                self.error = self._with_proxy_context(f"检查更新失败：{exc}")
                self.checked_at = int(time.time())
        return self.status()

    def start_check(self, force: bool = False) -> dict[str, Any]:
        """Start a non-blocking update check and return the current state."""
        with self.lock:
            if self.phase == "checking":
                return self.status()
            self.phase = "checking"
            self.error = ""

        def run() -> None:
            try:
                self.check(force=force)
            except Exception as exc:
                with self.lock:
                    self.phase = "error"
                    self.error = self._with_proxy_context(f"检查更新失败：{exc}")
                    self.checked_at = int(time.time())

        threading.Thread(target=run, name="naiba-update-check", daemon=True).start()
        return self.status()

    def _download(self, latest: dict[str, Any]) -> Path:
        update_dir = self.data_dir / "update"
        update_dir.mkdir(parents=True, exist_ok=True)
        target = update_dir / f"naiba-chat-{latest['commit'][:12]}.download"
        request = urllib.request.Request(
            str(latest["download_url"]),
            headers={"Accept": "application/octet-stream", "User-Agent": "naiba-chat-updater"},
        )
        with net_io.open(request, timeout=120) as response, target.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        if target.stat().st_size < 1024 * 1024 or target.read_bytes()[:2] != b"MZ":
            target.unlink(missing_ok=True)
            raise RuntimeError("下载的更新文件不是有效的 Windows 程序")
        if _sha256(target) != latest["sha256"]:
            target.unlink(missing_ok=True)
            raise RuntimeError("更新文件校验失败，已拒绝安装")
        return target

    def _launch_replacer(self, downloaded: Path) -> None:
        target = Path(sys.executable).resolve()
        update_dir = downloaded.parent
        script = update_dir / "apply-update.ps1"
        backup = update_dir / "naiba-chat.previous.exe"
        script.write_text(APPLY_UPDATE_SCRIPT, encoding="utf-8")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-ProcessId",
                str(os.getpid()),
                "-Downloaded",
                str(downloaded),
                "-Target",
                str(target),
                "-Backup",
                str(backup),
            ],
            cwd=str(self.app_dir),
            creationflags=creation_flags,
        )

    def start_install(
        self, target_tag: str | None = None, on_ready: Callable[[], None] | None = None
    ) -> dict[str, Any]:
        if not self.supported:
            raise RuntimeError("当前运行方式不支持自动更新")
        if self.mode == "source":
            raise RuntimeError("源码模式不支持一键更新，请在终端中执行 git pull --ff-only origin master")
        with self.lock:
            if self.phase in {"downloading", "restarting"}:
                raise RuntimeError("更新正在下载或重启中，请稍候")
        if target_tag:
            meta = next((release for release in self.releases if release["tag"] == target_tag), None)
            if not meta:
                raise RuntimeError("目标版本不存在")
            if not self._is_auto_update_version(str(meta.get("version") or "")):
                if self._is_manual_migration_version(str(meta.get("version") or "")):
                    raise RuntimeError(f"{MANUAL_RELEASE_VERSION[0]}.{MANUAL_RELEASE_VERSION[1]}.{MANUAL_RELEASE_VERSION[2]} 属于大版本更新，请前往 GitHub Release 手动下载：{MANUAL_RELEASE_URL}")
                raise RuntimeError("该版本不支持自动更新")
            # target_tag 为合成条目 LATEST_TAG 时，_fetch_manifest_for_tag 会自动改走
            # releases/latest/download 静态直连（不访问 api.github.com），与 check() 兜底同入口。
            latest = self._fetch_manifest_for_tag(target_tag)
            latest["published_at"] = meta["published_at"]
            latest["release_url"] = meta["release_url"]
            if latest.get("commit") == self.build.get("commit"):
                raise RuntimeError("当前已安装该版本，无需更新")
        else:
            with self.lock:
                latest = dict(self.latest or {})
            if not latest or latest.get("commit") == self.build.get("commit"):
                raise RuntimeError("没有可安装的新版本")
        with self.lock:
            self.phase = "downloading"
            self.error = ""
        self._write_pending(latest)

        def install() -> None:
            try:
                downloaded = self._download(latest)
                self._launch_replacer(downloaded)
                with self.lock:
                    self.phase = "restarting"
                time.sleep(0.5)
                if on_ready:
                    on_ready()
            except Exception as exc:
                with self.lock:
                    self.phase = "error"
                    self.error = self._with_proxy_context(f"安装更新失败：{exc}")

        threading.Thread(target=install, name="naiba-update-install", daemon=True).start()
        return self.status()
