#!/usr/bin/env python3
"""WangVer H-Downloader 入口。"""
import sys

from wangver_h_downloader.cli import main as cli_main

if __name__ == "__main__":
    # 默认启动 GUI；传入 --cli 时回退命令行模式
    if "--cli" in sys.argv:
        cli_main()
    else:
        from wangver_h_downloader.gui import main as gui_main
        gui_main()
