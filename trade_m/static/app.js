const state = {
  rules: [],
  events: [],
  lastEventId: Number(localStorage.getItem("tradeM.lastEventId") || 0),
  eventsLoaded: false,
  authenticated: false,
};

const $ = (selector) => document.querySelector(selector);

function showMessage(text, kind = "info") {
  const node = $("#message");
  node.textContent = text;
  node.className = `message ${kind}`;
  window.setTimeout(() => node.classList.add("hidden"), 7000);
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

function renderStatus(status) {
  state.authenticated = status.authenticated;
  const market = $("#marketBadge");
  market.textContent = status.market_open ? "Market session open" : "Market session closed";
  market.className = `badge ${status.market_open ? "good" : "neutral"}`;

  const feed = $("#feedBadge");
  feed.textContent = status.monitor.connected ? "Live feed connected" : "Feed disconnected";
  feed.className = `badge ${status.monitor.connected ? "good" : "warn"}`;
  $("#serverTime").textContent = `IST ${formatTime(status.server_time)}`;

  const loginButton = $("#loginButton");
  const loginCopy = $("#loginCopy");
  if (!status.kite_configured) {
    loginCopy.textContent = "Add KITE_API_KEY and KITE_API_SECRET to .env, then restart the app.";
    loginButton.textContent = "Configuration required";
    loginButton.classList.add("disabled");
  } else if (status.authenticated) {
    loginCopy.textContent = `Connected${status.user_name ? ` as ${status.user_name}` : ""}. The session expires by the next morning.`;
    loginButton.textContent = "Reconnect Zerodha";
    loginButton.classList.remove("disabled");
  }
  if (status.monitor.last_error) showMessage(status.monitor.last_error, "warn");
}

function renderRules() {
  const container = $("#rules");
  if (!state.rules.length) {
    container.className = "rules-list empty-state";
    container.textContent = "No active rules yet.";
    return;
  }
  container.className = "rules-list";
  container.innerHTML = state.rules.map(rule => `
    <article class="rule-row">
      <div class="symbol-block">
        <span class="exchange">${rule.exchange}</span>
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
      <button class="icon-button" data-delete="${rule.id}" title="Stop monitoring">×</button>
    </article>`).join("");
}

function eventTitle(event) {
  return `${event.exchange}:${event.tradingsymbol} ${event.direction.toLowerCase()} level crossed`;
}

function eventBody(event) {
  return `₹${event.threshold_display} was inside the ${formatTime(event.candle_start)}–${formatTime(event.candle_end)} candle (low ₹${event.candle_low}, high ₹${event.candle_high}).`;
}

function notify(event) {
  if (Notification.permission !== "granted") return;
  const notification = new Notification(eventTitle(event), {
    body: eventBody(event),
    tag: `trade-m-${event.id}`,
    requireInteraction: true,
  });
  notification.onclick = () => window.focus();
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
      <time>${formatTime(event.created_at)}</time>
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
  if (!state.authenticated || query.length < 2) {
    $("#suggestions").classList.add("hidden");
    return;
  }
  searchTimer = setTimeout(async () => {
    try {
      const exchange = $("#exchange").value;
      const results = await api(`/api/instruments/search?q=${encodeURIComponent(query)}&exchange=${exchange}`);
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
  if (permission === "granted") new Notification("Trade M is ready", { body: "Live crossing alerts are enabled." });
});

$("#ruleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  button.textContent = "Calculating…";
  try {
    await api("/api/rules", {
      method: "POST",
      body: JSON.stringify({
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

$("#rules").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-delete]");
  if (!button || !confirm("Stop monitoring this rule for today?")) return;
  try {
    await api(`/api/rules/${button.dataset.delete}`, { method: "DELETE" });
    showMessage("Monitoring stopped.", "success");
    await refresh();
  } catch (error) { showMessage(error.message, "error"); }
});

const query = new URLSearchParams(window.location.search);
if (query.get("error")) showMessage(query.get("error"), "error");
if (query.get("login") === "success") showMessage("Zerodha connected successfully.", "success");
history.replaceState({}, "", "/");

if ("Notification" in window && Notification.permission === "granted") {
  $("#notificationCopy").textContent = "Desktop alerts are enabled. Keep this page open during market hours.";
}

refresh();
setInterval(refresh, 2500);
