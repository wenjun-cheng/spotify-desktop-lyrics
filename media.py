"""从 Windows 系统媒体控件（SMTC）读取 Spotify 的当前曲目和播放进度，也用它来控制播放。"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
MEDIA_KEYS = {"toggle": 0xB3, "next": 0xB0, "prev": 0xB1}  # VK_MEDIA_PLAY_PAUSE / NEXT / PREV


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    album: str
    duration: float  # 秒，未知时为 0


@dataclass(frozen=True)
class Snapshot:
    track: Track | None
    position: float    # sampled_at 那一刻的播放进度（秒）
    sampled_at: float  # time.monotonic()
    playing: bool
    running: bool      # Spotify 进程是否在运行


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.Process32FirstW.argtypes = _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def process_running(exe: str) -> bool:
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return False
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == exe:
                return True
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
        return False
    finally:
        _k32.CloseHandle(snap)


def press_media_key(command: str):
    vk = MEDIA_KEYS[command]
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP


class MediaWatcher(threading.Thread):
    """后台轮询 SMTC，每次采样都把一个 Snapshot 交给 on_update。"""

    def __init__(self, on_update, app_id: str = "spotify", exe: str = "spotify.exe",
                 interval: float = 0.25, debug: bool = False):
        super().__init__(name="MediaWatcher", daemon=True)
        self._on_update = on_update
        self._app_id = app_id.lower()
        self._exe = exe
        self._interval = interval
        self._debug = debug
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._session = None
        self._running = False
        self._running_checked = 0.0

    def run(self):
        while True:
            try:
                asyncio.run(self._main())
            except Exception:
                log.exception("SMTC 读取出错，3 秒后重试")
                self._on_update(Snapshot(None, 0.0, time.monotonic(), False, self._running))
                time.sleep(3)

    def send(self, command: str):
        """从任意线程发播放控制命令：toggle / next / prev。"""
        if self._loop and self._session is not None:
            asyncio.run_coroutine_threadsafe(self._command(command), self._loop)
        else:
            # 刚打开 Spotify、还没播过的时候它没有 SMTC 会话，只能发系统媒体键
            press_media_key(command)

    async def _command(self, command: str):
        s = self._session
        op = {"toggle": s.try_toggle_play_pause_async, "next": s.try_skip_next_async,
              "prev": s.try_skip_previous_async}[command]
        try:
            await op()
        except Exception:
            log.exception("播放控制失败：%s", command)
        await asyncio.sleep(0.15)
        self._wake.set()  # 马上重新采样，让界面尽快跟上

    async def _main(self):
        # 在这个线程里才导入 winrt：它导入时会把当前线程初始化成 MTA，
        # 放在主线程会和 Qt 需要的 STA 冲突。
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )

        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        manager = await Manager.request_async()
        while True:
            now = time.monotonic()
            if now - self._running_checked > 2:
                self._running = process_running(self._exe)
                self._running_checked = now
            snap = await self._sample(manager)
            self._on_update(snap)
            try:
                # Spotify 没开时不用采得那么勤
                await asyncio.wait_for(self._wake.wait(), self._interval if self._running else 2)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _sample(self, manager) -> Snapshot:
        session = None
        if self._running:
            session = next(
                (s for s in manager.get_sessions()
                 if self._app_id in (s.source_app_user_model_id or "").lower()),
                None,
            )
        self._session = session
        if session is None:
            return Snapshot(None, 0.0, time.monotonic(), False, self._running)

        props = await session.try_get_media_properties_async()
        tl = session.get_timeline_properties()
        playing = session.get_playback_info().playback_status == PLAYING

        # Spotify 只在播放/暂停/拖动时才更新 position，
        # 播放中要用 last_updated_time 推算出现在的进度。
        raw = tl.position.total_seconds()
        pos = raw
        last = tl.last_updated_time
        if playing and last.year > 1601:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            pos += max(0.0, (datetime.now(timezone.utc) - last).total_seconds())
        sampled_at = time.monotonic()

        duration = max(0.0, (tl.end_time - tl.start_time).total_seconds())
        if duration:
            pos = min(pos, duration)

        title = (props.title or "").strip()
        track = None
        if title:
            track = Track(title, (props.artist or "").strip(), (props.album_title or "").strip(), duration)
        if self._debug:
            log.debug("sample %r raw=%.2f last=%s pos=%.2f playing=%s", title, raw, last, pos, playing)
        return Snapshot(track, pos, sampled_at, playing, True)
