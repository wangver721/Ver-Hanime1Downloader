"""
智能链接解析与目标提取：单链接解析、批量 URL 导入、列表页遍历。
提取最高画质直链（mp4/m3u8）及视频标题。
"""
import html
import re
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from .config import TARGET_BASE_URL


@dataclass
class VideoTarget:
    """单个视频目标：直链、标题、来源 URL。"""
    url: str           # 视频页面 URL
    direct_url: str    # 直链（mp4 或 m3u8）
    title: str
    is_m3u8: bool = False
    cover_url: str = ""
    author: str = ""


# 页面标题中常见的站点水印，保存文件名时去掉（支持全角/空格等变体）
TITLE_WATERMARK_PATTERNS = [
    r"\s*[\-–—]\s*H動漫裏番線上看\s*[\-–—]\s*Hanime1\.me\s*$",
    r"\s*[\-–—]\s*H動漫裏番線上看\s*$",  # 仅「 - H動漫裏番線上看」结尾
    r"\s*[\-–—]\s*Hanime1\.me\s*$",
    r"\s*[\|\-–—]\s*H動漫裏番線上看\s*$",
    r"\s*[\|\-–—]\s*Hanime1\.me\s*$",
]


def _strip_title_watermark(raw: str) -> str:
    """去掉标题末尾的站点水印（如 - H動漫裏番線上看 - Hanime1.me）。"""
    if not raw or not raw.strip():
        return raw
    s = raw.strip()
    for pat in TITLE_WATERMARK_PATTERNS:
        s = re.sub(pat, "", s, flags=re.I).strip()
    return s.strip()


def _sanitize_title(raw: str) -> str:
    """清洗标题为合法文件名：解码 HTML、剥离水印、剔除非法字符。"""
    from .file_manager import sanitize_filename
    if not raw or not raw.strip():
        return "未命名"
    s = html.unescape(raw.strip())
    s = _strip_title_watermark(s)
    return sanitize_filename(s)


def _extract_author(page_html: str) -> str:
    """提取视频上传者/作者名称，找不到返回空字符串。"""
    from urllib.parse import unquote
    patterns = [
        # hanime1 常见：链接到 ?query=作者
        r'<a[^>]+href="[^"]*(?:\?|&amp;|&)query=([^"&]+)[^"]*"[^>]*>\s*([^<]+?)\s*</a>',
        r'<a[^>]+href="[^"]*/artists?/([^"/?]+)"[^>]*>\s*([^<]+?)\s*</a>',
        # 单一捕获组兜底
        r'<a[^>]+href="[^"]*(?:\?|&amp;|&)query=([^"&]+)[^"]*"',
        r'<meta[^>]+name=["\']twitter:creator["\'][^>]+content=["\']@?([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:author["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, page_html, re.I)
        if not m:
            continue
        # 优先取最后一个捕获组（通常是显示文本）
        raw = m.group(m.lastindex).strip()
        raw = html.unescape(unquote(raw))
        raw = re.sub(r'[\\/:*?"<>|\r\n\t]', "", raw).strip()
        # 过滤明显不是作者名的结果
        if 1 <= len(raw) <= 60 and not raw.lower().startswith("http"):
            return raw
    return ""


def _extract_cover_url(page_html: str, page_url: str) -> str:
    """提取封面图 URL（优先 og:image，其次 video poster、twitter:image）。"""
    candidates = [
        re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            page_html,
            re.I,
        ),
        re.search(
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
            page_html,
            re.I,
        ),
        re.search(
            r'<video[^>]+poster=["\']([^"\']+)["\']',
            page_html,
            re.I,
        ),
    ]
    for m in candidates:
        if not m:
            continue
        raw = html.unescape(m.group(1).strip())
        if not raw:
            continue
        if raw.startswith("//"):
            return "https:" + raw
        if raw.startswith("/"):
            return urljoin(page_url, raw)
        return raw
    return ""


def _is_list_page(url: str) -> bool:
    """判断是否为系列列表页（可根据站点规则扩展）。"""
    # hanime1 列表页通常包含 /videos/ 等路径
    p = urlparse(url)
    path = (p.path or "").lower()
    return "/videos" in path or "/series" in path or "/search" in path


def _is_single_video_page(url: str) -> bool:
    """判断是否为单集视频页。"""
    p = urlparse(url)
    path = (p.path or "").strip("/")
    # 单集页多为 /watch/xxx 或 /videos/xxx 等形式
    return "/watch" in path or re.match(r"^videos/[^/]+/?$", path)


def parse_single_page_html(
    page_html: str,
    page_url: str,
    preferred_quality: str = "1080p",
) -> Optional[VideoTarget]:
    """
    从单集视频页 HTML 中提取直链与标题。
    preferred_quality：优先选择的画质（如 1080p、720p），默认 1080p。
    """
    if not page_html or not page_url:
        return None

    title = "未命名"
    title_m = re.search(r"<title[^>]*>([^<]+)</title>", page_html, re.I | re.S)
    if title_m:
        title = _sanitize_title(title_m.group(1).strip())
    cover_url = _extract_cover_url(page_html, page_url)
    author = _extract_author(page_html)

    # 提取所有 mp4 链接（带画质信息的 URL 或相邻文本）
    mp4_urls = re.findall(r'["\']?(https?://[^"\'>\s]+\.mp4[^"\'>\s]*)["\']?', page_html, re.I)
    m3u8_urls = re.findall(r'["\']?(https?://[^"\'>\s]+\.m3u8[^"\'>\s]*)["\']?', page_html, re.I)

    def _quality_key(url: str) -> tuple:
        """用于排序：优先包含 preferred 数字的 URL。"""
        q = preferred_quality.replace("p", "").strip()
        url_lower = url.lower()
        if q in url_lower or preferred_quality.lower() in url_lower:
            return (0, url)
        for p in ("1080", "720", "480", "360"):
            if p in url_lower:
                return (1, url)
        return (2, url)

    direct_url = ""
    is_m3u8 = False

    if mp4_urls:
        mp4_urls.sort(key=lambda u: _quality_key(u)[0])
        # 优先选 URL 中含 preferred_quality 的
        for u in mp4_urls:
            if preferred_quality.replace("p", "") in u or preferred_quality in u:
                direct_url = u
                break
        if not direct_url:
            direct_url = mp4_urls[0]
    if not direct_url and m3u8_urls:
        for u in m3u8_urls:
            if preferred_quality.replace("p", "") in u or preferred_quality in u:
                direct_url = u
                break
        if not direct_url:
            direct_url = m3u8_urls[0]
        is_m3u8 = True

    if direct_url and direct_url.startswith("/"):
        direct_url = urljoin(page_url, direct_url)
    if not direct_url:
        return None
    # 直链可能含 HTML 实体（如 &amp;），请求前必须解码，否则 403
    direct_url = html.unescape(direct_url)

    return VideoTarget(
        url=page_url,
        direct_url=direct_url,
        title=title,
        is_m3u8=is_m3u8,
        cover_url=cover_url,
        author=author,
    )


def collect_urls_from_batch_file(file_path: Path) -> List[str]:
    """从本地 .txt 文件读取批量 URL，每行一个。"""
    urls = []
    path = Path(file_path)
    if not path.exists() or not path.suffix.lower() == ".txt":
        return urls
    text = path.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        line = line.strip()
        if line and (line.startswith("http://") or line.startswith("https://")):
            urls.append(line)
    return urls


def _find_matching_closing_div(html: str, id_pos: int) -> int:
    """从 id_pos（id="video-playlist-wrapper" 所在位置）向前找到该 <div> 起始，再找到匹配的 </div> 结束位置。"""
    div_start = html.rfind("<div", 0, id_pos)
    if div_start == -1:
        return -1
    depth = 0
    i = div_start
    while i < len(html):
        if (i + 4 <= len(html) and html[i : i + 4].lower() == "<div" and
                (i + 4 == len(html) or html[i + 4] in " \t\n\r>")):
            depth += 1
            i += 4
            continue
        if i + 6 <= len(html) and html[i : i + 6].lower() == "</div>":
            depth -= 1
            if depth == 0:
                return i + 6
            i += 6
            continue
        i += 1
    return -1


def _extract_playlist_overlay_links(html: str) -> List[str]:
    """
    仅提取第一个 #video-playlist-wrapper 内部 <a class="overlay" href="...watch?v=xxx"> 的链接。
    严格限定在该 div 的起止范围内，不包含页面其它区域的列表。
    """
    wrapper_start = html.find('id="video-playlist-wrapper"')
    if wrapper_start == -1:
        wrapper_start = html.find('id="playlist-scroll"')
    if wrapper_start != -1:
        end = _find_matching_closing_div(html, wrapper_start)
        if end != -1:
            div_start = html.rfind("<div", 0, wrapper_start)
            search_html = html[div_start:end] if div_start != -1 else html[wrapper_start:end]
        else:
            search_html = html[wrapper_start : wrapper_start + 80000]
    else:
        search_html = html

    urls: List[str] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'<a\s+class="overlay"\s+href="(https://hanime1\.me/watch\?v=\d+)"',
        search_html, re.I
    ):
        u = m.group(1)
        if u not in seen:
            seen.add(u)
            urls.append(u)
    if not urls:
        for m in re.finditer(
            r'<a\s+[^>]*href="(https://hanime1\.me/watch\?v=\d+)"[^>]*class="[^"]*overlay[^"]*"',
            search_html, re.I
        ):
            u = m.group(1)
            if u not in seen:
                seen.add(u)
                urls.append(u)
    return urls


def _extract_links_dense_cluster(list_page_html: str, list_page_url: str) -> List[str]:
    """全页收集 watch/videos 链接，按位置密集区间取主列表。"""
    matches: List[tuple[str, int]] = []
    seen_urls: set[str] = set()

    def _norm(link: str) -> str:
        link = link.strip()
        if link.startswith("/"):
            link = urljoin(list_page_url, link)
        return link

    for m in re.finditer(
        r'href\s*=\s*["\']([^"\']*(?:/watch/|/videos/)[a-zA-Z0-9_-]+/?)[^"\']*["\']',
        list_page_html, re.I
    ):
        u = _norm(m.group(1))
        if (TARGET_BASE_URL in u or "hanime1" in u) and u not in seen_urls:
            seen_urls.add(u)
            matches.append((u, m.start()))
    for m in re.finditer(
        r'href\s*=\s*["\']([^"\']*watch\?v=(\d+)[^"\']*)["\']',
        list_page_html, re.I
    ):
        full = m.group(1).strip()
        u = full if full.startswith("http") else urljoin(list_page_url, full if full.startswith("/") else "/" + full)
        if (TARGET_BASE_URL in u or "hanime1" in u) and u not in seen_urls:
            seen_urls.add(u)
            matches.append((u, m.start()))
    if not matches:
        return []
    matches.sort(key=lambda x: x[1])
    MAX_GAP = 3500
    clusters: List[List[tuple[str, int]]] = []
    current: List[tuple[str, int]] = [matches[0]]
    for i in range(1, len(matches)):
        if matches[i][1] - matches[i - 1][1] <= MAX_GAP:
            current.append(matches[i])
        else:
            clusters.append(current)
            current = [matches[i]]
    clusters.append(current)
    best = max(clusters, key=len)
    return [url for url, _ in best]


def extract_list_page_video_links(list_page_html: str, list_page_url: str) -> List[str]:
    """
    从页面 HTML 中提取「同一播放列表」中的视频链接。
    优先从 hanime1 的 #video-playlist-wrapper 内 <a class="overlay" href="...watch?v=xxx"> 提取。
    若无该结构则回退：单集页返回当前页；否则用密集区间取主列表。
    """
    playlist_urls = _extract_playlist_overlay_links(list_page_html)
    if playlist_urls:
        return playlist_urls
    if "watch?v=" in list_page_url.lower() or "/watch/" in list_page_url.lower():
        u = list_page_url if list_page_url.startswith("http") else urljoin(list_page_url, list_page_url)
        return [u]
    return _extract_links_dense_cluster(list_page_html, list_page_url)
