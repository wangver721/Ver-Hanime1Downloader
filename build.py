"""
Ver-Hanime1Downloader 一键打包脚本（Windows）。

用法：
    pip install pyinstaller
    python build.py

输出：
    dist/Ver-Hanime1Downloader/Ver-Hanime1Downloader.exe
    （连同其依赖目录，整体压缩后即可发布到 GitHub Releases）
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
APP_NAME = "Ver-Hanime1Downloader"
ENTRY = ROOT / "run.py"
ICON = ROOT / "assets" / "icon.ico"


def _check_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[!] 未检测到 PyInstaller，正在安装…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def _clean() -> None:
    for d in ("build", "dist"):
        p = ROOT / d
        if p.exists():
            print(f"[*] 清理 {p}")
            shutil.rmtree(p, ignore_errors=True)
    for spec in ROOT.glob("*.spec"):
        spec.unlink(missing_ok=True)


def _build() -> None:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", APP_NAME,
        "--windowed",                       # 无控制台窗口
        "--collect-all", "playwright",      # 包含 playwright 的 driver / 资源
        "--collect-data", "rich",           # rich 的样式数据
        "--hidden-import", "PySide6.QtNetwork",
        str(ENTRY),
    ]
    if ICON.exists():
        cmd[cmd.index("--windowed") + 1:cmd.index("--windowed") + 1] = ["--icon", str(ICON)]

    print("[*] 开始打包：")
    print("    " + " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(ROOT))


def _post() -> None:
    out = ROOT / "dist" / APP_NAME
    if out.exists():
        print()
        print(f"[+] 打包完成：{out}")
        print(f"[+] 入口可执行文件：{out / (APP_NAME + '.exe')}")
        print()
        print("发布建议：")
        print(f"  1. 进入 {out.parent} 目录")
        print(f"  2. 将 {APP_NAME} 整个文件夹压缩为 zip")
        print(f"  3. 在 GitHub Releases 中作为产物上传")
        print()
        print("最终用户首次运行需求：")
        print("  - Windows 10/11，64-bit")
        print("  - 已安装 Google Chrome（Playwright 会调用系统 Chrome）")
        print("  - 若提示缺少浏览器，请在 PowerShell 执行：")
        print("      python -m playwright install chromium")
    else:
        print("[!] 未找到打包产物，请检查上方日志。")


def main() -> None:
    _check_pyinstaller()
    _clean()
    _build()
    _post()


if __name__ == "__main__":
    main()
