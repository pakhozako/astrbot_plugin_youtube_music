// Test-only fixtures, not a real AstrBot connection or QQ delivery.
const songs = [
  { video_id: "abcdefghijk", title: "示例曲目 Alpha", artist: "测试音乐人", duration: 120, url: "https://www.youtube.com/watch?v=abcdefghijk", thumbnail_url: "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg" },
  { video_id: "bcdefghijkl", title: "示例曲目 Beta", artist: "测试音乐人", duration: 90, url: "https://www.youtube.com/watch?v=bcdefghijkl", thumbnail_url: "https://i.ytimg.com/vi/bcdefghijkl/hqdefault.jpg" },
  { video_id: "cdefghijklm", title: "示例曲目 Gamma", artist: "测试合奏", duration: 150, url: "https://www.youtube.com/watch?v=cdefghijklm", thumbnail_url: "https://i.ytimg.com/vi/cdefghijklm/hqdefault.jpg" },
];
const context = { locale: "zh-CN", isDark: false, preview: true };
const fixtures = {
  sessions: [{ key: "session-a", label: "QQ 群 123456", count: 2 }, { key: "session-b", label: "QQ 群 654321", count: 0 }],
  queues: {
    "session-a": { paused: true, items: songs.slice(0, 2).map((song, i) => ({ id: `entry-${i}`, song, requested_by: "示例用户" })), current: null, error: "" },
    "session-b": { paused: false, items: [], current: null, error: "" },
  },
  limits: { queue: 20, duration: 180, batch: 20 }, revision: 1,
  dependency_update: { status: "idle", phase: "", progress: 0, message: "" },
};
const library = { favorites: [], history: [] };
const subscribers = new Map();
const failedEntries = new Map();
const contextListeners = new Set();
let translations = {};
let nextId = 0;
function notify() { for (const handler of subscribers.values()) handler.onMessage({ parsed: { revision: ++fixtures.revision } }); }
const clone = (value) => structuredClone(value);
window.mockControl = {
  fixtures, library, songs, subscribers, notify, failNext: "", requests: [], waiting: new Map(),
  captureStatus: false, contextListeners,
  updaterSupported: true, updaterReason: "此平台不支持自动更新。",
  hold(endpoint) { this.waiting.set(endpoint, {}); },
  release(endpoint) { this.waiting.get(endpoint)?.resolve?.(); this.waiting.delete(endpoint); },
  disconnect() { for (const handler of subscribers.values()) handler.onError(); },
  restartRuntime() {
    failedEntries.clear();
    for (const queue of Object.values(fixtures.queues)) {
      queue.error = "";
      queue.current = null;
    }
    notify();
  },
  theme(dark) { context.isDark = dark; document.documentElement.dataset.theme = dark ? "dark" : "light"; },
  locale(locale) { context.locale = locale; for (const listener of contextListeners) listener(context); },
};
async function beforeRequest(method, endpoint, body) {
  window.mockControl.requests.push({ method, endpoint, body: clone(body) });
  const wait = window.mockControl.waiting.get(endpoint);
  if (wait) await new Promise((resolve) => { wait.resolve = resolve; });
}
function failIfRequested() {
  if (!window.mockControl.failNext) return;
  const message = window.mockControl.failNext;
  window.mockControl.failNext = "";
  throw new Error(message);
}
function statusSnapshot() {
  for (const session of fixtures.sessions) {
    const queue = fixtures.queues[session.key];
    session.count = queue.items.length;
    rememberFailure(session.key, queue);
  }
  const result = clone(fixtures);
  for (const queue of Object.values(result.queues)) {
    queue.summary = { count: queue.items.length, duration_seconds: 0, unknown_duration_count: 0 };
    for (const { song } of queue.items) {
      if (Number.isFinite(song.duration) && song.duration > 0) queue.summary.duration_seconds += song.duration;
      else queue.summary.unknown_duration_count += 1;
    }
  }
  return result;
}
function rememberFailure(session, queue) {
  if (!queue.error) failedEntries.delete(session);
  else if (!failedEntries.has(session)) failedEntries.set(session, queue.items[0]?.id || "");
}
function sourceSongs(source) {
  if (source === "favorites") return library.favorites;
  if (source === "history") return library.history.map((row) => row.song);
  if (source === "search") return songs;
  throw new Error("歌曲来源无效。");
}
function resolveSong(body) {
  const source = body.source || "search";
  const candidates = sourceSongs(source);
  const song = source === "search" ? candidates[body.index - 1] : candidates.find((song) => song.video_id === body.video_id);
  if (!song) throw new Error("歌曲不存在或已过期。");
  if (song.video_id !== body.video_id) throw new Error("搜索结果已更新，请重新选择。");
  return song;
}
function targetQueue(session) {
  if (typeof session !== "string" || !Object.hasOwn(fixtures.queues, session) || !fixtures.sessions.some((item) => item.key === session)) throw new Error("会话不存在。");
  return fixtures.queues[session];
}
function validateDuration(song) {
  if (!Number.isFinite(song.duration) || song.duration <= 0 || song.duration > fixtures.limits.duration) throw new Error("歌曲超过时长限制或时长未知。");
}
window.AstrBotPluginView = {
  async ready() {
    await beforeRequest("HOST", "ready");
    translations = (await (await fetch("/tests/en-US.json")).json()).views.console;
    return context;
  },
  t(key, fallback) { return context.locale.startsWith("en") ? translations[key.split(".").at(-1)] || fallback : fallback; },
  onContext(handler) { contextListeners.add(handler); return () => contextListeners.delete(handler); },
  async apiGet(endpoint) {
    const captured = endpoint === "status" && window.mockControl.captureStatus ? statusSnapshot() : null;
    await beforeRequest("GET", endpoint);
    failIfRequested();
    if (endpoint === "status") {
      return captured || statusSnapshot();
    }
    if (endpoint === "search") return { songs: clone(songs) };
    if (endpoint === "library") return clone(library);
    if (endpoint === "maintenance") return {
      dependencies: { platform: "win32", architecture: "AMD64", updater_supported: window.mockControl.updaterSupported, updater_reason: window.mockControl.updaterReason, dependencies: ["yt-dlp", "Deno", "FFmpeg", "ffprobe"].map((name) => ({ name, version: "模拟版本", status: "ok" })) },
      cache: { items: 8, bytes: 24 * 1048576 },
    };
    throw new Error(`Unknown mock endpoint: ${endpoint}`);
  },
  async apiPost(endpoint, body) {
    await beforeRequest("POST", endpoint, body);
    failIfRequested();
    if (endpoint === "queue/add") {
      const queue = targetQueue(body.session);
      const song = resolveSong(body);
      validateDuration(song);
      if (queue.items.some((item) => item.song.video_id === song.video_id)) throw new Error("这首歌已在队列中。");
      if (queue.items.length >= fixtures.limits.queue) throw new Error("当前会话队列已满。");
      queue.items.push({ id: `mock-${++nextId}`, song: clone(song), requested_by: "Dashboard" });
      notify();
      return { position: queue.items.length };
    }
    if (endpoint === "queue/batch") {
      if (!["favorites", "history"].includes(body.source) || !Array.isArray(body.video_ids) || !body.video_ids.length || body.video_ids.length > 20 || body.video_ids.some((id) => typeof id !== "string" || !/^[A-Za-z0-9_-]{6,20}$/.test(id)) || new Set(body.video_ids).size !== body.video_ids.length) throw new Error("批量歌曲参数无效。");
      const queue = targetQueue(body.session);
      const candidates = sourceSongs(body.source);
      const requested = body.video_ids.map((id) => {
        const song = candidates.find((song) => song.video_id === id);
        if (!song) throw new Error("歌曲不存在或已移除，请刷新音乐库。");
        return song;
      });
      const existing = new Set(queue.items.map((item) => item.song.video_id));
      const additions = requested.filter((song) => !existing.has(song.video_id));
      const skipped = requested.filter((song) => existing.has(song.video_id)).map((song) => song.video_id);
      additions.forEach(validateDuration);
      if (queue.items.length + additions.length > fixtures.limits.queue) throw new Error("当前会话队列剩余容量不足，未加入任何歌曲。");
      const positions = additions.map((song, index) => ({ video_id: song.video_id, position: queue.items.length + index + 1 }));
      if (additions.length) {
        queue.items.push(...additions.map((song) => ({ id: `mock-${++nextId}`, song: clone(song), requested_by: "Dashboard" })));
        notify();
      }
      return { added: additions.length, positions, skipped, queue_size: queue.items.length };
    }
    if (endpoint === "queue/action") {
      const queue = targetQueue(body.session);
      rememberFailure(body.session, queue);
      const failedId = failedEntries.get(body.session);
      if (body.action === "resume") queue.paused = false;
      if (body.action === "pause") queue.paused = true;
      if (body.action === "clear") { queue.items = []; queue.paused = true; }
      if (body.action === "skip") queue.items.shift();
      if (body.action === "remove") queue.items = queue.items.filter((item) => item.id !== body.entry_id);
      if (body.action === "move") {
        const index = queue.items.findIndex((item) => item.id === body.entry_id);
        const target = index + (body.direction === "up" ? -1 : 1);
        [queue.items[index], queue.items[target]] = [queue.items[target], queue.items[index]];
      }
      if (body.action === "forget") {
        fixtures.sessions = fixtures.sessions.filter((session) => session.key !== body.session);
        delete fixtures.queues[body.session];
      }
      const removedFailure = failedId && ["skip", "remove", "cancel_mine"].includes(body.action) && !queue.items.some((item) => item.id === failedId);
      if (["resume", "clear", "forget"].includes(body.action) || removedFailure) {
        queue.error = "";
        failedEntries.delete(body.session);
      }
      if (body.action === "skip") queue.paused = Boolean(queue.error);
      notify();
      return { ok: true };
    }
    if (endpoint === "library/action") {
      if (body.action === "favorite") {
        const song = resolveSong(body);
        if (!library.favorites.some((item) => item.video_id === song.video_id)) library.favorites.unshift(clone(song));
      }
      if (body.action === "remove") library.favorites = library.favorites.filter((song) => song.video_id !== body.video_id);
      if (body.action === "clear_history") library.history = [];
      return { ok: true };
    }
    if (endpoint === "dependencies/update") {
      fixtures.dependency_update = { status: "running", phase: "模拟下载", progress: 35, message: "模拟更新任务" };
      notify(); return { started: true };
    }
    if (endpoint === "dependencies/cancel") {
      fixtures.dependency_update.status = "cancelled";
      fixtures.dependency_update.phase = "已取消";
      notify(); return { ok: true };
    }
    if (endpoint === "cache/cleanup") return { deleted: 8 };
    throw new Error(`Unknown mock endpoint: ${endpoint}`);
  },
  async subscribeSSE(endpoint, handlers) {
    if (endpoint !== "events") throw new Error("Unexpected SSE endpoint");
    const id = `sse-${++nextId}`;
    subscribers.set(id, handlers);
    handlers.onOpen();
    return id;
  },
  async unsubscribeSSE(id) { subscribers.delete(id); },
};
