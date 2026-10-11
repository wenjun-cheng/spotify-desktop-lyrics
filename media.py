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


# Playback commands straight to Spotify's window: APPCOMMAND_MEDIA_PLAY_PAUSE / NEXTTRACK / PREVIOUSTRACK
APP_COMMANDS = {"toggle": 14, "next": 11, "prev": 12}
_u32 = ctypes.WinDLL("user32")
_u32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT,
                                     wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
_u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_u32.IsWindowVisible.argtypes = [wintypes.HWND]
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)]
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _process_name(hwnd) -> str:
    pid = wintypes.DWORD()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = _k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(260), wintypes.DWORD(260)
        ok = _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return buf.value.rsplit("\\", 1)[-1].lower() if ok else ""
    finally:
        _k32.CloseHandle(handle)


def app_window(exe: str) -> int:
    """The app's main window (shown or minimized, with a title: Spotify's is the song), or 0. Only window titles
    and classes are read: nothing goes through Spotify's UI."""
    found = []

    def check(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        _u32.GetClassNameW(hwnd, cls, 64)
        if cls.value == "Chrome_WidgetWin_1" and _u32.IsWindowVisible(hwnd) and _u32.GetWindowTextLengthW(hwnd) \
                and _process_name(hwnd) == exe:
            found.append(hwnd)
            return False
        return True

    _u32.EnumWindows(_EnumProc(check), 0)
    return found[0] if found else 0


NOT_PLAYING_TITLES = {"Spotify", "Spotify Premium", "Spotify Free"}  # its window title when nothing plays
_u32.IsWindow.argtypes = [wintypes.HWND]
_u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]


def window_says_playing(hwnd: int) -> bool | None:
    """Whether Spotify's window title says it's playing: it's the song then ("Artist - Title"), and "Spotify
    Premium" or the like when paused. None if there's no title to go by."""
    title = ctypes.create_unicode_buffer(256)
    if not hwnd or not _u32.IsWindow(hwnd) or not _u32.GetWindowTextW(hwnd, title, 256):
        return None
    return title.value.strip() not in NOT_PLAYING_TITLES


def send_app_command(hwnd: int, command: str) -> bool:
    """A playback command to the window, as the media keys of a keyboard aimed at it would send it."""
    result = ctypes.c_size_t()
    return bool(_u32.SendMessageTimeoutW(hwnd, 0x0319, hwnd, APP_COMMANDS[command] << 16,  # WM_APPCOMMAND
                                         0x2, 1000, ctypes.byref(result)))  # SMTO_ABORTIFHUNG


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
        self._window = 0  # Spotify's main window (see app_window)

    def run(self):
        while True:
            try:
                asyncio.run(self._main())
            except Exception:
                log.exception("SMTC error, retrying in 3 s")
                self._on_update(Snapshot(None, 0.0, time.monotonic(), False, self._running))
                time.sleep(3)

    def send(self, command: str):
        """Send a playback command from any thread: toggle / next / prev. Straight to Spotify's window: through
        Windows' media controls (SMTC), and through the media keys, Spotify sometimes ignores them, while SMTC
        still reports success. SMTC (or the media keys) only if its window can't be found."""
        hwnd = app_window(self._exe)
        if hwnd and send_app_command(hwnd, command):
            self._sample_soon(0.8)  # (Spotify reports the change to SMTC about 0.7 s later)
        elif self._loop and self._session is not None:
            asyncio.run_coroutine_threadsafe(self._command(command), self._loop)
        else:
            # right after Spotify starts it has no SMTC session yet, so fall back to media keys
            press_media_key(command)

    def _sample_soon(self, delay: float):
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def later():
            await asyncio.sleep(delay)
            self._wake.set()
        try:
            asyncio.run_coroutine_threadsafe(later(), loop)
        except RuntimeError:  # the loop just closed (SMTC restarting)
            pass

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
        # Spotify's playback status in SMTC can get stuck; its window title can't
        if not _u32.IsWindow(self._window):
            self._window = app_window(self._exe)
        said = window_says_playing(self._window)
        if said is not None:
            playing = said

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
