"""Read Spotify's current track and playback position from Windows' media controls (SMTC),
and use them to control playback."""
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
    duration: float  # seconds, 0 if unknown


@dataclass(frozen=True)
class Snapshot:
    track: Track | None
    position: float    # playback position (seconds) at sampled_at
    sampled_at: float  # time.monotonic()
    playing: bool
    running: bool      # whether the Spotify process is running


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
    """Polls SMTC in the background and hands every sample to on_update as a Snapshot."""

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
                log.exception("SMTC error, retrying in 3 s")
                self._on_update(Snapshot(None, 0.0, time.monotonic(), False, self._running))
                time.sleep(3)

    def send(self, command: str):
        """Send a playback command from any thread: toggle / next / prev."""
        if self._loop and self._session is not None:
            asyncio.run_coroutine_threadsafe(self._command(command), self._loop)
        else:
            # right after Spotify starts it has no SMTC session yet, so fall back to media keys
            press_media_key(command)

    async def _command(self, command: str):
        s = self._session
        op = {"toggle": s.try_toggle_play_pause_async, "next": s.try_skip_next_async,
              "prev": s.try_skip_previous_async}[command]
        try:
            await op()
        except Exception:
            log.exception("Playback command failed: %s", command)
        await asyncio.sleep(0.15)
        self._wake.set()  # sample again right away so the UI catches up quickly

    async def _main(self):
        # Import winrt only in this thread: importing it initializes the current thread as MTA,
        # which would clash with the STA that Qt needs on the main thread.
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
                # no need to poll as often while Spotify is closed
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

        # Spotify only updates `position` on play / pause / seek,
        # so while playing we extrapolate from last_updated_time.
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
