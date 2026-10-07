"""搜索带时间轴的歌词：QQ音乐 / 网易云 / LRCLIB，结果缓存在本地。"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from bisect import bisect_right
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import partial
from pathlib import Path
from typing import Callable

import zhconv

from media import Track

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
ACCEPT = 0.6     # 低于这个分数的候选直接不要
GOOD = 0.78      # 歌名 + 时长都对上就够了，不用再查其它源
MISS_TTL = 3 * 24 * 3600  # 没找到的歌 3 天内不再重复搜索


@dataclass
class Lyrics:
    lines: list[tuple[float, str]]  # (开始秒数, 文本)，按时间排序
    trans: list[str]                # 和 lines 一一对应的译文，没有就是 ""
    source: str
    matched: str                    # 实际匹配到的「歌名 - 歌手」
    starts: list[float] = field(init=False, repr=False)

    def __post_init__(self):
        self.starts = [t for t, _ in self.lines]

    def index_at(self, t: float) -> int:
        """t 时刻正在唱的行号；还没到第一行时返回 -1。"""
        return bisect_right(self.starts, t) - 1

    def next_text(self, i: int) -> str:
        """从第 i 行起第一句非空歌词。"""
        return next((s for _, s in self.lines[max(i, 0):] if s), "")

    def to_dict(self) -> dict:
        return {"lines": self.lines, "trans": self.trans, "source": self.source, "matched": self.matched}

    @classmethod
    def from_dict(cls, d: dict) -> Lyrics:
        return cls([(float(t), s) for t, s in d["lines"]], list(d["trans"]), d["source"], d["matched"])


@dataclass
class Candidate:
    name: str          # 歌名本体，用来比相似度
    full: str          # 带版本说明的完整标题，用来识别 Live / 伴奏 / 翻唱
    artists: list[str]
    duration: float    # 秒
    fetch: Callable[[], tuple[str, str]]  # -> (lrc, 译文 lrc)


# ---------------------------------------------------------------- LRC 解析

_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_OFFSET = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.I)


def parse_lrc(text: str) -> list[tuple[float, str]]:
    offset = 0.0
    out = []
    for raw in text.splitlines():
        raw = raw.strip()
        if m := _OFFSET.match(raw):
            offset = int(m.group(1)) / 1000
            continue
        stamps, pos = [], 0
        while m := _STAMP.match(raw, pos):
            stamps.append(int(m.group(1)) * 60 + float(m.group(2).replace(":", ".")))
            pos = m.end()
        text_part = raw[pos:].strip()
        if text_part == "//":  # QQ 音乐译文里的「本句无翻译」
            text_part = ""
        out.extend((t, text_part) for t in stamps)
    out.sort(key=lambda x: x[0])
    # LRC 的 offset 为正表示歌词要提前出现
    return [(max(0.0, t - offset), s) for t, s in out]


def build_lyrics(lrc: str, trans_lrc: str, source: str, matched: str) -> Lyrics | None:
    lines = parse_lrc(lrc)
    if not any(s for _, s in lines):
        return None
    if sum(1 for t, _ in lines if t > 0) < 2 and not any("纯音乐" in s for _, s in lines):
        return None  # 没有时间轴，没法同步
    by_cs = {round(t * 100): s for t, s in parse_lrc(trans_lrc) if s}
    trans = []
    for t, s in lines:
        cs = round(t * 100)
        tr = by_cs.get(cs) or by_cs.get(cs - 1) or by_cs.get(cs + 1) or ""
        trans.append("" if tr == s or not s else tr)
    return Lyrics(lines, trans, source, matched)


# ---------------------------------------------------------------- 匹配打分

_BRACKETS = re.compile(r"[(\[（【「《<].*?[)\]）】」》>]")
_VERSION_WORDS = [
    "live", "cover", "remix", "instrumental", "inst", "karaoke", "acoustic", "demo",
    "piano", "dj", "伴奏", "翻唱", "原唱", "钢琴", "现场", "演唱会", "铃声", "片段",
    "抖音", "女声版", "男声版", "加速", "降调", "纯音乐",
]


def _norm(s: str) -> str:
    s = zhconv.convert(s, "zh-cn").lower().replace("’", "'")
    core = _BRACKETS.sub(" ", s)
    core = re.split(r"\s+[-–—]\s+", core)[0]          # "Song - Remastered 2011"
    core = re.sub(r"\b(?:feat|ft)\b\.?.*$", "", core)  # "Song feat. X"
    core = re.sub(r"[\W_]+", "", core)
    return core or re.sub(r"[\W_]+", "", s)


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


def _split_artists(s: str) -> list[str]:
    return [x for x in re.split(r"\s*(?:,|&|、|/|;|\bfeat\.?|\bft\.)\s*", s, flags=re.I) if x]


def _version_words(s: str) -> set[str]:
    s = zhconv.convert(s, "zh-cn").lower()
    found = set()
    for w in _VERSION_WORDS:
        pat = rf"\b{w}\b" if w.isascii() else re.escape(w)
        if re.search(pat, s):
            found.add(w)
    return found


def score(c: Candidate, track: Track) -> float:
    t = _sim(c.name, track.title)
    if t < 0.5:
        return 0.0
    wanted = _split_artists(track.artist) or [track.artist]
    a = max((_sim(x, y) for x in c.artists for y in wanted), default=0.0)
    if a < 0.6:
        a = 0.0  # "Jay Chou" 对 "周杰伦" 这种比不了的，零碎的相似度只是噪音
    if track.duration and c.duration:
        diff = abs(track.duration - c.duration)
        d = 1.0 if diff <= 2 else 0.7 if diff <= 5 else 0.3 if diff <= 10 else 0.0
    else:
        d = 0.5
    penalty = 0.25 if _version_words(c.full) - _version_words(track.title) else 0.0
    return 0.5 * t + 0.2 * a + 0.3 * d - penalty


# ---------------------------------------------------------------- 歌词源

def _http(url: str, *, data=None, headers=None, timeout: float = 8):
    h = {"User-Agent": UA}
    h.update(headers or {})
    if data is not None:
        data = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _clean_title(title: str) -> str:
    """搜索用：去掉 " - Remastered"、"(feat. X)" 之类会干扰搜索的尾巴。"""
    t = re.split(r"\s+[-–—]\s+", title)[0]
    t = re.sub(r"\s*[(\[（【][^)\]）】]*(?:feat|ft\.|with|remaster|version|版)[^)\]）】]*[)\]）】]", "", t, flags=re.I)
    return t.strip() or title


def _query(track: Track, title_only: bool) -> str:
    title = _clean_title(track.title)
    return title if title_only else f"{title} {track.artist}".strip()


QQ_HEADERS = {"Referer": "https://y.qq.com/"}


def qq_search(track: Track, title_only: bool) -> list[Candidate]:
    body = {
        "comm": {"ct": 19, "cv": 1859},
        "req": {"module": "music.search.SearchCgiService", "method": "DoSearchForQQMusicDesktop",
                "param": {"query": _query(track, title_only), "num_per_page": 10, "page_num": 1, "search_type": 0}},
    }
    d = _http("https://u.y.qq.com/cgi-bin/musicu.fcg", data=body, headers=QQ_HEADERS)
    songs = d["req"]["data"]["body"]["song"]["list"]
    return [Candidate(s["name"], s.get("title") or s["name"], [a["name"] for a in s.get("singer", [])],
                      float(s.get("interval") or 0), partial(qq_lyric, s["mid"]))
            for s in songs]


def qq_lyric(mid: str) -> tuple[str, str]:
    d = _http("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?format=json&nobase64=1&g_tk=5381&songmid="
              + urllib.parse.quote(mid), headers=QQ_HEADERS)
    return html.unescape(d.get("lyric") or ""), html.unescape(d.get("trans") or "")


NE_HEADERS = {"Referer": "https://music.163.com/"}


def netease_search(track: Track, title_only: bool) -> list[Candidate]:
    d = _http("https://music.163.com/api/cloudsearch/pc?type=1&limit=10&s="
              + urllib.parse.quote(_query(track, title_only)), headers=NE_HEADERS)
    out = []
    for s in (d.get("result") or {}).get("songs") or []:
        artists = []
        for a in s.get("ar") or []:
            artists += [a.get("name") or "", *(a.get("alia") or []), *(a.get("tns") or [])]
        full = " ".join([s["name"], *(s.get("alia") or []), *(s.get("tns") or [])])
        out.append(Candidate(s["name"], full, [x for x in artists if x],
                             (s.get("dt") or 0) / 1000, partial(netease_lyric, s["id"])))
    return out


def netease_lyric(song_id: int) -> tuple[str, str]:
    d = _http(f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&tv=-1", headers=NE_HEADERS)
    if d.get("pureMusic") or d.get("nolyric"):
        return "[00:00.00]纯音乐，请欣赏", ""
    return (d.get("lrc") or {}).get("lyric") or "", (d.get("tlyric") or {}).get("lyric") or ""


def lrclib_search(track: Track, title_only: bool) -> list[Candidate]:
    params = ({"q": _clean_title(track.title)} if title_only
              else {"track_name": _clean_title(track.title), "artist_name": track.artist})
    d = _http("https://lrclib.net/api/search?" + urllib.parse.urlencode(params),
              headers={"User-Agent": "SpotifyLyricsOverlay/1.0"})
    return [Candidate(s["trackName"], s["trackName"], [s.get("artistName") or ""], float(s.get("duration") or 0),
                      partial(lambda lrc: (lrc, ""), s["syncedLyrics"]))
            for s in d[:15] if s.get("syncedLyrics")]


SOURCES = {"QQ音乐": qq_search, "网易云": netease_search, "LRCLIB": lrclib_search}
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")  # 中日韩文字


# ---------------------------------------------------------------- 对外接口

def track_key(track: Track) -> str:
    return f"{track.title}\n{track.artist}\n{track.album}"


def find_lyrics(track: Track, cache_dir: Path, use_cache: bool = True) -> Lyrics | None:
    cache = cache_dir / (hashlib.sha1(track_key(track).encode()).hexdigest() + ".json")
    if use_cache and cache.exists():
        try:
            d = json.loads(cache.read_text("utf-8"))
            if "lyrics" in d:
                return Lyrics.from_dict(d["lyrics"])
            if time.time() - d.get("miss", 0) < MISS_TTL:
                return None
        except Exception:
            log.exception("缓存损坏：%s", cache)

    order = ["QQ音乐", "网易云", "LRCLIB"] if CJK.search(track.title + track.artist) else ["网易云", "QQ音乐", "LRCLIB"]
    best: tuple[float, Lyrics] | None = None
    errors = 0
    for title_only in (False, True):
        for name in order:
            try:
                cands = SOURCES[name](track, title_only)
            except Exception as e:
                errors += 1
                log.warning("%s 搜索失败：%s", name, e)
                continue
            # 同分时按搜索结果原本的排名
            ranked = sorted(((score(c, track) - 0.001 * i, c) for i, c in enumerate(cands)),
                            key=lambda x: x[0], reverse=True)
            for s, c in ranked[:3]:
                if s < ACCEPT or (best and s <= best[0]):
                    break
                try:
                    lyr = build_lyrics(*c.fetch(), name, f"{c.full} - {' / '.join(c.artists[:2])}")
                except Exception as e:
                    log.warning("%s 取歌词失败：%s", name, e)
                    continue
                if lyr:
                    best = (s, lyr)
                    log.info("候选 %.2f [%s] %s", s, name, lyr.matched)
                    break
            if best and best[0] >= GOOD:
                break
        if best:
            break

    if best is None and errors:
        if errors == 2 * len(order):
            raise ConnectionError("所有歌词源都连不上")
        return None  # 有歌词源没连上，这次的「没找到」不算数，不写缓存
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {"lyrics": best[1].to_dict()} if best else {"miss": time.time()}
    cache.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    log.info("%s - %s -> %s", track.title, track.artist, best[1].matched if best else "未找到")
    return best[1] if best else None
