"""Generic capability tools: safe local Skill installation / unpack / inspection."""
from __future__ import annotations

from naiba.core.contracts import AppContext, RunContext

import json
from pathlib import Path
from typing import Any

from naiba.skills.install import validate_and_install_skill, validate_and_extract_archive


class CapabilityRuntime:
    """Expose the runtime's capability tools (install / unpack / inspect skill)."""

    def __init__(self, app: AppContext) -> None:
        self.app = app

    def _skill_max_bytes(self) -> int | None:
        """AI 帮装链路的大小上限（字节）：跟随「Skill 大小上限」设置。

        设置 0（内置默认）传 None，skills/install.py 用内置 50MB 常量兜底——
        与引入该设置前逐字一致；设置 > 0 时统一放宽到该值（MB → 字节）。
        """
        limit_mb = self.app.config.get_skill_max_size_mb()
        return limit_mb * 1024 * 1024 if limit_mb > 0 else None

    def install_skill(
        self,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: RunContext | None = None,
    ) -> tuple[bool, str]:
        # 放宽：exclusive 模式下也允许安装（中途导入 Skill），供后续引用/使用。
        source = str(arguments.get("source_path") or "").strip()
        if not source:
            return False, "缺少 source_path"
        destination_raw = str(arguments.get("destination") or "").strip()
        if destination_raw:
            destination = self.app.config._resolve_dir(destination_raw)
        else:
            # 缺省装到托管目录（数据目录的上级 /skills），而不是遗留 APP_DIR/skills。
            destination = self.app.config.resolve_managed_skills_dir()
        result = validate_and_install_skill(
            source,
            destination,
            str(arguments.get("name") or "").strip() or None,
            max_total_bytes=self._skill_max_bytes(),
        )
        if not result.get("success"):
            return False, str(result.get("error") or "Skill 安装失败")

        # 把实际安装目标目录（绝对路径）固化进配置，供后续持久扫描。
        self.app.config.add_skills_dir(str(destination))
        # Register the resolved destination. Relative names must resolve against
        # the app's configured skill root, not the process working directory.
        self.app.catalog.add_directory(destination)
        installed_path = Path(str(result.get("path") or "")).expanduser().resolve()
        installed = next(
            (
                skill for skill in self.app.catalog.scan()
                if Path(str(skill.get("path") or "")).expanduser().resolve() == installed_path
            ),
            None,
        )
        if installed is None:
            installed = next(
                (
                    skill for skill in self.app.catalog.scan()
                    if str(skill.get("name") or "").lower()
                    == str(result.get("name") or "").lower()
                    and Path(str(skill.get("root") or "")).expanduser().resolve()
                    == installed_path.parent.resolve()
                ),
                None,
            )
        if installed and not any(
            Path(str(skill.get("path") or "")).expanduser().resolve()
            == installed_path
            for skill in active_skills
        ):
            active_skills.append(installed)
        return True, json.dumps(result, ensure_ascii=False)

    def unpack_skill_archive(
        self,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: RunContext | None = None,
    ) -> tuple[bool, str]:
        """校验并解压一个 Skill zip 到工作区专属子目录（由后端代码做强校验）。

        AI 拿到解压后的文件夹路径后，再用 install_skill 安装。
        """
        archive_path = str(arguments.get("archive_path") or "").strip()
        if not archive_path:
            return False, "缺少 archive_path"
        name = str(arguments.get("name") or "").strip() or None
        workspace = getattr((run_context or {}).get("executor"), "workspace", None) or ""
        if not workspace:
            return False, "无法确定工作区目录"
        incoming = Path(workspace).expanduser().resolve() / ".skill_incoming"
        result = validate_and_extract_archive(archive_path, incoming, name, max_total_bytes=self._skill_max_bytes())
        if not result.get("success"):
            return False, str(result.get("error") or "解压校验失败")
        return True, json.dumps(result, ensure_ascii=False)

    def inspect_installed_skill(
        self,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: RunContext | None = None,
    ) -> tuple[bool, str]:
        """定位一个已安装 Skill 的文件路径，供后续读取/编辑。仅返回精确命中的 Skill 信息。"""
        query = str(arguments.get("skill") or arguments.get("name") or "").strip()
        if not query:
            return False, "缺少 skill（支持 Skill 名称或 id）"
        ql = query.lower()
        skills = self.app.catalog.scan()
        target = next(
            (
                s for s in skills
                if str(s.get("id") or "") == query
                or str(s.get("name") or "").lower() == ql
                or str(s.get("ref") or "").lower() == ql
            ),
            None,
        )
        if not target:
            return False, f"未找到 Skill：{query}"
        result = {
            "id": str(target.get("id") or ""),
            "name": str(target.get("name") or ""),
            "ref": str(target.get("ref") or ""),
            "description": str(target.get("description") or ""),
            "path": str(target.get("path") or ""),
            "root": str(target.get("root") or ""),
            "char_count": int(target.get("char_count") or 0),
            "requires_mcp": bool(target.get("requires_mcp")),
            "active": any(str(s.get("id") or "") == str(target.get("id")) for s in active_skills),
        }
        return True, json.dumps(result, ensure_ascii=False)

    def tool_handlers(self) -> dict[str, Any]:
        return {
            "install_skill": self.install_skill,
            "unpack_skill_archive": self.unpack_skill_archive,
            "inspect_installed_skill": self.inspect_installed_skill,
        }
