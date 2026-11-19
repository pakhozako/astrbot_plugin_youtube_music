"""Pure, bounded search ranking and conservative recording/version deduplication."""

from __future__ import annotations

import re
import unicodedata

from .utils import Song

_CJK_RANGE = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK = re.compile(rf"[{_CJK_RANGE}]")
_NON_CJK_WORD = rf"[^\W_{_CJK_RANGE}]"
_MIXED_SCRIPT_BOUNDARY = re.compile(
    rf"(?<={_CJK.pattern})(?={_NON_CJK_WORD})|(?<={_NON_CJK_WORD})(?={_CJK.pattern})"
)
_REMIX = re.compile(r"(?<![a-z0-9])remix(?:ed)?(?![a-z0-9])|\u6df7\u97f3|\u91cd\u6df7")
_COVER = re.compile(r"(?<![a-z0-9])cover(?![a-z0-9])|\u7ffb\u5531")
_LIVE = re.compile(
    r"[\[(]\s*live(?:\s|[\])])|"
    r"(?<![a-z0-9])live\s+(?:at|from|in|on|version|performance|session)(?![a-z0-9])|"
    r"[-:|]\s*live(?:\s|$)|\u73b0\u573a|\u6f14\u5531\u4f1a"
)
_LIVE_QUERY = re.compile(r"(?<![a-z0-9])live(?![a-z0-9])|\u73b0\u573a|\u6f14\u5531\u4f1a")
_MV = re.compile(
    r"(?<![a-z0-9])(?:official\s+)?music\s+video(?![a-z0-9])|"
    r"(?<![a-z0-9])(?:official\s+)?mv(?![a-z0-9])|"
    r"(?<![a-z0-9])official\s+video(?![a-z0-9])|"
    r"\u97f3\u4e50\u5f55\u5f71\u5e26|\u97f3\u4e50\u89c6\u9891"
)
_OFFICIAL_AUDIO = re.compile(
    r"(?<![a-z0-9])official\s+audio(?![a-z0-9])|\u5b98\u65b9\s*\u97f3\u9891"
)
_TOPIC = re.compile(r"\s[-\u2013\u2014]\s*topic$")
_BRACKET = re.compile(r"[\[(\u3010]([^\[\]()\u3010\u3011]{1,80})[\])\u3011]")
_OFFICIAL_SUFFIX = re.compile(
    r"\s+(?:[-:|]\s*)?(?:official\s+audio|(?:official\s+)?music\s+video|"
    r"official\s+video|(?:official\s+)?mv|"
    r"\u5b98\u65b9\s*(?:mv|\u97f3\u9891|\u97f3\u4e50\u5f55\u5f71\u5e26|"
    r"\u97f3\u4e50\u89c6\u9891))\s*$"
)
_ARTIST_SEPARATOR = re.compile(r"\s+[-:|\u2013\u2014]\s+")
_LABELS = {
    "official_audio": "\u5b98\u65b9\u97f3\u9891",
    "mv": "MV",
    "live": "Live",
    "cover": "\u7ffb\u5531",
    "remix": "Remix",
    "": "",
}
_VERSION_TERMS = {
    "official_audio": {
        "official",
        "audio",
        "\u5b98\u65b9",
        "\u97f3\u9891",
        "\u5b98\u65b9\u97f3\u9891",
    },
    "mv": {
        "official",
        "music",
        "video",
        "mv",
        "\u5b98\u65b9",
        "\u5b98\u65b9\u97f3\u4e50\u5f55\u5f71\u5e26",
        "\u5b98\u65b9\u97f3\u4e50\u89c6\u9891",
        "\u97f3\u4e50\u5f55\u5f71\u5e26",
        "\u97f3\u4e50\u89c6\u9891",
    },
    "live": {"live", "\u73b0\u573a", "\u73b0\u573a\u7248", "\u6f14\u5531\u4f1a"},
    "cover": {"cover", "\u7ffb\u5531", "\u7ffb\u5531\u7248"},
    "remix": {"remix", "remixed", "\u6df7\u97f3", "\u6df7\u97f3\u7248", "\u91cd\u6df7"},
}
_DEFAULT_PREFERENCE = {"official_audio": 4, "mv": 3, "": 2, "live": 1, "cover": 0, "remix": 0}
_DECORATIONS = {
    "official audio",
    "official music video",
    "official video",
    "official mv",
    "music video",
    "audio",
    "mv",
    "lyrics",
    "lyric video",
    "official lyric video",
    "hd",
    "hq",
    "4k",
    "1080p",
    "\u5b98\u65b9\u97f3\u9891",
    "\u5b98\u65b9 \u97f3\u9891",
    "\u5b98\u65b9 mv",
    "\u5b98\u65b9\u97f3\u4e50\u5f55\u5f71\u5e26",
    "\u5b98\u65b9 \u97f3\u4e50\u5f55\u5f71\u5e26",
    "\u5b98\u65b9\u97f3\u4e50\u89c6\u9891",
    "\u5b98\u65b9 \u97f3\u4e50\u89c6\u9891",
    "\u97f3\u4e50\u5f55\u5f71\u5e26",
    "\u97f3\u4e50\u89c6\u9891",
    "\u6b4c\u8bcd",
}
_UNKNOWN_ARTISTS = {
    "",
    "unknown",
    "unknown artist",
    "unknown channel",
    "various",
    "various artists",
    "\u672a\u77e5",
    "\u672a\u77e5\u6b4c\u624b",
    "\u672a\u77e5\u9891\u9053",
}


def _normal(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _words(value: object) -> str:
    text = _MIXED_SCRIPT_BOUNDARY.sub(" ", _normal(value))
    return " ".join(re.findall(r"[^\W_]+", text))


def _clean_title(value: object) -> str:
    title = _MIXED_SCRIPT_BOUNDARY.sub(" ", _normal(value))
    title = _BRACKET.sub(lambda match: " " if _words(match[1]) in _DECORATIONS else match[0], title)
    return _OFFICIAL_SUFFIX.sub("", title)


def _contains(term: str, text: str) -> bool:
    if _CJK.search(term):
        return term in text
    return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text))


def _version_kind(title: str, artist: str = "", *, query: bool = False) -> str:
    title = _normal(title)
    for kind, pattern in (
        ("remix", _REMIX),
        ("cover", _COVER),
        ("live", _LIVE_QUERY if query else _LIVE),
        ("mv", _MV),
        ("official_audio", _OFFICIAL_AUDIO),
    ):
        if pattern.search(title):
            return kind
    return "official_audio" if not query and _TOPIC.search(_normal(artist)) else ""


def version_label(song: Song) -> str:
    """Describe title/channel clues, not verified artist or official provenance."""
    return _LABELS[_version_kind(song.title, song.artist)]


def is_unavailable_live(entry: dict) -> bool:
    """Reject current broadcasts and premieres, not archived live performances."""
    status = entry.get("live_status")
    return (
        entry.get("is_live") is True
        or entry.get("is_upcoming") is True
        or isinstance(status, str)
        and status in {"is_live", "is_upcoming"}
    )


def _query_spec(query: str) -> tuple[str, list[str], str]:
    query_words = _words(query).split()
    has_official = any(
        term == "official" or term.startswith("\u5b98\u65b9") for term in query_words
    )
    ignored: set[str] = set()
    for kind, pattern in (("mv", _MV), ("official_audio", _OFFICIAL_AUDIO)):
        if (kind == "official_audio" or has_official) and pattern.search(_normal(query)):
            ignored.update(_VERSION_TERMS[kind])
    # Official format aliases constrain the version, not the artist/title phrase.
    normalized = " ".join(term for term in query_words if term not in ignored)
    return normalized, list(dict.fromkeys(normalized.split())), _version_kind(query, query=True)


def _relevance(normalized: str, terms: list[str], preferred: str, song: Song) -> float:
    if not normalized:
        return 0.09 if preferred and _version_kind(song.title, song.artist) == preferred else 0.0
    title = _words(_clean_title(song.title))
    artist = _words(_TOPIC.sub("", _normal(song.artist)))
    kind = _version_kind(song.title, song.artist)
    version_terms = _VERSION_TERMS.get(preferred, set())
    matched_names = name_terms = score = 0
    for term in terms:
        is_version = term in version_terms
        if not is_version:
            name_terms += 1
        matches = _contains(term, title) or _contains(term, artist)
        if is_version and kind == preferred:
            matches = True
        if matches:
            score += 1 if is_version else 4
            if not is_version:
                matched_names += 1
    if name_terms and matched_names == name_terms:
        score += 6
    fields = (title, artist, f"{artist} {title}", f"{title} {artist}")
    phrase_fields = [text for text in fields if _contains(normalized, text)]
    if phrase_fields:
        score += 8
        if any(text.startswith(normalized) for text in phrase_fields):
            score += 2
        if normalized in fields:
            score += 2
    # Integer text scores always dominate the small version tie-breaker.
    preference = (
        9 if preferred and kind == preferred else 0 if preferred else _DEFAULT_PREFERENCE[kind]
    )
    return score + preference / 100


def search_relevance(query: str, song: Song) -> float:
    normalized, terms, preferred = _query_spec(query)
    return _relevance(normalized, terms, preferred, song)


def _recording_identity(song: Song) -> tuple[str, str, str] | None:
    artist = _words(song.artist)
    if (
        artist in _UNKNOWN_ARTISTS
        or "[\u94fe\u63a5]" in song.artist
        or "[\u9690\u85cf]" in song.artist
        or type(song.duration) is not int
        or song.duration <= 0
    ):
        return None
    title = _clean_title(song.title)
    pieces = _ARTIST_SEPARATOR.split(title.strip())
    if len(pieces) > 1 and _words(pieces[0]) == artist:
        pieces.pop(0)
    if len(pieces) > 1 and _words(pieces[-1]) == artist:
        pieces.pop()
    title = _words(" - ".join(pieces))
    if not title:
        return None
    return artist, title, _version_kind(song.title, song.artist)


def rank_search_results(query: str, songs: list[Song]) -> list[Song]:
    """Rank stably, then retain one video/near-identical recording within each version."""
    normalized, terms, preferred = _query_spec(query)
    ranked = sorted(
        songs,
        key=lambda song: _relevance(normalized, terms, preferred, song),
        reverse=True,
    )
    seen_ids: set[str] = set()
    recordings: dict[tuple[str, str, str], list[int]] = {}
    result = []
    for song in ranked:
        if song.video_id in seen_ids:
            continue
        seen_ids.add(song.video_id)
        identity = _recording_identity(song)
        if identity is not None:
            durations = recordings.setdefault(identity, [])
            if any(abs(song.duration - duration) <= 2 for duration in durations):
                continue
            durations.append(song.duration)
        result.append(song)
    return result
