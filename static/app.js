(function () {
  const ACCENTS = { 1: "#10b981", 2: "#f59e0b" };

  const HF_BANDS = [
    { lbl: "1.8", min: 1800000, max: 2000000, def: 1840000, cwEnd: 1840000, digiEnd: 1843000 },
    { lbl: "3.5", min: 3500000, max: 3800000, def: 3707000, cwEnd: 3570000, digiEnd: 3600000 },
    { lbl: "5", min: 5351500, max: 5366500, def: 5357000, cwEnd: 5354000, digiEnd: 5360000 },
    { lbl: "7", min: 7000000, max: 7200000, def: 7074000, cwEnd: 7040000, digiEnd: 7078000 },
    { lbl: "10", min: 10100000, max: 10150000, def: 10136000, cwEnd: 10130000, digiEnd: 10142000 },
    { lbl: "14", min: 14000000, max: 14350000, def: 14200000, cwEnd: 14070000, digiEnd: 14099000 },
    { lbl: "18", min: 18068000, max: 18168000, def: 18100000, cwEnd: 18095000, digiEnd: 18110000 },
    { lbl: "21", min: 21000000, max: 21450000, def: 21074000, cwEnd: 21070000, digiEnd: 21149000 },
    { lbl: "24", min: 24890000, max: 24990000, def: 24915000, cwEnd: 24915000, digiEnd: 24929000 },
    { lbl: "28", min: 28000000, max: 29700000, def: 28400000, cwEnd: 28070000, digiEnd: 28190000 },
    { lbl: "50", min: 50000000, max: 52000000, def: 50313000, cwEnd: 50100000, digiEnd: 50400000 },
    { lbl: "70", min: 70000000, max: 70500000, def: 70200000, cwEnd: 70090000, digiEnd: 70250000 },
  ];

  const VHF_BANDS = [
    { lbl: "144", min: 144000000, max: 146000000, def: 145725000, cwEnd: 144150000, digiEnd: 144400000 },
    { lbl: "430", min: 430000000, max: 440000000, def: 434650000, cwEnd: 432100000, digiEnd: 432500000 },
    { lbl: "1296", min: 1240000000, max: 1300000000, def: 1296200000, cwEnd: 1296150000, digiEnd: 1296400000 },
  ];

  let ws = null;
  let fullState = null;
  let activeSlot = 1;
  let palette = "classic";
  let stepHz = { 1: 100, 2: 100 };
  let vfoLocked = { 1: false, 2: false };
  let fineMode = { 1: false, 2: false };
  let bindingSlot = 0;
  let storeArmed = false;
  let vmRecArmed = false;
  let dualView = false;
  try { dualView = localStorage.getItem("rig_dual_view_v1") === "1"; } catch (_) {}
  let wfTrack = false;
  try { wfTrack = localStorage.getItem("rig_wf_track_v1") === "1"; } catch (_) {}
  let cwMacros = ["", "", "", ""];
  try {
    const savedCw = JSON.parse(localStorage.getItem("rig_cw_macros_v1") || "[]");
    if (Array.isArray(savedCw)) cwMacros = [0, 1, 2, 3].map((i) => String(savedCw[i] || ""));
  } catch (_) {}
  let cwEditingSlot = 0;
  let sMeterStyle = 0;
  try { sMeterStyle = Math.max(0, Math.min(3, Number(localStorage.getItem("rig_smeter_style_v1") || 0))); } catch (_) {}
  let uiPrefsSynced = false;
  const dualSuggestedRep = { 1: null, 2: null };
  const dualMatchedReps = { 1: [], 2: [] };

  const REP_CACHE_KEY = "nordic_repeaters_cache_v2";
  try { localStorage.removeItem("nordic_repeaters_cache_v1"); } catch (_) {}
  const REP_CACHE_TTL_MS = 30 * 24 * 60 * 60 * 1000;
  let repeatersList = [];
  let repeatersCachedAt = 0;
  let repeatersFetching = false;
  let currentSuggestedRep = null;
  let currentMatchedReps = [];
  let closestQrvWaterfallList = [];
  let closestQrvCacheKey = "";

  function getClosestQrvRepeatersForWaterfall(qthLoc) {
    const key = `${qthLoc || "KP20"}|${repeatersList.length}|${repeatersCachedAt}`;
    if (key === closestQrvCacheKey) return closestQrvWaterfallList;
    const byFreq = new Map();
    for (let i = 0; i < repeatersList.length; i++) {
      const r = repeatersList[i];
      if (r.qrv === false || r.rx_hz <= 0) continue;
      const db = calcDistBearing(qthLoc, r.lat, r.lon);
      const distKm = db.distKm != null ? db.distKm : 99999;
      const freqKey = Math.round(r.rx_hz / 1000);
      const prev = byFreq.get(freqKey);
      if (!prev || distKm < prev.distKm) {
        byFreq.set(freqKey, { ...r, distKm, bearingDeg: db.bearingDeg });
      }
    }
    closestQrvWaterfallList = Array.from(byFreq.values()).sort((a, b) => a.rx_hz - b.rx_hz);
    closestQrvCacheKey = key;
    return closestQrvWaterfallList;
  }

  function sqlPctToSUnits(sqlPct) {
    const p = Math.max(0, Math.min(100, Number(sqlPct || 0)));
    if (p < 34) return null;
    if (p <= 50) return 1 + (p - 34) / 4;
    return 5 + (p - 50) / 5;
  }

  function isVhfUhfRadio(r) {
    const curHz = (r?.active_band === "SUB" ? r.sub?.freq_hz : r?.main?.freq_hz) || r?.main?.freq_hz || 0;
    return Boolean(r?.supports_dual_rx) || curHz >= 30000000;
  }

  function getWaterfallStepHz(r, fine) {
    if (fine) return 50;
    return isVhfUhfRadio(r) ? 12500 : 1000;
  }

  function maidenheadToLatLon(loc) {
    const s = String(loc || "").trim().toUpperCase();
    if (s.length < 4) return null;
    const fLon = s.charCodeAt(0) - 65;
    const fLat = s.charCodeAt(1) - 65;
    const sLon = parseInt(s[2], 10);
    const sLat = parseInt(s[3], 10);
    if (isNaN(fLon) || isNaN(fLat) || isNaN(sLon) || isNaN(sLat)) return null;
    let lon = -180 + fLon * 20 + sLon * 2;
    let lat = -90 + fLat * 10 + sLat * 1;
    if (s.length >= 6 && s[4] >= "A" && s[4] <= "X" && s[5] >= "A" && s[5] <= "X") {
      lon += ((s.charCodeAt(4) - 65) + 0.5) * (2 / 24);
      lat += ((s.charCodeAt(5) - 65) + 0.5) * (1 / 24);
    } else {
      lon += 1.0;
      lat += 0.5;
    }
    return { lat, lon };
  }

  function calcDistBearing(qthLoc, lat2, lon2) {
    if (lat2 == null || lon2 == null) return { distKm: null, bearingDeg: null };
    const p1 = maidenheadToLatLon(qthLoc || "KP20");
    if (!p1) return { distKm: null, bearingDeg: null };
    const rad = Math.PI / 180;
    const rLat1 = p1.lat * rad, rLat2 = lat2 * rad;
    const dLat = (lat2 - p1.lat) * rad, dLon = (lon2 - p1.lon) * rad;
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(rLat1) * Math.cos(rLat2) * Math.sin(dLon / 2) ** 2;
    const distKm = Math.round(2 * 6371 * Math.asin(Math.min(1, Math.sqrt(a))));
    const y = Math.sin(dLon) * Math.cos(rLat2);
    const x = Math.cos(rLat1) * Math.sin(rLat2) - Math.sin(rLat1) * Math.cos(rLat2) * Math.cos(dLon);
    const bearingDeg = Math.round(((Math.atan2(y, x) / rad) + 360) % 360);
    return { distKm, bearingDeg };
  }

  function formatRepTone(rep) {
    if (rep.ctcss_hz > 0) {
      return `${rep.tone_type || "CTCSS"} ${Number(rep.ctcss_hz).toFixed(1)}`;
    }
    if (rep.tone_1750 || String(rep.tone_type || "").includes("1750")) return "1750Hz";
    return rep.mode && rep.mode.includes("D-STAR") ? "DV" : "CSQ";
  }

  function formatRepSummary(rep) {
    const rxMhz = (rep.rx_hz / 1e6).toFixed(4).replace(/0+$/, "").replace(/\.$/, ".0");
    const shiftStr = rep.shift_hz === 0 ? "Simplex" : `${rep.shift_hz > 0 ? "+" : ""}${(rep.shift_hz / 1e6).toFixed(rep.shift_hz % 100000 === 0 ? 1 : 2)}M`;
    const distStr = rep.distKm != null ? ` · ${rep.distKm} km ${String(rep.bearingDeg).padStart(3, "0")}°` : "";
    const qthStr = rep.qth ? ` (${rep.qth})` : "";
    const qrvStr = rep.qrv === false ? " [OFF]" : "";
    return `${rep.callsign}${qthStr} · ${rxMhz} ${shiftStr} ${formatRepTone(rep)}${distStr}${qrvStr}`;
  }

  function findMatchingRepeaters(freqHz, activeRep, qthLoc) {
    if (!freqHz || !repeatersList.length) return [];
    const matches = [];
    for (let i = 0; i < repeatersList.length; i++) {
      const r = repeatersList[i];
      if (r.rx_hz > 0 && Math.abs(r.rx_hz - freqHz) <= 5000) {
        const db = calcDistBearing(qthLoc, r.lat, r.lon);
        const isActivated = Boolean(activeRep && activeRep.callsign === r.callsign && Math.abs(activeRep.rx_hz - r.rx_hz) <= 1000);
        matches.push({ ...r, distKm: db.distKm, bearingDeg: db.bearingDeg, isActivated });
      }
    }
    matches.sort((a, b) => {
      if (a.isActivated !== b.isActivated) return a.isActivated ? -1 : 1;
      const aQrv = a.qrv !== false;
      const bQrv = b.qrv !== false;
      if (aQrv !== bQrv) return aQrv ? -1 : 1;
      const da = a.distKm != null ? a.distKm : 99999;
      const db = b.distKm != null ? b.distKm : 99999;
      return da - db;
    });
    return matches;
  }

  function repBandOfHz(hz) {
    if (hz >= 28000000 && hz <= 30000000) return "10m";
    if (hz >= 50000000 && hz <= 54000000) return "6m";
    if (hz >= 144000000 && hz <= 148000000) return "2m";
    if (hz >= 430000000 && hz <= 440000000) return "70cm";
    if (hz >= 1240000000 && hz <= 1300000000) return "23cm";
    return "other";
  }

  function normalizeRepeaterList(rawList) {
    return (rawList || []).map((r) => {
      const rxHz = Number(r.rx_hz || 0);
      const shiftHz = Number(r.offset_hz != null ? r.offset_hz : (r.shift_hz || 0));
      const qrv = r.qrv != null ? Boolean(r.qrv) : (String(r.status || "QRV").toUpperCase() === "QRV");
      const qth = r.city || r.qth || "";
      return {
        ...r,
        rx_hz: rxHz,
        shift_hz: shiftHz,
        offset_hz: shiftHz,
        qrv,
        qth,
        band: r.band || repBandOfHz(rxHz),
      };
    });
  }

  async function loadRepeatersOnce(force = false) {
    if (repeatersFetching) return;
    if (!force && repeatersList.length > 0 && (Date.now() - repeatersCachedAt) < REP_CACHE_TTL_MS) {
      return;
    }
    if (!force) {
      try {
        const cached = JSON.parse(localStorage.getItem(REP_CACHE_KEY) || "null");
        const arr = cached?.repeaters || cached?.stations;
        if (cached && Array.isArray(arr) && arr.length > 0 && (Date.now() - (cached.cached_at || 0)) < REP_CACHE_TTL_MS) {
          repeatersList = normalizeRepeaterList(arr);
          repeatersCachedAt = cached.cached_at;
          if (fullState) renderAll();
          return;
        }
      } catch (_) {}
    }
    repeatersFetching = true;
    try {
      const res = await fetch(force ? "/api/repeaters?refresh=1" : "/api/repeaters");
      const data = await res.json();
      const raw = data.stations || data.repeaters;
      if (Array.isArray(raw)) {
        repeatersList = normalizeRepeaterList(raw);
        repeatersCachedAt = Date.now();
        try {
          localStorage.setItem(REP_CACHE_KEY, JSON.stringify({ cached_at: repeatersCachedAt, repeaters: repeatersList }));
        } catch (_) {}
        if (fullState) renderAll();
        if (document.getElementById("repeaters-backdrop")?.classList.contains("open")) {
          renderRepeaterModalTable();
        }
      }
    } catch (_) {
    } finally {
      repeatersFetching = false;
    }
  }

  const meterAnim = {
    1: { needle: 0, peak: 0, peakTs: 0 },
    2: { needle: 0, peak: 0, peakTs: 0 },
  };
  const bandMem = { 1: {}, 2: {} };

  let slotMemories = { 1: Array(6).fill(null), 2: Array(6).fill(null) };
  try {
    const saved = JSON.parse(localStorage.getItem("rig_cockpit_mem_v1") || "null");
    if (saved && saved[1] && saved[2]) slotMemories = saved;
  } catch (_) {}

  function saveMemories() {
    try {
      localStorage.setItem("rig_cockpit_mem_v1", JSON.stringify(slotMemories));
    } catch (_) {}
  }

  const scopeStates = {
    1: window.ScopeRenderer.createScopeState(),
    2: window.ScopeRenderer.createScopeState(),
  };

  const swrWarnState = {
    1: { wasTx: false, triggered: false, peak: 0, cur: 0, fadeStart: 0 },
    2: { wasTx: false, triggered: false, peak: 0, cur: 0, fadeStart: 0 },
  };

  function updateSwrWarnState(slot, r) {
    const w = swrWarnState[slot];
    if (!w || !r) return;
    const now = performance.now();
    const isTx = Boolean(r.tx);
    const scanActive = Boolean(r.swr_scan && r.swr_scan.active);
    if (isTx) {
      if (!w.wasTx) {
        w.triggered = false;
        w.peak = 0;
        w.cur = 0;
        w.fadeStart = 0;
      }
      w.wasTx = true;
      if (!scanActive) {
        const swr = Number(r.swr || 1.0);
        w.cur = swr;
        if (swr > 2.5) {
          w.triggered = true;
          if (swr > w.peak) w.peak = swr;
        }
      }
    } else {
      if (w.wasTx) {
        w.wasTx = false;
        if (w.triggered) {
          w.fadeStart = now;
        }
      }
      if (w.triggered && w.fadeStart > 0 && now - w.fadeStart >= 5000) {
        w.triggered = false;
        w.peak = 0;
        w.cur = 0;
        w.fadeStart = 0;
      }
    }
  }

  function getSwrWarnOverlay(slot, r) {
    const w = swrWarnState[slot];
    if (!w) return null;
    updateSwrWarnState(slot, r);
    if (!w.triggered) return null;
    const now = performance.now();
    if (w.wasTx) {
      return { active: true, tx: true, peak: w.peak, cur: w.cur, alpha: 1.0, remSec: 0 };
    }
    const elapsed = now - (w.fadeStart || now);
    if (elapsed >= 5000) {
      w.triggered = false;
      w.peak = 0;
      w.cur = 0;
      w.fadeStart = 0;
      return null;
    }
    return {
      active: true,
      tx: false,
      peak: w.peak,
      cur: w.cur,
      alpha: Math.max(0, 1 - elapsed / 5000),
      remSec: Math.max(0, (5000 - elapsed) / 1000),
    };
  }

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws/ui`);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "state") {
        fullState = msg.data;
        updateSwrWarnState(1, msg.data?.radios?.["1"]);
        updateSwrWarnState(2, msg.data?.radios?.["2"]);
        if (!uiPrefsSynced && msg.data?.ui) {
          uiPrefsSynced = true;
          dualView = Boolean(msg.data.ui.dual_view);
          wfTrack = Boolean(msg.data.ui.wf_track);
          if (Array.isArray(msg.data.ui.cw_macros) && msg.data.ui.cw_macros.some((x) => x)) {
            cwMacros = [0, 1, 2, 3].map((i) => String(msg.data.ui.cw_macros[i] || ""));
          }
          if (msg.data.ui.smeter_style != null) {
            sMeterStyle = Math.max(0, Math.min(3, Number(msg.data.ui.smeter_style || 0)));
          }
          try {
            localStorage.setItem("rig_dual_view_v1", dualView ? "1" : "0");
            localStorage.setItem("rig_wf_track_v1", wfTrack ? "1" : "0");
            localStorage.setItem("rig_cw_macros_v1", JSON.stringify(cwMacros));
            localStorage.setItem("rig_smeter_style_v1", String(sMeterStyle));
          } catch (_) {}
          document.getElementById("btn-wf-track")?.classList.toggle("active", wfTrack);
        }
        renderAll();
      } else if (msg.type === "scope") {
        const slot = msg.slot;
        if (scopeStates[slot]) {
          window.ScopeRenderer.pushSpectrumRow(
            scopeStates[slot],
            msg.bins,
            msg.center_hz,
            msg.span_hz,
            palette,
            wfTrack
          );
        }
      }
    };
    ws.onclose = () => setTimeout(connectWs, 1500);
  }

  function sendCmd(slot, action, params = {}) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "cmd", slot, action, params }));
    }
  }

  function getPersonalSql(slot) {
    const m = fullState?.windows_sql || {};
    const v = m[String(slot)] ?? m[slot];
    return v != null ? Number(v) : 15;
  }

  function fmtTabFreq(hz) {
    if (!hz) return "---";
    return (hz / 1e6).toFixed(3);
  }

  function formatFreqDigits(hz, slot, target) {
    const clamped = Math.max(0, Math.min(9999999999, Math.round(hz || 0)));
    const str = String(clamped).padStart(10, "0");
    const mhz = String(parseInt(str.slice(0, 4), 10));
    const khz = str.slice(4, 7);
    const hzPart = str.slice(7, 10);

    const specs = [];
    for (let i = 0; i < mhz.length; i++) {
      specs.push({ ch: mhz[i], step: Math.pow(10, 6 + (mhz.length - 1 - i)), muted: false });
    }
    specs.push({ ch: ".", step: 0 });
    for (let i = 0; i < 3; i++) {
      specs.push({ ch: khz[i], step: Math.pow(10, 5 - i), muted: false });
    }
    specs.push({ ch: ".", step: 0 });
    for (let i = 0; i < 3; i++) {
      specs.push({ ch: hzPart[i], step: Math.pow(10, 2 - i), muted: i === 2 });
    }

    return specs
      .map((d) =>
        d.step === 0
          ? `<span style="color:var(--muted)">${d.ch}</span>`
          : `<span class="freq-digit ${d.muted ? "freq-hz-muted" : ""}" data-digit-step="${d.step}" data-rig="${slot}" data-target="${target}">${d.ch}</span>`
      )
      .join("");
  }

  function formatS(sUnits) {
    const s = sUnits || 0;
    if (s <= 9) return `S${s.toFixed(0)}`;
    return `S9+${Math.round((s - 9) * 10)}`;
  }

  function findBandForFreq(bands, hz) {
    return bands.find((b) => hz >= b.min && hz <= b.max) || null;
  }

  function syncBandButtons(container, bands, matchedBand) {
    if (!container) return;
    const key = bands.map((b) => b.lbl).join(",");
    if (container.dataset.bandKey !== key) {
      container.dataset.bandKey = key;
      container.innerHTML = bands
        .map((b) => `<button class="cbtn band-btn" data-band-lbl="${b.lbl}">${b.lbl}</button>`)
        .join("");
    }
    const activeLbl = matchedBand ? matchedBand.lbl : "";
    container.querySelectorAll(".band-btn").forEach((btn) => {
      btn.classList.toggle("band-active", btn.dataset.bandLbl === activeLbl);
    });
  }

  function syncFreqDigits(el, freqHz, slot, target) {
    if (!el) return;
    const key = `${slot}:${target}:${freqHz || 0}`;
    if (el.dataset.freqKey !== key) {
      el.dataset.freqKey = key;
      el.innerHTML = formatFreqDigits(freqHz, slot, target);
    }
  }

  function syncRepeaterSelect(sel, matches, detailed) {
    if (!sel) return;
    if (matches.length <= 1) {
      sel.style.display = "none";
      sel.dataset.optKey = "";
      return;
    }
    sel.style.display = "inline-block";
    const optKey = matches.map((m) => `${m.callsign}:${m.distKm ?? ""}`).join("|");
    if (sel.dataset.optKey !== optKey) {
      sel.dataset.optKey = optKey;
      sel.innerHTML = matches
        .map((m, idx) =>
          detailed
            ? `<option value="${idx}">${m.callsign}${m.qth ? " · " + m.qth : ""}${m.distKm != null ? " (" + m.distKm + "km)" : ""}${m.qrv === false ? " OFF" : ""}</option>`
            : `<option value="${idx}">${m.callsign}${m.distKm != null ? " (" + m.distKm + "km)" : ""}</option>`
        )
        .join("");
      sel.value = "0";
    }
  }

  function setSliderVisual(el, val, min, max) {
    if (!el || document.activeElement === el) {
      // still update percentage CSS if user isn't mid-drag; if active, input listener updates it
    }
    const pct = Math.max(0, Math.min(100, ((val - min) / (max - min)) * 100));
    el.value = String(val);
    el.style.setProperty("--pct", `${pct}%`);
  }

  let appUpdatePending = false;

  function renderAll() {
    if (!fullState) return;
    const isClient = fullState.mode === "client";
    const upd = fullState.update || {};
    const updBtn = document.getElementById("btn-app-update");
    if (updBtn && !appUpdatePending) {
      const avail = Boolean(upd.update_available);
      updBtn.style.display = avail ? "inline-block" : "none";
      updBtn.classList.toggle("active", avail);
      if (avail) {
        const shortSha = String(upd.remote_sha || "").slice(0, 7);
        const msgTxt = upd.remote_msg ? `: ${upd.remote_msg}` : "";
        updBtn.textContent = "⬇ Update Available";
        updBtn.title = `GitHub (${upd.repo || "jorksx/RemoteHam"}) ${shortSha}${msgTxt}\nClick to download & update automatically`;
      }
    }
    const modeBadge = document.getElementById("mode-badge");
    modeBadge.textContent = isClient
      ? (fullState.client.connected ? `CLIENT (${fullState.client.server_ip})` : "CLIENT...")
      : (fullState.server.running ? `HOST :${fullState.server.port} (${fullState.server.clients})` : "HOST");

    document.getElementById("mobile-suppression-badge").style.display =
      fullState.windows_suppressed_by_mobile ? "inline-flex" : "none";

    // Live Windows Audio Health Diagnostics
    const ast = fullState.audio_status || {};
    const healthPill = document.getElementById("audio-health-pill");
    const winMutedAct = Boolean((fullState.windows_mute || {})[String(activeSlot)] ?? (fullState.windows_mute || {})[activeSlot]);
    const winVolAct = (fullState.windows_volume || {})[String(activeSlot)] ?? (fullState.windows_volume || {})[activeSlot] ?? 85;
    if (!ast.pc_out_ok) {
      healthPill.textContent = "AUDIO: NO OUTPUT";
      healthPill.className = "status-pill err mono";
    } else if (fullState.windows_suppressed_by_mobile) {
      healthPill.textContent = "AUDIO: MOBILE RX";
      healthPill.className = "status-pill warn mono";
    } else if (winMutedAct || winVolAct <= 0) {
      healthPill.textContent = winMutedAct ? `R${activeSlot} MUTED` : `R${activeSlot} VOL 0%`;
      healthPill.className = "status-pill warn mono";
    } else {
      const rmsKey = activeSlot === 1 ? ast.r1_rms : ast.r2_rms;
      healthPill.textContent = `AUDIO OK (${Math.round((rmsKey || 0) * 1000)})`;
      healthPill.className = "status-pill ok mono";
    }
    document.getElementById("pop-pc-dev").textContent = `Output: ${ast.pc_out_dev || "---"} (R1:${ast.r1_in_ok ? "OK" : "OFF"} R2:${ast.r2_in_ok ? "OK" : "OFF"})`;

    const hp = fullState.hamlib_ports || {};
    const p1 = (hp["1"] || hp[1] || []).join("/");
    const p2 = (hp["2"] || hp[2] || []).join("/");
    document.getElementById("hamlib-badge").textContent = `Hamlib: ${p1 || "-"} | ${p2 || "-"}`;

    const ar = fullState.audio_routing || {};
    document.getElementById("route-r1").value = ar.radio1_channel || "both";
    document.getElementById("route-r2").value = ar.radio2_channel || "both";
    const ilBtn = document.getElementById("interlock-btn");
    ilBtn.textContent = `Interlock: ${ar.tx_interlock !== false ? "ON" : "OFF"}`;
    ilBtn.className = ar.tx_interlock !== false ? "cbtn on" : "cbtn warn-on";

    // Update Dual View toggle & container visibility
    const dualBtn = document.getElementById("btn-dual-view");
    if (dualBtn) dualBtn.classList.toggle("active", dualView);
    const singleEl = document.getElementById("single-view-body");
    const dualEl = document.getElementById("dual-view-body");
    if (singleEl && dualEl) {
      singleEl.style.display = dualView ? "none" : "grid";
      dualEl.style.display = dualView ? "grid" : "none";
    }

    // Update top radio selector tabs
    [1, 2].forEach((slot) => {
      const rs = fullState.radios[String(slot)];
      const tab = document.getElementById(`rig-tab-${slot}`);
      tab.classList.toggle("active", !dualView && activeSlot === slot);
      tab.classList.toggle("r2", slot === 2);
      tab.title = (!rs?.connected && rs?.error) ? String(rs.error) : "";

      const dot = document.getElementById(`tab-dot-${slot}`);
      dot.className = rs?.tx ? "tab-dot tx" : (rs?.connected ? "tab-dot online" : "tab-dot");

      const model = rs?.usb_model || `Radio ${slot}`;
      document.getElementById(`tab-name-${slot}`).textContent = model;
      const vfo = rs?.active_band === "SUB" ? rs?.sub : rs?.main;
      document.getElementById(`tab-freq-${slot}`).textContent = fmtTabFreq(vfo?.freq_hz);

      const muted = Boolean((fullState.windows_mute || {})[String(slot)] ?? (fullState.windows_mute || {})[slot]);
      const muteEl = document.getElementById(`tab-mute-${slot}`);
      muteEl.textContent = muted ? "🔇" : "🔊";
      muteEl.classList.toggle("muted", muted);
    });

    if (dualView) {
      [1, 2].forEach((s) => renderDualPane(s));
    }

    const r = fullState.radios[String(activeSlot)];
    if (!r) return;

    const isSub = r.active_band === "SUB";
    const activeVfo = isSub ? r.sub : r.main;
    const isVhfUhf = Boolean(r.supports_dual_rx) || ((activeVfo?.freq_hz || 0) >= 100000000);

    // Band tracking & buttons
    const bands = r.supports_dual_rx ? VHF_BANDS : HF_BANDS;
    const curFreq = activeVfo?.freq_hz || 0;
    const matchedBand = findBandForFreq(bands, curFreq);
    if (matchedBand && curFreq > 0) {
      bandMem[activeSlot][matchedBand.lbl] = curFreq;
    }

    document.getElementById("active-rig-mini").textContent = `${r.usb_model || "Radio " + activeSlot} (${r.com_port || "-"})`;

    const bandGrid = document.getElementById("band-grid");
    bandGrid.className = r.supports_dual_rx ? "band-grid vhf" : "band-grid";
    syncBandButtons(bandGrid, bands, matchedBand);

    // Modes
    const curMode = activeVfo?.mode || "";
    document.querySelectorAll("#mode-grid .cbtn").forEach((btn) => {
      const m = btn.dataset.mode;
      const eq =
        m === curMode ||
        (m === "CW-U" && curMode === "CW") ||
        (m === "DATA-U" && (curMode === "DIGI" || curMode === "USB-D")) ||
        (m === "DATA-L" && curMode === "LSB-D");
      btn.classList.toggle("active", eq);
    });

    // Memories
    const memStoreBtn = document.getElementById("mem-store-btn");
    memStoreBtn.classList.toggle("warn", storeArmed);
    memStoreBtn.textContent = storeArmed ? "PICK M#" : "STORE";
    document.querySelectorAll(".mem-btn").forEach((btn) => {
      const idx = Number(btn.dataset.mem);
      const item = slotMemories[activeSlot]?.[idx];
      btn.classList.toggle("active", Boolean(item && Math.abs(item.freq_hz - curFreq) < 50));
      btn.title = item ? `${(item.freq_hz / 1e6).toFixed(4)} MHz (${item.mode})` : "Empty slot";
    });

    // RX DSP / PBT / Notch
    document.getElementById("pbt-controls").style.display = r.supports_pbt ? "flex" : "none";
    document.getElementById("btn-reset-pbt").style.display = r.supports_pbt ? "inline-block" : "none";
    const pbt1Hz = Math.round((((r.pbt1 ?? 50) - 50) / 50) * 1200);
    const pbt2Hz = Math.round((((r.pbt2 ?? 50) - 50) / 50) * 1200);
    setSliderVisual(document.getElementById("sl-pbt1"), r.pbt1 ?? 50, 0, 100);
    document.getElementById("val-pbt1").textContent = `${pbt1Hz >= 0 ? "+" : ""}${pbt1Hz} Hz`;
    setSliderVisual(document.getElementById("sl-pbt2"), r.pbt2 ?? 50, 0, 100);
    document.getElementById("val-pbt2").textContent = `${pbt2Hz >= 0 ? "+" : ""}${pbt2Hz} Hz`;

    document.getElementById("btn-notch").classList.toggle("active", Boolean(r.notch));
    document.getElementById("btn-anf").classList.toggle("active", Boolean(r.anf));
    document.getElementById("btn-nb-left").classList.toggle("active", Boolean(r.nb));
    setSliderVisual(document.getElementById("sl-notch-pos"), r.notch_pos ?? 50, 0, 100);
    document.getElementById("val-notch-pos").textContent = `${r.notch_pos ?? 50}%`;

    document.getElementById("filter-width-lbl").textContent = `${activeVfo?.filter_hz || 2400} Hz`;
    document.querySelectorAll(".agc-btn").forEach((b) => b.classList.toggle("active", Number(b.dataset.agc) === (r.agc || 2)));
    document.querySelectorAll(".rfil-btn").forEach((b) => b.classList.toggle("active", Number(b.dataset.fil) === (activeVfo?.filter || 1)));

    // CW Break-in & Tone/TSQL
    const bkNames = ["OFF", "SEMI", "FULL"];
    const bkinBtn = document.getElementById("btn-bkin");
    bkinBtn.textContent = `BKIN:${bkNames[r.bkin || 0] || "OFF"}`;
    bkinBtn.classList.toggle("active", Boolean(r.bkin));
    document.getElementById("btn-tone").classList.toggle("active", Boolean(r.tone_on));
    document.getElementById("btn-tsql").classList.toggle("active", Boolean(r.tsql_on));

    // VFO Cards
    document.getElementById("vfo-card-main").classList.toggle("active", !isSub);
    document.getElementById("vfo-card-sub").classList.toggle("active", isSub);

    document.getElementById("tag-main").textContent = r.supports_dual_rx ? "MAIN" : "VFO-A";
    document.getElementById("tag-main").className = !isSub ? "vfo-pill vfo-act" : "vfo-pill neutral";
    document.getElementById("tag-sub").textContent = r.supports_dual_rx ? "SUB" : "VFO-B";
    document.getElementById("tag-sub").className = isSub ? "vfo-pill vfo-act" : "vfo-pill neutral";

    const rxtx = document.getElementById("rxtx-pill");
    rxtx.textContent = r.tx ? "TX" : "RX";
    rxtx.className = r.tx ? "vfo-pill red" : "vfo-pill green";

    document.getElementById("mode-pill-main").textContent = r.main?.mode || "---";
    document.getElementById("mode-pill-sub").textContent = r.sub?.mode || "---";
    document.getElementById("sub-s-lbl").textContent = formatS(r.sub?.s_units);

    syncFreqDigits(document.getElementById("freq-main"), r.main?.freq_hz, activeSlot, "main");
    syncFreqDigits(document.getElementById("freq-sub"), r.sub?.freq_hz, activeSlot, "sub");

    document.getElementById("btn-lock-vfo").classList.toggle("warn", Boolean(vfoLocked[activeSlot]));
    document.getElementById("btn-fine-vfo").classList.toggle("active", Boolean(fineMode[activeSlot]));
    const wfStep = getWaterfallStepHz(r, fineMode[activeSlot]);
    document.getElementById("wf-hover-hz").textContent =
      `Wheel: ${wfStep >= 1000 ? (wfStep / 1000) + " kHz" : wfStep + " Hz"} · Click: Tune`;

    const dwBtn = document.getElementById("btn-dualwatch");
    dwBtn.style.display = r.supports_dualwatch ? "inline-block" : "none";
    dwBtn.classList.toggle("active", Boolean(r.dualwatch));

    // Toolbar
    document.getElementById("btn-split").classList.toggle("active", Boolean(r.split));
    document.getElementById("btn-vox").classList.toggle("active", Boolean(r.vox_on));
    document.getElementById("btn-rit").classList.toggle("active", Boolean(r.rit_on));
    const ritKhz = ((r.rit_hz || 0) / 1000).toFixed(2);
    document.getElementById("rit-val-lbl").textContent = `${(r.rit_hz || 0) >= 0 ? "+" : ""}${ritKhz} kHz`;

    document.getElementById("btn-repeaters-open").style.display = isVhfUhfRadio(r) ? "inline-block" : "none";
    document.getElementById("btn-1750-top").style.display = isVhfUhf ? "inline-block" : "none";
    document.getElementById("btn-1750-right").style.display = isVhfUhf ? "inline-flex" : "none";

    renderRepeaterSuggestion(r, activeVfo, isSub);

    document.querySelectorAll("#single-view-body .wf-span-btn").forEach((b) => {
      b.classList.toggle("active", Number(b.dataset.span) === (r.scope_span_hz || 100000));
    });

    // SWR Scan button & clear overlay badge
    const swrScan = r.swr_scan || {};
    const swrBusy = Boolean(swrScan.active);
    const swrPts = Array.isArray(swrScan.points) ? swrScan.points : [];
    const swrBtn = document.getElementById("btn-swr-scan");
    if (swrBtn) {
      swrBtn.classList.toggle("scanning", swrBusy);
      swrBtn.textContent = swrBusy
        ? `STOP (${swrScan.progress || 0}/${swrScan.total || 0})`
        : "SWR SCAN";
    }
    const swrClearBtn = document.getElementById("btn-swr-clear");
    if (swrClearBtn) {
      swrClearBtn.style.display = !swrBusy && swrPts.length > 0 ? "inline-flex" : "none";
    }

    // Right sidebar sliders & toggles
    setSliderVisual(document.getElementById("sl-win"), winVolAct, 0, 100);
    document.getElementById("val-sl-win").textContent = `${winVolAct}%`;
    const muteBtn = document.getElementById("btn-win-mute");
    muteBtn.textContent = winMutedAct ? "MUTED" : "ON";
    muteBtn.classList.toggle("muted", winMutedAct);

    setSliderVisual(document.getElementById("sl-af"), r.af_gain ?? 50, 0, 100);
    document.getElementById("val-sl-af").textContent = String(r.af_gain ?? 50);

    setSliderVisual(document.getElementById("sl-pwr"), r.tx_power ?? 50, 1, 100);
    document.getElementById("val-sl-pwr").textContent = `${r.tx_power ?? 50}%`;

    setSliderVisual(document.getElementById("sl-rf"), r.rf_gain ?? 100, 0, 100);
    document.getElementById("val-sl-rf").textContent = String(r.rf_gain ?? 100);

    const personalSql = getPersonalSql(activeSlot);
    setSliderVisual(document.getElementById("sl-sql"), personalSql, 0, 100);
    document.getElementById("val-sl-sql").textContent = String(personalSql);

    document.querySelectorAll(".pre-btn").forEach((b) => b.classList.toggle("active", Number(b.dataset.pre) === (r.preamp || 0)));

    const nrRight = document.getElementById("btn-nr-right");
    nrRight.textContent = r.nr ? "ON" : "OFF";
    nrRight.classList.toggle("active", Boolean(r.nr));
    setSliderVisual(document.getElementById("sl-nr-lvl"), r.nr_level ?? 50, 0, 100);
    document.getElementById("val-sl-nr").textContent = `${r.nr_level ?? 50}%`;

    const nbRight = document.getElementById("btn-nb-right");
    nbRight.textContent = r.nb ? "ON" : "OFF";
    nbRight.classList.toggle("active", Boolean(r.nb));
    setSliderVisual(document.getElementById("sl-nb-lvl"), r.nb_level ?? 50, 0, 100);
    document.getElementById("val-sl-nb").textContent = `${r.nb_level ?? 50}%`;

    setSliderVisual(document.getElementById("sl-mic"), r.mic_gain ?? 50, 0, 100);
    document.getElementById("val-sl-mic").textContent = `${r.mic_gain ?? 50}%`;

    const compSw = document.getElementById("btn-comp-sw");
    compSw.textContent = r.comp_on ? "ON" : "OFF";
    compSw.classList.toggle("active", Boolean(r.comp_on));
    setSliderVisual(document.getElementById("sl-comp-lvl"), r.comp_level ?? 50, 0, 100);
    document.getElementById("val-sl-comp").textContent = `${r.comp_level ?? 50}%`;

    const monSw = document.getElementById("btn-mon-sw");
    monSw.textContent = r.mon_on ? "ON" : "OFF";
    monSw.classList.toggle("active", Boolean(r.mon_on));
    setSliderVisual(document.getElementById("sl-mon-lvl"), r.mon_level ?? 50, 0, 100);
    document.getElementById("val-sl-mon").textContent = `${r.mon_level ?? 50}%`;

    setSliderVisual(document.getElementById("sl-cw-wpm"), r.cw_wpm ?? 20, 6, 48);
    document.getElementById("val-sl-cw").textContent = `${r.cw_wpm ?? 20}`;

    // CW Keyer 4 memory buttons
    const cwTxSlot = Number(r.cw_tx_slot || 0);
    document.querySelectorAll(".cw-mem-btn").forEach((btn) => {
      const idx = Number(btn.dataset.cw);
      const txt = (cwMacros[idx - 1] || "").trim();
      btn.textContent = txt ? txt.slice(0, 6) : `CW${idx}`;
      btn.classList.toggle("has-text", Boolean(txt));
      btn.classList.toggle("cw-tx", cwTxSlot === idx);
      btn.title = txt
        ? `CW${idx}: ${txt}\nLeft-click: Key / Abort · Right-click: Edit`
        : `CW${idx} (empty)\nRight-click to program CW text`;
    });
    const cwSendBtn = document.getElementById("btn-cw-send");
    const cwLiveEl = document.getElementById("cw-live-input");
    if (cwSendBtn) {
      const liveTx = cwTxSlot === 5;
      const hasTyped = Boolean((cwLiveEl?.value || "").trim());
      cwSendBtn.classList.toggle("cw-tx", liveTx);
      cwSendBtn.textContent = liveTx ? (hasTyped ? "ADD TO TX" : "STOP CW") : "SEND";
    }

    // DTMF live box below CW SEND (visible whenever active repeater on selected VFO is FM/analog-capable)
    const activeRepSel = r.active_repeater?.[isSub ? "sub" : "main"] || null;
    const dtmfWrap = document.getElementById("dtmf-live-wrap");
    if (dtmfWrap) {
      const canDtmf = Boolean(activeRepSel && (activeRepSel.mode !== "DV" || String(activeVfo?.mode || "").toUpperCase() === "FM"));
      dtmfWrap.style.display = canDtmf ? "flex" : "none";
      if (canDtmf) {
        const repCall = String(activeRepSel.callsign || "REPEATER");
        const repLbl = document.getElementById("dtmf-rep-lbl");
        if (repLbl) repLbl.textContent = repCall;
        const dtmfIn = document.getElementById("dtmf-live-input");
        const dtmfBtn = document.getElementById("btn-dtmf-send");
        const dtmfTx = Boolean(fullState.dtmf?.active && Number(fullState.dtmf?.slot || 0) === activeSlot);
        if (dtmfIn) {
          dtmfIn.title = repCall.includes("OH1DMR")
            ? "OH1DMRA MetarInfo DTMF: 51# Pori · 5# Module ON · 0# Help · 01# Stations · 1# Pori · 2# EFHK · 3# TMP · 4# MIK · # Close"
            : `Send DTMF tones (0-9 A-D * #) to ${repCall}`;
        }
        if (dtmfBtn) {
          dtmfBtn.classList.toggle("cw-tx", dtmfTx);
          dtmfBtn.textContent = dtmfTx ? "STOP DTMF" : "SEND DTMF";
        }
      }
    }

    // Audio Passthrough (Windows default playback -> Radio TX) + Mic Ignore dropdown + PTT warnings
    const passOn = Boolean(fullState.passthrough?.active);
    const passMicIgnored = fullState.passthrough?.mic_ignored !== false;
    const passWrap = document.getElementById("pass-wrap");
    if (passWrap) {
      passWrap.classList.toggle("open", passOn);
    }
    const passBtn = document.getElementById("btn-passthrough");
    if (passBtn) {
      passBtn.classList.toggle("active", passOn);
      passBtn.textContent = passOn ? "AUDIO PASSTHROUGH: ON" : "AUDIO PASSTHROUGH";
      passBtn.title = passOn
        ? "Passthrough Active — Windows default speaker audio feeds TX on PTT (Click to disable)"
        : "Feed Windows default playback device audio straight into Radio TX when PTT is active";
    }
    const passMicSel = document.getElementById("sel-pass-mic");
    if (passMicSel && !passMicSel.matches(":focus")) {
      passMicSel.value = passMicIgnored ? "ignored" : "mixed";
    }
    document.getElementById("ptt-stack")?.classList.toggle("pass-on", passOn);

    // QSO RX+TX REC button
    const qsoRec = fullState.qso_rec || {};
    const qsoBtn = document.getElementById("btn-qso-rec");
    if (qsoBtn) {
      const active = Boolean(qsoRec.active);
      qsoBtn.classList.toggle("recording", active);
      if (active) {
        const sTot = Math.max(0, Math.floor(Number(qsoRec.sec || 0)));
        const mm = String(Math.floor(sTot / 60)).padStart(2, "0");
        const ss = String(sTot % 60).padStart(2, "0");
        qsoBtn.textContent = `■ REC ${mm}:${ss}`;
        qsoBtn.title = `Recording RX+TX -> ${qsoRec.filename || "recordings/"} (Click to stop & save)`;
      } else {
        qsoBtn.textContent = "● REC";
        qsoBtn.title = qsoRec.last_saved
          ? `Last saved: ${qsoRec.last_saved} (Click to start new RX+TX recording)`
          : "Record live RX & TX audio to WAV";
      }
    }

    // ATU section
    const atuSec = document.getElementById("atu-section");
    atuSec.style.display = r.supports_tuner ? "flex" : "none";
    if (r.supports_tuner) {
      document.getElementById("atu-status-lbl").textContent = r.tuning ? "ATU TUNING..." : (r.tuner_on ? "ATU ON" : "ATU OFF");
      document.getElementById("btn-atu-on").classList.toggle("active", Boolean(r.tuner_on));
      document.getElementById("btn-atu-off").classList.toggle("active", !r.tuner_on);
      const tBtn = document.getElementById("btn-atu-tune");
      tBtn.textContent = r.tuning ? "TUNING..." : "TUNE";
      tBtn.classList.toggle("tuning", Boolean(r.tuning));
    }

    // PTT buttons
    const pttMom = document.getElementById("btn-ptt-mom");
    const pttHold = document.getElementById("btn-ptt-hold");
    pttMom.textContent = r.tx ? `ON AIR · TX (${r.pwr_meter || 0}%)` : "PTT";
    pttMom.classList.toggle("tx", Boolean(r.tx));
    pttHold.textContent = r.tx ? "TX LOCKED (CLICK TO RX)" : "PTT HOLD";
    pttHold.classList.toggle("tx", Boolean(r.tx));

    const bindBtn = document.getElementById("btn-bind-ptt");
    const bindInfo = (fullState.ptt_binds || {})[String(activeSlot)] || {};
    if (bindingSlot === activeSlot) {
      bindBtn.textContent = "Press Key/Btn...";
      bindBtn.className = "cbtn warn-on";
    } else {
      bindBtn.textContent = bindInfo.label ? `Bind: ${bindInfo.label}` : "Bind: -";
      bindBtn.className = bindInfo.vk ? "cbtn active" : "cbtn";
    }

    const globOn = Boolean(fullState.ptt_global);
    const globBtn = document.getElementById("btn-global-ptt");
    globBtn.textContent = globOn ? "Global: ON" : "Global: OFF";
    globBtn.className = globOn ? "cbtn active" : "cbtn";

    // Voice Memory status, buttons & repeat Timer
    const vm = fullState.voice_memory || {};
    const vmSlots = vm.slots || {};
    const recSlot = vm.recording_slot || 0;
    const playSlot = vm.playing_slot || 0;
    const timerEnabled = Boolean(vm.timer_enabled);
    const timerSec = Math.max(1, Math.min(10, Number(vm.timer_sec || 5)));
    const timerSlot = Number(vm.timer_slot || 0);
    const timerRem = Number(vm.timer_remaining || 0);
    const vmLbl = document.getElementById("vm-status-lbl");
    if (recSlot > 0) {
      vmLbl.textContent = `REC V${recSlot} ${vm.rec_sec || 0}s`;
      vmLbl.style.color = "var(--red)";
    } else if (playSlot > 0) {
      vmLbl.textContent = timerEnabled ? `TX V${playSlot} LOOP` : `TX PLAY V${playSlot}`;
      vmLbl.style.color = "var(--red)";
    } else if (timerEnabled && timerSlot > 0 && timerRem > 0) {
      vmLbl.textContent = `V${timerSlot} IN ${timerRem.toFixed(1)}s`;
      vmLbl.style.color = "var(--amber)";
    } else if (timerEnabled) {
      vmLbl.textContent = `TIMER (${timerSec}s)`;
      vmLbl.style.color = "var(--cyan)";
    } else if (vmRecArmed) {
      vmLbl.textContent = "PICK V1-V4";
      vmLbl.style.color = "var(--amber)";
    } else {
      vmLbl.textContent = "READY";
      vmLbl.style.color = "var(--muted)";
    }

    const recBtn = document.getElementById("btn-vm-rec");
    const vmBusy = recSlot > 0 || playSlot > 0;
    recBtn.className = vmBusy ? "cbtn rec-on" : (vmRecArmed ? "cbtn warn-on" : "cbtn");
    recBtn.textContent = vmBusy ? "■ STOP" : (vmRecArmed ? "● PICK V1-V4" : "● REC");

    const vmTimerBtn = document.getElementById("btn-vm-timer");
    if (vmTimerBtn) {
      if (timerEnabled && timerSlot > 0 && timerRem > 0) {
        vmTimerBtn.className = "cbtn timer-wait";
        vmTimerBtn.textContent = `⏱ ${timerRem.toFixed(1)}s`;
      } else if (timerEnabled) {
        vmTimerBtn.className = "cbtn timer-on";
        vmTimerBtn.textContent = "TIMER: ON";
      } else {
        vmTimerBtn.className = "cbtn";
        vmTimerBtn.textContent = "TIMER";
      }
    }
    const vmTimerSel = document.getElementById("sel-vm-timer");
    if (vmTimerSel && !vmTimerSel.matches(":focus")) {
      vmTimerSel.value = String(timerSec);
    }

    document.querySelectorAll(".vm-btn").forEach((btn) => {
      const idx = Number(btn.dataset.vm);
      const dur = vmSlots[String(idx)] || 0;
      const isRec = recSlot === idx;
      const isPlay = playSlot === idx;
      btn.textContent = dur > 0 ? `V${idx} ${dur}s` : `V${idx}`;
      btn.className = "cbtn vm-btn" + (isRec || isPlay ? " vm-tx" : (dur > 0 ? " vm-has" : ""));
      btn.title = dur > 0 ? `Recorded ${dur}s — click to transmit (manual PTT stops)` : "Empty — click ● REC then pick slot";
    });

    // Bargraph text readouts
    document.getElementById("val-s").textContent = formatS(activeVfo?.s_units);
    document.getElementById("val-po").textContent = `${r.tx ? (r.pwr_meter || 0) : 0}%`;
    document.getElementById("val-swr").textContent = r.tx ? (r.swr || 1.0).toFixed(2) : "--";
    document.getElementById("val-alc").textContent = r.tx ? `${r.alc || 0}%` : "--";
    document.getElementById("val-comp").textContent = r.tx ? `${(r.comp_db || 0).toFixed(0)} dB` : (r.comp_on ? "ON" : "0 dB");
  }

  function drawAnalogMeter(r) {
    const canvas = document.getElementById("analog-meter-canvas");
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(rect.width * dpr) || canvas.height !== Math.round(rect.height * dpr)) {
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.save();
    ctx.scale(dpr, dpr);

    const w = rect.width;
    const h = rect.height;

    const activeVfo = r.active_band === "SUB" ? r.sub : r.main;
    const isTx = Boolean(r.tx);
    const targetNorm = isTx
      ? Math.max(0, Math.min(1, (r.pwr_meter || 0) / 100))
      : Math.max(0, Math.min(1, ((activeVfo?.s_units || 1) - 1) / 14));

    const anim = meterAnim[activeSlot];
    anim.needle += (targetNorm - anim.needle) * 0.25;
    const nowTs = performance.now();
    if (targetNorm >= anim.peak) {
      anim.peak = targetNorm;
      anim.peakTs = nowTs;
    } else if (nowTs - anim.peakTs > 850) {
      anim.peak = Math.max(targetNorm, anim.peak - 0.015);
    }

    const sqlPct = getPersonalSql(r.slot || activeSlot);
    const sqlS = !isTx ? sqlPctToSUnits(sqlPct) : null;
    const sqlNorm = sqlS !== null ? Math.min(1, Math.max(0, (sqlS - 1) / 14)) : null;
    const sqlOpen = sqlS !== null ? (sqlPct <= 1 || (activeVfo?.s_units || 0) >= sqlS) : false;
    const sqlCol = sqlOpen ? "#10b981" : "#facc15";
    const readoutMain = isTx ? `PO ${r.pwr_meter || 0}%` : formatS(activeVfo?.s_units);
    const readoutSub = isTx ? `SWR ${(r.swr || 1.0).toFixed(1)}` : `${activeVfo?.dbm ?? -127} dBm`;

    const s9Frac = 8 / 14;
    const sLabels = [
      { frac: 0, txt: "1" },
      { frac: 2 / 14, txt: "3" },
      { frac: 4 / 14, txt: "5" },
      { frac: 6 / 14, txt: "7" },
      { frac: 8 / 14, txt: "9" },
      { frac: 10 / 14, txt: "+20" },
      { frac: 12 / 14, txt: "+40" },
      { frac: 14 / 14, txt: "+60" },
    ];

    if (sMeterStyle === 1) {
      // STYLE 1: Curved Radial LED Arc + Peak-Hold + Large Digital Center Readout
      ctx.fillStyle = "#05070d";
      ctx.fillRect(0, 0, w, h);
      const cx = w * 0.5;
      const cy = h * 1.18;
      const rxOuter = Math.min(w * 0.45, h * 0.98);
      const rxInner = rxOuter - 16;
      const startAng = Math.PI + 0.34;
      const endAng = 2 * Math.PI - 0.34;
      const spanAng = endAng - startAng;

      const segs = 28;
      const litSegs = Math.round(anim.needle * segs);
      const peakSeg = Math.min(segs - 1, Math.floor(anim.peak * segs));
      for (let i = 0; i < segs; i++) {
        const f = i / (segs - 1);
        const a0 = startAng + (i / segs) * spanAng + 0.008;
        const a1 = startAng + ((i + 1) / segs) * spanAng - 0.008;
        const am = (a0 + a1) * 0.5;
        let onCol = isTx ? "#38bdf8" : (f > s9Frac ? "#ef4444" : (f > 0.35 ? "#10b981" : "#34d399"));
        const isLit = i < litSegs || (i === peakSeg && anim.peak > 0.04);
        ctx.strokeStyle = isLit ? onCol : (f > s9Frac && !isTx ? "rgba(239, 68, 68, 0.16)" : "rgba(148, 163, 184, 0.14)");
        ctx.lineWidth = (i === peakSeg && i >= litSegs && anim.peak > 0.04) ? 5 : 11;
        ctx.beginPath();
        ctx.arc(cx, cy, (rxOuter + rxInner) * 0.5, a0, a1);
        ctx.stroke();
        if (i % 4 === 0 || i === segs - 1) {
          ctx.fillStyle = isLit ? "#f8fafc" : "#64748b";
        }
      }
      ctx.font = "700 8.5px ui-monospace, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      sLabels.forEach((t) => {
        const a = startAng + t.frac * spanAng;
        ctx.fillStyle = (!isTx && t.frac > s9Frac + 0.01) ? "#f87171" : "#94a3b8";
        ctx.fillText(t.txt, cx + Math.cos(a) * (rxOuter + 7), cy + Math.sin(a) * (rxOuter + 7));
      });
      if (sqlNorm !== null) {
        const sqlAng = startAng + sqlNorm * spanAng;
        ctx.fillStyle = sqlCol;
        ctx.beginPath();
        ctx.arc(cx + Math.cos(sqlAng) * (rxInner - 5), cy + Math.sin(sqlAng) * (rxInner - 5), 3.2, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.textAlign = "center";
      ctx.fillStyle = isTx ? "#f87171" : "#f8fafc";
      ctx.font = "800 22px ui-monospace, monospace";
      ctx.fillText(readoutMain, cx, h * 0.65);
      ctx.fillStyle = "#94a3b8";
      ctx.font = "600 10px ui-monospace, monospace";
      ctx.fillText(readoutSub, cx, h * 0.85);
    } else if (sMeterStyle === 2) {
      // STYLE 2: Classic Warm Tungsten-Backlit Studio VU Window Instrument
      ctx.fillStyle = "#0a0b0e";
      ctx.fillRect(0, 0, w, h);
      const pad = 6;
      const bg = ctx.createRadialGradient(w * 0.5, h * 0.78, 8, w * 0.5, h * 0.55, w * 0.55);
      bg.addColorStop(0.0, "#432c14");
      bg.addColorStop(0.55, "#281b0d");
      bg.addColorStop(1.0, "#120d08");
      ctx.fillStyle = bg;
      ctx.beginPath();
      ctx.roundRect(pad, pad, w - pad * 2, h - pad * 2, 7);
      ctx.fill();
      ctx.strokeStyle = "rgba(251, 191, 36, 0.35)";
      ctx.lineWidth = 1.2;
      ctx.stroke();

      const cx = w * 0.5;
      const cy = h * 1.22;
      const radius = Math.min(w * 0.41, h * 0.96);
      const startAng = Math.PI + 0.40;
      const endAng = 2 * Math.PI - 0.40;
      const spanAng = endAng - startAng;
      const s9Ang = startAng + s9Frac * spanAng;

      ctx.strokeStyle = "#fde68a";
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.arc(cx, cy, radius, startAng, s9Ang);
      ctx.stroke();

      ctx.strokeStyle = "#ef4444";
      ctx.lineWidth = 4.5;
      ctx.beginPath();
      ctx.arc(cx, cy, radius, s9Ang, endAng);
      ctx.stroke();

      ctx.font = "700 9px Georgia, serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (let i = 0; i <= 14; i++) {
        const frac = i / 14;
        const a = startAng + frac * spanAng;
        const len = i % 2 === 0 ? 6 : 3.5;
        ctx.strokeStyle = frac > s9Frac + 0.01 ? "#fca5a5" : "#fef3c7";
        ctx.lineWidth = i % 2 === 0 ? 1.5 : 1;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(a) * (radius - len), cy + Math.sin(a) * (radius - len));
        ctx.lineTo(cx + Math.cos(a) * (radius + 1), cy + Math.sin(a) * (radius + 1));
        ctx.stroke();
      }
      sLabels.forEach((t) => {
        const a = startAng + t.frac * spanAng;
        ctx.fillStyle = t.frac > s9Frac + 0.01 ? "#fca5a5" : "#fef3c7";
        ctx.fillText(t.txt, cx + Math.cos(a) * (radius - 14), cy + Math.sin(a) * (radius - 14));
      });
      if (sqlNorm !== null) {
        const sqlAng = startAng + sqlNorm * spanAng;
        ctx.fillStyle = sqlCol;
        ctx.beginPath();
        ctx.arc(cx + Math.cos(sqlAng) * (radius + 3), cy + Math.sin(sqlAng) * (radius + 3), 2.8, 0, Math.PI * 2);
        ctx.fill();
      }
      const nAng = startAng + anim.needle * spanAng;
      ctx.strokeStyle = "rgba(0,0,0,0.55)";
      ctx.lineWidth = 3.2;
      ctx.beginPath();
      ctx.moveTo(cx + 2, h - 8);
      ctx.lineTo(cx + 2 + Math.cos(nAng) * (radius + 2), cy + 2 + Math.sin(nAng) * (radius + 2));
      ctx.stroke();

      ctx.strokeStyle = "#fb923c";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(cx, h - 8);
      ctx.lineTo(cx + Math.cos(nAng) * (radius + 2), cy + Math.sin(nAng) * (radius + 2));
      ctx.stroke();

      ctx.fillStyle = "#52525b";
      ctx.beginPath();
      ctx.arc(cx, h - 8, 7, Math.PI, 0);
      ctx.fill();
    } else if (sMeterStyle === 3) {
      // STYLE 3: Tactical Split Horizontal SDR Telemetry Gauge + Peak-Hold
      ctx.fillStyle = "#06080f";
      ctx.fillRect(0, 0, w, h);
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillStyle = isTx ? "#f87171" : "#f8fafc";
      ctx.font = "800 24px ui-monospace, monospace";
      ctx.fillText(readoutMain, 14, 24);

      ctx.textAlign = "right";
      ctx.fillStyle = isTx ? "#fca5a5" : "#38bdf8";
      ctx.font = "700 12px ui-monospace, monospace";
      ctx.fillText(readoutSub, w - 14, 24);

      const bx = 14;
      const bw = w - 28;
      const by = 45;
      const bh = 22;
      const segs = 36;
      const gap = 2;
      const sw = (bw - (segs - 1) * gap) / segs;
      const lit = Math.round(anim.needle * segs);
      const peakIdx = Math.min(segs - 1, Math.floor(anim.peak * segs));
      for (let i = 0; i < segs; i++) {
        const f = i / (segs - 1);
        const x = bx + i * (sw + gap);
        const col = isTx ? "#38bdf8" : (f > s9Frac ? "#ef4444" : (f > 0.35 ? "#10b981" : "#34d399"));
        const isOn = i < lit || (i === peakIdx && anim.peak > 0.03);
        ctx.fillStyle = isOn ? col : (f > s9Frac && !isTx ? "rgba(239,68,68,0.14)" : "rgba(148,163,184,0.12)");
        ctx.fillRect(x, by, sw, bh);
      }

      if (sqlNorm !== null) {
        const sx = bx + sqlNorm * bw;
        ctx.strokeStyle = sqlCol;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(sx, by - 4);
        ctx.lineTo(sx, by + bh + 4);
        ctx.stroke();
      }

      ctx.font = "700 9px ui-monospace, monospace";
      ctx.textAlign = "center";
      sLabels.forEach((t) => {
        const x = bx + t.frac * bw;
        ctx.fillStyle = t.frac > s9Frac + 0.01 ? "#f87171" : "#94a3b8";
        ctx.fillText(t.txt, x, by + bh + 13);
      });
    } else {
      // STYLE 0: Classic Round Dual Arc Analog Meter
      const cx = w * 0.5;
      const cy = h * 1.24;
      const radius = Math.min(w * 0.43, h * 1.06);

      ctx.fillStyle = "#060910";
      ctx.fillRect(0, 0, w, h);
      const glow = ctx.createRadialGradient(cx, h * 0.85, 8, cx, h * 0.65, radius * 0.95);
      glow.addColorStop(0.0, "rgba(245, 158, 11, 0.30)");
      glow.addColorStop(0.5, "rgba(56, 189, 248, 0.08)");
      glow.addColorStop(1.0, "rgba(6, 9, 16, 0)");
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, w, h);

      const startAng = Math.PI + 0.38;
      const endAng = 2 * Math.PI - 0.38;
      const spanAng = endAng - startAng;
      const s9Ang = startAng + s9Frac * spanAng;
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = "#cbd5e1";
      ctx.beginPath();
      ctx.arc(cx, cy, radius, startAng, s9Ang);
      ctx.stroke();

      ctx.strokeStyle = "#f59e0b";
      ctx.beginPath();
      ctx.arc(cx, cy, radius, s9Ang, endAng);
      ctx.stroke();

      ctx.font = "700 9.5px ui-monospace, monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      sLabels.forEach((t) => {
        const a = startAng + t.frac * spanAng;
        const isPlus = t.frac > s9Frac + 0.01;
        ctx.strokeStyle = isPlus ? "#f59e0b" : "#e2e8f0";
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(a) * (radius - 6), cy + Math.sin(a) * (radius - 6));
        ctx.lineTo(cx + Math.cos(a) * (radius + 2), cy + Math.sin(a) * (radius + 2));
        ctx.stroke();
        ctx.fillStyle = isPlus ? "#fbbf24" : "#cbd5e1";
        ctx.fillText(t.txt, cx + Math.cos(a) * (radius + 11), cy + Math.sin(a) * (radius + 11));
      });

      const poRadius = radius - 18;
      ctx.lineWidth = 1.4;
      ctx.strokeStyle = "rgba(56, 189, 248, 0.4)";
      ctx.beginPath();
      ctx.arc(cx, cy, poRadius, startAng, endAng);
      ctx.stroke();

      [0, 25, 50, 75, 100].forEach((p) => {
        const a = startAng + (p / 100) * spanAng;
        ctx.fillStyle = "rgba(203, 213, 225, 0.72)";
        ctx.fillText(String(p), cx + Math.cos(a) * (poRadius - 10), cy + Math.sin(a) * (poRadius - 10));
      });

      const sxStart = cx + Math.cos(startAng) * radius;
      const syStart = cy + Math.sin(startAng) * radius;
      const pxStart = cx + Math.cos(startAng) * poRadius;
      const pyStart = cy + Math.sin(startAng) * poRadius;

      ctx.font = "700 9.5px ui-monospace, monospace";
      ctx.textAlign = "right";
      ctx.fillStyle = "#e2e8f0";
      ctx.fillText("S", sxStart - 7, syStart + 1);
      ctx.fillStyle = "#38bdf8";
      ctx.fillText("PO", pxStart - 7, pyStart + 1);

      if (sqlNorm !== null) {
        const sqlAng = startAng + sqlNorm * spanAng;
        ctx.strokeStyle = sqlCol;
        ctx.lineWidth = 2.2;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(sqlAng) * (radius - 9), cy + Math.sin(sqlAng) * (radius - 9));
        ctx.lineTo(cx + Math.cos(sqlAng) * (radius + 3), cy + Math.sin(sqlAng) * (radius + 3));
        ctx.stroke();

        ctx.fillStyle = sqlCol;
        ctx.beginPath();
        ctx.arc(cx + Math.cos(sqlAng) * radius, cy + Math.sin(sqlAng) * radius, 3, 0, Math.PI * 2);
        ctx.fill();
      }

      const nAng = startAng + anim.needle * spanAng;
      ctx.strokeStyle = "#ef4444";
      ctx.lineWidth = 2.4;
      ctx.shadowColor = "rgba(239, 68, 68, 0.75)";
      ctx.shadowBlur = 7;
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(nAng) * (radius * 0.35), cy + Math.sin(nAng) * (radius * 0.35));
      ctx.lineTo(cx + Math.cos(nAng) * (radius + 3), cy + Math.sin(nAng) * (radius + 3));
      ctx.stroke();
      ctx.shadowBlur = 0;
    }

    // Subtle corner label indicating active style
    const styleNames = ["ANALOG", "CURVE", "VU", "DIGITAL"];
    ctx.font = "700 8px ui-monospace, monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "bottom";
    ctx.fillStyle = "rgba(148, 163, 184, 0.45)";
    ctx.fillText(styleNames[sMeterStyle] || "ANALOG", w - 6, h - 4);

    ctx.restore();
  }

  function drawBarGraph(canvasId, norm, colorFn) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(rect.width * dpr) || canvas.height !== Math.round(rect.height * dpr)) {
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.save();
    ctx.scale(dpr, dpr);

    const w = rect.width;
    const h = rect.height;
    ctx.clearRect(0, 0, w, h);

    const segs = 48;
    const gap = 2;
    const segW = Math.max(2, (w - (segs - 1) * gap) / segs);
    const lit = Math.round(Math.max(0, Math.min(1, norm)) * segs);

    for (let i = 0; i < segs; i++) {
      const x = i * (segW + gap);
      const frac = i / segs;
      ctx.fillStyle = i < lit ? colorFn(frac) : "rgba(148, 163, 184, 0.1)";
      ctx.fillRect(x, 3, segW, h - 6);
    }
    ctx.restore();
  }

  function drawSMeterBar(canvasId, r) {
    const activeVfo = r.active_band === "SUB" ? r.sub : r.main;
    const sNorm = Math.max(0, Math.min(1, ((activeVfo?.s_units || 1) - 1) / 14));
    drawBarGraph(canvasId, sNorm, (f) => (f > 8 / 14 ? "#ef4444" : (f > 0.35 ? "#10b981" : "#34d399")));

    const sqlPct = getPersonalSql(r.slot || activeSlot);
    const sqlS = !r.tx ? sqlPctToSUnits(sqlPct) : null;
    if (sqlS !== null) {
      const sCanv = document.getElementById(canvasId);
      if (sCanv) {
        const rect = sCanv.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        const dpr = window.devicePixelRatio || 1;
        const ctx = sCanv.getContext("2d");
        const sqlNorm = Math.min(1, Math.max(0, (sqlS - 1) / 14));
        const sqlX = Math.max(2, Math.min(rect.width - 2, sqlNorm * rect.width));
        const isOpen = sqlPct <= 1 || (activeVfo?.s_units || 0) >= sqlS;
        const sqlCol = isOpen ? "#10b981" : "#facc15";

        ctx.save();
        ctx.scale(dpr, dpr);
        ctx.strokeStyle = sqlCol;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(sqlX, 0);
        ctx.lineTo(sqlX, rect.height);
        ctx.stroke();

        ctx.fillStyle = sqlCol;
        ctx.beginPath();
        ctx.moveTo(sqlX, 4);
        ctx.lineTo(sqlX - 3, 0);
        ctx.lineTo(sqlX + 3, 0);
        ctx.closePath();
        ctx.fill();
        ctx.restore();
      }
    }
  }

  function drawAllBars(r) {
    drawSMeterBar("bar-s", r);

    const poNorm = r.tx ? Math.min(1, (r.pwr_meter || 0) / 100) : 0;
    drawBarGraph("bar-po", poNorm, (f) => (f > 0.8 ? "#ef4444" : "#38bdf8"));

    const swrNorm = r.tx ? Math.min(1, Math.max(0, ((r.swr || 1) - 1) / 3)) : 0;
    drawBarGraph("bar-swr", swrNorm, (f) => (f > 0.45 ? "#ef4444" : (f > 0.2 ? "#f59e0b" : "#10b981")));

    const alcNorm = r.tx ? Math.min(1, (r.alc || 0) / 100) : 0;
    drawBarGraph("bar-alc", alcNorm, (f) => (f > 0.78 ? "#ef4444" : "#10b981"));

    const compNorm = r.tx ? Math.min(1, (r.comp_db || 0) / 30) : 0;
    drawBarGraph("bar-comp", compNorm, (f) => (f > 0.65 ? "#f59e0b" : "#14b8a6"));
  }

  function drawBandplan(canvasId, bands, freqHz) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(rect.width * dpr) || canvas.height !== Math.round(rect.height * dpr)) {
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.save();
    ctx.scale(dpr, dpr);

    const w = rect.width;
    const h = rect.height;
    ctx.fillStyle = "#04070e";
    ctx.fillRect(0, 0, w, h);

    const band = findBandForFreq(bands, freqHz);
    if (!band) {
      ctx.fillStyle = "#1e293b";
      ctx.fillRect(2, 6, w - 4, 6);
      ctx.fillStyle = "#64748b";
      ctx.font = "600 9px ui-monospace, monospace";
      ctx.fillText("OUT OF BAND", 6, 12);
      ctx.restore();
      return;
    }

    const span = band.max - band.min;
    const xCw = ((band.cwEnd - band.min) / span) * w;
    const xDig = ((band.digiEnd - band.min) / span) * w;
    const segs = [
      { x: 0, w: Math.max(4, xCw), col: "#059669", lbl: "CW" },
      { x: xCw, w: Math.max(4, xDig - xCw), col: "#0891b2", lbl: "DIGI" },
      { x: xDig, w: Math.max(4, w - xDig), col: "#dc2626", lbl: "SSB" },
    ];

    ctx.font = "800 8.5px ui-monospace, monospace";
    ctx.textBaseline = "middle";
    segs.forEach((s) => {
      ctx.fillStyle = s.col;
      ctx.fillRect(s.x, 3, s.w, h - 6);
      if (s.w >= 20) {
        ctx.fillStyle = "#f8fafc";
        ctx.textAlign = "center";
        ctx.fillText(s.lbl, s.x + s.w * 0.5, h * 0.5 + 0.5);
      }
    });

    const posX = Math.max(2, Math.min(w - 2, ((freqHz - band.min) / span) * w));
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(posX - 1.5, 1, 3, h - 2);
    ctx.restore();
  }

  function animLoop() {
    if (fullState) {
      if (dualView) {
        [1, 2].forEach((slot) => {
          const r = fullState.radios[String(slot)];
          if (!r) return;
          const activeVfo = r.active_band === "SUB" ? r.sub : r.main;
          const activeRep = r.active_repeater?.[r.active_band === "SUB" ? "sub" : "main"] || null;
          const wfRepeaters = isVhfUhfRadio(r)
            ? getClosestQrvRepeatersForWaterfall(fullState.qth_locator || "KP20")
            : null;
          window.ScopeRenderer.renderScope(
            document.getElementById(`dual-scope-${slot}`),
            scopeStates[slot],
            {
              accent: ACCENTS[slot],
              palette,
              connected: r.connected,
              tx: r.tx,
              mode: activeVfo?.mode,
              filterHz: activeVfo?.filter_hz || 2400,
              centerHz: activeVfo?.freq_hz || r.scope_center_hz || 0,
              spanHz: r.scope_span_hz || 100000,
              repeaters: wfRepeaters,
              activeRepeater: activeRep,
              swrScan: r.swr_scan,
              swrWarn: getSwrWarnOverlay(slot, r),
            }
          );
          drawSMeterBar(`dual-bar-s-${slot}`, r);
        });
      } else {
        const r = fullState.radios[String(activeSlot)];
        if (r) {
          const activeVfo = r.active_band === "SUB" ? r.sub : r.main;
          const activeRep = r.active_repeater?.[r.active_band === "SUB" ? "sub" : "main"] || null;
          const wfRepeaters = isVhfUhfRadio(r)
            ? getClosestQrvRepeatersForWaterfall(fullState.qth_locator || "KP20")
            : null;
          window.ScopeRenderer.renderScope(
            document.getElementById("scope-canvas"),
            scopeStates[activeSlot],
            {
              accent: ACCENTS[activeSlot],
              palette,
              connected: r.connected,
              tx: r.tx,
              mode: activeVfo?.mode,
              filterHz: activeVfo?.filter_hz || 2400,
              centerHz: activeVfo?.freq_hz || r.scope_center_hz || 0,
              spanHz: r.scope_span_hz || 100000,
              repeaters: wfRepeaters,
              activeRepeater: activeRep,
              swrScan: r.swr_scan,
              swrWarn: getSwrWarnOverlay(activeSlot, r),
            }
          );

          drawAnalogMeter(r);
          drawAllBars(r);
        }
      }
    }
    requestAnimationFrame(animLoop);
  }

  function persistUiPrefs() {
    try {
      localStorage.setItem("rig_dual_view_v1", dualView ? "1" : "0");
      localStorage.setItem("rig_wf_track_v1", wfTrack ? "1" : "0");
      localStorage.setItem("rig_cw_macros_v1", JSON.stringify(cwMacros));
      localStorage.setItem("rig_smeter_style_v1", String(sMeterStyle));
    } catch (_) {}
    sendCmd(1, "set_ui_prefs", { dual_view: dualView, wf_track: wfTrack, cw_macros: cwMacros, smeter_style: sMeterStyle });
  }

  function setDualView(on) {
    dualView = Boolean(on);
    persistUiPrefs();
    renderAll();
  }

  function attachEvents() {
    const winControlsEl = document.getElementById("win-controls");
    const initDesktopControls = () => {
      if (winControlsEl && window.pywebview?.api) {
        winControlsEl.style.display = "flex";
      }
    };
    initDesktopControls();
    window.addEventListener("pywebviewready", initDesktopControls);

    document.getElementById("win-min-btn")?.addEventListener("click", () => {
      window.pywebview?.api?.minimize?.();
    });
    document.getElementById("win-max-btn")?.addEventListener("click", () => {
      window.pywebview?.api?.toggle_maximize?.();
    });
    document.getElementById("win-close-btn")?.addEventListener("click", () => {
      window.pywebview?.api?.close?.();
    });
    document.getElementById("topbar-drag-spacer")?.addEventListener("dblclick", () => {
      window.pywebview?.api?.toggle_maximize?.();
    });

    const wfTrackBtn = document.getElementById("btn-wf-track");
    if (wfTrackBtn) {
      wfTrackBtn.classList.toggle("active", wfTrack);
      wfTrackBtn.addEventListener("click", () => {
        wfTrack = !wfTrack;
        wfTrackBtn.classList.toggle("active", wfTrack);
        persistUiPrefs();
      });
    }

    document.getElementById("btn-dual-view").addEventListener("click", () => {
      setDualView(!dualView);
    });

    document.getElementById("analog-meter-canvas")?.addEventListener("click", () => {
      sMeterStyle = (sMeterStyle + 1) % 4;
      persistUiPrefs();
      const r = fullState?.radios?.[String(activeSlot)];
      if (r) drawAnalogMeter(r);
    });

    document.getElementById("btn-app-update")?.addEventListener("click", async () => {
      if (appUpdatePending) return;
      const btn = document.getElementById("btn-app-update");
      appUpdatePending = true;
      const prevText = btn.textContent;
      btn.textContent = "⏳ Updating...";
      btn.disabled = true;
      try {
        const res = await fetch("/api/app_update", { method: "POST" });
        const d = await res.json();
        if (d.ok) {
          btn.textContent = d.restarting ? "🔄 Restarting..." : "✅ Updated";
          if (!d.restarting) {
            setTimeout(() => { btn.disabled = false; btn.textContent = prevText; appUpdatePending = false; }, 3000);
          }
        } else {
          btn.textContent = "⚠ " + (d.error || "Failed");
          setTimeout(() => { btn.disabled = false; btn.textContent = prevText; appUpdatePending = false; }, 3500);
        }
      } catch (err) {
        btn.textContent = "⚠ Update Error";
        setTimeout(() => { btn.disabled = false; btn.textContent = prevText; appUpdatePending = false; }, 3500);
      }
    });

    // Radio switcher tabs + quick mute icons
    [1, 2].forEach((slot) => {
      document.getElementById(`rig-tab-${slot}`).addEventListener("click", (e) => {
        if (e.target.closest(".tab-mute")) {
          e.stopPropagation();
          const cur = Boolean((fullState?.windows_mute || {})[String(slot)] ?? (fullState?.windows_mute || {})[slot]);
          sendCmd(slot, "set_windows_mute", { mute: !cur });
          return;
        }
        activeSlot = slot;
        storeArmed = false;
        if (dualView) {
          setDualView(false);
        } else {
          renderAll();
        }
      });
    });

    const popBtn = document.getElementById("audio-popover-btn");
    const pop = document.getElementById("audio-popover");
    popBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      pop.classList.toggle("open");
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest("#audio-popover") && !e.target.closest("#audio-popover-btn")) {
        pop.classList.remove("open");
      }
    });

    document.getElementById("route-r1").addEventListener("change", (e) => {
      sendCmd(1, "set_audio_routing", { radio1_channel: e.target.value });
    });
    document.getElementById("route-r2").addEventListener("change", (e) => {
      sendCmd(2, "set_audio_routing", { radio2_channel: e.target.value });
    });
    document.getElementById("interlock-btn").addEventListener("click", () => {
      const cur = fullState?.audio_routing?.tx_interlock !== false;
      sendCmd(1, "set_audio_routing", { tx_interlock: !cur });
    });

    // Bands
    document.getElementById("band-grid").addEventListener("click", (e) => {
      const btn = e.target.closest(".band-btn");
      if (!btn || !fullState || vfoLocked[activeSlot]) return;
      const r = fullState.radios[String(activeSlot)];
      const bands = r.supports_dual_rx ? VHF_BANDS : HF_BANDS;
      const b = bands.find((x) => x.lbl === btn.dataset.bandLbl);
      if (b) {
        const targetHz = bandMem[activeSlot][b.lbl] || b.def;
        sendCmd(activeSlot, "set_freq", { freq_hz: targetHz, target: "selected" });
      }
    });

    // Modes
    document.getElementById("mode-grid").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-mode]");
      if (btn) sendCmd(activeSlot, "set_mode", { mode: btn.dataset.mode, target: "selected" });
    });

    // Memories
    document.getElementById("mem-store-btn").addEventListener("click", () => {
      storeArmed = !storeArmed;
      renderAll();
    });
    document.getElementById("mem-grid").addEventListener("click", (e) => {
      const btn = e.target.closest(".mem-btn");
      if (!btn || !fullState) return;
      const idx = Number(btn.dataset.mem);
      const r = fullState.radios[String(activeSlot)];
      const activeVfo = r?.active_band === "SUB" ? r.sub : r.main;
      if (storeArmed) {
        if (activeVfo?.freq_hz) {
          slotMemories[activeSlot][idx] = { freq_hz: activeVfo.freq_hz, mode: activeVfo.mode || "USB" };
          saveMemories();
        }
        storeArmed = false;
        renderAll();
      } else {
        const item = slotMemories[activeSlot]?.[idx];
        if (item && !vfoLocked[activeSlot]) {
          sendCmd(activeSlot, "set_freq", { freq_hz: item.freq_hz, target: "selected" });
          if (item.mode && item.mode !== "---") sendCmd(activeSlot, "set_mode", { mode: item.mode, target: "selected" });
        }
      }
    });

    // Voice Memory (single REC/STOP button: REC -> PICK V1-V4 -> red STOP)
    document.getElementById("btn-vm-rec").addEventListener("click", () => {
      const recSlot = fullState?.voice_memory?.recording_slot || 0;
      const playSlot = fullState?.voice_memory?.playing_slot || 0;
      if (recSlot > 0) {
        sendCmd(activeSlot, "voice_rec_stop", {});
        vmRecArmed = false;
      } else if (playSlot > 0) {
        sendCmd(activeSlot, "voice_stop", {});
        vmRecArmed = false;
      } else {
        vmRecArmed = !vmRecArmed;
      }
      renderAll();
    });
    document.getElementById("btn-vm-timer").addEventListener("click", () => {
      sendCmd(activeSlot, "voice_timer_toggle", {});
    });
    document.getElementById("sel-vm-timer").addEventListener("change", (e) => {
      sendCmd(activeSlot, "voice_timer_interval", { sec: Number(e.target.value) });
    });
    document.getElementById("vm-grid").addEventListener("click", (e) => {
      const btn = e.target.closest(".vm-btn");
      if (!btn || !fullState) return;
      const mem = Number(btn.dataset.vm);
      const recSlot = fullState?.voice_memory?.recording_slot || 0;
      if (recSlot > 0) {
        sendCmd(activeSlot, "voice_rec_stop", {});
        vmRecArmed = false;
        renderAll();
        return;
      }
      if (vmRecArmed) {
        vmRecArmed = false;
        sendCmd(activeSlot, "voice_rec_start", { mem });
        renderAll();
      } else {
        sendCmd(activeSlot, "voice_play", { mem });
      }
    });

    const toggleDsp = (name) => {
      const r = fullState?.radios?.[String(activeSlot)];
      if (r) sendCmd(activeSlot, "set_dsp", { name, value: !r[name] });
    };

    document.getElementById("btn-reset-pbt").addEventListener("click", () => sendCmd(activeSlot, "reset_pbt", {}));
    document.getElementById("btn-notch").addEventListener("click", () => toggleDsp("notch"));
    document.getElementById("btn-anf").addEventListener("click", () => toggleDsp("anf"));
    document.getElementById("btn-nb-left").addEventListener("click", () => toggleDsp("nb"));
    document.getElementById("btn-nb-right").addEventListener("click", () => toggleDsp("nb"));
    document.getElementById("btn-nr-right").addEventListener("click", () => toggleDsp("nr"));
    document.getElementById("btn-comp-sw").addEventListener("click", () => toggleDsp("comp_on"));
    document.getElementById("btn-mon-sw").addEventListener("click", () => toggleDsp("mon_on"));
    document.getElementById("btn-vox").addEventListener("click", () => toggleDsp("vox_on"));
    document.getElementById("btn-tone").addEventListener("click", () => toggleDsp("tone_on"));
    document.getElementById("btn-tsql").addEventListener("click", () => toggleDsp("tsql_on"));
    document.getElementById("btn-bkin").addEventListener("click", () => {
      const r = fullState?.radios?.[String(activeSlot)];
      if (r) sendCmd(activeSlot, "set_dsp", { name: "bkin", value: ((r.bkin || 0) + 1) % 3 });
    });

    document.querySelectorAll(".agc-btn").forEach((b) => {
      b.addEventListener("click", () => sendCmd(activeSlot, "set_dsp", { name: "agc", value: Number(b.dataset.agc) }));
    });
    document.querySelectorAll(".rfil-btn").forEach((b) => {
      b.addEventListener("click", () => {
        const r = fullState?.radios?.[String(activeSlot)];
        const activeVfo = r?.active_band === "SUB" ? r.sub : r?.main;
        sendCmd(activeSlot, "set_mode", { mode: activeVfo?.mode || "USB", filter: Number(b.dataset.fil), target: "selected" });
      });
    });

    // VFO cards & toolbar (on Dual-RX rigs like IC-9700, clicking SUB swaps SUB into MAIN like A/B)
    document.getElementById("vfo-card-main").addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      const r = fullState?.radios?.[String(activeSlot)];
      if (r?.supports_dual_rx) {
        if (r.active_band === "SUB") sendCmd(activeSlot, "set_active_band", { band: "MAIN" });
        return;
      }
      sendCmd(activeSlot, "set_active_band", { band: "MAIN" });
    });
    document.getElementById("vfo-card-sub").addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      const r = fullState?.radios?.[String(activeSlot)];
      if (r?.supports_dual_rx) {
        sendCmd(activeSlot, "swap_vfo");
        return;
      }
      sendCmd(activeSlot, "set_active_band", { band: "SUB" });
    });

    document.getElementById("btn-lock-vfo").addEventListener("click", () => {
      vfoLocked[activeSlot] = !vfoLocked[activeSlot];
      renderAll();
    });
    document.getElementById("btn-fine-vfo").addEventListener("click", () => {
      fineMode[activeSlot] = !fineMode[activeSlot];
      stepHz[activeSlot] = fineMode[activeSlot] ? 100 : 1000;
      renderAll();
    });
    document.getElementById("btn-dualwatch").addEventListener("click", () => {
      const r = fullState?.radios?.[String(activeSlot)];
      if (r) sendCmd(activeSlot, "set_dualwatch", { dualwatch: !r.dualwatch });
    });

    const stepVfo = (dir) => {
      if (vfoLocked[activeSlot] || !fullState) return;
      const r = fullState.radios[String(activeSlot)];
      const activeVfo = r?.active_band === "SUB" ? r.sub : r?.main;
      const step = getWaterfallStepHz(r, fineMode[activeSlot]);
      if (activeVfo?.freq_hz) {
        sendCmd(activeSlot, "set_freq", {
          freq_hz: Math.max(30000, activeVfo.freq_hz + dir * step),
          target: "selected",
        });
      }
    };
    document.getElementById("btn-step-down").addEventListener("click", () => stepVfo(-1));
    document.getElementById("btn-step-up").addEventListener("click", () => stepVfo(1));
    document.getElementById("btn-ab-swap").addEventListener("click", () => sendCmd(activeSlot, "swap_vfo"));
    document.getElementById("btn-ab-eq").addEventListener("click", () => sendCmd(activeSlot, "equalize_vfo"));
    document.getElementById("btn-split").addEventListener("click", () => {
      const r = fullState?.radios?.[String(activeSlot)];
      if (r) sendCmd(activeSlot, "set_split", { split: !r.split });
    });

    document.getElementById("btn-rit").addEventListener("click", () => toggleDsp("rit_on"));
    document.getElementById("btn-rit-down").addEventListener("click", () => {
      const r = fullState?.radios?.[String(activeSlot)];
      sendCmd(activeSlot, "set_rit_hz", { hz: (r?.rit_hz || 0) - 50 });
    });
    document.getElementById("btn-rit-up").addEventListener("click", () => {
      const r = fullState?.radios?.[String(activeSlot)];
      sendCmd(activeSlot, "set_rit_hz", { hz: (r?.rit_hz || 0) + 50 });
    });
    document.getElementById("btn-rit-clr").addEventListener("click", () => sendCmd(activeSlot, "set_rit_hz", { hz: 0 }));

    document.querySelector(".vfo-deck").addEventListener("wheel", (e) => {
      const digit = e.target.closest("[data-digit-step]");
      if (!digit || !fullState || vfoLocked[activeSlot]) return;
      e.preventDefault();
      const target = digit.dataset.target;
      const r = fullState.radios[String(activeSlot)];
      const curHz = r?.[target]?.freq_hz || 0;
      if (curHz > 0) {
        const delta = (e.deltaY < 0 ? 1 : -1) * Number(digit.dataset.digitStep);
        sendCmd(activeSlot, "set_freq", { freq_hz: Math.max(30000, curHz + delta), target });
      }
    }, { passive: false });

    // Waterfall spans + hover crosshair + 1 kHz wheel + click
    document.querySelectorAll("#single-view-body .wf-span-btn").forEach((b) => {
      b.addEventListener("click", () => sendCmd(activeSlot, "set_scope_span", { span_hz: Number(b.dataset.span) }));
    });
    document.getElementById("btn-swr-scan").addEventListener("click", () => {
      const r = fullState?.radios?.[String(activeSlot)];
      if (!r) return;
      if (r.swr_scan?.active) {
        sendCmd(activeSlot, "swr_scan_abort", {});
        return;
      }
      const activeVfo = r.active_band === "SUB" ? r.sub : r.main;
      const centerHz = activeVfo?.freq_hz || r.scope_center_hz || 0;
      const spanHz = r.scope_span_hz || 100000;
      sendCmd(activeSlot, "swr_scan_start", { center_hz: centerHz, span_hz: spanHz });
    });
    document.getElementById("btn-swr-clear").addEventListener("click", () => {
      sendCmd(activeSlot, "swr_scan_clear", {});
    });

    const scopeCanvas = document.getElementById("scope-canvas");
    scopeCanvas.addEventListener("mousemove", (e) => {
      const st = scopeStates[activeSlot];
      const rect = scopeCanvas.getBoundingClientRect();
      if (rect.width > 0 && st) {
        st.hoverRelX = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      }
    });
    scopeCanvas.addEventListener("mouseleave", () => {
      if (scopeStates[activeSlot]) scopeStates[activeSlot].hoverRelX = null;
    });
    scopeCanvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      if (!fullState || vfoLocked[activeSlot]) return;
      const r = fullState.radios[String(activeSlot)];
      const activeVfo = r?.active_band === "SUB" ? r.sub : r?.main;
      const curHz = activeVfo?.freq_hz || 0;
      if (curHz > 0) {
        const step = getWaterfallStepHz(r, fineMode[activeSlot]);
        const delta = (e.deltaY < 0 ? 1 : -1) * step;
        sendCmd(activeSlot, "set_freq", { freq_hz: Math.max(30000, curHz + delta), target: "selected" });
      }
    }, { passive: false });
    scopeCanvas.addEventListener("click", (e) => {
      if (!fullState || vfoLocked[activeSlot]) return;
      const r = fullState.radios[String(activeSlot)];
      const st = scopeStates[activeSlot];
      const rect = scopeCanvas.getBoundingClientRect();
      const relX = (e.clientX - rect.left) / rect.width;
      const span = r.scope_span_hz || st.spanHz || 100000;
      const center = r.scope_center_hz || st.centerHz || (r.main?.freq_hz || 0);
      if (center > 0) {
        const step = getWaterfallStepHz(r, fineMode[activeSlot]);
        const targetHz = Math.round((center + (relX - 0.5) * span) / step) * step;
        sendCmd(activeSlot, "set_freq", { freq_hz: targetHz, target: "selected" });
      }
    });

    // Inline repeater auto-suggest + modal browser events
    document.getElementById("btn-rep-apply").addEventListener("click", () => {
      if (currentSuggestedRep) {
        sendCmd(activeSlot, "tune_repeater", { repeater: currentSuggestedRep, target: "selected" });
      }
    });
    document.getElementById("rep-match-sel").addEventListener("change", (e) => {
      const idx = Number(e.target.value);
      const chosen = currentMatchedReps[idx];
      if (chosen) {
        currentSuggestedRep = chosen;
        sendCmd(activeSlot, "tune_repeater", { repeater: chosen, target: "selected" });
      }
    });
    document.getElementById("btn-repeaters-open").addEventListener("click", () => {
      loadRepeatersOnce(false);
      document.getElementById("repeaters-backdrop").classList.add("open");
      renderRepeaterModalTable();
    });
    document.getElementById("rep-view-list").addEventListener("click", () => setRepeaterView("list"));
    document.getElementById("rep-view-map").addEventListener("click", () => setRepeaterView("map"));
    document.getElementById("rep-gmap-layer").addEventListener("change", (e) => {
      repGmapLyrs = e.target.value || "m";
      if (repMap && repTileLayer) {
        repTileLayer.setUrl(`https://mt{s}.google.com/vt/lyrs=${repGmapLyrs}&hl=en&x={x}&y={y}&z={z}`);
      }
    });
    document.getElementById("repeaters-close").addEventListener("click", () => {
      document.getElementById("repeaters-backdrop").classList.remove("open");
    });
    document.getElementById("repeaters-backdrop").addEventListener("click", (e) => {
      if (e.target.id === "repeaters-backdrop") {
        document.getElementById("repeaters-backdrop").classList.remove("open");
      }
    });
    document.getElementById("rep-refresh-btn").addEventListener("click", () => loadRepeatersOnce(true));
    ["rep-search-input", "rep-band-filter", "rep-mode-filter", "rep-qrv-only"].forEach((id) => {
      document.getElementById(id).addEventListener("input", renderRepeaterModalTable);
      document.getElementById(id).addEventListener("change", renderRepeaterModalTable);
    });
    document.getElementById("rep-table-body").addEventListener("click", (e) => {
      const row = e.target.closest("tr[data-rep-idx]");
      if (!row) return;
      const rep = window._filteredRepsRef?.[Number(row.dataset.repIdx)];
      if (rep) {
        sendCmd(activeSlot, "tune_repeater", { repeater: rep, target: "selected" });
        document.getElementById("repeaters-backdrop").classList.remove("open");
      }
    });

    // Helper to bind any range slider cleanly
    const bindSlider = (id, min, max, onVal) => {
      const el = document.getElementById(id);
      let lastSent = 0;
      el.addEventListener("input", () => {
        const val = Number(el.value);
        setSliderVisual(el, val, min, max);
        const now = performance.now();
        if (now - lastSent > 35) {
          lastSent = now;
          onVal(val);
        }
      });
      el.addEventListener("change", () => onVal(Number(el.value)));
    };

    bindSlider("sl-win", 0, 100, (v) => {
      document.getElementById("val-sl-win").textContent = `${v}%`;
      sendCmd(activeSlot, "set_windows_volume", { volume: v });
    });
    bindSlider("sl-af", 0, 100, (v) => {
      document.getElementById("val-sl-af").textContent = String(v);
      sendCmd(activeSlot, "set_level", { name: "af_gain", value: v });
    });
    bindSlider("sl-pwr", 1, 100, (v) => {
      document.getElementById("val-sl-pwr").textContent = `${v}%`;
      sendCmd(activeSlot, "set_level", { name: "tx_power", value: v });
    });
    bindSlider("sl-rf", 0, 100, (v) => {
      document.getElementById("val-sl-rf").textContent = String(v);
      sendCmd(activeSlot, "set_level", { name: "rf_gain", value: v });
    });
    bindSlider("sl-sql", 0, 100, (v) => {
      document.getElementById("val-sl-sql").textContent = String(v);
      sendCmd(activeSlot, "set_windows_sql", { sql: v });
    });
    bindSlider("sl-pbt1", 0, 100, (v) => {
      const hz = Math.round(((v - 50) / 50) * 1200);
      document.getElementById("val-pbt1").textContent = `${hz >= 0 ? "+" : ""}${hz} Hz`;
      sendCmd(activeSlot, "set_level", { name: "pbt1", value: v });
    });
    bindSlider("sl-pbt2", 0, 100, (v) => {
      const hz = Math.round(((v - 50) / 50) * 1200);
      document.getElementById("val-pbt2").textContent = `${hz >= 0 ? "+" : ""}${hz} Hz`;
      sendCmd(activeSlot, "set_level", { name: "pbt2", value: v });
    });
    bindSlider("sl-notch-pos", 0, 100, (v) => {
      document.getElementById("val-notch-pos").textContent = `${v}%`;
      sendCmd(activeSlot, "set_level", { name: "notch_pos", value: v });
    });
    bindSlider("sl-nr-lvl", 0, 100, (v) => {
      document.getElementById("val-sl-nr").textContent = `${v}%`;
      sendCmd(activeSlot, "set_level", { name: "nr_level", value: v });
    });
    bindSlider("sl-nb-lvl", 0, 100, (v) => {
      document.getElementById("val-sl-nb").textContent = `${v}%`;
      sendCmd(activeSlot, "set_level", { name: "nb_level", value: v });
    });
    bindSlider("sl-mic", 0, 100, (v) => {
      document.getElementById("val-sl-mic").textContent = `${v}%`;
      sendCmd(activeSlot, "set_level", { name: "mic_gain", value: v });
    });
    bindSlider("sl-comp-lvl", 0, 100, (v) => {
      document.getElementById("val-sl-comp").textContent = `${v}%`;
      sendCmd(activeSlot, "set_level", { name: "comp_level", value: v });
    });
    bindSlider("sl-mon-lvl", 0, 100, (v) => {
      document.getElementById("val-sl-mon").textContent = `${v}%`;
      sendCmd(activeSlot, "set_level", { name: "mon_level", value: v });
    });
    bindSlider("sl-cw-wpm", 6, 48, (v) => {
      document.getElementById("val-sl-cw").textContent = String(v);
      sendCmd(activeSlot, "set_level", { name: "cw_wpm", value: v });
    });

    document.getElementById("btn-win-mute").addEventListener("click", () => {
      const cur = Boolean((fullState?.windows_mute || {})[String(activeSlot)] ?? (fullState?.windows_mute || {})[activeSlot]);
      sendCmd(activeSlot, "set_windows_mute", { mute: !cur });
    });

    document.querySelectorAll(".pre-btn").forEach((b) => {
      b.addEventListener("click", () => sendCmd(activeSlot, "set_dsp", { name: "preamp", value: Number(b.dataset.pre) }));
    });

    document.getElementById("btn-atu-on").addEventListener("click", () => sendCmd(activeSlot, "set_tuner", { on: true }));
    document.getElementById("btn-atu-off").addEventListener("click", () => sendCmd(activeSlot, "set_tuner", { on: false }));
    document.getElementById("btn-atu-tune").addEventListener("click", () => sendCmd(activeSlot, "start_tune", {}));
    document.getElementById("btn-passthrough").addEventListener("click", () => sendCmd(activeSlot, "passthrough_toggle", {}));
    document.getElementById("sel-pass-mic").addEventListener("change", (e) => {
      sendCmd(activeSlot, "set_passthrough_mic", { mic_ignored: e.target.value === "ignored" });
    });
    document.getElementById("btn-qso-rec").addEventListener("click", async () => {
      if (!fullState?.qso_rec?.active) {
        sendCmd(activeSlot, "qso_rec_toggle", {});
        return;
      }
      try {
        const res = await fetch("/api/qso_rec/stop", { method: "POST" });
        const d = await res.json();
        if (d.ok && d.filename) {
          if (window.pywebview?.api?.save_wav_dialog) {
            await window.pywebview.api.save_wav_dialog(d.filename);
          } else {
            const a = document.createElement("a");
            a.href = `/api/qso_rec/download?file=${encodeURIComponent(d.filename)}`;
            a.download = d.filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
          }
        }
      } catch (_) {}
    });

    // CW Keyer 4 macro buttons (Left-click: send/abort, Right-click: inline edit)
    const cwBar = document.getElementById("cw-edit-bar");
    const cwIn = document.getElementById("cw-edit-input");
    const cwOk = document.getElementById("cw-edit-ok");
    const openCwEditor = (idx) => {
      cwEditingSlot = idx;
      cwIn.value = cwMacros[idx - 1] || "";
      cwBar.style.display = "flex";
      cwIn.focus();
      cwIn.select();
    };
    const saveCwEditor = () => {
      if (cwEditingSlot < 1 || cwEditingSlot > 4) return;
      cwMacros[cwEditingSlot - 1] = cwIn.value.trim().toUpperCase();
      cwEditingSlot = 0;
      cwBar.style.display = "none";
      persistUiPrefs();
      renderAll();
    };
    cwOk.addEventListener("click", saveCwEditor);
    cwIn.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        saveCwEditor();
      } else if (e.key === "Escape") {
        e.preventDefault();
        cwEditingSlot = 0;
        cwBar.style.display = "none";
      }
    });
    document.querySelectorAll(".cw-mem-btn").forEach((btn) => {
      const idx = Number(btn.dataset.cw);
      btn.addEventListener("click", () => {
        const txt = (cwMacros[idx - 1] || "").trim();
        const r = fullState?.radios?.[String(activeSlot)];
        if (r?.cw_tx_slot === idx) {
          sendCmd(activeSlot, "cw_abort", {});
          return;
        }
        if (!txt) {
          openCwEditor(idx);
          return;
        }
        sendCmd(activeSlot, "cw_send", { macro: idx, text: txt });
      });
      btn.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        openCwEditor(idx);
      });
    });

    // Free-text CW live sender (Enter or SEND -> transmit, clear & keep focus!)
    const cwLiveIn = document.getElementById("cw-live-input");
    const cwSendBtn = document.getElementById("btn-cw-send");
    const submitLiveCw = () => {
      const txt = (cwLiveIn?.value || "").trim().toUpperCase();
      const r = fullState?.radios?.[String(activeSlot)];
      if (!txt) {
        if (r?.cw_tx_slot === 5) sendCmd(activeSlot, "cw_abort", {});
        cwLiveIn?.focus();
        return;
      }
      sendCmd(activeSlot, "cw_send", { macro: 5, text: txt });
      cwLiveIn.value = "";
      cwLiveIn.focus();
    };
    cwLiveIn?.addEventListener("input", () => {
      const liveTx = Number(fullState?.radios?.[String(activeSlot)]?.cw_tx_slot || 0) === 5;
      if (liveTx && cwSendBtn) {
        cwSendBtn.textContent = cwLiveIn.value.trim() ? "ADD TO TX" : "STOP CW";
      }
    });
    cwSendBtn?.addEventListener("click", submitLiveCw);
    cwLiveIn?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submitLiveCw();
      }
    });

    // Repeater DTMF live sender (Enter or SEND DTMF -> key PTT + tones, clear & keep focus!)
    const dtmfLiveIn = document.getElementById("dtmf-live-input");
    const dtmfSendBtn = document.getElementById("btn-dtmf-send");
    const submitLiveDtmf = () => {
      const dtmfTx = Boolean(fullState?.dtmf?.active && Number(fullState?.dtmf?.slot || 0) === activeSlot);
      const raw = (dtmfLiveIn?.value || "").trim().toUpperCase().replace(/[^0-9A-D*#,]/g, "");
      if (!raw) {
        if (dtmfTx) sendCmd(activeSlot, "abort_dtmf", {});
        dtmfLiveIn?.focus();
        return;
      }
      sendCmd(activeSlot, "send_dtmf", { digits: raw });
      dtmfLiveIn.value = "";
      dtmfLiveIn.focus();
    };
    dtmfSendBtn?.addEventListener("click", submitLiveDtmf);
    dtmfLiveIn?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        submitLiveDtmf();
      }
    });

    const pttMom = document.getElementById("btn-ptt-mom");
    pttMom.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      pttMom.setPointerCapture(e.pointerId);
      sendCmd(activeSlot, "set_ptt", { tx: true, source: "pc" });
    });
    const pttUp = () => sendCmd(activeSlot, "set_ptt", { tx: false, source: "pc" });
    pttMom.addEventListener("pointerup", pttUp);
    pttMom.addEventListener("pointercancel", pttUp);

    document.getElementById("btn-ptt-hold").addEventListener("click", () => {
      const r = fullState?.radios?.[String(activeSlot)];
      sendCmd(activeSlot, "set_ptt", { tx: !r?.tx, source: "pc" });
    });

    const wire1750 = (el) => {
      el.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        el.setPointerCapture(e.pointerId);
        sendCmd(activeSlot, "set_1750", { active: true });
      });
      const up = () => sendCmd(activeSlot, "set_1750", { active: false });
      el.addEventListener("pointerup", up);
      el.addEventListener("pointercancel", up);
    };
    wire1750(document.getElementById("btn-1750-top"));
    wire1750(document.getElementById("btn-1750-right"));

    document.getElementById("btn-bind-ptt").addEventListener("click", () => startPttBind(activeSlot));
    document.getElementById("btn-global-ptt").addEventListener("click", () => {
      sendCmd(activeSlot, "set_ptt_global", { ptt_global: !fullState?.ptt_global });
    });

    window.addEventListener("focus", () => sendCmd(1, "set_ui_focus", { focused: true }));
    window.addEventListener("blur", () => sendCmd(1, "set_ui_focus", { focused: false }));

    document.getElementById("settings-btn").addEventListener("click", openSettings);
    document.getElementById("settings-close").addEventListener("click", closeSettings);
    document.getElementById("settings-cancel").addEventListener("click", closeSettings);
    document.getElementById("refresh-devices-btn").addEventListener("click", loadSettingsData);
    document.getElementById("cfg-mode").addEventListener("change", toggleSettingsModeVisibility);
    document.getElementById("settings-save").addEventListener("click", saveSettings);
  }

  function toggleSettingsModeVisibility() {
    const isClient = document.getElementById("cfg-mode").value === "client";
    document.querySelectorAll(".client-only").forEach((el) => (el.style.display = isClient ? "flex" : "none"));
    document.querySelectorAll(".host-only").forEach((el) => (el.style.display = isClient ? "none" : "flex"));
  }

  async function loadSettingsData() {
    const [devRes, cfgRes] = await Promise.all([fetch("/api/devices"), fetch("/api/config")]);
    const devs = await devRes.json();
    const cfg = await cfgRes.json();

    const ports = devs.serial_ports || [];
    const inputs = devs.audio?.inputs || [];
    const outputs = devs.audio?.outputs || [];

    const fillPortSelect = (selId, current) => {
      const sel = document.getElementById(selId);
      sel.innerHTML = `<option value="">-- Not Selected --</option>` +
        ports.map((p) => `<option value="${p.port}">${p.port} — ${p.usb_model}${p.usb_serial ? " (" + p.usb_serial + ")" : ""}</option>`).join("");
      sel.value = current || "";
    };

    const fillAudioSelect = (selId, list, current, autoLbl) => {
      const sel = document.getElementById(selId);
      sel.innerHTML = `<option value="">${autoLbl}</option>` +
        list.map((d) => `<option value="${d.name}">${d.name}</option>`).join("");
      sel.value = current || "";
    };

    document.getElementById("cfg-mode").value = cfg.mode || "host";
    document.getElementById("cfg-qth").value = cfg.qth_locator || fullState?.qth_locator || "KP20";
    document.getElementById("cfg-client-ip").value = cfg.client?.server_ip || "";
    document.getElementById("cfg-client-port").value = cfg.client?.server_port || 8090;
    document.getElementById("cfg-srv-enabled").value = String(Boolean(cfg.server?.enabled));
    document.getElementById("cfg-srv-port").value = cfg.server?.port || 8090;
    document.getElementById("cfg-mob-port").value = cfg.mobile?.port || 8085;

    fillAudioSelect("cfg-pc-spk", outputs, cfg.pc_audio?.speaker_device, "-- Default Speakers --");
    fillAudioSelect("cfg-pc-mic", inputs, cfg.pc_audio?.mic_device, "-- Default Microphone --");
    document.getElementById("cfg-pc-vol").value = cfg.pc_audio?.master_volume ?? 85;

    const r1 = cfg.radios?.[0] || {};
    const r2 = cfg.radios?.[1] || {};
    fillPortSelect("cfg-r1-com", r1.com_port);
    document.getElementById("cfg-r1-baud").value = String(r1.baud_rate || 115200);
    fillAudioSelect("cfg-r1-ain", inputs, r1.audio_in_device, "-- Auto USB Match --");
    fillAudioSelect("cfg-r1-aout", outputs, r1.audio_out_device, "-- Auto USB Match --");
    document.getElementById("cfg-r1-h1").value = r1.hamlib_port || 4532;
    document.getElementById("cfg-r1-h2").value = r1.hamlib_port_2 || 4533;

    fillPortSelect("cfg-r2-com", r2.com_port);
    document.getElementById("cfg-r2-baud").value = String(r2.baud_rate || 115200);
    fillAudioSelect("cfg-r2-ain", inputs, r2.audio_in_device, "-- Auto USB Match --");
    fillAudioSelect("cfg-r2-aout", outputs, r2.audio_out_device, "-- Auto USB Match --");
    document.getElementById("cfg-r2-h1").value = r2.hamlib_port || 4534;
    document.getElementById("cfg-r2-h2").value = r2.hamlib_port_2 || 4535;

    const mobUrls = fullState?.mobile?.urls?.join("  |  ") || "";
    document.getElementById("mobile-url-hint").textContent = mobUrls ? `Mobile HTTPS: ${mobUrls}` : "";
    toggleSettingsModeVisibility();
  }

  async function openSettings() {
    await loadSettingsData();
    document.getElementById("settings-backdrop").classList.add("open");
  }

  function closeSettings() {
    document.getElementById("settings-backdrop").classList.remove("open");
  }

  async function saveSettings() {
    const payload = {
      mode: document.getElementById("cfg-mode").value,
      qth_locator: (document.getElementById("cfg-qth").value || "KP20").trim().toUpperCase(),
      client: {
        server_ip: document.getElementById("cfg-client-ip").value.trim(),
        server_port: Number(document.getElementById("cfg-client-port").value) || 8090,
      },
      server: {
        enabled: document.getElementById("cfg-srv-enabled").value === "true",
        port: Number(document.getElementById("cfg-srv-port").value) || 8090,
      },
      mobile: {
        port: Number(document.getElementById("cfg-mob-port").value) || 8085,
      },
      pc_audio: {
        speaker_device: document.getElementById("cfg-pc-spk").value,
        mic_device: document.getElementById("cfg-pc-mic").value,
        master_volume: Number(document.getElementById("cfg-pc-vol").value) || 85,
      },
      radios: [
        {
          slot: 1,
          enabled: true,
          com_port: document.getElementById("cfg-r1-com").value,
          baud_rate: Number(document.getElementById("cfg-r1-baud").value) || 115200,
          audio_in_device: document.getElementById("cfg-r1-ain").value,
          audio_out_device: document.getElementById("cfg-r1-aout").value,
          hamlib_port: Number(document.getElementById("cfg-r1-h1").value) || 4532,
          hamlib_port_2: Number(document.getElementById("cfg-r1-h2").value) || 4533,
        },
        {
          slot: 2,
          enabled: true,
          com_port: document.getElementById("cfg-r2-com").value,
          baud_rate: Number(document.getElementById("cfg-r2-baud").value) || 115200,
          audio_in_device: document.getElementById("cfg-r2-ain").value,
          audio_out_device: document.getElementById("cfg-r2-aout").value,
          hamlib_port: Number(document.getElementById("cfg-r2-h1").value) || 4534,
          hamlib_port_2: Number(document.getElementById("cfg-r2-h2").value) || 4535,
        },
      ],
    };

    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.state) {
      fullState = data.state;
      renderAll();
    }
    closeSettings();
  }

  function keyEventToWinVk(e) {
    const code = e.code || "";
    if (code === "Space") return { vk: 0x20, label: "Space" };
    if (code === "Enter" || code === "NumpadEnter") return { vk: 0x0d, label: "Enter" };
    if (code === "Tab") return { vk: 0x09, label: "Tab" };
    if (code === "CapsLock") return { vk: 0x14, label: "CapsLock" };
    if (code === "Pause") return { vk: 0x13, label: "Pause" };
    if (code === "Insert") return { vk: 0x2d, label: "Ins" };
    if (code === "Delete") return { vk: 0x2e, label: "Del" };
    if (code === "ShiftLeft") return { vk: 0xa0, label: "L-Shift" };
    if (code === "ShiftRight") return { vk: 0xa1, label: "R-Shift" };
    if (code === "ControlLeft") return { vk: 0xa2, label: "L-Ctrl" };
    if (code === "ControlRight") return { vk: 0xa3, label: "R-Ctrl" };
    if (code === "AltLeft") return { vk: 0xa4, label: "L-Alt" };
    if (code === "AltRight") return { vk: 0xa5, label: "R-Alt" };
    if (/^F([1-9]|1[0-2])$/.test(code)) {
      const n = Number(code.slice(1));
      return { vk: 0x6f + n, label: code };
    }
    if (/^Key([A-Z])$/.test(code)) {
      const ch = code.slice(3);
      return { vk: ch.charCodeAt(0), label: ch };
    }
    if (/^Digit([0-9])$/.test(code)) {
      const ch = code.slice(5);
      return { vk: ch.charCodeAt(0), label: ch };
    }
    if (/^Numpad([0-9])$/.test(code)) {
      const n = Number(code.slice(6));
      return { vk: 0x60 + n, label: `Num${n}` };
    }
    if (e.keyCode) return { vk: e.keyCode, label: e.key?.toUpperCase() || code };
    return null;
  }

  function mouseButtonToWinVk(button) {
    if (button === 0) return { vk: 0x01, label: "Mouse L" };
    if (button === 1) return { vk: 0x04, label: "Mouse M" };
    if (button === 2) return { vk: 0x02, label: "Mouse R" };
    if (button === 3) return { vk: 0x05, label: "Mouse 4" };
    if (button === 4) return { vk: 0x06, label: "Mouse 5" };
    return null;
  }

  function startPttBind(slot) {
    if (bindingSlot !== 0) return;
    bindingSlot = slot;
    sendCmd(slot, "set_binding_mode", { active: true });
    renderAll();

    const cleanup = () => {
      bindingSlot = 0;
      sendCmd(slot, "set_binding_mode", { active: false });
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("mousedown", onMouse, true);
      window.removeEventListener("contextmenu", suppress, true);
      window.removeEventListener("auxclick", suppress, true);
      renderAll();
    };

    const suppress = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
    };

    const onKey = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (ev.code === "Escape") {
        sendCmd(slot, "set_ptt_bind", { vk: 0, label: "" });
        cleanup();
        return;
      }
      const mapped = keyEventToWinVk(ev);
      if (mapped) {
        sendCmd(slot, "set_ptt_bind", mapped);
        cleanup();
      }
    };

    const onMouse = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const mapped = mouseButtonToWinVk(ev.button);
      if (mapped) {
        sendCmd(slot, "set_ptt_bind", mapped);
        cleanup();
      }
    };

    setTimeout(() => {
      window.addEventListener("keydown", onKey, true);
      window.addEventListener("mousedown", onMouse, true);
      window.addEventListener("contextmenu", suppress, true);
      window.addEventListener("auxclick", suppress, true);
    }, 80);
  }

  function updateClocks() {
    const now = new Date();
    const lhh = String(now.getHours()).padStart(2, "0");
    const lmm = String(now.getMinutes()).padStart(2, "0");
    const uhh = String(now.getUTCHours()).padStart(2, "0");
    const umm = String(now.getUTCMinutes()).padStart(2, "0");
    document.getElementById("local-clock").textContent = `${lhh}:${lmm}`;
    document.getElementById("utc-clock").textContent = `${uhh}:${umm}`;
  }

  function renderRepeaterSuggestion(r, activeVfo, isSub) {
    const bar = document.getElementById("rep-suggest-bar");
    if (!bar) return;
    if (!isVhfUhfRadio(r) || !activeVfo?.freq_hz) {
      bar.style.display = "none";
      currentSuggestedRep = null;
      currentMatchedReps = [];
      return;
    }

    bar.style.display = "flex";
    const qthLoc = fullState?.qth_locator || "KP20";
    const activeRep = r.active_repeater?.[isSub ? "sub" : "main"] || null;
    const matches = findMatchingRepeaters(activeVfo.freq_hz, activeRep, qthLoc);
    currentMatchedReps = matches;

    const applyBtn = document.getElementById("btn-rep-apply");
    const sel = document.getElementById("rep-match-sel");

    if (!matches.length && !activeRep) {
      bar.classList.add("is-idle");
      bar.classList.remove("is-active");
      applyBtn.classList.remove("active");
      applyBtn.textContent = "No repeater within ±5 kHz";
      applyBtn.disabled = true;
      sel.style.display = "none";
      currentSuggestedRep = null;
      return;
    }

    bar.classList.remove("is-idle");
    applyBtn.disabled = false;
    const top = matches[0] || activeRep;
    currentSuggestedRep = top;

    const isAct = Boolean(activeRep && top && activeRep.callsign === top.callsign);
    bar.classList.toggle("is-active", isAct);
    applyBtn.classList.toggle("active", isAct);
    applyBtn.textContent = formatRepSummary(top);
    syncRepeaterSelect(sel, matches, true);
  }

  let repViewMode = "list";
  let repMap = null;
  let repTileLayer = null;
  let repMarkerLayer = null;
  let repCanvasRenderer = null;
  let repGmapLyrs = "m";

  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setRepeaterView(mode) {
    repViewMode = mode === "map" ? "map" : "list";
    document.getElementById("rep-view-list").classList.toggle("active", repViewMode === "list");
    document.getElementById("rep-view-map").classList.toggle("active", repViewMode === "map");
    document.getElementById("rep-table").style.display = repViewMode === "list" ? "table" : "none";
    document.getElementById("rep-map-wrap").style.display = repViewMode === "map" ? "block" : "none";
    renderRepeaterModalTable();
    if (repViewMode === "map") {
      setTimeout(() => {
        if (repMap) repMap.invalidateSize();
      }, 60);
    }
  }

  function ensureRepeaterMap(qthLoc) {
    if (repMap || typeof window.L === "undefined") return;
    const center = maidenheadToLatLon(qthLoc) || { lat: 62.5, lon: 21.0 };
    repMap = window.L.map("rep-map", {
      center: [center.lat, center.lon],
      zoom: 6,
      zoomControl: true,
    });
    repTileLayer = window.L.tileLayer(`https://mt{s}.google.com/vt/lyrs=${repGmapLyrs}&hl=en&x={x}&y={y}&z={z}`, {
      subdomains: ["0", "1", "2", "3"],
      maxZoom: 19,
      attribution: "Map data © Google",
    }).addTo(repMap);
    repCanvasRenderer = window.L.canvas({ padding: 0.5 });
    repMarkerLayer = window.L.layerGroup().addTo(repMap);
  }

  function getBandMarkerColor(rep, isAct) {
    if (isAct) return "#10b981";
    if (rep.qrv === false) return "#ef4444";
    if (rep.band === "2m") return "#38bdf8";
    if (rep.band === "70cm") return "#f59e0b";
    if (rep.band === "23cm") return "#a78bfa";
    return "#94a3b8";
  }

  function renderRepeaterMap(filtered, qthLoc, activeRep) {
    ensureRepeaterMap(qthLoc);
    if (!repMap || !repMarkerLayer) return;
    repMarkerLayer.clearLayers();
    window._mapRepsRef = filtered;

    const qthPos = maidenheadToLatLon(qthLoc);
    if (qthPos) {
      window.L.circleMarker([qthPos.lat, qthPos.lon], {
        renderer: repCanvasRenderer,
        radius: 7,
        color: "#0284c7",
        weight: 2.5,
        fillColor: "#f8fafc",
        fillOpacity: 1,
      })
        .bindPopup(`<div class="rep-pop"><strong>QTH ${escHtml(qthLoc)}</strong></div>`)
        .addTo(repMarkerLayer);
    }

    let plotted = 0;
    for (let i = 0; i < filtered.length; i++) {
      const rep = filtered[i];
      if (!rep.lat && !rep.lon) continue;
      plotted++;
      const isAct = Boolean(activeRep && activeRep.callsign === rep.callsign && Math.abs(activeRep.rx_hz - rep.rx_hz) <= 1000);
      const col = getBandMarkerColor(rep, isAct);
      const marker = window.L.circleMarker([rep.lat, rep.lon], {
        renderer: repCanvasRenderer,
        radius: isAct ? 8 : 5.5,
        color: isAct ? "#ffffff" : "#070b13",
        weight: isAct ? 2 : 1.2,
        fillColor: col,
        fillOpacity: rep.qrv === false ? 0.45 : 0.92,
      });

      const rxMhz = (rep.rx_hz / 1e6).toFixed(4).replace(/0+$/, "").replace(/\.$/, ".0");
      const shiftStr = rep.shift_hz === 0 ? "0" : `${rep.shift_hz > 0 ? "+" : ""}${(rep.shift_hz / 1e6).toFixed(rep.shift_hz % 100000 === 0 ? 1 : 2)} MHz`;
      const distStr = rep.distKm != null ? `${rep.distKm} km · ${String(rep.bearingDeg).padStart(3, "0")}°` : (rep.locator || "-");
      const idx = i;
      marker.bindPopup(() => {
        const div = document.createElement("div");
        div.className = "rep-pop";
        div.innerHTML = `
          <div class="rep-pop-head">
            <strong class="mono" style="font-size:13px;">${escHtml(rep.callsign)}</strong>
            <span class="${rep.qrv !== false ? "rep-qrv-ok" : "rep-qrv-off"}">${rep.qrv !== false ? "QRV" : "OFF"}</span>
          </div>
          <div>${escHtml(rep.qth || "-")} ${rep.locator ? `<span class="mono" style="color:#94a3b8;">(${escHtml(rep.locator)})</span>` : ""}</div>
          <div class="mono" style="color:#38bdf8; margin-top:2px;">${rxMhz} MHz · Shift ${shiftStr}</div>
          <div class="mono">${escHtml(rep.mode || "FM")} · ${escHtml(formatRepTone(rep))}</div>
          <div class="mono" style="color:#cbd5e1;">${escHtml(distStr)}</div>
          <button class="rep-pop-tune" type="button">Tune ${escHtml(rep.callsign)}</button>
        `;
        div.querySelector(".rep-pop-tune").addEventListener("click", () => {
          const targetRep = window._mapRepsRef?.[idx];
          if (targetRep) {
            sendCmd(activeSlot, "tune_repeater", { repeater: targetRep, target: "selected" });
            document.getElementById("repeaters-backdrop").classList.remove("open");
          }
        });
        return div;
      });
      marker.addTo(repMarkerLayer);
    }
    setTimeout(() => repMap.invalidateSize(), 50);
    return plotted;
  }

  function renderRepeaterModalTable() {
    const tbody = document.getElementById("rep-table-body");
    const meta = document.getElementById("rep-cache-meta");
    if (!tbody) return;
    const qthLoc = fullState?.qth_locator || "KP20";
    const q = (document.getElementById("rep-search-input")?.value || "").trim().toLowerCase();
    const bandF = document.getElementById("rep-band-filter")?.value || "all";
    const modeF = document.getElementById("rep-mode-filter")?.value || "all";
    const qrvOnly = Boolean(document.getElementById("rep-qrv-only")?.checked);

    const r = fullState?.radios?.[String(activeSlot)];
    const activeRep = r?.active_repeater?.[r?.active_band === "SUB" ? "sub" : "main"] || null;

    const filtered = [];
    for (let i = 0; i < repeatersList.length; i++) {
      const rep = repeatersList[i];
      if (qrvOnly && rep.qrv === false) continue;
      if (bandF !== "all" && rep.band !== bandF) continue;
      if (modeF !== "all") {
        const upMode = String(rep.mode || "").toUpperCase();
        if (modeF === "D-STAR") {
          if (!rep.is_dstar && !upMode.includes("DSTAR") && !upMode.includes("D-STAR")) continue;
        } else if (!upMode.includes(modeF.toUpperCase())) {
          continue;
        }
      }
      if (q) {
        const hay = `${rep.callsign} ${rep.qth} ${rep.locator} ${(rep.rx_hz / 1e6).toFixed(4)} ${rep.mode}`.toLowerCase();
        if (!hay.includes(q)) continue;
      }
      const db = calcDistBearing(qthLoc, rep.lat, rep.lon);
      filtered.push({ ...rep, distKm: db.distKm, bearingDeg: db.bearingDeg });
    }

    filtered.sort((a, b) => {
      const da = a.distKm != null ? a.distKm : 99999;
      const db = b.distKm != null ? b.distKm : 99999;
      if (da !== db) return da - db;
      return String(a.callsign).localeCompare(String(b.callsign));
    });

    const ageDays = repeatersCachedAt ? Math.round((Date.now() - repeatersCachedAt) / 86400000) : 0;
    if (repViewMode === "map") {
      const plotted = renderRepeaterMap(filtered, qthLoc, activeRep) || 0;
      meta.textContent = `${repeatersList.length} Nordic/FI stations cached (${ageDays}d old · QTH ${qthLoc}) · ${plotted} stations on map`;
      return;
    }

    const shown = filtered.slice(0, 250);
    window._filteredRepsRef = shown;
    meta.textContent = `${repeatersList.length} Nordic/FI stations cached (${ageDays}d old · QTH ${qthLoc}) · showing ${shown.length} of ${filtered.length}`;

    tbody.innerHTML = shown
      .map((rep, idx) => {
        const rxMhz = (rep.rx_hz / 1e6).toFixed(4).replace(/0+$/, "").replace(/\.$/, ".0");
        const shiftStr = rep.shift_hz === 0 ? "0" : `${rep.shift_hz > 0 ? "+" : ""}${(rep.shift_hz / 1e6).toFixed(rep.shift_hz % 100000 === 0 ? 1 : 2)} MHz`;
        const distStr = rep.distKm != null ? `${rep.distKm} km / ${String(rep.bearingDeg).padStart(3, "0")}°` : (rep.locator || "-");
        const isAct = Boolean(activeRep && activeRep.callsign === rep.callsign && Math.abs(activeRep.rx_hz - rep.rx_hz) <= 1000);
        return `<tr data-rep-idx="${idx}" class="${isAct ? "is-active" : ""}">
          <td class="mono"><strong>${escHtml(rep.callsign)}</strong></td>
          <td>${escHtml(rep.qth || "-")}${rep.locator ? ` <span class="muted-xs mono">${escHtml(rep.locator)}</span>` : ""}</td>
          <td class="mono">${rxMhz}</td>
          <td class="mono">${shiftStr}</td>
          <td>${escHtml(rep.mode || "FM")}</td>
          <td class="mono">${escHtml(formatRepTone(rep))}</td>
          <td class="mono">${escHtml(distStr)}</td>
          <td><span class="${rep.qrv !== false ? "rep-qrv-ok" : "rep-qrv-off"}">${rep.qrv !== false ? "QRV" : "OFF"}</span></td>
          <td><button class="tbtn ${isAct ? "active" : ""}">Tune</button></td>
        </tr>`;
      })
      .join("");
  }

  function buildDualPaneHtml(slot) {
    return `
      <section id="dual-pane-${slot}" class="dual-pane slot-${slot}" data-dual-slot="${slot}">
        <div class="dual-head">
          <div class="dual-head-left">
            <span id="dual-dot-${slot}" class="tab-dot"></span>
            <span id="dual-title-${slot}" class="dual-title">Radio ${slot}</span>
            <span id="dual-rxtx-${slot}" class="vfo-pill green">RX</span>
            <span id="dual-mode-pill-${slot}" class="vfo-pill neutral">USB</span>
            <span id="dual-s-txt-${slot}" class="mono accent-xs">S0</span>
          </div>
          <div class="dual-head-right">
            <span id="dual-tele-${slot}" class="mono muted-xs">0.0A · 13.8V</span>
            <button id="dual-mute-${slot}" class="tbtn" title="Toggle Radio ${slot} Windows Audio">🔊</button>
            <button id="dual-solo-${slot}" class="tbtn" title="Switch to Single View on Radio ${slot}">Solo</button>
          </div>
        </div>

        <div class="dual-meter-strip">
          <span class="mono muted-xs" style="width:14px;">S</span>
          <canvas id="dual-bar-s-${slot}" class="dual-s-canvas"></canvas>
          <span id="dual-pwr-txt-${slot}" class="mono muted-xs"></span>
        </div>

        <div class="dual-vfo-grid">
          <div id="dual-vfo-main-${slot}" class="dual-vfo-box active">
            <div class="vfo-split-row">
              <div id="dual-freq-main-${slot}" class="dual-freq-main mono">0.000.000</div>
              <div class="vfo-meta-col">
                <div class="vfo-badges">
                  <span id="dual-tag-main-${slot}" class="vfo-pill vfo-act">VFO-A</span>
                  <span id="dual-m-mode-${slot}" class="muted-xs mono">USB</span>
                </div>
                <div class="vfo-flags">
                  <button id="dual-lock-${slot}" class="mini-pill">LOCK</button>
                  <button id="dual-fine-${slot}" class="mini-pill" title="50 Hz fine step">FINE</button>
                </div>
              </div>
            </div>
          </div>

          <div id="dual-vfo-sub-${slot}" class="dual-vfo-box">
            <div class="vfo-split-row">
              <div id="dual-freq-sub-${slot}" class="dual-freq-sub mono">0.000.000</div>
              <div class="vfo-meta-col">
                <div class="vfo-badges">
                  <span id="dual-tag-sub-${slot}" class="vfo-pill neutral">VFO-B</span>
                  <span id="dual-s-mode-${slot}" class="muted-xs mono">LSB</span>
                </div>
                <div class="vfo-flags">
                  <button id="dual-dw-${slot}" class="mini-pill" style="display:none;">DW</button>
                  <span id="dual-sub-s-${slot}" class="muted-xs mono">S0</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="dual-chip-row" style="justify-content:space-between;">
          <div id="dual-bands-${slot}" class="tool-grp" style="flex-wrap:wrap;"></div>
          <div id="dual-modes-${slot}" class="tool-grp">
            <button class="cbtn dual-mode-btn" data-mode="LSB">LSB</button>
            <button class="cbtn dual-mode-btn" data-mode="USB">USB</button>
            <button class="cbtn dual-mode-btn" data-mode="CW-U">CW</button>
            <button class="cbtn dual-mode-btn" data-mode="FM">FM</button>
            <button class="cbtn dual-mode-btn" data-mode="DATA-U">DIGI</button>
            <button class="cbtn dual-mode-btn" data-mode="DV">DV</button>
          </div>
        </div>

        <div class="dual-chip-row" style="justify-content:space-between; flex-wrap:nowrap;">
          <div class="tool-grp">
            <button id="dual-ab-${slot}" class="tbtn">A/B</button>
            <button id="dual-eq-${slot}" class="tbtn">A=B</button>
            <button id="dual-split-${slot}" class="tbtn">SPLIT</button>
            <button id="dual-nr-${slot}" class="tbtn">NR</button>
            <button id="dual-nb-${slot}" class="tbtn">NB</button>
            <button id="dual-notch-${slot}" class="tbtn">NOTCH</button>
            <button id="dual-atu-${slot}" class="tbtn" style="display:none;">ATU</button>
            <button id="dual-tune-${slot}" class="tbtn warn" style="display:none;">TUNE</button>
          </div>
          <div id="dual-rep-bar-${slot}" class="rep-suggest-bar rep-inline is-idle" style="display:none;">
            <button id="dual-rep-apply-${slot}" class="rep-apply-btn">No repeater</button>
            <select id="dual-rep-sel-${slot}" class="tsel rep-match-sel" style="display:none;"></select>
          </div>
          <div class="tool-grp">
            <button id="dual-rep-open-${slot}" class="tbtn" style="display:none;">📡 Repeaters</button>
            <button id="dual-1750-${slot}" class="tbtn warn" style="display:none;">1750 Hz</button>
          </div>
        </div>

        <div class="dual-wf-card">
          <div class="wf-toolbar">
            <div class="span-pills">
              <button class="wf-span-btn dual-span-${slot}" data-span="50000">±25k</button>
              <button class="wf-span-btn dual-span-${slot}" data-span="100000">±50k</button>
              <button class="wf-span-btn dual-span-${slot}" data-span="200000">±100k</button>
              <button class="wf-span-btn dual-span-${slot}" data-span="500000">±250k</button>
            </div>
            <span id="dual-wf-step-${slot}" class="mono muted-xs">Wheel: 1 kHz</span>
          </div>
          <canvas id="dual-scope-${slot}" class="dual-wf-canvas"></canvas>
        </div>

        <div class="dual-sliders">
          <div class="dual-sl">
            <div class="sl-head"><span>WIN</span><span id="dual-v-win-${slot}" class="sl-num mono">85%</span></div>
            <input id="dual-sl-win-${slot}" class="modern-range" type="range" min="0" max="100" value="85" />
          </div>
          <div class="dual-sl">
            <div class="sl-head"><span>AF</span><span id="dual-v-af-${slot}" class="sl-num mono">50</span></div>
            <input id="dual-sl-af-${slot}" class="modern-range" type="range" min="0" max="100" value="50" />
          </div>
          <div class="dual-sl">
            <div class="sl-head"><span>RF</span><span id="dual-v-rf-${slot}" class="sl-num mono">100</span></div>
            <input id="dual-sl-rf-${slot}" class="modern-range" type="range" min="0" max="100" value="100" />
          </div>
          <div class="dual-sl">
            <div class="sl-head"><span>SQL</span><span id="dual-v-sql-${slot}" class="sl-num mono">15</span></div>
            <input id="dual-sl-sql-${slot}" class="modern-range" type="range" min="0" max="100" value="15" />
          </div>
          <div class="dual-sl">
            <div class="sl-head"><span>PWR</span><span id="dual-v-pwr-${slot}" class="sl-num mono">50%</span></div>
            <input id="dual-sl-pwr-${slot}" class="modern-range" type="range" min="1" max="100" value="50" />
          </div>
        </div>

        <div class="dual-ptt-bar">
          <div class="tool-grp">
            <button class="cbtn dual-vm-${slot}" data-vm="1">V1</button>
            <button class="cbtn dual-vm-${slot}" data-vm="2">V2</button>
            <button class="cbtn dual-vm-${slot}" data-vm="3">V3</button>
            <button class="cbtn dual-vm-${slot}" data-vm="4">V4</button>
          </div>
          <button id="dual-ptt-${slot}" class="ptt-big-btn">PTT · R${slot}</button>
          <button id="dual-ptt-hold-${slot}" class="ptt-hold-btn">HOLD</button>
        </div>
      </section>
    `;
  }

  function initDualView() {
    const container = document.getElementById("dual-view-body");
    if (!container) return;
    container.innerHTML = buildDualPaneHtml(1) + buildDualPaneHtml(2);
    const singleEl = document.getElementById("single-view-body");
    const dualBtn = document.getElementById("btn-dual-view");
    if (singleEl) singleEl.style.display = dualView ? "none" : "grid";
    container.style.display = dualView ? "grid" : "none";
    if (dualBtn) dualBtn.classList.toggle("active", dualView);

    [1, 2].forEach((slot) => {
      const pane = document.getElementById(`dual-pane-${slot}`);
      pane.addEventListener("pointerdown", () => {
        if (activeSlot !== slot) {
          activeSlot = slot;
          [1, 2].forEach((s) => document.getElementById(`dual-pane-${s}`)?.classList.toggle("is-focus", s === activeSlot));
        }
      });

      document.getElementById(`dual-solo-${slot}`).addEventListener("click", (e) => {
        e.stopPropagation();
        activeSlot = slot;
        setDualView(false);
      });
      document.getElementById(`dual-mute-${slot}`).addEventListener("click", (e) => {
        e.stopPropagation();
        const cur = Boolean((fullState?.windows_mute || {})[String(slot)] ?? (fullState?.windows_mute || {})[slot]);
        sendCmd(slot, "set_windows_mute", { mute: !cur });
      });

      document.getElementById(`dual-vfo-main-${slot}`).addEventListener("click", () => {
        const r = fullState?.radios?.[String(slot)];
        if (r?.supports_dual_rx) {
          if (r.active_band === "SUB") sendCmd(slot, "set_active_band", { band: "MAIN" });
          return;
        }
        sendCmd(slot, "set_active_band", { band: "MAIN" });
      });
      document.getElementById(`dual-vfo-sub-${slot}`).addEventListener("click", () => {
        const r = fullState?.radios?.[String(slot)];
        if (r?.supports_dual_rx) {
          sendCmd(slot, "swap_vfo");
          return;
        }
        sendCmd(slot, "set_active_band", { band: "SUB" });
      });

      document.getElementById(`dual-lock-${slot}`).addEventListener("click", (e) => {
        e.stopPropagation();
        vfoLocked[slot] = !vfoLocked[slot];
        renderDualPane(slot);
      });
      document.getElementById(`dual-fine-${slot}`).addEventListener("click", (e) => {
        e.stopPropagation();
        fineMode[slot] = !fineMode[slot];
        renderDualPane(slot);
      });
      document.getElementById(`dual-dw-${slot}`).addEventListener("click", (e) => {
        e.stopPropagation();
        const r = fullState?.radios?.[String(slot)];
        if (r) sendCmd(slot, "set_dualwatch", { dualwatch: !r.dualwatch });
      });

      pane.querySelector(".dual-vfo-grid").addEventListener("wheel", (e) => {
        const digit = e.target.closest("[data-digit-step]");
        if (!digit || !fullState || vfoLocked[slot]) return;
        e.preventDefault();
        const target = digit.dataset.target;
        const r = fullState.radios[String(slot)];
        const curHz = r?.[target]?.freq_hz || 0;
        if (curHz > 0) {
          const delta = (e.deltaY < 0 ? 1 : -1) * Number(digit.dataset.digitStep);
          sendCmd(slot, "set_freq", { freq_hz: Math.max(30000, curHz + delta), target });
        }
      }, { passive: false });

      document.getElementById(`dual-bands-${slot}`).addEventListener("click", (e) => {
        const btn = e.target.closest(".band-btn");
        if (!btn || !fullState || vfoLocked[slot]) return;
        const r = fullState.radios[String(slot)];
        const bands = r.supports_dual_rx ? VHF_BANDS : HF_BANDS;
        const b = bands.find((x) => x.lbl === btn.dataset.bandLbl);
        if (b) sendCmd(slot, "set_freq", { freq_hz: bandMem[slot][b.lbl] || b.def, target: "selected" });
      });

      document.getElementById(`dual-modes-${slot}`).addEventListener("click", (e) => {
        const btn = e.target.closest("[data-mode]");
        if (btn) sendCmd(slot, "set_mode", { mode: btn.dataset.mode, target: "selected" });
      });

      document.getElementById(`dual-ab-${slot}`).addEventListener("click", () => sendCmd(slot, "swap_vfo"));
      document.getElementById(`dual-eq-${slot}`).addEventListener("click", () => sendCmd(slot, "equalize_vfo"));
      document.getElementById(`dual-split-${slot}`).addEventListener("click", () => {
        const r = fullState?.radios?.[String(slot)];
        if (r) sendCmd(slot, "set_split", { split: !r.split });
      });
      document.getElementById(`dual-nr-${slot}`).addEventListener("click", () => {
        const r = fullState?.radios?.[String(slot)];
        if (r) sendCmd(slot, "set_dsp", { name: "nr", value: !r.nr });
      });
      document.getElementById(`dual-nb-${slot}`).addEventListener("click", () => {
        const r = fullState?.radios?.[String(slot)];
        if (r) sendCmd(slot, "set_dsp", { name: "nb", value: !r.nb });
      });
      document.getElementById(`dual-notch-${slot}`).addEventListener("click", () => {
        const r = fullState?.radios?.[String(slot)];
        if (r) sendCmd(slot, "set_dsp", { name: "notch", value: !r.notch });
      });
      document.getElementById(`dual-atu-${slot}`).addEventListener("click", () => {
        const r = fullState?.radios?.[String(slot)];
        if (r) sendCmd(slot, "set_tuner", { on: !r.tuner_on });
      });
      document.getElementById(`dual-tune-${slot}`).addEventListener("click", () => sendCmd(slot, "start_tune"));

      document.getElementById(`dual-rep-apply-${slot}`).addEventListener("click", () => {
        if (dualSuggestedRep[slot]) sendCmd(slot, "tune_repeater", { repeater: dualSuggestedRep[slot], target: "selected" });
      });
      document.getElementById(`dual-rep-sel-${slot}`).addEventListener("change", (e) => {
        const chosen = dualMatchedReps[slot][Number(e.target.value)];
        if (chosen) {
          dualSuggestedRep[slot] = chosen;
          sendCmd(slot, "tune_repeater", { repeater: chosen, target: "selected" });
        }
      });
      document.getElementById(`dual-rep-open-${slot}`).addEventListener("click", (e) => {
        e.stopPropagation();
        activeSlot = slot;
        loadRepeatersOnce(false);
        document.getElementById("repeaters-backdrop").classList.add("open");
        renderRepeaterModalTable();
      });

      const b1750 = document.getElementById(`dual-1750-${slot}`);
      b1750.addEventListener("pointerdown", () => sendCmd(slot, "set_1750", { active: true }));
      const up1750 = () => sendCmd(slot, "set_1750", { active: false });
      b1750.addEventListener("pointerup", up1750);
      b1750.addEventListener("pointerleave", up1750);

      pane.querySelectorAll(`.dual-span-${slot}`).forEach((b) => {
        b.addEventListener("click", () => sendCmd(slot, "set_scope_span", { span_hz: Number(b.dataset.span) }));
      });

      const dScope = document.getElementById(`dual-scope-${slot}`);
      dScope.addEventListener("mousemove", (e) => {
        const rect = dScope.getBoundingClientRect();
        if (rect.width > 0 && scopeStates[slot]) {
          scopeStates[slot].hoverRelX = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        }
      });
      dScope.addEventListener("mouseleave", () => {
        if (scopeStates[slot]) scopeStates[slot].hoverRelX = null;
      });
      dScope.addEventListener("wheel", (e) => {
        e.preventDefault();
        if (!fullState || vfoLocked[slot]) return;
        const r = fullState.radios[String(slot)];
        const activeVfo = r?.active_band === "SUB" ? r.sub : r?.main;
        const curHz = activeVfo?.freq_hz || 0;
        if (curHz > 0) {
          const step = getWaterfallStepHz(r, fineMode[slot]);
          const delta = (e.deltaY < 0 ? 1 : -1) * step;
          sendCmd(slot, "set_freq", { freq_hz: Math.max(30000, curHz + delta), target: "selected" });
        }
      }, { passive: false });
      dScope.addEventListener("click", (e) => {
        if (!fullState || vfoLocked[slot]) return;
        const r = fullState.radios[String(slot)];
        const st = scopeStates[slot];
        const rect = dScope.getBoundingClientRect();
        const relX = (e.clientX - rect.left) / rect.width;
        const span = r.scope_span_hz || st.spanHz || 100000;
        const center = r.scope_center_hz || st.centerHz || (r.main?.freq_hz || 0);
        if (center > 0) {
          const step = getWaterfallStepHz(r, fineMode[slot]);
          const targetHz = Math.round((center + (relX - 0.5) * span) / step) * step;
          sendCmd(slot, "set_freq", { freq_hz: targetHz, target: "selected" });
        }
      });

      document.getElementById(`dual-sl-win-${slot}`).addEventListener("input", (e) => {
        sendCmd(slot, "set_windows_volume", { volume: Number(e.target.value) });
      });
      const bindDualLvl = (id, name) => {
        document.getElementById(id).addEventListener("input", (e) => {
          sendCmd(slot, "set_level", { name, value: Number(e.target.value) });
        });
      };
      bindDualLvl(`dual-sl-af-${slot}`, "af_gain");
      bindDualLvl(`dual-sl-rf-${slot}`, "rf_gain");
      document.getElementById(`dual-sl-sql-${slot}`).addEventListener("input", (e) => {
        sendCmd(slot, "set_windows_sql", { sql: Number(e.target.value) });
      });
      bindDualLvl(`dual-sl-pwr-${slot}`, "tx_power");

      pane.querySelectorAll(`.dual-vm-${slot}`).forEach((btn) => {
        btn.addEventListener("click", () => {
          sendCmd(slot, "voice_play", { mem: Number(btn.dataset.vm) });
        });
      });

      const pttBtn = document.getElementById(`dual-ptt-${slot}`);
      pttBtn.addEventListener("pointerdown", (e) => {
        if (pttBtn.setPointerCapture && e.pointerId != null) {
          try { pttBtn.setPointerCapture(e.pointerId); } catch (_) {}
        }
        sendCmd(slot, "set_ptt", { tx: true, source: "desktop" });
      });
      const pttUp = () => sendCmd(slot, "set_ptt", { tx: false, source: "desktop" });
      pttBtn.addEventListener("pointerup", pttUp);
      pttBtn.addEventListener("pointercancel", pttUp);
      document.getElementById(`dual-ptt-hold-${slot}`).addEventListener("click", () => {
        const r = fullState?.radios?.[String(slot)];
        if (r) sendCmd(slot, "set_ptt", { tx: !r.tx, source: "desktop" });
      });
    });
  }

  function renderDualPane(slot) {
    if (!fullState) return;
    const r = fullState.radios[String(slot)];
    if (!r) return;
    const pane = document.getElementById(`dual-pane-${slot}`);
    if (!pane) return;

    pane.classList.toggle("is-focus", activeSlot === slot);
    const isSub = r.active_band === "SUB";
    const activeVfo = isSub ? r.sub : r.main;
    const isVhfUhf = isVhfUhfRadio(r);

    document.getElementById(`dual-dot-${slot}`).className = r.tx ? "tab-dot tx" : (r.connected ? "tab-dot online" : "tab-dot");
    document.getElementById(`dual-title-${slot}`).textContent = `${r.usb_model || "Radio " + slot} (${r.com_port || "-"})`;

    const rxtx = document.getElementById(`dual-rxtx-${slot}`);
    rxtx.textContent = r.tx ? "TX" : "RX";
    rxtx.className = r.tx ? "vfo-pill red" : "vfo-pill green";

    document.getElementById(`dual-mode-pill-${slot}`).textContent = activeVfo?.mode || "---";
    document.getElementById(`dual-s-txt-${slot}`).textContent = formatS(activeVfo?.s_units);
    const winSql = getPersonalSql(slot);
    document.getElementById(`dual-pwr-txt-${slot}`).textContent = r.tx
      ? `PWR ${r.pwr_meter || 0}% · SWR ${(r.swr || 1).toFixed(1)}`
      : `SQL ${winSql}`;
    document.getElementById(`dual-tele-${slot}`).textContent = `${(r.id_amps || 0).toFixed(1)}A · ${(r.vd_volts || 0).toFixed(1)}V`;

    const muted = Boolean((fullState.windows_mute || {})[String(slot)] ?? (fullState.windows_mute || {})[slot]);
    const mBtn = document.getElementById(`dual-mute-${slot}`);
    mBtn.textContent = muted ? "🔇" : "🔊";
    mBtn.classList.toggle("warn", muted);

    document.getElementById(`dual-vfo-main-${slot}`).classList.toggle("active", !isSub);
    document.getElementById(`dual-vfo-sub-${slot}`).classList.toggle("active", isSub);
    document.getElementById(`dual-tag-main-${slot}`).textContent = r.supports_dual_rx ? "MAIN" : "VFO-A";
    document.getElementById(`dual-tag-main-${slot}`).className = !isSub ? "vfo-pill vfo-act" : "vfo-pill neutral";
    document.getElementById(`dual-tag-sub-${slot}`).textContent = r.supports_dual_rx ? "SUB" : "VFO-B";
    document.getElementById(`dual-tag-sub-${slot}`).className = isSub ? "vfo-pill vfo-act" : "vfo-pill neutral";
    document.getElementById(`dual-m-mode-${slot}`).textContent = r.main?.mode || "---";
    document.getElementById(`dual-s-mode-${slot}`).textContent = r.sub?.mode || "---";
    document.getElementById(`dual-sub-s-${slot}`).textContent = formatS(r.sub?.s_units);
    syncFreqDigits(document.getElementById(`dual-freq-main-${slot}`), r.main?.freq_hz, slot, "main");
    syncFreqDigits(document.getElementById(`dual-freq-sub-${slot}`), r.sub?.freq_hz, slot, "sub");

    document.getElementById(`dual-lock-${slot}`).classList.toggle("warn", Boolean(vfoLocked[slot]));
    document.getElementById(`dual-fine-${slot}`).classList.toggle("active", Boolean(fineMode[slot]));
    const dwBtn = document.getElementById(`dual-dw-${slot}`);
    dwBtn.style.display = r.supports_dualwatch ? "inline-block" : "none";
    dwBtn.classList.toggle("active", Boolean(r.dualwatch));

    const bands = r.supports_dual_rx ? VHF_BANDS : HF_BANDS;
    const curFreq = activeVfo?.freq_hz || 0;
    const matchedBand = findBandForFreq(bands, curFreq);
    if (matchedBand && curFreq > 0) bandMem[slot][matchedBand.lbl] = curFreq;
    syncBandButtons(document.getElementById(`dual-bands-${slot}`), bands, matchedBand);

    const curMode = activeVfo?.mode || "";
    document.querySelectorAll(`#dual-modes-${slot} .dual-mode-btn`).forEach((btn) => {
      const m = btn.dataset.mode;
      const eq =
        m === curMode ||
        (m === "CW-U" && curMode === "CW") ||
        (m === "DATA-U" && (curMode === "DIGI" || curMode === "USB-D")) ||
        (m === "DATA-L" && curMode === "LSB-D");
      btn.classList.toggle("active", eq);
    });

    document.getElementById(`dual-split-${slot}`).classList.toggle("active", Boolean(r.split));
    document.getElementById(`dual-nr-${slot}`).classList.toggle("active", Boolean(r.nr));
    document.getElementById(`dual-nb-${slot}`).classList.toggle("active", Boolean(r.nb));
    document.getElementById(`dual-notch-${slot}`).classList.toggle("active", Boolean(r.notch));
    const atuBtn = document.getElementById(`dual-atu-${slot}`);
    const tuneBtn = document.getElementById(`dual-tune-${slot}`);
    atuBtn.style.display = r.supports_tuner ? "inline-block" : "none";
    tuneBtn.style.display = r.supports_tuner ? "inline-block" : "none";
    atuBtn.classList.toggle("active", Boolean(r.tuner_on));
    tuneBtn.textContent = r.tuning ? "TUNING" : "TUNE";

    document.getElementById(`dual-rep-open-${slot}`).style.display = isVhfUhf ? "inline-block" : "none";
    document.getElementById(`dual-1750-${slot}`).style.display = isVhfUhf ? "inline-block" : "none";

    // Fixed-height repeater auto-suggest strip inside dual pane
    const repBar = document.getElementById(`dual-rep-bar-${slot}`);
    if (!isVhfUhf || !curFreq) {
      repBar.style.display = "none";
      dualSuggestedRep[slot] = null;
      dualMatchedReps[slot] = [];
    } else {
      repBar.style.display = "flex";
      const qthLoc = fullState.qth_locator || "KP20";
      const activeRep = r.active_repeater?.[isSub ? "sub" : "main"] || null;
      const matches = findMatchingRepeaters(curFreq, activeRep, qthLoc);
      dualMatchedReps[slot] = matches;
      const applyBtn = document.getElementById(`dual-rep-apply-${slot}`);
      const sel = document.getElementById(`dual-rep-sel-${slot}`);
      if (!matches.length && !activeRep) {
        repBar.classList.add("is-idle");
        repBar.classList.remove("is-active");
        applyBtn.classList.remove("active");
        applyBtn.textContent = "No repeater within ±5 kHz";
        applyBtn.disabled = true;
        sel.style.display = "none";
        dualSuggestedRep[slot] = null;
      } else {
        repBar.classList.remove("is-idle");
        applyBtn.disabled = false;
        const top = matches[0] || activeRep;
        dualSuggestedRep[slot] = top;
        const isAct = Boolean(activeRep && top && activeRep.callsign === top.callsign);
        repBar.classList.toggle("is-active", isAct);
        applyBtn.classList.toggle("active", isAct);
        applyBtn.textContent = formatRepSummary(top);
        syncRepeaterSelect(sel, matches, false);
      }
    }

    const wfStep = getWaterfallStepHz(r, fineMode[slot]);
    document.getElementById(`dual-wf-step-${slot}`).textContent =
      `Wheel: ${wfStep >= 1000 ? (wfStep / 1000) + " kHz" : wfStep + " Hz"}`;
    pane.querySelectorAll(`.dual-span-${slot}`).forEach((b) => {
      b.classList.toggle("active", Number(b.dataset.span) === (r.scope_span_hz || 100000));
    });

    const winVol = (fullState.windows_volume || {})[String(slot)] ?? (fullState.windows_volume || {})[slot] ?? 85;
    setSliderVisual(document.getElementById(`dual-sl-win-${slot}`), winVol, 0, 100);
    document.getElementById(`dual-v-win-${slot}`).textContent = `${winVol}%`;
    setSliderVisual(document.getElementById(`dual-sl-af-${slot}`), r.af_gain ?? 50, 0, 100);
    document.getElementById(`dual-v-af-${slot}`).textContent = String(r.af_gain ?? 50);
    setSliderVisual(document.getElementById(`dual-sl-rf-${slot}`), r.rf_gain ?? 100, 0, 100);
    document.getElementById(`dual-v-rf-${slot}`).textContent = String(r.rf_gain ?? 100);
    setSliderVisual(document.getElementById(`dual-sl-sql-${slot}`), winSql, 0, 100);
    document.getElementById(`dual-v-sql-${slot}`).textContent = String(winSql);
    setSliderVisual(document.getElementById(`dual-sl-pwr-${slot}`), r.tx_power ?? 50, 1, 100);
    document.getElementById(`dual-v-pwr-${slot}`).textContent = `${r.tx_power ?? 50}%`;

    const pttBtn = document.getElementById(`dual-ptt-${slot}`);
    pttBtn.classList.toggle("tx", Boolean(r.tx));
    pttBtn.textContent = r.tx ? `ON AIR · TX R${slot}` : `PTT · R${slot}`;
    document.getElementById(`dual-ptt-hold-${slot}`).classList.toggle("tx", Boolean(r.tx));
  }

  initDualView();
  attachEvents();
  connectWs();
  loadRepeatersOnce(false);
  updateClocks();
  setInterval(updateClocks, 1000);
  requestAnimationFrame(animLoop);
})();
