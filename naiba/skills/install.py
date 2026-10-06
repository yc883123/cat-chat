"""Skill 安装/校验/删除（原 skill_runtime 安装区整区搬移，含 ZIP 炸弹防护与可恢复删除）。"""

from __future__ import annotations

import logging
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Callable

from naiba.skills.catalog import SkillCatalog, _frontmatter_value

logger = logging.getLogger("naiba.skill_runtime")


EventCallback = Callable[[dict[str, Any]], None]

SKILL_POLICY_MODES = {"auto", "pinned", "exclusive"}



# Conservative context ceiling (tokens) used when a provider exposes no window
# (e.g. DeepSeek's /v1/models returns no context-length field, so auto-detection
# yields 0). Rather than silently truncating history — which both drops context
# and re-breaks DeepSeek's token-prefix cache every turn — a conversation is
# blocked with a user-visible notice once it reaches this bound.



# --------------------------------------------------------------------------
# Skill import (folder / ZIP / single .md) validation + recoverable delete
# --------------------------------------------------------------------------

# Validation constants
MAX_FILE_COUNT = 2000
MAX_TOTAL_SIZE = 50 * 1024 * 1024          # 50 MB
ZIP_BOMB_RATIO = 100                        # uncompressed > 100x compressed

# 各上限的内置默认唯一事实来源在 naiba/config.py（SKILL_*_MAX_MB_DEFAULT 常量），
# 这里的 50MB 必须与 SKILL_TOOL_MAX_MB_DEFAULT 一致（守门见 tests/test_skill_size_limit.py）。
# 「Skill 大小上限」设置项 > 0 时由调用方（capability.py）经 max_total_bytes 参数传入放宽值；
# zip 炸弹比率与文件数上限是防攻击边界，不随设置放宽。


def _resolve_total_limit(max_total_bytes: int | None) -> int:
    """归一化总大小上限：None = 内置默认（50MB），其余取正整数。"""
    if max_total_bytes is None:
        return MAX_TOTAL_SIZE
    return max(1, int(max_total_bytes))


class _SkillInstallError(RuntimeError):
    pass


def _path_within(path: Any, root: Any) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _unique_dir(parent: Path, base_name: str) -> Path:
    base = re.sub(r"[^\w.\-]+", "_", str(base_name).strip()) or "skill"
    candidate = parent / base
    if not candidate.exists():
        return candidate
    index = 2
    while (parent / f"{base}_{index}").exists():
        index += 1
    return parent / f"{base}_{index}"


def _folder_has_skill_md(directory: Path) -> bool:
    """SKILL.md present at top level or exactly one level down."""
    if (directory / "SKILL.md").is_file():
        return True
    for child in directory.iterdir():
        if child.is_dir() and (child / "SKILL.md").is_file():
            return True
    return False


def _zip_has_skill_md(archive: zipfile.ZipFile) -> bool:
    for info in archive.infolist():
        parts = Path(info.filename).parts
        if len(parts) in (1, 2) and parts[-1] == "SKILL.md":
            return True
    return False


def _finalize_install(dest: Path, display_name: str | None = None) -> dict[str, Any]:
    catalog = SkillCatalog([dest])
    skills = catalog.scan()
    if not skills:
        raise _SkillInstallError("未能在来源中识别到有效的 Skill 定义")
    skill = skills[0]
    return {
        "success": True,
        "skill_id": skill["id"],
        "name": skill["name"],
        "path": skill["path"],
        "source": "managed",
        "error": None,
    }


def _install_folder(
    src: Path,
    managed_dir: Path,
    name: str | None,
    max_total_bytes: int | None = None,
) -> dict[str, Any]:
    limit = _resolve_total_limit(max_total_bytes)
    file_count = 0
    total = 0
    for item in src.rglob("*"):
        if item.is_file():
            file_count += 1
            total += item.stat().st_size
    if file_count > MAX_FILE_COUNT:
        raise _SkillInstallError(f"文件夹内文件数量过多（超过 {MAX_FILE_COUNT}）")
    if total > limit:
        raise _SkillInstallError(
            f"文件夹总大小超过当前上限（{limit // (1024 * 1024)} MB）。"
            "可在 设置 → Skills 管理 调大「Skill 大小上限」后重试"
        )
    if not _folder_has_skill_md(src):
        raise _SkillInstallError("文件夹缺少 SKILL.md（需位于顶层或下一级目录）")
    dest = _unique_dir(managed_dir, name or src.name)
    shutil.copytree(src, dest)
    return _finalize_install(dest, name)


def _install_zip(
    src: Path,
    managed_dir: Path,
    name: str | None,
    max_total_bytes: int | None = None,
) -> dict[str, Any]:
    limit = _resolve_total_limit(max_total_bytes)
    try:
        archive = zipfile.ZipFile(src)
    except zipfile.BadZipFile as exc:
        raise _SkillInstallError(f"不是有效的 zip 压缩包：{exc}")
    with archive:
        bad = archive.testzip()
        if bad is not None:
            raise _SkillInstallError(f"压缩包损坏：{bad}")
        members = archive.infolist()
        if len(members) > MAX_FILE_COUNT:
            raise _SkillInstallError(f"压缩包内文件数量过多（超过 {MAX_FILE_COUNT}）")
        total_uncompressed = sum(member.file_size for member in members)
        if total_uncompressed > limit:
            raise _SkillInstallError(
                f"压缩包解压后体积超过当前上限（{limit // (1024 * 1024)} MB）。"
                "可在 设置 → Skills 管理 调大「Skill 大小上限」后重试"
            )
        compressed = src.stat().st_size
        if compressed > 0 and total_uncompressed > ZIP_BOMB_RATIO * compressed:
            raise _SkillInstallError("检测到可能的 zip 炸弹（解压体积远超压缩体积）")
        for member in members:
            if member.file_size > limit:
                raise _SkillInstallError(
                    f"压缩包单文件解压后超过当前上限（{limit // (1024 * 1024)} MB）：{member.filename}"
                )
            filename = member.filename
            parts = Path(filename).parts
            if (
                Path(filename).is_absolute()
                or filename.startswith("/")
                or ".." in parts
                or any(":" in part for part in parts)
            ):
                raise _SkillInstallError(f"压缩包包含非法或越界路径：{filename}")
        if not _zip_has_skill_md(archive):
            raise _SkillInstallError("压缩包缺少 SKILL.md（需位于顶层或下一级目录）")
        dest = _unique_dir(managed_dir, name or src.stem)
        dest.mkdir(parents=True, exist_ok=True)
        for member in members:
            target = (dest / member.filename).resolve()
            if target != dest and not _path_within(target, dest):
                raise _SkillInstallError(f"压缩包包含越界路径：{member.filename}")
        archive.extractall(dest)
    return _finalize_install(dest, name)


def _install_single_md(src: Path, managed_dir: Path, name: str | None) -> dict[str, Any]:
    try:
        text = src.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _SkillInstallError(f"无法读取 .md 文件：{exc}")
    md_name = _frontmatter_value(text, "name")
    md_desc = _frontmatter_value(text, "description")
    if not md_name or not md_desc:
        # 单文件导入允许裸 Markdown。为 SkillCatalog 补齐最小元数据，同时保留原正文。
        body_lines = [line.strip() for line in text.splitlines() if line.strip()]
        heading_index = next((index for index, line in enumerate(body_lines) if line.startswith("#")), -1)
        heading = (
            re.sub(r"^#{1,6}\s*", "", body_lines[heading_index]).strip()
            if heading_index >= 0 else ""
        )
        md_name = md_name or heading or src.stem or "skill"
        description_lines = body_lines[heading_index + 1:] if heading_index >= 0 else body_lines
        md_desc = md_desc or next(
            (
                line.lstrip("#>*- ").strip()
                for line in description_lines
                if line not in {"---", "..."}
                and not re.match(r"^(name|description):\s*", line, re.I)
                and not line.startswith("#")
            ),
            "未提供描述",
        )
        md_name = md_name.replace("\n", " ").strip()[:120] or "skill"
        md_desc = md_desc.replace("\n", " ").strip()[:240] or "未提供描述"
        text = f"---\nname: {md_name}\ndescription: {md_desc}\n---\n\n{text.lstrip()}"
    dest = _unique_dir(managed_dir, name or md_name or src.stem)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(text, encoding="utf-8")
    return _finalize_install(dest, name or md_name)


def validate_and_install_skill(
    source_path: Any,
    managed_dir: Any,
    name: str | None = None,
    max_total_bytes: int | None = None,
) -> dict[str, Any]:
    """校验并安装一个 Skill 来源（文件夹 / ZIP / 单个 .md）。

    Args:
        source_path: 本地来源路径（文件夹、.zip 或 .md）。
        managed_dir: 应用托管的 skills 目录，安装目标。
        name: 可选的目标目录名覆盖。
        max_total_bytes: 可选的总大小上限（字节）；None = 内置默认（50MB，
            与 SKILL_TOOL_MAX_MB_DEFAULT 一致）。「Skill 大小上限」设置 > 0 时
            由 capability.py 传入放宽值。

    Returns:
        {"success": True, "skill_id", "name", "path", "source": "managed", "error": None}
        或 {"success": False, "error": str}
    """
    src = Path(source_path).expanduser().resolve()
    managed = Path(managed_dir).expanduser().resolve()
    managed.mkdir(parents=True, exist_ok=True)
    try:
        if src.is_dir():
            return _install_folder(src, managed, name, max_total_bytes)
        if src.suffix.lower() == ".md":
            return _install_single_md(src, managed, name)
        if src.suffix.lower() == ".zip":
            return {"success": False, "error": "压缩包请先使用 unpack_skill_archive 解压到工作区后，再对该文件夹调用 install_skill"}
        return {"success": False, "error": "不支持的来源类型：仅支持文件夹或单个 .md 文件"}
    except _SkillInstallError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def validate_and_extract_archive(
    archive_path: Any,
    target_dir: Any,
    name: str | None = None,
    max_total_bytes: int | None = None,
) -> dict[str, Any]:
    """校验一个 zip 压缩包并在安全校验通过后解压到 target_dir（工作区专属子目录）。

    校验与 ``_install_zip`` 一致：zip 损坏、越界路径（绝对/``..``/盘符/UNC）、
    zip 炸弹比率、条目数/体积上限、解压后必须含 SKILL.md。校验失败抛 ``_SkillInstallError``。

    Args:
        archive_path: zip 压缩包路径。
        target_dir: 解压目标目录（工作区专属子目录）。
        name: 可选的解压子目录名覆盖。
        max_total_bytes: 可选的总大小上限（字节）；None = 内置默认（50MB）。
            「Skill 大小上限」设置 > 0 时由 capability.py 传入放宽值。

    Returns:
        {"success": True, "extracted_dir": str, "root_dir": str, "name": str}
        或 {"success": False, "error": str}
    """
    limit = _resolve_total_limit(max_total_bytes)
    src = Path(archive_path).expanduser().resolve()
    if not src.is_file() or src.suffix.lower() != ".zip":
        return {"success": False, "error": "仅支持 .zip 压缩包（rar/7z 暂不支持，请转成 zip）"}
    target = Path(target_dir).expanduser().resolve()
    try:
        archive = zipfile.ZipFile(src)
    except zipfile.BadZipFile as exc:
        return {"success": False, "error": f"不是有效的 zip 压缩包：{exc}"}
    with archive:
        default_error = None
        try:
            bad = archive.testzip()
            if bad is not None:
                return {"success": False, "error": f"压缩包损坏：{bad}"}
            members = archive.infolist()
            if len(members) > MAX_FILE_COUNT:
                return {"success": False, "error": f"压缩包内文件数量过多（超过 {MAX_FILE_COUNT}）"}
            total_uncompressed = sum(member.file_size for member in members)
            if total_uncompressed > limit:
                return {
                    "success": False,
                    "error": (
                        f"压缩包解压后体积超过当前上限（{limit // (1024 * 1024)} MB）。"
                        "可在 设置 → Skills 管理 调大「Skill 大小上限」后重试"
                    ),
                }
            compressed = src.stat().st_size
            if compressed > 0 and total_uncompressed > ZIP_BOMB_RATIO * compressed:
                return {"success": False, "error": "检测到可能的 zip 炸弹（解压体积远超压缩体积）"}
            for member in members:
                if member.file_size > limit:
                    return {
                        "success": False,
                        "error": (
                            f"压缩包单文件解压后超过当前上限（{limit // (1024 * 1024)} MB）："
                            f"{member.filename}"
                        ),
                    }
                filename = member.filename
                parts = Path(filename).parts
                if (
                    Path(filename).is_absolute()
                    or filename.startswith("/")
                    or ".." in parts
                    or any(":" in part for part in parts)
                ):
                    return {"success": False, "error": f"压缩包包含非法或越界路径：{filename}"}
            if not _zip_has_skill_md(archive):
                return {"success": False, "error": "压缩包缺少 SKILL.md（需位于顶层或下一级目录）"}
            target.mkdir(parents=True, exist_ok=True)
            dest = _unique_dir(target, name or src.stem)
            dest.mkdir(parents=True, exist_ok=True)
            for member in members:
                t = (dest / member.filename).resolve()
                if t != dest and not _path_within(t, dest):
                    return {"success": False, "error": f"压缩包包含越界路径：{member.filename}"}
            archive.extractall(dest)
        except _SkillInstallError as exc:
            default_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            default_error = f"{type(exc).__name__}: {exc}"
        if default_error:
            return {"success": False, "error": default_error}
    # 定位含 SKILL.md 的目录（顶层或下一级）
    head = dest
    if not (dest / "SKILL.md").is_file():
        sub = next(
            (child for child in dest.iterdir() if child.is_dir() and (child / "SKILL.md").is_file()),
            None,
        )
        if sub is not None:
            head = sub
    return {
        "success": True,
        "extracted_dir": str(head),
        "root_dir": str(dest),
        "name": str(head.name),
    }


def remove_skill_references(
    skill_id: str,
    agent_configs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """返回一个新的 agent_configs 列表，移除每个 agent 对 skill_id 的引用。

    不修改传入的列表（调用方负责持久化）。
    """
    updated: list[dict[str, Any]] = []
    for agent in agent_configs:
        new_agent = dict(agent)
        skill_ids = list(agent.get("skill_ids", []))
        if skill_id in skill_ids:
            skill_ids.remove(skill_id)
        new_agent["skill_ids"] = skill_ids
        updated.append(new_agent)
    return updated


def delete_skill(
    skill_id: str,
    recycle_dir: Any,
    agent_configs: list[dict[str, Any]],
    managed_dir: Any,
    skills_by_id: dict[str, dict[str, Any]] | None = None,
    permanently: bool = False,
) -> dict[str, Any]:
    """删除一个托管 Skill，可选择移入回收目录或永久删除。

    Args:
        skill_id: 目标 Skill id。
        recycle_dir: 应用托管的回收目录。
        agent_configs: agent 配置列表，每项含 'id' 与 'skill_ids'。
        managed_dir: 应用托管的 skills 目录（用于校验路径归属）。
        skills_by_id: 可选，预构建的 {id: skill}；缺省时扫描 managed_dir。
        permanently: True 时直接删除托管文件；False 时移入 recycle_dir。

    Returns:
        {"success": True, "skill_id", "name", "recycled_to", "cleaned_agent_refs": [...], "error": None}
        或 {"success": False, "error": str}
    """
    if skills_by_id is None:
        managed = Path(managed_dir).expanduser().resolve()
        skills_by_id = SkillCatalog([managed]).by_id()
    skill = skills_by_id.get(skill_id)
    if not skill:
        return {"success": False, "error": "Skill 不存在或未被托管"}
    cleaned = [
        str(agent["id"])
        for agent in agent_configs
        if skill_id in agent.get("skill_ids", [])
    ]
    # 托管 Skill 移入回收目录；内置/外部 Skill 由上层持久化隐藏，不移动原文件。
    if skill.get("source") != "managed":
        return {
            "success": True,
            "skill_id": skill_id,
            "name": str(skill.get("name") or ""),
            "hidden": True,
            "recycled_to": None,
            "cleaned_agent_refs": cleaned,
            "error": None,
        }

    managed = Path(managed_dir).expanduser().resolve()
    root = Path(str(skill.get("root") or skill.get("path") or "")).expanduser().resolve()
    if root == managed or not _path_within(root, managed):
        # 散装 Skill：定义文件直接放在扫描目录根下（例如单文件导入的 <managed>/SKILL.md），
        # 此时 root 就是扫描目录本身。绝不能整目录移动，否则会把该目录下所有 Skill 一起
        # 搬走；这里退化为只回收这一个定义文件（同样是可恢复删除）。
        skill_file = Path(str(skill.get("path") or "")).expanduser().resolve()
        if skill_file == managed or not _path_within(skill_file, managed) or not skill_file.is_file():
            return {"success": False, "error": "拒绝删除：Skill 定义不在托管目录内，请手动处理"}
        try:
            if permanently:
                skill_file.unlink()
                recycled_to = None
            else:
                recycle = Path(recycle_dir).expanduser().resolve()
                recycle.mkdir(parents=True, exist_ok=True)
                dest = _unique_dir(recycle, str(skill.get("name") or skill_file.stem))
                dest.mkdir(parents=True, exist_ok=True)
                shutil.move(str(skill_file), str(dest / skill_file.name))
                recycled_to = str(dest)
        except OSError as exc:
            operation = "永久删除" if permanently else "移动"
            return {"success": False, "error": f"{operation}失败：{exc}"}
        return {
            "success": True,
            "skill_id": skill_id,
            "name": str(skill.get("name", skill_file.stem)),
            "recycled_to": recycled_to,
            "permanently_deleted": permanently,
            "cleaned_agent_refs": cleaned,
            "error": None,
        }
    try:
        if permanently:
            shutil.rmtree(root)
            recycled_to = None
        else:
            recycle = Path(recycle_dir).expanduser().resolve()
            recycle.mkdir(parents=True, exist_ok=True)
            dest = _unique_dir(recycle, root.name)
            shutil.move(str(root), str(dest))
            recycled_to = str(dest)
    except OSError as exc:
        operation = "永久删除" if permanently else "移动"
        return {"success": False, "error": f"{operation}失败：{exc}"}
    return {
        "success": True,
        "skill_id": skill_id,
        "name": str(skill.get("name", root.name)),
        "recycled_to": recycled_to,
        "permanently_deleted": permanently,
        "cleaned_agent_refs": cleaned,
        "error": None,
    }


def clear_skill_recycle(recycle_dir: Any) -> dict[str, Any]:
    """Permanently clear only direct children of the dedicated Skill recycle directory."""
    recycle = Path(recycle_dir).expanduser().resolve()
    if not recycle.exists():
        return {"success": True, "deleted": 0}
    if not recycle.is_dir():
        return {"success": False, "error": "Skill 回收目录不是文件夹"}
    deleted = 0
    try:
        for item in recycle.iterdir():
            if item.is_symlink() or item.is_file():
                item.unlink()
            else:
                shutil.rmtree(item)
            deleted += 1
    except OSError as exc:
        return {"success": False, "error": f"清空回收目录失败：{exc}"}
    return {"success": True, "deleted": deleted}


def list_skill_recycle(recycle_dir: Any) -> list[dict[str, str]]:
    """List restorable Skill folders directly contained by the recycle directory."""
    recycle = Path(recycle_dir).expanduser().resolve()
    if not recycle.is_dir():
        return []
    entries: list[dict[str, str]] = []
    for item in recycle.iterdir():
        if item.is_symlink() or not item.is_dir() or not (item / "SKILL.md").is_file():
            continue
        try:
            text = (item / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        entries.append({
            "entry": item.name,
            "name": _frontmatter_value(text, "name") or item.name,
            "path": str(item),
        })
    return sorted(entries, key=lambda entry: entry["name"].lower())


def restore_skill_recycle_entry(entry: str, recycle_dir: Any, managed_dir: Any) -> dict[str, Any]:
    """Move one validated recycled Skill folder back into the managed directory."""
    name = str(entry or "").strip()
    if not name or Path(name).name != name or name in {".", ".."}:
        return {"success": False, "error": "回收目录条目无效"}
    recycle = Path(recycle_dir).expanduser().resolve()
    source = (recycle / name).resolve()
    if not _path_within(source, recycle) or source == recycle or source.is_symlink():
        return {"success": False, "error": "回收目录条目无效"}
    if not source.is_dir() or not (source / "SKILL.md").is_file():
        return {"success": False, "error": "回收目录中没有可恢复的 Skill"}
    managed = Path(managed_dir).expanduser().resolve()
    managed.mkdir(parents=True, exist_ok=True)
    dest = _unique_dir(managed, source.name)
    try:
        shutil.move(str(source), str(dest))
    except OSError as exc:
        return {"success": False, "error": f"恢复失败：{exc}"}
    return {"success": True, "restored_to": str(dest)}
