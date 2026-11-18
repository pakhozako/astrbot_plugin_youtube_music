export class LiveConnection {
  constructor(bridge, { onSnapshot, onState, onPoll, timers = globalThis,
    retryBase = 1000, retryCap = 30000, maxRetries = 6, pollEvery = 15000,
    heartbeatTimeout = 45000 } = {}) {
    Object.assign(this, { bridge, onSnapshot, onState, onPoll, timers,
      retryBase, retryCap, maxRetries, pollEvery, heartbeatTimeout });
    this.id = null;
    this.retryTimer = null;
    this.pollTimer = null;
    this.watchdogTimer = null;
    this.pollBusy = false;
    this.attempt = 0;
    this.generation = 0;
    this.stopped = true;
  }

  async start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.attempt = 0;
    await this.connect();
  }

  async connect() {
    if (this.stopped) return;
    const generation = ++this.generation;
    this.onState?.("connecting");
    this.watch(generation);
    try {
      const id = await this.bridge.subscribeSSE("events", {
        onOpen: () => {
          if (this.stopped || generation !== this.generation) return;
          this.attempt = 0;
          this.stopPolling();
          this.watch(generation);
          this.onState?.("live");
        },
        onMessage: (event) => {
          if (this.stopped || generation !== this.generation) return;
          this.attempt = 0;
          this.stopPolling();
          this.watch(generation);
          if (event?.eventType === "heartbeat" || event?.parsed?.heartbeat) return;
          if (event?.parsed && typeof event.parsed === "object") {
            Promise.resolve(this.onSnapshot?.(event.parsed)).catch(() => {});
          }
        },
        onError: () => this.disconnected(generation),
        onClose: () => this.disconnected(generation),
      });
      if (this.stopped || generation !== this.generation) await this.unsubscribe(id);
      else this.id = id;
    } catch {
      this.disconnected(generation);
    }
  }

  disconnected(generation) {
    if (this.stopped || generation !== this.generation) return;
    this.generation += 1;
    this.clearWatchdog();
    this.unsubscribe(this.id);
    this.id = null;
    this.startPolling();
    if (this.attempt >= this.maxRetries) {
      this.onState?.("polling");
      return;
    }
    const delay = Math.min(this.retryCap, this.retryBase * (2 ** this.attempt));
    this.attempt += 1;
    this.onState?.("retrying");
    if (this.retryTimer !== null) this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = this.timers.setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }

  watch(generation) {
    this.clearWatchdog();
    this.watchdogTimer = this.timers.setTimeout(
      () => this.disconnected(generation), this.heartbeatTimeout,
    );
  }

  clearWatchdog() {
    if (this.watchdogTimer !== null) this.timers.clearTimeout(this.watchdogTimer);
    this.watchdogTimer = null;
  }

  startPolling() {
    if (!this.onPoll || this.pollTimer !== null) return;
    const poll = async () => {
      if (this.stopped || this.pollBusy) return;
      this.pollBusy = true;
      try { await this.onPoll(); } catch { /* The next bounded poll can recover. */ }
      finally { this.pollBusy = false; }
    };
    this.pollTimer = this.timers.setInterval(poll, this.pollEvery);
    poll();
  }

  stopPolling() {
    if (this.pollTimer !== null) this.timers.clearInterval(this.pollTimer);
    this.pollTimer = null;
  }

  async unsubscribe(id) {
    if (id === null || id === undefined) return;
    try { await this.bridge.unsubscribeSSE(id); } catch { /* Already-closed streams are harmless. */ }
  }

  async stop() {
    this.stopped = true;
    this.generation += 1;
    if (this.retryTimer !== null) this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.clearWatchdog();
    this.stopPolling();
    const id = this.id;
    this.id = null;
    await this.unsubscribe(id);
  }

  async restart() {
    const stopping = this.stop();
    const generation = this.generation;
    await stopping;
    if (generation !== this.generation || !this.stopped) return;
    await this.start();
  }
}
