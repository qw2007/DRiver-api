"""仓库卫生：不许把本机路径与个人邮箱写进受版本控制的内容。

这些检查只针对「提交进仓库的文本」，不检查运行时行为；它们存在的理由很具体：
文档里一旦写死 `C:\\Users\\<某人>` 或内网 IP，仓库就不能干净地公开，
而且后来的人会照着抄。发现命中就把它们换成 `<用户目录>` / `<你的局域网 IP>` 之类的占位符。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# 只扫文本类文件；二进制与依赖目录不参与。
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".toml",
    ".yml",
    ".yaml",
    ".json",
    ".txt",
    ".ps1",
    ".sh",
    ".cfg",
    ".ini",
}
SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    ".tmp",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}

PERSONAL_PATH = re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+|D:\\DSh\\|/home/[A-Za-z0-9._-]+/")
PERSONAL_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(outlook|qq|gmail|163|126)\.[A-Za-z]{2,}")
SECRET_LIKE = re.compile(
    r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
)


def _iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(REPO_ROOT).parts):
            continue
        files.append(path)
    return files


def _scan(pattern: re.Pattern[str]) -> list[str]:
    hits: list[str] = []
    for path in _iter_text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:120]}")
    return hits


@pytest.mark.parametrize(
    ("pattern", "what"),
    [
        (PERSONAL_PATH, "本机绝对路径"),
        (PERSONAL_EMAIL, "个人邮箱"),
        (SECRET_LIKE, "疑似密钥"),
    ],
)
def test_repository_text_has_no_personal_or_secret_content(
    pattern: re.Pattern[str], what: str
) -> None:
    hits = _scan(pattern)
    assert not hits, f"仓库里出现了{what}，请改成占位符：\n" + "\n".join(hits)
