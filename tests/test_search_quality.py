"""Offline regressions for group-chat search ranking, recording identity and shares."""

import json
import tempfile
import unittest
from dataclasses import asdict, fields
from pathlib import Path
from unittest.mock import AsyncMock

from core.maintenance import MaintenanceGate
from core.process import ProcessResult
from core.search_quality import (
    is_unavailable_live,
    rank_search_results,
    search_relevance,
    version_label,
)
from core.service import UserFacingError, YouTubeMusicService
from core.settings import Settings
from core.store_validation import valid_song
from core.utils import (
    Song,
    parse_youtube_url,
    song_from_flat_entry,
    song_from_full_info,
    youtube_video_id,
)


def song(index, title="Hello", artist="Artist", duration=60):
    video_id = f"track{index:06d}"
    return Song(video_id, title, artist, duration, f"https://www.youtube.com/watch?v={video_id}")


class SearchQualityTests(unittest.TestCase):
    def test_ascii_keywords_do_not_match_inside_an_unrelated_word(self):
        for query, unrelated in (
            ("it", "Whitney"),
            ("love", "Gloves"),
            ("live", "Alive"),
            ("it", "\u00e9it"),
            ("it", "it\u00e9"),
        ):
            with self.subTest(query=query):
                self.assertLess(
                    search_relevance(query, song(1, unrelated)),
                    search_relevance(query, song(2, query)),
                )
        self.assertEqual(search_relevance("it", song(1, "Whitney")), 0.02)

    def test_nfkc_casefold_and_punctuation_match_the_same_keyword(self):
        expected = search_relevance("ABC", song(1, "ABC"))
        self.assertEqual(search_relevance("abc", song(1, "\uff21\uff22\uff23")), expected)
        self.assertEqual(
            search_relevance("AT&T", song(1, "AT & T")),
            search_relevance("at t", song(1, "AT & T")),
        )
        self.assertGreater(search_relevance("Beyonc\u00e9", song(1, "BEYONC\u00c9")), 0.1)

    def test_multiple_keywords_match_across_artist_and_title(self):
        exact = song(1, "\u7a3b\u9999", "\u5468\u6770\u4f26")
        partial = song(2, "\u7a3b\u9999", "\u5176\u4ed6\u6b4c\u624b")
        self.assertEqual(
            rank_search_results("\u5468\u6770\u4f26 \u7a3b\u9999", [partial, exact])[0], exact
        )
        self.assertGreater(search_relevance("\u9999", exact), 0.1)

    def test_mixed_chinese_and_ascii_names_remain_searchable_without_spaces(self):
        for artist in ("Adele", "Beyonc\u00e9"):
            with self.subTest(artist=artist):
                related = song(1, f"{artist}\u73b0\u573aHello", "Artist")
                unrelated = song(2, "Hello", "Someone Else")
                self.assertEqual(
                    rank_search_results(f"{artist} Hello", [unrelated, related])[0], related
                )
                self.assertGreater(search_relevance(artist, related), 0.1)
                self.assertGreater(search_relevance("Hello", related), 0.1)
        self.assertEqual(search_relevance("it", song(3, "\u00e9it")), 0.02)
        self.assertEqual(search_relevance("it", song(3, "it\u00e9")), 0.02)

    def test_repeating_a_keyword_does_not_multiply_its_token_score(self):
        self.assertEqual(
            search_relevance("Extra Artist Hello", song(3, "Hello Extra")),
            search_relevance("Extra Artist Hello Artist", song(3, "Hello Extra")),
        )

    def test_version_preference_is_only_a_text_relevance_tie_breaker(self):
        unrelated_official = song(1, "Another Song (Official Audio)")
        matching_cover = song(2, "Hello (Cover)")
        self.assertEqual(
            rank_search_results("Hello", [unrelated_official, matching_cover])[0], matching_cover
        )
        self.assertEqual(
            [
                version_label(row)
                for row in rank_search_results(
                    "Hello",
                    [
                        song(3, "Hello (Live)"),
                        song(4, "Hello (Official Audio)"),
                        song(5, "Hello (Official Music Video)"),
                    ],
                )
            ],
            ["\u5b98\u65b9\u97f3\u9891", "MV", "Live"],
        )

    def test_explicit_versions_are_respected_across_language_aliases(self):
        rows = [
            song(1, "Hello (Official Audio)"),
            song(2, "Hello (Live)"),
            song(3, "Hello (Cover)"),
            song(4, "Hello (Remix)"),
            song(5, "Hello (Official Music Video)"),
        ]
        for query, label in (
            ("Hello live", "Live"),
            ("Hello \u73b0\u573a\u7248", "Live"),
            ("Hello cover", "\u7ffb\u5531"),
            ("Hello \u7ffb\u5531", "\u7ffb\u5531"),
            ("Hello remix", "Remix"),
            ("Hello \u6df7\u97f3", "Remix"),
            ("Hello MV", "MV"),
        ):
            with self.subTest(query=query):
                self.assertEqual(version_label(rank_search_results(query, rows)[0]), label)

    def test_chinese_official_mv_modifiers_do_not_become_artist_keywords(self):
        audio = song(1, "Hello \u5b98\u65b9\u97f3\u9891")
        video = song(2, "Hello (Official Music Video)")
        for query in (
            "Hello \u5b98\u65b9 MV",
            "Hello \u5b98\u65b9MV",
            "Hello \u5b98\u65b9 \u97f3\u4e50\u89c6\u9891",
            "Hello \u5b98\u65b9\u97f3\u4e50\u89c6\u9891",
            "Hello official MV",
        ):
            with self.subTest(query=query):
                self.assertEqual(rank_search_results(query, [audio, video])[0], video)

    def test_official_format_aliases_do_not_bias_name_phrase_matching(self):
        for titles, queries in (
            (
                ("Hello (Official Audio)", "Hello \u5b98\u65b9\u97f3\u9891"),
                (
                    "Adele Hello official audio",
                    "Adele Hello \u5b98\u65b9\u97f3\u9891",
                    "Adele Hello \u5b98\u65b9 \u97f3\u9891",
                ),
            ),
            (
                ("Hello (Official Music Video)", "Hello \u5b98\u65b9MV"),
                ("Adele Hello official music video", "Adele Hello \u5b98\u65b9 MV"),
            ),
        ):
            rows = [song(index, title, "Adele") for index, title in enumerate(titles, 1)]
            for query in queries:
                with self.subTest(query=query):
                    self.assertEqual(
                        search_relevance(query, rows[0]), search_relevance(query, rows[1])
                    )

    def test_topic_suffix_does_not_penalize_the_same_artist_name(self):
        audio = song(1, "Hello", "Adele - Topic")
        live = song(2, "Hello (Live)", "Adele")
        for query in ("Adele Hello", "Hello Adele"):
            with self.subTest(query=query):
                self.assertEqual(rank_search_results(query, [live, audio])[0], audio)
        self.assertEqual(audio.artist, "Adele - Topic")
        self.assertEqual(version_label(audio), "\u5b98\u65b9\u97f3\u9891")

    def test_unknown_channel_suffixes_are_not_removed_from_scoring(self):
        live = song(1, "Hello (Live)", "Adele")
        for suffix in ("Channel", "Archive", "Topic Archive", "Music"):
            with self.subTest(suffix=suffix):
                other = song(2, "Hello", f"Adele - {suffix}")
                self.assertEqual(rank_search_results("Adele Hello", [other, live])[0], live)
                self.assertGreater(
                    search_relevance(f"Adele {suffix} Hello", other),
                    search_relevance("Adele Missing Hello", other),
                )
                self.assertEqual(other.artist, f"Adele - {suffix}")

    def test_official_modifiers_do_not_override_explicit_live_remix_or_cover(self):
        rows = [
            song(1, "Hello (Official Audio)", "Adele"),
            song(2, "Hello (Official Music Video)", "Adele"),
            song(3, "Hello (Live at Wembley)", "Adele"),
            song(4, "Hello (Club Remix)", "Adele"),
            song(5, "Hello (Cover)", "Adele"),
        ]
        for query, label in (
            ("Adele Hello official audio live", "Live"),
            ("Adele Hello \u5b98\u65b9\u97f3\u9891 \u73b0\u573a", "Live"),
            ("Adele Hello official MV remix", "Remix"),
            ("Adele Hello \u5b98\u65b9 MV \u6df7\u97f3", "Remix"),
            ("Adele Hello official audio cover", "\u7ffb\u5531"),
            ("Adele Hello \u5b98\u65b9 MV \u7ffb\u5531", "\u7ffb\u5531"),
        ):
            with self.subTest(query=query):
                self.assertEqual(version_label(rank_search_results(query, rows)[0]), label)

    def test_literal_live_song_names_keep_their_phrase_and_format_only_queries_rank(self):
        exact = song(1, "Live Forever", "Artist - Topic")
        unrelated = song(2, "Forever (Live)")
        self.assertEqual(
            rank_search_results("Live Forever official audio", [unrelated, exact])[0], exact
        )
        audio, video = song(3, "Hello (Official Audio)"), song(4, "Hello (Official Music Video)")
        self.assertEqual(rank_search_results("official audio", [video, audio])[0], audio)
        self.assertEqual(rank_search_results("\u5b98\u65b9 MV", [audio, video])[0], video)
        literal = song(5, "Music Video")
        other = song(6, "Another Song (Official Music Video)")
        self.assertEqual(rank_search_results("Music Video", [other, literal])[0], literal)

    def test_version_labels_are_clues_not_arbitrary_substring_matches(self):
        cases = (
            ("Hello (Official Audio)", "Artist", "\u5b98\u65b9\u97f3\u9891"),
            ("Hello", "Artist - Topic", "\u5b98\u65b9\u97f3\u9891"),
            ("Hello (Official Music Video)", "Artist", "MV"),
            ("Hello \u5b98\u65b9MV", "Artist", "MV"),
            ("Hello (Live at Wembley)", "Artist", "Live"),
            ("Hello - Live", "Artist", "Live"),
            ("Hello \u73b0\u573a", "Artist", "Live"),
            ("Hello (Vocal Cover)", "Artist", "\u7ffb\u5531"),
            ("Hello (Club Remix)", "Artist", "Remix"),
            ("Hello (Official Audio Remix)", "Artist", "Remix"),
            ("Live Forever", "Artist", ""),
            ("Alive", "Artist", ""),
            ("Uncovered", "Artist", ""),
            ("MVHS", "Artist", ""),
        )
        for title, artist, label in cases:
            with self.subTest(title=title):
                self.assertEqual(version_label(song(1, title, artist)), label)

    def test_id_dedup_keeps_the_highest_ranked_description(self):
        first = song(1, "Unrelated")
        better = song(1, "Hello")
        self.assertEqual(rank_search_results("Hello", [first, better]), [better])

    def test_near_identical_recordings_are_deduplicated_after_ranking(self):
        lower = song(1, "Artist - Hello (Official Audio)", duration=61)
        preferred = song(2, "Hello [Official Audio]")
        more = song(3, "Hello (Official Audio)", artist="Other Artist")
        self.assertEqual(rank_search_results("Hello", [lower, preferred, more]), [preferred, more])

    def test_normalized_artist_and_title_keep_named_versions_distinct(self):
        first = song(1, "Hello (Club Remix)")
        second = song(2, "Hello (Radio Remix)")
        third = song(3, "Hello (Live at Wembley 2020)")
        fourth = song(4, "Hello (Live at Wembley 2021)")
        rows = [first, second, third, fourth]
        self.assertEqual(set(rank_search_results("Hello", rows)), set(rows))

    def test_dedup_never_merges_different_artist_version_or_duration(self):
        rows = [
            song(1, "Hello (Official Audio)"),
            song(2, "Hello (Official Music Video)"),
            song(3, "Hello (Live)"),
            song(4, "Hello (Cover)"),
            song(5, "Hello (Remix)"),
            song(6, "Hello (Official Audio)", artist="Other"),
            song(7, "Hello (Official Audio)", duration=64),
        ]
        self.assertEqual(set(rank_search_results("Hello", rows)), set(rows))

    def test_unknown_artists_or_unknown_duration_do_not_allow_semantic_dedup(self):
        for artist in (
            "",
            "Unknown",
            "Unknown Channel",
            "\u672a\u77e5\u9891\u9053",
            "Various Artists",
            "[\u94fe\u63a5]",
            "Artist token=[\u9690\u85cf]",
        ):
            with self.subTest(artist=artist):
                rows = [song(1, artist=artist), song(2, artist=artist)]
                self.assertEqual(rank_search_results("Hello", rows), rows)
        rows = [song(1, duration=None), song(2, duration=None)]
        self.assertEqual(rank_search_results("Hello", rows), rows)

    def test_ranking_is_stable_and_does_not_mutate_the_input(self):
        rows = [song(1, "Other One"), song(2, "Other Two"), song(3, "Other Three")]
        before = list(rows)
        self.assertEqual(rank_search_results("missing", rows), before)
        self.assertEqual(rows, before)
        self.assertEqual(rank_search_results("", []), [])
        self.assertEqual(search_relevance(" \t", rows[0]), 0)

    def test_live_state_does_not_reject_archived_live_music(self):
        for entry in (
            {"is_live": True},
            {"is_upcoming": True},
            {"live_status": "is_live"},
            {"live_status": "is_upcoming"},
        ):
            with self.subTest(entry=entry):
                self.assertTrue(is_unavailable_live(entry))
        for entry in (
            {},
            {"title": "Hello (Live)"},
            {"live_status": "was_live", "is_live": False},
            {"live_status": "not_live"},
            {"live_status": "post_live"},
            {"live_status": []},
            {"live_status": {}},
            {"is_live": "false"},
        ):
            with self.subTest(entry=entry):
                self.assertFalse(is_unavailable_live(entry))


class SearchMetadataTests(unittest.TestCase):
    def test_song_schema_is_unchanged_and_real_artist_metadata_takes_precedence(self):
        self.assertEqual(
            [field.name for field in fields(Song)],
            ["video_id", "title", "artist", "duration", "url", "thumbnail_url"],
        )
        for convert in (song_from_flat_entry, song_from_full_info):
            row = convert(
                {"id": "abcdefghijk", "duration": 60, "artist": "Adele", "channel": "XL Recordings"}
            )
            self.assertEqual(row.artist, "Adele")
            self.assertTrue(valid_song(asdict(row)))

    def test_artist_lists_are_bounded_unique_and_sanitized(self):
        for convert in (song_from_flat_entry, song_from_full_info):
            row = convert(
                {
                    "id": "abcdefghijk",
                    "duration": 60,
                    "artists": ["A", "A", None, {"name": "ignored"}, "B\nC"],
                    "channel": "Fallback",
                }
            )
            self.assertEqual(row.artist, "A, B C")
            row = convert({"id": "abcdefghijk", "duration": 60, "artists": ["x" * 200] * 20})
            self.assertLessEqual(len(row.artist), 120)
            self.assertTrue(valid_song(asdict(row)))

    def test_malformed_artist_values_use_existing_channel_fallbacks(self):
        for convert in (song_from_flat_entry, song_from_full_info):
            for row, expected in (
                ({"artist": {}, "artists": {}, "channel": "Channel"}, "Channel"),
                ({"artist": " ", "artists": [None], "uploader": "Uploader"}, "Uploader"),
                ({"artist": "\x00", "artists": ["\x00"], "uploader": "Uploader"}, "Uploader"),
                ({"channel": None, "creator": "Creator"}, "Creator"),
                ({"artists": []}, "\u672a\u77e5\u9891\u9053"),
            ):
                with self.subTest(convert=convert.__name__, row=row):
                    self.assertEqual(
                        convert({"id": "abcdefghijk", "duration": 60, **row}).artist, expected
                    )


class ServiceSearchQualityTests(unittest.IsolatedAsyncioTestCase):
    def service(self, payload):
        service = YouTubeMusicService(
            Settings.from_config({}, data_dir=Path(tempfile.gettempdir()))
        )
        service.dependency_gate = MaintenanceGate()
        service._run_ytdlp = AsyncMock(return_value=ProcessResult((), 0, json.dumps(payload), ""))
        return service

    async def test_valid_duplicate_survives_after_unknown_or_over_limit_duration(self):
        for duration in (None, 999):
            with self.subTest(duration=duration):
                service = self.service(
                    {
                        "entries": [
                            {"id": "abcdefghijk", "title": "Invalid", "duration": duration},
                            {"id": "abcdefghijk", "title": "Hello", "duration": 60},
                        ]
                    }
                )
                rows = await service.search("Hello")
                self.assertEqual(
                    [(row.video_id, row.title) for row in rows], [("abcdefghijk", "Hello")]
                )

    async def test_archived_duplicate_survives_after_live_search_entry(self):
        for state in (
            {"is_live": True},
            {"is_upcoming": True},
            {"live_status": "is_live"},
            {"live_status": "is_upcoming"},
        ):
            with self.subTest(state=state):
                service = self.service(
                    {
                        "entries": [
                            {"id": "abcdefghijk", "title": "Live Now", "duration": 60, **state},
                            {
                                "id": "abcdefghijk",
                                "title": "Hello (Live)",
                                "duration": 60,
                                "live_status": "was_live",
                            },
                        ]
                    }
                )
                rows = await service.search("Hello")
                self.assertEqual(
                    [(row.video_id, version_label(row)) for row in rows], [("abcdefghijk", "Live")]
                )

    async def test_malformed_entries_are_user_facing_errors(self):
        for entries in ("bad", 0, 1, True, {"id": "abcdefghijk"}):
            with self.subTest(entries=entries):
                with self.assertRaises(UserFacingError):
                    await self.service({"entries": entries}).search("Hello")
        for payload in ("bad", 0, 1, True, []):
            with self.subTest(payload=payload):
                with self.assertRaises(UserFacingError):
                    await self.service(payload).search("Hello")

    async def test_invalid_queries_do_not_start_a_search(self):
        service = self.service({})
        for query in (None, {}, [], "", " \t", "x" * 201):
            with self.subTest(query=query):
                with self.assertRaises(UserFacingError):
                    await service.search(query)
        service._run_ytdlp.assert_not_awaited()

    async def test_candidate_pool_preserves_default_search_limits_and_fetch_arguments(self):
        payload = {
            "entries": [
                {"id": f"track{index:06d}", "title": f"Hello {index}", "duration": 60}
                for index in range(20)
            ]
        }
        for limit, expected in ((None, 5), (3, 3)):
            with self.subTest(limit=limit):
                service = self.service(payload)
                arguments = {} if limit is None else {"result_limit": limit}
                rows = await service.search("Hello", **arguments)
                candidates = await service.search("Hello", candidate_pool=True, **arguments)
                self.assertEqual(len(rows), expected)
                self.assertEqual(len(candidates), expected * 3)
                self.assertEqual(rows, candidates[:expected])
                first, second = service._run_ytdlp.await_args_list
                self.assertEqual(first, second)
                self.assertEqual(first.args[0][-1], f"ytsearch{expected * 3}:Hello")

    async def test_service_respects_keywords_versions_and_real_artist_metadata(self):
        payload = {
            "entries": [
                {
                    "id": "track000001",
                    "title": "Another Song (Official Audio)",
                    "artist": "Someone Else",
                    "duration": 60,
                },
                {
                    "id": "track000002",
                    "title": "Hello (Official Audio)",
                    "artist": "Adele",
                    "channel": "XL Recordings",
                    "duration": 60,
                },
                {
                    "id": "track000003",
                    "title": "Hello (Live at Wembley)",
                    "artist": "Adele",
                    "channel": "Uploader",
                    "duration": 60,
                },
                {
                    "id": "track000004",
                    "title": "Hello (Cover)",
                    "artist": "Other Artist",
                    "duration": 60,
                },
            ]
        }
        service = self.service(payload)
        for query, video_id, label in (
            ("Adele Hello", "track000002", "\u5b98\u65b9\u97f3\u9891"),
            ("Adele Hello \u73b0\u573a", "track000003", "Live"),
        ):
            with self.subTest(query=query):
                rows = await service.search(query)
                self.assertEqual(rows[0].video_id, video_id)
                self.assertEqual(rows[0].artist, "Adele")
                self.assertEqual(version_label(rows[0]), label)

    async def test_service_ranks_topic_audio_and_bilingual_official_mv_keywords(self):
        service = self.service(
            {
                "entries": [
                    {
                        "id": "track000001",
                        "title": "Hello (Live)",
                        "artist": "Adele",
                        "duration": 60,
                    },
                    {
                        "id": "track000002",
                        "title": "Hello",
                        "channel": "Adele - Topic",
                        "duration": 60,
                    },
                    {
                        "id": "track000003",
                        "title": "Hello (Official Music Video)",
                        "artist": "Adele",
                        "duration": 60,
                    },
                    {
                        "id": "track000004",
                        "title": "Hello \u5b98\u65b9\u97f3\u9891",
                        "artist": "Adele",
                        "duration": 60,
                    },
                ]
            }
        )
        for query, video_id, label in (
            ("Adele Hello", "track000002", "\u5b98\u65b9\u97f3\u9891"),
            ("Hello \u5b98\u65b9 MV", "track000003", "MV"),
        ):
            with self.subTest(query=query):
                rows = await service.search(query)
                self.assertEqual(rows[0].video_id, video_id)
                self.assertEqual(version_label(rows[0]), label)

    async def test_metadata_rejects_current_live_and_upcoming_video(self):
        for state in (
            {"is_live": True},
            {"is_upcoming": True},
            {"live_status": "is_live"},
            {"live_status": "is_upcoming"},
        ):
            with self.subTest(state=state):
                service = self.service(
                    {"id": "abcdefghijk", "title": "Hello", "duration": 60, **state}
                )
                with self.assertRaises(UserFacingError):
                    await service.get_metadata("https://youtu.be/abcdefghijk")

    async def test_metadata_accepts_archived_live_artist_and_bare_share(self):
        service = self.service(
            {
                "id": "abcdefghijk",
                "title": "Hello (Live at Wembley)",
                "artist": "Adele",
                "channel": "Uploader",
                "duration": 60,
                "live_status": "was_live",
                "is_live": False,
            }
        )
        row = await service.get_metadata("youtu.be/abcdefghijk")
        self.assertEqual(row.artist, "Adele")
        self.assertEqual(version_label(row), "Live")
        self.assertEqual(row.url, "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(service._run_ytdlp.await_args.args[0][-1], row.url)


class YouTubeShareTests(unittest.TestCase):
    def test_existing_watch_short_music_and_embedded_links_still_normalize(self):
        for url in (
            "https://youtu.be/abcdefghijk?t=30",
            "http://youtube.com/watch?v=abcdefghijk&list=playlist",
            "https://www.youtube.com/watch?v=abcdefghijk",
            "https://m.youtube.com/watch?v=abcdefghijk",
            "https://music.youtube.com/watch?v=abcdefghijk",
            "https://youtube.com/shorts/abcdefghijk",
            "https://youtube.com/embed/abcdefghijk",
            "https://youtube.com/live/abcdefghijk",
            "https://youtube.com/v/abcdefghijk",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    parse_youtube_url(url), "https://www.youtube.com/watch?v=abcdefghijk"
                )

    def test_bare_and_nocookie_shares_are_canonical_single_videos(self):
        for url in (
            "youtu.be/abcdefghijk",
            "YouTube.com/watch?v=abcdefghijk",
            "www.youtube.com/shorts/abcdefghijk",
            "music.youtube.com/watch?v=abcdefghijk",
            "//youtu.be/abcdefghijk",
            "https://youtube-nocookie.com/embed/abcdefghijk",
            "https://www.youtube-nocookie.com/embed/abcdefghijk",
            "www.youtube-nocookie.com/embed/abcdefghijk",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    parse_youtube_url(url), "https://www.youtube.com/watch?v=abcdefghijk"
                )
                self.assertEqual(youtube_video_id(url), "abcdefghijk")

    def test_host_credentials_and_unsupported_targets_never_produce_video_urls(self):
        for url in (
            "youtube.com.evil.test/watch?v=abcdefghijk",
            "youtube.com@evil.test/watch?v=abcdefghijk",
            "https://evil.test/?url=https://youtube.com/watch?v=abcdefghijk",
            "https://api.youtube.com/watch?v=abcdefghijk",
            "//api.youtube.com/watch?v=abcdefghijk",
            "https://user:password@youtube.com/watch?v=abcdefghijk",
            "https://youtube.com:bad/watch?v=abcdefghijk",
            "https://youtube.com:8443/watch?v=abcdefghijk",
            "https://youtube.com/clip/abcdefghijk",
            "https://youtube.com/playlist?list=abcdefghijk",
            "https://youtube.com/@Artist",
            "https://youtu.be/invalid_id_that_is_too_long",
            "https://youtu.be/abc",
            "https://you\ntube.com/watch?v=abcdefghijk",
            "https://youtu.be/abcdefghijk" + "x" * 2048,
            "file://youtube.com/watch?v=abcdefghijk",
            None,
            {},
        ):
            with self.subTest(url=url):
                self.assertIsNone(parse_youtube_url(url))
                self.assertEqual(youtube_video_id(url), "")

    def test_existing_synthetic_video_id_range_is_not_tightened(self):
        for video_id in ("abc123", "abcdefghijk", "a" * 20):
            self.assertEqual(
                parse_youtube_url(f"https://youtu.be/{video_id}"),
                f"https://www.youtube.com/watch?v={video_id}",
            )
