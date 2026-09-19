"""曲库排序与时间记录；时间戳和原有歌曲记忆共用同一份配置。"""

import math
import os
import time
import unicodedata
from functools import lru_cache

from pypinyin import lazy_pinyin


SORT_MODES = {"name": "首字母 A–Z", "added": "添加时间", "played": "播放时间"}


def normalize_sort_mode(value):
    return value if isinstance(value, str) and value in SORT_MODES else "name"


def song_key(name):
    return os.path.normcase(name).replace("\\", "/")


def valid_timestamp(value):
    # 排除 JSON 中的字符串、布尔值、NaN 和超出日期范围的值。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if 0 < value < 253402214400 and math.isfinite(value) else 0.0


@lru_cache(maxsize=8192)
def song_name_key(name):
    title = unicodedata.normalize("NFKC", os.path.splitext(name)[0]).casefold()
    romanized = "".join(lazy_pinyin(title))
    romanized = "".join(c for c in unicodedata.normalize("NFKD", romanized)
                        if not unicodedata.combining(c))
    # 歌名开头的括号、书名号、空白等不抢占字母顺序。
    romanized = romanized.lstrip()
    while romanized and not romanized[0].isalnum():
        romanized = romanized[1:]
    first = romanized[:1]
    group = 0 if first and "a" <= first <= "z" else (1 if first.isdigit() else 2)
    return group, romanized, name.casefold(), name


def song_entry(store, name):
    entry = store.get("songs", {}).get(song_key(name))
    return entry if isinstance(entry, dict) else {}


def sort_songs(names, store, mode):
    mode = normalize_sort_mode(mode)
    if mode == "name":
        return sorted(names, key=song_name_key)
    field = "added_at" if mode == "added" else "last_played_at"
    return sorted(names, key=lambda name: (
        -valid_timestamp(song_entry(store, name).get(field)), song_name_key(name)))


def register_songs(names, music_dir, store, now=None):
    """旧曲库用文件创建时间初始化；此后新增歌曲记录首次发现的时间。"""
    now = time.time() if now is None else now
    settings = store.setdefault("settings", {})
    initial_scan = settings.get("library_indexed") is not True
    songs = store.setdefault("songs", {})
    changed = initial_scan
    for name in names:
        key = song_key(name)
        entry = songs.get(key)
        if not isinstance(entry, dict):
            entry = songs[key] = {"file": name, "labels": []}
            changed = True
        if valid_timestamp(entry.get("added_at")):
            continue
        added_at = now
        if initial_scan:
            try:
                stat = os.stat(os.path.join(music_dir, name))
                # Windows 的 birthtime/ctime 为创建时间；其他系统无 birthtime 时用修改时间。
                fallback = stat.st_ctime if os.name == "nt" else stat.st_mtime
                added_at = valid_timestamp(getattr(stat, "st_birthtime", fallback)) or now
            except OSError:
                pass
        entry["added_at"] = added_at
        changed = True
    settings["library_indexed"] = True
    return changed


def format_timestamp(value, missing="尚无记录"):
    timestamp = valid_timestamp(value)
    if not timestamp:
        return missing
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except (OSError, OverflowError, ValueError):
        return missing
