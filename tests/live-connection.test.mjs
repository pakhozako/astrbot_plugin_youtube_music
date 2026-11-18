import test from "node:test";
import assert from "node:assert/strict";
import { LiveConnection } from "../views/console/live-connection.mjs";

class Timers {
  next = 0;
  timeouts = new Map();
  intervals = new Map();
  setTimeout = (callback, delay) => { const id = ++this.next; this.timeouts.set(id, { callback, delay }); return id; };
  clearTimeout = (id) => this.timeouts.delete(id);
  setInterval = (callback) => { const id = ++this.next; this.intervals.set(id, callback); return id; };
  clearInterval = (id) => this.intervals.delete(id);
  async fire(delay) {
    const entry = [...this.timeouts].find(([, value]) => value.delay === delay);
    assert.ok(entry);
    this.timeouts.delete(entry[0]);
    await entry[1].callback();
    await new Promise((resolve) => setImmediate(resolve));
  }
}

test("uses the current endpoint and releases subscriptions and timers", async () => {
  const timers = new Timers(), removed = [];
  const bridge = {
    async subscribeSSE(endpoint, handlers) { assert.equal(endpoint, "events"); handlers.onOpen(); return "one"; },
    async unsubscribeSSE(id) { removed.push(id); },
  };
  const live = new LiveConnection(bridge, { timers });
  await live.start();
  await live.stop();
  assert.deepEqual(removed, ["one"]);
  assert.equal(timers.timeouts.size, 0);
  assert.equal(timers.intervals.size, 0);
});

test("a subscription resolved after disposal is still released", async () => {
  let resolve;
  const removed = [];
  const live = new LiveConnection({
    subscribeSSE: () => new Promise((done) => { resolve = done; }),
    unsubscribeSSE: async (id) => removed.push(id),
  }, { timers: new Timers() });
  const starting = live.start();
  await live.stop();
  resolve("late");
  await starting;
  assert.deepEqual(removed, ["late"]);
});

test("a stop supersedes a restart waiting for its old subscription to close", async () => {
  const timers = new Timers();
  let attempts = 0, release;
  const live = new LiveConnection({
    async subscribeSSE(_endpoint, handlers) { attempts++; handlers.onOpen(); return "one"; },
    unsubscribeSSE: () => new Promise((resolve) => { release = resolve; }),
  }, { timers });
  await live.start();
  const restarting = live.restart();
  await live.stop();
  release();
  await restarting;
  assert.equal(attempts, 1);
  assert.equal(live.stopped, true);
  assert.equal(timers.timeouts.size, 0);
  assert.equal(timers.intervals.size, 0);
});

test("retries are bounded and failure polling has only one interval", async () => {
  const timers = new Timers(), states = [];
  let polls = 0, attempts = 0;
  const live = new LiveConnection({
    async subscribeSSE() { attempts++; throw new Error("offline"); },
    async unsubscribeSSE() {},
  }, { timers, maxRetries: 1, onState: (state) => states.push(state), onPoll: async () => { polls++; } });
  await live.start();
  await timers.fire(1000);
  assert.equal(attempts, 2);
  assert.equal(timers.intervals.size, 1);
  assert.equal(states.at(-1), "polling");
  assert.equal(polls, 1);
  await live.stop();
  assert.equal(timers.intervals.size, 0);
});

test("heartbeat resets watchdog without producing a snapshot", async () => {
  const timers = new Timers();
  let handlers, snapshots = 0;
  const live = new LiveConnection({
    async subscribeSSE(_endpoint, callbacks) { handlers = callbacks; callbacks.onOpen(); return "one"; },
    async unsubscribeSSE() {},
  }, { timers, onSnapshot: () => { snapshots++; } });
  await live.start();
  const oldWatchdog = [...timers.timeouts.keys()][0];
  handlers.onMessage({ parsed: { heartbeat: true } });
  assert.equal(snapshots, 0);
  assert.equal(timers.timeouts.has(oldWatchdog), false);
  handlers.onMessage({ parsed: { revision: 2 } });
  assert.equal(snapshots, 1);
  handlers.onClose();
  handlers.onMessage({ parsed: { revision: 3 } });
  assert.equal(snapshots, 1);
  await live.stop();
});
