export const LIBRARY_PAGE_SIZE = 20;
export const MAX_BATCH_SELECTION = 20;

export function libraryEntries(library, source) {
  return source === "history"
    ? (library.history || []).map((row) => ({ ...row }))
    : (library.favorites || []).map((song) => ({ song }));
}

export function orderedLibraryEntries(library, source, { query = "", sort = "recent", locale = "en" } = {}) {
  const needle = query.trim().toLocaleLowerCase(locale);
  const entries = libraryEntries(library, source).filter(({ song }) =>
    `${song.title} ${song.artist}`.toLocaleLowerCase(locale).includes(needle));
  if (sort === "recent") {
    if (source === "history") entries.sort((a, b) => (b.played_at || 0) - (a.played_at || 0));
  } else if (sort === "plays" && source === "history") {
    entries.sort((a, b) => (b.play_count || 0) - (a.play_count || 0) || (b.played_at || 0) - (a.played_at || 0));
  } else if (sort === "duration") {
    entries.sort((a, b) => (a.song.duration ?? Infinity) - (b.song.duration ?? Infinity));
  } else if (sort === "title" || sort === "artist") {
    entries.sort((a, b) => String(a.song[sort]).localeCompare(String(b.song[sort]), locale));
  }
  return entries;
}

export function libraryPage(entries, requestedPage = 1) {
  const pages = Math.max(1, Math.ceil(entries.length / LIBRARY_PAGE_SIZE));
  const page = Math.min(pages, Math.max(1, Math.floor(requestedPage) || 1));
  const offset = (page - 1) * LIBRARY_PAGE_SIZE;
  return {
    items: entries.slice(offset, offset + LIBRARY_PAGE_SIZE),
    page,
    pages,
    start: entries.length ? offset + 1 : 0,
    end: Math.min(offset + LIBRARY_PAGE_SIZE, entries.length),
    total: entries.length,
  };
}

export function selectTracks(selection, videoIds, checked, limit = MAX_BATCH_SELECTION) {
  const next = new Set(selection);
  for (const id of videoIds) {
    if (!checked) next.delete(id);
    else if (next.size < limit) next.add(id);
  }
  return next;
}

export function pruneSelection(selection, entries) {
  const available = new Set(entries.map(({ song }) => song.video_id));
  return new Set([...selection].filter((id) => available.has(id)));
}

export function selectedLibraryEntries(library, source, selection, options) {
  return orderedLibraryEntries(library, source, { ...options, query: "" })
    .filter(({ song }) => selection.has(song.video_id));
}

export function summarizeSongs(songs) {
  let durationSeconds = 0, unknownDurationCount = 0;
  for (const song of songs) {
    if (Number.isFinite(song.duration) && song.duration > 0) durationSeconds += song.duration;
    else unknownDurationCount += 1;
  }
  return {
    count: songs.length,
    duration_seconds: durationSeconds,
    unknown_duration_count: unknownDurationCount,
  };
}
