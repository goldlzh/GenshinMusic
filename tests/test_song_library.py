import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from song_library import (format_timestamp, normalize_sort_mode, register_songs,
                          song_key, song_name_key, sort_songs, valid_timestamp)


class SongLibraryDataTests(unittest.TestCase):
    def test_names_mix_chinese_pinyin_with_english_and_ignore_case(self):
        names = ['月亮.mid', 'Beta.MIDI', 'apple.mid', '安静.mid', 'zoo.mid', '【稻香】.mid']
        self.assertEqual(sort_songs(names, {}, 'name'),
                         ['安静.mid', 'apple.mid', 'Beta.MIDI', '【稻香】.mid', '月亮.mid', 'zoo.mid'])

    def test_name_sort_handles_fullwidth_accents_numbers_and_symbols(self):
        names = ['2.mid', '♪.mid', 'Écho.mid', ' Ａlpha.mid', '雪.mid']
        self.assertEqual(sort_songs(names, {}, 'name'),
                         [' Ａlpha.mid', 'Écho.mid', '雪.mid', '2.mid', '♪.mid'])
        self.assertEqual(song_name_key('重庆.mid')[1], 'chongqing')

    def test_times_are_newest_first_with_alphabetic_ties_and_unknown_last(self):
        store = {'songs': {song_key('A.mid'): {'added_at': 100, 'last_played_at': 300},
                           song_key('B.mid'): {'added_at': 200, 'last_played_at': 100},
                           song_key('C.mid'): {'added_at': 200},
                           song_key('D.mid'): {'last_played_at': '999'}}}
        names = ['D.mid', 'C.mid', 'B.mid', 'A.mid']
        self.assertEqual(sort_songs(names, store, 'added'), ['B.mid', 'C.mid', 'A.mid', 'D.mid'])
        self.assertEqual(sort_songs(names, store, 'played'), ['A.mid', 'B.mid', 'C.mid', 'D.mid'])

    def test_invalid_modes_entries_and_dates_do_not_crash_sorting(self):
        for value in ([], {}, True, None, 'count'):
            self.assertEqual(normalize_sort_mode(value), 'name')
        for value in (True, '100', None, float('nan'), float('inf'), -1, 10 ** 1000):
            self.assertEqual(valid_timestamp(value), 0)
            self.assertEqual(format_timestamp(value), '尚无记录')
        self.assertEqual(sort_songs(['B.mid', 'A.mid'],
                                   {'songs': {song_key('B.mid'): []}}, 'played'), ['A.mid', 'B.mid'])

    def test_initial_migration_uses_birth_time_and_preserves_existing_data(self):
        original = {'file': 'A.mid', 'labels': [{'name': '主歌', 'ratio': .2}],
                    'playback_memory': {'manual': {'channels': [0]}}}
        store = {'settings': {'insert_hotkey': 'Num+'},
                 'songs': {song_key('A.mid'): copy.deepcopy(original)}}
        stat = SimpleNamespace(st_birthtime=100, st_ctime=200, st_mtime=300)
        with mock.patch('song_library.os.stat', return_value=stat):
            self.assertTrue(register_songs(['A.mid'], 'songs', store, now=1000))
        entry = store['songs'][song_key('A.mid')]
        self.assertEqual(entry['added_at'], 100)
        self.assertNotIn('last_played_at', entry)
        self.assertEqual({key: entry[key] for key in original}, original)
        self.assertEqual(store['settings']['insert_hotkey'], 'Num+')

    def test_rescan_is_stable_and_new_songs_use_first_discovery_time(self):
        store = {'settings': {'library_indexed': True},
                 'songs': {song_key('A.mid'): {'added_at': 100, 'last_played_at': 150}}}
        with mock.patch('song_library.os.stat') as stat:
            self.assertTrue(register_songs(['A.mid', 'B.mid'], 'songs', store, now=200))
            stat.assert_not_called()
        self.assertFalse(register_songs(['B.mid', 'A.mid'], 'songs', store, now=300))
        self.assertEqual(store['songs'][song_key('A.mid')]['added_at'], 100)
        self.assertEqual(store['songs'][song_key('B.mid')]['added_at'], 200)
        self.assertEqual(store['songs'][song_key('A.mid')]['last_played_at'], 150)

    def test_initial_unreadable_file_uses_discovery_time(self):
        store = {}
        with mock.patch('song_library.os.stat', side_effect=OSError('gone')):
            register_songs(['A.mid'], 'songs', store, now=200)
        self.assertEqual(store['songs'][song_key('A.mid')]['added_at'], 200)


if __name__ == '__main__':
    unittest.main()
