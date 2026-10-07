"""UI text in English and Chinese. English is the default."""
from __future__ import annotations

LANGUAGES = {"en": "English", "zh": "中文"}

# key: (English, Chinese)
_STRINGS = {
    "app_name": ("Spotify Lyrics", "Spotify 歌词"),

    # overlay
    "waiting": ("Waiting for Spotify…", "等待 Spotify 播放…"),
    "searching": ("Searching for lyrics…", "正在搜索歌词…"),
    "not_found": ("No lyrics found", "没有找到歌词"),
    "load_failed": ("Couldn't load lyrics. Right-click → Search lyrics again",
                    "歌词加载失败，右键 → 重新搜索歌词"),
    "instrumental": ("Instrumental", "纯音乐，请欣赏"),

    # overlay buttons
    "btn_prev": ("Previous", "上一首"),
    "btn_toggle": ("Play / Pause", "播放 / 暂停"),
    "btn_next": ("Next", "下一首"),
    "btn_lock": ("Lock (click-through)", "锁定（鼠标穿透）"),
    "btn_unlock": ("Unlock", "解锁"),
    "btn_menu": ("Settings", "设置"),
    "btn_close": ("Hide lyrics", "隐藏歌词"),

    # menu
    "spotify_idle": ("Spotify isn't playing", "Spotify 没在播放"),
    "spotify_closed": ("Spotify isn't open", "Spotify 没有打开"),
    "lyrics_from": ("Lyrics: {source} ({matched})", "歌词：{source}（{matched}）"),
    "lyrics_searching": ("Lyrics: searching…", "歌词：搜索中…"),
    "lyrics_none": ("Lyrics: not found", "歌词：没找到"),
    "lyrics_error": ("Lyrics: failed to load", "歌词：加载失败"),
    "open_spotify": ("Open Spotify", "打开 Spotify"),
    "follow": ("Follow Spotify's lyrics button", "跟随 Spotify 歌词按钮"),
    "lock": ("Lock (click-through)", "锁定（鼠标穿透）"),
    "translation": ("Show translation", "显示翻译"),
    "second_line": ("Show second line", "显示第二行"),
    "offset_now": ("Offset for this song: {offset:+.1f} s", "当前歌曲偏移：{offset:+.1f} 秒"),
    "earlier": ("Lyrics 0.5 s earlier", "歌词提前 0.5 秒"),
    "later": ("Lyrics 0.5 s later", "歌词延后 0.5 秒"),
    "reset_offset": ("Reset offset", "偏移归零"),
    "research": ("Search lyrics again", "重新搜索歌词"),
    "appearance": ("Appearance", "外观"),
    "font_bigger": ("Larger text", "字号 +"),
    "font_smaller": ("Smaller text", "字号 −"),
    "font": ("Font…", "字体…"),
    "text_color": ("Text color…", "文字颜色…"),
    "highlight_color": ("Highlight color…", "高亮颜色…"),
    "language": ("Language / 语言", "Language / 语言"),
    "autostart": ("Start with Windows", "开机自动启动"),
    "quit": ("Quit", "退出"),
    "font_title": ("Lyrics font", "歌词字体"),
    "color_title": ("Choose a color", "选择颜色"),

    # one-time tips
    "tip_welcome": ("Spotify Lyrics is running in the system tray. Hover over the lyrics for controls; "
                    "right-click for settings.",
                    "Spotify 歌词已在托盘运行。鼠标移到歌词上会出现控制按钮，右键打开设置。"),
    "tip_follow": ("Click the lyrics button (microphone icon) in Spotify's playback bar to show desktop "
                   "lyrics; click it again to hide them.",
                   "点 Spotify 播放栏里的「歌词」按钮（麦克风图标）就会显示桌面歌词，再点一次隐藏。"),
    "tip_hidden": ("Lyrics hidden. Click the tray icon or Spotify's lyrics button to show them again.",
                   "歌词已隐藏。单击托盘图标或 Spotify 的歌词按钮可以重新显示。"),
    "tip_locked": ("Locked: clicks now go through the lyrics. Hover over the lyrics and click the small "
                   "lock to unlock.",
                   "已锁定，鼠标会穿过歌词。把鼠标移到歌词上方，点出现的小锁就能解锁。"),

    # lyrics sources
    "source_qq": ("QQ Music", "QQ 音乐"),
    "source_netease": ("NetEase Cloud Music", "网易云音乐"),
    "source_lrclib": ("LRCLIB", "LRCLIB"),
}

_lang = "en"


def set_language(lang: str):
    global _lang
    _lang = lang if lang in LANGUAGES else "en"


def tr(key: str, **kwargs) -> str:
    en, zh = _STRINGS[key]
    text = zh if _lang == "zh" else en
    return text.format(**kwargs) if kwargs else text


def has(key: str) -> bool:
    return key in _STRINGS
