const state = {
  rules: [],
  events: [],
  niftySymbols: [],
  providers: {},
  providerErrors: {},
  feedIssues: {},
  lastEventId: Number(localStorage.getItem("tradeM.lastEventId") || 0),
  eventsLoaded: false,
};

const $ = (selector) => document.querySelector(selector);

function showMessage(text, kind = "info") {
  const node = $("#message");
  node.textContent = text;
  node.className = `message ${kind}`;
  window.setTimeout(() => node.classList.add("hidden"), 9000);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
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
    if (status.monitor.recovering) feed = "Live feed connected; recovering missed candles.";
    else if (status.monitor.stale) feed = "Feed connected but no recent ticks were received.";
    else if (status.monitor.connected) feed = "Live feed connected.";
    copy.textContent = `${feed}${status.user_name ? ` Account: ${status.user_name}.` : ""}`;
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
    const issue = monitor.stale
      ? `${providerTitle(name)} feed is stale; no recent market ticks were received.`
      : (!monitor.connected ? `${providerTitle(name)} live feed is disconnected.` : null);
    if (issue && state.feedIssues[name] !== issue) {
      state.feedIssues[name] = issue;
      showMessage(issue, "error");
      notifySystem(`${providerTitle(name)} feed problem`, issue, `trade-m-feed-${name}`);
    } else if (!issue) {
      delete state.feedIssues[name];
    }
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
  return `${event.exchange}:${event.tradingsymbol} ${event.direction.toLowerCase()} level crossed`;
}

function eventBody(event) {
  const movement = event.direction === "UPPER" ? "retraced downward through" : "rebounded upward through";
  return `Price ${movement} ₹${event.threshold_display} in the completed ${formatTime(event.candle_start)}–${formatTime(event.candle_end)} candle.`;
}

function notifySystem(title, body, tag) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  const notification = new Notification(title, { body, tag, requireInteraction: true });
  notification.onclick = () => window.focus();
}

function notify(event) {
  notifySystem(eventTitle(event), eventBody(event), `trade-m-${event.id}`);
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
      <div><strong>${eventTitle(event)}</strong><p>${eventBody(event)}</p></div>
      <time>${event.provider?.toUpperCase() || ""} · ${formatTime(event.created_at)}</time>
    </article>`).join("");
}

async function refresh() {
  try {
    const eventCursor = state.eventsLoaded ? state.lastEventId : 0;
    const [status, rules, newEvents] = await Promise.all([
      api("/api/status"), api("/api/rules"), api(`/api/events?after_id=${eventCursor}`)
    ]);
    renderStatus(status);
    state.rules = rules;
    renderRules();
    if (newEvents.length) {
      for (const event of newEvents) {
        if (!state.events.some(existing => existing.id === event.id)) state.events.push(event);
        if (event.id > state.lastEventId) notify(event);
        state.lastEventId = Math.max(state.lastEventId, event.id);
      }
      localStorage.setItem("tradeM.lastEventId", String(state.lastEventId));
      renderEvents();
    }
    state.eventsLoaded = true;
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
    ? "Desktop alerts are enabled. Keep this page open during market hours."
    : "Notifications were not allowed. Enable them in the browser site settings.";
  if (permission === "granted") {
    new Notification("Trade M is ready", { body: "Live crossing alerts are enabled." });
  }
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
  button.textContent = "Calculating…";
  try {
    await api("/api/rules", {
      method: "POST",
      body: JSON.stringify({
        provider,
        exchange: $("#exchange").value,
        tradingsymbol: $("#symbol").value,
        percentage: $("#percentage").value,
      }),
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
  button.textContent = "Fetching 50 reference closes…";
  try {
    const result = await api("/api/rules/nifty50", {
      method: "POST",
      body: JSON.stringify({ provider, common_percentage: common, percentages }),
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

if ("Notification" in window && Notification.permission === "granted") {
  $("#notificationCopy").textContent = "Desktop alerts are enabled. Keep this page open during market hours.";
}

async function refreshLoop() {
  await refresh();
  window.setTimeout(refreshLoop, 1000);
}

refreshLoop();
