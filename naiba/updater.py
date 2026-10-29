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
import urllib.parse
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
# 只能用旧缓存兜底时的有效期：短到让下一次「检查更新」真的重试，而不是 6 小时不再问。
FALLBACK_TTL_SECONDS = 10 * 60
# 发布列表最多翻几页：API 每页 100 条（5 页覆盖现有全部 tag），
# 静态发布页每页 10 条（3 页 = 30 条，足够补上一次版本跨度）。
MAX_RELEASE_PAGES = 5
HTML_RELEASE_PAGES = 3
# 静态发布页里版本链接的仓库段：仓库改名后 GitHub 页面用新名，但旧地址仍可访问。
_RELEASE_TAG_HREF_RE = re.compile(
    r'href="/[^"/]+/(?:naiba-chat|cat-chat)/releases/tag/([^"?#]+)"'
)
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

# 下载进度在「无新字节」超过该秒数后判定为疑似卡死（前端据此把进度条标黄并提示）。
STALL_SECONDS = 8
# 「已下载、待重启」的落盘标记：下载与校验成功后写入，重启前一直保留，
# 因此下次启动 App 仍能给出「立即重启」入口，不必重新下载。
READY_MARKER = "ready-update.json"


class UpdateCancelled(Exception):
    """用户主动取消下载（内部信号，不作为错误展示）。"""


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


def _is_windows_executable(path: Path) -> bool:
    """只读前两个字节判断 PE 头（MZ）。

    历史实现用 ``path.read_bytes()[:2]``：为了两个字节把整个 exe（上百 MB）读进内存，
    下载校验和每次启动恢复「待重启」状态都要吃一次这个开销。
    """
    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"MZ"
    except OSError:
        return False


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
        # 本次列表的有效期：实时来源用 6 小时，纯缓存兜底降到 10 分钟（见 FALLBACK_TTL_SECONDS）。
        self.releases_ttl_seconds = CACHE_TTL_SECONDS
        # 列表来源说明（前端「来源条」据此如实展示，不再让用户靠版本号跳变自己发现过期）。
        self.releases_meta: dict[str, Any] = {
            "source": "none",
            "fetched_at": 0,
            "legacy_cache": False,
            "truncated": False,
        }
        self.phase = "idle"
        self.error = ""
        self.checked_at = 0
        # True 表示 self.releases 来自磁盘缓存回退（非实时 API），更新决策须以直连清单为准。
        self._releases_from_cache = False
        self.manual_release_url = ""
        self.manual_update_available = False
        # 「更新代理」scoped 覆盖项：仅作用于本模块发起的请求（检查/清单/下载），
        # 与全局 net_io 策略解耦。None / {"mode": "inherit"} 表示跟随全局。
        self.proxy_override: dict[str, Any] | None = None
        # 下载进度（供前端进度条）：received/total 为字节，speed 为字节/秒（约 0.5s 采样）。
        self.download: dict[str, Any] = {
            "received": 0,
            "total": 0,
            "speed": 0.0,
            "started_at": 0.0,
            "updated_at": 0.0,
        }
        self._cancel = threading.Event()
        # 更新产物的磁盘视图（设置页展示「文件在哪、占多大」）：_package_file 是本次下载正在写的
        # .download，校验通过后由 ready 元数据接管；_cleanup_failures 记录删不掉的文件（被占用/无权限），
        # 只用于如实展示，绝不覆盖原始的下载/校验错误。
        self._package_file: Path | None = None
        self._cleanup_failures: dict[str, str] = {}
        self.build = self._read_build_info()
        # 「已下载待重启」状态：从磁盘标记恢复，重启 App 后仍可一键应用。
        self.ready: dict[str, Any] | None = self._load_ready()
        self.pending_verification = self.verify_pending()

    # ---- 更新代理（scoped 覆盖项） ----

    def configure_proxy(self, settings: dict[str, Any] | None) -> None:
        """注入「更新代理」设置；非法值一律按跟随全局处理（不阻断更新能力）。"""
        if not isinstance(settings, dict):
            settings = {}
        mode = str(settings.get("mode") or "inherit").strip().lower()
        if mode not in {"inherit", "system", "direct", "manual"}:
            mode = "inherit"
        with self.lock:
            self.proxy_override = {"mode": mode, "url": str(settings.get("url") or "").strip()}

    def _proxy_note(self) -> str:
        """本次更新实际走哪条路（含 scoped 覆盖项），用于错误文案与状态展示。"""
        override = self.proxy_override
        try:
            info = net_io.describe_override(override)
        except Exception:  # noqa: BLE001 - 诊断信息失败不影响主流程
            return ""
        return str(info.get("note") or "").strip()

    def _open(self, request: urllib.request.Request, timeout: float):
        """本模块唯一的出站入口：统一携带「更新代理」覆盖项。"""
        with self.lock:
            override = dict(self.proxy_override) if self.proxy_override else None
        return net_io.open_scoped(request, override=override, timeout=timeout)

    def _with_proxy_context(self, message: str) -> str:
        """在错误消息末尾附上本次更新实际走的链路，便于用户定位网络问题。"""
        note = self._proxy_note()
        if not note:
            return message
        return f"{message}（更新链路：{note}）"

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
            phase = self.phase
            ready = dict(self.ready or {})
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
                "phase": phase,
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
                # 列表是哪来的 / 什么时候拉的 / 是否可能不全：前端「来源条」直接展示。
                "releases_meta": dict(self.releases_meta),
                "pending_verification": self.pending_verification,
                "source_dirty": self._source_dirty(),
                # 下载进度（前端进度条 / 卡死提示）与取消可用性。
                "download": self._download_snapshot(),
                "can_cancel": phase == "downloading",
                # 产物位置与占用：目录由 data_dir 推导，文件名/体积只读 stat，不读包内容。
                "artifacts": self._artifacts_summary(phase, ready),
                # 已下载待重启：跨重启保留，用户可选「立即重启」或稍后手动重启。
                "ready": {
                    "version": str(ready.get("version") or ""),
                    "commit": str(ready.get("commit") or ""),
                    "downloaded_at": int(ready.get("downloaded_at") or 0),
                } if ready else {},
                "can_apply": bool(ready) and phase not in {"downloading", "restarting"},
                # 本次更新实际走的链路（受「更新代理」影响），设置页直接展示。
                "proxy": {
                    "mode": (self.proxy_override or {}).get("mode", "inherit"),
                    "url": (self.proxy_override or {}).get("url", ""),
                    "note": self._proxy_note(),
                },
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
    def _request_json(url: str, timeout: int = 20, opener=None) -> dict[str, Any] | list[Any]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "naiba-chat-updater",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        open_fn = opener or (lambda req, timeout: net_io.open(req, timeout=timeout))
        with open_fn(request, timeout) as response:
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

    def _read_releases_cache(self, cache: Path) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
        """读取磁盘发布列表缓存，返回 ``(列表, 元信息)``。

        新格式写成 ``{"saved_at", "source", "count", "items"}``；老版本写的是裸数组，
        按 legacy 识别后照旧解析——升级用户不能因为格式变化丢掉历史版本列表。
        """
        meta: dict[str, Any] = {"legacy": False, "saved_at": 0, "source": "cache"}
        try:
            if not cache.is_file():
                return None, meta
            value = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, meta
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            try:
                meta["saved_at"] = int(value.get("saved_at") or 0)
            except (TypeError, ValueError):
                meta["saved_at"] = 0
            meta["source"] = str(value.get("source") or "cache")
            value = value["items"]
        elif isinstance(value, list):
            meta["legacy"] = True
            try:
                meta["saved_at"] = int(cache.stat().st_mtime)
            except OSError:
                pass
        else:
            return None, meta
        if not isinstance(value, list) or not value:
            return None, meta
        releases: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                return None, meta
            tag = str(item.get("tag") or "")
            version = str(item.get("version") or "")
            installable = item.get("installable")
            if not tag or not version or not isinstance(installable, bool):
                return None, meta
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
                    "synthetic": False,
                    "current": False,
                }
            )
        return (self._normalize_releases(releases) or None), meta

    def _load_cached_releases(self, cache: Path) -> list[dict[str, Any]] | None:
        """只要列表的兼容入口（元信息由 ``_read_releases_cache`` 提供）。"""
        return self._read_releases_cache(cache)[0]

    def _write_releases_cache(
        self, cache: Path, releases: list[dict[str, Any]], source: str
    ) -> None:
        """带 meta 落盘；写失败不影响本次结果（缓存只是加速与兜底）。"""
        payload = {
            "saved_at": int(time.time()),
            "source": source,
            "count": len(releases),
            "items": releases,
        }
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

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

    @staticmethod
    def _parse_api_releases(data: list[Any]) -> list[dict[str, Any]]:
        releases: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
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
                    "synthetic": False,
                    "current": False,
                }
            )
        return releases

    def _fetch_releases_api(self) -> tuple[list[dict[str, Any]], bool]:
        """翻页拉取 API 发布列表；返回 ``(列表, 是否因页数上限可能被截断)``。"""
        base = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
        releases: list[dict[str, Any]] = []
        for page in range(1, MAX_RELEASE_PAGES + 1):
            data = self._request_json(f"{base}&page={page}", opener=self._open)
            if not isinstance(data, list):
                raise RuntimeError("更新服务器返回了无效数据")
            releases.extend(self._parse_api_releases(data))
            if len(data) < 100:
                return releases, False
        return releases, True

    def _request_text(self, url: str, timeout: int = 20) -> str:
        """取纯文本/HTML（兜底数据源用）；与 ``_request_json`` 共用「更新代理」出口。"""
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "naiba-chat-updater",
            },
        )
        with self._open(request, timeout) as response:
            raw = response.read()
        return raw.decode("utf-8", errors="replace")

    def _parse_release_tags_html(self, html: str) -> list[dict[str, Any]]:
        """从 github.com 静态发布页解析版本条目；解析不到返回空列表，绝不抛。"""
        if not isinstance(html, str) or not html:
            return []
        releases: list[dict[str, Any]] = []
        seen: set[str] = set()
        matches = list(_RELEASE_TAG_HREF_RE.finditer(html))
        for index, match in enumerate(matches):
            try:
                tag = urllib.parse.unquote(match.group(1)).strip()
            except (TypeError, ValueError):
                continue
            if not tag or tag in seen:
                continue
            version = tag.lstrip("v")
            if self._release_version_key(version) is None:
                continue
            seen.add(tag)
            # 日期是 best-effort：只看「本条发布」的范围（到下一个版本链接为止）。
            # 发布正文可以很长（实测一条正文就能拉开 1 万多个字符），固定窗口会取不到日期；
            # 以下一个版本链接为边界既能跨过长正文，也不会串到下一版的日期。
            boundary = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else min(len(html), match.end() + 40000)
            )
            date_match = re.search(r'datetime="([^"]+)"', html[match.end(): boundary])
            releases.append(
                {
                    "tag": tag,
                    "version": version,
                    "published_at": date_match.group(1) if date_match else "",
                    "release_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
                    # 静态页面只有版本号，没有资产清单：按版本号判定可自动更新（>= 2.0.1）。
                    "release_notes": [],
                    "installable": self._is_auto_update_version(version),
                    "synthetic": False,
                    "current": False,
                }
            )
        return releases

    def _fetch_release_tags_html(self, pages: int = HTML_RELEASE_PAGES) -> list[dict[str, Any]]:
        """兜底数据源：github.com 静态发布页不受 API 配额限制，按页取版本号。

        只做兜底：任何一页失败或结构变化都静默跳过，绝不把异常抛给调用方，也不污染缓存。
        """
        releases: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(1, max(1, int(pages)) + 1):
            url = f"https://github.com/{REPOSITORY}/releases?page={page}"
            try:
                html = self._request_text(url)
            except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
                continue
            page_items = [
                item for item in self._parse_release_tags_html(html) if item["tag"] not in seen
            ]
            if not page_items:
                break
            seen.update(item["tag"] for item in page_items)
            releases.extend(page_items)
            if len(page_items) < 10:
                break
        return releases

    @staticmethod
    def _release_info_score(release: dict[str, Any]) -> tuple[int, int, int, int]:
        """合并时挑「信息更全」的一条：有更新说明 > 说明更多 > 有发布日期 > 可安装。"""
        notes = release.get("release_notes") or []
        return (
            1 if notes else 0,
            len(notes),
            1 if release.get("published_at") else 0,
            1 if release.get("installable") else 0,
        )

    @classmethod
    def _merge_releases(cls, *groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按版本去重合并多路来源；同一版本保留信息更全的一条，缺的字段互相补齐。"""
        merged: dict[str, dict[str, Any]] = {}
        for group in groups:
            for release in group or []:
                if not isinstance(release, dict):
                    continue
                version = str(release.get("version") or "").strip()
                if not version:
                    continue
                key = version.lower()
                candidate = dict(release)
                candidate["current"] = False
                candidate.setdefault("synthetic", False)
                existing = merged.get(key)
                if existing is None:
                    merged[key] = candidate
                    continue
                best, other = (
                    (candidate, existing)
                    if cls._release_info_score(candidate) > cls._release_info_score(existing)
                    else (existing, candidate)
                )
                merged_entry = {**best}
                if not merged_entry.get("release_notes") and other.get("release_notes"):
                    merged_entry["release_notes"] = other["release_notes"]
                for key_name in ("published_at", "release_url"):
                    if not merged_entry.get(key_name) and other.get(key_name):
                        merged_entry[key_name] = other[key_name]
                # 任一路来源认为可安装即可安装：只有 API 能给出真实资产清单，
                # 静态页按版本号判定，旧缓存是上一次 API 的结论——取并集不会凭空造出资产。
                merged_entry["installable"] = bool(
                    best.get("installable") or other.get("installable")
                )
                merged[key] = merged_entry
        return list(merged.values())

    def _set_releases_meta(
        self,
        source: str,
        *,
        fetched_at: int = 0,
        legacy_cache: bool = False,
        truncated: bool = False,
    ) -> None:
        self.releases_meta = {
            "source": source,
            "fetched_at": int(fetched_at or 0),
            "legacy_cache": bool(legacy_cache),
            "truncated": bool(truncated),
        }

    def _fetch_releases(self, force: bool = False) -> list[dict[str, Any]]:
        """返回发布列表。

        数据源按可用性合并：GitHub API（含翻页）→ 静态发布页（配额打满时兜底）→ 磁盘缓存。
        任何一路拿到的条目都先按版本去重，避免「最新一条 + 5 天前旧快照」之间的断层。
        """
        cache = self.data_dir / "update" / "releases.json"
        if not force and self.releases_loaded_at and time.time() - self.releases_loaded_at < self.releases_ttl_seconds:
            return self.releases
        if not force and not self.releases_loaded_at:
            # 启动场景：磁盘缓存较新则直接复用，避免无谓请求 API 消耗配额。
            try:
                cache_fresh = cache.is_file() and time.time() - cache.stat().st_mtime < CACHE_TTL_SECONDS
            except OSError:
                cache_fresh = False
            if cache_fresh:
                cached, cache_meta = self._read_releases_cache(cache)
                if cached is not None:
                    self.releases = self._mark_current(cached)
                    self.releases_loaded_at = int(time.time())
                    self._releases_from_cache = True
                    self.releases_ttl_seconds = CACHE_TTL_SECONDS
                    self._set_releases_meta(
                        "cache",
                        fetched_at=cache_meta.get("saved_at", 0),
                        legacy_cache=cache_meta.get("legacy", False),
                        truncated=True,
                    )
                    return self.releases
        api_error: Exception | None = None
        api_releases: list[dict[str, Any]] = []
        api_truncated = False
        try:
            api_releases, api_truncated = self._fetch_releases_api()
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
            api_error = exc
        html_releases: list[dict[str, Any]] = []
        if not api_releases:
            # API 限流/网络抖动时补静态发布页：不受配额限制，解析失败静默返回空列表。
            html_releases = self._fetch_release_tags_html()
        cached, cache_meta = self._read_releases_cache(cache)
        groups = [group for group in (api_releases, html_releases, cached or []) if group]
        if not groups:
            if api_error is not None:
                raise api_error
            raise RuntimeError("更新服务器返回了无效数据")
        merged = self._normalize_releases(self._merge_releases(*groups))
        if not merged:
            if api_error is not None:
                raise api_error
            raise RuntimeError("更新服务器返回了无效数据")
        self._mark_current(merged)
        source = "api" if api_releases else ("html" if html_releases else "cache")
        if api_releases or html_releases:
            # 实时来源才回写缓存；纯缓存回退不刷新内容与 mtime（否则过期快照会一直「装新」）。
            self._write_releases_cache(cache, merged, source)
        self.releases = merged
        self.releases_loaded_at = int(time.time())
        self._releases_from_cache = source != "api"
        self.releases_ttl_seconds = (
            CACHE_TTL_SECONDS if api_releases or html_releases else FALLBACK_TTL_SECONDS
        )
        self._set_releases_meta(
            source,
            fetched_at=int(time.time()) if api_releases or html_releases else cache_meta.get("saved_at", 0),
            legacy_cache=bool(cache_meta.get("legacy")),
            truncated=bool(api_truncated) if api_releases else True,
        )
        return merged

    def _release_from_manifest(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """依据已校验的 latest 清单生成合成的可安装发布条目，供版本列表一键安装。

        ``synthetic`` 标出「这不是一条真实发布」：它来自静态直连清单而不是发布列表，
        前端据此如实标注「最新 · 来自直连清单」，不让它假装与其它条目同源。
        """
        version = str(manifest.get("version") or "")
        return {
            "tag": LATEST_TAG,
            "version": version,
            "published_at": str(manifest.get("published_at") or ""),
            "release_url": str(manifest.get("release_url") or ""),
            "release_notes": manifest.get("release_notes", []),
            "installable": True,
            "synthetic": True,
            "current": version == self.build.get("version", "") and bool(manifest.get("commit")) and manifest.get("commit") == self.build.get("commit", ""),
        }

    def notes_for_tag(self, tag: str) -> dict[str, Any]:
        """按需补齐某个版本的更新说明（列表来自静态发布页时没有说明）。

        走 ``releases/download/{tag}/naiba-chat-update.json`` 静态直连，不消耗 API 配额；
        拿到后回填内存条目，同一版本不重复请求。
        """
        target = str(tag or "").strip()
        if not target:
            raise RuntimeError("目标版本不存在")
        with self.lock:
            release = next((item for item in self.releases if item.get("tag") == target), None)
        if release is None:
            raise RuntimeError("目标版本不存在")
        notes = [
            str(note).strip()
            for note in (release.get("release_notes") or [])
            if str(note).strip()
        ]
        if not notes:
            if target == LATEST_TAG:
                raise RuntimeError("该版本没有更新说明")
            manifest = self._fetch_manifest_for_tag(target)
            notes = [
                str(note).strip()
                for note in (manifest.get("release_notes") or [])
                if str(note).strip()
            ]
            with self.lock:
                release["release_notes"] = notes
        return {
            "tag": target,
            "version": str(release.get("version") or ""),
            "published_at": str(release.get("published_at") or ""),
            "release_url": str(release.get("release_url") or ""),
            "release_notes": notes,
        }

    def _manifest_base(self, tag: str) -> str:
        """版本下载基址；latest（或空）映射到 releases/latest 静态直连，不消耗 API 配额。"""
        if not tag or tag == LATEST_TAG:
            return f"https://github.com/{REPOSITORY}/releases/latest/download"
        return f"https://github.com/{REPOSITORY}/releases/download/{tag}"

    def _fetch_manifest_for_tag(self, tag: str) -> dict[str, Any]:
        base = self._manifest_base(tag)
        manifest = self._validate_manifest(
            self._request_json(f"{base}/{MANIFEST_ASSET}", opener=self._open)
        )
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
            if not force and self.checked_at and time.time() - self.checked_at < self.releases_ttl_seconds:
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

    # ---- 下载进度 ----

    def _reset_download_progress(self) -> None:
        with self.lock:
            self.download = {
                "received": 0,
                "total": 0,
                "speed": 0.0,
                "started_at": 0.0,
                "updated_at": 0.0,
            }

    def _begin_download(self, total: int) -> None:
        now = time.time()
        with self.lock:
            self.download = {
                "received": 0,
                "total": max(0, int(total or 0)),
                "speed": 0.0,
                "started_at": now,
                "updated_at": now,
            }

    def _advance_download(self, received: int) -> None:
        """按块更新已下载字节，并以 ≥0.5s 的采样间隔估算速度。"""
        now = time.time()
        with self.lock:
            last_at = float(self.download.get("updated_at") or now)
            last_received = int(self.download.get("received") or 0)
            elapsed = now - last_at
            if elapsed >= 0.5:
                self.download["speed"] = max(0.0, (received - last_received) / elapsed)
            self.download["received"] = int(received)
            self.download["updated_at"] = now

    def _download_snapshot(self) -> dict[str, Any]:
        with self.lock:
            data = dict(self.download)
            phase = self.phase
        received = int(data.get("received") or 0)
        total = int(data.get("total") or 0)
        updated_at = float(data.get("updated_at") or 0)
        stalled = bool(
            phase == "downloading"
            and updated_at
            and (time.time() - updated_at) >= STALL_SECONDS
        )
        return {
            "received": received,
            "total": total,
            "speed": float(data.get("speed") or 0.0),
            "percent": round(received / total * 100) if total else 0,
            "stalled": stalled,
        }

    # ---- 更新产物（位置 / 文件名 / 磁盘占用） ----

    def _update_dir(self) -> Path:
        return self.data_dir / "update"

    def _safe_update_path(self, name: str) -> Path | None:
        """把标记里的文件名映射到更新目录内；带目录、绝对路径或空名一律拒绝。

        标记文件是本机数据，但仍按不可信输入处理：``../../x.exe`` 不能越出更新目录。
        """
        text = str(name or "").strip()
        if not text or text != Path(text).name:
            return None
        return self._update_dir() / text

    @staticmethod
    def _file_size(path: Path) -> int:
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0

    def _remove_file(self, path: Path) -> bool:
        """删除更新产物；失败不抛。

        清理发生在下载失败/取消/放弃的收尾路径上，抛出去只会盖掉真正要告诉用户的错误。
        删不掉（被杀毒软件或权限占住）时记下来，界面按「未能清理 + 实际大小」如实展示。
        """
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            with self.lock:
                self._cleanup_failures[str(path)] = str(exc)
            return False
        with self.lock:
            self._cleanup_failures.pop(str(path), None)
        return True

    def _artifacts_summary(self, phase: str, ready: dict[str, Any]) -> dict[str, Any]:
        """设置页「更新产物」一行：目录 + 当前包名 + 实际体积。

        优先级：正在写的包 → 待重启的包 → 删不掉的残留 → 没有产物。只 stat，不读内容。
        """
        directory = self._update_dir()
        active = self._package_file if phase == "downloading" else None
        if active is not None and active.is_file():
            return {
                "directory": str(directory),
                "file_name": active.name,
                "size_bytes": self._file_size(active),
                "state": "downloading",
            }
        ready_path = self._safe_update_path(str(ready.get("file") or "")) if ready else None
        if ready_path is not None and ready_path.is_file():
            return {
                "directory": str(directory),
                "file_name": ready_path.name,
                "size_bytes": self._file_size(ready_path),
                "state": "ready",
            }
        with self.lock:
            leftovers = [Path(item) for item in self._cleanup_failures if Path(item).is_file()]
        if leftovers:
            leftover = leftovers[0]
            return {
                "directory": str(directory),
                "file_name": leftover.name,
                "size_bytes": self._file_size(leftover),
                "state": "cleanup_failed",
            }
        return {
            "directory": str(directory),
            "file_name": "",
            "size_bytes": 0,
            "state": "absent",
        }

    # ---- 取消下载 ----

    def cancel_install(self) -> dict[str, Any]:
        """请求中止正在进行的下载；实际收尾由下载线程完成（前端轮询可见）。"""
        with self.lock:
            if self.phase != "downloading":
                raise RuntimeError("当前没有正在进行的下载")
            self._cancel.set()
        return self.status()

    def _download(self, latest: dict[str, Any]) -> Path:
        update_dir = self._update_dir()
        update_dir.mkdir(parents=True, exist_ok=True)
        target = update_dir / f"naiba-chat-{latest['commit'][:12]}.download"
        request = urllib.request.Request(
            str(latest["download_url"]),
            headers={"Accept": "application/octet-stream", "User-Agent": "naiba-chat-updater"},
        )
        self._cancel.clear()
        with self.lock:
            self._package_file = target
        received = 0
        try:
            with self._open(request, timeout=120) as response, target.open("wb") as output:
                try:
                    total = int(response.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    total = 0
                self._begin_download(total)
                while True:
                    if self._cancel.is_set():
                        raise UpdateCancelled()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    received += len(chunk)
                    self._advance_download(received)
        except BaseException:
            # 断网 / 取消 / 写盘失败都别把半成品留在磁盘上（with 已先关闭文件句柄再删）。
            self._remove_file(target)
            self._clear_package_file(target)
            raise
        if target.stat().st_size < 1024 * 1024 or not _is_windows_executable(target):
            self._remove_file(target)
            self._clear_package_file(target)
            raise RuntimeError("下载的更新文件不是有效的 Windows 程序")
        if _sha256(target) != latest["sha256"]:
            self._remove_file(target)
            self._clear_package_file(target)
            raise RuntimeError("更新文件校验失败，已拒绝安装")
        return target

    def _clear_package_file(self, path: Path) -> None:
        with self.lock:
            if self._package_file == path:
                self._package_file = None

    # ---- 「已下载待重启」状态 ----

    def _ready_marker_path(self) -> Path:
        return self._update_dir() / READY_MARKER

    def _load_ready(self) -> dict[str, Any] | None:
        """启动时恢复「已下载待重启」：标记、文件存在且哈希一致才算数。"""
        marker = self._ready_marker_path()
        if not marker.is_file():
            return None
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            marker.unlink(missing_ok=True)
            return None
        if not isinstance(data, dict):
            marker.unlink(missing_ok=True)
            return None
        commit = str(data.get("commit") or "")
        sha256 = str(data.get("sha256") or "").lower()
        name = str(data.get("file") or "")
        path = self._safe_update_path(name) if name else None
        valid = bool(
            re.fullmatch(r"[0-9a-f]{40}", commit)
            and re.fullmatch(r"[0-9a-f]{64}", sha256)
            and path is not None
            and path.is_file()
            and path.stat().st_size >= 1024 * 1024
            and _is_windows_executable(path)
            and _sha256(path) == sha256
        )
        if not valid:
            # 半成品/损坏/被清理：静默丢弃，让用户重新下载，不留下误导性的待重启入口。
            marker.unlink(missing_ok=True)
            if path is not None:
                self._remove_file(path)
            return None
        if commit == str(self.build.get("commit") or ""):
            # 标记对应的版本已经是当前运行版本（说明更新已应用）：清掉残留。
            marker.unlink(missing_ok=True)
            if path is not None:
                self._remove_file(path)
            return None
        return {
            "version": str(data.get("version") or commit[:7]),
            "commit": commit,
            "sha256": sha256,
            "file": name,
            "downloaded_at": int(data.get("downloaded_at") or 0),
            "release_url": str(data.get("release_url") or ""),
        }

    def _mark_ready(self, latest: dict[str, Any], downloaded: Path) -> None:
        payload = {
            "version": str(latest.get("version") or ""),
            "commit": str(latest.get("commit") or ""),
            "sha256": str(latest.get("sha256") or ""),
            "file": downloaded.name,
            "downloaded_at": int(time.time()),
            "release_url": str(latest.get("release_url") or ""),
        }
        marker = self._ready_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with self.lock:
            self.ready = payload
            self.phase = "ready"
            self.error = ""
            self._package_file = None
            self._reset_download_progress()

    def discard_ready(self) -> dict[str, Any]:
        """放弃已下载的更新：删标记与安装包，回到可再次下载的状态。"""
        with self.lock:
            if self.phase in {"downloading", "restarting"}:
                raise RuntimeError("更新正在下载或重启中，无法放弃")
            ready = dict(self.ready or {})
            self.ready = None
            self.phase = "available" if self._update_pending() else "idle"
        self._ready_marker_path().unlink(missing_ok=True)
        path = self._safe_update_path(str(ready.get("file") or ""))
        if path is not None:
            self._remove_file(path)
        return self.status()

    def _update_pending(self) -> bool:
        latest = self.latest or {}
        return bool(latest.get("update_available", True))

    def apply_ready(self, on_ready: Callable[[], None] | None = None) -> dict[str, Any]:
        """把已下载并校验通过的新版安装包交给替换脚本；由用户点「立即重启」触发。"""
        with self.lock:
            if self.phase in {"downloading", "restarting"}:
                raise RuntimeError("更新正在下载或重启中，请稍候")
            ready = dict(self.ready or {})
        if not ready:
            raise RuntimeError("没有已下载的更新，请先下载")
        path = self._safe_update_path(str(ready.get("file") or ""))
        if path is None or not path.is_file() or _sha256(path) != str(ready.get("sha256") or ""):
            self.discard_ready()
            raise RuntimeError("更新文件已损坏或被清理，请重新下载")
        with self.lock:
            self.phase = "restarting"
            self.error = ""
        # pending 标记只在「真正要安装」这一刻写：此前版本在下载前就写，
        # 下载失败/取消后下次启动会误报「上次更新校验失败」。
        self._write_pending(ready)
        self._launch_replacer(path)

        def restart() -> None:
            time.sleep(0.5)
            if on_ready:
                on_ready()

        threading.Thread(target=restart, name="naiba-update-restart", daemon=True).start()
        return self.status()

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
        """开始下载更新（非阻塞）。下载+校验通过后进入 ``ready`` 态，**不自动重启**；
        真正安装由 :meth:`apply_ready` 在用户点「立即重启」时执行。

        ``on_ready`` 仅为兼容旧调用方保留，当前不再使用（重启回调归 apply_ready）。
        """
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
            self._cancel.clear()
            self._reset_download_progress()

        def install() -> None:
            try:
                downloaded = self._download(latest)
                # 下载与校验都通过后才进入「待重启」：此时既不写 pending、也不启动替换脚本，
                # 由用户在「立即重启 / 稍后重启」之间自己选（§九 更新链路）。
                self._mark_ready(latest, downloaded)
            except UpdateCancelled:
                with self.lock:
                    self.phase = "available" if self._update_pending() else "idle"
                    self.error = ""
                    self._reset_download_progress()
            except Exception as exc:
                with self.lock:
                    self.phase = "error"
                    self.error = self._with_proxy_context(f"安装更新失败：{exc}")
                    self._reset_download_progress()

        threading.Thread(target=install, name="naiba-update-install", daemon=True).start()
        return self.status()
