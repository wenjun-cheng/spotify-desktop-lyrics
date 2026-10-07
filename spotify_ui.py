"""读取 Spotify 播放栏里「歌词」按钮（麦克风图标）的开关状态。

Spotify 是 Chromium 做的，会通过 UI Automation（读屏软件用的接口）暴露界面，
歌词按钮在里面是一个带开关状态（ToggleState）的按钮。
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes

log = logging.getLogger(__name__)

BUTTON_NAMES = ["歌词", "歌詞", "Lyrics", "가사", "Letra", "Paroles", "Songtext", "Testo"]  # 各语言界面
TREE_SCOPE_DESCENDANTS = 4
UIA_NAME = 30005
UIA_CONTROL_TYPE = 30003
UIA_BUTTON = 50000
UIA_IS_TOGGLE_AVAILABLE = 30041
UIA_TOGGLE_STATE = 30086
MISSING_AFTER = 8  # 秒：Spotify 开着却一直找不到按钮，就认为这个功能用不了

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
    """定时读按钮状态，变化时回调 on_change：
    "on" / "off"：按钮开着 / 关着；"pending"：Spotify 没开，还不知道；"missing"：找不到按钮。
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
            log.info("Spotify 歌词按钮：%s", state)
            self._on_change(state)

    def run(self):
        # 这个库导入时会设置进程的 DPI 模式，放到这里（Qt 已经初始化完）就不会覆盖 Qt 的设置
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
                    log.debug("读取歌词按钮失败", exc_info=True)
                if state is None:
                    win = None  # 窗口可能换了，下次重新找
                    if time.monotonic() - last_found > MISSING_AFTER:
                        self._emit("missing")
                    continue
                last_found = time.monotonic()
                # 按钮重新渲染的瞬间状态可能不准，连续两次读到一样的才算数
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
