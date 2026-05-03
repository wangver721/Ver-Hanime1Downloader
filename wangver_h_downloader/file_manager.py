"""
断点续传与媒体文件管理：.part 识别、智能重命名、文件名水印剥离。
"""
import html
import re
from pathlib import Path
from typing import Optional

from .config import INVALID_FILENAME_CHARS, PART_SUFFIX


# 站点水印（出现在标题尾部，需在生成文件名前剥离）
_WATERMARKS = [
    r"\s*[\-–—|｜]\s*H動漫裏番線上看\s*[\-–—|｜]\s*Hanime1\.me\s*$",
    r"\s*[\-–—|｜]\s*H動漫裏番線上看\s*$",
    r"\s*[\-–—|｜]\s*Hanime1\.me\s*$",
    r"\s*[\-–—|｜]\s*hanime1\s*$",
    r"\s*-\s*H動漫裏番線上看\s*$",
]


def strip_watermark(name: str) -> str:
    """去掉标题尾部的站点水印（兼容多种破折号与全角竖线）。"""
    if not name:
        return name
    s = html.unescape(name).strip()
    for _ in range(2):
        for pat in _WATERMARKS:
            s = re.sub(pat, "", s, flags=re.I).strip()
    return s


def sanitize_filename(name: str) -> str:
    """生成合法的文件名：水印剥离 + 非法字符替换 + 长度限制。"""
    if not name or not name.strip():
        return "未命名"
    s = strip_watermark(name)
    s = re.sub(INVALID_FILENAME_CHARS, "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(" .")  # Windows 不允许文件名以空格或点结尾
    if not s:
        return "未命名"
    return s[:180] if len(s) > 180 else s


def find_part_file(output_dir: Path, base_name: str, ext: str) -> Optional[Path]:
    """查找断点续传临时文件 {base}{ext}.part 或 {base}.part。"""
    output_dir = Path(output_dir)
    if not output_dir.exists():
        return None
    for cand in (
        output_dir / f"{base_name}{ext}{PART_SUFFIX}",
        output_dir / f"{base_name}{PART_SUFFIX}",
    ):
        if cand.exists():
            return cand
    return None


def build_output_path(output_dir: Path, title: str, ext: str = ".mp4") -> Path:
    """根据标题生成最终输出文件路径。"""
    return Path(output_dir) / f"{sanitize_filename(title)}{ext}"
