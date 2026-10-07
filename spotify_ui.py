"""Read the on/off state of the lyrics button (microphone icon) in Spotify's playback bar.

Spotify is built on Chromium, which exposes its UI through UI Automation (the API screen readers use);
the lyrics button shows up there as a button with a toggle state.
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

# the button's name in Spotify's UI languages
BUTTON_NAMES = ["Lyrics", "歌词", "歌詞", "가사", "Letra", "Paroles", "Songtext", "Testo"]
TREE_SCOPE_DESCENDANTS = 4
UIA_NAME = 30005
UIA_CONTROL_TYPE = 30003
UIA_BUTTON = 50000
UIA_IS_TOGGLE_AVAILABLE = 30041
UIA_TOGGLE_STATE = 30086
MISSING_AFTER = 8  # seconds: if Spotify is open but the button can't be found for this long, give up on it

_k32 = ctypes.WinDLL("kernel32")
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _exe_name(pid: int) -> str:
    h = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(len(buf))
        if not _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        _k32.CloseHandle(h)


class LyricsButtonWatcher(threading.Thread):
    """Polls the button and calls on_change when its state changes:
    "on" / "off": the button is on / off; "pending": Spotify isn't open yet; "missing": button not found.
    """

    def __init__(self, on_change, spotify_running, interval: float = 0.4):
        super().__init__(name="LyricsButtonWatcher", daemon=True)
        self._on_change = on_change
        self._spotify_running = spotify_running
        self._interval = interval
        self._state = "pending"

    def _emit(self, state: str):
        if state != self._state:
            self._state = state
            log.info("Spotify lyrics button: %s", state)
            self._on_change(state)

    def run(self):
        # uiautomation sets the process DPI awareness on import; importing it here, after Qt has
        # initialized, keeps it from overriding Qt's setting
        import uiautomation as auto

        with auto.UIAutomationInitializerInThread():
            uia = auto.uiautomation._AutomationClient.instance().IUIAutomation
            names = uia.CreatePropertyCondition(UIA_NAME, BUTTON_NAMES[0])
            for n in BUTTON_NAMES[1:]:
                names = uia.CreateOrCondition(names, uia.CreatePropertyCondition(UIA_NAME, n))
            cond = uia.CreateAndCondition(uia.CreatePropertyCondition(UIA_CONTROL_TYPE, UIA_BUTTON), names)

            win = None
            last_found = time.monotonic()
            prev_read = None
            while True:
                time.sleep(self._interval)
                if not self._spotify_running():
                    win, prev_read, last_found = None, None, time.monotonic()
                    self._emit("pending")
                    continue
                state = None
                try:
                    if win is None:
                        win = self._find_window(auto)
                    if win is not None:
                        state = self._read(win.Element, cond)
                except Exception:
                    log.debug("Failed to read the lyrics button", exc_info=True)
                if state is None:
                    win = None  # the window may have changed; look it up again next time
                    if time.monotonic() - last_found > MISSING_AFTER:
                        self._emit("missing")
                    continue
                last_found = time.monotonic()
                # the state can be off for a moment while the button re-renders,
                # so only trust it after two identical reads
                if state == prev_read:
                    self._emit(state)
                prev_read = state

    @staticmethod
    def _find_window(auto):
        for w in auto.GetRootControl().GetChildren():
            if w.ClassName == "Chrome_WidgetWin_1" and w.Name and _exe_name(w.ProcessId) == "spotify.exe":
                return w
        return None

    @staticmethod
    def _read(root, cond) -> str | None:
        el = root.FindFirst(TREE_SCOPE_DESCENDANTS, cond)
        if not el or not el.GetCurrentPropertyValue(UIA_IS_TOGGLE_AVAILABLE):
            return None
        return "on" if el.GetCurrentPropertyValue(UIA_TOGGLE_STATE) == 1 else "off"
