import createButton from "material/components/button";
import createIconButton from "material/components/icon-button";
import "material/styles/base";
import "material/styles/button";
import "material/styles/icon-button";
import { icons } from "./icons.js";
import { LiveConnection } from "../views/console/live-connection.mjs";
import {
  MAX_BATCH_SELECTION, libraryEntries, libraryPage, orderedLibraryEntries,
  pruneSelection, selectedLibraryEntries, selectTracks, summarizeSongs,
} from "./library-model.mjs";

const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView;
let live, disposed = false, pageGeneration = 0, libraryView = "favorites", selected = "";
let connectionState = "connecting";
let snapshot = null, results = [], library = { favorites: [], history: [] }, maintenance = null;
let statusRequest, queuedStatusRequest, librarySequence = 0, maintenanceSequence = 0, toastTimer, updateTimer, offContext;
let searchState = "idle", libraryState = "loading";
const libraryPages = { favorites: 1, history: 1 };
const librarySelection = { favorites: new Set(), history: new Set() };
const activeBatchSources = new Set();
let visibleLibraryPage = libraryPage([]);
const pending = new Set();
const components = new WeakMap();
const t = (key, fallback) => bridge.t(`views.console.${key}`, fallback);

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

function icon(name) {
  const node = new DOMParser().parseFromString(icons[name], "image/svg+xml").documentElement;
  node.setAttribute("aria-hidden", "true");
  node.setAttribute("focusable", "false");
  return document.importNode(node, true);
}

function disposeComponents(root) {
  for (const button of root.querySelectorAll("button")) components.get(button)?.destroy();
}

function replaceContent(root, ...nodes) {
  const focusKey = root.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
  disposeComponents(root);
  root.replaceChildren(...nodes);
  if (focusKey) [...root.querySelectorAll("[data-focus-key]")].find((node) => node.dataset.focusKey === focusKey)?.focus({ preventScroll: true });
}

function upgradeButtons() {
  for (const original of document.querySelectorAll("button:not(.tabs button):not(.segmented button)")) {
    const content = element("span", "command-content");
    content.append(...original.childNodes);
    const component = original.classList.contains("icon-button")
      ? createIconButton({ icon: "", ariaLabel: original.getAttribute("aria-label") || "", tooltip: false })
      : createButton({ text: content, type: original.type, variant: original.classList.contains("primary") ? "filled" : "text", progress: false });
    const button = component.element;
    for (const attribute of original.attributes) {
      if (attribute.name === "class") button.classList.add(...original.classList);
      else button.setAttribute(attribute.name, attribute.value);
    }
    if (original.classList.contains("icon-button")) button.append(content);
    components.set(button, component);
    original.replaceWith(button);
  }
}

function iconButton(name, label, onClick, disabled = false, operation = "") {
  const busy = pending.has(operation);
  const component = createIconButton({ icon: icons[name], ariaLabel: label, disabled: disabled || busy, tooltip: false });
  const button = component.element;
  button.classList.add("icon-button");
  components.set(button, component);
  button.title = label;
  button.dataset.tooltip = label;
  button.setAttribute("aria-label", label);
  button.disabled = disabled || busy;
  button.dataset.blocked = String(disabled);
  button.dataset.busy = String(busy);
  button.setAttribute("aria-busy", String(busy));
  if (operation) button.dataset.operation = operation;
  button.addEventListener("click", () => run(button, onClick));
  return button;
}

function artwork(song) {
  const url = song.thumbnail_url || "";
  let parsed;
  try { parsed = new URL(url); } catch { parsed = null; }
  if (!parsed || parsed.protocol !== "https:" || !["i.ytimg.com", "img.youtube.com", "yt3.ggpht.com"].includes(parsed.hostname)) {
    const placeholder = element("div", "artwork art-placeholder");
    placeholder.append(icon("Music2"));
    return placeholder;
  }
  const image = element("img", "artwork");
  image.src = url;
  image.alt = "";
  image.loading = "lazy";
  image.referrerPolicy = "no-referrer";
  image.addEventListener("error", () => {
    const replacement = element("div", "artwork art-placeholder");
    replacement.append(icon("Music2"));
    image.replaceWith(replacement);
  }, { once: true });
  return image;
}

function duration(value) {
  if (!Number.isFinite(value)) return t("unknownDuration", "时长未知");
  const seconds = Math.floor(value);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function details(song, columnDuration = false) {
  const node = element("div", "track-details");
  const subtitle = element("div", "track-subtitle", song.artist);
  subtitle.append(element("span", columnDuration ? "inline-duration" : "", ` · ${duration(song.duration)}`));
  node.append(element("div", "track-title", song.title), subtitle);
  return node;
}

function empty(list, message, symbol = "Music2") {
  const row = element("li", "empty-state");
  row.append(icon(symbol), element("span", "", message));
  replaceContent(list, row);
}

function showError(error) {
  if (disposed) return;
  $("error-text").textContent = String(error?.message || error || t("requestFailed", "请求未完成"));
  $("error").hidden = false;
}

function toast(message) {
  if (disposed) return;
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3000);
}

async function run(button, action) {
  const operation = button.dataset.operation || button.id;
  if (disposed || pending.has(operation) || button.dataset.busy === "true") return;
  pending.add(operation);
  button.dataset.busy = "true";
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  renderAvailability();
  $("error").hidden = true;
  try { await action(); } catch (error) { showError(error); }
  finally {
    pending.delete(operation);
    if (!disposed) {
      button.dataset.busy = "false";
      button.removeAttribute("aria-busy");
      button.disabled = false;
      drawResults();
      drawLibrary();
      drawQueue();
      renderAvailability();
    }
  }
}

function confirmAction(title, detail = "", tracks = []) {
  if ($("confirm-dialog").open || disposed) return Promise.resolve(false);
  $("confirm-title").textContent = title;
  $("confirm-detail").textContent = detail;
  $("confirm-detail").hidden = !detail;
  $("confirm-tracks").replaceChildren(...tracks.map(({ song, skipped }) =>
    element("li", "", `${song.title} · ${duration(song.duration)}${skipped ? ` · ${t("alreadyQueued", "已在队列")}` : ""}`)));
  $("confirm-tracks").hidden = !tracks.length;
  const dialog = $("confirm-dialog");
  dialog.returnValue = "";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
    dialog.showModal();
  });
}

async function refreshStatus() {
  if (disposed) return;
  if (statusRequest) {
    // A notification during a read must also fetch the state after that read.
    queuedStatusRequest ??= statusRequest.catch(() => {}).then(() => {
      queuedStatusRequest = null;
      return refreshStatus();
    });
    return queuedStatusRequest;
  }
  statusRequest = fetchStatus().finally(() => { statusRequest = null; });
  return statusRequest;
}

async function fetchStatus() {
  const generation = pageGeneration;
  const result = await bridge.apiGet("status");
  if (disposed || generation !== pageGeneration) return;
  const previousSnapshot = snapshot;
  const previousSelected = selected;
  const wasUpdating = ["waiting", "running"].includes(snapshot?.dependency_update.status);
  snapshot = result;
  if (!snapshot.sessions.some((session) => session.key === selected)) selected = snapshot.sessions[0]?.key || "";
  const options = snapshot.sessions.map((session) => {
    const option = element("option", "", `${session.label} (${session.count})`);
    option.value = session.key;
    return option;
  });
  if (!options.length) {
    const option = element("option", "", t("noSessions", "暂无会话"));
    option.value = "";
    options.push(option);
  }
  const existingOptions = [...$("session").options];
  if (options.length !== existingOptions.length || options.some((option, index) => option.value !== existingOptions[index].value || option.textContent !== existingOptions[index].textContent)) $("session").replaceChildren(...options);
  $("session").value = selected;
  $("session").disabled = !selected;
  $("duration-limit").textContent = `${t("durationLimit", "时长上限")} ${snapshot.limits.duration}s`;
  $("active-jobs").textContent = String(snapshot.active_jobs ?? 0);
  const previousLabel = previousSnapshot?.sessions.find((session) => session.key === previousSelected)?.label;
  const nextLabel = snapshot.sessions.find((session) => session.key === selected)?.label;
  if (!previousSnapshot || previousSelected !== selected || previousLabel !== nextLabel || JSON.stringify(previousSnapshot.queues[selected]) !== JSON.stringify(snapshot.queues[selected])) drawQueue();
  const previousQueuedIds = previousSnapshot?.queues[selected]?.items.map((item) => item.song.video_id);
  const nextQueuedIds = snapshot.queues[selected]?.items.map((item) => item.song.video_id);
  if (previousSelected !== selected) drawResults();
  if (previousSelected !== selected || JSON.stringify(previousQueuedIds) !== JSON.stringify(nextQueuedIds)) drawLibrary();
  drawUpdate();
  renderAvailability();
  if (wasUpdating && !["waiting", "running"].includes(result.dependency_update.status)) loadMaintenance().catch(showError);
}

function renderAvailability() {
  if (disposed) return;
  const queue = snapshot?.queues[selected];
  const hasItems = Boolean(queue?.items.length);
  for (const id of ["resume", "pause", "skip", "clear-queue", "forget-session"]) {
    $(id).disabled = !selected;
    $(id).dataset.operation = `queue:${selected}`;
  }
  $("resume").disabled ||= !hasItems || Boolean(queue?.current);
  $("pause").disabled ||= !queue?.current && queue?.paused;
  $("skip").disabled ||= !hasItems && !queue?.current;
  $("clear-queue").disabled ||= !hasItems && !queue?.current;
  $("forget-session").disabled ||= hasItems || Boolean(queue?.current);
  $("forget-session").hidden = hasItems || Boolean(queue?.current) || !selected;
  const updating = ["waiting", "running"].includes(snapshot?.dependency_update.status);
  $("update-dependencies").disabled = updating || $("update-dependencies").dataset.busy === "true" || !maintenance || maintenance.dependencies.updater_supported === false || Boolean(snapshot?.dependency_update.recovery_required);
  $("component").disabled = updating;
  $("health-refresh").disabled = updating || $("health-refresh").dataset.busy === "true";
  $("cancel-update").disabled = !updating || $("cancel-update").dataset.busy === "true";
  $("clear-history").disabled = activeBatchSources.has("history");
  renderLibraryAvailability();
  for (const button of document.querySelectorAll("button[data-operation]")) {
    const busy = pending.has(button.dataset.operation);
    button.dataset.busy = String(busy);
    button.setAttribute("aria-busy", String(busy));
    button.disabled = busy || (button.dataset.blocked === undefined ? button.disabled : button.dataset.blocked === "true");
  }
  for (const button of document.querySelectorAll("button")) {
    const component = components.get(button);
    if (component) button.disabled ? component.disable() : component.enable();
  }
}

async function enqueue(target, song, index, source) {
  if (!target) throw new Error(t("noSessions", "暂无会话"));
  const position = await bridge.apiPost("queue/add", { session: target, index, source, video_id: song.video_id });
  const label = snapshot?.sessions.find((session) => session.key === target)?.label || target;
  toast(`${label} · ${t("queued", "已加入队列")} #${position.position} · ${song.title}`);
  await refreshStatus();
}

function drawResults() {
  const target = selected;
  $("result-count").textContent = results.length ? String(results.length) : "";
  const rows = results.map((song, index) => {
    const row = element("li", "track-row");
    const actions = element("div", "track-actions");
    const favorite = library.favorites.some((item) => item.video_id === song.video_id);
    actions.append(
      iconButton("Plus", t("addToQueue", "加入队列"), () => enqueue(target, song, index + 1, "search"), !target, `queue:${target}`),
      iconButton(favorite ? "Check" : "Heart", t("favorite", "收藏歌曲"), async () => {
        await bridge.apiPost("library/action", { action: "favorite", index: index + 1, video_id: song.video_id });
        await loadLibrary();
        toast(t("favorited", "已收藏"));
      }, favorite, "library"),
    );
    row.dataset.id = song.video_id;
    actions.querySelectorAll("button").forEach((button, action) => { button.dataset.focusKey = `${song.video_id}:${action}`; });
    const info = details(song, true);
    row.append(artwork(song), info, element("span", "track-duration", duration(song.duration)), actions);
    return row;
  });
  replaceContent($("results"), ...rows);
  if (!rows.length) empty($("results"), searchState === "loading" ? t("searching", "搜索中") : searchState === "error" ? t("searchFailed", "搜索失败") : t("noResults", "暂无搜索结果"), "Search");
  $("results").setAttribute("aria-busy", String(searchState === "loading"));
}

function libraryOptions(query = $("library-filter").value) {
  return { query, sort: $("library-sort").value, locale: document.documentElement.lang };
}

function selectionLimit() {
  return Math.min(MAX_BATCH_SELECTION, snapshot?.limits.batch || MAX_BATCH_SELECTION);
}

function librarySelectionBusy() {
  return libraryState === "loading" || pending.has("library") || pending.has(`queue:${selected}`) || activeBatchSources.has(libraryView);
}

function durationSummary(summary) {
  const unknown = summary.unknown_duration_count || 0;
  return `${duration(summary.duration_seconds)}${unknown ? ` · ${unknown} ${t("unknownDurationTracks", "首时长未知")}` : ""}`;
}

function renderLibraryAvailability() {
  const selection = librarySelection[libraryView];
  const busy = librarySelectionBusy();
  const pageIds = visibleLibraryPage.items.map(({ song }) => song.video_id);
  const pageSelected = pageIds.filter((id) => selection.has(id)).length;
  const allPageSelected = Boolean(pageIds.length && pageSelected === pageIds.length);
  $("library-select-page").checked = allPageSelected;
  $("library-select-page").indeterminate = pageSelected > 0 && !allPageSelected;
  $("library-select-page").disabled = busy || !pageIds.length || selection.size >= selectionLimit() && !pageSelected;
  $("library-page-selection-label").textContent = allPageSelected || selection.size >= selectionLimit() && pageSelected
    ? t("deselectPage", "取消本页选择") : t("selectPage", "选择本页");
  for (const input of $("library-list").querySelectorAll('input[type="checkbox"]')) {
    input.disabled = busy || !input.checked && selection.size >= selectionLimit();
  }
  $("library-clear-selection").disabled = busy || !selection.size;
  $("library-batch").dataset.operation = `queue:${selected}`;
  $("library-batch").dataset.blocked = String(!selected || !selection.size || busy);
  $("library-batch").disabled = !selected || !selection.size || busy;
  $("library-previous").disabled = libraryState === "loading" || visibleLibraryPage.page <= 1;
  $("library-next").disabled = libraryState === "loading" || visibleLibraryPage.page >= visibleLibraryPage.pages;
}

function submissionTime(value) {
  if (!Number.isFinite(value) || value <= 0) return "--";
  const date = new Date(value * 1000);
  if (!Number.isFinite(date.getTime())) return "--";
  return new Intl.DateTimeFormat(document.documentElement.lang, {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

async function enqueueLibraryBatch(batch) {
  const { target, source, entries, label, queuedIds, queueSize, queueLimit, generation } = batch;
  if (!target || !entries.length) return;
  activeBatchSources.add(source);
  renderAvailability();
  try {
    const toAdd = entries.filter(({ song }) => !queuedIds.has(song.video_id));
    const skipped = entries.length - toAdd.length;
    const detail = [
      label,
      `${t(source, source === "favorites" ? "收藏" : "历史")} · ${entries.length} ${t("tracks", "首")}`,
      `${t("toAdd", "将加入")} ${toAdd.length} ${t("tracks", "首")} · ${t("skipQueued", "跳过已排队")} ${skipped} ${t("tracks", "首")}`,
      `${t("selectedDuration", "选中时长")} ${durationSummary(summarizeSongs(entries.map(({ song }) => song)))}`,
      `${t("projectedQueue", "预计队列")} ${queueSize + toAdd.length} / ${queueLimit}`,
    ].join("\n");
    const confirmed = await confirmAction(t("confirmBatch", "将选中歌曲加入队列？"), detail,
      entries.map((entry) => ({ ...entry, skipped: queuedIds.has(entry.song.video_id) })));
    if (!confirmed || disposed || generation !== pageGeneration) return;
    const response = await bridge.apiPost("queue/batch", { session: target, source, video_ids: entries.map(({ song }) => song.video_id) });
    if (disposed || generation !== pageGeneration) return;
    for (const { song } of entries) librarySelection[source].delete(song.video_id);
    toast(`${label} · ${t("queued", "已加入队列")} ${response.added} ${t("tracks", "首")}${response.skipped.length ? ` · ${t("skipQueued", "跳过已排队")} ${response.skipped.length} ${t("tracks", "首")}` : ""}`);
    await refreshStatus();
  } finally {
    activeBatchSources.delete(source);
  }
}

function drawLibrary() {
  const target = selected, source = libraryView;
  $("library-heading").textContent = libraryView === "favorites" ? t("favorites", "收藏") : t("history", "历史");
  $("clear-history").hidden = libraryView !== "history";
  $("library-sort-plays").hidden = source !== "history";
  $("library-sort-plays").disabled = source !== "history";
  if (source !== "history" && $("library-sort").value === "plays") $("library-sort").value = "recent";
  $("library-sort-recent").textContent = source === "history" ? t("recentSubmission", "最近提交") : t("recent", "最近添加");
  const allEntries = libraryEntries(library, source);
  librarySelection[source] = pruneSelection(librarySelection[source], allEntries);
  const selection = librarySelection[source];
  const query = $("library-filter").value.trim().toLocaleLowerCase(document.documentElement.lang);
  const entries = orderedLibraryEntries(library, source, libraryOptions());
  visibleLibraryPage = libraryPage(entries, libraryPages[source]);
  libraryPages[source] = visibleLibraryPage.page;
  $("library-count").textContent = `${entries.length} / ${allEntries.length}`;
  const selectedEntries = selectedLibraryEntries(library, source, selection, libraryOptions());
  $("library-selection-summary").textContent = `${t(source, source === "favorites" ? "收藏" : "历史")} · ${t("selected", "已选")} ${selection.size} / ${selectionLimit()} · ${durationSummary(summarizeSongs(selectedEntries.map(({ song }) => song)))}`;
  $("library-page-label").textContent = `${visibleLibraryPage.page} / ${visibleLibraryPage.pages} · ${visibleLibraryPage.start}–${visibleLibraryPage.end} / ${visibleLibraryPage.total}`;
  $("library-pagination").hidden = visibleLibraryPage.pages <= 1;
  const queuedIds = new Set(snapshot?.queues[target]?.items.map((item) => item.song.video_id) || []);
  const rows = visibleLibraryPage.items.map((entry) => {
    const { song } = entry;
    const row = element("li", "track-row library-row");
    row.dataset.id = song.video_id;
    const selector = element("label", "track-selector");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.checked = selection.has(song.video_id);
    checkbox.setAttribute("aria-label", `${t("selectTrack", "选择歌曲")} · ${song.title}`);
    checkbox.dataset.focusKey = `${song.video_id}:selection`;
    checkbox.addEventListener("change", () => {
      librarySelection[source] = selectTracks(librarySelection[source], [song.video_id], checkbox.checked, selectionLimit());
      drawLibrary();
      renderAvailability();
    });
    selector.append(checkbox);
    const actions = element("div", "track-actions");
    actions.append(iconButton("Plus", t("addToQueue", "加入队列"), () => enqueue(target, song, undefined, source), !target, `queue:${target}`));
    if (libraryView === "favorites") actions.append(iconButton("Trash2", t("removeFavorite", "移除收藏"), async () => {
      await bridge.apiPost("library/action", { action: "remove", video_id: song.video_id });
      await loadLibrary();
      toast(t("removedFavorite", "已移除收藏"));
    }, activeBatchSources.has(source), "library"));
    else {
      const favorite = library.favorites.some((item) => item.video_id === song.video_id);
      actions.append(iconButton(favorite ? "Check" : "Heart", t("favorite", "收藏歌曲"), async () => {
        await bridge.apiPost("library/action", { action: "favorite", source: "history", video_id: song.video_id });
        await loadLibrary();
        toast(`${t("favorited", "已收藏")} · ${song.title}`);
      }, favorite || activeBatchSources.has(source), "library"));
    }
    actions.querySelectorAll("button").forEach((button, action) => { button.dataset.focusKey = `${song.video_id}:${action}`; });
    const info = details(song, true);
    if (source === "history") {
      const metadata = element("div", "history-metadata");
      metadata.append(
        element("span", "", `${t("submissionCount", "提交次数")} ${Number.isFinite(entry.play_count) ? entry.play_count : "--"}`),
        element("span", "", `${t("lastSubmission", "最近提交")} ${submissionTime(entry.played_at)}`),
      );
      info.append(metadata);
    }
    if (queuedIds.has(song.video_id)) info.append(element("div", "track-queued-label", t("alreadyQueued", "已在队列")));
    row.append(selector, artwork(song), info, element("span", "track-duration", duration(song.duration)), actions);
    return row;
  });
  replaceContent($("library-list"), ...rows);
  if (!rows.length) empty($("library-list"), libraryState === "loading" ? t("loading", "加载中") : libraryState === "error" ? t("loadFailed", "加载失败") : query ? t("noMatches", "无匹配歌曲") : libraryView === "favorites" ? t("noFavorites", "暂无收藏") : t("noHistory", "暂无语音提交记录"), libraryView === "history" ? "History" : "Heart");
  $("library-list").setAttribute("aria-busy", String(libraryState === "loading"));
  renderLibraryAvailability();
}

async function loadLibrary() {
  const sequence = ++librarySequence;
  libraryState = "loading";
  drawLibrary();
  try {
    const result = await bridge.apiGet("library");
    if (disposed || sequence !== librarySequence) return;
    library = result;
    for (const source of ["favorites", "history"]) librarySelection[source] = pruneSelection(librarySelection[source], libraryEntries(library, source));
    libraryState = "ready";
  } catch (error) {
    if (disposed || sequence !== librarySequence) return;
    libraryState = "error";
    throw error;
  } finally {
    if (!disposed && sequence === librarySequence) { drawLibrary(); drawResults(); renderAvailability(); }
  }
}

async function queueAction(target, action, extra = {}) {
  await bridge.apiPost("queue/action", { session: target, action, ...extra });
  await refreshStatus();
  toast(t("actionComplete", "操作完成"));
}

function drawQueue() {
  const target = selected;
  const queue = snapshot?.queues[selected];
  const items = queue?.items || [];
  $("queue-count").textContent = String(items.length);
  $("queue-total").textContent = `${items.length} / ${snapshot?.limits.queue || 0}`;
  $("queue-duration").textContent = `${t("pendingDuration", "待播总时长")} ${durationSummary(queue?.summary || summarizeSongs(items.map((item) => item.song)))}`;
  $("queue-state").textContent = queue?.paused ? t("paused", "已暂停") : queue?.current ? t("active", "运行中") : t("idle", "待启动");
  $("queue-state").dataset.active = String(Boolean(queue?.current && !queue?.paused));
  $("queue-session").textContent = snapshot?.sessions.find((session) => session.key === target)?.label || t("noSessions", "暂无会话");
  $("queue-error").hidden = !queue?.error;
  $("queue-error").textContent = queue?.error || "";
  const current = queue?.current;
  $("now-playing").hidden = !current;
  $("queue-idle").hidden = Boolean(current);
  if (current) {
    const info = details(current.row.song);
    const phases = new Map([
      ["等待处理", ["phaseProcessingWait", "等待处理"]], ["等待歌曲缓存", ["phaseCacheWait", "等待歌曲缓存"]],
      ["读取视频信息", ["phaseMetadata", "读取视频信息"]], ["校验已缓存音频", ["phaseCacheCheck", "校验已缓存音频"]],
      ["下载音轨", ["downloadAudio", "下载音轨"]], ["检查源音频", ["phaseSourceCheck", "检查源音频"]],
      ["转换为 WAV", ["convertWav", "转换为 WAV"]], ["验证 WAV 编码与时长", ["phaseWavCheck", "验证 WAV 编码与时长"]],
      ["写入歌曲缓存", ["phaseCacheWrite", "写入歌曲缓存"]], ["提交消息", ["phaseMessageSend", "提交消息"]],
      ["已提交语音", ["phaseVoiceSubmitted", "已提交语音"]], ["已提交链接", ["phaseLinkSubmitted", "已提交链接"]],
    ]);
    const phase = phases.get(current.phase);
    info.prepend(element("div", "phase", phase ? t(...phase) : current.phase));
    replaceContent($("now-playing"), artwork(current.row.song), info);
  }
  const rows = items.map((item, index) => {
    const active = current && !current.submitted && item.id === current.row.id;
    const row = element("li", "queue-row");
    row.dataset.active = String(Boolean(active));
    row.dataset.id = item.id;
    const info = details(item.song);
    info.append(element("div", "track-subtitle", item.requested_by));
    const actions = element("div", "track-actions");
    const operation = `queue:${target}`;
    const remove = iconButton("Trash2", t("removeTrack", "移除歌曲"), () => queueAction(target, "remove", { entry_id: item.id }), Boolean(active), operation);
    remove.classList.add("remove");
    actions.append(
      iconButton("ArrowUp", t("moveUp", "上移"), () => queueAction(target, "move", { entry_id: item.id, direction: "up" }), Boolean(active) || index === 0 || Boolean(current && !current.submitted && index === 1), operation),
      iconButton("ArrowDown", t("moveDown", "下移"), () => queueAction(target, "move", { entry_id: item.id, direction: "down" }), Boolean(active) || index === items.length - 1, operation),
      remove,
    );
    actions.querySelectorAll("button").forEach((button, action) => { button.dataset.focusKey = `${item.id}:${action}`; });
    row.append(element("span", "queue-number", String(index + 1)), artwork(item.song), info, actions);
    return row;
  });
  replaceContent($("queue-list"), ...rows);
  if (!rows.length) empty($("queue-list"), t("emptyQueue", "暂无待播歌曲"), "ListMusic");
}

function drawUpdate() {
  const update = snapshot?.dependency_update;
  $("update-state").hidden = !update || update.status === "idle";
  if (!update) return;
  const phases = new Map([
    ["空闲", ["phaseIdle", "空闲"]], ["等待维护权限", ["phaseWaiting", "等待维护权限"]],
    ["已取消", ["phaseCancelled", "已取消"]], ["超时", ["phaseTimeout", "超时"]],
    ["失败", ["phaseFailed", "失败"]], ["准备下载", ["phasePreparing", "准备下载"]],
    ["验证暂存程序", ["phaseStaged", "验证暂存程序"]], ["备份并安装", ["phaseInstalling", "备份并安装"]],
    ["验证安装", ["phaseInstalled", "验证安装"]], ["完成", ["phaseComplete", "完成"]], ["回滚", ["phaseRollback", "回滚"]],
  ]);
  const phase = phases.get(update.phase);
  let phaseText = phase ? t(...phase) : update.phase;
  if (update.phase?.startsWith("请求：")) phaseText = `${t("phaseRequesting", "请求")} · ${update.phase.slice(3)}`;
  if (update.phase?.startsWith("下载：")) phaseText = `${t("phaseDownloading", "下载")} · ${update.phase.slice(3)}`;
  const release = /^读取 (.+) 官方版本$/.exec(update.phase || "");
  if (release) phaseText = `${t("phaseRelease", "读取官方版本")} · ${release[1]}`;
  $("update-phase").textContent = phaseText;
  $("update-message").textContent = update.message;
  $("update-state").dataset.status = update.status;
  const updating = ["waiting", "running"].includes(update.status);
  $("update-progress").hidden = !updating;
  if (Number.isFinite(update.progress)) $("update-progress").value = update.progress;
  else $("update-progress").removeAttribute("value");
  const bytes = (value) => `${(Math.max(0, value || 0) / 1048576).toFixed(1)} MiB`;
  const metrics = [];
  if (update.downloaded_bytes > 0) {
    metrics.push(`${bytes(update.downloaded_bytes)}${update.total_bytes ? ` / ${bytes(update.total_bytes)}` : ""}`);
    if (updating && update.speed_bytes_per_second > 0) metrics.push(`${bytes(update.speed_bytes_per_second)}/s`);
  }
  if (update.started_at) metrics.push(`${Math.max(0, Math.floor((update.finished_at || Date.now() / 1000) - update.started_at))}s`);
  $("update-metrics").textContent = metrics.join(" · ");
  $("update-versions").textContent = Object.entries(update.updated || {}).map(([name, version]) => `${name} ${version}`).join(" · ");
  $("recovery-message").hidden = !update.recovery_required;
  $("recovery-message").textContent = update.recovery_required ? `${t("recoveryRequired", "需恢复原始备份")} · ${update.recovery_directory || ""} · ${(update.recovery_targets || []).join(", ")}` : "";
}

async function loadMaintenance() {
  const sequence = ++maintenanceSequence;
  $("dependencies").setAttribute("aria-busy", "true");
  $("maintenance-notice").textContent = t("checking", "检查中");
  $("maintenance-notice").hidden = false;
  $("runtime-health").textContent = t("checking", "检查中");
  $("runtime-health").dataset.state = "checking";
  let result;
  try { result = await bridge.apiGet("maintenance"); }
  catch (error) {
    if (disposed || sequence !== maintenanceSequence) return;
    maintenance = null;
    $("runtime-health").textContent = t("checkFailed", "检查失败");
    $("runtime-health").dataset.state = "error";
    $("maintenance-notice").textContent = t("checkFailed", "检查失败");
    $("dependencies").setAttribute("aria-busy", "false");
    renderAvailability();
    throw error;
  }
  if (disposed || sequence !== maintenanceSequence) return;
  maintenance = result;
  $("dependencies").setAttribute("aria-busy", "false");
  $("maintenance-notice").hidden = true;
  const { dependencies, cache } = maintenance;
  $("platform").textContent = `${dependencies.platform} · ${dependencies.architecture}`;
  $("dependencies").replaceChildren(...dependencies.dependencies.map((dependency) => {
    const row = element("li", "dependency-row");
    const health = element("span", "health", dependency.status === "ok" ? t("ready", "就绪") : t("notReady", "未就绪"));
    health.dataset.ok = String(dependency.status === "ok");
    const symbol = element("span", "dependency-icon");
    symbol.append(icon(dependency.status === "ok" ? "Check" : "CircleAlert"));
    row.append(symbol, element("strong", "", dependency.name), element("span", "version", dependency.version || dependency.status), health);
    return row;
  }));
  $("cache-summary").textContent = `${cache.items} ${t("tracks", "首")} · ${(cache.bytes / 1048576).toFixed(1)} MiB`;
  $("runtime-health").textContent = dependencies.dependencies.every((dependency) => dependency.status === "ok") ? t("allReady", "全部就绪") : t("needsAttention", "需检查");
  $("runtime-health").dataset.state = dependencies.dependencies.every((dependency) => dependency.status === "ok") ? "ready" : "attention";
  $("updater-reason").hidden = dependencies.updater_supported;
  $("updater-reason").textContent = dependencies.updater_supported ? "" : dependencies.updater_reason;
  renderAvailability();
}

function setView(next) {
  document.querySelector(".workspace").dataset.view = next;
  for (const name of ["search", "library", "maintenance"]) $(`${name}-view`).hidden = name !== next;
  for (const button of document.querySelectorAll("[data-view]")) {
    if (button.tagName !== "BUTTON") continue;
    if (button.dataset.view === next) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  if (next === "library") loadLibrary().catch(showError);
  if (next === "maintenance" && !["waiting", "running"].includes(snapshot?.dependency_update.status)) loadMaintenance().catch(showError);
  const heading = next === "queue" ? "queue-heading" : next === "library" ? "library-heading-page" : `${next}-heading`;
  document.querySelector(".skip-link").setAttribute("href", `#${heading}`);
  $(heading).focus({ preventScroll: true });
  window.scrollTo({ top: 0, left: 0, behavior: "instant" });
}

function drawConnection() {
  $("connection").dataset.state = connectionState;
  $("connection").textContent = { live: t("connected", "已连接"), connecting: t("connecting", "连接中"), retrying: t("reconnecting", "重连中"), polling: t("polling", "定时同步") }[connectionState];
}

function applyContext(context) {
  document.documentElement.lang = context.locale || "zh-CN";
  document.title = t("title", "YouTube 点歌");
  $("preview-badge").hidden = context.preview !== true;
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.dataset.defaultText ??= node.textContent;
    const text = t(node.dataset.i18n, node.dataset.defaultText);
    const component = components.get(node);
    if (component?.setText) component.setText(text);
    else node.textContent = text;
  }
  for (const node of document.querySelectorAll("[data-i18n-label]")) {
    node.dataset.defaultLabel ??= node.getAttribute("aria-label");
    const label = t(node.dataset.i18nLabel, node.dataset.defaultLabel);
    node.setAttribute("aria-label", label);
    if (node.tagName === "BUTTON") { node.title = label; node.dataset.tooltip = label; }
  }
  $("query").placeholder = t("query", "歌曲、艺人或 YouTube 视频链接");
  $("library-filter").placeholder = t("filterLibrary", "筛选歌曲或艺人");
  drawConnection();
  drawResults();
  drawLibrary();
  drawQueue();
  drawUpdate();
  renderAvailability();
  if (snapshot) $("duration-limit").textContent = `${t("durationLimit", "时长上限")} ${snapshot.limits.duration}s`;
  if (snapshot && !snapshot.sessions.length) $("session").options[0].textContent = t("noSessions", "暂无会话");
  if (maintenance && document.querySelector(".workspace").dataset.view === "maintenance") loadMaintenance().catch(showError);
}

async function start() {
  upgradeButtons();
  for (const node of document.querySelectorAll("[data-icon]")) node.replaceChildren(icon(node.dataset.icon));
  $("dismiss-error").addEventListener("click", () => { $("error").hidden = true; });
  if (!bridge) throw new Error("当前页面未连接 AstrBot。");
  const context = await bridge.ready();
  if (disposed) return;
  applyContext(context);
  offContext = bridge.onContext(applyContext);
  $("session").addEventListener("change", () => { selected = $("session").value; drawQueue(); drawResults(); drawLibrary(); renderAvailability(); });
  $("library-filter").addEventListener("input", () => { libraryPages.favorites = libraryPages.history = 1; drawLibrary(); renderAvailability(); });
  $("library-sort").addEventListener("change", () => { libraryPages.favorites = libraryPages.history = 1; drawLibrary(); renderAvailability(); });
  $("library-select-page").addEventListener("change", () => {
    const ids = visibleLibraryPage.items.map(({ song }) => song.video_id);
    const previous = librarySelection[libraryView];
    const clearCappedPage = previous.size >= selectionLimit() && ids.some((id) => previous.has(id));
    const checked = !clearCappedPage && $("library-select-page").checked;
    librarySelection[libraryView] = selectTracks(previous, ids, checked, selectionLimit());
    if (checked && ids.some((id) => !librarySelection[libraryView].has(id))) toast(`${t("selectionLimit", "最多选择")} ${selectionLimit()} ${t("tracks", "首")}`);
    drawLibrary();
    renderAvailability();
  });
  $("library-clear-selection").addEventListener("click", () => {
    librarySelection[libraryView].clear();
    drawLibrary();
    renderAvailability();
  });
  for (const [id, direction] of [["library-previous", -1], ["library-next", 1]]) $(id).addEventListener("click", () => {
    libraryPages[libraryView] += direction;
    drawLibrary();
    renderAvailability();
    $("library-list").scrollIntoView({ block: "start", behavior: "instant" });
  });
  $("library-batch").addEventListener("click", () => {
    const target = selected, source = libraryView;
    const queue = snapshot?.queues[target];
    const batch = {
      target, source, entries: selectedLibraryEntries(library, source, librarySelection[source], libraryOptions()),
      label: snapshot?.sessions.find((session) => session.key === target)?.label || target,
      queuedIds: new Set(queue?.items.map((item) => item.song.video_id) || []),
      queueSize: queue?.items.length || 0, queueLimit: snapshot?.limits.queue || 0, generation: pageGeneration,
    };
    run($("library-batch"), () => enqueueLibraryBatch(batch));
  });
  document.querySelectorAll(".tabs button").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  document.querySelectorAll("[data-library]").forEach((button) => button.addEventListener("click", () => {
    libraryView = button.dataset.library;
    document.querySelectorAll("[data-library]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
    drawLibrary();
    renderAvailability();
  }));
  $("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const query = $("query").value.trim();
    if (!query) { $("query").focus(); return; }
    run($("search-button"), async () => {
      results = [];
      searchState = "loading";
      drawResults();
      try {
        const response = await bridge.apiGet("search", { q: query });
        if (disposed) return;
        results = response.songs;
        searchState = "ready";
      } catch (error) { searchState = "error"; throw error; }
      finally { if (!disposed) drawResults(); }
    });
  });
  $("refresh").addEventListener("click", () => run($("refresh"), async () => { await refreshStatus(); if (!disposed) await live.restart(); }));
  $("library-refresh").addEventListener("click", () => run($("library-refresh"), loadLibrary));
  $("health-refresh").addEventListener("click", () => run($("health-refresh"), loadMaintenance));
  for (const [id, action, confirmation] of [
    ["resume", "resume"], ["pause", "pause"], ["skip", "skip"],
    ["clear-queue", "clear", "confirmClear"], ["forget-session", "forget", "confirmForget"],
  ]) $(id).addEventListener("click", () => {
    const target = selected;
    const label = snapshot?.sessions.find((session) => session.key === target)?.label || "";
    const queue = snapshot?.queues[target];
    const resumeError = queue?.error || "";
    const confirmResume = action === "resume" && Boolean(queue?.error || queue?.paused);
    const generation = pageGeneration;
    run($(id), async () => {
      if (confirmResume && !await confirmAction(t("confirmResume", "继续处理此会话队列？"),
        [label, resumeError, t("resumeUncertain", "请先核对目标会话与队列；若上次提交结果未知，继续可能再次发送当前项。")].filter(Boolean).join("\n"))) return;
      if (confirmation && !await confirmAction(t(confirmation, action === "clear" ? "清空当前会话队列？" : "移除当前空会话？"), label)) return;
      if (!disposed && generation === pageGeneration) await queueAction(target, action);
    });
  });
  $("clear-history").dataset.operation = "library";
  $("clear-history").addEventListener("click", () => run($("clear-history"), async () => {
    if (await confirmAction(t("confirmHistory", "清除我的历史记录？"))) {
      await bridge.apiPost("library/action", { action: "clear_history" }); await loadLibrary();
      toast(t("historyCleared", "已清除历史"));
    }
  }));
  $("clean-cache").addEventListener("click", () => run($("clean-cache"), async () => {
    if (await confirmAction(t("confirmCache", "清理未使用的音频缓存？"))) {
      await bridge.apiPost("cache/cleanup", {}); await loadMaintenance();
      toast(t("cacheCleared", "已清理缓存"));
    }
  }));
  $("update-dependencies").addEventListener("click", () => run($("update-dependencies"), async () => {
    const component = $("component").value;
    if (await confirmAction(`${t("confirmUpdate", "确认更新依赖")} · ${component}？`, t("updateDetail", "更新期间，新点歌任务将等待。"))) {
      await bridge.apiPost("dependencies/update", { component, confirmed: true }); await refreshStatus();
    }
  }));
  $("cancel-update").addEventListener("click", () => run($("cancel-update"), async () => { await bridge.apiPost("dependencies/cancel", {}); await refreshStatus(); }));
  live = new LiveConnection(bridge, {
    onSnapshot: () => {
      if (disposed || updateTimer) return;
      updateTimer = setTimeout(() => { updateTimer = null; refreshStatus().catch(showError); }, 100);
    },
    onPoll: async () => { try { await refreshStatus(); } catch (error) { showError(error); } },
    onState: (state) => {
      connectionState = state;
      drawConnection();
    },
  });
  const initial = await Promise.allSettled([refreshStatus(), loadLibrary()]);
  if (disposed) return;
  for (const result of initial) if (result.status === "rejected") showError(result.reason);
  await live.start();
}

window.addEventListener("pagehide", (event) => {
  if (disposed) return;
  pageGeneration += 1;
  librarySequence += 1;
  maintenanceSequence += 1;
  clearTimeout(toastTimer);
  $("toast").hidden = true;
  clearTimeout(updateTimer);
  updateTimer = null;
  live?.stop();
  if ($("confirm-dialog").open) $("confirm-dialog").close("cancel");
  if (event.persisted) return;
  disposed = true;
  offContext?.();
  disposeComponents(document);
});

window.addEventListener("pageshow", (event) => {
  if (!event.persisted || disposed) return;
  pageGeneration += 1;
  refreshStatus().catch(showError);
  loadLibrary().catch(showError);
  if (document.querySelector(".workspace").dataset.view === "maintenance" && !["waiting", "running"].includes(snapshot?.dependency_update.status)) loadMaintenance().catch(showError);
  live?.restart().catch(showError);
});

start().catch(showError);
