import { test, expect } from "@playwright/test";
import fs from "node:fs/promises";
import { musicThumbnail } from "../fixtures/music-thumbnail.mjs";

const screenshots = ".verification/m3/screenshots";

test.beforeEach(async ({ page }) => {
  await fs.mkdir(screenshots, { recursive: true });
  await page.route("https://i.ytimg.com/**", (route) => route.fulfill({ body: musicThumbnail(), contentType: "image/png" }));
  await page.addInitScript(() => {
    if (location.search === "?initial-error") {
      Object.defineProperty(window, "AstrBotPluginView", {
        configurable: true,
        set(bridge) {
          Object.defineProperty(window, "AstrBotPluginView", { value: bridge, configurable: true });
          window.mockControl.failNext = "模拟首次状态请求失败";
        },
      });
    }
  });
  await page.goto("/preview/");
  await expect(page.locator("#connection")).toHaveText("已连接");
  await expect(page.locator("#preview-badge")).toBeVisible();
});

async function search(page) {
  await page.locator("#query").fill("音乐");
  await page.locator("#search-button").click();
  await expect(page.locator("#results .track-row")).toHaveCount(3);
}

async function queueView(page) { await page.locator('.tabs [data-view="queue"]').click(); }

async function populateLibrary(page, count = 45) {
  await page.evaluate((count) => {
    const control = window.mockControl;
    control.library.favorites = Array.from({ length: count }, (_, index) => ({
      ...control.songs[0], video_id: `track${String(index).padStart(6, "0")}`,
      title: `Track ${String(index).padStart(2, "0")}`, artist: "Batch Artist", duration: 120 + index % 30,
    }));
    control.library.history = control.library.favorites.map((song, index) => ({
      song, played_at: 1700000000 + index * 3600, play_count: index % 8 + 1,
    }));
  }, count);
  await page.locator('.tabs [data-view="library"]').click();
  await expect(page.locator("#library-list .track-row")).toHaveCount(Math.min(count, 20));
}

test("search, favorite and known-session queue submission", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await search(page);
  await page.locator("#session").selectOption("session-b");
  await page.locator("#results .track-row").first().getByRole("button", { name: "加入队列", exact: true }).click();
  await expect(page.locator("#toast")).toContainText("已加入队列");
  await page.locator("#results .track-row").first().getByRole("button", { name: "收藏歌曲" }).click();
  await page.locator('.tabs [data-view="library"]').click();
  await expect(page.locator("#library-list .track-row")).toHaveCount(1);
  await queueView(page);
  await expect(page.locator("#queue-list .queue-row")).toHaveCount(1);
  expect(errors).toEqual([]);
});

test("queue reorder, exact removal and confirmed clearing", async ({ page }) => {
  await queueView(page);
  await page.locator("#queue-list .queue-row").nth(1).getByRole("button", { name: "上移", exact: true }).click();
  await expect(page.locator("#queue-list .track-title").first()).toHaveText("示例曲目 Beta");
  await page.locator("#queue-list .queue-row").first().getByRole("button", { name: "移除歌曲", exact: true }).click();
  await expect(page.locator("#queue-list .queue-row")).toHaveCount(1);
  await page.locator("#clear-queue").click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.locator("#queue-list .queue-row")).toHaveCount(1);
  await page.locator("#clear-queue").click();
  await page.getByRole("dialog").getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.locator("#queue-list .empty-state")).toBeVisible();
});

test("dependency update confirmation and cancellation", async ({ page }) => {
  await page.locator('.tabs [data-view="maintenance"]').click();
  await expect(page.locator("#dependencies .dependency-row")).toHaveCount(4);
  await page.locator("#update-dependencies").click();
  await page.getByRole("dialog").getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.locator("#update-phase")).toHaveText("模拟下载");
  await expect(page.locator("#update-dependencies")).toBeDisabled();
  await page.locator("#cancel-update").click();
  await expect(page.locator("#update-phase")).toHaveText("已取消");
});

test("errors recover and SSE reconnect does not duplicate subscriptions", async ({ page }) => {
  await page.evaluate(() => { window.mockControl.failNext = "模拟网络故障"; });
  await page.locator("#query").fill("音乐");
  await page.locator("#search-button").click();
  await expect(page.locator("#error")).toContainText("模拟网络故障");
  await search(page);
  await expect(page.locator("#error")).toBeHidden();
  await page.evaluate(() => window.mockControl.disconnect());
  await expect(page.locator("#connection")).toHaveText("重连中");
  await expect(page.locator("#connection")).toHaveText("已连接", { timeout: 5000 });
  expect(await page.evaluate(() => window.mockControl.subscribers.size)).toBe(1);
});

test("manual refresh recovers an initial status request failure", async ({ page }) => {
  await page.goto("/preview/?initial-error");
  await expect(page.locator("#error")).toContainText("模拟首次状态请求失败");
  await expect(page.locator("#connection")).toHaveText("已连接");
  expect(await page.evaluate(() => window.mockControl.subscribers.size)).toBe(1);
  await page.locator("#refresh").click();
  await expect(page.locator("#error")).toBeHidden();
  await expect(page.locator("#connection")).toHaveText("已连接");
  await expect(page.locator("#session option")).toHaveCount(2);
  expect(await page.evaluate(() => window.mockControl.subscribers.size)).toBe(1);
});

test("confirmation retains the rendered session across a session change", async ({ page }) => {
  await queueView(page);
  await page.locator("#clear-queue").click();
  await expect(page.locator("#confirm-detail")).toContainText("QQ 群 123456");
  await page.evaluate(() => {
    const select = document.getElementById("session");
    select.value = "session-b";
    select.dispatchEvent(new Event("change"));
  });
  await page.getByRole("dialog").getByRole("button", { name: "确认", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((r) => r.endpoint === "queue/action").at(-1)?.body.session)).toBe("session-a");
  expect(await page.evaluate(() => window.mockControl.fixtures.queues["session-a"].items.length)).toBe(0);
});

test("queue busy state survives an SSE redraw and prevents duplicate submissions", async ({ page }) => {
  await queueView(page);
  await page.evaluate(() => window.mockControl.hold("queue/action"));
  await page.locator("#queue-list .queue-row").nth(1).getByRole("button", { name: "上移", exact: true }).click();
  await page.evaluate(() => window.mockControl.notify());
  await expect(page.locator("#queue-list .queue-row").nth(1).getByRole("button", { name: "上移", exact: true })).toBeDisabled();
  await expect(page.locator("#skip")).toBeDisabled();
  expect(await page.evaluate(() => window.mockControl.requests.filter((r) => r.endpoint === "queue/action").length)).toBe(1);
  await page.evaluate(() => window.mockControl.release("queue/action"));
  await expect(page.locator("#queue-list .track-title").first()).toHaveText("示例曲目 Beta");
  await expect(page.locator("#skip")).toBeEnabled();
});

test("an SSE notification during an older status read queues a fresh snapshot", async ({ page }) => {
  await queueView(page);
  const before = await page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "status").length);
  await page.evaluate(() => {
    window.mockControl.captureStatus = true;
    window.mockControl.hold("status");
    window.mockControl.notify();
  });
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "status").length)).toBeGreaterThan(before);
  await page.evaluate(() => {
    const control = window.mockControl;
    control.fixtures.queues["session-a"].items = [{ id: "new-entry", song: control.songs[2], requested_by: "Updated user" }];
    control.notify();
  });
  await page.waitForTimeout(150);
  await page.evaluate(() => window.mockControl.release("status"));
  await expect(page.locator("#queue-list .track-title")).toHaveText("示例曲目 Gamma");
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "status").length)).toBeGreaterThan(before + 1);
});

test("unrelated progress preserves session options and queue controls", async ({ page }) => {
  await queueView(page);
  const before = await page.evaluate(() => {
    window.originalQueueButton = document.querySelector("#queue-list button");
    window.originalSessionOption = document.querySelector("#session option");
    return window.mockControl.requests.filter((request) => request.endpoint === "status").length;
  });
  await page.evaluate(() => {
    window.mockControl.fixtures.dependency_update = { status: "running", phase: "下载：Deno", progress: 25, message: "" };
    window.mockControl.notify();
  });
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "status").length)).toBeGreaterThan(before);
  await expect.poll(() => page.evaluate(() => (
    window.originalQueueButton === document.querySelector("#queue-list button")
    && window.originalSessionOption === document.querySelector("#session option")
  ))).toBe(true);
});

test("a queue mutation waits for a status read newer than the mutation", async ({ page }) => {
  await queueView(page);
  const before = await page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "status").length);
  await page.evaluate(() => {
    window.mockControl.captureStatus = true;
    window.mockControl.hold("status");
  });
  await page.locator("#refresh").click();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "status").length)).toBeGreaterThan(before);
  await page.locator("#queue-list .queue-row").last().getByRole("button", { name: "移除歌曲", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.mockControl.fixtures.queues["session-a"].items.length)).toBe(1);
  await page.waitForTimeout(150);
  await page.evaluate(() => window.mockControl.release("status"));
  await expect(page.locator("#queue-list .queue-row")).toHaveCount(1);
  await expect(page.locator("#toast")).toHaveText("操作完成");
  await expect(page.locator("#skip")).toBeEnabled();
});

test("a cached page resumes with its controls and one live subscription", async ({ page }) => {
  await search(page);
  await page.locator("#results .track-row").first().getByRole("button", { name: "收藏歌曲" }).click();
  await expect(page.locator("#toast")).toHaveText("已收藏");
  await expect(page.locator("#toast")).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true })));
  await expect.poll(() => page.evaluate(() => window.mockControl.subscribers.size)).toBe(0);
  await expect(page.locator("#toast")).toBeHidden();
  await expect(page.locator("#search-button")).toHaveClass(/mtrl-button/);
  await page.evaluate(() => {
    window.mockControl.fixtures.queues["session-a"].items = [];
    window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true }));
  });
  await expect(page.locator("#queue-count")).toHaveText("0");
  await expect.poll(() => page.evaluate(() => window.mockControl.subscribers.size)).toBe(1);
  await expect(page.locator("#toast")).toBeHidden();
  await search(page);
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide")));
  await expect.poll(() => page.evaluate(() => window.mockControl.subscribers.size)).toBe(0);
  await expect.poll(() => page.evaluate(() => window.mockControl.contextListeners.size)).toBe(0);
  await expect(page.locator(".mtrl-button, .mtrl-icon-button")).toHaveCount(0);
});

test("disposal during host readiness does not start requests or subscriptions later", async ({ page }) => {
  await page.addInitScript(() => {
    if (location.search !== "?late-ready") return;
    Object.defineProperty(window, "AstrBotPluginView", {
      configurable: true,
      set(bridge) {
        Object.defineProperty(window, "AstrBotPluginView", { value: bridge, configurable: true });
        window.mockControl.hold("ready");
      },
    });
  });
  await page.goto("/preview/?late-ready");
  await expect.poll(() => page.evaluate(() => window.mockControl.waiting.has("ready"))).toBe(true);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("pagehide"));
    window.mockControl.release("ready");
  });
  await page.waitForTimeout(100);
  expect(await page.evaluate(() => window.mockControl.requests.filter((request) => request.method === "GET").length)).toBe(0);
  expect(await page.evaluate(() => window.mockControl.contextListeners.size)).toBe(0);
  expect(await page.evaluate(() => window.mockControl.subscribers.size)).toBe(0);
});

test("disposal during initial status loading prevents a late live connection", async ({ page }) => {
  await page.addInitScript(() => {
    if (location.search !== "?late-status") return;
    Object.defineProperty(window, "AstrBotPluginView", {
      configurable: true,
      set(bridge) {
        Object.defineProperty(window, "AstrBotPluginView", { value: bridge, configurable: true });
        window.mockControl.hold("status");
      },
    });
  });
  await page.goto("/preview/?late-status");
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.some((request) => request.endpoint === "status"))).toBe(true);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("pagehide"));
    window.mockControl.release("status");
  });
  await page.waitForTimeout(100);
  expect(await page.evaluate(() => window.mockControl.contextListeners.size)).toBe(0);
  expect(await page.evaluate(() => window.mockControl.subscribers.size)).toBe(0);
});

test("disposal during manual refresh does not reconnect when the old read returns", async ({ page }) => {
  const before = await page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "status").length);
  await page.evaluate(() => window.mockControl.hold("status"));
  await page.locator("#refresh").click();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "status").length)).toBeGreaterThan(before);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("pagehide"));
    window.mockControl.release("status");
  });
  await page.waitForTimeout(100);
  expect(await page.evaluate(() => window.mockControl.contextListeners.size)).toBe(0);
  expect(await page.evaluate(() => window.mockControl.subscribers.size)).toBe(0);
});

test("the skip link targets the visible heading in every view", async ({ page }) => {
  for (const [view, heading] of [
    ["search", "search-heading"], ["queue", "queue-heading"],
    ["library", "library-heading-page"], ["maintenance", "maintenance-heading"],
  ]) {
    await page.locator(`.tabs [data-view="${view}"]`).click();
    await expect(page.locator(".skip-link")).toHaveAttribute("href", `#${heading}`);
    await page.locator(".skip-link").focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(`#${heading}`)).toBeFocused();
    await expect(page.locator(`#${heading}`)).toBeVisible();
  }
});

test("continuous SSE progress is not starved and unknown totals remain indeterminate", async ({ page }) => {
  await page.locator('.tabs [data-view="maintenance"]').click();
  await page.evaluate(() => {
    window.mockControl.fixtures.dependency_update = { status: "running", phase: "下载：Deno", progress: null, downloaded_bytes: 10485760, total_bytes: null, speed_bytes_per_second: 1048576, started_at: Date.now() / 1000 - 12, message: "下载中" };
    let count = 0;
    const timer = setInterval(() => { window.mockControl.notify(); if (++count === 20) clearInterval(timer); }, 30);
  });
  await expect(page.locator("#update-phase")).toHaveText("下载 · Deno");
  await expect(page.locator("#update-metrics")).toContainText("10.0 MiB");
  await expect(page.locator("#update-metrics")).toContainText("1.0 MiB/s");
  expect(await page.locator("#update-progress").getAttribute("value")).toBeNull();
});

test("unsupported platform reason is visible before an update starts", async ({ page }) => {
  await page.evaluate(() => { window.mockControl.updaterSupported = false; });
  await page.locator('.tabs [data-view="maintenance"]').click();
  await expect(page.locator("#updater-reason")).toHaveText("此平台不支持自动更新。");
  await expect(page.locator("#update-dependencies")).toBeDisabled();
});

test("completed updates refresh health and failed recovery remains visible", async ({ page }) => {
  await page.locator('.tabs [data-view="maintenance"]').click();
  await expect(page.locator("#dependencies .dependency-row")).toHaveCount(4);
  await page.evaluate(() => { window.mockControl.fixtures.dependency_update = { status: "running", phase: "安装", message: "" }; window.mockControl.notify(); });
  await expect(page.locator("#update-phase")).toHaveText("安装");
  const before = await page.evaluate(() => window.mockControl.requests.filter((r) => r.endpoint === "maintenance").length);
  await page.evaluate(() => {
    window.mockControl.fixtures.dependency_update = { status: "failed", phase: "失败", message: "回滚未完成", recovery_required: true, recovery_directory: "dependency-staging/test", recovery_targets: ["deno.exe"] };
    window.mockControl.notify();
  });
  await expect(page.locator("#recovery-message")).toContainText("dependency-staging/test");
  await expect(page.locator("#update-dependencies")).toBeDisabled();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((r) => r.endpoint === "maintenance").length)).toBeGreaterThan(before);
});

test("search pending and errors show distinct result states", async ({ page }) => {
  await page.evaluate(() => window.mockControl.hold("search"));
  await page.locator("#query").fill("音乐");
  await page.locator("#search-button").click();
  await expect(page.locator("#results")).toContainText("搜索中");
  await expect(page.locator("#search-button")).toBeDisabled();
  await page.evaluate(() => { window.mockControl.failNext = "模拟搜索故障"; window.mockControl.release("search"); });
  await expect(page.locator("#results")).toContainText("搜索失败");
  await expect(page.locator("#search-button")).toBeEnabled();
});

test("mobile bottom content remains reachable above navigation", async ({ page }, info) => {
  if (!["mobile", "narrow"].includes(info.project.name)) {
    await expect(page.locator(".tabs")).toBeVisible();
    return;
  }
  await page.locator('.tabs [data-view="maintenance"]').click();
  await expect(page.locator("#cache-summary")).toContainText("MiB");
  await page.locator("#cache-summary").scrollIntoViewIfNeeded();
  await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight));
  const bounds = await page.evaluate(() => ({
    content: document.getElementById("cache-summary").getBoundingClientRect().bottom,
    navigation: document.querySelector(".tabs").getBoundingClientRect().top,
  }));
  expect(bounds.content).toBeLessThan(bounds.navigation);
  await page.screenshot({ path: `${screenshots}/${info.project.name}-maintenance-bottom.png`, animations: "disabled" });
});

test("untrusted titles remain text and missing sessions disable enqueue", async ({ page }) => {
  await page.evaluate(() => {
    window.mockControl.songs[0].title = '<img src=x onerror="window.xss=true">';
    window.mockControl.fixtures.sessions = [];
  });
  await page.locator("#refresh").click();
  await search(page);
  await expect(page.locator("#results .track-title").first()).toContainText("<img");
  expect(await page.evaluate(() => window.xss)).toBeUndefined();
  await expect(page.locator("#results .track-row").first().getByRole("button", { name: "加入队列", exact: true })).toBeDisabled();
  await expect(page.locator("#session")).toBeDisabled();
  await expect(page.locator("#session option:checked")).toHaveText("暂无会话");
  await page.evaluate(() => window.mockControl.locale("en-US"));
  await expect(page.locator("#session option:checked")).toHaveText("No sessions");
});

test("responsive light and dark layouts have rendered assets and no overflow", async ({ page }, info) => {
  await search(page);
  await expect(page.locator("#results img").first()).toBeVisible();
  await expect.poll(() => page.locator("#results img").first().evaluate((image) => image.naturalWidth)).toBeGreaterThan(0);
  for (const dark of [false, true]) {
    await page.evaluate((dark) => window.mockControl.theme(dark), dark);
    for (const view of ["search", "queue", "library", "maintenance"]) {
      await page.locator(`.tabs [data-view="${view}"]`).click();
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const clipped = await page.locator("button:visible").evaluateAll((buttons) => buttons.filter((button) => button.scrollWidth > button.clientWidth + 2).map((button) => button.textContent));
      expect(clipped).toEqual([]);
      await page.mouse.move(0, 0);
      await page.screenshot({ path: `${screenshots}/${info.project.name}-${view}-${dark ? "dark" : "light"}.png`, fullPage: true, animations: "disabled" });
      await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: "instant" }));
      await page.screenshot({ path: `${screenshots}/${info.project.name}-${view}-${dark ? "dark" : "light"}-viewport.png`, animations: "disabled" });
    }
  }
});

test("language changes restore original labels and fit compact navigation", async ({ page }) => {
  await page.evaluate(() => window.mockControl.locale("en-US"));
  await expect(page.locator("h1")).toHaveText("YouTube Music");
  await expect(page.locator('.tabs [data-view="maintenance"]')).toHaveText("Maintenance");
  const clipped = await page.locator(".tabs button").evaluateAll((buttons) => buttons.filter((button) => button.scrollWidth > button.clientWidth + 2).map((button) => button.textContent));
  expect(clipped).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.evaluate(() => window.mockControl.locale("zh-CN"));
  await expect(page.locator("h1")).toHaveText("YouTube 点歌");
  await expect(page.locator('.tabs [data-view="maintenance"]')).toHaveText("维护");
});

test("active item is protected and its phase is visible on mobile", async ({ page }, info) => {
  await page.evaluate(() => {
    const queue = window.mockControl.fixtures.queues["session-a"];
    queue.current = { row: queue.items[0], phase: "转换为 WAV", submitted: false };
    queue.paused = false;
  });
  await page.locator("#refresh").click();
  await queueView(page);
  await expect(page.locator("#now-playing")).toContainText("转换为 WAV");
  await expect(page.locator("#queue-list .queue-row").first().getByRole("button", { name: "移除歌曲" })).toBeDisabled();
  await expect(page.locator("#queue-list .queue-row").nth(1).getByRole("button", { name: "上移", exact: true })).toBeDisabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: `${screenshots}/${info.project.name}-active-queue.png`, fullPage: true, animations: "disabled" });
  await page.screenshot({ path: `${screenshots}/${info.project.name}-active-queue-viewport.png`, animations: "disabled" });
});

test("playback phases follow locale changes and fit every viewport", async ({ page }, info) => {
  const phases = [
    ["等待处理", "Waiting for resources"], ["等待歌曲缓存", "Waiting for song cache"],
    ["读取视频信息", "Reading video information"], ["校验已缓存音频", "Validating cached audio"],
    ["下载音轨", "Downloading audio"], ["检查源音频", "Checking source audio"],
    ["转换为 WAV", "Converting to WAV"], ["验证 WAV 编码与时长", "Validating WAV encoding and duration"],
    ["写入歌曲缓存", "Saving song cache"], ["提交消息", "Sending message"],
    ["已提交语音", "Voice submitted"], ["已提交链接", "Link submitted"],
  ];
  await page.evaluate(() => {
    const queue = window.mockControl.fixtures.queues["session-a"];
    queue.current = { row: queue.items[0], phase: "等待处理", submitted: false };
    queue.paused = false;
  });
  await page.locator("#refresh").click();
  await queueView(page);
  const phaseLabel = page.locator("#now-playing .phase");
  for (const [source, translated] of phases) {
    await page.evaluate((phase) => {
      window.mockControl.fixtures.queues["session-a"].current.phase = phase;
      window.mockControl.notify();
    }, source);
    await expect(phaseLabel).toHaveText(source);
    await page.evaluate(() => window.mockControl.locale("en-US"));
    await expect(phaseLabel).toHaveText(translated);
    expect(await phaseLabel.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true);
    await page.evaluate(() => window.mockControl.locale("zh-CN"));
    await expect(phaseLabel).toHaveText(source);
  }
  await page.evaluate(() => {
    window.mockControl.fixtures.queues["session-a"].current.phase = "Custom <phase>";
    window.mockControl.notify();
  });
  await expect(phaseLabel).toHaveText("Custom <phase>");
  await expect(phaseLabel.locator("phase")).toHaveCount(0);
  await page.evaluate(() => {
    window.mockControl.fixtures.queues["session-a"].current.phase = "验证 WAV 编码与时长";
    window.mockControl.notify();
  });
  await expect(phaseLabel).toHaveText("验证 WAV 编码与时长");
  await page.evaluate(() => window.mockControl.locale("en-US"));
  await expect(phaseLabel).toHaveText("Validating WAV encoding and duration");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: `${screenshots}/${info.project.name}-queue-progress-en-US.png`, animations: "disabled" });
});

test("library filtering and sorting preserve exact video IDs and sources", async ({ page }, info) => {
  await page.evaluate(() => {
    const control = window.mockControl;
    control.library.favorites = [control.songs[2], control.songs[0], control.songs[1]];
    control.library.history = control.songs.map((song) => ({ song }));
  });
  await page.locator('.tabs [data-view="library"]').click();
  await expect(page.locator("#library-list .track-row")).toHaveCount(3);
  await page.locator("#library-sort").selectOption("duration");
  await expect(page.locator("#library-list .track-title").first()).toHaveText("示例曲目 Beta");
  await page.locator("#library-filter").fill("合奏");
  await expect(page.locator("#library-list .track-row")).toHaveCount(1);
  await page.locator("#session").selectOption("session-b");
  await page.locator("#library-list").getByRole("button", { name: "加入队列", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((r) => r.endpoint === "queue/add").at(-1)?.body)).toEqual({
    session: "session-b", index: undefined, source: "favorites", video_id: "cdefghijklm",
  });
  await page.locator("#library-list").getByRole("button", { name: "移除收藏", exact: true }).click();
  await expect(page.locator("#library-list .empty-state")).toHaveText("无匹配歌曲");
  await page.locator("#library-filter").fill("");
  await expect(page.locator("#library-list .track-row")).toHaveCount(2);
  await page.evaluate(() => window.mockControl.theme(true));
  await page.mouse.move(0, 0);
  await page.screenshot({ path: `${screenshots}/${info.project.name}-library-populated-dark.png`, fullPage: true, animations: "disabled" });
  await page.locator('[data-library="history"]').click();
  await expect(page.locator("#library-list .track-row")).toHaveCount(3);
  await page.locator("#clear-history").click();
  await page.getByRole("dialog").getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.locator("#library-list .empty-state")).toHaveText("暂无语音提交记录");
  await expect(page.locator("#toast")).toHaveText("已清除历史");
});

test("locale changes update tooltips, connection state, options and dynamic actions", async ({ page }) => {
  await search(page);
  await page.evaluate(() => window.mockControl.locale("en-US"));
  await expect(page.locator("#connection")).toHaveText("Connected");
  await expect(page.locator("#refresh")).toHaveAttribute("aria-label", "Refresh");
  await expect(page.locator("#refresh")).toHaveAttribute("data-tooltip", "Refresh");
  await expect(page.locator("#component option").first()).toHaveText("All components");
  await expect(page.locator("#results").getByRole("button", { name: "Add to queue", exact: true })).toHaveCount(3);
  await page.locator('.tabs [data-view="maintenance"]').click();
  await expect(page.locator("#runtime-health")).toHaveText("All ready");
  await expect(page.locator("#dependencies .health").first()).toHaveText("Ready");
  const untranslated = await page.locator("[data-i18n-label]").evaluateAll((nodes) => nodes.map((node) => node.getAttribute("aria-label")).filter((label) => /[\u3400-\u9fff]/.test(label)));
  expect(untranslated).toEqual([]);
  await page.evaluate(() => window.mockControl.locale("zh-CN"));
  await expect(page.locator("#connection")).toHaveText("已连接");
  await expect(page.locator("#refresh")).toHaveAttribute("aria-label", "刷新");
  await expect(page.locator("#component option").first()).toHaveText("全部组件");
});

test("untrusted and failed artwork uses a local placeholder", async ({ page }) => {
  await page.evaluate(() => {
    window.mockControl.songs[0].thumbnail_url = "https://attacker.invalid/track.jpg";
    window.mockControl.songs[1].thumbnail_url = "javascript:alert(1)";
    window.mockControl.songs[2].thumbnail_url = "https://i.ytimg.com/vi/cdefghijklm/failure.jpg";
  });
  await page.route("https://i.ytimg.com/**", (route) => route.abort());
  await search(page);
  await expect(page.locator("#results .art-placeholder")).toHaveCount(3);
  await expect(page.locator("#results img")).toHaveCount(0);
  await expect(page.locator("#results .art-placeholder svg")).toHaveCount(3);
});

test("native keyboard submission and Escape cancellation remain safe", async ({ page }) => {
  await page.locator("#query").fill("音乐");
  await page.locator("#query").press("Enter");
  await expect(page.locator("#results .track-row")).toHaveCount(3);
  expect(await page.evaluate(() => window.mockControl.requests.filter((r) => r.endpoint === "search").length)).toBe(1);
  await queueView(page);
  await expect(page.locator("#queue-heading")).toBeFocused();
  await page.locator("#clear-queue").click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(page.locator("#queue-list .queue-row")).toHaveCount(2);
  expect(await page.evaluate(() => window.mockControl.requests.filter((r) => r.endpoint === "queue/action").length)).toBe(0);
  await page.locator("#clear-queue").click();
  await page.evaluate(() => window.dispatchEvent(new Event("pagehide")));
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect.poll(() => page.evaluate(() => window.mockControl.subscribers.size)).toBe(0);
  await expect.poll(() => page.locator(".mtrl-button, .mtrl-icon-button").count()).toBe(0);
  expect(await page.evaluate(() => window.mockControl.requests.filter((r) => r.endpoint === "queue/action").length)).toBe(0);
});

test("Material factories render readable primary controls in both host themes", async ({ page }) => {
  await expect(page.locator("#search-button")).toHaveClass(/mtrl-button/);
  await expect(page.locator("#refresh")).toHaveClass(/mtrl-icon-button/);
  for (const dark of [false, true]) {
    await page.evaluate((dark) => window.mockControl.theme(dark), dark);
    const contrast = await page.locator("#search-button").evaluate((button) => {
      const style = getComputedStyle(button);
      const luminance = (color) => {
        const rgb = color.match(/[\d.]+/g).slice(0, 3).map((value) => {
          const channel = Number(value) / 255;
          return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
        });
        return rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722;
      };
      const colors = [luminance(style.color), luminance(style.backgroundColor)].sort((a, b) => a - b);
      return (colors[1] + .05) / (colors[0] + .05);
    });
    expect(contrast).toBeGreaterThanOrEqual(4.5);
  }
});

test("reduced motion, minimum touch targets and narrow tablet layouts stay usable", async ({ page }, info) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await search(page);
  expect(await page.locator("#search-view").evaluate((node) => getComputedStyle(node).animationName)).toBe("none");
  for (const view of ["search", "queue", "library", "maintenance"]) {
    await page.locator(`.tabs [data-view="${view}"]`).click();
    expect(await page.evaluate(() => scrollY)).toBe(0);
    const small = await page.locator("button:visible").evaluateAll((buttons) => buttons.filter((button) => {
      const bounds = button.getBoundingClientRect();
      return bounds.width < 43.9 || bounds.height < 43.9;
    }).map((button) => button.id || button.textContent));
    expect(small).toEqual([]);
  }
  if (info.project.name === "tablet") {
    await page.setViewportSize({ width: 600, height: 960 });
    for (const view of ["search", "queue", "library", "maintenance"]) {
      await page.locator(`.tabs [data-view="${view}"]`).click();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const overlaps = await page.evaluate(() => {
        const nav = document.querySelector(".tabs").getBoundingClientRect();
        const header = document.querySelector(".app-header").getBoundingClientRect();
        return nav.top < header.bottom && header.bottom > 0;
      });
      expect(overlaps).toBe(false);
    }
  }
});

test("library pending actions remain disabled through filters and segment redraws", async ({ page }) => {
  await page.evaluate(() => {
    window.mockControl.library.favorites = window.mockControl.songs.slice();
    window.mockControl.library.history = window.mockControl.songs.map((song) => ({ song }));
  });
  await page.locator('.tabs [data-view="library"]').click();
  await expect(page.locator("#library-list .track-row")).toHaveCount(3);
  await page.evaluate(() => window.mockControl.hold("library/action"));
  await page.locator("#library-list .track-row").first().getByRole("button", { name: "移除收藏", exact: true }).click();
  await page.locator("#library-sort").selectOption("title");
  await page.locator("#library-filter").fill("测试");
  await expect(page.locator("#library-list .track-row").last().getByRole("button", { name: "移除收藏", exact: true })).toBeDisabled();
  await page.locator('[data-library="history"]').click();
  await expect(page.locator("#clear-history")).toBeDisabled();
  expect(await page.evaluate(() => window.mockControl.requests.filter((r) => r.endpoint === "library/action").length)).toBe(1);
  await page.evaluate(() => window.mockControl.release("library/action"));
  await expect(page.locator("#clear-history")).toBeEnabled();
});

test("maintenance failures invalidate health and current update phases are localized", async ({ page }) => {
  await page.locator('.tabs [data-view="maintenance"]').click();
  await expect(page.locator("#runtime-health")).toHaveText("全部就绪");
  await page.evaluate(() => { window.mockControl.failNext = "模拟检查故障"; });
  await page.locator("#health-refresh").click();
  await expect(page.locator("#runtime-health")).toHaveText("检查失败");
  await expect(page.locator("#runtime-health")).toHaveAttribute("data-state", "error");
  await expect(page.locator("#update-dependencies")).toBeDisabled();
  await page.locator("#health-refresh").click();
  await expect(page.locator("#runtime-health")).toHaveText("全部就绪");
  await page.evaluate(() => {
    window.mockControl.fixtures.dependency_update = { status: "running", phase: "验证暂存程序", message: "" };
    window.mockControl.notify();
    window.mockControl.locale("en-US");
  });
  await expect(page.locator("#update-phase")).toHaveText("Verifying staged programs");
});

test("library pagination retains capped selection through filters, sorts and source changes", async ({ page }) => {
  await populateLibrary(page);
  await page.locator("#library-select-page").check();
  await expect(page.locator("#library-selection-summary")).toContainText("已选 20 / 20");
  await page.locator("#library-next").click();
  await expect(page.locator("#library-page-label")).toContainText("2 / 3");
  await expect(page.locator("#library-list input").first()).toBeDisabled();
  await expect(page.locator("#library-list .track-row")).toHaveCount(20);
  await page.locator("#library-previous").click();
  const first = page.locator("#library-list input").first();
  await first.focus();
  await first.press("Space");
  await expect(first).not.toBeChecked();
  await expect(first).toBeFocused();
  await expect(page.locator("#library-select-page")).toHaveJSProperty("indeterminate", true);
  await page.locator("#library-next").click();
  await page.locator("#library-list input").first().check();
  await expect(page.locator("#library-selection-summary")).toContainText("已选 20 / 20");
  await expect(page.locator("#library-select-page")).toHaveJSProperty("indeterminate", true);
  await expect(page.locator("#library-select-page")).toBeEnabled();
  await expect(page.locator("#library-page-selection-label")).toHaveText("取消本页选择");
  await page.locator("#library-select-page").press("Space");
  await expect(page.locator("#library-selection-summary")).toContainText("已选 19 / 20");
  await page.locator("#library-list input").first().check();
  await page.locator("#library-filter").fill("Track 4");
  await expect(page.locator("#library-list .track-row")).toHaveCount(5);
  await expect(page.locator("#library-pagination")).toBeHidden();
  await page.locator("#library-sort").selectOption("title");
  await expect(page.locator("#library-selection-summary")).toContainText("收藏 · 已选 20 / 20");
  await page.locator('[data-library="history"]').click();
  await expect(page.locator("#library-selection-summary")).toContainText("历史 · 已选 0 / 20");
  await page.locator("#library-list input").first().check();
  await page.locator('[data-library="favorites"]').click();
  await expect(page.locator("#library-selection-summary")).toContainText("收藏 · 已选 20 / 20");
  await page.locator("#library-clear-selection").click();
  await expect(page.locator("#library-selection-summary")).toContainText("收藏 · 已选 0 / 20");
  await page.locator('[data-library="history"]').click();
  await expect(page.locator("#library-selection-summary")).toContainText("历史 · 已选 1 / 20");
  await expect(page.locator("#library-list input").first()).toBeChecked();
});

test("library batch confirms all selected IDs in sorted order and skips queued tracks", async ({ page }, info) => {
  await page.evaluate(() => { window.mockControl.library.favorites = [window.mockControl.songs[2], window.mockControl.songs[0], window.mockControl.songs[1]]; });
  await page.locator('.tabs [data-view="library"]').click();
  await expect(page.locator("#library-list .track-row")).toHaveCount(3);
  await page.locator("#library-select-page").check();
  await page.locator("#library-sort").selectOption("title");
  await page.locator("#library-filter").fill("Gamma");
  await page.locator("#library-batch").click();
  await expect(page.locator("#confirm-detail")).toContainText("QQ 群 123456");
  await expect(page.locator("#confirm-detail")).toContainText("将加入 1 首 · 跳过已排队 2 首");
  await expect(page.locator("#confirm-detail")).toContainText("预计队列 3 / 20");
  await expect(page.locator("#confirm-tracks li")).toHaveCount(3);
  await expect(page.locator("#confirm-tracks li").first()).toContainText("Alpha");
  await page.screenshot({ path: `${screenshots}/${info.project.name}-library-batch-confirm.png`, animations: "disabled" });
  await page.getByRole("dialog").getByRole("button", { name: "确认", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.find((request) => request.endpoint === "queue/batch")?.body)).toEqual({
    session: "session-a", source: "favorites", video_ids: ["abcdefghijk", "bcdefghijkl", "cdefghijklm"],
  });
  await expect(page.locator("#toast")).toContainText("QQ 群 123456 · 已加入队列 1 首 · 跳过已排队 2 首");
  await expect(page.locator("#library-selection-summary")).toContainText("已选 0 / 20");
  expect(await page.evaluate(() => window.mockControl.fixtures.queues["session-a"].items.map((item) => item.song.video_id))).toEqual(["abcdefghijk", "bcdefghijkl", "cdefghijklm"]);
});

test("library batch retains its captured session and source without clearing newer selections", async ({ page }) => {
  await page.evaluate(() => {
    const control = window.mockControl;
    control.library.favorites = control.songs.slice();
    control.library.history = control.songs.map((song) => ({ song, played_at: 1700000000, play_count: 1 }));
    control.hold("queue/batch");
  });
  await page.locator('.tabs [data-view="library"]').click();
  await expect(page.locator("#library-list .track-row")).toHaveCount(3);
  await page.locator("#session").selectOption("session-b");
  await page.locator("#library-list input").first().check();
  await page.locator("#library-batch").click();
  await expect(page.locator("#confirm-detail")).toContainText("QQ 群 654321");
  await page.evaluate(() => {
    const session = document.querySelector("#session");
    session.value = "session-a";
    session.dispatchEvent(new Event("change", { bubbles: true }));
    document.querySelector('[data-library="history"]').click();
  });
  await page.getByRole("dialog").getByRole("button", { name: "确认", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "queue/batch").length)).toBe(1);
  await page.locator("#library-list input").last().check();
  await expect(page.locator("#library-selection-summary")).toContainText("历史 · 已选 1 / 20");
  await page.evaluate(() => window.mockControl.release("queue/batch"));
  await expect(page.locator("#toast")).toContainText("QQ 群 654321 · 已加入队列 1 首");
  await expect(page.locator("#library-selection-summary")).toContainText("历史 · 已选 1 / 20");
  await page.locator('[data-library="favorites"]').click();
  await expect(page.locator("#library-selection-summary")).toContainText("收藏 · 已选 0 / 20");
  expect(await page.evaluate(() => window.mockControl.requests.find((request) => request.endpoint === "queue/batch").body)).toEqual({
    session: "session-b", source: "favorites", video_ids: ["abcdefghijk"],
  });
  expect(await page.evaluate(() => window.mockControl.fixtures.queues["session-a"].items.length)).toBe(2);
});

test("library batch capacity and write failures preserve selection and never partially enqueue", async ({ page }) => {
  await populateLibrary(page, 3);
  await page.locator("#session").selectOption("session-b");
  await page.locator("#library-list input").nth(0).check();
  await page.locator("#library-list input").nth(1).check();
  await page.locator("#library-batch").click();
  await page.evaluate(() => { window.mockControl.fixtures.limits.queue = 1; });
  await page.getByRole("dialog").getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.locator("#error-text")).toContainText("未加入任何歌曲");
  await expect(page.locator("#library-selection-summary")).toContainText("已选 2 / 20");
  expect(await page.evaluate(() => window.mockControl.fixtures.queues["session-b"].items.length)).toBe(0);
  await page.evaluate(() => { window.mockControl.fixtures.limits.queue = 20; });
  await page.locator("#library-batch").click();
  await page.evaluate(() => { window.mockControl.failNext = "模拟状态保存失败"; });
  await page.getByRole("dialog").getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.locator("#error-text")).toHaveText("模拟状态保存失败");
  await expect(page.locator("#library-selection-summary")).toContainText("已选 2 / 20");
  expect(await page.evaluate(() => window.mockControl.fixtures.queues["session-b"].items.length)).toBe(0);
  await page.locator("#library-batch").click();
  await page.getByRole("dialog").getByRole("button", { name: "确认", exact: true }).click();
  await expect(page.locator("#library-selection-summary")).toContainText("已选 0 / 20");
  expect(await page.evaluate(() => window.mockControl.fixtures.queues["session-b"].items.length)).toBe(2);
});

test("library batch pending state survives SSE and filter redraws with one atomic request", async ({ page }) => {
  await populateLibrary(page, 3);
  await page.locator("#session").selectOption("session-b");
  await page.locator("#library-select-page").check();
  await page.evaluate(() => window.mockControl.hold("queue/batch"));
  await page.locator("#library-batch").click();
  await page.getByRole("dialog").getByRole("button", { name: "确认", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "queue/batch").length)).toBe(1);
  await page.evaluate(() => {
    const control = window.mockControl;
    control.fixtures.queues["session-b"].items = [{ id: "outside-request", song: control.library.favorites[1], requested_by: "Other user" }];
    control.notify();
  });
  await page.locator("#library-sort").selectOption("duration");
  await page.locator("#library-filter").fill("Track 00");
  await expect(page.locator("#library-batch")).toBeDisabled();
  await expect(page.locator("#library-clear-selection")).toBeDisabled();
  await expect(page.locator("#library-list input").first()).toBeDisabled();
  await page.evaluate(() => document.querySelector("#library-batch").click());
  expect(await page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "queue/batch").length)).toBe(1);
  await page.evaluate(() => window.mockControl.release("queue/batch"));
  await expect(page.locator("#toast")).toContainText("已加入队列 2 首 · 跳过已排队 1 首");
  await expect(page.locator("#library-selection-summary")).toContainText("已选 0 / 20");
  expect(await page.evaluate(() => window.mockControl.fixtures.queues["session-b"].items.length)).toBe(3);
});

test("library batch Escape and cached-page cancellation retain choices without posting", async ({ page }) => {
  await populateLibrary(page, 3);
  await page.locator("#library-list input").first().check();
  await page.locator("#library-batch").click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(page.locator("#library-selection-summary")).toContainText("已选 1 / 20");
  await page.locator("#library-batch").click();
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true })));
  await expect(page.getByRole("dialog")).toBeHidden();
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true })));
  await expect(page.locator("#connection")).toHaveText("已连接");
  await expect(page.locator("#library-list input").first()).toBeChecked();
  await expect(page.locator("#library-batch")).toBeEnabled();
  expect(await page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "queue/batch").length)).toBe(0);
});

test("history metadata and frequency sorting favorite the exact owner-scoped history ID", async ({ page }) => {
  await page.evaluate(() => {
    const control = window.mockControl;
    control.library.favorites = [control.songs[1]];
    control.library.history = [
      { song: control.songs[0], played_at: 1700000000, play_count: 2 },
      { song: control.songs[1], played_at: 1700003600, play_count: 5 },
      { song: control.songs[2], played_at: 1700007200, play_count: 5 },
    ];
  });
  await page.locator('.tabs [data-view="library"]').click();
  await page.locator('[data-library="history"]').click();
  await expect(page.locator("#library-sort-recent")).toHaveText("最近提交");
  await expect(page.locator("#library-list .track-title").first()).toContainText("Gamma");
  await expect(page.locator("#library-list .history-metadata").first()).toContainText("提交次数 5");
  await expect(page.locator("#library-list .history-metadata").first()).toContainText("最近提交");
  await page.locator("#library-sort").selectOption("plays");
  await expect(page.locator("#library-list .track-title").nth(1)).toContainText("Beta");
  await expect(page.locator("#library-list .track-row").nth(1).getByRole("button", { name: "收藏歌曲", exact: true })).toBeDisabled();
  await page.locator("#library-list .track-row").first().getByRole("button", { name: "收藏歌曲", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.find((request) => request.endpoint === "library/action")?.body)).toEqual({
    action: "favorite", source: "history", video_id: "cdefghijklm",
  });
  await expect(page.locator("#library-list .track-row").first().getByRole("button", { name: "收藏歌曲", exact: true })).toBeDisabled();
  await page.evaluate(() => window.mockControl.locale("en-US"));
  await expect(page.locator("#library-sort-plays")).toHaveText("Most submitted");
  await expect(page.locator("#library-list .history-metadata").first()).toContainText("Submissions 5");
  await expect(page.locator("#library-list .history-metadata").first()).toContainText("Last submitted");
  await page.locator('[data-library="favorites"]').click();
  await expect(page.locator("#library-list .track-row")).toHaveCount(2);
  await expect(page.locator("#library-sort")).toHaveValue("recent");
});

test("pending duration summarizes persisted queue items without double counting the current song", async ({ page }) => {
  await queueView(page);
  await expect(page.locator("#queue-duration")).toHaveText("待播总时长 3:30");
  await page.evaluate(() => {
    const control = window.mockControl;
    const queue = control.fixtures.queues["session-a"];
    queue.current = { row: queue.items[0], submitted: true, phase: "已提交语音" };
    queue.items = [
      { id: "pending-known", song: control.songs[1], requested_by: "User" },
      { id: "pending-unknown", song: { ...control.songs[2], duration: null }, requested_by: "User" },
    ];
    control.notify();
  });
  await expect(page.locator("#queue-duration")).toHaveText("待播总时长 1:30 · 1 首时长未知");
  await expect(page.locator("#queue-total")).toHaveText("2 / 20");
  await page.evaluate(() => {
    const control = window.mockControl;
    const queue = control.fixtures.queues["session-a"];
    queue.items = [{ id: "active", song: control.songs[0], requested_by: "User" }, { id: "next", song: control.songs[1], requested_by: "User" }];
    queue.current = { row: queue.items[0], submitted: false, phase: "等待处理" };
    control.notify();
    control.locale("en-US");
  });
  await expect(page.locator("#queue-duration")).toHaveText("Pending duration 3:30");
  expect(await page.locator("#queue-duration").textContent()).not.toMatch(/ETA/);
});

test("library selection controls and long batch confirmations fit both locales and host themes", async ({ page }, info) => {
  await populateLibrary(page);
  await page.locator("#library-select-page").check();
  for (const locale of ["zh-CN", "en-US"]) {
    for (const dark of [false, true]) {
      await page.evaluate(({ locale, dark }) => { window.mockControl.locale(locale); window.mockControl.theme(dark); }, { locale, dark });
      const bounds = await page.locator(".track-selector").first().boundingBox();
      expect(bounds.width).toBeGreaterThanOrEqual(44);
      expect(bounds.height).toBeGreaterThanOrEqual(44);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const clipped = await page.locator("#library-view button:visible").evaluateAll((buttons) => buttons.filter((button) => button.scrollWidth > button.clientWidth + 2).map((button) => button.id));
      expect(clipped).toEqual([]);
      await page.screenshot({ path: `${screenshots}/${info.project.name}-library-selected-${locale}-${dark ? "dark" : "light"}.png`, animations: "disabled" });
      await page.locator("#library-batch").click();
      await expect(page.locator("#confirm-tracks li")).toHaveCount(20);
      const dialog = await page.getByRole("dialog").boundingBox();
      const viewport = page.viewportSize();
      expect(dialog.width).toBeLessThanOrEqual(viewport.width - 16);
      expect(dialog.height).toBeLessThanOrEqual(viewport.height - 16);
      await page.screenshot({ path: `${screenshots}/${info.project.name}-library-confirm-${locale}-${dark ? "dark" : "light"}.png`, animations: "disabled" });
      await page.keyboard.press("Escape");
      await expect(page.locator("#library-selection-summary")).toContainText("20 / 20");
    }
  }
  if (info.project.name === "desktop") {
    await page.evaluate(() => {
      const control = window.mockControl;
      control.library.favorites = control.songs.slice();
      control.library.history = control.songs.map((song, index) => ({ song, played_at: 1794960000 + index * 3600, play_count: index + 2 }));
      control.locale("zh-CN");
      control.theme(false);
    });
    await page.locator("#library-refresh").click();
    await page.locator('[data-library="history"]').click();
    await page.locator("#library-select-page").check();
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({ path: `${screenshots}/desktop-library.png`, animations: "disabled" });
    await page.locator('.tabs [data-view="search"]').click();
    await search(page);
    await page.screenshot({ path: `${screenshots}/desktop-search.png`, animations: "disabled" });
  }
  if (info.project.name === "mobile") {
    await page.evaluate(() => { window.mockControl.locale("zh-CN"); window.mockControl.theme(false); });
    await queueView(page);
    await page.screenshot({ path: `${screenshots}/mobile-queue.png`, animations: "disabled" });
  }
});

test("error-state resume confirms a captured session and error while ordinary resume stays direct", async ({ page }) => {
  await page.evaluate(() => { window.mockControl.fixtures.queues["session-a"].paused = false; window.mockControl.notify(); });
  await queueView(page);
  await expect(page.locator("#queue-state")).toHaveText("待启动");
  await page.locator("#resume").click();
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "queue/action").length)).toBe(1);
  await page.evaluate(() => {
    const control = window.mockControl;
    control.fixtures.queues["session-a"].paused = true;
    control.fixtures.queues["session-a"].error = "上次提交结果未知";
    control.notify();
  });
  await expect(page.locator("#queue-error")).toHaveText("上次提交结果未知");
  await page.locator("#queue-list .queue-row").last().getByRole("button", { name: "上移", exact: true }).click();
  await expect(page.locator("#queue-list .track-title").first()).toContainText("Beta");
  await page.locator("#skip").click();
  await expect(page.locator("#queue-list .queue-row")).toHaveCount(1);
  await expect(page.locator("#queue-error")).toHaveText("上次提交结果未知");
  await expect(page.locator("#queue-state")).toHaveText("已暂停");
  await page.locator("#resume").click();
  await expect(page.locator("#confirm-detail")).toContainText("QQ 群 123456");
  await expect(page.locator("#confirm-detail")).toContainText("上次提交结果未知");
  await expect(page.locator("#confirm-detail")).toContainText("继续可能再次发送当前项");
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
  expect(await page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "queue/action").length)).toBe(3);
  await page.locator("#resume").click();
  await page.evaluate(() => {
    const session = document.querySelector("#session");
    session.value = "session-b";
    session.dispatchEvent(new Event("change", { bubbles: true }));
    window.mockControl.fixtures.queues["session-a"].error = "更新后的错误";
    window.mockControl.notify();
  });
  await expect(page.locator("#confirm-detail")).toContainText("上次提交结果未知");
  await expect(page.locator("#confirm-detail")).not.toContainText("更新后的错误");
  await page.getByRole("dialog").getByRole("button", { name: "确认", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "queue/action").length)).toBe(4);
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "queue/action").at(-1)?.body)).toEqual({ session: "session-a", action: "resume" });
  await expect.poll(() => page.evaluate(() => window.mockControl.fixtures.queues["session-a"].paused)).toBe(false);
  await expect.poll(() => page.evaluate(() => window.mockControl.fixtures.queues["session-a"].error)).toBe("");
});

test("paused queue resume requires confirmation after a normal pause and supports cancellation", async ({ page }) => {
  await page.evaluate(() => { window.mockControl.fixtures.queues["session-a"].paused = false; window.mockControl.notify(); });
  await queueView(page);
  await expect(page.locator("#queue-state")).toHaveText("待启动");
  await page.locator("#pause").click();
  await expect(page.locator("#queue-state")).toHaveText("已暂停");
  await expect(page.locator("#queue-error")).toBeHidden();
  await page.locator("#resume").click();
  await expect(page.locator("#confirm-detail")).toHaveText("QQ 群 123456\n请先核对目标会话与队列；若上次提交结果未知，继续可能再次发送当前项。");
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
  expect(await page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "queue/action").length)).toBe(1);
  expect(await page.evaluate(() => window.mockControl.fixtures.queues["session-a"].paused)).toBe(true);
  await page.locator("#resume").click();
  await page.getByRole("dialog").getByRole("button", { name: "确认", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "queue/action").length)).toBe(2);
  await expect.poll(() => page.evaluate(() => window.mockControl.fixtures.queues["session-a"].paused)).toBe(false);
});

test("runtime restart preserves a paused queue and requires resume confirmation without an error", async ({ page }) => {
  await page.evaluate(() => {
    const control = window.mockControl;
    const queue = control.fixtures.queues["session-a"];
    queue.error = "模拟重启前提交超时";
    queue.current = { row: queue.items[0], submitted: true, phase: "已提交语音" };
    control.notify();
  });
  await queueView(page);
  await expect(page.locator("#queue-error")).toHaveText("模拟重启前提交超时");
  await page.evaluate(() => window.mockControl.restartRuntime());
  await expect(page.locator("#queue-error")).toBeHidden();
  await expect(page.locator("#queue-state")).toHaveText("已暂停");
  await expect(page.locator("#now-playing")).toBeHidden();
  await expect(page.locator("#queue-list .queue-row")).toHaveCount(2);
  await page.locator("#resume").click();
  await expect(page.locator("#confirm-detail")).toHaveText("QQ 群 123456\n请先核对目标会话与队列；若上次提交结果未知，继续可能再次发送当前项。");
  await expect(page.locator("#confirm-detail")).not.toContainText("模拟重启前提交超时");
  await page.keyboard.press("Escape");
  expect(await page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "queue/action").length)).toBe(0);
  await page.evaluate(() => window.mockControl.locale("en-US"));
  await page.locator("#resume").click();
  await expect(page.locator("#confirm-title")).toHaveText("Resume this session queue?");
  await expect(page.locator("#confirm-detail")).toContainText("Check the target session and queue first");
  await page.evaluate(() => {
    const session = document.querySelector("#session");
    session.value = "session-b";
    session.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await page.getByRole("dialog").getByRole("button", { name: "Confirm", exact: true }).click();
  await expect.poll(() => page.evaluate(() => window.mockControl.requests.filter((request) => request.endpoint === "queue/action").length)).toBe(1);
  expect(await page.evaluate(() => window.mockControl.requests.find((request) => request.endpoint === "queue/action").body)).toEqual({ session: "session-a", action: "resume" });
  await expect.poll(() => page.evaluate(() => window.mockControl.fixtures.queues["session-a"].paused)).toBe(false);
});
