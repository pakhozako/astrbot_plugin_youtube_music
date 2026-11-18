import { test } from "node:test";
import assert from "node:assert/strict";
import {
  LIBRARY_PAGE_SIZE, MAX_BATCH_SELECTION, libraryEntries, libraryPage,
  orderedLibraryEntries, pruneSelection, selectedLibraryEntries, selectTracks, summarizeSongs,
} from "../frontend/library-model.mjs";

const songs = [
  { video_id: "alpha000001", title: "Alpha", artist: "Zed", duration: 120 },
  { video_id: "beta0000002", title: "Beta", artist: "Ada", duration: 60 },
  { video_id: "gamma000003", title: "Gamma", artist: "Ada", duration: null },
];
const library = {
  favorites: songs,
  history: songs.map((song, index) => ({ song, played_at: index + 1, play_count: [3, 5, 5][index] })),
};
const ids = (entries) => entries.map(({ song }) => song.video_id);

test("library entries retain history metadata without mutating source records", () => {
  const entries = libraryEntries(library, "history");
  assert.equal(entries[0].play_count, 3);
  entries[0].play_count = 99;
  assert.equal(library.history[0].play_count, 3);
  assert.deepEqual(ids(libraryEntries(library, "favorites")), songs.map((song) => song.video_id));
});

test("filtering and sorting preserve video IDs and never mutate the library", () => {
  assert.deepEqual(ids(orderedLibraryEntries(library, "favorites", { query: " ADA ", sort: "title" })), [songs[1].video_id, songs[2].video_id]);
  assert.deepEqual(ids(orderedLibraryEntries(library, "favorites", { sort: "duration" })), [songs[1].video_id, songs[0].video_id, songs[2].video_id]);
  assert.deepEqual(ids(orderedLibraryEntries(library, "favorites", { sort: "artist" })), [songs[1].video_id, songs[2].video_id, songs[0].video_id]);
  assert.deepEqual(library.favorites, songs);
});

test("history sorts newest submissions first and breaks frequency ties by latest submission", () => {
  assert.deepEqual(ids(orderedLibraryEntries(library, "history")), [songs[2].video_id, songs[1].video_id, songs[0].video_id]);
  assert.deepEqual(ids(orderedLibraryEntries(library, "history", { sort: "plays" })), [songs[2].video_id, songs[1].video_id, songs[0].video_id]);
  assert.deepEqual(ids(orderedLibraryEntries(library, "favorites", { sort: "plays" })), songs.map((song) => song.video_id));
});

test("pagination renders at most 20 rows and clamps an out-of-date page after removal", () => {
  const entries = Array.from({ length: 45 }, (_, id) => ({ song: { video_id: String(id) } }));
  assert.equal(LIBRARY_PAGE_SIZE, 20);
  const middle = libraryPage(entries, 2);
  assert.deepEqual([middle.page, middle.pages, middle.start, middle.end, middle.items.length], [2, 3, 21, 40, 20]);
  assert.deepEqual(libraryPage(entries.slice(0, 5), 3), { items: entries.slice(0, 5), page: 1, pages: 1, start: 1, end: 5, total: 5 });
});

test("empty and invalid page values keep stable pagination dimensions", () => {
  assert.deepEqual(libraryPage([], -1), { items: [], page: 1, pages: 1, start: 0, end: 0, total: 0 });
  assert.equal(libraryPage(Array(25).fill({}), Number.NaN).page, 1);
  assert.equal(libraryPage(Array(25).fill({}), Number.POSITIVE_INFINITY).page, 2);
});

test("selection is capped at 20 without mutating its previous state", () => {
  const initial = new Set([songs[0].video_id]);
  const selection = selectTracks(initial, Array.from({ length: 25 }, (_, id) => `id-${id}`), true);
  assert.equal(MAX_BATCH_SELECTION, 20);
  assert.equal(selection.size, 20);
  assert.equal(initial.size, 1);
  assert.equal(selectTracks(selection, ["new-id"], true).size, 20);
  assert.equal(selectTracks(selection, [songs[0].video_id], false).size, 19);
});

test("page deselection preserves selected tracks on another page", () => {
  const selection = selectTracks(new Set(["off-page"]), ["page-a", "page-b"], true);
  assert.deepEqual([...selectTracks(selection, ["page-a", "page-b"], false)], ["off-page"]);
});

test("selection survives filtering and uses the current full-source sort order", () => {
  const selection = new Set([songs[0].video_id, songs[1].video_id]);
  const selected = selectedLibraryEntries(library, "favorites", selection, { query: "Gamma", sort: "duration" });
  assert.deepEqual(ids(selected), [songs[1].video_id, songs[0].video_id]);
  assert.deepEqual([...selection], [songs[0].video_id, songs[1].video_id]);
});

test("refreshing one source only prunes selections no longer in that source", () => {
  const selection = new Set(songs.map((song) => song.video_id));
  assert.deepEqual([...pruneSelection(selection, libraryEntries({ favorites: [songs[0]] }, "favorites"))], [songs[0].video_id]);
  assert.equal(selection.size, 3);
});

test("duration totals distinguish unknown values and never count a submitted current item implicitly", () => {
  assert.deepEqual(summarizeSongs([songs[0], songs[1], songs[2], { duration: 0 }, { duration: Number.NaN }]), {
    count: 5, duration_seconds: 180, unknown_duration_count: 3,
  });
  assert.deepEqual(summarizeSongs([]), { count: 0, duration_seconds: 0, unknown_duration_count: 0 });
});
