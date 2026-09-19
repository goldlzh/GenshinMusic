"""歌曲演奏记忆的数据校验与优先级；不依赖 GUI 或键盘输入。"""

import math


def clamp_ratio(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, min(value, 1.0)) if math.isfinite(value) else 0.0


def normalize_memory(value):
    """返回独立、可序列化的记忆快照；不合法的记录不参与恢复。"""
    if not isinstance(value, dict) or not isinstance(value.get("channels"), list):
        return None
    channels = sorted({c for c in value["channels"]
                       if type(c) is int and 0 <= c <= 15 and c != 9})
    if not channels:
        return None
    track = value.get("melody_track")
    if type(track) is not int or track < 0:
        track = None
    try:
        transpose = int(value.get("transpose", 0))
    except (TypeError, ValueError, OverflowError):
        transpose = 0
    try:
        speed = float(value.get("speed", 1.0))
    except (TypeError, ValueError, OverflowError):
        speed = 1.0
    if not math.isfinite(speed):
        speed = 1.0
    start = value.get("start")
    if not isinstance(start, dict):
        start = {}
    label_id = start.get("label_id")
    if not isinstance(label_id, str) or not label_id:
        label_id = None

    def flag(key, default):
        result = value.get(key, default)
        return result if isinstance(result, bool) else default

    duration_mode = value.get("duration_mode", "fixed")
    if duration_mode not in ("fixed", "score", "melody", "chord"):
        duration_mode = "fixed"

    return {
        "channels": channels,
        "melody_track": track,
        "melody_only": flag("melody_only", False),
        "remove_glissando": flag("remove_glissando", True),
        "octave_fold": flag("octave_fold", True),
        "duration_mode": duration_mode,
        "sustain_pedal": flag("sustain_pedal", False),
        "transpose": transpose,
        "speed": round(max(0.5, min(speed, 2.0)), 2),
        "start": {"label_id": label_id, "ratio": round(clamp_ratio(start.get("ratio", 0)), 6)},
    }


def preferred_memory(song_entry):
    """手动记忆优先；自动记忆独立更新，以便清除手动记忆后恢复。"""
    memories = song_entry.get("playback_memory", {})
    if isinstance(memories, dict):
        for source in ("manual", "auto"):
            memory = normalize_memory(memories.get(source))
            if memory is not None:
                return source, memory
    return None, None


def resolve_memory_start(memory, labels):
    """标签 ID 优先于位置：移动后跟随标签，删除后使用快照的比例。"""
    start = memory["start"]
    label_id = start["label_id"]
    if label_id:
        for label in labels:
            if label.get("id") == label_id:
                return clamp_ratio(label.get("ratio", 0)), label_id
    return clamp_ratio(start["ratio"]), None
