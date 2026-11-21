"""Full-duplex 48 kHz Windows audio engine with stereo routing, software SQL, mobile exclusivity & client tunneling."""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import sounddevice as sd

import config as cfg_mod
import usb_discovery
from wasapi_loopback import WasapiLoopbackCapture

log = logging.getLogger("audio_engine")

SAMPLE_RATE = 48000
BLOCK_SIZE = 960  # 20 ms at 48 kHz

MORSE_PATTERNS: Dict[str, str] = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
    "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
    "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
    "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....",
    "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    "/": "-..-.", "?": "..--..", ".": ".-.-.-", "-": "-....-", ",": "--..--",
    ":": "---...", "'": ".----.", "(": "-.--.", ")": "-.--.-", "=": "-...-",
    "+": ".-.-.", "\"": ".-..-.", "@": ".--.-.",
}


def render_cw_sidetone(text: str, wpm: int, freq_hz: float = 700.0) -> np.ndarray:
    """Synthesize smooth raised-cosine 700 Hz CW sidetone for Windows monitor/recording."""
    unit_n = max(120, int(round((1.2 / max(6, min(48, int(wpm or 20)))) * SAMPLE_RATE)))
    ramp_n = min(unit_n // 3, int(round(0.005 * SAMPLE_RATE)))
    segs: List[np.ndarray] = []
    prev_char = False
    for ch in str(text or "").upper():
        if ch == " ":
            segs.append(np.zeros((4 if prev_char else 7) * unit_n, dtype=np.float32))
            prev_char = False
            continue
        if ch == "^":
            prev_char = False
            continue
        pat = MORSE_PATTERNS.get(ch)
        if not pat:
            continue
        if prev_char:
            segs.append(np.zeros(3 * unit_n, dtype=np.float32))
        for ei, elem in enumerate(pat):
            dur_n = (3 if elem == "-" else 1) * unit_n
            env = np.ones(dur_n, dtype=np.float32)
            if ramp_n > 1 and dur_n > 2 * ramp_n:
                r = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, ramp_n, dtype=np.float32)))
                env[:ramp_n] = r
                env[-ramp_n:] = r[::-1]
            segs.append(env)
            if ei < len(pat) - 1:
                segs.append(np.zeros(unit_n, dtype=np.float32))
        prev_char = True
    if not segs:
        return np.zeros(0, dtype=np.float32)
    env_all = np.concatenate(segs)
    t = np.arange(env_all.size, dtype=np.float32) * (2.0 * np.pi * freq_hz / SAMPLE_RATE)
    return (0.24 * env_all * np.sin(t)).astype(np.float32)


DTMF_TONES: Dict[str, Tuple[int, int]] = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "A": (697, 1633),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477), "B": (770, 1633),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "C": (852, 1633),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477), "D": (941, 1633),
}


def render_dtmf_sequence(digits: str) -> np.ndarray:
    """Synthesize repeater-safe ITU-T DTMF sequence (650ms TX/CTCSS lead, 130ms tone, 100ms gap, 220ms tail)."""
    cleaned = [ch for ch in str(digits or "").upper() if ch in DTMF_TONES or ch == ","]
    if not any(ch in DTMF_TONES for ch in cleaned):
        return np.zeros(0, dtype=np.float32)
    tone_n = int(round(0.130 * SAMPLE_RATE))
    gap_n = int(round(0.100 * SAMPLE_RATE))
    comma_n = int(round(0.350 * SAMPLE_RATE))
    lead_n = int(round(0.650 * SAMPLE_RATE))
    tail_n = int(round(0.220 * SAMPLE_RATE))
    ramp_n = max(1, int(round(0.005 * SAMPLE_RATE)))

    env = np.ones(tone_n, dtype=np.float32)
    if tone_n > 2 * ramp_n:
        r = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, ramp_n, dtype=np.float32)))
        env[:ramp_n] = r
        env[-ramp_n:] = r[::-1]

    t = np.arange(tone_n, dtype=np.float32) / SAMPLE_RATE
    segs: List[np.ndarray] = [np.zeros(lead_n, dtype=np.float32)]
    for ch in cleaned:
        if ch == ",":
            segs.append(np.zeros(comma_n, dtype=np.float32))
            continue
        f_lo, f_hi = DTMF_TONES[ch]
        # Linear level (~0.29 peak with +1.8 dB high-group pre-emphasis) to avoid FM modulator clipping
        sig = (0.13 * np.sin(2.0 * np.pi * f_lo * t) + 0.16 * np.sin(2.0 * np.pi * f_hi * t)) * env
        segs.append(sig.astype(np.float32))
        segs.append(np.zeros(gap_n, dtype=np.float32))
    segs.append(np.zeros(tail_n, dtype=np.float32))
    return np.concatenate(segs).astype(np.float32)


def sql_pct_to_s_pct(sql_pct: float) -> float:
    """Calibrated Icom IC-7300 / IC-9700 SQL (0..100%) -> S-meter threshold % (0..100% of 241).
    Measured curve on both rigs: 0..33 = Noise SQL (<S1), 34 = S1 (0), 50 = S5 (60),
    70 = S9 (120), 80 = S9+20 (160.3), 100 = S9+60 (241).
    """
    if sql_pct < 34.0:
        return 0.0
    if sql_pct <= 50.0:
        s_raw = ((sql_pct - 34.0) / 16.0) * 60.0
    elif sql_pct <= 70.0:
        s_raw = 60.0 + ((sql_pct - 50.0) / 20.0) * 60.0
    else:
        s_raw = 120.0 + ((min(100.0, sql_pct) - 70.0) / 30.0) * 121.0
    return (s_raw / 241.0) * 100.0


class StereoRingBuffer:
    """Lock-protected FIFO sample ring buffer eliminating clock-jitter crackles and robotic repeats."""

    def __init__(self, capacity: int = 24000, prefill: int = 1920, max_fill: int = 5760) -> None:
        self._buf = np.zeros((capacity, 2), dtype=np.float32)
        self._cap = capacity
        self._prefill = prefill
        self._max_fill = max_fill
        self._r = 0
        self._w = 0
        self._count = 0
        self._primed = False
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._r = 0
            self._w = 0
            self._count = 0
            self._primed = False

    def push(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        n = samples.shape[0]
        with self._lock:
            if n >= self._cap:
                samples = samples[-self._cap :]
                n = self._cap
            # Trim gently if clock drift exceeds max_fill
            if self._count + n > self._max_fill:
                drop = (self._count + n) - (self._prefill + BLOCK_SIZE)
                if drop > 0:
                    drop = min(drop, self._count)
                    self._r = (self._r + drop) % self._cap
                    self._count -= drop

            first = min(n, self._cap - self._w)
            self._buf[self._w : self._w + first] = samples[:first]
            rem = n - first
            if rem > 0:
                self._buf[:rem] = samples[first:]
            self._w = (self._w + n) % self._cap
            self._count += n
            if not self._primed and self._count >= self._prefill:
                self._primed = True

    def pop(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, 2), dtype=np.float32)
        with self._lock:
            if not self._primed:
                return out
            if self._count < frames:
                self._primed = False
                return out
            first = min(frames, self._cap - self._r)
            out[:first] = self._buf[self._r : self._r + first]
            rem = frames - first
            if rem > 0:
                out[first:] = self._buf[:rem]
            self._r = (self._r + frames) % self._cap
            self._count -= frames
        return out


class AudioEngine:
    """Manages local radio capture/playback, Windows speaker routing, SQL gating, mobile & remote client audio."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Per-radio capture/playback streams (Host mode)
        self._radio_in_streams: Dict[int, sd.InputStream] = {}
        self._radio_out_streams: Dict[int, sd.OutputStream] = {}
        self._radio_tx_rings: Dict[int, StereoRingBuffer] = {
            1: StereoRingBuffer(capacity=24000, prefill=960, max_fill=4800),
            2: StereoRingBuffer(capacity=24000, prefill=960, max_fill=4800),
        }

        # Jitter-buffered contiguous RX ring buffers per radio
        self._rx_rings: Dict[int, StereoRingBuffer] = {
            1: StereoRingBuffer(capacity=24000, prefill=1920, max_fill=12000),
            2: StereoRingBuffer(capacity=24000, prefill=1920, max_fill=12000),
        }
        self._radio_rx_ts: Dict[int, float] = {1: 0.0, 2: 0.0}

        # PC Speaker & Mic streams
        self._pc_out_stream: Optional[sd.OutputStream] = None
        self._pc_in_stream: Optional[sd.InputStream] = None
        self._pc_out_queue: queue.Queue = queue.Queue(maxsize=30)

        # Config & live radio references
        self.mode: str = "host"  # "host" or "client"
        self.routing: Dict[int, str] = {1: "both", 2: "both"}
        self.master_volume: float = 0.85
        self.windows_mute: Dict[int, bool] = {1: False, 2: False}
        self.windows_volume: Dict[int, int] = {1: 85, 2: 85}
        self.windows_sql: Dict[int, int] = {1: 15, 2: 15}
        self.windows_af: Dict[int, int] = {1: 75, 2: 75}
        self.radio_s_pct: Dict[int, float] = {1: 0.0, 2: 0.0}
        self.radio_tx: Dict[int, bool] = {1: False, 2: False}
        self.radio_connected: Dict[int, bool] = {1: False, 2: False}
        self.radio_dual_rx: Dict[int, bool] = {1: False, 2: False}
        self.radio_dualwatch: Dict[int, bool] = {1: False, 2: False}
        self.radio_data_mode: Dict[int, bool] = {1: False, 2: False}
        self.radio_has_hw_sql: Dict[int, bool] = {1: False, 2: False}
        self.radio_hw_sql_open: Dict[int, bool] = {1: True, 2: True}
        self.radio_hw_sql_level: Dict[int, int] = {1: 15, 2: 15}
        self.radio_cw_tx: Dict[int, bool] = {1: False, 2: False}
        self._cw_lock = threading.Lock()
        self._cw_sidetone_last_seq: Dict[int, int] = {1: 0, 2: 0}
        self._cw_sidetone_chunks: Dict[int, List[np.ndarray]] = {1: [], 2: []}
        self.rx_rms: Dict[int, float] = {1: 0.0, 2: 0.0}
        self.pc_out_device_name: str = ""

        # Squelch hang timers & 1750 Hz repeater tone burst state
        self._win_sql_open_until: Dict[int, float] = {1: 0.0, 2: 0.0}
        self._mobile_connected: bool = False
        self._tone_1750: Dict[int, bool] = {1: False, 2: False}
        self._tone_phase: Dict[int, float] = {1: 0.0, 2: 0.0}
        self._dtmf_lock = threading.Lock()
        self.dtmf_active: bool = False
        self.dtmf_radio: int = 0
        self._dtmf_buf: Optional[np.ndarray] = None
        self._dtmf_pos: int = 0

        # Audio callbacks for WebSocket streaming (to mobile and remote PC clients)
        self.on_radio_rx_frame: Optional[Callable[[int, bytes, float], None]] = None
        self.on_client_tx_frame: Optional[Callable[[int, bytes], None]] = None
        self.on_voice_play_finished: Optional[Callable[[int], None]] = None
        self.active_tx_source: str = "pc"  # "pc", "mobile", "client", "voice_mem"

        # Voice Memory (record from PC mic & transmit to radio)
        self._voice_dir = cfg_mod.APP_DIR / "voice_memories"
        self._voice_dir.mkdir(parents=True, exist_ok=True)
        self._voice_lock = threading.Lock()
        self._voice_clips: Dict[int, np.ndarray] = {}
        self.voice_rec_slot: int = 0
        self._voice_rec_chunks: List[np.ndarray] = []
        self._voice_rec_samples: int = 0
        self.voice_play_active: bool = False
        self.voice_play_slot: int = 0
        self.voice_play_radio: int = 0
        self._voice_play_buf: Optional[np.ndarray] = None
        self._voice_play_pos: int = 0
        self._load_voice_clips()

        # Full QSO Audio Recorder (records RX + TX 48 kHz WAV into recordings/)
        self._qso_dir = cfg_mod.APP_DIR / "recordings"
        self._qso_dir.mkdir(parents=True, exist_ok=True)
        self._qso_lock = threading.Lock()
        self.qso_rec_active: bool = False
        self._qso_queue: Optional[queue.Queue] = None
        self._qso_writer_thread: Optional[threading.Thread] = None
        self._qso_filename: str = ""
        self._qso_last_saved: str = ""
        self._qso_samples: int = 0
        self._qso_tx_ring = StereoRingBuffer(capacity=24000, prefill=0, max_fill=4800)

        # Windows Default Playback Loopback -> Radio TX (Audio Passthrough)
        self.passthrough_active: bool = False
        self.passthrough_mic_ignored: bool = True
        self._pass_mic_ring = StereoRingBuffer(capacity=12000, prefill=0, max_fill=4800)
        self.swr_scan_mute_slot: int = 0
        self._loopback = WasapiLoopbackCapture(self._on_loopback_audio, target_rate=SAMPLE_RATE)

        # Current device selections
        self._dev_cfg: Dict[str, Any] = {}

    def start(self, loop: asyncio.AbstractEventLoop, cfg: Dict[str, Any]) -> None:
        self._loop = loop
        self._running = True
        self._dev_cfg = cfg

    def stop(self) -> None:
        self._running = False
        self.passthrough_active = False
        self._loopback.stop()
        self.stop_qso_rec()
        self._close_all_streams()

    def set_passthrough(self, enabled: bool) -> bool:
        on = bool(enabled)
        self.passthrough_active = on
        self._pass_mic_ring.clear()
        if on:
            self._loopback.start()
        else:
            self._loopback.stop()
        return self.passthrough_active

    def set_passthrough_mic_ignored(self, ignored: bool) -> bool:
        self.passthrough_mic_ignored = bool(ignored)
        if self.passthrough_mic_ignored:
            self._pass_mic_ring.clear()
        return self.passthrough_mic_ignored

    def get_passthrough_status(self) -> Dict[str, Any]:
        return {
            "active": self.passthrough_active,
            "mic_ignored": self.passthrough_mic_ignored,
            "error": self._loopback.error if self.passthrough_active else "",
        }

    def _on_loopback_audio(self, stereo_48k: np.ndarray) -> None:
        if self.passthrough_active and self._running:
            mix = stereo_48k
            if not self.passthrough_mic_ignored:
                mic_blk = self._pass_mic_ring.pop(stereo_48k.shape[0])
                if mic_blk.shape[0] == stereo_48k.shape[0] and np.any(mic_blk):
                    mix = np.clip(stereo_48k + mic_blk, -1.0, 1.0)
            else:
                self._pass_mic_ring.clear()
            self._push_pc_tx_audio(mix)

    def _push_pc_tx_audio(self, stereo_48k: np.ndarray) -> None:
        if not self._running:
            return
        mute_slot = self.swr_scan_mute_slot
        if self.mode == "host":
            if self.active_tx_source == "pc" and not self.voice_play_active and not self.dtmf_active:
                for slot in (1, 2):
                    if self.radio_tx.get(slot) and slot != mute_slot:
                        self._push_radio_tx(slot, stereo_48k)
        else:
            tx_active = False
            pcm: Optional[bytes] = None
            for slot in (1, 2):
                if self.radio_tx.get(slot) and slot != mute_slot and self.on_client_tx_frame:
                    if pcm is None:
                        pcm = np.clip(stereo_48k[:, 0] * 32767.0, -32768, 32767).astype("<i2").tobytes()
                    tx_active = True
                    self.on_client_tx_frame(slot, pcm)
            if tx_active and self.qso_rec_active:
                self._qso_tx_ring.push(stereo_48k)

    def _close_all_streams(self) -> None:
        for s in list(self._radio_in_streams.values()):
            try:
                s.stop(); s.close()
            except Exception:
                pass
        self._radio_in_streams.clear()

        for s in list(self._radio_out_streams.values()):
            try:
                s.stop(); s.close()
            except Exception:
                pass
        self._radio_out_streams.clear()

        if self._pc_out_stream:
            try:
                self._pc_out_stream.stop(); self._pc_out_stream.close()
            except Exception:
                pass
            self._pc_out_stream = None

        if self._pc_in_stream:
            try:
                self._pc_in_stream.stop(); self._pc_in_stream.close()
            except Exception:
                pass
            self._pc_in_stream = None

    def apply_config(self, cfg: Dict[str, Any]) -> None:
        """Apply updated audio devices and routing configuration."""
        with self._lock:
            self.mode = cfg.get("mode", "host")
            ar = cfg.get("audio_routing", {})
            self.routing[1] = ar.get("radio1_channel", "both")
            self.routing[2] = ar.get("radio2_channel", "both")
            pc_cfg = cfg.get("pc_audio", {})
            self.master_volume = max(0.0, min(1.0, pc_cfg.get("master_volume", 85) / 100.0))
            ui_cfg = cfg.get("ui", {})
            if "passthrough_mic_ignored" in ui_cfg:
                self.passthrough_mic_ignored = bool(ui_cfg.get("passthrough_mic_ignored", True))
            for rcfg in cfg.get("radios", []):
                s = int(rcfg.get("slot", 0))
                if s in (1, 2):
                    if "win_volume" in rcfg:
                        self.windows_volume[s] = max(0, min(100, int(rcfg["win_volume"])))
                    if "win_mute" in rcfg:
                        self.windows_mute[s] = bool(rcfg["win_mute"])
                    if "sql_threshold" in rcfg:
                        self.windows_sql[s] = max(0, min(100, int(rcfg["sql_threshold"])))
            self._dev_cfg = cfg

        self._rebuild_pc_streams(cfg)
        if self.mode == "host":
            self._rebuild_radio_streams(cfg)
        else:
            # Close host radio streams in client mode
            for s in list(self._radio_in_streams.values()):
                try:
                    s.stop(); s.close()
                except Exception:
                    pass
            self._radio_in_streams.clear()
            for s in list(self._radio_out_streams.values()):
                try:
                    s.stop(); s.close()
                except Exception:
                    pass
            self._radio_out_streams.clear()

    def update_radio_state(self, slot: int, state: Dict[str, Any]) -> None:
        """Sync live S-meter, connection status, SQL, AF, TX from RadioManager."""
        was_connected = self.radio_connected.get(slot, False)
        now_connected = bool(state.get("connected", False))
        self.radio_connected[slot] = now_connected
        self.radio_dual_rx[slot] = bool(state.get("supports_dual_rx", False))
        self.radio_tx[slot] = bool(state.get("tx", False))
        self.radio_hw_sql_level[slot] = int(state.get("sql", 15))
        self.windows_af[slot] = int(state.get("af_gain", 75))

        active_key = "sub" if state.get("active_band") == "SUB" else "main"
        vfo_st = state.get(active_key, state.get("main", {})) or {}
        mode_u = str(vfo_st.get("mode", "")).upper()
        self.radio_data_mode[slot] = bool(vfo_st.get("data_mode")) or mode_u in ("DIGI", "DATA-U", "DATA-L", "USB-D", "LSB-D", "FM-D")

        # S-meter percentage (0..100) + hardware squelch flag for Windows SQL gating
        sel_s = float(vfo_st.get("s_raw", 0))
        main_s = float(state.get("main", {}).get("s_raw", 0))
        sub_s = float(state.get("sub", {}).get("s_raw", 0))
        dw = bool(state.get("dualwatch", False))
        effective_s = max(main_s, sub_s) if dw else sel_s
        self.radio_s_pct[slot] = min(100.0, (effective_s / 241.0) * 100.0)
        self.radio_dualwatch[slot] = dw
        self.radio_has_hw_sql[slot] = bool(state.get("has_hw_sql", False))
        self.radio_hw_sql_open[slot] = bool(state.get("sql_hw_open", True))

        cw_active = int(state.get("cw_tx_slot", 0)) > 0
        self.radio_cw_tx[slot] = cw_active
        seq = int(state.get("cw_sidetone_seq", 0))
        if not cw_active:
            with self._cw_lock:
                self._cw_sidetone_chunks[slot].clear()
            if seq != self._cw_sidetone_last_seq.get(slot, 0):
                self._cw_sidetone_last_seq[slot] = seq
        elif seq != self._cw_sidetone_last_seq.get(slot, 0):
            self._cw_sidetone_last_seq[slot] = seq
            txt = str(state.get("cw_sidetone_text", "")).strip()
            if txt:
                wave_arr = render_cw_sidetone(txt, int(state.get("cw_wpm", 20)))
                if wave_arr.size > 0:
                    with self._cw_lock:
                        self._cw_sidetone_chunks[slot].append(wave_arr)

        # Auto-start audio immediately when radio connects
        if now_connected and not was_connected and self.mode == "host" and self._running:
            self._ensure_radio_stream_for_slot(slot, state.get("usb_model", ""))

    def _open_input_stream(self, dev_idx: Optional[int], callback: Callable) -> Optional[sd.InputStream]:
        last_exc: Optional[Exception] = None
        try:
            info = sd.query_devices(dev_idx) if dev_idx is not None else sd.query_devices(kind="input")
            ch = min(2, int(info["max_input_channels"]))
            for sr in (SAMPLE_RATE, int(info["default_samplerate"]) or SAMPLE_RATE):
                try:
                    bs = int(round(sr * (BLOCK_SIZE / SAMPLE_RATE)))
                    stream = sd.InputStream(
                        device=dev_idx,
                        channels=ch,
                        samplerate=sr,
                        blocksize=bs,
                        dtype="float32",
                        callback=callback,
                    )
                    stream.start()
                    return stream
                except Exception as exc:
                    last_exc = exc
        except Exception as exc:
            last_exc = exc
        if last_exc:
            log.warning("Failed to open input stream %s: %s", dev_idx, last_exc)
        return None

    def _open_output_stream(self, dev_idx: Optional[int], callback: Callable) -> Optional[sd.OutputStream]:
        last_exc: Optional[Exception] = None
        try:
            info = sd.query_devices(dev_idx) if dev_idx is not None else sd.query_devices(kind="output")
            ch = min(2, int(info["max_output_channels"]))
            for sr in (SAMPLE_RATE, int(info["default_samplerate"]) or SAMPLE_RATE):
                try:
                    bs = int(round(sr * (BLOCK_SIZE / SAMPLE_RATE)))
                    stream = sd.OutputStream(
                        device=dev_idx,
                        channels=ch,
                        samplerate=sr,
                        blocksize=bs,
                        dtype="float32",
                        callback=callback,
                    )
                    stream.start()
                    return stream
                except Exception as exc:
                    last_exc = exc
        except Exception as exc:
            last_exc = exc
        if last_exc:
            log.warning("Failed to open output stream %s: %s", dev_idx, last_exc)
        return None

    def _resample_block(self, data: np.ndarray, target_len: int = BLOCK_SIZE) -> np.ndarray:
        """Fast linear resample to 48kHz stereo if device native sample rate differs."""
        if data.ndim == 1:
            data = np.column_stack([data, data])
        elif data.shape[1] == 1:
            data = np.column_stack([data[:, 0], data[:, 0]])
        elif data.shape[1] > 2:
            data = data[:, :2]

        if data.shape[0] == target_len:
            return data.astype(np.float32, copy=False)
        x_old = np.linspace(0.0, 1.0, data.shape[0], endpoint=False)
        x_new = np.linspace(0.0, 1.0, target_len, endpoint=False)
        l = np.interp(x_new, x_old, data[:, 0])
        r = np.interp(x_new, x_old, data[:, 1])
        return np.column_stack([l, r]).astype(np.float32)

    def _rebuild_pc_streams(self, cfg: Dict[str, Any]) -> None:
        if self._pc_out_stream:
            try:
                self._pc_out_stream.stop(); self._pc_out_stream.close()
            except Exception:
                pass
            self._pc_out_stream = None
        if self._pc_in_stream:
            try:
                self._pc_in_stream.stop(); self._pc_in_stream.close()
            except Exception:
                pass
            self._pc_in_stream = None

        pc_cfg = cfg.get("pc_audio", {})
        spk_name = pc_cfg.get("speaker_device", "")
        mic_name = pc_cfg.get("mic_device", "")

        spk_idx = usb_discovery.find_audio_device_index(spk_name, is_input=False)
        mic_idx = usb_discovery.find_audio_device_index(mic_name, is_input=True)

        # Fallback to preferred hostapi default if empty
        if spk_idx is None:
            ha = usb_discovery.get_preferred_hostapi_index()
            if ha is not None:
                try:
                    spk_idx = sd.query_hostapis(ha)["default_output_device"]
                except Exception:
                    spk_idx = None
        if mic_idx is None:
            ha = usb_discovery.get_preferred_hostapi_index()
            if ha is not None:
                try:
                    mic_idx = sd.query_hostapis(ha)["default_input_device"]
                except Exception:
                    mic_idx = None

        pc_out_sr = [SAMPLE_RATE]
        pc_in_sr = [SAMPLE_RATE]

        def pc_out_cb(outdata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            sr_now = pc_out_sr[0] or SAMPLE_RATE
            src_frames = frames if sr_now == SAMPLE_RATE else max(1, int(round(frames * (SAMPLE_RATE / sr_now))))
            mixed = self._mix_windows_playback(src_frames)
            if self.qso_rec_active:
                tx_blk = self._qso_tx_ring.pop(src_frames)
                rx_m = (mixed[:, 0] + mixed[:, 1]) * 0.5
                tx_m = (tx_blk[:, 0] + tx_blk[:, 1]) * 0.5
                pcm_rec = (np.clip(rx_m + tx_m, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                self._enqueue_qso_bytes(pcm_rec, src_frames)
            if src_frames != frames:
                mixed = self._resample_block(mixed, frames)
            if outdata.shape[1] == 1:
                outdata[:, 0] = (mixed[:, 0] + mixed[:, 1]) * 0.5
            else:
                outdata[:, :2] = mixed[:, :2]

        def pc_in_cb(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            if not self._running:
                return
            sr_now = pc_in_sr[0] or SAMPLE_RATE
            tgt_frames = max(1, int(round(frames * (SAMPLE_RATE / sr_now))))
            stereo = self._resample_block(indata, tgt_frames)
            if self.voice_rec_slot in (1, 2, 3, 4):
                mono_rec = ((stereo[:, 0] + stereo[:, 1]) * 0.5).astype(np.float32)
                with self._voice_lock:
                    if self.voice_rec_slot in (1, 2, 3, 4) and self._voice_rec_samples < SAMPLE_RATE * 60:
                        self._voice_rec_chunks.append(mono_rec)
                        self._voice_rec_samples += mono_rec.size

            if not self.passthrough_active:
                self._push_pc_tx_audio(stereo)
            elif not self.passthrough_mic_ignored:
                self._pass_mic_ring.push(stereo)
            else:
                self._pass_mic_ring.clear()

        self._pc_out_stream = self._open_output_stream(spk_idx, pc_out_cb)
        if not self._pc_out_stream and spk_idx is not None:
            # Fallback to system default output if saved device fails to open
            ha = usb_discovery.get_preferred_hostapi_index()
            if ha is not None:
                try:
                    def_idx = sd.query_hostapis(ha)["default_output_device"]
                    if def_idx is not None and def_idx != spk_idx:
                        self._pc_out_stream = self._open_output_stream(def_idx, pc_out_cb)
                        spk_idx = def_idx
                except Exception:
                    pass
        if self._pc_out_stream:
            try:
                pc_out_sr[0] = int(round(self._pc_out_stream.samplerate)) or SAMPLE_RATE
                self.pc_out_device_name = sd.query_devices(spk_idx)["name"] if spk_idx is not None else "Default"
            except Exception:
                self.pc_out_device_name = str(spk_idx)
            log.info("Started PC speaker playback (device %s: %s @ %d Hz)", spk_idx, self.pc_out_device_name, pc_out_sr[0])
        else:
            self.pc_out_device_name = "FAILED"
        self._pc_in_stream = self._open_input_stream(mic_idx, pc_in_cb)
        if self._pc_in_stream:
            try:
                pc_in_sr[0] = int(round(self._pc_in_stream.samplerate)) or SAMPLE_RATE
            except Exception:
                pass
            log.info("Started PC microphone capture (device %s @ %d Hz)", mic_idx, pc_in_sr[0])

    def _rebuild_radio_streams(self, cfg: Dict[str, Any]) -> None:
        for slot_cfg in cfg.get("radios", []):
            slot = int(slot_cfg.get("slot", 1))
            self._open_radio_slot_streams(slot, slot_cfg, usb_model="")

    def _ensure_radio_stream_for_slot(self, slot: int, usb_model: str) -> None:
        if slot in self._radio_in_streams:
            return
        for slot_cfg in self._dev_cfg.get("radios", []):
            if int(slot_cfg.get("slot", 0)) == slot:
                self._open_radio_slot_streams(slot, slot_cfg, usb_model=usb_model)
                break

    def _open_radio_slot_streams(self, slot: int, slot_cfg: Dict[str, Any], usb_model: str) -> None:
        # Close existing for slot
        if slot in self._radio_in_streams:
            try:
                self._radio_in_streams[slot].stop(); self._radio_in_streams[slot].close()
            except Exception:
                pass
            del self._radio_in_streams[slot]
        if slot in self._radio_out_streams:
            try:
                self._radio_out_streams[slot].stop(); self._radio_out_streams[slot].close()
            except Exception:
                pass
            del self._radio_out_streams[slot]

        in_name = slot_cfg.get("audio_in_device", "")
        out_name = slot_cfg.get("audio_out_device", "")

        in_idx = usb_discovery.find_audio_device_index(in_name, is_input=True)
        out_idx = usb_discovery.find_audio_device_index(out_name, is_input=False)

        # Auto-match from USB bus model string if not explicitly configured
        if in_idx is None:
            model_guess = usb_model
            if not model_guess:
                for p in usb_discovery.list_serial_ports_with_usb():
                    if p["port"].upper() == slot_cfg.get("com_port", "").upper():
                        model_guess = p.get("usb_model", "")
                        break
            if model_guess:
                m_in = usb_discovery.auto_match_audio_device(model_guess, is_input=True)
                if m_in:
                    in_idx = m_in["index"]
        if out_idx is None:
            model_guess = usb_model
            if not model_guess:
                for p in usb_discovery.list_serial_ports_with_usb():
                    if p["port"].upper() == slot_cfg.get("com_port", "").upper():
                        model_guess = p.get("usb_model", "")
                        break
            if model_guess:
                m_out = usb_discovery.auto_match_audio_device(model_guess, is_input=False)
                if m_out:
                    out_idx = m_out["index"]

        if in_idx is not None:
            self._rx_rings[slot].clear()
            r_in_sr = [SAMPLE_RATE]
            def radio_in_cb(indata: np.ndarray, frames: int, time_info: Any, status: Any, _slot: int = slot) -> None:
                if not self._running:
                    return
                sr_now = r_in_sr[0] or SAMPLE_RATE
                tgt_frames = max(1, int(round(frames * (SAMPLE_RATE / sr_now))))
                stereo = self._resample_block(indata, tgt_frames)
                self._rx_rings[_slot].push(stereo)
                self._radio_rx_ts[_slot] = time.monotonic()

                mono = (stereo[:, 0] + stereo[:, 1]) * 0.5
                rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
                self.rx_rms[_slot] = rms

                if self.on_radio_rx_frame:
                    # Mix L+R to mono 48kHz Int16 for network streaming
                    pcm_bytes = np.clip(mono * 32767.0, -32768, 32767).astype("<i2").tobytes()
                    self.on_radio_rx_frame(_slot, pcm_bytes, rms)

            st_in = self._open_input_stream(in_idx, radio_in_cb)
            if st_in:
                try:
                    r_in_sr[0] = int(round(st_in.samplerate)) or SAMPLE_RATE
                except Exception:
                    pass
                self._radio_in_streams[slot] = st_in
                log.info("Started RX audio capture for Radio %d (device %s @ %d Hz)", slot, in_idx, r_in_sr[0])

        if out_idx is not None:
            self._radio_tx_rings[slot].clear()
            def radio_out_cb(outdata: np.ndarray, frames: int, time_info: Any, status: Any, _slot: int = slot) -> None:
                if self.swr_scan_mute_slot == _slot:
                    outdata.fill(0)
                    return

                if self._tone_1750.get(_slot, False):
                    ph = self._tone_phase[_slot]
                    inc = 2.0 * np.pi * 1750.0 / SAMPLE_RATE
                    t = ph + inc * np.arange(frames, dtype=np.float32)
                    self._tone_phase[_slot] = float((ph + inc * frames) % (2.0 * np.pi))
                    tone = (0.45 * np.sin(t)).astype(np.float32)
                    if self.qso_rec_active and self.radio_tx.get(_slot):
                        self._qso_tx_ring.push(np.column_stack([tone, tone]))
                    if outdata.shape[1] == 1:
                        outdata[:, 0] = tone
                    else:
                        outdata[:, 0] = tone
                        outdata[:, 1] = tone
                    return

                if self.dtmf_active and self.dtmf_radio == _slot:
                    dtmf_blk = np.zeros(frames, dtype=np.float32)
                    with self._dtmf_lock:
                        if self.dtmf_active and self.dtmf_radio == _slot and self._dtmf_buf is not None:
                            buf = self._dtmf_buf
                            pos = self._dtmf_pos
                            avail = min(frames, buf.size - pos)
                            if avail > 0:
                                dtmf_blk[:avail] = buf[pos : pos + avail]
                                self._dtmf_pos = pos + avail
                            if self._dtmf_pos >= buf.size:
                                self.dtmf_active = False
                                self.dtmf_radio = 0
                                self._dtmf_buf = None
                                self._dtmf_pos = 0
                    if self.qso_rec_active and self.radio_tx.get(_slot):
                        self._qso_tx_ring.push(np.column_stack([dtmf_blk, dtmf_blk]))
                    if outdata.shape[1] == 1:
                        outdata[:, 0] = dtmf_blk
                    else:
                        outdata[:, 0] = dtmf_blk
                        outdata[:, 1] = dtmf_blk
                    return

                if self.voice_play_active and self.voice_play_radio == _slot:
                    finished_radio = 0
                    vm_blk = np.zeros(frames, dtype=np.float32)
                    with self._voice_lock:
                        if self.voice_play_active and self.voice_play_radio == _slot and self._voice_play_buf is not None:
                            buf = self._voice_play_buf
                            pos = self._voice_play_pos
                            avail = min(frames, buf.size - pos)
                            if avail > 0:
                                vm_blk[:avail] = buf[pos : pos + avail]
                                self._voice_play_pos = pos + avail
                            if self._voice_play_pos >= buf.size:
                                finished_radio = self.voice_play_radio
                                self.voice_play_active = False
                                self.voice_play_slot = 0
                                self.voice_play_radio = 0
                                self._voice_play_buf = None
                                self._voice_play_pos = 0
                    if self.qso_rec_active and self.radio_tx.get(_slot):
                        self._qso_tx_ring.push(np.column_stack([vm_blk, vm_blk]))
                    if outdata.shape[1] == 1:
                        outdata[:, 0] = vm_blk
                    else:
                        outdata[:, 0] = vm_blk
                        outdata[:, 1] = vm_blk
                    if finished_radio and self.on_voice_play_finished and self._loop:
                        self._loop.call_soon_threadsafe(self.on_voice_play_finished, finished_radio)
                    return

                blk = self._radio_tx_rings[_slot].pop(frames)
                if self.qso_rec_active and self.radio_tx.get(_slot):
                    self._qso_tx_ring.push(blk)
                if outdata.shape[1] == 1:
                    outdata[:, 0] = (blk[:, 0] + blk[:, 1]) * 0.5
                else:
                    outdata[:, :2] = blk[:, :2]

            st_out = self._open_output_stream(out_idx, radio_out_cb)
            if st_out:
                self._radio_out_streams[slot] = st_out
                log.info("Started TX audio output for Radio %d (device %s)", slot, out_idx)

    def set_tone_1750(self, slot: int, active: bool) -> None:
        self._tone_1750[slot] = bool(active)

    def set_dtmf_standby(self, radio_slot: int) -> None:
        """Hold silent TX carrier on slot while CI-V PTT engages, blocking mic/passthrough leak."""
        with self._dtmf_lock:
            self._radio_tx_rings[radio_slot].clear()
            self._dtmf_buf = None
            self._dtmf_pos = 0
            self.dtmf_radio = radio_slot
            self.dtmf_active = True

    def start_dtmf_play(self, radio_slot: int, digits: str) -> float:
        buf = render_dtmf_sequence(digits)
        if buf.size == 0:
            return 0.0
        with self._dtmf_lock:
            self._radio_tx_rings[radio_slot].clear()
            self._dtmf_buf = buf
            self._dtmf_pos = 0
            self.dtmf_radio = radio_slot
            self.dtmf_active = True
        with self._cw_lock:
            self._cw_sidetone_chunks[radio_slot].append((buf * 0.35).astype(np.float32))
        return float(buf.size) / SAMPLE_RATE

    def stop_dtmf_play(self) -> None:
        with self._dtmf_lock:
            r = self.dtmf_radio
            self.dtmf_active = False
            self.dtmf_radio = 0
            self._dtmf_buf = None
            self._dtmf_pos = 0
        if r in (1, 2):
            self._radio_tx_rings[r].clear()

    def _push_radio_tx(self, slot: int, stereo_48k: np.ndarray) -> None:
        ring = self._radio_tx_rings.get(slot)
        if ring is not None:
            ring.push(stereo_48k)

    def inject_remote_tx_pcm(self, slot: int, pcm_bytes: bytes, source: str = "mobile", sample_rate: int = SAMPLE_RATE) -> None:
        """Accept Int16 mono PCM from Mobile HTTPS client or Remote PC Client and route to radio TX."""
        self.active_tx_source = source
        arr = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
        if arr.size == 0:
            return
        if sample_rate != SAMPLE_RATE:
            target_len = max(1, int(round(arr.size * (SAMPLE_RATE / sample_rate))))
            x_old = np.linspace(0.0, 1.0, arr.size, endpoint=False)
            x_new = np.linspace(0.0, 1.0, target_len, endpoint=False)
            arr = np.interp(x_new, x_old, arr).astype(np.float32)
        self._push_radio_tx(slot, np.column_stack([arr, arr]))

    def inject_client_rx_pcm(self, slot: int, pcm_bytes: bytes) -> None:
        """Used in Client Mode: feed incoming remote RX PCM into local Windows mixer."""
        arr = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
        if arr.size == 0:
            return
        stereo = np.column_stack([arr, arr]).astype(np.float32)
        self._rx_rings[slot].push(stereo)
        self._radio_rx_ts[slot] = time.monotonic()

    def set_mobile_connected(self, connected: bool) -> None:
        """Mute Windows local speaker unconditionally while any mobile client is connected."""
        self._mobile_connected = bool(connected)

    def clear_mobile_suppression(self) -> None:
        self._mobile_connected = False

    def is_windows_suppressed_by_mobile(self) -> bool:
        return self._mobile_connected

    def _is_windows_sql_open(self, slot: int, buf: np.ndarray) -> bool:
        if self.radio_data_mode.get(slot, False) or self.radio_cw_tx.get(slot, False):
            return True
        sql = self.windows_sql.get(slot, 0)
        if sql <= 1:
            return True
        thresh_pct = sql_pct_to_s_pct(float(sql))
        eff_thresh = max(0.4, thresh_pct)
        s_pct = self.radio_s_pct.get(slot, 0.0)
        now = time.monotonic()
        if self.radio_connected.get(slot, False):
            if s_pct >= eff_thresh:
                self._win_sql_open_until[slot] = now + 0.25
                return True
            return now < self._win_sql_open_until.get(slot, 0.0)
        rms = float(np.sqrt(np.mean(buf * buf))) if buf.size else 0.0
        audio_pct = min(100.0, rms * 450.0)
        if audio_pct >= thresh_pct:
            self._win_sql_open_until[slot] = now + 0.25
            return True
        return now < self._win_sql_open_until.get(slot, 0.0)

    def _pop_cw_sidetone(self, slot: int, frames: int) -> np.ndarray:
        out = np.zeros(frames, dtype=np.float32)
        with self._cw_lock:
            chunks = self._cw_sidetone_chunks[slot]
            pos = 0
            while pos < frames and chunks:
                head = chunks[0]
                take = min(frames - pos, head.size)
                out[pos : pos + take] = head[:take]
                pos += take
                if take >= head.size:
                    chunks.pop(0)
                else:
                    chunks[0] = head[take:]
        return out

    def _mix_windows_playback(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, 2), dtype=np.float32)
        if self.is_windows_suppressed_by_mobile():
            self._rx_rings[1].pop(frames)
            self._rx_rings[2].pop(frames)
            return out

        now = time.monotonic()
        for slot in (1, 2):
            if self.windows_mute.get(slot, False):
                self._rx_rings[slot].pop(frames)
                self._pop_cw_sidetone(slot, frames)
                continue
            if self.mode == "host" and not self.radio_connected.get(slot, False):
                self._rx_rings[slot].pop(frames)
                self._pop_cw_sidetone(slot, frames)
                continue

            st_mono = self._pop_cw_sidetone(slot, frames)
            has_st = bool(np.any(st_mono))
            rx_fresh = (now - self._radio_rx_ts.get(slot, 0.0)) <= 0.5
            buf = self._rx_rings[slot].pop(frames) if rx_fresh else np.zeros((frames, 2), dtype=np.float32)

            route = self.routing.get(slot, "both")
            if route == "mute":
                continue

            win_vol = max(0.0, min(1.0, self.windows_volume.get(slot, 85) / 100.0))

            if rx_fresh and self._is_windows_sql_open(slot, buf):
                gain = win_vol * self.master_volume * 2.2
                mono = (buf[:, 0] + buf[:, 1]) * 0.5 * gain
                if route == "left":
                    out[:, 0] += mono
                elif route == "right":
                    out[:, 1] += mono
                else:  # both
                    out[:, 0] += buf[:, 0] * gain if self.radio_dual_rx.get(slot) else mono
                    out[:, 1] += buf[:, 1] * gain if self.radio_dual_rx.get(slot) else mono

            # Mix clean local 700 Hz CW sidetone unless radio USB CODEC already sends hardware sidetone
            if has_st:
                usb_rms = float(np.sqrt(np.mean(buf * buf))) if buf.size else 0.0
                if usb_rms < 0.015:
                    st_scaled = st_mono * win_vol * self.master_volume * 1.4
                    if route == "left":
                        out[:, 0] += st_scaled
                    elif route == "right":
                        out[:, 1] += st_scaled
                    else:
                        out[:, 0] += st_scaled
                        out[:, 1] += st_scaled

        return np.clip(out, -0.98, 0.98).astype(np.float32)

    # --- Voice Memory (Record from PC Mic & Play to Radio TX) ---

    def _load_voice_clips(self) -> None:
        for idx in (1, 2, 3, 4):
            path = self._voice_dir / f"v{idx}.wav"
            if path.is_file():
                try:
                    with wave.open(str(path), "rb") as wf:
                        frames = wf.readframes(wf.getnframes())
                        arr = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
                        sr = wf.getframerate()
                        if sr != SAMPLE_RATE and arr.size > 1:
                            new_len = max(1, int(round(arr.size * (SAMPLE_RATE / sr))))
                            arr = np.interp(
                                np.linspace(0.0, 1.0, new_len, endpoint=False),
                                np.linspace(0.0, 1.0, arr.size, endpoint=False),
                                arr,
                            ).astype(np.float32)
                        if arr.size > 0:
                            self._voice_clips[idx] = arr
                except Exception as exc:
                    log.warning("Failed loading voice clip %s: %s", path, exc)

    def get_voice_memory_status(self) -> Dict[str, Any]:
        with self._voice_lock:
            slots = {}
            for idx in (1, 2, 3, 4):
                clip = self._voice_clips.get(idx)
                slots[str(idx)] = round(float(clip.size) / SAMPLE_RATE, 1) if clip is not None else 0.0
            return {
                "slots": slots,
                "recording_slot": self.voice_rec_slot,
                "rec_sec": round(float(self._voice_rec_samples) / SAMPLE_RATE, 1),
                "playing_slot": self.voice_play_slot if self.voice_play_active else 0,
                "playing_radio": self.voice_play_radio if self.voice_play_active else 0,
            }

    def start_voice_rec(self, mem_idx: int) -> None:
        if mem_idx not in (1, 2, 3, 4):
            return
        with self._voice_lock:
            self.voice_rec_slot = mem_idx
            self._voice_rec_chunks = []
            self._voice_rec_samples = 0

    def stop_voice_rec(self) -> int:
        saved_idx = 0
        arr: Optional[np.ndarray] = None
        with self._voice_lock:
            saved_idx = self.voice_rec_slot
            self.voice_rec_slot = 0
            if saved_idx in (1, 2, 3, 4) and self._voice_rec_chunks:
                arr = np.concatenate(self._voice_rec_chunks).astype(np.float32)
                self._voice_clips[saved_idx] = arr
            self._voice_rec_chunks = []
            self._voice_rec_samples = 0

        if saved_idx in (1, 2, 3, 4) and arr is not None and arr.size > 0:
            path = self._voice_dir / f"v{saved_idx}.wav"
            try:
                pcm = np.clip(arr * 32767.0, -32768, 32767).astype("<i2")
                with wave.open(str(path), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(pcm.tobytes())
            except Exception as exc:
                log.warning("Failed saving voice clip %s: %s", path, exc)
        return saved_idx

    def start_voice_play(self, radio_slot: int, mem_idx: int) -> bool:
        with self._voice_lock:
            clip = self._voice_clips.get(mem_idx)
            if clip is None or clip.size == 0:
                return False
            self._radio_tx_rings[radio_slot].clear()
            self._voice_play_buf = clip
            self._voice_play_pos = 0
            self.voice_play_slot = mem_idx
            self.voice_play_radio = radio_slot
            self.voice_play_active = True
            return True

    def stop_voice_play(self) -> int:
        stopped_radio = 0
        with self._voice_lock:
            if self.voice_play_active:
                stopped_radio = self.voice_play_radio
            self.voice_play_active = False
            self.voice_play_slot = 0
            self.voice_play_radio = 0
            self._voice_play_buf = None
            self._voice_play_pos = 0
        if stopped_radio in (1, 2):
            self._radio_tx_rings[stopped_radio].clear()
        return stopped_radio

    # --- Full QSO Recorder (RX + TX -> 48 kHz mono WAV in recordings/) ---

    def _enqueue_qso_bytes(self, pcm_bytes: bytes, frames: int) -> None:
        q = self._qso_queue
        if q is None or not self.qso_rec_active:
            return
        try:
            q.put_nowait(pcm_bytes)
            self._qso_samples += frames
        except queue.Full:
            pass

    @staticmethod
    def _qso_writer_worker(q: queue.Queue, path: Path) -> None:
        try:
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                while True:
                    chunk = q.get()
                    if chunk is None:
                        break
                    wf.writeframes(chunk)
        except Exception as exc:
            log.warning("QSO recorder write error (%s): %s", path, exc)

    def start_qso_rec(self) -> str:
        with self._qso_lock:
            if self.qso_rec_active:
                return self._qso_filename
            fname = f"QSO_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
            out_path = self._qso_dir / fname
            self._qso_dir.mkdir(parents=True, exist_ok=True)
            self._qso_tx_ring.clear()
            self._qso_samples = 0
            self._qso_filename = fname
            q: queue.Queue = queue.Queue(maxsize=256)
            self._qso_queue = q
            t = threading.Thread(target=self._qso_writer_worker, args=(q, out_path), daemon=True)
            self._qso_writer_thread = t
            self.qso_rec_active = True
            t.start()
            log.info("Started QSO audio recording: %s", out_path)
            return fname

    def stop_qso_rec(self) -> str:
        with self._qso_lock:
            if not self.qso_rec_active:
                return ""
            self.qso_rec_active = False
            q = self._qso_queue
            t = self._qso_writer_thread
            saved = self._qso_filename
            self._qso_last_saved = saved
            self._qso_queue = None
            self._qso_writer_thread = None
            self._qso_filename = ""
            self._qso_samples = 0
            self._qso_tx_ring.clear()
        if q is not None:
            try:
                q.put_nowait(None)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(None)
                except Exception:
                    pass
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        if saved:
            log.info("Stopped QSO audio recording: %s", saved)
        return saved

    def get_qso_rec_status(self) -> Dict[str, Any]:
        return {
            "active": self.qso_rec_active,
            "sec": round(float(self._qso_samples) / SAMPLE_RATE, 1) if self.qso_rec_active else 0.0,
            "filename": self._qso_filename,
            "last_saved": self._qso_last_saved,
        }
