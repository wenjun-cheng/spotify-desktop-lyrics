"""Find time-synced lyrics on QQ Music / NetEase Cloud Music / LRCLIB, with a local cache."""
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
ACCEPT = 0.6     # candidates scoring below this are ignored
GOOD = 0.78      # title + duration match: good enough, skip the remaining sources
MISS_TTL = 3 * 24 * 3600  # don't search again for a song we failed to find within 3 days

# Chinese, Japanese and Korean characters
CJK = re.compile("[%s-%s%s-%s%s-%s]" % (chr(0x3040), chr(0x30FF), chr(0x3400), chr(0x9FFF),
                                        chr(0xAC00), chr(0xD7AF)))
INSTRUMENTAL_MARK = "纯音乐"  # how QQ Music / NetEase label songs without lyrics
_LEGACY_SOURCES = {"QQ音乐": "qq", "网易云": "netease", "LRCLIB": "lrclib"}  # caches from v0.1.0


@dataclass
class Lyrics:
    lines: list[tuple[float, str]]  # (start in seconds, text), sorted by time
    trans: list[str]                # translation of each line, "" if none
    source: str                     # "qq" / "netease" / "lrclib"
    matched: str                    # "title - artist" of the song we actually matched
    instrumental: bool = False
    starts: list[float] = field(init=False, repr=False)

    def __post_init__(self):
        self.starts = [t for t, _ in self.lines]

    def index_at(self, t: float) -> int:
        """Index of the line being sung at time t, or -1 before the first line."""
        return bisect_right(self.starts, t) - 1

    def next_text(self, i: int) -> str:
        """First non-empty line from line i on."""
        return next((s for _, s in self.lines[max(i, 0):] if s), "")

    def to_dict(self) -> dict:
        return {"lines": self.lines, "trans": self.trans, "source": self.source, "matched": self.matched,
                "instrumental": self.instrumental}

    @classmethod
    def from_dict(cls, d: dict) -> Lyrics:
        lines = [(float(t), s) for t, s in d["lines"]]
        instrumental = d.get("instrumental") or (len(lines) == 1 and INSTRUMENTAL_MARK in lines[0][1])
        return cls(lines, list(d["trans"]), _LEGACY_SOURCES.get(d["source"], d["source"]), d["matched"],
                   bool(instrumental))


@dataclass
class Candidate:
    name: str          # bare song title, used for similarity
    full: str          # full title with version notes, used to spot live / karaoke / cover versions
    artists: list[str]
    duration: float    # seconds
    fetch: Callable[[], tuple[str, str]]  # -> (lrc, translation lrc)


# ---------------------------------------------------------------- LRC parsing

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
        if text_part == "//":  # QQ Music's "no translation for this line"
            text_part = ""
        out.extend((t, text_part) for t in stamps)
    out.sort(key=lambda x: x[0])
    # a positive LRC offset means the lyrics should show up earlier
    return [(max(0.0, t - offset), s) for t, s in out]


def build_lyrics(lrc: str, trans_lrc: str, source: str, matched: str) -> Lyrics | None:
    lines = parse_lrc(lrc)
    if not any(s for _, s in lines):
        return None
    if sum(1 for t, _ in lines if t > 0) < 2:
        if any(INSTRUMENTAL_MARK in s for _, s in lines):
            return Lyrics([(0.0, "")], [""], source, matched, instrumental=True)
        return None  # no timestamps, can't sync
    by_cs = {round(t * 100): s for t, s in parse_lrc(trans_lrc) if s}
    trans = []
    for t, s in lines:
        cs = round(t * 100)
        tr = by_cs.get(cs) or by_cs.get(cs - 1) or by_cs.get(cs + 1) or ""
        trans.append("" if tr == s or not s else tr)
    return Lyrics(lines, trans, source, matched)


# ---------------------------------------------------------------- matching

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
        a = 0.0  # "Jay Chou" vs "周杰伦" can't be compared; small similarities are just noise
    if track.duration and c.duration:
        diff = abs(track.duration - c.duration)
        d = 1.0 if diff <= 2 else 0.7 if diff <= 5 else 0.3 if diff <= 10 else 0.0
    else:
        d = 0.5
    penalty = 0.25 if _version_words(c.full) - _version_words(track.title) else 0.0
    return 0.5 * t + 0.2 * a + 0.3 * d - penalty


# ---------------------------------------------------------------- sources

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
    """For searching: drop tails like " - Remastered" or "(feat. X)" that confuse search engines."""
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
        return f"[00:00.00]{INSTRUMENTAL_MARK}", ""
    return (d.get("lrc") or {}).get("lyric") or "", (d.get("tlyric") or {}).get("lyric") or ""


def lrclib_search(track: Track, title_only: bool) -> list[Candidate]:
    params = ({"q": _clean_title(track.title)} if title_only
              else {"track_name": _clean_title(track.title), "artist_name": track.artist})
    d = _http("https://lrclib.net/api/search?" + urllib.parse.urlencode(params),
              headers={"User-Agent": "spotify-desktop-lyrics (https://github.com/wenjun-cheng/spotify-desktop-lyrics)"})
    return [Candidate(s["trackName"], s["trackName"], [s.get("artistName") or ""], float(s.get("duration") or 0),
                      partial(lambda lrc: (lrc, ""), s["syncedLyrics"]))
            for s in d[:15] if s.get("syncedLyrics")]


SOURCES = {"qq": qq_search, "netease": netease_search, "lrclib": lrclib_search}


# ---------------------------------------------------------------- public API

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
            log.exception("Corrupted cache file: %s", cache)

    # CJK songs: QQ Music first (it has the most Chinese-language catalog); everything else: NetEase first
    order = ["qq", "netease", "lrclib"] if CJK.search(track.title + track.artist) else ["netease", "qq", "lrclib"]
    best: tuple[float, Lyrics] | None = None
    errors = 0
    for title_only in (False, True):
        for name in order:
            try:
                cands = SOURCES[name](track, title_only)
            except Exception as e:
                errors += 1
                log.warning("%s search failed: %s", name, e)
                continue
            # on equal scores, keep the source's own ranking
            ranked = sorted(((score(c, track) - 0.001 * i, c) for i, c in enumerate(cands)),
                            key=lambda x: x[0], reverse=True)
            for s, c in ranked[:3]:
                if s < ACCEPT or (best and s <= best[0]):
                    break
                try:
                    lyr = build_lyrics(*c.fetch(), name, f"{c.full} - {' / '.join(c.artists[:2])}")
                except Exception as e:
                    log.warning("%s lyrics download failed: %s", name, e)
                    continue
                if lyr:
                    best = (s, lyr)
                    log.info("candidate %.2f [%s] %s", s, name, lyr.matched)
                    break
            if best and best[0] >= GOOD:
                break
        if best:
            break

    if best is None and errors:
        if errors == 2 * len(order):
            raise ConnectionError("none of the lyrics sources could be reached")
        return None  # some source was unreachable, so don't cache this miss
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {"lyrics": best[1].to_dict()} if best else {"miss": time.time()}
    cache.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    log.info("%s - %s -> %s", track.title, track.artist, best[1].matched if best else "not found")
    return best[1] if best else None
