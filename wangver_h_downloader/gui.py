"""
WangVer H-Downloader 桌面 GUI。
浅色 Apple 风格，圆润极简。
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, QTimer, QSize, Signal, QUrl
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDropEvent, QIcon, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .browser_cf import BrowserCFHandler, SessionCredentials
from .config import (
    DEFAULT_CHUNK_THREADS,
    DEFAULT_MAX_CONCURRENT_TASKS,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_QUALITY,
    DEFAULT_USER_DATA_DIR,
    QUALITY_OPTIONS,
)
from .downloader import DownloadCancelled, download_task
from .file_manager import sanitize_filename
from .parser import (
    VideoTarget,
    collect_urls_from_batch_file,
    extract_list_page_video_links,
    parse_single_page_html,
)


# ───────────────────────────────────────────────
#  工具函数
# ───────────────────────────────────────────────

def _fmt_size(b: float) -> str:
    if b < 1024: return f"{b:.0f} B"
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


def _fmt_eta(sec: float) -> str:
    if sec <= 0 or sec > 99 * 3600: return "--"
    sec = int(sec)
    if sec < 60: return f"{sec}秒"
    if sec < 3600: return f"{sec//60}分{sec%60:02d}秒"
    return f"{sec//3600}时{(sec%3600)//60:02d}分"


class ElideLabel(QLabel):
    """单行 QLabel，宽度不够时自动省略号截断（保留完整 tooltip）。"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full = text
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)
        if text:
            self.setToolTip(text)

    def setText(self, text: str) -> None:
        self._full = text or ""
        self.setToolTip(self._full)
        self._update()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update()

    def _update(self) -> None:
        fm = self.fontMetrics()
        elided = fm.elidedText(self._full, Qt.TextElideMode.ElideRight, max(0, self.width() - 4))
        super().setText(elided)


def _open_in_explorer(path: str) -> bool:
    """在系统文件管理器中打开（并选中）目标文件或目录。"""
    try:
        p = Path(path)
        if not p.exists():
            return False
        if sys.platform == "win32":
            if p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                os.startfile(str(p))
        elif sys.platform == "darwin":
            if p.is_file():
                subprocess.Popen(["open", "-R", str(p)])
            else:
                subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p.parent if p.is_file() else p)])
        return True
    except Exception:
        return False


# ───────────────────────────────────────────────
#  后台线程
# ───────────────────────────────────────────────

class ParseWorker(QThread):
    status = Signal(str)
    progress = Signal(int, int)
    found = Signal(object)
    done = Signal(object, object)
    failed = Signal(str)

    def __init__(self, text: str, quality: str, ud: Path, expand_playlist: bool = True):
        super().__init__()
        self._text, self._q, self._ud = text, quality, ud
        self._expand = expand_playlist

    def run(self):
        try:
            asyncio.run(self._go())
        except Exception as e:
            self.failed.emit(str(e) or repr(e))

    def _entries(self) -> list[str]:
        lines = [l.strip() for l in self._text.splitlines() if l.strip()]
        if len(lines) == 1:
            p = Path(lines[0]).expanduser()
            if p.exists() and p.suffix.lower() == ".txt":
                return collect_urls_from_batch_file(p.resolve())
        return [l for l in lines if l.startswith("http")]

    async def _go(self):
        entries = self._entries()
        if not entries:
            raise ValueError("请输入有效的视频链接")
        h = BrowserCFHandler(user_data_dir=self._ud, headless=False,
                             on_cf_triggered=lambda m: self.status.emit(m))
        creds = None
        cache, urls, seen, targets = {}, [], set(), []
        await h.start()
        try:
            for i, e in enumerate(entries, 1):
                self.status.emit(f"收集链接 {i}/{len(entries)}")
                creds = await h.goto_and_handle_cf(e, wait_for_enter=False)
                html_text = await h.get_page_content()
                pus = (extract_list_page_video_links(html_text, e) or [e]) if self._expand else [e]
                for u in pus:
                    if u not in seen:
                        seen.add(u); urls.append(u)
                if e in pus:
                    cache[e] = html_text
            if not urls:
                raise RuntimeError("未找到可下载链接")
            n = len(urls)
            for i, u in enumerate(urls, 1):
                self.status.emit(f"解析 {i}/{n}")
                ph = cache.get(u)
                if not ph:
                    creds = await h.goto_and_handle_cf(u, wait_for_enter=False)
                    ph = await h.get_page_content()
                t = parse_single_page_html(ph, u, preferred_quality=self._q)
                if t:
                    targets.append(t); self.found.emit(t)
                self.progress.emit(i, n)
            if not targets:
                raise RuntimeError("没有解析到有效直链")
            self.done.emit(targets, creds)
        finally:
            await h.close()


class DownloadWorker(QThread):
    """下载线程。

    prog 信号：(url, received, total, speed_bps, eta_sec)
    支持取消：cancel_all() / cancel_url(url)，会让 progress_callback 抛 DownloadCancelled
    """
    status = Signal(str)
    prog = Signal(str, int, object, float, float)
    ok = Signal(str, str)
    fail = Signal(str, str)
    overall = Signal(int, int)
    finished = Signal(object)

    def __init__(self, targets: list[VideoTarget], creds, base_out: Path,
                 tasks: int, chunks: int, group_by_author: bool):
        super().__init__()
        self._targets = targets
        self._creds = creds
        self._base = base_out
        self._tasks = tasks
        self._chunks = chunks
        self._group = group_by_author
        self._cancel_all_flag = False
        self._cancel_urls: set[str] = set()

    def cancel_all(self):
        self._cancel_all_flag = True

    def cancel_url(self, url: str):
        self._cancel_urls.add(url)

    def run(self):
        try:
            self.finished.emit(asyncio.run(self._go()))
        except Exception as e:
            self.status.emit(f"下载异常：{str(e) or repr(e)}")
            self.finished.emit([])

    def _resolve_dir(self, target: VideoTarget) -> Path:
        d = self._base / sanitize_filename(target.author) if (self._group and target.author) else self._base
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _go(self) -> list[str]:
        sem = asyncio.Semaphore(self._tasks)
        success: list[str] = []
        done_count = 0
        total = len(self._targets)

        async def one(t: VideoTarget):
            nonlocal done_count
            async with sem:
                # 进入前先看是否已被取消
                if self._cancel_all_flag or t.url in self._cancel_urls:
                    self.fail.emit(t.url, "已取消")
                    done_count += 1
                    self.overall.emit(done_count, total)
                    return

                start_ts = time.monotonic()
                last_emit = 0.0
                last_recv = 0
                last_speed_ts = start_ts
                received = 0
                known_total: Optional[int] = None
                speed = 0.0

                def cb(delta: int, total_bytes):
                    nonlocal received, known_total, last_emit, last_recv, last_speed_ts, speed
                    if self._cancel_all_flag or t.url in self._cancel_urls:
                        raise DownloadCancelled()
                    received += delta
                    if total_bytes and total_bytes > 0:
                        known_total = total_bytes
                    now = time.monotonic()
                    if now - last_speed_ts >= 0.5:
                        speed = (received - last_recv) / (now - last_speed_ts)
                        last_recv = received
                        last_speed_ts = now
                    if delta == 0 or now - last_emit >= 0.15:
                        eta = (known_total - received) / speed if (known_total and speed > 0) else 0
                        self.prog.emit(t.url, received, known_total, speed, eta)
                        last_emit = now

                try:
                    out_dir = self._resolve_dir(t)
                    p = await download_task(
                        t.direct_url, t.title, out_dir, self._creds,
                        chunk_threads=self._chunks, progress_callback=cb,
                    )
                    self.prog.emit(t.url, received, known_total, 0.0, 0.0)
                    self.ok.emit(t.url, str(p))
                    success.append(t.title)
                except DownloadCancelled:
                    self.fail.emit(t.url, "已取消")
                except Exception as e:
                    msg = str(e)
                    if hasattr(e, "response") and hasattr(e.response, "status_code"):
                        msg = f"HTTP {e.response.status_code}"
                    self.fail.emit(t.url, msg or type(e).__name__)
                finally:
                    done_count += 1
                    self.overall.emit(done_count, total)

        await asyncio.gather(*[one(t) for t in self._targets])
        return success


# ───────────────────────────────────────────────
#  解析模式询问对话框
# ───────────────────────────────────────────────

class ParseModeDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("解析模式")
        self.setModal(True)
        self.setFixedWidth(440)
        self.mode: Optional[str] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(14)

        title = QLabel("如何解析这个链接？")
        title.setObjectName("dlgTitle")
        lay.addWidget(title)

        sub = QLabel("如果链接所在页面包含播放列表（同作者 / 同系列），\n可以一次性解析全部视频。")
        sub.setObjectName("dlgSub")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        b1 = QPushButton("仅这一个视频")
        b1.clicked.connect(lambda: self._pick("single"))
        b2 = QPushButton("整个播放列表")
        b2.setObjectName("primary")
        b2.setDefault(True)
        b2.clicked.connect(lambda: self._pick("playlist"))
        btns.addWidget(b1, stretch=1)
        btns.addWidget(b2, stretch=1)
        lay.addLayout(btns)

    def _pick(self, m: str):
        self.mode = m
        self.accept()


# ───────────────────────────────────────────────
#  视频卡片
# ───────────────────────────────────────────────

class VideoCard(QFrame):
    cancel_clicked = Signal(str)
    open_clicked = Signal(str)

    CARD_HEIGHT = 268
    COVER_HEIGHT = 152

    def __init__(self, target: VideoTarget):
        super().__init__()
        self.target = target
        self.saved_path: str = ""
        self.state: str = "wait"
        self.setObjectName("card")
        self.setFixedHeight(self.CARD_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 封面
        self.cover = QLabel()
        self.cover.setFixedHeight(self.COVER_HEIGHT)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setObjectName("cover")
        root.addWidget(self.cover)

        # 进度薄条（紧贴封面下沿）
        self.bar = QProgressBar()
        self.bar.setRange(0, 100); self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.bar.setObjectName("thinBar")
        root.addWidget(self.bar)

        # 文字区
        body = QVBoxLayout()
        body.setContentsMargins(14, 10, 14, 12)
        body.setSpacing(6)
        root.addLayout(body)

        # 标题行：勾选框 + 标题（单行 elide）
        head = QHBoxLayout()
        head.setSpacing(8)
        self.chk = QCheckBox()
        self.chk.setChecked(True)
        head.addWidget(self.chk, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.lbl_title = ElideLabel(target.title)
        self.lbl_title.setObjectName("cTitle")
        self.lbl_title.setFixedHeight(20)
        head.addWidget(self.lbl_title, stretch=1)
        body.addLayout(head)

        # 作者行 + 操作按钮
        meta1 = QHBoxLayout()
        meta1.setSpacing(6)
        self.lbl_author = ElideLabel(target.author or "")
        self.lbl_author.setObjectName("cMeta")
        self.lbl_author.setFixedHeight(16)
        meta1.addWidget(self.lbl_author, stretch=1)
        self.btn_act = QToolButton()
        self.btn_act.setObjectName("cardActBtn")
        self.btn_act.setVisible(False)
        self.btn_act.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_act.setFixedHeight(24)
        self.btn_act.clicked.connect(self._on_act)
        meta1.addWidget(self.btn_act, alignment=Qt.AlignmentFlag.AlignRight)
        body.addLayout(meta1)

        # 状态信息独占一行（避免被按钮挤压）
        self.lbl_info = ElideLabel("待下载")
        self.lbl_info.setObjectName("cInfo")
        self.lbl_info.setFixedHeight(16)
        body.addWidget(self.lbl_info)
        body.addStretch(1)

    def _on_act(self):
        if self.state == "act":
            self.cancel_clicked.emit(self.target.url)
        elif self.state == "ok":
            self.open_clicked.emit(self.target.url)

    def is_selected(self): return self.chk.isChecked()

    def set_cover(self, px: QPixmap):
        w = max(self.cover.width(), 300)
        scaled = px.scaled(w, 152, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
        self.cover.setPixmap(scaled)

    def mark_wait(self):
        self.state = "wait"
        self.lbl_info.setText("排队中"); self.lbl_info.setProperty("s", "")
        self.bar.setValue(0)
        self.btn_act.setVisible(False)
        self._restyle()

    def mark_prog(self, recv: int, total, speed: float = 0.0, eta: float = 0.0):
        self.state = "act"
        if total and total > 0:
            pct = min(100, int(recv * 100 / total))
            self.bar.setRange(0, 100); self.bar.setValue(pct)
            parts = [f"{_fmt_size(recv)} / {_fmt_size(total)}", f"{pct}%"]
        else:
            self.bar.setRange(0, 0)
            parts = [_fmt_size(recv)]
        if speed > 0: parts.append(f"{_fmt_size(speed)}/s")
        if eta > 0:   parts.append(f"剩 {_fmt_eta(eta)}")
        self.lbl_info.setText("  ·  ".join(parts))
        self.lbl_info.setProperty("s", "act")
        self.btn_act.setText("✕"); self.btn_act.setToolTip("取消下载")
        self.btn_act.setProperty("kind", "cancel"); self.btn_act.setVisible(True)
        self._restyle()
        self._restyle_btn()

    def mark_ok(self, path: str):
        self.state = "ok"
        self.saved_path = path
        self.bar.setRange(0, 100); self.bar.setValue(100)
        self.lbl_info.setText("✓ 已完成"); self.lbl_info.setProperty("s", "ok")
        self.btn_act.setText("📁"); self.btn_act.setToolTip("打开所在文件夹")
        self.btn_act.setProperty("kind", "open"); self.btn_act.setVisible(True)
        self._restyle()
        self._restyle_btn()

    def mark_err(self, msg: str):
        self.state = "err"
        self.bar.setRange(0, 100)
        is_cancel = "取消" in msg
        if is_cancel: self.state = "cancelled"
        self.lbl_info.setText(msg[:60])
        self.lbl_info.setProperty("s", "cancelled" if is_cancel else "err")
        self.btn_act.setVisible(False)
        self._restyle()

    def _restyle(self):
        self.lbl_info.style().unpolish(self.lbl_info)
        self.lbl_info.style().polish(self.lbl_info)

    def _restyle_btn(self):
        self.btn_act.style().unpolish(self.btn_act)
        self.btn_act.style().polish(self.btn_act)


# ───────────────────────────────────────────────
#  表格状态行 widget（嵌入到表格状态列）
# ───────────────────────────────────────────────

class RowStatusWidget(QWidget):
    cancel_clicked = Signal(str)
    open_clicked = Signal(str)

    def __init__(self, url: str):
        super().__init__()
        self._url = url
        self.state = "wait"
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedHeight(60)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 8, 6, 8)
        lay.setSpacing(8)

        self.bar = QProgressBar()
        self.bar.setFixedSize(80, 6)
        self.bar.setRange(0, 100); self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setObjectName("rowBar")
        self.bar.setVisible(False)
        lay.addWidget(self.bar, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.lbl = ElideLabel("待下载")
        self.lbl.setObjectName("rowInfo")
        self.lbl.setFixedHeight(18)
        lay.addWidget(self.lbl, stretch=1, alignment=Qt.AlignmentFlag.AlignVCenter)

        self.btn = QToolButton()
        self.btn.setObjectName("rowActBtn")
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.setFixedHeight(24)
        self.btn.setVisible(False)
        self.btn.clicked.connect(self._on_btn)
        lay.addWidget(self.btn, alignment=Qt.AlignmentFlag.AlignVCenter)

    def _on_btn(self):
        if self.state == "act":
            self.cancel_clicked.emit(self._url)
        elif self.state == "ok":
            self.open_clicked.emit(self._url)

    def set_wait(self):
        self.state = "wait"
        self.bar.setValue(0); self.bar.setVisible(False)
        self.lbl.setText("排队中"); self.lbl.setProperty("s", ""); self._restyle()
        self.btn.setVisible(False)

    def set_progress(self, recv: int, total, speed: float, eta: float):
        self.state = "act"
        self.bar.setVisible(True)
        if total and total > 0:
            pct = min(100, int(recv * 100 / total))
            self.bar.setRange(0, 100); self.bar.setValue(pct)
            text = f"{pct}%"
            if speed > 0: text += f" · {_fmt_size(speed)}/s"
            self.lbl.setToolTip(
                f"已下载 {_fmt_size(recv)} / {_fmt_size(total)}\n"
                f"速度 {_fmt_size(speed)}/s\n"
                f"剩余 {_fmt_eta(eta) if eta > 0 else '--'}"
            )
        else:
            self.bar.setRange(0, 0)
            text = _fmt_size(recv) + (f" · {_fmt_size(speed)}/s" if speed > 0 else "")
        self.lbl.setText(text); self.lbl.setProperty("s", "act"); self._restyle()
        self.btn.setText("✕"); self.btn.setToolTip("取消下载"); self.btn.setVisible(True)

    def set_done(self):
        self.state = "ok"
        self.bar.setRange(0, 100); self.bar.setValue(100); self.bar.setVisible(True)
        self.lbl.setText("✓ 已完成"); self.lbl.setProperty("s", "ok"); self._restyle()
        self.btn.setText("📁"); self.btn.setToolTip("打开所在文件夹"); self.btn.setVisible(True)

    def set_error(self, msg: str):
        is_cancel = "取消" in msg
        self.state = "cancelled" if is_cancel else "err"
        self.bar.setVisible(False)
        self.lbl.setText(msg[:50])
        self.lbl.setProperty("s", "cancelled" if is_cancel else "err"); self._restyle()
        self.btn.setVisible(False)

    def set_idle_text(self, text: str):
        self.lbl.setText(text)

    def _restyle(self):
        self.lbl.style().unpolish(self.lbl); self.lbl.style().polish(self.lbl)


# ───────────────────────────────────────────────
#  样式表
# ───────────────────────────────────────────────

_S = """
* {
    font-family: "SF Pro Display", "SF Pro Text", "PingFang SC",
                 "Microsoft YaHei UI", "Segoe UI Variable", sans-serif;
    font-size: 13px; color: #1D1D1F;
}
QMainWindow { background: #F5F5F7; }

QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 5px; margin: 2px 0; }
QScrollBar::handle:vertical {
    background: rgba(0,0,0,0.12); border-radius: 2px; min-height: 28px;
}
QScrollBar::handle:vertical:hover { background: rgba(0,0,0,0.22); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QTextEdit, QLineEdit, QPlainTextEdit {
    background: #FFFFFF; border: 1px solid #D2D2D7; border-radius: 12px;
    padding: 8px 14px;
    selection-background-color: #0071E3; selection-color: #FFFFFF;
    color: #1D1D1F;
}
QTextEdit:focus, QLineEdit:focus { border-color: #0071E3; }

QComboBox {
    background: #FFFFFF; border: 1px solid #D2D2D7; border-radius: 10px;
    padding: 5px 12px; color: #1D1D1F;
}
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background: #FFFFFF; border: 1px solid #D2D2D7; border-radius: 8px;
    selection-background-color: #0071E3; selection-color: #FFF; outline: 0;
}
QSpinBox {
    background: #FFFFFF; border: 1px solid #D2D2D7; border-radius: 10px;
    padding: 5px 8px; color: #1D1D1F;
}
QCheckBox::indicator {
    width: 16px; height: 16px; border-radius: 4px;
    border: 1.5px solid #C7C7CC; background: #FFFFFF;
}
QCheckBox::indicator:checked { background: #0071E3; border: 1.5px solid #0071E3; }

QPushButton {
    background: #FFFFFF; border: 1px solid #D2D2D7; border-radius: 10px;
    padding: 7px 18px; font-weight: 500; color: #1D1D1F;
}
QPushButton:hover { background: #F0F0F5; }
QPushButton:pressed { background: #E8E8ED; }
QPushButton:disabled { color: #AEAEB2; background: #FAFAFC; border-color: #E5E5EA; }

#primary {
    background: #0071E3; border: none; color: #FFFFFF; font-weight: 600;
    border-radius: 10px;
}
#primary:hover { background: #0077ED; }
#primary:pressed { background: #006ADB; }
#primary:disabled { background: #B4D7F5; color: #FFFFFF; }

#green {
    background: #34C759; border: none; color: #FFFFFF; font-weight: 600;
    border-radius: 10px;
}
#green:hover { background: #30D158; }
#green:pressed { background: #28A745; }
#green:disabled { background: #A8E6B8; color: #FFFFFF; }

#danger {
    background: #FFFFFF; border: 1px solid #FF453A; color: #FF453A; font-weight: 600;
    border-radius: 10px; padding: 6px 14px;
}
#danger:hover { background: #FFF1F0; }
#danger:pressed { background: #FFE0DD; }

#viewToggle { padding: 6px 12px; }
#viewToggle[active="true"] {
    background: #E8F0FE; border-color: #B6D2F7; color: #0071E3;
}

#dirBtn { padding: 6px 14px; min-width: 56px; }

QDialog { background: #FFFFFF; border-radius: 14px; }
#dlgTitle { font-size: 17px; font-weight: 700; color: #1D1D1F; }
#dlgSub   { font-size: 13px; color: #6E6E73; line-height: 1.5; }

QProgressBar {
    background: #E5E5EA; border: none; border-radius: 3px;
    text-align: center; color: #1D1D1F; font-size: 10px;
}
QProgressBar::chunk { background: #0071E3; border-radius: 3px; }

#thinBar { background: #E5E5EA; border-radius: 0; }
#thinBar::chunk { background: #0071E3; border-radius: 0; }

#rowBar { background: #E5E5EA; border-radius: 3px; }
#rowBar::chunk { background: #0071E3; border-radius: 3px; }

/* 卡片操作按钮（取消 / 打开） */
#cardActBtn, #rowActBtn {
    background: #F2F2F7;
    border: 1px solid #E5E5EA;
    border-radius: 12px;
    padding: 2px 10px;
    color: #1D1D1F;
    font-size: 12px;
    min-width: 28px; min-height: 22px;
}
#cardActBtn:hover, #rowActBtn:hover { background: #E5E5EA; }
#cardActBtn[kind="cancel"] {
    background: #FFFFFF; border-color: #FFD6D3; color: #FF3B30;
}
#cardActBtn[kind="cancel"]:hover { background: #FFF1F0; }
#cardActBtn[kind="open"] {
    background: #E8F0FE; border-color: #B6D2F7; color: #0071E3;
}
#cardActBtn[kind="open"]:hover { background: #D9E7FE; }

#card {
    background: #FFFFFF; border: 1px solid #E5E5EA; border-radius: 16px;
}
#card:hover { border-color: #C7C7CC; }

#cover {
    background: #F2F2F7;
    border-top-left-radius: 16px; border-top-right-radius: 16px;
    color: #C7C7CC; font-size: 11px;
}

#cTitle { font-size: 13px; font-weight: 600; color: #1D1D1F; }
#cMeta  { font-size: 11px; color: #8E8E93; }
#cInfo  { font-size: 11px; color: #8E8E93; }
#cInfo[s="act"]       { color: #0071E3; }
#cInfo[s="ok"]        { color: #34C759; font-weight: 600; }
#cInfo[s="err"]       { color: #FF3B30; }
#cInfo[s="cancelled"] { color: #8E8E93; }

#rowInfo { font-size: 12px; color: #8E8E93; }
#rowInfo[s="act"]       { color: #0071E3; }
#rowInfo[s="ok"]        { color: #34C759; font-weight: 600; }
#rowInfo[s="err"]       { color: #FF3B30; }
#rowInfo[s="cancelled"] { color: #8E8E93; }

#heading { font-size: 24px; font-weight: 700; color: #1D1D1F; letter-spacing: -0.3px; }
#sub     { font-size: 13px; color: #8E8E93; }
#stat    { font-size: 12px; color: #8E8E93; }
#clip    { font-size: 12px; color: #0071E3; }
#label   { font-size: 12px; color: #8E8E93; font-weight: 500; }
#logBox  {
    background: #FFFFFF; border: 1px solid #E5E5EA; border-radius: 12px;
    font-size: 11px; color: #636366;
}

#settingsRow {
    background: #FFFFFF; border: 1px solid #E5E5EA; border-radius: 14px;
}

#tableView {
    background: #FFFFFF; border: 1px solid #E5E5EA; border-radius: 12px;
    outline: 0; font-size: 12px;
    show-decoration-selected: 1;
}
#tableView::item { padding: 6px 4px; border-bottom: 1px solid #F2F2F7; }
#tableView::item:hover { background: #F5F5F7; }
#tableView::item:selected { background: #E8F0FE; color: #1D1D1F; }
#tableView::indicator {
    width: 16px; height: 16px;
    border: 1.5px solid #C7C7CC; border-radius: 4px; background: #FFFFFF;
}
#tableView::indicator:checked { background: #0071E3; border: 1.5px solid #0071E3; }
QHeaderView::section {
    background: #FAFAFA; border: none; border-bottom: 1px solid #E5E5EA;
    padding: 6px 8px; font-size: 11px; font-weight: 600; color: #8E8E93;
}
"""


# ───────────────────────────────────────────────
#  主窗口
# ───────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ver-Hanime1Downloader")
        self.resize(1100, 840)
        self.setAcceptDrops(True)
        self.setStyleSheet(_S)

        self.targets: list[VideoTarget] = []
        self.cards: list[VideoCard] = []
        self.cards_by_url: dict[str, VideoCard] = {}
        self.row_widgets: dict[str, RowStatusWidget] = {}
        self.creds: Optional[SessionCredentials] = None
        self.ok_urls: set[str] = set()
        self.fail_urls: set[str] = set()
        self.pw: Optional[ParseWorker] = None
        self.dw: Optional[DownloadWorker] = None
        self.cover_cache: dict[str, QPixmap] = {}
        self.pending_covers: dict[str, list[VideoCard]] = defaultdict(list)
        self.net = QNetworkAccessManager(self)
        self.last_clip = ""

        self._build()
        QTimer.singleShot(0, lambda: self._ct.start(800))
        self._set_view_active(0)

    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        m = QVBoxLayout(root)
        m.setContentsMargins(32, 28, 32, 24)
        m.setSpacing(0)

        h = QLabel("Ver-Hanime1Downloader"); h.setObjectName("heading")
        m.addWidget(h)
        m.addSpacing(2)
        s = QLabel("粘贴链接 · 预览封面 · 勾选下载 · 自动按作者归档"); s.setObjectName("sub")
        m.addWidget(s)
        m.addSpacing(20)

        self.inp = QTextEdit()
        self.inp.setPlaceholderText("粘贴视频链接（支持多行），或输入 .txt 文件路径")
        self.inp.setFixedHeight(68)
        m.addWidget(self.inp)
        m.addSpacing(6)

        cr = QHBoxLayout()
        self.clip_lbl = QLabel(); self.clip_lbl.setObjectName("clip")
        self.clip_lbl.setVisible(False)
        cr.addWidget(self.clip_lbl); cr.addStretch(1)
        m.addLayout(cr)
        m.addSpacing(10)

        br = QHBoxLayout(); br.setSpacing(8)
        self.b_parse = QPushButton("解析"); self.b_parse.setObjectName("primary")
        self.b_dl = QPushButton("下载勾选项"); self.b_dl.setObjectName("green"); self.b_dl.setEnabled(False)
        self.b_cancel_all = QPushButton("取消全部"); self.b_cancel_all.setObjectName("danger")
        self.b_cancel_all.setVisible(False)
        self.b_clr = QPushButton("清空")
        self.b_all = QPushButton("全选")
        self.b_non = QPushButton("取消全选")
        self.b_inv = QPushButton("反选")
        self.b_view_card = QPushButton("卡片"); self.b_view_card.setObjectName("viewToggle")
        self.b_view_table = QPushButton("表格"); self.b_view_table.setObjectName("viewToggle")
        for b in (self.b_parse, self.b_dl, self.b_cancel_all, self.b_clr): br.addWidget(b)
        br.addStretch(1)
        for b in (self.b_all, self.b_non, self.b_inv): br.addWidget(b)
        br.addSpacing(8)
        for b in (self.b_view_card, self.b_view_table): br.addWidget(b)
        m.addLayout(br)
        m.addSpacing(14)

        sf = QFrame(); sf.setObjectName("settingsRow")
        sl = QHBoxLayout(sf); sl.setContentsMargins(16, 10, 16, 10); sl.setSpacing(14)
        sl.addWidget(self._lbl("输出目录"))
        self.e_out = QLineEdit(str(DEFAULT_OUTPUT_DIR)); self.e_out.setMinimumWidth(180)
        sl.addWidget(self.e_out, stretch=1)
        self.b_br = QPushButton("选择…"); self.b_br.setObjectName("dirBtn")
        sl.addWidget(self.b_br)
        sl.addWidget(self._lbl("画质"))
        self.cb_q = QComboBox(); self.cb_q.addItems(list(QUALITY_OPTIONS))
        self.cb_q.setCurrentText(DEFAULT_QUALITY)
        sl.addWidget(self.cb_q)
        sl.addWidget(self._lbl("并发"))
        self.sp_t = QSpinBox(); self.sp_t.setRange(1, 16); self.sp_t.setValue(DEFAULT_MAX_CONCURRENT_TASKS)
        sl.addWidget(self.sp_t)
        sl.addWidget(self._lbl("分块"))
        self.sp_c = QSpinBox(); self.sp_c.setRange(1, 32); self.sp_c.setValue(DEFAULT_CHUNK_THREADS)
        sl.addWidget(self.sp_c)
        m.addWidget(sf)
        m.addSpacing(14)

        sr = QHBoxLayout(); sr.setSpacing(12)
        self.lbl_st = QLabel("0 个视频"); self.lbl_st.setObjectName("stat")
        self.lbl_st.setMinimumWidth(120)
        sr.addWidget(self.lbl_st)
        self.obar = QProgressBar(); self.obar.setFixedHeight(5)
        self.obar.setRange(0, 100); self.obar.setValue(0)
        self.obar.setTextVisible(False)
        sr.addWidget(self.obar, stretch=1)
        m.addLayout(sr)
        m.addSpacing(12)

        self.view_stack = QStackedWidget()
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.si = QWidget()
        self.grid = QGridLayout(self.si)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14); self.grid.setVerticalSpacing(14)
        self.scroll.setWidget(self.si)
        self.view_stack.addWidget(self.scroll)

        self.table = QTreeWidget()
        self.table.setObjectName("tableView")
        self.table.setHeaderLabels(["", "封面", "标题", "作者", "状态"])
        self.table.setRootIsDecorated(False)
        self.table.setIconSize(QSize(96, 54))
        self.table.setUniformRowHeights(False)
        hdr = self.table.header()
        hdr.setStretchLastSection(False)
        hdr.setMinimumSectionSize(80)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(3, 110)
        self.table.setColumnWidth(4, 280)
        self.view_stack.addWidget(self.table)

        self.view_stack.setCurrentIndex(0)
        m.addWidget(self.view_stack, stretch=1)
        m.addSpacing(12)

        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setObjectName("logBox")
        self.logbox.setFixedHeight(90)
        self.logbox.setMaximumBlockCount(300)
        m.addWidget(self.logbox)

        self.b_parse.clicked.connect(self.do_parse)
        self.b_dl.clicked.connect(self.do_dl)
        self.b_clr.clicked.connect(self.clear)
        self.b_all.clicked.connect(lambda: self._chk_all(True))
        self.b_non.clicked.connect(lambda: self._chk_all(False))
        self.b_inv.clicked.connect(self._chk_invert)
        self.b_view_card.clicked.connect(lambda: self._switch_view(0))
        self.b_view_table.clicked.connect(lambda: self._switch_view(1))
        self.b_br.clicked.connect(self._pick_dir)
        self.b_cancel_all.clicked.connect(self._cancel_all)
        self._ct = QTimer(self)
        self._ct.timeout.connect(self._clip_poll)

    @staticmethod
    def _lbl(t): l = QLabel(t); l.setObjectName("label"); return l

    # ── 剪贴板 ──
    @staticmethod
    def _url(t):
        m = re.search(r"https?://[^\s\"'<>]+", t or "")
        return m.group(0).strip() if m else ""

    def _clip_poll(self):
        if self._busy(): return
        u = self._url(QApplication.clipboard().text())
        has = bool(self.inp.toPlainText().strip())
        if u and not has and u != self.last_clip:
            self.last_clip = u
            self.clip_lbl.setText("剪贴板检测到链接 — 点击「解析」自动填入")
            self.clip_lbl.setVisible(True)
        elif has or not u:
            self.clip_lbl.setVisible(False)

    # ── 工具 ──
    def _busy(self):
        return (self.pw and self.pw.isRunning()) or (self.dw and self.dw.isRunning())

    def _set_busy(self, b):
        for w in (self.b_parse, self.b_clr, self.b_all, self.b_non, self.b_inv,
                  self.b_br, self.cb_q, self.sp_t, self.sp_c):
            w.setEnabled(not b)
        self.b_dl.setEnabled(not b and bool(self.cards))

    def _plog(self, t):
        self.logbox.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {t}")

    def _st(self, t):
        self.lbl_st.setText(t); self._plog(t)

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择目录", self.e_out.text())
        if d: self.e_out.setText(d)

    def _chk_all(self, v):
        for c in self.cards: c.chk.setChecked(v)
        self._sync_table_checks(); self._ustat()

    def _chk_invert(self):
        for c in self.cards: c.chk.setChecked(not c.chk.isChecked())
        self._sync_table_checks(); self._ustat()

    def _switch_view(self, idx: int):
        if idx == 1: self._sync_table()
        self.view_stack.setCurrentIndex(idx)
        self._set_view_active(idx)

    def _set_view_active(self, idx: int):
        self.b_view_card.setProperty("active", "true" if idx == 0 else "false")
        self.b_view_table.setProperty("active", "true" if idx == 1 else "false")
        for b in (self.b_view_card, self.b_view_table):
            b.style().unpolish(b); b.style().polish(b)

    def _sync_table(self):
        try: self.table.itemChanged.disconnect()
        except (RuntimeError, TypeError): pass
        self.table.clear()
        self.row_widgets.clear()
        for card in self.cards:
            item = QTreeWidgetItem()
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if card.is_selected() else Qt.CheckState.Unchecked)
            cover_url = card.target.cover_url
            if cover_url and cover_url in self.cover_cache:
                item.setIcon(1, QIcon(self.cover_cache[cover_url]))
            item.setText(2, card.target.title)
            item.setText(3, card.target.author or "—")
            item.setData(0, Qt.ItemDataRole.UserRole, card.target.url)
            # 强制行高与封面对齐
            for col in range(self.table.columnCount()):
                item.setSizeHint(col, QSize(0, 60))
            self._paint_row(item)
            self.table.addTopLevelItem(item)

            row = RowStatusWidget(card.target.url)
            row.cancel_clicked.connect(self._cancel_url)
            row.open_clicked.connect(self._open_url)
            # 同步当前状态
            if card.state == "ok":
                row.set_done()
            elif card.state == "act":
                row.set_progress(0, None, 0, 0)
            elif card.state in ("err", "cancelled"):
                row.set_error(card.lbl_info.text())
            else:
                row.set_idle_text("待下载")
            self.row_widgets[card.target.url] = row
            self.table.setItemWidget(item, 4, row)
        self.table.itemChanged.connect(self._on_table_check)

    def _paint_row(self, item: QTreeWidgetItem):
        checked = item.checkState(0) == Qt.CheckState.Checked
        bg = QBrush(QColor("#EAF3FF") if checked else QColor(0, 0, 0, 0))
        for col in range(self.table.columnCount()):
            item.setBackground(col, bg)

    def _sync_table_checks(self):
        try: self.table.itemChanged.disconnect()
        except (RuntimeError, TypeError): pass
        for i in range(self.table.topLevelItemCount()):
            item = self.table.topLevelItem(i)
            url = item.data(0, Qt.ItemDataRole.UserRole)
            card = self.cards_by_url.get(url)
            if card:
                item.setCheckState(0, Qt.CheckState.Checked if card.is_selected() else Qt.CheckState.Unchecked)
                self._paint_row(item)
        self.table.itemChanged.connect(self._on_table_check)

    def _on_table_check(self, item, col):
        if col != 0: return
        url = item.data(0, Qt.ItemDataRole.UserRole)
        card = self.cards_by_url.get(url)
        if card:
            checked = item.checkState(0) == Qt.CheckState.Checked
            card.chk.setChecked(checked)
            self._paint_row(item)
            self._ustat()

    def _ustat(self):
        n = len(self.cards)
        sel = sum(1 for c in self.cards if c.is_selected())
        parts = [f"{n} 个视频"]
        if sel != n: parts.append(f"已选 {sel}")
        ok = len(self.ok_urls); fa = len(self.fail_urls)
        if ok: parts.append(f"成功 {ok}")
        if fa: parts.append(f"失败 {fa}")
        self.lbl_st.setText("  ·  ".join(parts))

    def _ncols(self):
        w = self.scroll.viewport().width()
        if w >= 1100: return 3
        if w >= 720: return 2
        return 1

    def _lay(self):
        while self.grid.count(): self.grid.takeAt(0)
        c = self._ncols()
        for i, card in enumerate(self.cards):
            self.grid.addWidget(card, i // c, i % c)
        for j in range(c): self.grid.setColumnStretch(j, 1)

    # ── 解析 ──
    def do_parse(self):
        raw = self.inp.toPlainText().strip()
        if not raw:
            u = self._url(QApplication.clipboard().text())
            if u: raw = u; self.inp.setPlainText(raw)
            else: QMessageBox.warning(self, "提示", "请输入视频链接"); return
        if self._busy(): return

        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        expand_playlist = True
        if len(lines) == 1 and lines[0].startswith("http"):
            dlg = ParseModeDialog(self)
            if not dlg.exec(): return
            expand_playlist = (dlg.mode == "playlist")

        self.clear()
        self._set_busy(True)
        self.obar.setRange(0, 0)
        mode_txt = "整个播放列表" if expand_playlist else "仅当前视频"
        self._st(f"正在启动浏览器…（{mode_txt}）")
        self.pw = ParseWorker(raw, self.cb_q.currentText(), DEFAULT_USER_DATA_DIR, expand_playlist)
        self.pw.status.connect(self._st)
        self.pw.progress.connect(self._pp)
        self.pw.found.connect(self._pf)
        self.pw.done.connect(self._pd)
        self.pw.failed.connect(self._pe)
        self.pw.start()

    def _pp(self, d, t):
        if t > 0:
            self.obar.setRange(0, 100); self.obar.setValue(int(d * 100 / t))

    def _pf(self, obj):
        if not isinstance(obj, VideoTarget): return
        card = VideoCard(obj)
        card.chk.stateChanged.connect(lambda *_a, u=obj.url: self._on_card_check(u))
        card.cancel_clicked.connect(self._cancel_url)
        card.open_clicked.connect(self._open_url)
        self.cards.append(card)
        self.cards_by_url[obj.url] = card
        self.targets.append(obj)
        self._lay(); self._ustat()
        if obj.cover_url: self._get_cover(obj.cover_url, card)
        if self.view_stack.currentIndex() == 1:
            self._sync_table()

    def _on_card_check(self, url):
        if self.view_stack.currentIndex() == 1:
            self._sync_table_checks()
        self._ustat()

    def _pd(self, tobj, cobj):
        if isinstance(tobj, list): self.targets = tobj
        self.creds = cobj
        self._set_busy(False)
        self.b_dl.setEnabled(bool(self.targets))
        self.obar.setRange(0, 100); self.obar.setValue(100 if self.targets else 0)
        self._ustat()
        self._st(f"解析完成，{len(self.targets)} 个视频")

    def _pe(self, msg):
        self._set_busy(False)
        self.obar.setRange(0, 100); self.obar.setValue(0)
        self._st(f"解析失败：{msg}")
        QMessageBox.critical(self, "解析失败", msg)

    # ── 封面 ──
    def _get_cover(self, url, card):
        if url in self.cover_cache:
            card.set_cover(self.cover_cache[url]); return
        self.pending_covers[url].append(card)
        if len(self.pending_covers[url]) > 1: return
        rq = QNetworkRequest(QUrl(url))
        rq.setRawHeader(b"Referer", b"https://hanime1.me/")
        rp = self.net.get(rq)
        rp.setProperty("u", url)
        rp.finished.connect(lambda r=rp: self._on_cover(r))

    def _on_cover(self, rp):
        url = str(rp.property("u") or "")
        cards = self.pending_covers.pop(url, [])
        try:
            if rp.error() == QNetworkReply.NetworkError.NoError:
                px = QPixmap()
                if px.loadFromData(bytes(rp.readAll())):
                    self.cover_cache[url] = px
                    for c in cards: c.set_cover(px)
                    if self.view_stack.currentIndex() == 1:
                        for i in range(self.table.topLevelItemCount()):
                            it = self.table.topLevelItem(i)
                            target_url = it.data(0, Qt.ItemDataRole.UserRole)
                            card = self.cards_by_url.get(target_url)
                            if card and card.target.cover_url == url:
                                it.setIcon(1, QIcon(px))
        finally:
            rp.deleteLater()

    # ── 下载 ──
    def do_dl(self):
        if self._busy() or not self.creds: return
        sel = [c.target for c in self.cards if c.is_selected()]
        if not sel:
            QMessageBox.warning(self, "提示", "请勾选视频"); return
        self.ok_urls.clear(); self.fail_urls.clear()
        out = Path(self.e_out.text().strip() or str(DEFAULT_OUTPUT_DIR)).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)

        group = len(sel) >= 2 and any(t.author for t in sel)
        if group:
            authors = sorted({t.author for t in sel if t.author})
            if authors: self._plog(f"按作者归档：{', '.join(authors)}")

        for c in self.cards:
            if c.is_selected(): c.mark_wait()
        if self.view_stack.currentIndex() == 1:
            for url in (c.target.url for c in self.cards if c.is_selected()):
                if url in self.row_widgets:
                    self.row_widgets[url].set_wait()

        self._set_busy(True)
        self.obar.setRange(0, len(sel)); self.obar.setValue(0)
        self.b_dl.setVisible(False)
        self.b_cancel_all.setVisible(True)
        self.b_cancel_all.setEnabled(True)
        self._st(f"开始下载 {len(sel)} 个" + ("（按作者分目录）" if group else ""))

        self.dw = DownloadWorker(sel, self.creds, out, self.sp_t.value(), self.sp_c.value(), group)
        self.dw.status.connect(self._st)
        self.dw.prog.connect(self._dp)
        self.dw.ok.connect(self._dok)
        self.dw.fail.connect(self._dfail)
        self.dw.overall.connect(self._dover)
        self.dw.finished.connect(self._ddone)
        self.dw.start()

    # ── 取消 / 打开 ──
    def _cancel_url(self, url: str):
        if self.dw and self.dw.isRunning():
            self.dw.cancel_url(url)
            self._plog("已请求取消单个任务")

    def _cancel_all(self):
        if self.dw and self.dw.isRunning():
            self.dw.cancel_all()
            self.b_cancel_all.setEnabled(False)
            self._plog("已请求取消全部下载…")

    def _open_url(self, url: str):
        card = self.cards_by_url.get(url)
        if not card or not card.saved_path:
            return
        if not _open_in_explorer(card.saved_path):
            QMessageBox.warning(self, "提示", f"无法打开：{card.saved_path}")

    def _dp(self, url: str, recv: int, tobj, speed: float, eta: float):
        c = self.cards_by_url.get(url)
        total = tobj if isinstance(tobj, int) else None
        if c: c.mark_prog(recv, total, speed, eta)
        row = self.row_widgets.get(url)
        if row: row.set_progress(recv, total, speed, eta)

    def _dok(self, url, path):
        c = self.cards_by_url.get(url)
        if c: c.mark_ok(path)
        row = self.row_widgets.get(url)
        if row: row.set_done()
        self.ok_urls.add(url); self.fail_urls.discard(url); self._ustat()
        self._plog(f"完成 {Path(path).name}")

    def _dfail(self, url, msg):
        c = self.cards_by_url.get(url)
        if c: c.mark_err(msg)
        row = self.row_widgets.get(url)
        if row: row.set_error(msg)
        self.fail_urls.add(url); self._ustat()
        self._plog(f"失败 {msg}")

    def _dover(self, d, t):
        self.obar.setRange(0, t); self.obar.setValue(d)

    def _ddone(self, obj):
        n = len(obj) if isinstance(obj, list) else 0
        self._set_busy(False)
        self.b_cancel_all.setVisible(False)
        self.b_cancel_all.setEnabled(True)
        self.b_dl.setVisible(True)
        self.b_dl.setEnabled(bool(self.cards))
        self._ustat(); self._st(f"下载完成，成功 {n} 个")

    # ── 清空 / 事件 ──
    def clear(self):
        for c in self.cards:
            self.grid.removeWidget(c)
            c.deleteLater()
        self.targets.clear(); self.cards.clear(); self.cards_by_url.clear()
        self.ok_urls.clear(); self.fail_urls.clear()
        try: self.table.itemChanged.disconnect()
        except (RuntimeError, TypeError): pass
        self.table.clear()
        self.row_widgets.clear()
        self.creds = None
        self.b_dl.setVisible(True); self.b_dl.setEnabled(False)
        self.b_cancel_all.setVisible(False)
        self.obar.setValue(0)
        self._ustat()

    def resizeEvent(self, e):
        super().resizeEvent(e); self._lay()

    def dragEnterEvent(self, e: QDragEnterEvent):
        mi = e.mimeData()
        if mi.hasUrls() or self._url(mi.text()): e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent):
        mi = e.mimeData(); urls = []
        if mi.hasUrls():
            urls.extend(u.toString() for u in mi.urls() if u.toString().startswith("http"))
        u = self._url(mi.text())
        if u: urls.append(u)
        urls = list(dict.fromkeys(urls))
        if urls:
            self.inp.setPlainText("\n".join(urls)); self.do_parse()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Ver-Hanime1Downloader")
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
