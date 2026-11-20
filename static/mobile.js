(function () {
  const ACCENTS = { 1: "#38bdf8", 2: "#f59e0b" };
  let ws = null;
  let fullState = null;
  let activeSlot = 1;
  let settingsOpen = false;

  // Independent Mobile Audio State (both radios OFF/muted by default)
  const rxEnabled = { 1: false, 2: false };
  let mobileSql = 15;
  let mobileAf = 85;
  let stepHz = 100;

  const REP_CACHE_KEY = "nordic_repeaters_cache_v2";
  try { localStorage.removeItem("nordic_repeaters_cache_v1"); } catch (_) {}
  const REP_CACHE_TTL_MS = 30 * 24 * 60 * 60 * 1000;
  let repeatersList = [];
  let repeatersCachedAt = 0;
  let repeatersFetching = false;
  let currentMobRep = null;
  let currentMobMatches = [];
  let mobClosestQrvList = [];
  let mobClosestQrvKey = "";

  function getMobClosestQrvRepeaters(qthLoc) {
    const key = `${qthLoc || "KP20"}|${repeatersList.length}|${repeatersCachedAt}`;
    if (key === mobClosestQrvKey) return mobClosestQrvList;
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
    mobClosestQrvList = Array.from(byFreq.values()).sort((a, b) => a.rx_hz - b.rx_hz);
    mobClosestQrvKey = key;
    return mobClosestQrvList;
  }

  function sqlPctToSUnits(sqlPct) {
    const p = Math.max(0, Math.min(100, Number(sqlPct || 0)));
    if (p <= 50) return 1 + (p / 50) * 4;
    if (p <= 75) return 5 + ((p - 50) / 25) * 4;
    return 9 + ((p - 75) / 25) * 6;
  }

  function updateMobSqlMark() {
    const sqlS = sqlPctToSUnits(mobileSql);
    const sqlPctPos = Math.min(100, Math.max(0, ((sqlS - 1) / 14) * 100));
    const el = document.getElementById("mob-sql-mark");
    if (el) el.style.left = `${sqlPctPos}%`;
  }

  function maidenheadToLatLon(loc) {
    const s = String(loc || "").trim().toUpperCase();
    if (s.length < 4) return null;
    const fLon = s.charCodeAt(0) - 65, fLat = s.charCodeAt(1) - 65;
    const sLon = parseInt(s[2], 10), sLat = parseInt(s[3], 10);
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

  function repBandOfHz(hz) {
    if (hz >= 28000000 && hz <= 30000000) return "10m";
    if (hz >= 50000000 && hz <= 54000000) return "6m";
    if (hz >= 144000000 && hz <= 148000000) return "2m";
    if (hz >= 430000000 && hz <= 440000000) return "70cm";
    if (hz >= 1240000000 && hz <= 1300000000) return "23cm";
    return "other";
  }

  function normalizeMobReps(rawList) {
    return (rawList || []).map((r) => {
      const rxHz = Number(r.rx_hz || 0);
      const shiftHz = Number(r.offset_hz != null ? r.offset_hz : (r.shift_hz || 0));
      const qrv = r.qrv != null ? Boolean(r.qrv) : (String(r.status || "QRV").toUpperCase() === "QRV");
      return { ...r, rx_hz: rxHz, shift_hz: shiftHz, offset_hz: shiftHz, qrv, qth: r.city || r.qth || "", band: r.band || repBandOfHz(rxHz) };
    });
  }

  async function loadMobileRepeatersOnce(force = false) {
    if (repeatersFetching) return;
    if (!force && repeatersList.length > 0 && (Date.now() - repeatersCachedAt) < REP_CACHE_TTL_MS) return;
    if (!force) {
      try {
        const cached = JSON.parse(localStorage.getItem(REP_CACHE_KEY) || "null");
        const arr = cached?.repeaters || cached?.stations;
        if (cached && Array.isArray(arr) && arr.length > 0 && (Date.now() - (cached.cached_at || 0)) < REP_CACHE_TTL_MS) {
          repeatersList = normalizeMobReps(arr);
          repeatersCachedAt = cached.cached_at;
          if (fullState) render();
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
        repeatersList = normalizeMobReps(raw);
        repeatersCachedAt = Date.now();
        try {
          localStorage.setItem(REP_CACHE_KEY, JSON.stringify({ cached_at: repeatersCachedAt, repeaters: repeatersList }));
        } catch (_) {}
        if (fullState) render();
        if (document.getElementById("mob-rep-modal")?.classList.contains("open")) renderMobRepeaterList();
      }
    } catch (_) {
    } finally {
      repeatersFetching = false;
    }
  }

  let audioCtx = null;
  let gainNode = null;
  let rxNode = null;
  let micStream = null;
  let micProcessor = null;
  let micZeroGain = null;
  let micInitPromise = null;
  let pttHeld = false;

  function createRxRing() {
    const cap = 24000;
    return {
      buf: new Float32Array(cap),
      cap,
      r: 0,
      w: 0,
      count: 0,
      primed: false,
      prefill: 3840, // ~80ms Wi-Fi jitter buffer
      maxFill: 9600, // ~200ms max latency
    };
  }

  const rxRings = {
    1: createRxRing(),
    2: createRxRing(),
  };

  function clearRxRing(slot) {
    const ring = rxRings[slot];
    if (!ring) return;
    ring.r = 0;
    ring.w = 0;
    ring.count = 0;
    ring.primed = false;
  }

  function pushToRing(ring, samples) {
    const n = samples.length;
    if (n === 0) return;
    if (ring.count + n > ring.maxFill) {
      const drop = Math.min(ring.count, (ring.count + n) - ring.prefill);
      ring.r = (ring.r + drop) % ring.cap;
      ring.count -= drop;
    }
    for (let i = 0; i < n; i++) {
      ring.buf[ring.w] = samples[i];
      ring.w = (ring.w + 1) % ring.cap;
    }
    ring.count += n;
    if (!ring.primed && ring.count >= ring.prefill) {
      ring.primed = true;
    }
  }

  function popFromRing(ring, outArr) {
    const n = outArr.length;
    if (!ring.primed) {
      outArr.fill(0);
      return;
    }
    if (ring.count < n) {
      ring.primed = false;
      outArr.fill(0);
      return;
    }
    for (let i = 0; i < n; i++) {
      outArr[i] = ring.buf[ring.r];
      ring.r = (ring.r + 1) % ring.cap;
    }
    ring.count -= n;
  }

  const scopeStates = {
    1: window.ScopeRenderer.createScopeState(),
    2: window.ScopeRenderer.createScopeState(),
  };

  let pingTimer = null;

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws/mobile`);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      sendMobileCfg();
      if (pingTimer) clearInterval(pingTimer);
      pingTimer = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          try { ws.send(JSON.stringify({ type: "ping" })); } catch (_) {}
        }
      }, 1500);
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data);
        if (msg.type === "state") {
          fullState = msg.data;
          // Sync flywheel local target if not actively spinning
          if (!flywheel.dragging && Math.abs(flywheel.vel) < 5) {
            const r = fullState.radios[String(activeSlot)];
            const vfo = r?.active_band === "SUB" ? r.sub : r?.main;
            if (vfo?.freq_hz) flywheel.freqHz = vfo.freq_hz;
          }
          render();
        } else if (msg.type === "scope") {
          if (scopeStates[msg.slot]) {
            window.ScopeRenderer.pushSpectrumRow(
              scopeStates[msg.slot],
              msg.bins,
              msg.center_hz,
              msg.span_hz,
              "classic"
            );
          }
        }
      } else if (ev.data instanceof ArrayBuffer) {
        if ((rxEnabled[1] || rxEnabled[2]) && audioCtx) {
          playPcmPacket(ev.data);
        }
      }
    };
    ws.onclose = () => {
      if (pingTimer) {
        clearInterval(pingTimer);
        pingTimer = null;
      }
      setTimeout(connectWs, 1500);
    };
  }

  function closeMobWsCleanly() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: "mobile_cfg", listen_radios: [] }));
        ws.close(1000, "pagehide");
      } catch (_) {}
    }
  }
  window.addEventListener("pagehide", closeMobWsCleanly);
  window.addEventListener("beforeunload", closeMobWsCleanly);
  window.addEventListener("pageshow", () => {
    if (!ws || ws.readyState === WebSocket.CLOSED) connectWs();
  });

  function sendMobileCfg() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      const listen = [];
      if (rxEnabled[1]) listen.push(1);
      if (rxEnabled[2]) listen.push(2);
      ws.send(JSON.stringify({
        type: "mobile_cfg",
        listen_radios: listen,
        mobile_sql: mobileSql,
        mobile_af: mobileAf,
        sample_rate: audioCtx ? audioCtx.sampleRate : 48000,
      }));
    }
  }

  function sendCmd(slot, action, params = {}) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "cmd", slot, action, params }));
    }
  }

  function ensureAudioContext() {
    if (!audioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      audioCtx = new AC();
      gainNode = audioCtx.createGain();
      gainNode.gain.value = mobileAf / 100;
      gainNode.connect(audioCtx.destination);

      const bufLen = 2048;
      const tmp1 = new Float32Array(bufLen);
      const tmp2 = new Float32Array(bufLen);
      rxNode = audioCtx.createScriptProcessor(bufLen, 0, 1);
      rxNode.onaudioprocess = (ev) => {
        const out = ev.outputBuffer.getChannelData(0);
        if (!rxEnabled[1] && !rxEnabled[2]) {
          out.fill(0);
          return;
        }
        if (rxEnabled[1]) popFromRing(rxRings[1], tmp1); else tmp1.fill(0);
        if (rxEnabled[2]) popFromRing(rxRings[2], tmp2); else tmp2.fill(0);
        for (let i = 0; i < out.length; i++) {
          out[i] = Math.tanh((tmp1[i] + tmp2[i]) * 1.8);
        }
      };
      rxNode.connect(gainNode);
      sendMobileCfg();
    }
    if (audioCtx.state === "suspended") {
      audioCtx.resume();
    }
  }

  function playPcmPacket(buf) {
    if (!audioCtx || buf.byteLength < 3) return;
    const view = new Uint8Array(buf);
    const slot = view[0];
    if (!rxEnabled[slot]) return;
    const ring = rxRings[slot];
    if (!ring) return;

    const sampleCount = (buf.byteLength - 1) >> 1;
    const dv = new DataView(buf, 1);
    const srOut = audioCtx.sampleRate || 48000;

    if (srOut === 48000) {
      const samples = new Float32Array(sampleCount);
      for (let i = 0; i < sampleCount; i++) {
        samples[i] = dv.getInt16(i * 2, true) / 32768.0;
      }
      pushToRing(ring, samples);
    } else {
      const targetCount = Math.max(1, Math.round(sampleCount * (srOut / 48000)));
      const samples = new Float32Array(targetCount);
      for (let i = 0; i < targetCount; i++) {
        const pos = (i / targetCount) * (sampleCount - 1);
        const lo = Math.floor(pos);
        const hi = Math.min(sampleCount - 1, lo + 1);
        const frac = pos - lo;
        const s0 = dv.getInt16(lo * 2, true) / 32768.0;
        const s1 = dv.getInt16(hi * 2, true) / 32768.0;
        samples[i] = s0 * (1 - frac) + s1 * frac;
      }
      pushToRing(ring, samples);
    }
  }

  async function ensureMobileMic() {
    ensureAudioContext();
    if (micStream) return true;
    if (micInitPromise) return micInitPromise;

    micInitPromise = (async () => {
      try {
        micStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true },
        });
        const src = audioCtx.createMediaStreamSource(micStream);
        micProcessor = audioCtx.createScriptProcessor(1024, 1, 1);
        micProcessor.onaudioprocess = (e) => {
          if (!pttHeld || !ws || ws.readyState !== WebSocket.OPEN) return;
          const input = e.inputBuffer.getChannelData(0);
          const out = new Uint8Array(1 + input.length * 2);
          out[0] = activeSlot;
          const dv = new DataView(out.buffer, 1);
          for (let i = 0; i < input.length; i++) {
            const s = Math.max(-1, Math.min(1, input[i]));
            dv.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
          }
          ws.send(out.buffer);
        };
        micZeroGain = audioCtx.createGain();
        micZeroGain.gain.value = 0;
        src.connect(micProcessor);
        micProcessor.connect(micZeroGain);
        micZeroGain.connect(audioCtx.destination);
        sendMobileCfg();
        return true;
      } catch (err) {
        alert("Microphone access requires HTTPS and browser permission: " + err.message);
        return false;
      } finally {
        micInitPromise = null;
      }
    })();

    return micInitPromise;
  }

  function updateAudioButtons() {
    const b1 = document.getElementById("mob-rx1-btn");
    const b2 = document.getElementById("mob-rx2-btn");
    b1.textContent = rxEnabled[1] ? "RX1: ON" : "RX1: OFF";
    b1.className = rxEnabled[1] ? "pill-btn on" : "pill-btn";
    b2.textContent = rxEnabled[2] ? "RX2: ON" : "RX2: OFF";
    b2.className = rxEnabled[2] ? "pill-btn on" : "pill-btn";

    const c1 = document.getElementById("mob-cfg-rx1");
    const c2 = document.getElementById("mob-cfg-rx2");
    c1.textContent = rxEnabled[1] ? "Radio 1 Audio: ON" : "Radio 1 Audio: OFF";
    c1.className = rxEnabled[1] ? "pill-btn on" : "pill-btn";
    c2.textContent = rxEnabled[2] ? "Radio 2 Audio: ON" : "Radio 2 Audio: OFF";
    c2.className = rxEnabled[2] ? "pill-btn on" : "pill-btn";
  }

  function toggleRadioAudio(slot) {
    ensureAudioContext();
    rxEnabled[slot] = !rxEnabled[slot];
    if (!rxEnabled[slot]) clearRxRing(slot);
    updateAudioButtons();
    sendMobileCfg();
  }

  function fmtMhz(hz) {
    const s = String(Math.max(0, Math.round(hz || 0))).padStart(9, "0");
    return `${parseInt(s.slice(0, 3), 10)}.${s.slice(3, 6)}.${s.slice(6, 9)}`;
  }

  function render() {
    if (!fullState) return;
    const r = fullState.radios[String(activeSlot)];
    if (!r) return;

    document.getElementById("mob-rig-name").textContent = `${r.usb_model || "Radio " + activeSlot} (${r.connected ? "OK" : "OFF"})`;
    const activeVfo = r.active_band === "SUB" ? r.sub : r.main;
    const sUnits = activeVfo?.s_units || 0;
    document.getElementById("mob-s-text").textContent = r.tx
      ? `TX ${r.pwr_meter}% · SWR ${r.swr.toFixed(2)}`
      : (sUnits <= 9 ? `S${sUnits.toFixed(1)}` : `S9+${Math.round((sUnits - 9) * 10)}dB`);
    const pct = r.tx ? (r.pwr_meter || 0) : Math.min(100, Math.max(0, ((sUnits - 1) / 14) * 100));
    document.getElementById("mob-s-fill").style.width = `${pct}%`;

    document.getElementById("mob-tag-main").textContent = r.supports_dual_rx ? "MAIN" : "VFO A";
    document.getElementById("mob-tag-sub").textContent = r.supports_dual_rx ? "SUB" : "VFO B";

    // While flywheel is spinning, show live local frequency immediately without snapping back
    const spinning = flywheel.dragging || Math.abs(flywheel.vel) >= 5;
    const mainFreq = spinning && r.active_band !== "SUB" ? flywheel.freqHz : r.main?.freq_hz;
    const subFreq = spinning && r.active_band === "SUB" ? flywheel.freqHz : r.sub?.freq_hz;

    document.getElementById("mob-freq-main").textContent = fmtMhz(mainFreq);
    document.getElementById("mob-freq-sub").textContent = fmtMhz(subFreq);
    document.getElementById("mob-mode-main").textContent = r.main?.mode || "---";
    document.getElementById("mob-mode-sub").textContent = r.sub?.mode || "---";

    document.getElementById("mob-vfo-main").classList.toggle("active", r.active_band !== "SUB");
    document.getElementById("mob-vfo-sub").classList.toggle("active", r.active_band === "SUB");

    const modeSel = document.getElementById("mob-mode-sel");
    if (["LSB", "USB", "CW", "FM", "DV", "DIGI"].includes(activeVfo?.mode)) {
      modeSel.value = activeVfo.mode;
    }

    const dwBtn = document.getElementById("mob-dw-btn");
    dwBtn.style.display = r.supports_dual_rx ? "inline-block" : "none";
    dwBtn.className = r.dualwatch ? "pill-btn on" : "pill-btn";

    const tunerBtn = document.getElementById("mob-tuner-btn");
    const tuneBtn = document.getElementById("mob-tune-btn");
    tunerBtn.style.display = r.supports_tuner ? "inline-block" : "none";
    tuneBtn.style.display = r.supports_tuner ? "inline-block" : "none";
    if (r.supports_tuner) {
      tunerBtn.textContent = r.tuner_on ? "Tuner: ON" : "Tuner: OFF";
      tunerBtn.className = r.tuner_on ? "pill-btn on" : "pill-btn";
      tuneBtn.textContent = r.tuning ? "TUNING..." : "TUNE";
      tuneBtn.className = r.tuning ? "pill-btn active" : "pill-btn warn";
    }

    const txBox = document.getElementById("mob-tx-telemetry");
    txBox.classList.toggle("active", Boolean(r.tx));
    if (r.tx) {
      const pwr = Math.max(0, Math.min(100, r.pwr_meter || 0));
      const alc = Math.max(0, Math.min(100, r.alc || 0));
      const swr = Math.max(1.0, r.swr || 1.0);
      const swrPct = Math.max(0, Math.min(100, ((swr - 1.0) / 3.0) * 100));

      document.getElementById("mob-tx-pwr").textContent = `${pwr}%`;
      document.getElementById("mob-tx-pwr-bar").style.width = `${pwr}%`;

      document.getElementById("mob-tx-alc").textContent = `${alc}%`;
      const alcBar = document.getElementById("mob-tx-alc-bar");
      alcBar.style.width = `${alc}%`;
      alcBar.style.background = alc >= 80 ? "var(--red)" : (alc >= 55 ? "var(--amber)" : "var(--green)");

      document.getElementById("mob-tx-swr").textContent = swr.toFixed(2);
      const swrBar = document.getElementById("mob-tx-swr-bar");
      swrBar.style.width = `${swrPct}%`;
      swrBar.style.background = swr >= 2.0 ? "var(--red)" : (swr >= 1.5 ? "var(--amber)" : "var(--green)");
    }

    const toneBtn = document.getElementById("mob-1750-btn");
    const isVhfUhf = r.supports_dual_rx || ((activeVfo?.freq_hz || 0) >= 30000000);
    toneBtn.style.display = isVhfUhf ? "inline-block" : "none";
    document.getElementById("mob-rep-open").style.display = isVhfUhf ? "inline-block" : "none";
    renderMobRepeaterSuggestion(r, activeVfo, isVhfUhf);

    const ptt = document.getElementById("mob-ptt-btn");
    ptt.textContent = r.tx ? `ON AIR · TX (${r.pwr_meter}%)` : `HOLD PTT (TX + MIC) · ${r.usb_model || "R" + activeSlot}`;
    ptt.className = r.tx ? "ptt-big tx" : "ptt-big";

    document.getElementById("mob-nb").className = r.nb ? "pill-btn on" : "pill-btn";
    document.getElementById("mob-nr").className = r.nr ? "pill-btn on" : "pill-btn";
    document.getElementById("mob-notch").className = r.notch ? "pill-btn on" : "pill-btn";
    document.getElementById("mob-split").className = r.split ? "pill-btn warn" : "pill-btn";
    document.getElementById("mob-pre").className = r.preamp ? "pill-btn on" : "pill-btn";
    document.getElementById("mob-pwr").value = r.tx_power ?? 50;
    document.getElementById("mob-pwr-val").textContent = `${r.tx_power ?? 50}%`;
  }

  // --- WEIGHTED FLYWHEEL VFO PHYSICS & RENDERING ---
  const flywheel = {
    canvas: document.getElementById("mob-flywheel"),
    phasePx: 0,
    vel: 0,           // px/s
    dragging: false,
    lastX: 0,
    lastTs: 0,
    samples: [],      // recent {dx, dt} for clean flick velocity
    stepAccum: 0,
    pxPerStep: 14,
    freqHz: 0,
    lastSentTs: 0,
    dirtySend: false,
    frameTs: performance.now(),
  };

  function stepFlywheelByPx(dx) {
    flywheel.phasePx += dx;
    flywheel.stepAccum -= dx / flywheel.pxPerStep;
    const steps = Math.trunc(flywheel.stepAccum);
    if (steps !== 0) {
      flywheel.stepAccum -= steps;
      if (flywheel.freqHz > 0) {
        flywheel.freqHz = Math.max(30000, flywheel.freqHz + steps * stepHz);
        flywheel.dirtySend = true;
        // Update live frequency text immediately at 60fps
        const r = fullState?.radios?.[String(activeSlot)];
        if (r) {
          const id = r.active_band === "SUB" ? "mob-freq-sub" : "mob-freq-main";
          document.getElementById(id).textContent = fmtMhz(flywheel.freqHz);
        }
      }
    }
  }

  function updateFlywheelPhysics(now) {
    const dtMs = Math.max(1, Math.min(45, now - flywheel.frameTs));
    flywheel.frameTs = now;

    if (!flywheel.dragging && Math.abs(flywheel.vel) >= 4) {
      const dtSec = dtMs / 1000;
      stepFlywheelByPx(flywheel.vel * dtSec);
      // Smooth heavy flywheel exponential coasting decay
      flywheel.vel *= Math.exp(-dtMs / 360);
      if (Math.abs(flywheel.vel) < 4) {
        flywheel.vel = 0;
        if (flywheel.dirtySend && flywheel.freqHz > 0) {
          flywheel.dirtySend = false;
          sendCmd(activeSlot, "set_freq", { freq_hz: flywheel.freqHz, target: "selected" });
        }
      }
    }

    if (flywheel.dirtySend && (now - flywheel.lastSentTs >= 45)) {
      flywheel.lastSentTs = now;
      flywheel.dirtySend = false;
      if (flywheel.freqHz > 0) {
        sendCmd(activeSlot, "set_freq", { freq_hz: flywheel.freqHz, target: "selected" });
      }
    }
  }

  function drawFlywheel() {
    const canvas = flywheel.canvas;
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
    const accent = ACCENTS[activeSlot];

    // Cylindrical machined drum shading (darker curved sides, lighter center)
    const drumGrad = ctx.createLinearGradient(0, 0, w, 0);
    drumGrad.addColorStop(0.0, "#05070c");
    drumGrad.addColorStop(0.22, "#141c2b");
    drumGrad.addColorStop(0.50, "#1f2a3f");
    drumGrad.addColorStop(0.78, "#141c2b");
    drumGrad.addColorStop(1.0, "#05070c");
    ctx.fillStyle = drumGrad;
    ctx.fillRect(0, 0, w, h);

    // Scrolling knurled vertical notches (with subtle cylindrical perspective spacing)
    const spacing = 14;
    const offset = ((flywheel.phasePx % spacing) + spacing) % spacing;
    for (let x = offset - spacing; x <= w + spacing; x += spacing) {
      const distNorm = Math.abs(x - w / 2) / (w / 2);
      const alpha = Math.max(0.08, 0.55 * (1 - distNorm * 0.75));
      ctx.fillStyle = `rgba(226, 232, 240, ${alpha})`;
      ctx.fillRect(Math.round(x), 7, 2, h - 14);
      ctx.fillStyle = `rgba(2, 6, 14, ${alpha * 0.9})`;
      ctx.fillRect(Math.round(x) + 2, 7, 1.5, h - 14);
    }

    // Center tuning pointer
    ctx.fillStyle = accent;
    ctx.fillRect(w / 2 - 1.5, 0, 3, h);

    // Subtle label & momentum indicator
    ctx.font = "700 10px ui-monospace, Menlo, monospace";
    ctx.textAlign = "left";
    ctx.fillStyle = "rgba(148, 163, 184, 0.75)";
    ctx.fillText("◀ FLYWHEEL VFO", 8, 13);
    ctx.textAlign = "right";
    ctx.fillText(`${stepHz >= 1000 ? "1k" : stepHz}Hz/step ▶`, w - 8, 13);

    ctx.restore();
  }

  flywheel.canvas.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    flywheel.canvas.setPointerCapture(e.pointerId);
    flywheel.dragging = true;
    flywheel.vel = 0; // touching a spinning flywheel brakes it immediately
    flywheel.samples = [];
    flywheel.lastX = e.clientX;
    flywheel.lastTs = performance.now();

    const r = fullState?.radios?.[String(activeSlot)];
    const vfo = r?.active_band === "SUB" ? r.sub : r?.main;
    if (vfo?.freq_hz) flywheel.freqHz = vfo.freq_hz;
  });

  flywheel.canvas.addEventListener("pointermove", (e) => {
    if (!flywheel.dragging) return;
    const now = performance.now();
    const dx = e.clientX - flywheel.lastX;
    const dt = Math.max(1, now - flywheel.lastTs);
    flywheel.lastX = e.clientX;
    flywheel.lastTs = now;

    stepFlywheelByPx(dx);
    flywheel.samples.push({ dx, dt, ts: now });
    // Keep only recent ~70ms of samples for crisp flick release
    while (flywheel.samples.length > 0 && now - flywheel.samples[0].ts > 70) {
      flywheel.samples.shift();
    }
  });

  const endFlywheelDrag = () => {
    if (!flywheel.dragging) return;
    flywheel.dragging = false;
    const now = performance.now();
    const recent = flywheel.samples.filter((s) => now - s.ts <= 80);
    let sumDx = 0;
    let sumDt = 0;
    recent.forEach((s) => { sumDx += s.dx; sumDt += s.dt; });
    if (sumDt > 0 && Math.abs(sumDx) > 4) {
      const v = (sumDx / sumDt) * 1000;
      flywheel.vel = Math.max(-2800, Math.min(2800, v));
    } else {
      flywheel.vel = 0;
    }
    if (flywheel.dirtySend && flywheel.freqHz > 0) {
      flywheel.dirtySend = false;
      sendCmd(activeSlot, "set_freq", { freq_hz: flywheel.freqHz, target: "selected" });
    }
  };
  flywheel.canvas.addEventListener("pointerup", endFlywheelDrag);
  flywheel.canvas.addEventListener("pointercancel", endFlywheelDrag);

  function animLoop() {
    if (fullState && !settingsOpen) {
      updateFlywheelPhysics(performance.now());
      drawFlywheel();

      const r = fullState.radios[String(activeSlot)];
      if (r) {
        const activeVfo = r.active_band === "SUB" ? r.sub : r.main;
        const isVhfUhf = r.supports_dual_rx || ((activeVfo?.freq_hz || 0) >= 30000000);
        const activeRep = r.active_repeater?.[r.active_band === "SUB" ? "sub" : "main"] || null;
        window.ScopeRenderer.renderScope(
          document.getElementById("mob-scope-canvas"),
          scopeStates[activeSlot],
          {
            accent: ACCENTS[activeSlot],
            palette: "classic",
            connected: r.connected,
            tx: r.tx,
            mode: activeVfo?.mode,
            filterHz: activeVfo?.filter_hz || 2400,
            spanHz: r.scope_span_hz || 100000,
            centerHz: r.scope_center_hz || activeVfo?.freq_hz || 0,
            freqHz: activeVfo?.freq_hz || 0,
            repeaters: isVhfUhf ? getMobClosestQrvRepeaters(fullState.qth_locator || "KP20") : null,
            activeRepeater: activeRep,
          }
        );
      }
    }
    requestAnimationFrame(animLoop);
  }

  function showTab(tab) {
    settingsOpen = tab === "cfg";
    document.getElementById("tab-r1").classList.toggle("active", tab === "r1");
    document.getElementById("tab-r2").classList.toggle("active", tab === "r2");
    document.getElementById("tab-cfg").classList.toggle("active", tab === "cfg");
    document.getElementById("main-pane").classList.toggle("hidden", settingsOpen);
    document.getElementById("settings-pane").classList.toggle("open", settingsOpen);
    if (tab === "cfg" && fullState?.qth_locator) {
      document.getElementById("mob-qth-in").value = fullState.qth_locator;
    } else if (tab === "r1" || tab === "r2") {
      activeSlot = tab === "r1" ? 1 : 2;
      flywheel.vel = 0;
      const r = fullState?.radios?.[String(activeSlot)];
      const vfo = r?.active_band === "SUB" ? r.sub : r?.main;
      if (vfo?.freq_hz) flywheel.freqHz = vfo.freq_hz;
    }
    render();
  }

  document.getElementById("tab-r1").addEventListener("click", () => showTab("r1"));
  document.getElementById("tab-r2").addEventListener("click", () => showTab("r2"));
  document.getElementById("tab-cfg").addEventListener("click", () => showTab("cfg"));

  document.getElementById("mob-rx1-btn").addEventListener("click", () => toggleRadioAudio(1));
  document.getElementById("mob-rx2-btn").addEventListener("click", () => toggleRadioAudio(2));
  document.getElementById("mob-cfg-rx1").addEventListener("click", () => toggleRadioAudio(1));
  document.getElementById("mob-cfg-rx2").addEventListener("click", () => toggleRadioAudio(2));

  document.getElementById("mob-vfo-main").addEventListener("click", () => sendCmd(activeSlot, "set_active_band", { band: "MAIN" }));
  document.getElementById("mob-vfo-sub").addEventListener("click", () => sendCmd(activeSlot, "set_active_band", { band: "SUB" }));

  document.getElementById("mob-step-sel").addEventListener("change", (e) => {
    stepHz = Number(e.target.value) || 100;
  });

  document.getElementById("mob-mode-sel").addEventListener("change", (e) => {
    sendCmd(activeSlot, "set_mode", { mode: e.target.value, target: "selected" });
  });

  document.getElementById("mob-dw-btn").addEventListener("click", () => {
    const r = fullState?.radios?.[String(activeSlot)];
    sendCmd(activeSlot, "set_dualwatch", { dualwatch: !r?.dualwatch });
  });

  document.getElementById("mob-tuner-btn").addEventListener("click", () => {
    const r = fullState?.radios?.[String(activeSlot)];
    sendCmd(activeSlot, "set_tuner", { on: !r?.tuner_on });
  });

  document.getElementById("mob-tune-btn").addEventListener("click", () => {
    sendCmd(activeSlot, "start_tune", {});
  });

  document.getElementById("mob-sql").addEventListener("input", (e) => {
    mobileSql = Number(e.target.value);
    document.getElementById("mob-sql-val").textContent = `${mobileSql}%`;
    updateMobSqlMark();
    sendMobileCfg();
  });

  document.getElementById("mob-af").addEventListener("input", (e) => {
    mobileAf = Number(e.target.value);
    document.getElementById("mob-af-val").textContent = `${mobileAf}%`;
    if (gainNode) gainNode.gain.value = mobileAf / 100;
    sendMobileCfg();
  });

  // Momentary PTT auto-activates mobile microphone!
  const mobPttBtn = document.getElementById("mob-ptt-btn");
  mobPttBtn.addEventListener("contextmenu", (e) => e.preventDefault());
  mobPttBtn.addEventListener("selectstart", (e) => e.preventDefault());
  mobPttBtn.addEventListener("pointerdown", async (e) => {
    e.preventDefault();
    mobPttBtn.setPointerCapture(e.pointerId);
    pttHeld = true;
    sendCmd(activeSlot, "set_ptt", { tx: true, source: "mobile" });
    await ensureMobileMic();
  });
  const mobPttUp = (e) => {
    if (e) e.preventDefault();
    pttHeld = false;
    sendCmd(activeSlot, "set_ptt", { tx: false, source: "mobile" });
  };
  mobPttBtn.addEventListener("pointerup", mobPttUp);
  mobPttBtn.addEventListener("pointercancel", mobPttUp);

  const mob1750Btn = document.getElementById("mob-1750-btn");
  mob1750Btn.style.touchAction = "none";
  mob1750Btn.addEventListener("contextmenu", (e) => e.preventDefault());
  mob1750Btn.addEventListener("selectstart", (e) => e.preventDefault());
  mob1750Btn.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    mob1750Btn.setPointerCapture(e.pointerId);
    sendCmd(activeSlot, "set_1750", { active: true });
  });
  const mob1750Up = (e) => {
    if (e) e.preventDefault();
    sendCmd(activeSlot, "set_1750", { active: false });
  };
  mob1750Btn.addEventListener("pointerup", mob1750Up);
  mob1750Btn.addEventListener("pointercancel", mob1750Up);

  document.getElementById("mob-nb").addEventListener("click", () => sendCmd(activeSlot, "set_dsp", { name: "nb", value: !fullState?.radios?.[String(activeSlot)]?.nb }));
  document.getElementById("mob-nr").addEventListener("click", () => sendCmd(activeSlot, "set_dsp", { name: "nr", value: !fullState?.radios?.[String(activeSlot)]?.nr }));
  document.getElementById("mob-notch").addEventListener("click", () => sendCmd(activeSlot, "set_dsp", { name: "notch", value: !fullState?.radios?.[String(activeSlot)]?.notch }));
  document.getElementById("mob-split").addEventListener("click", () => sendCmd(activeSlot, "set_split", { split: !fullState?.radios?.[String(activeSlot)]?.split }));
  document.getElementById("mob-pre").addEventListener("click", () => sendCmd(activeSlot, "set_dsp", { name: "preamp", value: ((fullState?.radios?.[String(activeSlot)]?.preamp || 0) + 1) % 3 }));
  document.getElementById("mob-pwr").addEventListener("input", (e) => {
    document.getElementById("mob-pwr-val").textContent = `${e.target.value}%`;
    sendCmd(activeSlot, "set_level", { name: "tx_power", value: Number(e.target.value) });
  });

  document.querySelectorAll(".mob-span").forEach((btn) => {
    btn.addEventListener("click", () => {
      sendCmd(activeSlot, "set_scope_span", { span_hz: Number(btn.dataset.span) });
    });
  });

  document.getElementById("mob-scope-canvas").addEventListener("click", (e) => {
    const r = fullState?.radios?.[String(activeSlot)];
    if (!r) return;
    const rect = e.target.getBoundingClientRect();
    const relX = (e.clientX - rect.left) / rect.width;
    const span = r.scope_span_hz || 100000;
    const center = r.scope_center_hz || r.main?.freq_hz || 0;
    if (center > 0) {
      const targetHz = Math.round((center + (relX - 0.5) * span) / stepHz) * stepHz;
      sendCmd(activeSlot, "set_freq", { freq_hz: targetHz, target: "selected" });
    }
  });

  function renderMobRepeaterSuggestion(r, activeVfo, isVhfUhf) {
    const bar = document.getElementById("mob-rep-bar");
    if (!bar) return;
    const curHz = activeVfo?.freq_hz || 0;
    if (!isVhfUhf || !curHz || !repeatersList.length) {
      bar.style.display = "none";
      currentMobRep = null;
      currentMobMatches = [];
      return;
    }
    bar.style.display = "flex";
    const qthLoc = fullState?.qth_locator || "KP20";
    const activeRep = r.active_repeater?.[r.active_band === "SUB" ? "sub" : "main"] || r.active_repeater?.main || null;
    const matches = [];
    for (let i = 0; i < repeatersList.length; i++) {
      const rep = repeatersList[i];
      if (rep.rx_hz > 0 && Math.abs(rep.rx_hz - curHz) <= 5000) {
        const db = calcDistBearing(qthLoc, rep.lat, rep.lon);
        const isAct = Boolean(activeRep && activeRep.callsign === rep.callsign && Math.abs(activeRep.rx_hz - rep.rx_hz) <= 1000);
        matches.push({ ...rep, distKm: db.distKm, bearingDeg: db.bearingDeg, isActivated: isAct });
      }
    }
    matches.sort((a, b) => {
      if (a.isActivated !== b.isActivated) return a.isActivated ? -1 : 1;
      const aQ = a.qrv !== false, bQ = b.qrv !== false;
      if (aQ !== bQ) return aQ ? -1 : 1;
      return (a.distKm ?? 99999) - (b.distKm ?? 99999);
    });
    currentMobMatches = matches;
    const badge = document.getElementById("mob-rep-badge");
    const applyBtn = document.getElementById("mob-rep-apply");
    const sel = document.getElementById("mob-rep-sel");
    if (!matches.length) {
      bar.classList.add("is-idle");
      badge.textContent = "RPT";
      badge.className = "pill-btn";
      applyBtn.textContent = "Standby (±5 kHz)";
      sel.style.display = "none";
      currentMobRep = null;
      return;
    }
    bar.classList.remove("is-idle");
    const top = matches[0];
    currentMobRep = top;
    badge.textContent = top.isActivated ? "ACTIVE" : "AUTO";
    badge.className = top.isActivated ? "pill-btn on" : "pill-btn active";
    const dist = top.distKm != null ? ` · ${top.distKm}km ${String(top.bearingDeg).padStart(3, "0")}°` : "";
    const tone = top.ctcss_hz > 0 ? ` T${top.ctcss_hz}` : (top.tone_1750 ? " 1750" : "");
    applyBtn.textContent = `${top.callsign}${top.qth ? " (" + top.qth + ")" : ""}${tone}${dist}`;
    if (matches.length > 1) {
      sel.style.display = "block";
      sel.innerHTML = matches.map((m, idx) => `<option value="${idx}">${m.callsign}${m.distKm != null ? " " + m.distKm + "km" : ""} (${matches.length})</option>`).join("");
      sel.value = "0";
    } else {
      sel.style.display = "none";
    }
  }

  let mobRepView = "list";
  let mobMap = null;
  let mobMarkerLayer = null;
  let mobCanvasRenderer = null;

  function escHtmlMob(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function renderMobRepeaterMap(filtered, qthLoc) {
    if (typeof window.L === "undefined") return;
    if (!mobMap) {
      const c = maidenheadToLatLon(qthLoc) || { lat: 62.5, lon: 21.0 };
      mobMap = window.L.map("mob-rep-map", { center: [c.lat, c.lon], zoom: 6 });
      window.L.tileLayer("https://mt{s}.google.com/vt/lyrs=m&hl=en&x={x}&y={y}&z={z}", {
        subdomains: ["0", "1", "2", "3"],
        maxZoom: 19,
        attribution: "Map data © Google",
      }).addTo(mobMap);
      mobCanvasRenderer = window.L.canvas({ padding: 0.5 });
      mobMarkerLayer = window.L.layerGroup().addTo(mobMap);
    }
    mobMarkerLayer.clearLayers();
    window._mobMapRepsRef = filtered;

    const qthPos = maidenheadToLatLon(qthLoc);
    if (qthPos) {
      window.L.circleMarker([qthPos.lat, qthPos.lon], {
        renderer: mobCanvasRenderer, radius: 7, color: "#0284c7", weight: 2, fillColor: "#f8fafc", fillOpacity: 1,
      }).bindPopup(`<strong>QTH ${escHtmlMob(qthLoc)}</strong>`).addTo(mobMarkerLayer);
    }

    for (let i = 0; i < filtered.length; i++) {
      const rep = filtered[i];
      if (!rep.lat && !rep.lon) continue;
      const col = rep.qrv === false ? "#ef4444" : (rep.band === "2m" ? "#38bdf8" : (rep.band === "70cm" ? "#f59e0b" : "#a78bfa"));
      const m = window.L.circleMarker([rep.lat, rep.lon], {
        renderer: mobCanvasRenderer, radius: 6, color: "#070b13", weight: 1.2, fillColor: col, fillOpacity: rep.qrv === false ? 0.45 : 0.92,
      });
      const idx = i;
      m.bindPopup(() => {
        const div = document.createElement("div");
        div.style.color = "#0f172a";
        div.style.minWidth = "160px";
        const rxMhz = (rep.rx_hz / 1e6).toFixed(4).replace(/0+$/, "").replace(/\.$/, ".0");
        div.innerHTML = `<strong>${escHtmlMob(rep.callsign)}</strong> (${escHtmlMob(rep.qth || "-")})<br>${rxMhz} MHz · ${escHtmlMob(rep.mode || "FM")}<br><button type="button" class="mob-map-tune" style="margin-top:6px;width:100%;padding:5px;background:#0284c7;color:#fff;border:none;border-radius:4px;font-weight:700;">Tune (MAIN)</button>`;
        div.querySelector(".mob-map-tune").addEventListener("click", () => {
          const targetRep = window._mobMapRepsRef?.[idx];
          if (targetRep) {
            sendCmd(activeSlot, "tune_repeater", { repeater: targetRep, target: "main" });
            document.getElementById("mob-rep-modal").classList.remove("open");
          }
        });
        return div;
      });
      m.addTo(mobMarkerLayer);
    }
    setTimeout(() => mobMap && mobMap.invalidateSize(), 60);
  }

  function renderMobRepeaterList() {
    const listEl = document.getElementById("mob-rep-list");
    if (!listEl) return;
    const q = (document.getElementById("mob-rep-search")?.value || "").trim().toLowerCase();
    const band = document.getElementById("mob-rep-band")?.value || "all";
    const qthLoc = fullState?.qth_locator || "KP20";
    const out = [];
    for (let i = 0; i < repeatersList.length; i++) {
      const rep = repeatersList[i];
      if (band !== "all" && rep.band !== band) continue;
      if (q) {
        const hay = `${rep.callsign} ${rep.qth} ${rep.locator} ${(rep.rx_hz / 1e6).toFixed(4)}`.toLowerCase();
        if (!hay.includes(q)) continue;
      }
      const db = calcDistBearing(qthLoc, rep.lat, rep.lon);
      out.push({ ...rep, distKm: db.distKm, bearingDeg: db.bearingDeg });
    }
    out.sort((a, b) => (a.distKm ?? 99999) - (b.distKm ?? 99999));

    if (mobRepView === "map") {
      renderMobRepeaterMap(out, qthLoc);
      return;
    }

    const shown = out.slice(0, 120);
    window._mobRepsRef = shown;
    listEl.innerHTML = shown.map((rep, idx) => {
      const rxMhz = (rep.rx_hz / 1e6).toFixed(4).replace(/0+$/, "").replace(/\.$/, ".0");
      const sh = rep.shift_hz === 0 ? "0" : `${rep.shift_hz > 0 ? "+" : ""}${(rep.shift_hz / 1e6).toFixed(2)}M`;
      const tone = rep.ctcss_hz > 0 ? `CTCSS ${rep.ctcss_hz}` : (rep.tone_1750 ? "1750Hz" : rep.mode);
      const dist = rep.distKm != null ? `${rep.distKm} km · ${String(rep.bearingDeg).padStart(3, "0")}°` : (rep.locator || "");
      return `<div class="mob-rep-item" data-mob-rep="${idx}">
        <div>
          <strong class="mono">${escHtmlMob(rep.callsign)}</strong> <span style="color:var(--muted); font-size:11px;">${escHtmlMob(rep.qth || "")}</span>
          <div class="mono" style="font-size:11px; color:var(--cyan);">${rxMhz} MHz (${sh}) · ${escHtmlMob(tone)}</div>
        </div>
        <div class="mono" style="text-align:right; font-size:11px; color:${rep.qrv === false ? "#f87171" : "#6ee7b7"};">
          <div>${escHtmlMob(dist)}</div>
          <div>${rep.qrv === false ? "OFF" : "QRV"}</div>
        </div>
      </div>`;
    }).join("");
  }

  document.getElementById("mob-rep-view-btn").addEventListener("click", () => {
    mobRepView = mobRepView === "map" ? "list" : "map";
    document.getElementById("mob-rep-view-btn").textContent = mobRepView === "map" ? "📋 List" : "🗺 Map";
    document.getElementById("mob-rep-list").style.display = mobRepView === "list" ? "flex" : "none";
    document.getElementById("mob-rep-map-wrap").style.display = mobRepView === "map" ? "block" : "none";
    renderMobRepeaterList();
    if (mobRepView === "map") setTimeout(() => mobMap && mobMap.invalidateSize(), 60);
  });

  document.getElementById("mob-rep-apply").addEventListener("click", () => {
    if (currentMobRep) sendCmd(activeSlot, "tune_repeater", { repeater: currentMobRep, target: "main" });
  });
  document.getElementById("mob-rep-sel").addEventListener("change", (e) => {
    const rep = currentMobMatches[Number(e.target.value)];
    if (rep) sendCmd(activeSlot, "tune_repeater", { repeater: rep, target: "main" });
  });
  document.getElementById("mob-rep-open").addEventListener("click", () => {
    loadMobileRepeatersOnce(false);
    renderMobRepeaterList();
    document.getElementById("mob-rep-modal").classList.add("open");
  });
  document.getElementById("mob-rep-close").addEventListener("click", () => {
    document.getElementById("mob-rep-modal").classList.remove("open");
  });
  document.getElementById("mob-rep-refresh").addEventListener("click", () => loadMobileRepeatersOnce(true));
  document.getElementById("mob-rep-search").addEventListener("input", renderMobRepeaterList);
  document.getElementById("mob-rep-band").addEventListener("change", renderMobRepeaterList);
  document.getElementById("mob-rep-list").addEventListener("click", (e) => {
    const item = e.target.closest("[data-mob-rep]");
    if (!item) return;
    const rep = window._mobRepsRef?.[Number(item.dataset.mobRep)];
    if (rep) {
      sendCmd(activeSlot, "tune_repeater", { repeater: rep, target: "main" });
      document.getElementById("mob-rep-modal").classList.remove("open");
    }
  });
  document.getElementById("mob-qth-save").addEventListener("click", async () => {
    const loc = (document.getElementById("mob-qth-in").value || "KP20").trim().toUpperCase();
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ qth_locator: loc }),
    });
    showTab("r" + activeSlot);
  });

  updateAudioButtons();
  updateMobSqlMark();
  connectWs();
  loadMobileRepeatersOnce(false);
  requestAnimationFrame(animLoop);
})();
