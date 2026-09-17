const state = {
  rules: [],
  events: [],
  niftySymbols: [],
  providers: {},
  providerErrors: {},
  feedIssues: {},
  lastEventId: Number(localStorage.getItem("tradeM.lastEventId") || 0),
  eventsLoaded: false,
  eventsInitialized: false,
  eventStream: null,
  alertChannelConnected: false,
  eventSyncInFlight: false,
  lastEventSyncAt: null,
};

const $ = (selector) => document.querySelector(selector);

function showMessage(text, kind = "info") {
  const node = $("#message");
  node.textContent = text;
  node.className = `message ${kind}`;
  window.setTimeout(() => node.classList.add("hidden"), 9000);
}

async function api(path, options = {}) {
  const { timeoutMs = 15000, ...fetchOptions } = options;
  const headers = { ...(fetchOptions.headers || {}) };
  if (!(fetchOptions.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      ...fetchOptions,
      headers,
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      try { detail = (await response.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return response.json();
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The local server did not respond in time.");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function formatTime(iso) {
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", second: "2-digit"
  }).format(new Date(iso));
}

function providerTitle(name) {
  return { zerodha: "Zerodha", upstox: "Upstox", dhan: "DhanHQ" }[name] || name;
}

function renderProviderStatus(name, status) {
  const title = providerTitle(name);
  const copy = $(`#${name}Copy`);
  const button = $(`#${name}Button`);
  if (!status.configured) {
    copy.textContent = `Add ${title.toUpperCase()} API credentials to .env, then restart.`;
    button.textContent = "Configuration required";
    button.classList.add("disabled");
  } else if (status.authenticated) {
    let feed = "Signed in; feed connecting.";
    if (status.monitor.recovering && status.monitor.connected) feed = "Live feed connected; recovering missed candles.";
    else if (status.monitor.stale) feed = "Feed connected but no recent ticks were received.";
    else if (status.monitor.connected) feed = "Live feed connected.";
    const desired = status.monitor.desired_subscriptions || 0;
    const subscribed = status.monitor.subscribed_instruments || 0;
    const subscriptions = desired ? ` Monitoring ${subscribed}/${desired} stocks.` : "";
    copy.textContent = `${feed}${subscriptions}${status.user_name ? ` Account: ${status.user_name}.` : ""}`;
    button.textContent = `Reconnect ${title}`;
    button.classList.remove("disabled");
  } else {
    copy.textContent = `Configured. Sign in before using ${title} data.`;
    button.textContent = `Sign in with ${title}`;
    button.classList.remove("disabled");
  }
}

function renderStatus(status) {
  state.providers = status.providers || {};
  const market = $("#marketBadge");
  market.textContent = status.market_open ? "Market session open" : "Market session closed";
  market.className = `badge ${status.market_open ? "good" : "neutral"}`;

  const connected = Object.entries(state.providers)
    .filter(([, value]) => value.monitor.connected && !value.monitor.stale)
    .map(([name]) => providerTitle(name));
  const feed = $("#feedBadge");
  feed.textContent = connected.length ? `${connected.join(" + ")} live` : "Feeds disconnected";
  feed.className = `badge ${connected.length ? "good" : "warn"}`;
  $("#serverTime").textContent = `IST ${formatTime(status.server_time)}`;

  for (const name of ["zerodha", "upstox", "dhan"]) {
    if (state.providers[name]) renderProviderStatus(name, state.providers[name]);
    const provider = state.providers[name];
    const monitor = provider?.monitor;
    const error = monitor?.last_error;
    if (error && state.providerErrors[name] !== error) {
      state.providerErrors[name] = error;
      showMessage(error, "warn");
    }

    if (!status.market_open || !provider?.authenticated || !monitor?.running) {
      delete state.feedIssues[name];
      continue;
    }
    const issue = !monitor.connected
      ? `${providerTitle(name)} live feed is disconnected.`
      : monitor.subscription_gap > 0
      ? `${providerTitle(name)} is missing ${monitor.subscription_gap} live stock subscription${monitor.subscription_gap === 1 ? "" : "s"}; automatic retry is active.`
      : monitor.stale
      ? `${providerTitle(name)} feed is stale; no recent market ticks were received.` : null;
    if (issue && state.feedIssues[name] !== issue) {
      state.feedIssues[name] = issue;
      showMessage(issue, "error");
      notifySystem(`${providerTitle(name)} feed problem`, issue, `trade-m-feed-${name}`);
    } else if (!issue) {
      delete state.feedIssues[name];
    }
  }
}

function renderMarketMovement(metric) {
  const value = $("#movementPercentage");
  const status = $("#movementStatus");
  const close = $("#vixClose");
  const banner = document.querySelector(".movement-banner");
  if (!metric.available) {
    value.textContent = "—";
    close.textContent = "—";
    status.textContent = metric.error || "India VIX data is unavailable.";
    banner.classList.remove("available");
    return;
  }
  value.textContent = metric.moving_percentage_display;
  close.textContent = metric.vix_close_display;
  status.textContent = `${metric.reference_date} close · ${providerTitle(metric.provider)}`;
  banner.classList.add("available");
}

async function refreshMarketMovement() {
  try {
    renderMarketMovement(await api("/api/market-movement"));
  } catch (error) {
    renderMarketMovement({ available: false, error: error.message });
  }
}

function renderRules() {
  const container = $("#rules");
  if (!state.rules.length) {
    container.className = "rules-list empty-state";
    container.textContent = "No rules configured today.";
    return;
  }
  container.className = "rules-list";
  container.innerHTML = state.rules.map(rule => `
    <article class="rule-row ${rule.active ? "" : "paused"}">
      <div class="symbol-block">
        <span class="exchange">${rule.exchange} · ${rule.provider.toUpperCase()}</span>
        <strong>${rule.tradingsymbol}</strong>
        <small>Reference ${rule.reference_date} · ₹${rule.reference_close_display}</small>
      </div>
      <div class="level-block upper">
        <small>UPPER · +${rule.percentage}%</small>
        <strong>₹${rule.upper_level_display}</strong>
        <span class="pill ${rule.upper_sent ? "triggered" : "waiting"}">${rule.upper_sent ? "Alerted" : "Waiting"}</span>
      </div>
      <div class="level-block lower">
        <small>LOWER · −${rule.percentage}%</small>
        <strong>₹${rule.lower_level_display}</strong>
        <span class="pill ${rule.lower_sent ? "triggered" : "waiting"}">${rule.lower_sent ? "Alerted" : "Waiting"}</span>
      </div>
      <div class="rule-actions">
        <label>Daily %
          <div class="inline-percent">
            <input data-percentage="${rule.id}" type="number" min="0.01" max="50" step="0.01" value="${rule.percentage}">
            <button class="mini-button" data-save="${rule.id}">Save</button>
          </div>
        </label>
        <button class="mini-button ${rule.active ? "pause" : "start"}" data-active="${rule.id}" data-next="${!rule.active}">
          ${rule.active ? "Pause" : "Start"}
        </button>
      </div>
    </article>`).join("");
}

function eventTitle(event) {
  const setup = event.trade_side ? ` ${event.trade_side} entry alert` : ` ${event.direction.toLowerCase()} level crossed`;
  return `${event.exchange}:${event.tradingsymbol}${setup}`;
}

function eventBody(event) {
  const movement = event.direction === "UPPER" ? "retraced downward through" : "rebounded upward through";
  const crossing = `Price ${movement} ₹${event.threshold_display} in the completed ${formatTime(event.candle_start)}–${formatTime(event.candle_end)} candle.`;
  if (!event.stop_loss_display) return crossing;
  return `${crossing} Reference entry ₹${event.entry_price_display}; protective stop ₹${event.stop_loss_display} (${event.risk_percent_display} risk, ${String(event.stop_confidence || "unvalidated").toLowerCase()}).`;
}

function notifySystem(title, body, tag) {
  if (!("Notification" in window) || Notification.permission !== "granted") return false;
  try {
    const notification = new Notification(title, { body, tag, requireInteraction: true });
    notification.onclick = () => window.focus();
    return true;
  } catch (error) {
    state.alertChannelConnected = false;
    renderDeliveryHealth(`Browser rejected an alert: ${error.message}`, "error");
    return false;
  }
}

function notify(event) {
  return notifySystem(eventTitle(event), eventBody(event), `trade-m-${event.id}`);
}

function renderDeliveryHealth(message = null, forcedKind = null) {
  const box = $("#deliveryStatus");
  const text = $("#deliveryStatusText");
  const button = $("#notificationButton");
  const testButton = $("#testNotificationButton");
  const supported = "Notification" in window;
  const permission = supported ? Notification.permission : "unsupported";
  let kind = forcedKind;
  let copy = message;

  if (!copy && !supported) {
    kind = "error";
    copy = "This browser does not support desktop alerts.";
  } else if (!copy && permission === "denied") {
    kind = "error";
    copy = "Notifications are blocked in browser site settings.";
  } else if (!copy && permission !== "granted") {
    kind = "warn";
    copy = "Notification permission is required.";
  } else if (!copy && state.alertChannelConnected) {
    kind = "good";
    copy = "Live alert channel connected.";
  } else if (!copy) {
    kind = "warn";
    copy = "Live channel reconnecting; safety backfill is active.";
  }

  box.className = `delivery-status ${kind || "warn"}`;
  text.textContent = copy;
  button.textContent = permission === "granted" ? "Notifications enabled" : "Enable notifications";
  button.disabled = permission === "granted" || permission === "denied" || !supported;
  testButton.disabled = permission !== "granted";
}

function renderEvents() {
  const container = $("#events");
  if (!state.events.length) {
    container.className = "events-list empty-state";
    container.textContent = "No alerts triggered today.";
    return;
  }
  container.className = "events-list";
  container.innerHTML = [...state.events].reverse().map(event => `
    <article class="event-row">
      <span class="event-dot ${event.direction.toLowerCase()}"></span>
      <div>
        <strong>${eventTitle(event)}</strong>
        <p>${eventBody(event)}</p>
        ${event.stop_loss_display ? `<small class="stop-detail">${event.stop_method} · ${event.backtest_samples || 0} comparable prior alert(s). ${event.stop_explanation || ""}</small>` : ""}
      </div>
      <time>${event.provider?.toUpperCase() || ""} · ${formatTime(event.created_at)}</time>
    </article>`).join("");
}

function saveEventCursor() {
  localStorage.setItem("tradeM.lastEventId", String(state.lastEventId));
}

function processEvents(newEvents, { notifyNew = true } = {}) {
  if (!newEvents.length) return;
  const delayed = [];
  for (const event of newEvents) {
    const eventId = Number(event.id);
    const unseen = eventId > state.lastEventId;
    if (!state.events.some(existing => Number(existing.id) === eventId)) {
      state.events.push(event);
    }
    if (notifyNew && unseen) {
      const age = Date.now() - new Date(event.created_at).getTime();
      if (Number.isFinite(age) && age > 2 * 60 * 1000) delayed.push(event);
      else notify(event);
    }
    state.lastEventId = Math.max(state.lastEventId, eventId);
  }
  saveEventCursor();
  renderEvents();
  if (notifyNew && delayed.length) {
    notifySystem(
      `Trade M recovered ${delayed.length} missed alert${delayed.length === 1 ? "" : "s"}`,
      "The live page was interrupted. Open Trade M and review the alert log.",
      `trade-m-recovered-${state.lastEventId}`,
    );
    showMessage(`${delayed.length} older alert${delayed.length === 1 ? " was" : "s were"} recovered after a delivery interruption.`, "warn");
  }
}

async function initializeEvents() {
  try {
    const snapshot = await api("/api/events/snapshot", { timeoutMs: 8000 });
    const persistedCursor = state.lastEventId;
    const latestId = Number(snapshot.latest_id || 0);
    state.events = snapshot.events || [];
    state.lastEventId = persistedCursor > latestId ? latestId : persistedCursor;
    processEvents(state.events, { notifyNew: true });
    state.lastEventId = Math.max(state.lastEventId, latestId);
    saveEventCursor();
    state.eventsLoaded = true;
  } catch (error) {
    showMessage(`Alert history sync failed: ${error.message}`, "error");
  } finally {
    state.eventsInitialized = true;
    connectEventStream();
    syncEventBackfill();
  }
}

function connectEventStream() {
  if (!("EventSource" in window)) {
    renderDeliveryHealth("Live streaming is unsupported; safety backfill is active.", "warn");
    return;
  }
  if (state.eventStream) state.eventStream.close();
  const source = new EventSource(`/api/events/stream?after_id=${state.lastEventId}`);
  state.eventStream = source;
  source.onopen = () => {
    state.alertChannelConnected = true;
    renderDeliveryHealth();
  };
  source.addEventListener("alert", (message) => {
    try {
      processEvents([JSON.parse(message.data)], { notifyNew: true });
      state.lastEventSyncAt = new Date();
    } catch (error) {
      showMessage(`A live alert could not be read: ${error.message}`, "error");
    }
  });
  source.onerror = () => {
    state.alertChannelConnected = false;
    renderDeliveryHealth();
  };
}

async function syncEventBackfill() {
  if (state.eventSyncInFlight) return;
  state.eventSyncInFlight = true;
  try {
    const events = await api(`/api/events?after_id=${state.lastEventId}&limit=500`, {
      timeoutMs: 6000,
    });
    processEvents(events, { notifyNew: true });
    state.lastEventSyncAt = new Date();
  } catch (error) {
    if (!state.alertChannelConnected) {
      renderDeliveryHealth(`Alert delivery offline: ${error.message}`, "error");
    }
  } finally {
    state.eventSyncInFlight = false;
  }
}

async function refresh() {
  try {
    const [status, rules] = await Promise.all([
      api("/api/status", { timeoutMs: 8000 }), api("/api/rules", { timeoutMs: 8000 })
    ]);
    renderStatus(status);
    state.rules = rules;
    renderRules();
  } catch (error) {
    showMessage(error.message, "error");
  }
}

let searchTimer;
$("#symbol").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const query = $("#symbol").value.trim();
  const provider = $("#provider").value;
  if (!state.providers[provider]?.authenticated || query.length < 2) {
    $("#suggestions").classList.add("hidden");
    return;
  }
  searchTimer = setTimeout(async () => {
    try {
      const exchange = $("#exchange").value;
      const results = await api(`/api/instruments/search?q=${encodeURIComponent(query)}&exchange=${exchange}&provider=${provider}`);
      const box = $("#suggestions");
      box.innerHTML = results.map(item => `
        <button type="button" data-symbol="${item.tradingsymbol}">
          <strong>${item.tradingsymbol}</strong><span>${item.name || "Equity"}</span>
        </button>`).join("");
      box.classList.toggle("hidden", !results.length);
    } catch (error) { showMessage(error.message, "error"); }
  }, 300);
});

$("#suggestions").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-symbol]");
  if (!button) return;
  $("#symbol").value = button.dataset.symbol;
  $("#suggestions").classList.add("hidden");
});

$("#notificationButton").addEventListener("click", async () => {
  if (!("Notification" in window)) {
    showMessage("This browser does not support desktop notifications.", "error");
    return;
  }
  const permission = await Notification.requestPermission();
  $("#notificationCopy").textContent = permission === "granted"
    ? "Desktop alerts are enabled. Keep this page and the Trade M terminal open."
    : "Notifications were not allowed. Enable them in the browser site settings.";
  renderDeliveryHealth();
  if (permission === "granted") {
    notifySystem(
      "Trade M is ready",
      "Live crossing alerts are enabled. If you can see this, browser delivery works.",
      "trade-m-ready",
    );
  }
});

$("#testNotificationButton").addEventListener("click", () => {
  const delivered = notifySystem(
    "Trade M test alert",
    "Browser notification delivery is working.",
    `trade-m-test-${Date.now()}`,
  );
  showMessage(
    delivered
      ? "Test alert sent. Confirm that Windows displayed it."
      : "The browser could not create the test alert.",
    delivered ? "success" : "error",
  );
});

$("#ruleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const provider = $("#provider").value;
  if (!state.providers[provider]?.authenticated) {
    showMessage(`Sign in to ${providerTitle(provider)} first.`, "error");
    return;
  }
  button.disabled = true;
  button.textContent = "Loading history…";
  try {
    await api("/api/rules", {
      method: "POST",
      body: JSON.stringify({
        provider,
        exchange: $("#exchange").value,
        tradingsymbol: $("#symbol").value,
        percentage: $("#percentage").value,
      }),
      timeoutMs: 60000,
    });
    showMessage("Rule is active for today. Live monitoring has started.", "success");
    $("#symbol").value = "";
    $("#percentage").value = "";
    await refresh();
  } catch (error) { showMessage(error.message, "error"); }
  finally {
    button.disabled = false;
    button.textContent = "Calculate & monitor";
  }
});

function renderNiftyRows(symbols) {
  const common = $("#niftyPercentage").value;
  $("#niftyRows").innerHTML = symbols.map(symbol => `
    <tr>
      <td><strong>${symbol}</strong></td>
      <td><div class="percent-wrap table-percent"><input data-nifty-symbol="${symbol}" type="number" min="0.01" max="50" step="0.01" value="${common}"><span>%</span></div></td>
    </tr>`).join("");
  $("#niftyTableWrap").classList.remove("hidden");
  $("#addNiftyButton").classList.remove("hidden");
}

$("#loadNiftyButton").addEventListener("click", async () => {
  const button = $("#loadNiftyButton");
  button.disabled = true;
  button.textContent = "Loading…";
  try {
    const result = await api("/api/nifty50?refresh=true");
    state.niftySymbols = result.symbols;
    renderNiftyRows(result.symbols);
    $("#niftySource").textContent = `${result.count} constituents · Source: ${result.source}`;
  } catch (error) { showMessage(error.message, "error"); }
  finally {
    button.disabled = false;
    button.textContent = "Reload Nifty 50";
  }
});

$("#niftyPercentage").addEventListener("change", () => {
  const value = $("#niftyPercentage").value;
  document.querySelectorAll("input[data-nifty-symbol]").forEach(input => { input.value = value; });
});

$("#addNiftyButton").addEventListener("click", async () => {
  const provider = $("#niftyProvider").value;
  const common = $("#niftyPercentage").value;
  if (!state.providers[provider]?.authenticated) {
    showMessage(`Sign in to ${providerTitle(provider)} first.`, "error");
    return;
  }
  if (!common) {
    showMessage("Enter the common percentage first.", "error");
    return;
  }
  const percentages = {};
  for (const input of document.querySelectorAll("input[data-nifty-symbol]")) {
    if (!input.value) {
      showMessage(`Enter a percentage for ${input.dataset.niftySymbol}.`, "error");
      return;
    }
    percentages[input.dataset.niftySymbol] = input.value;
  }
  const button = $("#addNiftyButton");
  button.disabled = true;
  button.textContent = "Preparing 50 histories…";
  try {
    const result = await api("/api/rules/nifty50", {
      method: "POST",
      body: JSON.stringify({ provider, common_percentage: common, percentages }),
      timeoutMs: 180000,
    });
    const failureNames = result.failures.slice(0, 4).map(item => item.symbol).join(", ");
    const suffix = result.failed ? ` ${result.failed} failed${failureNames ? `: ${failureNames}` : ""}.` : "";
    showMessage(`${result.created} Nifty rules are active.${suffix}`, result.failed ? "warn" : "success");
    await refresh();
  } catch (error) { showMessage(error.message, "error"); }
  finally {
    button.disabled = false;
    button.textContent = "Add all 50";
  }
});

$("#excelImportForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const provider = $("#excelProvider").value;
  const file = $("#excelWorkbook").files[0];
  if (!state.providers[provider]?.authenticated) {
    showMessage(`Sign in to ${providerTitle(provider)} first.`, "error");
    return;
  }
  if (!file) {
    showMessage("Choose an .xlsx workbook first.", "error");
    return;
  }
  const button = $("#excelImportButton");
  const resultBox = $("#excelImportResult");
  const form = new FormData();
  form.append("provider", provider);
  form.append("workbook", file);
  button.disabled = true;
  button.textContent = "Validating & calculating…";
  resultBox.className = "import-result hidden";
  try {
    const result = await api("/api/rules/import", {
      method: "POST", body: form, timeoutMs: 180000,
    });
    const summary = `${result.created} stock${result.created === 1 ? "" : "s"} added, ${result.failed} failed, ${result.skipped} skipped.`;
    resultBox.replaceChildren();
    const heading = document.createElement("strong");
    heading.textContent = summary;
    resultBox.append(heading);
    if (result.failures.length) {
      const list = document.createElement("ul");
      for (const item of result.failures.slice(0, 8)) {
        const row = document.createElement("li");
        row.textContent = `Row ${item.row}: ${item.exchange}:${item.symbol} — ${item.error}`;
        list.append(row);
      }
      resultBox.append(list);
    }
    resultBox.className = `import-result ${result.failed ? "warn" : "success"}`;
    showMessage(summary, result.failed ? "warn" : "success");
    if (result.created) {
      $("#excelWorkbook").value = "";
      await refresh();
    }
  } catch (error) {
    resultBox.textContent = error.message;
    resultBox.className = "import-result warn";
    showMessage(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Upload & monitor";
  }
});

$("#rules").addEventListener("click", async (event) => {
  const save = event.target.closest("button[data-save]");
  const toggle = event.target.closest("button[data-active]");
  try {
    if (save) {
      const input = document.querySelector(`input[data-percentage="${save.dataset.save}"]`);
      await api(`/api/rules/${save.dataset.save}`, {
        method: "PATCH", body: JSON.stringify({ percentage: input.value })
      });
      showMessage("Percentage and levels updated for today.", "success");
    } else if (toggle) {
      const active = toggle.dataset.next === "true";
      await api(`/api/rules/${toggle.dataset.active}`, {
        method: "PATCH", body: JSON.stringify({ active })
      });
      showMessage(active ? "Monitoring started." : "Monitoring paused.", "success");
    } else {
      return;
    }
    await refresh();
  } catch (error) { showMessage(error.message, "error"); }
});

async function setAllRules(active) {
  try {
    const result = await api("/api/rules/bulk-status", {
      method: "POST", body: JSON.stringify({ active })
    });
    showMessage(`${result.affected} rules ${active ? "started" : "paused"}.`, "success");
    await refresh();
  } catch (error) { showMessage(error.message, "error"); }
}

$("#startAllButton").addEventListener("click", () => setAllRules(true));
$("#pauseAllButton").addEventListener("click", () => setAllRules(false));

const query = new URLSearchParams(window.location.search);
if (query.get("error")) showMessage(query.get("error"), "error");
if (query.get("login")) {
  const provider = providerTitle(query.get("login"));
  showMessage(`${provider} connected successfully.`, "success");
}
history.replaceState({}, "", "/");

if (document.wasDiscarded) {
  showMessage(
    "The browser previously discarded the Trade M tab. Disable Memory Saver for 127.0.0.1 so live alerts are not suspended.",
    "error",
  );
}

if ("Notification" in window && Notification.permission === "granted") {
  $("#notificationCopy").textContent = "Desktop alerts are enabled. Keep this page and the Trade M terminal open.";
}
renderDeliveryHealth();

async function refreshLoop() {
  try {
    await refresh();
  } finally {
    window.setTimeout(refreshLoop, 2500);
  }
}

refreshLoop();
initializeEvents();
refreshMarketMovement();
window.setInterval(refreshMarketMovement, 60000);
window.setInterval(syncEventBackfill, 5000);

function recoverLivePage() {
  refresh();
  if (!state.eventsInitialized) return;
  syncEventBackfill();
  if (
    "EventSource" in window
    && (!state.eventStream || state.eventStream.readyState === EventSource.CLOSED)
  ) {
    connectEventStream();
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") recoverLivePage();
});
document.addEventListener("resume", recoverLivePage);
window.addEventListener("focus", recoverLivePage);
window.addEventListener("online", recoverLivePage);
window.addEventListener("pageshow", recoverLivePage);
window.addEventListener("beforeunload", () => state.eventStream?.close());
