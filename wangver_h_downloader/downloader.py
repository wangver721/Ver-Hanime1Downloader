"""
多线程/并发下载引擎：接力浏览器凭证，分块多线程下载，多任务并发。
共享 httpx.AsyncClient + 自动重试 + 连接池限流。
"""
import asyncio
import re
from pathlib import Path
from typing import Optional, Callable

import httpx
import aiofiles

from .config import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNK_THREADS,
    PART_SUFFIX,
)
from .browser_cf import SessionCredentials
from .file_manager import find_part_file, sanitize_filename


def _cookies_to_headers(cookies: list) -> dict:
    """将 Playwright cookies 列表转为 Cookie 请求头。"""
    parts = []
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if name and value is not None:
            parts.append(f"{name}={value}")
    return {"Cookie": "; ".join(parts)} if parts else {}


class DownloadCancelled(Exception):
    """用户主动取消的下载（不被 _retry 重试）。"""


# 网络错误类型（瞬时错误，可重试）
_TRANSIENT_EXC = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)


def _make_client(credentials: Optional[SessionCredentials], chunk_threads: int) -> httpx.AsyncClient:
    """创建一个共享的 httpx.AsyncClient，统一连接池与超时。"""
    headers = {"Referer": "https://hanime1.me/"}
    if credentials and credentials.user_agent:
        headers["User-Agent"] = credentials.user_agent
    if credentials and credentials.cookies:
        headers.update(_cookies_to_headers(credentials.cookies))

    limits = httpx.Limits(
        max_connections=max(chunk_threads * 2, 16),
        max_keepalive_connections=chunk_threads,
    )
    timeout = httpx.Timeout(connect=20.0, read=120.0, write=60.0, pool=30.0)
    return httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=timeout,
        limits=limits,
        trust_env=True,
        http2=False,
    )


async def _retry(coro_factory, retries: int = 4, base_delay: float = 0.6):
    """
    通用重试：对瞬时网络错误进行指数退避，最多 retries 次。
    coro_factory: 一个无参回调，返回新的 coroutine（每次重试都重新构造）。
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except _TRANSIENT_EXC as e:
            last_exc = e
            if attempt >= retries:
                break
            await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc if last_exc else RuntimeError("未知网络错误")


async def _head_for_range(client: httpx.AsyncClient, url: str) -> tuple[int, bool]:
    """获取文件大小与是否支持 Range（HEAD 失败时退回到 Range 0-0 的 GET）。"""
    async def _do_head():
        r = await client.head(url)
        if r.status_code in (403, 405, 501):
            r = await client.get(url, headers={"Range": "bytes=0-0"})
        r.raise_for_status()
        return r

    r = await _retry(_do_head)
    total = int(r.headers.get("content-length", 0) or 0)
    cr = r.headers.get("content-range") or ""
    if total <= 0 and cr:
        m = re.search(r"/(\d+)$", cr)
        if m:
            total = int(m.group(1))
    accept_ranges = (r.headers.get("accept-ranges") or "").lower() == "bytes" or r.status_code == 206
    return total, accept_ranges


async def _download_chunk(
    client: httpx.AsyncClient,
    url: str,
    start: int,
    end: int,
    dest_path: Path,
    progress_callback: Optional[Callable[[int, Optional[int]], None]],
) -> int:
    """带重试的分块下载。"""
    async def _do():
        n = 0
        async with client.stream("GET", url, headers={"Range": f"bytes={start}-{end}"}) as r:
            r.raise_for_status()
            async with aiofiles.open(dest_path, "r+b") as f:
                await f.seek(start)
                async for chunk in r.aiter_bytes(chunk_size=65536):
                    await f.write(chunk)
                    n += len(chunk)
                    if progress_callback:
                        progress_callback(len(chunk), None)
        return n

    return await _retry(_do)


async def download_chunked(
    client: httpx.AsyncClient,
    url: str,
    dest_path: Path,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_concurrent_chunks: int = DEFAULT_CHUNK_THREADS,
    progress_callback: Optional[Callable[[int, Optional[int]], None]] = None,
) -> Path:
    """分块并发下载到 dest_path（支持断点续传）。"""
    total, accept_ranges = await _head_for_range(client, url)
    if progress_callback and total > 0:
        progress_callback(0, total)

    # 不支持 Range 或大小未知 → 整文件流式下载
    if total <= 0 or not accept_ranges:
        async def _do_full():
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                async with aiofiles.open(dest_path, "wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=65536):
                        await f.write(chunk)
                        if progress_callback:
                            progress_callback(len(chunk), None)
        await _retry(_do_full)
        return dest_path

    # 断点续传：识别已有数据
    done_ranges: list[tuple[int, int]] = []
    if dest_path.exists():
        size = dest_path.stat().st_size
        if size > 0:
            done_ranges.append((0, min(size, total) - 1))
            if progress_callback:
                progress_callback(min(size, total), total)

    # 计算待下载分块
    needed: list[tuple[int, int]] = []
    pos = 0
    while pos < total:
        end = min(pos + chunk_size, total) - 1
        covered = any(a <= pos and end <= b for a, b in done_ranges)
        if not covered:
            needed.append((pos, end))
        pos = end + 1
    if not needed:
        return dest_path

    # 预分配文件
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if not dest_path.exists():
        async with aiofiles.open(dest_path, "wb") as f:
            await f.seek(total - 1)
            await f.write(b"\x00")
    elif dest_path.stat().st_size < total:
        async with aiofiles.open(dest_path, "r+b") as f:
            await f.seek(total - 1)
            await f.write(b"\x00")

    sem = asyncio.Semaphore(max_concurrent_chunks)

    async def do_one(s: int, e: int):
        async with sem:
            await _download_chunk(client, url, s, e, dest_path, progress_callback)

    await asyncio.gather(*[do_one(s, e) for s, e in needed])
    return dest_path


async def download_task(
    url: str,
    title: str,
    output_dir: Path,
    credentials: Optional[SessionCredentials],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_threads: int = DEFAULT_CHUNK_THREADS,
    progress_callback: Optional[Callable[[int, Optional[int]], None]] = None,
) -> Path:
    """单任务：解析文件名、查 .part 断点、分块下载、完成后重命名。"""
    safe_title = sanitize_filename(title)
    ext = ".mp4" if ".m3u8" not in url.lower() else ".m3u8"
    final_path = output_dir / (safe_title + ext)

    part_path = find_part_file(output_dir, safe_title, ext) or (output_dir / (safe_title + ext + PART_SUFFIX))

    async with _make_client(credentials, chunk_threads) as client:
        await download_chunked(
            client, url, part_path,
            chunk_size=chunk_size,
            max_concurrent_chunks=chunk_threads,
            progress_callback=progress_callback,
        )

    if part_path.suffix == PART_SUFFIX or part_path.name.endswith(PART_SUFFIX):
        if final_path.exists():
            final_path.unlink()
        part_path.rename(final_path)
        return final_path
    return part_path
