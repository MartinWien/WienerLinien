/* wl-pulse departure board.
 * Plain ES2020, no dependencies. Talks only to the wl-pulse API on the same
 * origin; refresh cadence (15 s board / 60 s stats+disruptions) sits well
 * inside the API's own rate limit and never touches Wiener Linien directly.
 */
"use strict";

const REFRESH_MS = 15_000;
const PENDING_RETRY_MS = 5_000;
const SLOW_REFRESH_MS = 60_000;
const QUICK_STATIONS = ["Karlsplatz", "Stephansplatz", "Praterstern", "Schwedenplatz"];

/* Vienna line identity colors (real-world brand colors; the badge always
 * contains the line name, so identity is never color-alone). */
const METRO_COLORS = {
  U1: "#d3312e", U2: "#9469a1", U3: "#dd7529", U4: "#3d9e48", U6: "#8d6b32",
};
function lineColor(line, vehicleType) {
  if (METRO_COLORS[line]) return METRO_COLORS[line];
  if ((vehicleType || "").includes("Tram")) return "#a02c3a";
  if (/^N\d/i.test(line)) return "#20315f";
  if ((vehicleType || "").includes("Bus")) return "#2a5387";
  return "#4a5561";
}

const $ = (id) => document.getElementById(id);
const state = {
  station: null, // {id, name}
  timers: { board: null, slow: null },
};

/* ---------- helpers ---------- */

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text == null ? "" : String(text);
  return div.innerHTML;
}

async function fetchJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`HTTP ${response.status} for ${url}`);
  return response.json();
}

function fmtTime(iso) {
  return new Date(iso).toLocaleTimeString("de-AT", { hour: "2-digit", minute: "2-digit" });
}

function debounce(fn, ms) {
  let handle;
  return (...args) => {
    clearTimeout(handle);
    handle = setTimeout(() => fn(...args), ms);
  };
}

/* ---------- search ---------- */

const searchInput = $("search-input");
const searchResults = $("search-results");

const runSearch = debounce(async (query) => {
  if (query.trim().length < 2) {
    searchResults.hidden = true;
    return;
  }
  try {
    const body = await fetchJSON(`/stations?search=${encodeURIComponent(query.trim())}&limit=8`);
    const items = body.data.items;
    searchResults.innerHTML = items.length
      ? items
          .map(
            (s) =>
              `<li data-id="${s.id}" data-name="${esc(s.name)}">` +
              `<span>${esc(s.name)}</span><span class="muted">${s.stop_count} Steige</span></li>`
          )
          .join("")
      : `<li class="muted">Keine Stationen gefunden</li>`;
    searchResults.hidden = false;
  } catch {
    searchResults.hidden = true;
  }
}, 300);

searchInput.addEventListener("input", (event) => runSearch(event.target.value));
searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    const first = searchResults.querySelector("li[data-id]");
    if (first) pickStation(first);
  } else if (event.key === "Escape") {
    searchResults.hidden = true;
  }
});
searchResults.addEventListener("click", (event) => {
  const item = event.target.closest("li[data-id]");
  if (item) pickStation(item);
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".search-box")) searchResults.hidden = true;
});

function pickStation(element) {
  searchResults.hidden = true;
  searchInput.value = "";
  selectStation({ id: Number(element.dataset.id), name: element.dataset.name });
}

/* ---------- quick chips ---------- */

async function buildQuickChips() {
  const container = $("quick-chips");
  for (const name of QUICK_STATIONS) {
    try {
      const body = await fetchJSON(`/stations?search=${encodeURIComponent(name)}&limit=1`);
      const station = body.data.items[0];
      if (!station) continue;
      const button = document.createElement("button");
      button.textContent = station.name;
      button.addEventListener("click", () =>
        selectStation({ id: station.id, name: station.name })
      );
      container.appendChild(button);
    } catch {
      /* chip is cosmetic; skip on error */
    }
  }
}

/* ---------- departure board ---------- */

function selectStation(station) {
  state.station = station;
  localStorage.setItem("wlp-station", JSON.stringify(station));
  history.replaceState(null, "", `#s=${station.id}`);
  $("empty-state").hidden = true;
  $("board-section").hidden = false;
  $("board-station-name").textContent = station.name;
  $("board-body").innerHTML = "";
  $("board-meta").innerHTML = "";
  clearTimeout(state.timers.board);
  clearTimeout(state.timers.slow);
  loadBoard();
  loadSlowSections();
}

function scheduleBoard(ms) {
  clearTimeout(state.timers.board);
  state.timers.board = setTimeout(loadBoard, ms);
}

async function loadBoard() {
  const station = state.station;
  if (!station) return;
  let body;
  try {
    body = await fetchJSON(`/stations/${station.id}/departures`);
  } catch (error) {
    showNotice(`API nicht erreichbar (${esc(error.message)}). Neuer Versuch…`, "error");
    scheduleBoard(REFRESH_MS);
    return;
  }
  if (state.station?.id !== station.id) return; // user switched meanwhile

  const { meta, data } = body;
  if (data.station_name && data.station_name !== station.name) {
    station.name = data.station_name;
    localStorage.setItem("wlp-station", JSON.stringify(station));
    $("board-station-name").textContent = station.name;
  }
  renderBoardMeta(meta);

  if (data.watch_status === "pending") {
    showNotice(
      `<span class="spinner"></span>&nbsp; ${esc(station.name)} wurde zur Live-Abfrage ` +
        `hinzugefügt — erste Abfahrten in ca. 30 Sekunden.`
    );
    $("board-body").innerHTML = "";
    scheduleBoard(PENDING_RETRY_MS);
    return;
  }

  hideNotice();
  if (data.departures.length === 0) {
    showNotice("Derzeit keine Abfahrten für diese Station gemeldet.");
  }
  $("board-body").innerHTML = data.departures.slice(0, 20).map(renderDepartureRow).join("");
  scheduleBoard(REFRESH_MS);
}

function renderBoardMeta(meta) {
  const badge = meta.stale
    ? `<span class="badge badge-stale">⚠ veraltet</span>`
    : `<span class="badge badge-live">● live</span>`;
  const fetched = meta.fetched_at ? `Stand ${fmtTime(meta.fetched_at)}` : "noch keine Daten";
  $("board-meta").innerHTML = `${badge}<span>${fetched}</span>`;
  if (meta.stale && meta.fetched_at) {
    showNotice(
      `Die Echtzeitdaten sind veraltet — angezeigt werden die letzten bekannten Abfahrten ` +
        `von ${fmtTime(meta.fetched_at)}.`
    );
  }
}

function delayStatus(departure) {
  const seconds = departure.delay_seconds;
  if (seconds == null) return { cls: "plan", label: "laut Plan" };
  if (seconds <= -60) return { cls: "early", label: `▼ ${Math.round(-seconds / 60)} min früher` };
  if (seconds <= 120) return { cls: "good", label: "● pünktlich" };
  const minutes = Math.round(seconds / 60);
  return seconds <= 300
    ? { cls: "warn", label: `▲ +${minutes} min` }
    : { cls: "bad", label: `▲ +${minutes} min` };
}

function renderDepartureRow(departure) {
  const status = delayStatus(departure);
  const color = lineColor(departure.line, departure.vehicle_type);
  const platform = departure.platform ? `Steig ${esc(departure.platform)}` : "";
  const countdown =
    departure.countdown_minutes <= 0
      ? `<td class="countdown">jetzt</td>`
      : `<td class="countdown">${departure.countdown_minutes}<span class="unit">min</span></td>`;
  return (
    `<tr>` +
    `<td><span class="line-badge" style="background:${color}">${esc(departure.line)}</span></td>` +
    `<td class="towards">${esc(departure.towards)}` +
    (platform ? `<span class="sub">${platform}</span>` : "") +
    `</td>` +
    `<td><span class="status ${status.cls}">${status.label}</span></td>` +
    countdown +
    `</tr>`
  );
}

function showNotice(html, kind) {
  const notice = $("board-notice");
  notice.innerHTML = html;
  notice.className = kind === "error" ? "notice error" : "notice";
  notice.hidden = false;
}

function hideNotice() {
  $("board-notice").hidden = true;
}

/* ---------- stats + disruptions (slow refresh) ---------- */

async function loadSlowSections() {
  await Promise.allSettled([loadStats(), loadDisruptions()]);
  clearTimeout(state.timers.slow);
  state.timers.slow = setTimeout(loadSlowSections, SLOW_REFRESH_MS);
}

async function loadStats() {
  const station = state.station;
  if (!station) return;
  const body = await fetchJSON(`/stations/${station.id}/stats?window=60`);
  if (state.station?.id !== station.id) return;
  const lines = body.data.per_line.filter((entry) => entry.with_realtime > 0).slice(0, 8);
  $("stats-section").hidden = lines.length === 0;
  $("stats-grid").innerHTML = lines
    .map((entry) => {
      const onTime = entry.on_time_percent;
      const avg = entry.avg_delay_seconds;
      const avgLabel =
        avg == null ? "" : avg >= 0 ? `Ø +${Math.round(avg)} s` : `Ø ${Math.round(avg)} s`;
      return (
        `<div class="stat-tile">` +
        `<div class="tile-head">` +
        `<span class="line-badge" style="background:${lineColor(entry.line, "")}">${esc(entry.line)}</span>` +
        `<span class="muted">${entry.with_realtime} Fahrten</span>` +
        `</div>` +
        `<div class="big">${onTime == null ? "–" : onTime.toFixed(0) + "%"}</div>` +
        `<div class="sub">pünktlich · ${avgLabel}</div>` +
        `<div class="meter"><span style="width:${onTime ?? 0}%"></span></div>` +
        `</div>`
      );
    })
    .join("");
}

async function loadDisruptions() {
  const body = await fetchJSON(`/disruptions`);
  const disruptions = body.data;
  $("disruptions-section").hidden = disruptions.length === 0;
  const list = $("disruptions-list");
  const renderItem = (d) =>
    `<li><div class="d-title">${esc(d.title)}</div>` +
    (d.description ? `<div class="d-desc">${esc(d.description)}</div>` : "") +
    (d.related_lines.length
      ? `<div class="d-lines">${d.related_lines
          .map(
            (line) =>
              `<span class="line-badge" style="background:${lineColor(line, "")}">${esc(line)}</span>`
          )
          .join("")}</div>`
      : "") +
    `</li>`;
  const visible = disruptions.slice(0, 3).map(renderItem).join("");
  const hiddenCount = disruptions.length - 3;
  list.innerHTML = visible;
  if (hiddenCount > 0) {
    const more = document.createElement("button");
    more.className = "show-more";
    more.textContent = `${hiddenCount} weitere anzeigen`;
    more.addEventListener("click", () => {
      list.innerHTML = disruptions.map(renderItem).join("");
    });
    list.appendChild(more);
  }
}

/* ---------- boot ---------- */

(function boot() {
  buildQuickChips();
  const fromHash = location.hash.match(/^#s=(\d+)$/);
  const saved = localStorage.getItem("wlp-station");
  if (fromHash) {
    // The name resolves from the first departures response.
    selectStation({ id: Number(fromHash[1]), name: "…" });
  } else if (saved) {
    try {
      selectStation(JSON.parse(saved));
    } catch {
      /* stale localStorage; ignore */
    }
  }
})();
