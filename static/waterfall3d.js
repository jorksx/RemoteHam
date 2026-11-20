/**
 * Crisp 60fps Dual-Mode Scope Renderer:
 *   Top half    = Yaesu-style 3DSS 3D Waterfall (solid elevation-colored terrain ridges, 1:1 X-aligned front edge)
 *   Bottom half = Icom-style 2D Waterfall (exact 1:1 un-smeared CI-V pixel rows, sub-pixel 60fps glide without stretching)
 */
window.ScopeRenderer = (function () {
  const ROW_PX = 2; // 2 crisp pixels per real CI-V sweep (~22 px/s at 11 sweeps/s, zero vertical mush)

  function colorForLevel(t, palette) {
    const v = Math.max(0, Math.min(1, t));
    if (palette === "thermal") {
      return [
        Math.round(255 * Math.min(1, v * 1.9)),
        Math.round(255 * Math.max(0, Math.min(1, (v - 0.35) * 1.6))),
        Math.round(255 * (v < 0.3 ? v * 1.2 : Math.max(0, (v - 0.78) * 4.5))),
      ];
    }
    if (palette === "viridis") {
      if (v < 0.3) {
        const k = v / 0.3;
        return [Math.round(10 + k * 14), Math.round(14 + k * 46), Math.round(36 + k * 78)];
      }
      if (v < 0.6) {
        const k = (v - 0.3) / 0.3;
        return [Math.round(24 + k * 18), Math.round(60 + k * 120), Math.round(114 + k * 36)];
      }
      if (v < 0.85) {
        const k = (v - 0.6) / 0.25;
        return [Math.round(42 + k * 188), Math.round(180 + k * 42), Math.round(150 - k * 105)];
      }
      const k = (v - 0.85) / 0.15;
      return [Math.round(230 + k * 25), Math.round(222 + k * 25), Math.round(45 + k * 120)];
    }
    // Classic Icom SDR waterfall
    if (v < 0.22) {
      const k = v / 0.22;
      return [6, Math.round(14 + k * 34), Math.round(40 + k * 110)];
    }
    if (v < 0.5) {
      const k = (v - 0.22) / 0.28;
      return [Math.round(6 + k * 24), Math.round(48 + k * 155), Math.round(150 + k * 70)];
    }
    if (v < 0.78) {
      const k = (v - 0.5) / 0.28;
      return [Math.round(30 + k * 218), Math.round(203 + k * 36), Math.round(220 - k * 185)];
    }
    const k = (v - 0.78) / 0.22;
    return [255, Math.round(239 - k * 195), Math.round(35 - k * 25)];
  }

  function packRgbaU32(r, g, b, a = 255) {
    const ab = new ArrayBuffer(4);
    const u8 = new Uint8Array(ab);
    u8[0] = r; u8[1] = g; u8[2] = b; u8[3] = a;
    return new Uint32Array(ab)[0];
  }
  const _isLE = packRgbaU32(1, 2, 3, 4) === 0x04030201;
  const _pack = _isLE
    ? (r, g, b) => (0xFF000000 | (b << 16) | (g << 8) | r) >>> 0
    : (r, g, b) => ((r << 24) | (g << 16) | (b << 8) | 0xFF) >>> 0;
  const BG_U32 = _pack(5, 8, 17); // #050811

  const _paletteLutCache = {};
  function getPaletteLut(palette) {
    const key = palette || "classic";
    if (_paletteLutCache[key]) return _paletteLutCache[key];
    const lut = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      const [r, g, b] = colorForLevel(i / 255, key);
      lut[i] = _pack(r, g, b);
    }
    _paletteLutCache[key] = lut;
    return lut;
  }

  function createScopeState() {
    const wfCanvas = document.createElement("canvas");
    wfCanvas.width = 640;
    wfCanvas.height = 300;
    const wfCtx = wfCanvas.getContext("2d");
    const wfImg = wfCtx.createImageData(wfCanvas.width, wfCanvas.height);
    const wfU32 = new Uint32Array(wfImg.data.buffer);
    wfU32.fill(BG_U32);
    wfCtx.putImageData(wfImg, 0, 0);

    return {
      wfCanvas,
      wfCtx,
      wfImg,
      wfU32,
      wfDirty: false,
      history3d: [],
      max3dRows: 36,
      prevFrontRow: null,
      currentFrontRow: null,
      liveBins: null,
      subScrollPx: 0,      // glides from -ROW_PX -> 0 between real sweeps (no pixel stretching)
      subOffset3d: 0,      // glides from 0 -> 1 between real sweeps
      avgSweepMs: 90,
      lastSweepTs: 0,
      lastFrameTs: performance.now(),
      centerHz: 0,
      spanHz: 100000,
      histCenterHz: 0,
      histSpanHz: 100000,
      shiftAccumPx: 0,
      _tmpRowU32: new Uint32Array(640),
      _tmpRow3d: new Float32Array(160),
      palette: "classic",
      hoverRelX: null,
    };
  }

  function shiftRow3dInPlace(row, oldCenter, oldSpan, newCenter, newSpan, tmp) {
    if (!row || row.length < 2 || oldSpan <= 0 || newSpan <= 0) return;
    const n = row.length;
    tmp.set(row);
    if (oldSpan === newSpan) {
      const shiftBins = -((newCenter - oldCenter) / newSpan) * (n - 1);
      if (Math.abs(shiftBins) >= n) {
        row.fill(0);
        return;
      }
      for (let i = 0; i < n; i++) {
        const src = i - shiftBins;
        if (src >= 0 && src <= n - 1) {
          const lo = Math.floor(src);
          const hi = Math.min(n - 1, lo + 1);
          const f = src - lo;
          row[i] = tmp[lo] * (1 - f) + tmp[hi] * f;
        } else {
          row[i] = 0;
        }
      }
      return;
    }
    for (let i = 0; i < n; i++) {
      const uNew = i / (n - 1);
      const uOld = 0.5 + ((uNew - 0.5) * newSpan + (newCenter - oldCenter)) / oldSpan;
      if (uOld >= 0 && uOld <= 1) {
        const src = uOld * (n - 1);
        const lo = Math.floor(src);
        const hi = Math.min(n - 1, lo + 1);
        const f = src - lo;
        row[i] = tmp[lo] * (1 - f) + tmp[hi] * f;
      } else {
        row[i] = 0;
      }
    }
  }

  function shiftHistory(st, newCenterHz, newSpanHz) {
    const oldCenter = st.histCenterHz;
    const oldSpan = st.histSpanHz || 100000;
    if (oldCenter <= 0 || newCenterHz <= 0 || oldSpan <= 0 || newSpanHz <= 0) return;

    const cw = st.wfCanvas.width;
    const ch = st.wfCanvas.height;
    const u32 = st.wfU32;

    if (oldSpan === newSpanHz) {
      const exactShiftPx = st.shiftAccumPx - ((newCenterHz - oldCenter) / newSpanHz) * cw;
      const dx = Math.trunc(exactShiftPx);
      st.shiftAccumPx = exactShiftPx - dx;
      if (Math.abs(dx) >= cw) {
        st.shiftAccumPx = 0;
        u32.fill(BG_U32);
      } else if (dx > 0) {
        for (let y = 0; y < ch; y++) {
          const off = y * cw;
          u32.copyWithin(off + dx, off, off + cw - dx);
          u32.fill(BG_U32, off, off + dx);
        }
      } else if (dx < 0) {
        const k = -dx;
        for (let y = 0; y < ch; y++) {
          const off = y * cw;
          u32.copyWithin(off, off + k, off + cw);
          u32.fill(BG_U32, off + cw - k, off + cw);
        }
      }
    } else {
      st.shiftAccumPx = 0;
      if (st._tmpRowU32.length < cw) st._tmpRowU32 = new Uint32Array(cw);
      const tmp = st._tmpRowU32;
      const invScale = newSpanHz / oldSpan;
      const centerShiftU = (newCenterHz - oldCenter) / oldSpan;
      for (let y = 0; y < ch; y++) {
        const off = y * cw;
        tmp.set(u32.subarray(off, off + cw));
        for (let x = 0; x < cw; x++) {
          const uNew = x / (cw - 1);
          const uOld = 0.5 + (uNew - 0.5) * invScale + centerShiftU;
          if (uOld >= 0 && uOld <= 1) {
            u32[off + x] = tmp[Math.round(uOld * (cw - 1))];
          } else {
            u32[off + x] = BG_U32;
          }
        }
      }
    }
    st.wfDirty = true;

    const tmp3d = st._tmpRow3d;
    for (let k = 0; k < st.history3d.length; k++) {
      shiftRow3dInPlace(st.history3d[k], oldCenter, oldSpan, newCenterHz, newSpanHz, tmp3d);
    }
    if (st.prevFrontRow) {
      shiftRow3dInPlace(st.prevFrontRow, oldCenter, oldSpan, newCenterHz, newSpanHz, tmp3d);
    }
    if (st.currentFrontRow) {
      shiftRow3dInPlace(st.currentFrontRow, oldCenter, oldSpan, newCenterHz, newSpanHz, tmp3d);
    }
  }

  function sampleBins(src, targetLen) {
    const out = new Float32Array(targetLen);
    const n = src.length;
    if (n === 0) return out;
    if (n === 1) {
      out.fill(src[0]);
      return out;
    }
    for (let i = 0; i < targetLen; i++) {
      const pos = (i / (targetLen - 1)) * (n - 1);
      const lo = Math.floor(pos);
      const hi = Math.min(n - 1, lo + 1);
      const frac = pos - lo;
      out[i] = src[lo] * (1 - frac) + src[hi] * frac;
    }
    return out;
  }

  function ensureWfSize(st, targetW, targetH) {
    const w = Math.max(64, Math.round(targetW));
    const h = Math.max(64, Math.round(targetH + ROW_PX + 4));
    if (st.wfCanvas.width !== w || st.wfCanvas.height !== h) {
      const oldW = st.wfCanvas.width;
      const oldH = st.wfCanvas.height;
      const oldU32 = st.wfU32;

      st.wfCanvas.width = w;
      st.wfCanvas.height = h;
      const newImg = st.wfCtx.createImageData(w, h);
      const newU32 = new Uint32Array(newImg.data.buffer);
      newU32.fill(BG_U32);
      const copyH = Math.min(oldH, h);
      if (oldW === w) {
        newU32.set(oldU32.subarray(0, copyH * w));
      } else {
        for (let y = 0; y < copyH; y++) {
          const oldOff = y * oldW;
          const newOff = y * w;
          for (let x = 0; x < w; x++) {
            const sx = Math.round((x / (w - 1)) * (oldW - 1));
            newU32[newOff + x] = oldU32[oldOff + sx];
          }
        }
      }
      st.wfImg = newImg;
      st.wfU32 = newU32;
      if (st._tmpRowU32.length < w) st._tmpRowU32 = new Uint32Array(w);
      st.wfDirty = true;
    }
  }

  function pushSpectrumRow(st, bins, centerHz, spanHz, palette, trackFreq = false) {
    if (!bins || !bins.length) return;
    const now = performance.now();
    if (st.lastSweepTs > 0) {
      const measured = Math.max(40, Math.min(220, now - st.lastSweepTs));
      st.avgSweepMs = st.avgSweepMs * 0.7 + measured * 0.3;
    }
    st.lastSweepTs = now;
    const nextCenter = centerHz || st.centerHz;
    const nextSpan = spanHz || st.spanHz;
    if (trackFreq && st.histCenterHz > 0 && nextCenter > 0 && (nextCenter !== st.histCenterHz || nextSpan !== st.histSpanHz)) {
      shiftHistory(st, nextCenter, nextSpan);
    } else if (!trackFreq) {
      st.shiftAccumPx = 0;
    }
    if (nextCenter > 0) st.histCenterHz = nextCenter;
    if (nextSpan > 0) st.histSpanHz = nextSpan;
    st.centerHz = nextCenter;
    st.spanHz = nextSpan;
    if (palette) st.palette = palette;

    st.liveBins = bins;

    // 1. Commit ONE exact, un-smeared CI-V row directly in CPU RAM (zero GPU readbacks/stalls)
    const cw = st.wfCanvas.width;
    const ch = st.wfCanvas.height;
    const u32 = st.wfU32;
    u32.copyWithin(ROW_PX * cw, 0, (ch - ROW_PX) * cw);

    const lut = getPaletteLut(st.palette);
    const n = bins.length;
    for (let x = 0; x < cw; x++) {
      const pos = (x / (cw - 1)) * (n - 1);
      const lo = Math.floor(pos);
      const hi = Math.min(n - 1, lo + 1);
      const frac = pos - lo;
      const val = bins[lo] * (1 - frac) + bins[hi] * frac;
      const idx = Math.max(0, Math.min(255, (val * 255) | 0));
      u32[x] = lut[idx];
    }
    for (let ry = 1; ry < ROW_PX; ry++) {
      u32.copyWithin(ry * cw, 0, cw);
    }
    st.wfDirty = true;

    // Reset sub-pixel slide so the newly committed row glides smoothly into view over avgSweepMs
    st.subScrollPx = -ROW_PX;

    // 2. Commit ONE exact ridge into Yaesu 3DSS history & preserve continuous front morph
    const nextRow = sampleBins(bins, 160);
    st.prevFrontRow = st.currentFrontRow ? new Float32Array(st.currentFrontRow) : new Float32Array(nextRow);
    st.history3d.unshift(nextRow);
    if (st.history3d.length > st.max3dRows) {
      st.history3d.pop();
    }
    st.subOffset3d = 0;
  }

  function advanceSlide(st, now) {
    const dt = Math.max(0, Math.min(40, now - st.lastFrameTs));
    st.lastFrameTs = now;
    if (st.lastSweepTs === 0 || now - st.lastSweepTs > 1500) return;

    const rate = dt / Math.max(45, st.avgSweepMs);
    if (st.subScrollPx < 0) {
      st.subScrollPx = Math.min(0, st.subScrollPx + ROW_PX * rate);
    }
    if (st.subOffset3d < 1.0) {
      st.subOffset3d = Math.min(1.0, st.subOffset3d + rate);
    }
  }

  function renderScope(canvas, st, opts) {
    if (!canvas || !st) return;
    if (opts.palette) st.palette = opts.palette;

    const rect = canvas.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;

    const w = rect.width;
    const h = rect.height;
    const hasRepStrip = Boolean(Array.isArray(opts.repeaters) && opts.repeaters.length > 0);
    const yaesuH = Math.max(58, Math.round(h * 0.24));
    const freqRulerH = 17;
    const repStripH = hasRepStrip ? 17 : 0;
    const rulerH = freqRulerH + repStripH;
    const icomY = yaesuH + rulerH;
    const icomH = Math.max(20, h - icomY);

    ensureWfSize(st, w, icomH);
    if (st.wfDirty) {
      st.wfCtx.putImageData(st.wfImg, 0, 0);
      st.wfDirty = false;
    }
    advanceSlide(st, performance.now());

    const dpr = window.devicePixelRatio || 1;
    const pw = Math.round(w * dpr);
    const ph = Math.round(h * dpr);
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw;
      canvas.height = ph;
    }

    const ctx = canvas.getContext("2d");
    ctx.save();
    ctx.scale(dpr, dpr);

    // --- TOP: COMPACT YAESU 3DSS-STYLE 3D SPECTRUM STREAM ---
    ctx.fillStyle = "#08090b";
    ctx.fillRect(0, 0, w, yaesuH);

    const topY = Math.min(30, Math.round(yaesuH * 0.34));
    const baseY = yaesuH - 4;
    const peakMaxH = Math.max(16, (baseY - topY) * 0.72);
    const cx = w * 0.5;
    // Front edge spans full width (1:1 aligned with 2D waterfall below), receding smoothly toward top
    const frontHalfW = w * 0.5;
    const backHalfW = w * 0.36;

    // Perspective grid lines
    ctx.strokeStyle = "rgba(255, 255, 255, 0.09)";
    ctx.lineWidth = 1;
    for (let g = -4; g <= 4; g++) {
      const t = g / 4;
      ctx.beginPath();
      ctx.moveTo(cx + t * backHalfW, topY);
      ctx.lineTo(cx + t * frontHalfW, baseY);
      ctx.stroke();
    }
    for (let hTick = 0; hTick <= 3; hTick++) {
      const d = hTick / 3;
      const gy = baseY - d * (baseY - topY);
      const hw = frontHalfW - d * (frontHalfW - backHalfW);
      ctx.beginPath();
      ctx.moveTo(cx - hw, gy);
      ctx.lineTo(cx + hw, gy);
      ctx.stroke();
    }

    const rows = st.history3d;
    if (rows.length > 0) {
      const maxRows = st.max3dRows;
      const tMorph = st.subOffset3d;

      const draw3dRidge = (row, depth, isFront) => {
        const yBase = baseY - depth * (baseY - topY);
        const halfW = frontHalfW - depth * (frontHalfW - backHalfW);
        const ampScale = peakMaxH * (1 - depth * 0.42);

        ctx.beginPath();
        ctx.moveTo(cx - halfW, yBase);
        for (let i = 0; i < row.length; i++) {
          const x = cx - halfW + (i / (row.length - 1)) * (halfW * 2);
          const y = yBase - row[i] * ampScale;
          ctx.lineTo(x, y);
        }
        ctx.lineTo(cx + halfW, yBase);
        ctx.closePath();

        // Yaesu 3DSS vertical elevation color gradient: deep blue floor -> cyan -> green -> yellow -> red peaks
        const fade = Math.max(0.28, 1 - depth * 0.68);
        const elevGrad = ctx.createLinearGradient(0, yBase - ampScale, 0, yBase);
        elevGrad.addColorStop(0.0, `rgba(239, 68, 68, ${fade})`);
        elevGrad.addColorStop(0.24, `rgba(250, 204, 21, ${fade})`);
        elevGrad.addColorStop(0.50, `rgba(34, 197, 94, ${fade})`);
        elevGrad.addColorStop(0.76, `rgba(14, 165, 233, ${fade})`);
        elevGrad.addColorStop(1.0, `rgba(8, 22, 58, ${Math.min(0.96, fade + 0.2)})`);

        ctx.fillStyle = elevGrad;
        ctx.fill();

        if (!isFront) {
          ctx.strokeStyle = `rgba(125, 211, 252, ${0.08 + (1 - depth) * 0.25})`;
          ctx.lineWidth = 0.7;
          ctx.stroke();
        }
      };

      // Historical ridges march continuously from depth 0 -> back
      for (let r = rows.length - 1; r >= 1; r--) {
        const depth = Math.min(1, (r - 1 + tMorph) / (maxRows - 1));
        draw3dRidge(rows[r], depth, false);
      }

      // Live front ridge smoothly morphs from prevFrontRow -> rows[0] at depth = 0 (zero popping)
      const targetRow = rows[0];
      const prevRow = st.prevFrontRow || targetRow;
      if (!st.currentFrontRow || st.currentFrontRow.length !== targetRow.length) {
        st.currentFrontRow = new Float32Array(targetRow.length);
      }
      for (let i = 0; i < targetRow.length; i++) {
        st.currentFrontRow[i] = prevRow[i] + (targetRow[i] - prevRow[i]) * tMorph;
      }
      draw3dRidge(st.currentFrontRow, 0, true);
    } else {
      ctx.fillStyle = "rgba(148, 163, 184, 0.5)";
      ctx.font = "600 12px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(opts.connected ? "Waiting for spectrum data (CI-V 0x27)..." : "Radio disconnected", cx, yaesuH * 0.56);
    }

    const spanHz = opts.spanHz || st.spanHz || 100000;

    // --- BOTTOM HALF: ICOM 2D WATERFALL (1:1 SHARP PIXELS + SUB-PIXEL 60FPS GLIDE) ---
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, icomY, w, icomH);
    ctx.clip();
    ctx.fillStyle = "#050811";
    ctx.fillRect(0, icomY, w, icomH);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(st.wfCanvas, 0, Math.round((icomY + st.subScrollPx) * 10) / 10);
    ctx.restore();

    // Center VFO marker on 2D waterfall only (50% opacity, leaving 3D spectrum unobstructed)
    ctx.strokeStyle = opts.tx ? "rgba(239, 68, 68, 0.5)" : "rgba(250, 204, 21, 0.5)";
    ctx.lineWidth = 1.3;
    ctx.beginPath();
    ctx.moveTo(cx, icomY);
    ctx.lineTo(cx, h);
    ctx.stroke();

    // Frequency scale ruler (+ middle repeater label strip below 3D spectrum, above 2D waterfall)
    ctx.fillStyle = "#121318";
    ctx.fillRect(0, yaesuH, w, rulerH);
    if (hasRepStrip) {
      ctx.fillStyle = "#0b111d";
      ctx.fillRect(0, yaesuH + freqRulerH, w, repStripH);
    }
    ctx.strokeStyle = "rgba(148, 163, 184, 0.25)";
    ctx.beginPath();
    ctx.moveTo(0, yaesuH); ctx.lineTo(w, yaesuH);
    if (hasRepStrip) {
      ctx.moveTo(0, yaesuH + freqRulerH); ctx.lineTo(w, yaesuH + freqRulerH);
    }
    ctx.moveTo(0, icomY); ctx.lineTo(w, icomY);
    ctx.stroke();

    const centerHz = opts.centerHz || st.centerHz || opts.freqHz || 0;
    ctx.font = "600 10px ui-monospace, Menlo, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const rulerMidY = yaesuH + freqRulerH * 0.5 + 0.5;
    if (centerHz > 0) {
      [-0.4, -0.2, 0, 0.2, 0.4].forEach((s) => {
        const x = (0.5 + s) * w;
        const mhz = ((centerHz + s * spanHz) / 1e6).toFixed(3);
        ctx.fillStyle = s === 0 ? "#facc15" : "#94a3b8";
        ctx.fillText(mhz, x, rulerMidY);
      });
    }

    // Closest QRV repeater markers directly between 3D spectrum (above) and 2D waterfall (below)
    const repeaters = Array.isArray(opts.repeaters) ? opts.repeaters : null;
    if (repeaters && repeaters.length > 0 && centerHz > 0 && spanHz > 0) {
      const fMin = centerHz - spanHz * 0.5;
      const fMax = centerHz + spanHz * 0.5;
      const vis = [];
      for (let i = 0; i < repeaters.length; i++) {
        const rep = repeaters[i];
        if (rep.rx_hz >= fMin && rep.rx_hz <= fMax) {
          vis.push(rep);
        }
      }
      if (vis.length > 0) {
        const actRep = opts.activeRepeater || null;
        ctx.save();
        ctx.font = "700 9.5px ui-monospace, Menlo, monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const stripTop = yaesuH + freqRulerH;
        const pillY = stripTop + 1;
        const pillH = 12;
        let lastRightX = -999;
        for (let i = 0; i < vis.length; i++) {
          const rep = vis[i];
          const rx = ((rep.rx_hz - fMin) / spanHz) * w;
          const isAct = Boolean(actRep && actRep.callsign === rep.callsign && Math.abs(actRep.rx_hz - rep.rx_hz) <= 1000);
          const strokeCol = isAct ? "rgba(16, 185, 129, 0.85)" : "rgba(56, 189, 248, 0.42)";

          ctx.save();
          ctx.strokeStyle = strokeCol;
          ctx.lineWidth = isAct ? 1.4 : 1;
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(rx, icomY);
          ctx.lineTo(rx, h);
          ctx.stroke();
          ctx.restore();

          ctx.fillStyle = isAct ? "#10b981" : "#38bdf8";
          ctx.beginPath();
          ctx.moveTo(rx, icomY);
          ctx.lineTo(rx - 3, icomY - 3.5);
          ctx.lineTo(rx + 3, icomY - 3.5);
          ctx.closePath();
          ctx.fill();

          const label = rep.callsign || "";
          const pillW = Math.max(38, ctx.measureText(label).width + 8);
          let px = Math.max(2, Math.min(w - pillW - 2, rx - pillW * 0.5));
          if (px < lastRightX + 3) {
            px = lastRightX + 3;
          }
          if (px + pillW <= w - 2) {
            lastRightX = px + pillW;
            ctx.fillStyle = isAct ? "rgba(6, 78, 59, 0.94)" : "rgba(8, 22, 46, 0.92)";
            ctx.fillRect(px, pillY, pillW, pillH);
            ctx.strokeStyle = isAct ? "#10b981" : "rgba(56, 189, 248, 0.72)";
            ctx.lineWidth = 1;
            ctx.strokeRect(px, pillY, pillW, pillH);
            ctx.fillStyle = isAct ? "#6ee7b7" : "#e0f2fe";
            ctx.fillText(label, px + pillW * 0.5, pillY + pillH * 0.5 + 0.5);
          }
        }
        ctx.restore();
      }
    }

    // Subtle vertical gray hover crosshair + hovered frequency readout
    if (st.hoverRelX !== null && st.hoverRelX >= 0 && st.hoverRelX <= 1) {
      const hx = Math.round(st.hoverRelX * w) + 0.5;
      ctx.strokeStyle = "rgba(180, 192, 210, 0.42)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(hx, topY);
      ctx.lineTo(hx, h);
      ctx.stroke();

      if (centerHz > 0) {
        const hoverHz = centerHz + (st.hoverRelX - 0.5) * spanHz;
        const label = (hoverHz / 1e6).toFixed(4);
        const boxW = 58;
        const bx = Math.max(2, Math.min(w - boxW - 2, hx - boxW / 2));
        ctx.fillStyle = "rgba(30, 41, 59, 0.92)";
        ctx.fillRect(bx, yaesuH + 1, boxW, freqRulerH - 2);
        ctx.strokeStyle = "rgba(148, 163, 184, 0.5)";
        ctx.strokeRect(bx, yaesuH + 1, boxW, freqRulerH - 2);
        ctx.fillStyle = "#e2e8f0";
        ctx.fillText(label, bx + boxW / 2, rulerMidY);
      }
    }

    // SWR Sweep Overlay Curve directly on top of the 2D Waterfall
    if (opts.swrScan && Array.isArray(opts.swrScan.points) && opts.swrScan.points.length > 0 && centerHz > 0 && spanHz > 0) {
      drawSwrOverlay(ctx, w, icomY, icomH, centerHz, spanHz, opts.swrScan, st.hoverRelX);
    }

    // Centered blinking High-SWR (>2.5 or peak >2.5) warning overlay with 5s fade-out after PTT
    if (opts.swrWarn && opts.swrWarn.active && opts.swrWarn.alpha > 0.01) {
      drawSwrWarningOverlay(ctx, w, icomY, icomH, opts.swrWarn);
    }

    ctx.restore();
  }

  function swrToY(swr, yTop, yBottom) {
    const clamped = Math.max(1.0, Math.min(3.5, swr));
    const norm = (clamped - 1.0) / 2.5;
    return yBottom - norm * (yBottom - yTop);
  }

  function swrColor(swr) {
    if (swr <= 1.5) return "#22c55e";
    if (swr <= 2.2) return "#facc15";
    return "#ef4444";
  }

  function drawSwrOverlay(ctx, w, icomY, icomH, centerHz, spanHz, swrScan, hoverRelX) {
    const h = icomY + icomH;
    const yTop = icomY + Math.min(18, Math.max(10, icomH * 0.1));
    const yBottom = h - Math.min(14, Math.max(8, icomH * 0.08));
    if (yBottom - yTop < 24) return;

    const fMin = centerHz - spanHz * 0.5;
    const pts = swrScan.points;

    ctx.save();
    ctx.beginPath();
    ctx.rect(0, icomY, w, icomH);
    ctx.clip();

    // Subtle horizontal SWR reference grid (1.0, 1.5, 2.0, 3.0)
    ctx.font = "700 9px ui-monospace, Menlo, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    [1.0, 1.5, 2.0, 3.0].forEach((ref) => {
      const ry = Math.round(swrToY(ref, yTop, yBottom)) + 0.5;
      ctx.save();
      ctx.strokeStyle = ref === 2.0 ? "rgba(250, 204, 21, 0.35)" : "rgba(148, 163, 184, 0.25)";
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(32, ry);
      ctx.lineTo(w - 4, ry);
      ctx.stroke();
      ctx.restore();

      ctx.fillStyle = "rgba(5, 8, 17, 0.78)";
      ctx.fillRect(3, ry - 6, 26, 12);
      ctx.fillStyle = ref <= 1.5 ? "#86efac" : (ref <= 2.0 ? "#fde047" : "#fca5a5");
      ctx.fillText(ref.toFixed(1), 6, ry);
    });

    // Map frequency points into canvas coordinates (preserving nulls as curve breaks)
    const mapped = pts.map((p) => ({
      x: ((p.freq_hz - fMin) / spanHz) * w,
      y: p.swr !== null && p.swr !== undefined ? swrToY(Number(p.swr), yTop, yBottom) : null,
      swr: p.swr !== null && p.swr !== undefined ? Number(p.swr) : null,
      freq_hz: Number(p.freq_hz || 0),
    }));

    // Soft translucent fill below valid segments
    let segStart = null;
    for (let i = 0; i <= mapped.length; i++) {
      const m = i < mapped.length ? mapped[i] : null;
      if (m && m.y !== null) {
        if (segStart === null) {
          segStart = m;
          ctx.beginPath();
          ctx.moveTo(m.x, yBottom);
          ctx.lineTo(m.x, m.y);
        } else {
          ctx.lineTo(m.x, m.y);
        }
      } else if (segStart !== null) {
        const last = mapped[i - 1];
        ctx.lineTo(last.x, yBottom);
        ctx.closePath();
        ctx.fillStyle = "rgba(56, 189, 248, 0.12)";
        ctx.fill();
        segStart = null;
      }
    }

    // Dark contrast halo + colored line segments
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    for (let pass = 0; pass < 2; pass++) {
      for (let i = 1; i < mapped.length; i++) {
        const a = mapped[i - 1];
        const b = mapped[i];
        if (a.y === null || b.y === null) continue;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        if (pass === 0) {
          ctx.strokeStyle = "rgba(0, 0, 0, 0.78)";
          ctx.lineWidth = 4.6;
        } else {
          ctx.strokeStyle = swrColor(Math.max(a.swr, b.swr));
          ctx.lineWidth = 2.4;
        }
        ctx.stroke();
      }
    }

    // Individual sample dots + track minimum SWR
    let minPt = null;
    for (let i = 0; i < mapped.length; i++) {
      const m = mapped[i];
      if (m.y === null) continue;
      if (!minPt || m.swr < minPt.swr) minPt = m;
      ctx.fillStyle = "#050811";
      ctx.beginPath();
      ctx.arc(m.x, m.y, 3.6, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = swrColor(m.swr);
      ctx.beginPath();
      ctx.arc(m.x, m.y, 2.3, 0, Math.PI * 2);
      ctx.fill();
    }

    // Minimum SWR highlight badge
    if (minPt) {
      ctx.strokeStyle = "#38bdf8";
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.arc(minPt.x, minPt.y, 5.5, 0, Math.PI * 2);
      ctx.stroke();

      const badgeTxt = `MIN ${minPt.swr.toFixed(2)} @ ${(minPt.freq_hz / 1e6).toFixed(3)}`;
      ctx.font = "800 9.5px ui-monospace, Menlo, monospace";
      const tw = ctx.measureText(badgeTxt).width + 10;
      const bx = Math.max(34, Math.min(w - tw - 4, minPt.x - tw * 0.5));
      const by = Math.max(icomY + 3, minPt.y - 20);
      ctx.fillStyle = "rgba(5, 8, 17, 0.9)";
      ctx.fillRect(bx, by, tw, 14);
      ctx.strokeStyle = "#38bdf8";
      ctx.lineWidth = 1;
      ctx.strokeRect(bx, by, tw, 14);
      ctx.fillStyle = "#e0f2fe";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(badgeTxt, bx + tw * 0.5, by + 7.5);
    }

    // Interpolated SWR hover pill at cursor X
    if (hoverRelX !== null && hoverRelX >= 0 && hoverRelX <= 1) {
      const hoverHz = centerHz + (hoverRelX - 0.5) * spanHz;
      for (let i = 1; i < mapped.length; i++) {
        const a = mapped[i - 1];
        const b = mapped[i];
        if (a.swr !== null && b.swr !== null && hoverHz >= a.freq_hz && hoverHz <= b.freq_hz) {
          const t = b.freq_hz > a.freq_hz ? (hoverHz - a.freq_hz) / (b.freq_hz - a.freq_hz) : 0;
          const interpSwr = a.swr + (b.swr - a.swr) * t;
          const hx = hoverRelX * w;
          const hy = swrToY(interpSwr, yTop, yBottom);
          ctx.fillStyle = "#ffffff";
          ctx.beginPath();
          ctx.arc(hx, hy, 3.2, 0, Math.PI * 2);
          ctx.fill();

          const hTxt = `SWR ${interpSwr.toFixed(2)}`;
          ctx.font = "800 9.5px ui-monospace, Menlo, monospace";
          const hw = ctx.measureText(hTxt).width + 8;
          const hbx = Math.min(w - hw - 4, hx + 6);
          const hby = Math.max(icomY + 3, hy - 14);
          ctx.fillStyle = "rgba(15, 23, 42, 0.92)";
          ctx.fillRect(hbx, hby, hw, 14);
          ctx.strokeStyle = swrColor(interpSwr);
          ctx.lineWidth = 1;
          ctx.strokeRect(hbx, hby, hw, 14);
          ctx.fillStyle = swrColor(interpSwr);
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(hTxt, hbx + hw * 0.5, hby + 7.5);
          break;
        }
      }
    }

    ctx.restore();
  }

  function drawSwrWarningOverlay(ctx, w, icomY, icomH, swrWarn) {
    const now = performance.now();
    const blinkPhase = 0.5 + 0.5 * Math.sin((now / 155) * Math.PI);
    const fadeAlpha = Math.max(0, Math.min(1, swrWarn.alpha));

    const boxW = Math.min(250, Math.max(175, w * 0.34));
    const boxH = Math.min(68, Math.max(48, icomH * 0.42));
    const bx = w * 0.5 - boxW * 0.5;
    const by = icomY + icomH * 0.5 - boxH * 0.5;

    ctx.save();
    ctx.beginPath();
    ctx.rect(0, icomY, w, icomH);
    ctx.clip();

    ctx.globalAlpha = fadeAlpha;

    // Dark red high-contrast backing card
    ctx.fillStyle = `rgba(38, 7, 11, ${(0.82 + blinkPhase * 0.14).toFixed(3)})`;
    ctx.beginPath();
    if (ctx.roundRect) {
      ctx.roundRect(bx, by, boxW, boxH, 9);
    } else {
      ctx.rect(bx, by, boxW, boxH);
    }
    ctx.fill();

    // Pulsing outer warning border + glow
    ctx.shadowColor = "rgba(239, 68, 68, 0.85)";
    ctx.shadowBlur = 8 + blinkPhase * 12;
    ctx.strokeStyle = blinkPhase > 0.45 ? "#ef4444" : "#fca5a5";
    ctx.lineWidth = 2.2;
    ctx.stroke();
    ctx.shadowBlur = 0;

    // Title row (blinks gently between bright red-orange and bright white-amber)
    const textBright = 0.55 + 0.45 * blinkPhase;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = "900 12px system-ui, -apple-system, sans-serif";
    ctx.fillStyle = `rgba(252, 165, 165, ${textBright.toFixed(2)})`;
    ctx.fillText("⚠ HIGH SWR WARNING", w * 0.5, by + boxH * 0.28);

    // Big blinking SWR readout (tracks highest value > 2.5 while PTT is held)
    const swrTxt = Number(swrWarn.peak || swrWarn.cur || 0).toFixed(2);
    ctx.font = "900 20px ui-monospace, Menlo, monospace";
    ctx.fillStyle = blinkPhase > 0.35 ? "#ffffff" : "#fde047";
    ctx.fillText(`SWR ${swrTxt}`, w * 0.5, by + boxH * 0.65);

    // 5s fade progress strip at bottom once PTT releases
    if (!swrWarn.tx && swrWarn.remSec > 0) {
      const frac = Math.max(0, Math.min(1, swrWarn.remSec / 5.0));
      const barPad = 12;
      const barW = boxW - barPad * 2;
      const barY = by + boxH - 6;
      ctx.fillStyle = "rgba(255, 255, 255, 0.16)";
      ctx.fillRect(bx + barPad, barY, barW, 2.5);
      ctx.fillStyle = "#f87171";
      ctx.fillRect(bx + barPad, barY, barW * frac, 2.5);
    }

    ctx.restore();
  }

  return {
    createScopeState,
    pushSpectrumRow,
    renderScope,
  };
})();
