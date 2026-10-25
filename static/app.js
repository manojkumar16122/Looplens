/* LoopLens dashboard: polls the API, renders activity, token chart, and alerts. */
(() => {
  "use strict";

  const POLL_INTERVAL_MS = 2000;
  const SIMULATION_CALLS = 8;
  const SIMULATION_TOKENS = 2500;
  const SIMULATION_DELAY_MS = 700;

  const $ = (id) => document.getElementById(id);
  const els = {
    connection: $("connection"),
    sessionLabel: $("session-label"),
    sessionStatus: $("session-status"),
    callCount: $("call-count"),
    activity: $("activity"),
    statTokens: $("stat-tokens"),
    statCost: $("stat-cost"),
    statBlocked: $("stat-blocked"),
    costBar: $("cost-bar"),
    costSub: $("cost-sub"),
    alerts: $("alerts"),
    chartCanvas: $("token-chart"),
    simulateBtn: $("simulate-btn"),
    resetBtn: $("reset-btn"),
    toast: $("toast"),
  };

  const state = {
    sessionId: null,
    lastKey: null,
    chart: null,
    polling: false,
    simulating: false,
    toastTimer: null,
  };

  // Chart colors come from the stylesheet, so restyling only requires editing style.css.
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function api(path, { method = "GET", body } = {}) {
    const init = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) init.body = JSON.stringify(body);
    const response = await fetch(path, init);
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const data = await response.json();
        detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      } catch (_) { /* keep statusText */ }
      throw new Error(`${response.status}: ${detail}`);
    }
    return response.json();
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  const formatUsd = (value) => `$${Number(value).toFixed(4)}`;

  function formatTime(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime())
      ? ""
      : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function showToast(message, isError = false) {
    els.toast.textContent = message;
    els.toast.className = `toast show${isError ? " error" : ""}`;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { els.toast.className = "toast"; }, 2800);
  }

  function setConnection(online) {
    els.connection.textContent = online ? "LIVE" : "OFFLINE";
    els.connection.className = `pill ${online ? "pill-ok" : "pill-alert"}`;
  }

  function renderActivity(calls) {
    const list = els.activity;
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    list.replaceChildren();

    if (!calls.length) {
      list.append(el("li", "empty", "run a simulation to see calls appear here"));
    }
    for (const call of calls) {
      const item = el("li", `activity-item ${call.status}`);
      const body = el("div", "activity-body");
      body.append(
        el("div", "activity-tool", call.tool_name),
        el("div", "activity-args", JSON.stringify(call.arguments))
      );
      item.append(
        el("span", `badge badge-${call.status}`, `[${call.status}]`),
        body,
        el("div", "activity-meta", `${formatTime(call.timestamp)} · ${call.tokens}t`)
      );
      list.append(item);
    }
    if (nearBottom) list.scrollTop = list.scrollHeight;
  }

  function renderChart(series) {
    if (typeof Chart === "undefined") return;
    const labels = series.map((point) => `${point.call_index}`);
    const values = series.map((point) => point.cumulative_tokens);

    if (!state.chart) {
      const accent = cssVar("--accent");
      const grid = { color: cssVar("--line-2") };
      const ticks = {
        color: cssVar("--ink-3"),
        font: { family: "JetBrains Mono, monospace", size: 10 },
      };
      state.chart = new Chart(els.chartCanvas, {
        type: "line",
        data: {
          labels,
          datasets: [{
            label: "Cumulative tokens",
            data: values,
            borderColor: accent,
            backgroundColor: "transparent",
            borderWidth: 2,
            fill: false,
            stepped: "before",
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBackgroundColor: accent,
          }],
        },
        options: {
          animation: false,
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: cssVar("--surface-2"),
              borderColor: cssVar("--line"),
              borderWidth: 1,
              titleFont: { family: "JetBrains Mono, monospace", size: 11 },
              bodyFont: { family: "JetBrains Mono, monospace", size: 11 },
              displayColors: false,
              cornerRadius: 0,
              padding: 10,
            },
          },
          scales: {
            x: { ticks, grid, border: { display: false }, title: { display: true, text: "CALL #", color: cssVar("--ink-3"), font: { family: "JetBrains Mono, monospace", size: 10 } } },
            y: { beginAtZero: true, ticks, grid, border: { display: false } },
          },
        },
      });
      return;
    }
    state.chart.data.labels = labels;
    state.chart.data.datasets[0].data = values;
    state.chart.update("none");
  }

  function renderAlerts(alerts) {
    els.alerts.replaceChildren();
    if (!alerts.length) {
      els.alerts.append(el("div", "empty", "no alerts yet · loops and budget stops appear here"));
      return;
    }
    for (const alert of alerts) {
      const isCost = alert.alert_type === "COST";
      const card = el("article", `alert-card ${isCost ? "alert-cost" : "alert-loop"}`);
      const head = el("header", "alert-head");
      head.append(
        el("span", "alert-type", isCost ? "budget stop" : "loop detected"),
        el("span", "alert-time", formatTime(alert.timestamp))
      );
      card.append(head, el("p", "alert-diagnosis", alert.diagnosis));
      els.alerts.append(card);
    }
  }

  function renderSession(session, threshold) {
    if (!session) {
      els.sessionLabel.textContent = "NO SESSION";
      els.sessionStatus.textContent = "IDLE";
      els.sessionStatus.className = "pill pill-muted";
      els.callCount.textContent = "0";
      els.statTokens.textContent = "0";
      els.statCost.textContent = formatUsd(0);
      els.statBlocked.textContent = "0";
      els.costBar.style.width = "0%";
      els.costBar.classList.remove("danger");
      els.costSub.textContent = `budget ${formatUsd(threshold)}`;
      return;
    }

    const paused = session.status === "PAUSED";
    const pct = threshold > 0 ? Math.min(100, (session.total_cost_usd / threshold) * 100) : 0;

    els.sessionLabel.textContent = `SESSION ${session.session_id.slice(0, 8).toUpperCase()}`;
    els.sessionStatus.textContent = paused ? "PAUSED" : "ACTIVE";
    els.sessionStatus.className = `pill ${paused ? "pill-alert" : "pill-ok"}`;
    els.callCount.textContent = String(session.total_calls);
    els.statTokens.textContent = session.total_tokens.toLocaleString();
    els.statCost.textContent = formatUsd(session.total_cost_usd);
    els.statBlocked.textContent = String(session.blocked_calls);
    els.costBar.style.width = `${pct.toFixed(1)}%`;
    els.costBar.classList.toggle("danger", paused || pct >= 100);
    els.costSub.textContent = `${Math.round(pct)}% of ${formatUsd(threshold)} budget`;
  }

  function render(data) {
    renderSession(data.session, data.cost_threshold_usd);
    renderActivity(data.recent_calls);
    renderAlerts(data.alerts);
    renderChart(data.token_series);
  }

  async function refresh() {
    if (state.polling) return;
    state.polling = true;
    try {
      const query = state.sessionId ? `?session_id=${encodeURIComponent(state.sessionId)}` : "";
      const data = await api(`/dashboard${query}`);
      setConnection(true);
      if (data.session && !state.sessionId) state.sessionId = data.session.session_id;

      // Skip DOM work when nothing changed, which avoids flicker and keeps scroll position stable.
      const key = JSON.stringify(data);
      if (key !== state.lastKey) {
        state.lastKey = key;
        render(data);
      }
    } catch (_) {
      setConnection(false);
    } finally {
      state.polling = false;
    }
  }

  async function runSimulation() {
    if (state.simulating) return;
    state.simulating = true;
    els.simulateBtn.disabled = true;
    try {
      const session = await api("/session", { method: "POST" });
      state.sessionId = session.session_id;
      state.lastKey = null;
      showToast("simulation started");

      let blockedSeen = false;
      for (let i = 0; i < SIMULATION_CALLS; i++) {
        const decision = await api("/tool_call", {
          method: "POST",
          body: {
            session_id: state.sessionId,
            tool_name: "search_web",
            arguments: { query: "quarterly GPU pricing" },
            tokens: SIMULATION_TOKENS,
          },
        });
        if (decision.status === "blocked") blockedSeen = true;
        await refresh();
        if (decision.status === "blocked" && /paused/i.test(decision.reason || "")) break;
        await sleep(SIMULATION_DELAY_MS);
      }
      showToast(blockedSeen ? "loop blocked before it could repeat" : "simulation finished");
    } catch (err) {
      showToast(`simulation failed: ${err.message}`, true);
    } finally {
      state.simulating = false;
      els.simulateBtn.disabled = false;
      await refresh();
    }
  }

  async function resetDashboard() {
    if (!window.confirm("Reset all sessions, calls and alerts?")) return;
    try {
      await api("/reset", { method: "POST" });
      state.sessionId = null;
      state.lastKey = null;
      await refresh();
      showToast("dashboard reset");
    } catch (err) {
      showToast(`reset failed: ${err.message}`, true);
    }
  }

  function init() {
    els.simulateBtn.addEventListener("click", runSimulation);
    els.resetBtn.addEventListener("click", resetDashboard);
    refresh();
    setInterval(refresh, POLL_INTERVAL_MS);
  }

  init();
})();
