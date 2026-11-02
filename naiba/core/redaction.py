"""Credential-shaped text masking shared by task display and review inputs.

This is not a permission check and never modifies arguments used by an executor.
It deliberately does not truncate final result bodies.
"""
from __future__ import annotations

import re


def redact_execution_text(value: str) -> str:
    value = re.sub(r"(?i)(bearer\s+)[^\s\"'<>;,]+", r"\1[已隐藏]", value)
    credential = r"(?:api[_-]?key|access[_-]?token|token|secret|password|authorization)"
    value = re.sub(r"(?i)(" + credential + r"[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s;,}\]\"']+)",
                   r"\1[已隐藏]", value)
    value = re.sub(r"(?i)((?:--?|/)" + credential + r"\s+)(?:\"[^\"]*\"|'[^']*'|[^\s;,}\]\"']+)",
                   r"\1[已隐藏]", value)
    value = re.sub(r"(?i)(\$env:[\w]+\s*=\s*)(?:'[^']*'|\"[^\"]*\"|[^;\s]+)", r"\1[已隐藏]", value)
    return re.sub(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[已隐藏]@", value)
