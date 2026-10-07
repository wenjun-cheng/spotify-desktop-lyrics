"""Desktop lyrics overlay for Spotify. Run with `pythonw app.py`; add --debug for detailed logs."""
from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import sys
import time
import winreg
from concurrent.futures import Future, ThreadPoolExecutor
from ctypes import wintypes
from functools import partial
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QActionGroup, QColor, QCursor, QFont, QFontMetricsF, QGuiApplication, QIcon,
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QColorDialog, QFontDialog, QMenu, QSystemTrayIcon, QToolTip,
                               QWidget)

import i18n
import lyrics as lyr
from i18n import tr
from media import MediaWatcher, Snapshot, Track
from spotify_ui import LyricsButtonWatcher

__version__ = "0.2.0"
ROOT = Path(__file__).resolve().parent
FROZEN = getattr(sys, "frozen", False)  # running as a PyInstaller exe
DATA = Path(os.environ.get("APPDATA", str(Path.home()))) / "SpotifyLyrics"  # settings, lyrics cache, log
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "SpotifyLyrics"
SPOTIFY_EXE = Path(os.environ.get("APPDATA", "")) / "Spotify" / "Spotify.exe"
log = logging.getLogger("app")

DEFAULTS = {
    "language": "en",
    "x": None, "y": None, "width": 300,
    "font_family": "Microsoft YaHei UI", "font_size": 30,
    "color": "#ffffff", "highlight": "#1ed760",
    "locked": False, "second_line": True, "translation": True,
    "follow_button": True,  # show / hide together with Spotify's own lyrics button
    "offsets": {},     # per-song timing offset in seconds; positive = lyrics earlier
    "tips_shown": [],  # one-time tray tips that were already shown
}

_u32 = ctypes.WinDLL("user32")
_u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
_u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
GWL_EXSTYLE, WS_EX_TRANSPARENT, WS_EX_LAYERED = -20, 0x20, 0x80000


class Settings(dict):
    def __init__(self, path: Path):
        super().__init__(DEFAULTS)
        self.path = path
        self.first_run = not path.exists()
        if not self.first_run:
            try:
                self.update(json.loads(path.read_text("utf-8")))
            except Exception:
                log.exception("Couldn't read settings, using defaults")

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.path)


def _sing_seconds(text: str) -> float:
    """Rough guess of how long a line takes to sing; sets the speed of the highlight sweep."""
    cjk = len(lyr.CJK.findall(text))
    other = len(re.sub(r"\s", "", text)) - cjk
    return 1.0 + 0.45 * cjk + 0.07 * other


class LyricsApp(QObject):
    snapshot = Signal(object)
    fetched = Signal(str, object, bool)  # key, Lyrics | None, finished without error
    button_state = Signal(str)           # Spotify's lyrics button: on / off / pending / missing

    def __init__(self, debug: bool = False):
        super().__init__()
        self.settings = Settings(DATA / "settings.json")
        i18n.set_language(self.settings["language"])
        self.track: Track | None = None
        self.key = ""
        self.lyrics: lyr.Lyrics | None = None
        self.status = "idle"  # idle | skip | loading | ok | none | error
        self.pos, self.mono, self.playing = 0.0, time.monotonic(), False
        # The overlay comes and goes with Spotify and its lyrics button. After the user hides it,
        # it comes back the next time the lyrics button is clicked or Spotify is started.
        self.running = False
        self.lyrics_btn = "pending"
        self.hidden_by_user = False
        self.force_show = False

        self.pool = ThreadPoolExecutor(max_workers=2)
        # Wait a moment after a track change before searching: the duration arrives a bit later,
        # and skipping quickly through tracks won't trigger a search for each one.
        self.fetch_timer = QTimer(self, singleShot=True, interval=600, timeout=self.fetch)
        self.snapshot.connect(self._on_snapshot)
        self.fetched.connect(self._on_fetched)
        self.button_state.connect(self._on_button)

        self.menu = self._build_menu()
        self.overlay = Overlay(self)
        self.tray = QSystemTrayIcon(_make_icon(), self)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_tray)
        self._update_tooltip()
        self.tray.show()
        if self.settings.first_run:
            self.settings.save()
            self.tip("welcome", "tip_welcome")

        self.watcher = MediaWatcher(self.snapshot.emit, debug=debug)
        self.watcher.start()
        LyricsButtonWatcher(self.button_state.emit, lambda: self.running).start()

    def tip(self, name: str, key: str):
        """Show a tray tip, but only once ever."""
        if name in self.settings["tips_shown"]:
            return
        self.settings["tips_shown"].append(name)
        self.settings.save()
        self.tray.showMessage(tr("app_name"), tr(key), QSystemTrayIcon.MessageIcon.Information, 6000)

    def _update_tooltip(self):
        t = self.track
        self.tray.setToolTip(f"{tr('app_name')}\n{t.title} - {t.artist}" if t else tr("app_name"))

    # ------------------------------------------------------------ playback state

    def offset(self) -> float:
        return float(self.settings["offsets"].get(self.key, 0.0))

    def raw_position(self) -> float:
        return self.pos + (time.monotonic() - self.mono if self.playing else 0.0)

    def position(self) -> float:
        return self.raw_position() + self.offset()

    def _on_snapshot(self, snap: Snapshot):
        if snap.running != self.running:
            self.running = snap.running
            if snap.running:
                self.hidden_by_user = False
            else:
                self.force_show = False
        self.sync_visibility()

        self.pos, self.mono, self.playing = snap.position, snap.sampled_at, snap.playing
        track = snap.track
        key = lyr.track_key(track) if track else ""
        if key == self.key:
            self.track = track  # the duration may have just arrived
            return
        self.key, self.track, self.lyrics = key, track, None
        if not track:
            self.status = "idle"
        elif not track.artist:
            self.status = "skip"  # ads and the like have no artist
        else:
            self.status = "loading"
            self.fetch_timer.start()
        self._update_tooltip()

    def _on_button(self, state: str):
        prev, self.lyrics_btn = self.lyrics_btn, state
        if {prev, state} == {"on", "off"}:
            self.hidden_by_user = self.force_show = False  # the user clicked Spotify's button; follow it
        if state == "off" and self.settings["follow_button"]:
            self.tip("follow", "tip_follow")
        self.sync_visibility()

    def sync_visibility(self):
        # if the lyrics button can't be found ("missing"), fall back to "show while Spotify is open"
        follow_ok = not self.settings["follow_button"] or self.lyrics_btn in ("on", "missing")
        want = not self.hidden_by_user and (self.force_show or (self.running and follow_ok))
        if want != self.overlay.isVisible():
            self.overlay.setVisible(want)

    def control(self, command: str):
        """Playback control: toggle / next / prev."""
        if command == "toggle" and self.track:
            # flip the icon right away; the next sample will correct it if needed
            self.pos, self.mono, self.playing = self.raw_position(), time.monotonic(), not self.playing
        self.watcher.send(command)

    def hide_overlay(self):
        self.hidden_by_user = True
        self.sync_visibility()
        self.tip("hidden", "tip_hidden")

    def fetch(self, use_cache: bool = True):
        track, key = self.track, self.key
        if not track or not track.artist:
            return
        self.status = "loading"
        fut = self.pool.submit(lyr.find_lyrics, track, DATA / "cache", use_cache)

        def done(f: Future):
            try:
                self.fetched.emit(key, f.result(), True)
            except Exception:
                log.exception("Lyrics lookup failed")
                self.fetched.emit(key, None, False)

        fut.add_done_callback(done)

    def _on_fetched(self, key: str, lyrics, ok: bool):
        if key != self.key:
            return  # the track has changed in the meantime
        self.lyrics = lyrics
        self.status = "ok" if lyrics else "none" if ok else "error"

    def view(self) -> tuple[str, float | None, str, bool] | None:
        """What to show: (main line, highlight progress, second line, whether the second line is the next line)."""
        t = self.track
        if not t:
            return None
        head = f"{t.title} - {t.artist}" if t.artist else t.title
        if self.status == "skip":
            return head, None, "", False
        if self.status == "loading":
            return head, None, tr("searching"), True
        if self.status == "none":
            return head, None, tr("not_found"), True
        if self.status == "error":
            return head, None, tr("load_failed"), True

        L = self.lyrics
        if L.instrumental:
            return head, None, tr("instrumental"), True
        pos = self.position()
        i = L.index_at(pos)
        if i < 0:  # intro
            return head, None, L.next_text(0), True
        start, text = L.lines[i]
        end = L.lines[i + 1][0] if i + 1 < len(L.lines) else start + 6
        dur = min(end - start, max(2.5, _sing_seconds(text)))
        progress = min(1.0, max(0.0, (pos - start) / dur)) if dur > 0 else 1.0
        if self.settings["translation"] and L.trans[i]:
            return text or "♪", progress, L.trans[i], False
        return text or "♪", progress, L.next_text(i + 1), True

    # ------------------------------------------------------------ menu

    def _build_menu(self) -> QMenu:
        m = QMenu()
        self._labels = []  # (action, text key), re-translated every time the menu opens

        def add(menu: QMenu, key: str, slot, checkable: bool = False):
            a = menu.addAction(tr(key), slot)
            a.setCheckable(checkable)
            self._labels.append((a, key))
            return a

        self.act_info = m.addAction("")
        self.act_info.setEnabled(False)
        self.act_source = m.addAction("")
        self.act_source.setEnabled(False)
        self.act_open = add(m, "open_spotify", self.open_spotify)
        m.addSeparator()

        s = self.settings
        self.act_follow = add(m, "follow", self._toggle_follow, checkable=True)
        self.act_lock = add(m, "lock", lambda: self.set_locked(not s["locked"]), checkable=True)
        self.act_trans = add(m, "translation", lambda: self._toggle("translation"), checkable=True)
        self.act_second = add(m, "second_line", lambda: self._toggle("second_line", relayout=True), checkable=True)
        m.addSeparator()

        self.act_offset = m.addAction("")
        self.act_offset.setEnabled(False)
        add(m, "earlier", lambda: self.shift(+0.5))
        add(m, "later", lambda: self.shift(-0.5))
        add(m, "reset_offset", lambda: self.shift(None))
        add(m, "research", lambda: self.fetch(use_cache=False))
        m.addSeparator()

        look = m.addMenu(tr("appearance"))
        self._labels.append((look.menuAction(), "appearance"))
        add(look, "font_bigger", lambda: self.change_font(+2))
        add(look, "font_smaller", lambda: self.change_font(-2))
        add(look, "font", self.pick_font)
        add(look, "text_color", lambda: self.pick_color("color"))
        add(look, "highlight_color", lambda: self.pick_color("highlight"))

        langs = m.addMenu(tr("language"))
        group = QActionGroup(langs)
        self.act_langs = {}
        for code, name in i18n.LANGUAGES.items():
            a = langs.addAction(name)
            a.setCheckable(True)
            a.triggered.connect(partial(self.change_language, code))
            group.addAction(a)
            self.act_langs[code] = a

        self.act_autostart = add(m, "autostart", self.toggle_autostart, checkable=True)
        add(m, "quit", QApplication.quit)
        m.aboutToShow.connect(self._refresh_menu)
        return m

    def _refresh_menu(self):
        s, t = self.settings, self.track
        for a, key in self._labels:
            a.setText(tr(key))
        self.act_info.setText(f"♪ {t.title} - {t.artist}" if t else
                              tr("spotify_idle") if self.running else tr("spotify_closed"))
        if self.status == "ok" and self.lyrics:
            source = self.lyrics.source
            name = tr(f"source_{source}") if i18n.has(f"source_{source}") else source
            src = tr("lyrics_from", source=name, matched=self.lyrics.matched)
        else:
            src = {"loading": tr("lyrics_searching"), "none": tr("lyrics_none"),
                   "error": tr("lyrics_error")}.get(self.status, "")
        self.act_source.setText(src[:60] + ("…" if len(src) > 60 else ""))
        self.act_source.setVisible(bool(src))
        self.act_open.setVisible(not self.running)
        self.act_follow.setChecked(s["follow_button"])
        self.act_lock.setChecked(s["locked"])
        self.act_trans.setChecked(s["translation"])
        self.act_second.setChecked(s["second_line"])
        self.act_offset.setText(tr("offset_now", offset=self.offset()))
        for code, a in self.act_langs.items():
            a.setChecked(code == s["language"])
        self.act_autostart.setChecked(_autostart_enabled())

    def change_language(self, code: str, *_):
        self.settings["language"] = code
        self.settings.save()
        i18n.set_language(code)
        self._update_tooltip()
        self.overlay.update()

    def _toggle(self, name: str, relayout: bool = False):
        self.settings[name] = not self.settings[name]
        self.settings.save()
        if relayout:
            self.overlay.relayout()

    def _toggle_follow(self):
        self._toggle("follow_button")
        self.hidden_by_user = self.force_show = False
        self.sync_visibility()

    def _on_tray(self, reason):
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        if self.overlay.isVisible():
            self.hide_overlay()
        else:
            self.hidden_by_user, self.force_show = False, True
            self.sync_visibility()

    def open_spotify(self):
        os.startfile(str(SPOTIFY_EXE) if SPOTIFY_EXE.exists() else "spotify:")

    def set_locked(self, locked: bool):
        self.settings["locked"] = locked
        self.settings.save()
        self.overlay.apply_lock()
        if locked:
            self.tip("locked", "tip_locked")

    def shift(self, delta: float | None):
        if not self.key:
            return
        offsets = self.settings["offsets"]
        value = 0.0 if delta is None else round(self.offset() + delta, 2)
        if value:
            offsets[self.key] = value
        else:
            offsets.pop(self.key, None)
        self.settings.save()

    def change_font(self, delta: int):
        self.settings["font_size"] = max(14, min(96, self.settings["font_size"] + delta))
        self.settings.save()
        self.overlay.relayout()

    def pick_font(self):
        ok, font = QFontDialog.getFont(QFont(self.settings["font_family"]), None, tr("font_title"))
        if ok:
            self.settings["font_family"] = font.family()
            self.settings.save()

    def pick_color(self, name: str):
        c = QColorDialog.getColor(QColor(self.settings[name]), None, tr("color_title"))
        if c.isValid():
            self.settings[name] = c.name()
            self.settings.save()

    def toggle_autostart(self):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            if _autostart_enabled():
                winreg.DeleteValue(k, RUN_NAME)
            else:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _launch_command())


def _launch_command() -> str:
    if FROZEN:
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" "{ROOT / "app.py"}"'


def _autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def _icon_font(pixel_size: int) -> QFont:
    f = QFont()
    f.setFamilies(["Segoe Fluent Icons", "Segoe MDL2 Assets"])
    f.setPixelSize(pixel_size)
    return f


# glyphs from Segoe Fluent Icons / Segoe MDL2 Assets
ICON = {"prev": chr(0xE892), "play": chr(0xE768), "pause": chr(0xE769), "next": chr(0xE893),
        "lock": chr(0xE72E), "unlock": chr(0xE785), "menu": chr(0xE713), "close": chr(0xE8BB),
        "mic": chr(0xE720)}
# (button, tooltip text key); None is a gap between the two groups
BUTTONS = [("prev", "btn_prev"), ("toggle", "btn_toggle"), ("next", "btn_next"), None,
           ("lock", "btn_lock"), ("menu", "btn_menu"), ("close", "btn_close")]


def _make_icon() -> QIcon:
    """Green circle with a microphone, like Spotify's lyrics button."""
    pm = QPixmap(256, 256)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#1ed760"))
    p.drawEllipse(8, 8, 240, 240)
    p.setFont(_icon_font(136))
    p.setPen(QColor("#000000"))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, ICON["mic"])
    p.end()
    return QIcon(pm)


class Overlay(QWidget):
    PAD = 12
    EDGE = 10    # dragging within this distance of the left / right edge resizes the window
    BTN = 30     # button size
    TB_TOP = 6   # distance from the top to the button bar
    TOP = 42     # distance from the top to the lyrics (the button bar sits above them)

    def __init__(self, app: LyricsApp):
        super().__init__(None)
        self.app = app
        self.s = app.settings
        self.hover = False
        self.hot: str | None = None      # button under the mouse
        self._click_through: bool | None = None
        self.setWindowTitle(tr("app_name"))
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMinimumWidth(300)
        self.icon_font = _icon_font(15)
        self.relayout()
        self.resize(self.s["width"], self.height())
        self._place()

        self.save_timer = QTimer(self, singleShot=True, interval=500, timeout=self._save_geometry)
        self.frame_timer = QTimer(self, interval=33, timeout=self._tick)

    def _place(self):
        x, y = self.s["x"], self.s["y"]
        if x is not None and y is not None and QGuiApplication.screenAt(
                QPoint(x + self.width() // 2, y + self.height() // 2)):
            self.move(x, y)
            return
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.move(area.center().x() - self.width() // 2, area.bottom() - self.height() - 60)

    def _fonts(self) -> tuple[QFont, QFont]:
        size = self.s["font_size"]
        main = QFont(self.s["font_family"])
        main.setPixelSize(size)
        main.setBold(True)
        sub = QFont(main)
        sub.setPixelSize(max(10, round(size * 0.62)))
        return main, sub

    def _line_heights(self) -> tuple[float, float]:
        size = self.s["font_size"]
        return size * 1.4, (round(size * 0.62) * 1.4 if self.s["second_line"] else 0.0)

    def relayout(self):
        h1, h2 = self._line_heights()
        self.setFixedHeight(int(self.TOP + h1 + h2 + self.PAD))

    def _buttons(self) -> list[tuple[str, QRectF]]:
        gap, sep = 2, 16
        n = sum(1 for b in BUTTONS if b)
        total = n * self.BTN + (n - 1) * gap + sep
        x = (self.width() - total) / 2
        out = []
        for b in BUTTONS:
            if b is None:
                x += sep - gap
                continue
            out.append((b[0], QRectF(x, self.TB_TOP, self.BTN, self.BTN)))
            x += self.BTN + gap
        return out

    def _hit(self, pt: QPointF) -> str | None:
        return next((key for key, r in self._buttons() if r.contains(pt)), None)

    # ------------------------------------------------------------ locked = click-through

    def _set_click_through(self, on: bool):
        if on == self._click_through:
            return
        hwnd = int(self.winId())
        style = _u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TRANSPARENT | WS_EX_LAYERED) if on else (style & ~WS_EX_TRANSPARENT)
        _u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
        self._click_through = on

    def apply_lock(self):
        self._set_click_through(self.s["locked"])

    def showEvent(self, _):
        self._click_through = None
        self.apply_lock()
        self.frame_timer.start()

    def hideEvent(self, _):
        self.frame_timer.stop()
        QToolTip.hideText()

    def _tick(self):
        pos = QPointF(self.mapFromGlobal(QCursor.pos()))
        self.hover = self.rect().contains(pos.toPoint())
        hot = self._hit(pos) if self.hover else None
        if self.s["locked"]:
            # while locked the whole window is click-through, except when the mouse is on the small lock
            hot = hot if hot == "lock" else None
            self._set_click_through(hot is None)
        if hot != self.hot:
            self.hot = hot
            tips = dict(b for b in BUTTONS if b)
            if hot:
                QToolTip.showText(QCursor.pos(), tr("btn_unlock" if self.s["locked"] else tips[hot]), self)
            else:
                QToolTip.hideText()
        if not self.s["locked"]:
            if hot:
                shape = Qt.CursorShape.PointingHandCursor
            elif self._edge(pos.x()):
                shape = Qt.CursorShape.SizeHorCursor
            else:
                shape = Qt.CursorShape.SizeAllCursor
            if self.cursor().shape() != shape:
                self.setCursor(shape)
        self.update()

    # ------------------------------------------------------------ painting

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        locked = self.s["locked"]
        if not locked:
            # fully transparent pixels let clicks through, so keep an almost invisible background
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 110 if self.hover else 1))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
        if self.hover:
            self._draw_buttons(p, locked)

        main_font, sub_font = self._fonts()
        h1, h2 = self._line_heights()
        w = self.width() - 2 * self.PAD
        rect1 = QRectF(self.PAD, self.TOP, w, h1)
        rect2 = QRectF(self.PAD, self.TOP + h1, w, h2)
        color = QColor(self.s["color"])

        view = self.app.view()
        if view is None:
            if not locked:
                hint = QColor(color)
                hint.setAlpha(170)
                self._draw_text(p, tr("waiting"), sub_font, rect1, hint)
            return

        main, progress, sub, sub_is_next = view
        if not self.app.playing:
            p.setOpacity(0.55)
        self._draw_text(p, main, main_font, rect1, color, progress, QColor(self.s["highlight"]))
        if h2 and sub:
            c2 = QColor(color)
            c2.setAlpha(165 if sub_is_next else 235)
            self._draw_text(p, sub, sub_font, rect2, c2)

    def _draw_buttons(self, p: QPainter, locked: bool):
        p.setFont(self.icon_font)
        for key, r in self._buttons():
            if locked and key != "lock":
                continue
            if locked:  # only the small lock, on its own background so it stays visible on any wallpaper
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(0, 0, 0, 150 if self.hot else 100))
                p.drawRoundedRect(r, 8, 8)
            elif key == self.hot:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 50))
                p.drawRoundedRect(r, 6, 6)
            glyph = {"toggle": ICON["pause"] if self.app.playing else ICON["play"],
                     "lock": ICON["unlock"] if locked else ICON["lock"]}.get(key) or ICON[key]
            p.setPen(QColor(255, 255, 255, 235))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, glyph)

    def _draw_text(self, p: QPainter, text: str, font: QFont, rect: QRectF, color: QColor,
                   progress: float | None = None, highlight: QColor | None = None):
        fm = QFontMetricsF(font)
        width = fm.horizontalAdvance(text)
        if width > rect.width():  # too long: shrink the font to fit
            font = QFont(font)
            font.setPixelSize(max(10, int(font.pixelSize() * rect.width() / width)))
            fm = QFontMetricsF(font)
            width = fm.horizontalAdvance(text)
        x = rect.x() + (rect.width() - width) / 2
        baseline = rect.y() + (rect.height() + fm.ascent() - fm.descent()) / 2
        path = QPainterPath()
        path.addText(QPointF(x, baseline), font, text)

        outline = max(2.0, font.pixelSize() / 8)
        p.strokePath(path.translated(1.2, 1.6), QPen(QColor(0, 0, 0, 70), outline, Qt.PenStyle.SolidLine,
                                                     Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.strokePath(path, QPen(QColor(0, 0, 0, 160), outline, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.fillPath(path, color)
        if progress:
            p.save()
            p.setClipRect(QRectF(x - 2, rect.y(), (width + 4) * progress, rect.height()))
            p.fillPath(path, highlight)
            p.restore()

    # ------------------------------------------------------------ mouse

    def _edge(self, x: float):
        if x <= self.EDGE:
            return Qt.Edge.LeftEdge
        if x >= self.width() - self.EDGE:
            return Qt.Edge.RightEdge
        return None

    def mousePressEvent(self, e):
        pos = e.position()
        if e.button() == Qt.MouseButton.LeftButton:
            key = self._hit(pos)
            if self.s["locked"]:
                if key == "lock":
                    self.app.set_locked(False)
            elif key in ("prev", "toggle", "next"):
                self.app.control(key)
            elif key == "lock":
                self.app.set_locked(True)
            elif key == "menu":
                r = dict(self._buttons())["menu"]
                self.app.menu.popup(self.mapToGlobal(r.bottomLeft().toPoint()))
            elif key == "close":
                self.app.hide_overlay()
            elif edge := self._edge(pos.x()):
                self.windowHandle().startSystemResize(edge)
            else:
                self.windowHandle().startSystemMove()
        elif e.button() == Qt.MouseButton.RightButton and not self.s["locked"]:
            self.app.menu.popup(e.globalPosition().toPoint())

    def wheelEvent(self, e):
        if not self.s["locked"]:
            self.app.change_font(+2 if e.angleDelta().y() > 0 else -2)

    def moveEvent(self, _):
        self.save_timer.start()

    def resizeEvent(self, _):
        self.save_timer.start()

    def _save_geometry(self):
        self.s["x"], self.s["y"], self.s["width"] = self.x(), self.y(), self.width()
        self.s.save()


def main():
    debug = "--debug" in sys.argv
    DATA.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(DATA / "app.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.excepthook = lambda *exc: log.critical("Unhandled exception", exc_info=exc)

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    lock = QLockFile(str(DATA / "app.lock"))
    if not lock.tryLock(100):
        return  # another instance is already running
    app = LyricsApp(debug=debug)
    log.info("Started v%s", __version__)
    code = qapp.exec()
    app.pool.shutdown(wait=False, cancel_futures=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
