"""MIDI 音长、转调和按键时间轴。导入本模块不会注册热键或发送按键。"""

import bisect
import collections
import math
import os
import threading
import time
from dataclasses import dataclass, replace

import mido


NOTE_MAP = {
    48: 'Z', 50: 'X', 52: 'C', 53: 'V', 55: 'B', 57: 'N', 59: 'M',
    60: 'A', 62: 'S', 64: 'D', 65: 'F', 67: 'G', 69: 'H', 71: 'J',
    72: 'Q', 74: 'W', 76: 'E', 77: 'R', 79: 'T', 81: 'Y', 83: 'U',
}
LOW_KEY, HIGH_KEY = min(NOTE_MAP), max(NOTE_MAP)
KEY_HOLD_DURATION = 0.040
KEY_RETRIGGER_GAP = 0.010
MISSING_NOTE_DURATION = 0.250
CHORD_WINDOW = 0.080  # 仅用于报告统计，不合并组内先后发生的音符。
SKYLINE_WINDOW = 0.015
CHORD_ONSET_WINDOW = 0.015  # 按原速起音时间识别和弦，不随播放速度改变分组。
TIME_EPSILON = 1e-7
DRUM_CHANNEL = 9
AUTO_KEEP_CHANNELS = 3
MAX_SILENCE = 1.0
GLISS_MIN_LEN = 6
GLISS_MAX_GAP = 0.060
GLISS_MAX_STEP = 2
DURATION_MODES = {'fixed': '固定短按', 'score': '原谱音长', 'melody': '旋律长音', 'chord': '和弦长音'}


@dataclass(frozen=True)
class MidiNote:
    start: float
    end: float
    pitch: int
    channel: int
    track: int
    melody: bool = False
    pedal_end: float = 0.0


@dataclass(frozen=True)
class PlayableNote:
    start: float
    end: float
    key: str
    melody: bool
    sustained: bool


@dataclass(frozen=True)
class ScheduledNote:
    start: float
    end: float
    key: str
    melody: bool


@dataclass(frozen=True)
class UnavailableNote:
    """只用于显示的音符，不参与按键事件或播放器总时长。"""

    start: float
    end: float
    pitch: int
    reason: str


@dataclass(frozen=True)
class PlaybackSnapshot:
    """供界面读取的不可变快照；active_note_ids 来自已经发送的按键。"""

    position: float = 0.0
    phase: str = 'preview'
    active_note_ids: tuple = ()
    recent_triggers: tuple = ()  # (音符索引, perf_counter 时间)，短音的短暂提示。
    timestamp: float = 0.0
    seek_serial: int = 0  # 已应用的定位请求，供界面丢弃跳转前的迟到进度。


def build_tempo_map(mid):
    tempo_map = []
    tick = 0
    for msg in mido.merge_tracks(mid.tracks):
        tick += msg.time
        if msg.type == 'set_tempo':
            tempo_map.append((tick, msg.tempo))
    if not tempo_map or tempo_map[0][0] != 0:
        tempo_map.insert(0, (0, 500000))
    return tempo_map


def ticks_to_seconds(target_tick, tempo_map, tpb):
    seconds = 0.0
    for i, (start, tempo) in enumerate(tempo_map):
        end = tempo_map[i + 1][0] if i + 1 < len(tempo_map) else float('inf')
        if target_tick <= start:
            break
        seconds += mido.tick2second(min(target_tick, end) - start, tpb, tempo)
        if target_tick <= end:
            break
    return seconds


def extract_track_notes(mid, tempo_map, tpb, log=print):
    # 合并时间轴以处理跨轨道的通道踏板消息，同时保留音符来源供旋律识别使用。
    events = []
    for track_idx, track in enumerate(mid.tracks):
        tick = 0
        for order, msg in enumerate(track):
            tick += msg.time
            events.append((tick, track_idx, order, msg))
    events.sort(key=lambda event: event[:3])
    active = collections.defaultdict(collections.deque)
    held_by_pedal = collections.defaultdict(list)
    pedal_down = collections.defaultdict(bool)
    records = []
    seconds_cache = {}

    def seconds(tick):
        if tick not in seconds_cache:
            seconds_cache[tick] = ticks_to_seconds(tick, tempo_map, tpb)
        return seconds_cache[tick]

    def release_pedal(channel, when):
        for record in held_by_pedal.pop(channel, []):
            record['pedal_end'] = max(record['end'], when)

    def finish(record, when, respect_pedal=True):
        record['end'] = max(record['start'], when)
        record['pedal_end'] = record['end']
        if respect_pedal and pedal_down[record['channel']]:
            held_by_pedal[record['channel']].append(record)

    for tick, track_idx, _, msg in events:
        channel = getattr(msg, 'channel', None)
        if channel is None or channel == DRUM_CHANNEL:
            continue
        if msg.type == 'note_on' and msg.velocity > 0:
            record = dict(start=seconds(tick), end=None, pitch=msg.note,
                          channel=channel, track=track_idx, pedal_end=0.0)
            records.append(record)
            active[channel, msg.note].append(record)
        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
            pending = active[channel, msg.note]
            if pending:
                # 同轨优先；若 MIDI 将关音放到另一轨，则按通道/音高 FIFO 配对。
                index = next((i for i, note in enumerate(pending)
                              if note['track'] == track_idx), 0)
                record = pending[index]
                del pending[index]
                finish(record, seconds(tick))
        elif msg.type == 'control_change':
            if msg.control == 64:
                pedal_down[channel] = msg.value >= 64
                if not pedal_down[channel]:
                    release_pedal(channel, seconds(tick))
            elif msg.control in (120, 123):
                for (ch, _), pending in active.items():
                    if ch == channel:
                        while pending:
                            finish(pending.popleft(), seconds(tick), msg.control != 120)
                if msg.control == 120:
                    release_pedal(channel, seconds(tick))
            elif msg.control == 121:
                pedal_down[channel] = False
                release_pedal(channel, seconds(tick))

    missing = 0
    for record in records:
        if record['end'] is None:
            # 损坏或只有 note_on 的谱子仍可播放，并有有限的松键时间。
            record['end'] = record['start'] + MISSING_NOTE_DURATION
            record['pedal_end'] = record['end']
            missing += 1
    file_end = seconds(events[-1][0]) if events else 0.0
    for channel in list(held_by_pedal):
        release_pedal(channel, file_end)
    if missing:
        log(f"音长回退：{missing} 个音缺少关音消息，按 {MISSING_NOTE_DURATION:.2f}s 处理。")
    tracks = collections.defaultdict(list)
    for record in records:
        tracks[record['track']].append(MidiNote(**record))
    return [(idx, sorted(notes, key=lambda note: note.start))
            for idx, notes in sorted(tracks.items())]


def inspect_instruments(mid):
    channel_info = {}
    drum_notes = 0
    programs = {}
    for msg in mido.merge_tracks(mid.tracks):
        if msg.type == 'program_change':
            programs[msg.channel] = msg.program
        elif msg.type == 'note_on' and msg.velocity > 0:
            if msg.channel == DRUM_CHANNEL:
                drum_notes += 1
            else:
                info = channel_info.setdefault(msg.channel, {'programs': set(), 'notes': 0})
                info['notes'] += 1
                info['programs'].add(programs.get(msg.channel, 0))
    return channel_info, drum_notes


def filter_channels(note_bearing, keep_channels):
    result = []
    for idx, notes in note_bearing:
        kept = [note for note in notes if note.channel in keep_channels]
        if kept:
            result.append((idx, kept))
    return result


def auto_select_channels(channel_info, max_keep=AUTO_KEEP_CHANNELS):
    ranked = sorted(channel_info, key=lambda ch: channel_info[ch]['notes'], reverse=True)
    return set(ranked[:max_keep])


def skyline_tag(notes, window=SKYLINE_WINDOW):
    tagged = []
    i = 0
    while i < len(notes):
        j = i + 1
        while j < len(notes) and notes[j].start - notes[i].start <= window:
            j += 1
        highest = max(range(i, j), key=lambda index: notes[index].pitch)
        tagged.extend(replace(notes[index], melody=(index == highest)) for index in range(i, j))
        i = j
    return tagged


def detect_melody(note_bearing, forced_idx=None):
    if not note_bearing:
        return [], '无可播放音轨'
    if len(note_bearing) == 1:
        return skyline_tag(note_bearing[0][1]), '单音轨模式：天际线（15ms窗口最高音）作为主旋律'
    if forced_idx in {idx for idx, _ in note_bearing}:
        melody_idx = forced_idx
        desc = f'多音轨模式：手动指定音轨 {melody_idx} 作为主旋律'
    else:
        melody_idx = max(note_bearing, key=lambda item: sum(n.pitch for n in item[1]) / len(item[1]))[0]
        desc = f'多音轨模式：自动选择音轨 {melody_idx} 作为主旋律'
    tagged = [replace(note, melody=(idx == melody_idx)) for idx, notes in note_bearing for note in notes]
    return sorted(tagged, key=lambda note: note.start), desc


def detect_glissando(tagged):
    gliss, runs, i = set(), 0, 0
    while i < len(tagged) - 1:
        run, direction, j = [i], 0, i
        while j + 1 < len(tagged):
            gap = tagged[j + 1].start - tagged[j].start
            step = tagged[j + 1].pitch - tagged[j].pitch
            if gap > GLISS_MAX_GAP or step == 0 or abs(step) > GLISS_MAX_STEP:
                break
            sign = 1 if step > 0 else -1
            if direction and sign != direction:
                break
            direction = sign
            run.append(j + 1)
            j += 1
        if len(run) >= GLISS_MIN_LEN:
            gliss.update(run)
            runs += 1
            i = j + 1
        else:
            i += 1
    return gliss, runs


def remove_glissando(tagged, gliss_set):
    return [note for idx, note in enumerate(tagged) if idx not in gliss_set]


def keep_melody_only(tagged):
    return [note for note in tagged if note.melody]


def fold_to_range(note):
    while note > HIGH_KEY:
        note -= 12
    while note < LOW_KEY:
        note += 12
    return note if note in NOTE_MAP else None


def map_note(note, enable_fold):
    if note in NOTE_MAP:
        return note
    return fold_to_range(note) if enable_fold else None


def count_playable(tagged, offset, enable_fold=False):
    counts = [0, 0]
    for note in tagged:
        if map_note(note.pitch + offset, enable_fold) is not None:
            counts[0 if note.melody else 1] += 1
    return tuple(counts)


def find_best_transpose(tagged, enable_fold=False):
    if not tagged:
        return 0
    melody_line = [note for note in skyline_tag([n for n in tagged if n.melody]) if note.melody]
    best_offset, best_score = 0, None
    for offset in range(-12, 13):
        mel_count, acc_count = count_playable(tagged, offset, enable_fold)
        folding = contour = collisions = 0
        previous = None
        for note in melody_line:
            mapped = map_note(note.pitch + offset, enable_fold)
            if mapped is None:
                continue
            if previous:
                original, folded = previous
                contour += abs((mapped - folded) - (note.pitch - original))
            previous = note.pitch, mapped
        sounding = {}
        for note in tagged:
            mapped = map_note(note.pitch + offset, enable_fold)
            if mapped is None:
                continue
            folding += abs(mapped - (note.pitch + offset)) // 12
            previous = sounding.get(mapped)
            if (previous and previous.end > note.start + TIME_EPSILON
                    and previous.pitch != note.pitch and (previous.melody or note.melody)):
                collisions += 1
            if previous is None or note.end > previous.end:
                sounding[mapped] = note
        # 主旋律可弹数优先；同数时保护旋律走向和重叠长音，再比较伴奏。
        score = (mel_count, -contour, -collisions, acc_count, -folding, -abs(offset))
        if best_score is None or score > best_score:
            best_offset, best_score = offset, score
    return best_offset if best_score[0] + best_score[3] else 0


def analyze_midi(file_path, log=print):
    if not os.path.exists(file_path):
        log(f"错误：找不到文件 '{file_path}'")
        return None
    log(f'正在解析 MIDI 文件: {os.path.basename(file_path)} ...')
    try:
        mid = mido.MidiFile(file_path, clip=True)
        tempo_map = build_tempo_map(mid)
        note_bearing = extract_track_notes(mid, tempo_map, mid.ticks_per_beat, log)
        channel_info, drum_notes = inspect_instruments(mid)
    except Exception as error:
        log(f'[错误] 解析 MIDI 失败: {error}')
        return None
    if not note_bearing:
        log('错误：该 MIDI 文件中未检测到任何可播放音符！')
        return None
    return dict(mid=mid, note_bearing=note_bearing, channel_info=channel_info,
                drum_notes=drum_notes, total_tracks=len(mid.tracks))


class PlaybackTimeline:
    """原时间轴到演奏时间轴；被压缩空白中的标记按比例放入保留的间奏。"""

    def __init__(self, notes, max_silence=MAX_SILENCE, origin=0.0):
        notes = sorted(notes, key=lambda note: note.start)
        self.origin = notes[0].start if notes else origin
        self.gaps = []
        self.starts = []
        self.playback_starts = []
        shift = sounding_until = self.origin
        for note in notes:
            gap = note.start - sounding_until
            if gap > max_silence:
                self.starts.append(sounding_until)
                self.playback_starts.append(sounding_until - shift)
                self.gaps.append((sounding_until, note.start, shift, gap - max_silence))
                shift += gap - max_silence
            sounding_until = max(sounding_until, note.end)

    def position(self, seconds):
        index = bisect.bisect_right(self.starts, seconds) - 1
        if index < 0:
            return seconds - self.origin
        start, end, shift, removed = self.gaps[index]
        if seconds < end:
            return seconds - shift - removed * (seconds - start) / (end - start)
        return seconds - (shift + removed)

    def inverse_position(self, seconds):
        """演奏秒数还原为压缩前秒数；保留间奏中的位置按相同比例反解。"""
        index = bisect.bisect_right(self.playback_starts, seconds) - 1
        if index < 0:
            return seconds + self.origin
        start, end, shift, removed = self.gaps[index]
        playback_start = start - shift
        playback_end = end - shift - removed
        if seconds < playback_end:
            return start + (seconds - playback_start) * (end - start) / (playback_end - playback_start)
        return seconds + shift + removed


def compress_silence(notes, max_silence=MAX_SILENCE, log=print):
    if not notes:
        return []
    notes = sorted(notes, key=lambda note: note.start)
    timeline = PlaybackTimeline(notes, max_silence)
    result = [replace(note, start=timeline.position(note.start), end=timeline.position(note.end)) for note in notes]
    if notes[0].start > 0.01:
        log(f'首音归零：去掉了开头 {notes[0].start:.1f}s 的等待。')
    if timeline.gaps:
        log(f'间奏压缩：{len(timeline.gaps)} 处所有按键均释放的空白已缩短到 {max_silence:.1f}s。')
    return result


def sustained_note_flags(tagged, duration_mode, transpose_offset=0, enable_fold=False):
    """长音资格共用于实际按键与红音显示；和弦须含至少两个不同的可映射伴奏键。"""
    if duration_mode == 'score':
        return [True] * len(tagged)
    if duration_mode == 'melody':
        return [note.melody for note in tagged]
    flags = [False] * len(tagged)
    if duration_mode != 'chord':
        return flags
    pitches = [map_note(note.pitch + transpose_offset, enable_fold) for note in tagged]
    accompaniment = sorted((i for i, note in enumerate(tagged) if not note.melody and pitches[i] is not None),
                           key=lambda i: tagged[i].start)
    unavailable = sorted((i for i, note in enumerate(tagged) if not note.melody and pitches[i] is None),
                         key=lambda i: tagged[i].start)
    unavailable_starts = [tagged[i].start for i in unavailable]
    melody = sorted((note.start, pitches[i]) for i, note in enumerate(tagged)
                    if note.melody and pitches[i] is not None)
    melody_starts = [start for start, pitch in melody]
    i = 0
    while i < len(accompaniment):
        first = tagged[accompaniment[i]].start
        j = i + 1
        while j < len(accompaniment) and tagged[accompaniment[j]].start - first <= CHORD_ONSET_WINDOW + TIME_EPSILON:
            j += 1
        group = accompaniment[i:j]
        keys = {pitches[index] for index in group if pitches[index] is not None}
        if len(keys) >= 2:
            # 同一起音的旋律与伴奏共用琴键时，优先保持旋律短按。
            left = bisect.bisect_left(melody_starts, first - CHORD_ONSET_WINDOW - TIME_EPSILON)
            right = bisect.bisect_right(melody_starts, tagged[group[-1]].start + CHORD_ONSET_WINDOW + TIME_EPSILON)
            melody_keys = {pitch for start, pitch in melody[left:right]}
            for index in group:
                flags[index] = pitches[index] not in melody_keys
            # 不可奏音只跟随附近和弦的显示时值，不参与分组或改变可奏音的资格。
            left = bisect.bisect_left(unavailable_starts, first - CHORD_ONSET_WINDOW - TIME_EPSILON)
            right = bisect.bisect_right(unavailable_starts, tagged[group[-1]].start + CHORD_ONSET_WINDOW + TIME_EPSILON)
            for index in unavailable[left:right]:
                flags[index] = True
        i = j
    return flags


def finalize_notes(tagged, transpose_offset, enable_fold=False, log=print,
                   duration_mode='score', sustain_pedal=False):
    if duration_mode not in DURATION_MODES:
        duration_mode = 'score'
    tagged = list(tagged)
    sustained_flags = sustained_note_flags(tagged, duration_mode, transpose_offset, enable_fold)
    notes = []
    for index, note in enumerate(tagged):
        pitch = map_note(note.pitch + transpose_offset, enable_fold)
        if pitch is None:
            continue
        sustained = sustained_flags[index]
        end = max(note.end, note.pedal_end) if sustain_pedal else note.end
        end = max(end, note.start) if sustained else note.start + KEY_HOLD_DURATION
        notes.append(PlayableNote(note.start, end, NOTE_MAP[pitch], note.melody, sustained))
    if not notes:
        log('警告：过滤后没有剩下可弹奏的音符！')
        return []
    notes.sort(key=lambda note: note.start)
    log(f'解析完成：{len(notes)} 个可映射音符；音长模式：{DURATION_MODES[duration_mode]}。')
    return notes


def _resolve_schedule(notes, speed):
    """所有音乐时间同比缩放；40ms 最短按压和10ms重触发间隔使用真实时间。"""
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError('播放速度必须是大于零的有限数值')
    by_key = collections.defaultdict(list)
    scaled = []
    for note in notes:
        start = note.start / speed
        duration = (note.end - note.start) / speed if note.sustained else KEY_HOLD_DURATION
        scaled.append(ScheduledNote(start, start + max(KEY_HOLD_DURATION, duration), note.key, note.melody))
    for note in sorted(scaled, key=lambda item: item.start):
        previous = by_key[note.key]
        if previous and abs(note.start - previous[-1].start) <= TIME_EPSILON:
            old = previous[-1]
            previous[-1] = replace(old, end=max(old.end, note.end), melody=old.melody or note.melody)
            continue
        if previous:
            old = previous[-1]
            # 折叠后的伴奏不能打断正在持续的主旋律。
            if old.melody and not note.melody and note.start < old.end - TIME_EPSILON:
                continue
            end = min(old.end, note.start - KEY_RETRIGGER_GAP)
            if end <= old.start + TIME_EPSILON:
                if old.melody and not note.melody:
                    continue
                previous.pop()  # 物理间隔不足时优先新起音，不推迟后面的节拍。
            else:
                previous[-1] = replace(old, end=end)
        previous.append(note)
    resolved = sorted((note for group in by_key.values() for note in group),
                      key=lambda note: (note.start, note.key))
    return resolved


def build_schedule(notes, speed=1.0, log=lambda _: None):
    # 必须先解决同键截断和输入最短时长，再识别真正无人按键的间奏。
    return compress_silence(_resolve_schedule(notes, speed), max_silence=MAX_SILENCE / speed, log=log)


def _region_timeline(playable_notes, speed, tagged):
    resolved = _resolve_schedule(playable_notes, speed)
    return PlaybackTimeline(resolved, MAX_SILENCE / speed,
                            origin=min((note.start / speed for note in tagged), default=0.0))


def _region_seconds(start, end):
    start, end = float(start), float(end)
    if not math.isfinite(start) or not math.isfinite(end) or end < start:
        raise ValueError('片段起止时间必须有限，且终点不能早于起点')
    return start, end


def playback_region_to_source(playable_notes, start_seconds, end_seconds, speed=1.0, tagged=()):
    """把当前压缩演奏时间轴的半开区间还原成原谱秒数，保留不可奏音所在空白。"""
    start, end = _region_seconds(start_seconds, end_seconds)
    timeline = _region_timeline(playable_notes, speed, tagged)
    return timeline.inverse_position(start) * speed, timeline.inverse_position(end) * speed


def source_region_to_playback(playable_notes, raw_start, raw_end, speed=1.0, tagged=()):
    """把同一原谱片段投影到重新编曲后的演奏时间轴，不改变原谱边界。"""
    start, end = _region_seconds(raw_start, raw_end)
    timeline = _region_timeline(playable_notes, speed, tagged)
    return timeline.position(start / speed), timeline.position(end / speed)


def select_tagged_region(tagged, playable_notes, start_seconds, end_seconds, speed=1.0):
    """按当前演奏区间选择原谱起音，包含当前移调下不可奏或同键冲突淘汰的音。"""
    tagged = list(tagged)
    start, end = playback_region_to_source(playable_notes, start_seconds, end_seconds, speed, tagged)
    return [note for note in tagged if start - TIME_EPSILON <= note.start < end - TIME_EPSILON]


def build_unavailable_notes(tagged, transpose_offset, enable_fold, playable_notes,
                            speed=1.0, duration_mode='score', sustain_pedal=False):
    """保留无法映射的音符，并与实际按键共用时间变换，红音不延长演奏。"""
    resolved = _resolve_schedule(playable_notes, speed)
    tagged = list(tagged)
    timeline = PlaybackTimeline(resolved, MAX_SILENCE / speed,
                                origin=min((note.start / speed for note in tagged), default=0.0))
    if duration_mode not in DURATION_MODES:
        duration_mode = 'score'
    sustained_flags = sustained_note_flags(tagged, duration_mode, transpose_offset, enable_fold)
    result = []
    for index, note in enumerate(tagged):
        pitch = note.pitch + transpose_offset
        if map_note(pitch, enable_fold) is not None:
            continue
        reason = '超出音域' if not enable_fold and not LOW_KEY <= pitch <= HIGH_KEY else '无对应琴键'
        if enable_fold:
            while pitch > HIGH_KEY:
                pitch -= 12
            while pitch < LOW_KEY:
                pitch += 12
        sustained = sustained_flags[index]
        end = max(note.end, note.pedal_end) if sustain_pedal else note.end
        duration = max(KEY_HOLD_DURATION, (end - note.start) / speed) if sustained else KEY_HOLD_DURATION
        start = note.start / speed
        result.append(UnavailableNote(timeline.position(start), timeline.position(start + duration), pitch, reason))
    return sorted(result, key=lambda note: (note.start, note.pitch))


def performance_duration(notes, speed=1.0):
    return max((note.end for note in build_schedule(notes, speed)), default=0.0)


def count_chords(tagged, offset, enable_fold):
    times = sorted(note.start for note in tagged if map_note(note.pitch + offset, enable_fold) is not None)
    groups = chords = i = 0
    while i < len(times):
        j = i + 1
        while j < len(times) and times[j] - times[i] <= CHORD_WINDOW:
            j += 1
        groups += 1
        chords += j - i > 1
        i = j
    return groups, chords


class KeyboardPlayer:
    """一个线程拥有全部琴键；歌曲时间轴同时包含按下和松开事件。"""

    _keyboard_lock = threading.Lock()

    def __init__(self, notes, speed, start_ratio, progress_cb, log_cb, finish_cb,
                 *, keyboard, clock=None, waiter=None, end_ratio=1.0, region_start_ratio=0.0):
        self.notes = build_schedule(notes, speed, log=log_cb)
        self.total = max((note.end for note in self.notes), default=0.0)

        def clamp_ratio(value, default):
            value = float(value)
            return max(0.0, min(value, 1.0)) if math.isfinite(value) else default

        # start_ratio 仍是初始播放位置；区域下界独立，旧调用仍可向起播点前定位。
        self.region_start = clamp_ratio(region_start_ratio, 0.0) * self.total
        self.region_end = max(self.region_start, clamp_ratio(end_ratio, 1.0) * self.total)
        self.events = sorted((when, action, index)
                             for index, note in enumerate(self.notes)
                             if self.region_start - TIME_EPSILON <= note.start < self.region_end - TIME_EPSILON
                             for when, action in ((note.start, 1), (min(note.end, self.region_end), 0)))
        self.event_times = [event[0] for event in self.events]
        self.speed = speed
        self.play_time = max(self.region_start, min(clamp_ratio(start_ratio, 0.0) * self.total, self.region_end))
        self.progress_cb, self.log, self.finish_cb = progress_cb, log_cb, finish_cb
        self.keyboard = keyboard
        self.clock = clock or time.perf_counter
        self.waiter = waiter
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.wake_event = threading.Event()
        self.seek_lock = threading.Lock()
        self.seek_request = None
        self._requested_seek_serial = 0
        self.seek_serial = 0
        self.suppress_progress = False
        self.thread = None
        self.pressed = {}
        self.released_at = {}
        self._visual_lock = threading.Lock()
        self._visual_phase = 'ready'
        self._recent_triggers = collections.deque(maxlen=128)
        self._visual_state = PlaybackSnapshot(self.play_time, 'ready', timestamp=self.clock())

    def get_visual_state(self):
        with self._visual_lock:
            return self._visual_state

    def _publish_visual(self):
        now = self.clock()
        while self._recent_triggers and self._recent_triggers[0][1] < now - 0.18:
            self._recent_triggers.popleft()
        snapshot = PlaybackSnapshot(self.play_time, self._visual_phase,
                                    tuple(sorted(self.pressed.values())),
                                    tuple(self._recent_triggers), now, self.seek_serial)
        with self._visual_lock:
            self._visual_state = snapshot

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.pause_event.clear()
        self.wake_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=0.5)
        # 极端情况下旧线程仍在系统调用中，新实例也要等待 _keyboard_lock。

    def seek(self, seconds):
        with self.seek_lock:
            self._requested_seek_serial += 1
            serial = self._requested_seek_serial
            self.seek_request = (serial, max(self.region_start, min(seconds, self.region_end)))
        self.wake_event.set()
        return serial

    def _take_seek(self):
        with self.seek_lock:
            request, self.seek_request = self.seek_request, None
        return request

    def _wait(self, delay):
        if self.waiter:
            self.waiter(max(0.0001, delay))
        else:
            self.wake_event.wait(timeout=max(0.0001, delay))
            self.wake_event.clear()

    def _progress(self):
        self._publish_visual()
        if not self.suppress_progress:
            ratio = min(self.play_time / self.total, 1.0) if self.total else 0.0
            self.progress_cb(ratio, self.play_time, self.total)

    def _release_all(self):
        for key in NOTE_MAP.values():
            try:
                self.keyboard.keyUp(key.lower())
                if key in self.pressed:
                    self.released_at[key] = self.clock()
            except Exception:
                pass
        self.pressed.clear()
        self._recent_triggers.clear()
        self._publish_visual()

    def _press(self, index):
        note = self.notes[index]
        if self.pressed.get(note.key) == index:
            return True
        remaining = KEY_RETRIGGER_GAP - (self.clock() - self.released_at.get(note.key, -math.inf))
        if remaining > TIME_EPSILON:
            self._wait(min(remaining, 0.005))
            return False
        if (self.stop_event.is_set() or self.pause_event.is_set()
                or self.play_time >= self.region_end - TIME_EPSILON):
            return False
        with self.seek_lock:
            if self.seek_request is not None:
                return False
        self.keyboard.keyDown(note.key.lower())
        self.pressed[note.key] = index
        self._recent_triggers.append((index, self.clock()))
        self._publish_visual()
        return True

    def _resume_held(self, position, note_ids):
        # 只恢复这次暂停前确实按住的音；起播或定位都不补按过去的延长线。
        for index in note_ids:
            note = self.notes[index]
            if (note.start < position - TIME_EPSILON
                    and min(note.end, self.region_end) > position + TIME_EPSILON):
                while not self._press(index):
                    if self.stop_event.is_set() or self.pause_event.is_set():
                        return
                    with self.seek_lock:
                        if self.seek_request is not None:
                            return

    def _run(self):
        finished = False
        with self._keyboard_lock:
            try:
                self._visual_phase = 'playing'
                reference = self.clock() - self.play_time
                index = bisect.bisect_left(self.event_times, self.play_time - TIME_EPSILON)
                was_paused = False
                paused_notes = ()
                next_progress = -math.inf
                while not self.stop_event.is_set():
                    seek = self._take_seek()
                    if seek is not None:
                        self._release_all()
                        self.seek_serial, self.play_time = seek
                        paused_notes = ()
                        index = bisect.bisect_left(self.event_times, self.play_time - TIME_EPSILON)
                        if not self.pause_event.is_set():
                            self._visual_phase = 'playing'
                        reference = self.clock() - self.play_time
                        self._progress()
                    if self.pause_event.is_set():
                        if not was_paused:
                            self.play_time = min(self.region_end, self.clock() - reference)
                            self._visual_phase = 'paused'
                            paused_notes = tuple(self.pressed.values())
                            self._release_all()
                            self._progress()
                            was_paused = True
                        self._wait(0.01)
                        continue
                    if was_paused:
                        self._visual_phase = 'playing'
                        index = bisect.bisect_left(self.event_times, self.play_time - TIME_EPSILON)
                        self._resume_held(self.play_time, paused_notes)
                        paused_notes = ()
                        reference = self.clock() - self.play_time
                        was_paused = False
                    self.play_time = min(self.region_end, self.clock() - reference)
                    if self.clock() >= next_progress:
                        self._progress()
                        next_progress = self.clock() + 0.033
                    if self.play_time >= self.region_end - TIME_EPSILON:
                        finished = True
                        break
                    if index >= len(self.events):
                        # 区间尾部可能没有新起音，也要等到选定终点，不能提前完成。
                        self._wait(min(self.region_end - self.play_time, 0.005))
                        continue
                    target, action, note_id = self.events[index]
                    remaining = target - self.play_time
                    if remaining > TIME_EPSILON:
                        self._wait(min(remaining, 0.005))
                        continue
                    note = self.notes[note_id]
                    if action:
                        # 系统严重迟滞时略过已经结束的音，避免密集补发过期按键。
                        if self.play_time < note.end - TIME_EPSILON:
                            if not self._press(note_id):
                                continue
                    elif self.pressed.get(note.key) == note_id:
                        self.keyboard.keyUp(note.key.lower())
                        self.released_at[note.key] = self.clock()
                        del self.pressed[note.key]
                        self._publish_visual()
                    index += 1
            except Exception as error:
                self.log(f'演奏中断：{error}')
            finally:
                self._visual_phase = 'finished' if finished and not self.stop_event.is_set() else 'stopped'
                self._release_all()
        finished = finished and not self.stop_event.is_set()
        if finished:
            self.play_time = self.region_end
            self._progress()
        self.finish_cb(finished)
