"""Asynchronous Icom CI-V serial driver with USB auto-detection, Dual-VFO/Dual-RX & 0x27 scope streaming."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import serial

import usb_discovery

log = logging.getLogger("radio_civ")

MODE_MAP = {
    0x00: "LSB",
    0x01: "USB",
    0x02: "AM",
    0x03: "CW",
    0x04: "RTTY",
    0x05: "FM",
    0x06: "WFM",
    0x07: "CW-R",
    0x08: "RTTY-R",
    0x17: "DV",
}

MODE_REV = {
    "LSB": (0x00, 0x00),
    "USB": (0x01, 0x00),
    "AM": (0x02, 0x00),
    "CW": (0x03, 0x00),
    "RTTY": (0x04, 0x00),
    "FM": (0x05, 0x00),
    "WFM": (0x06, 0x00),
    "CW-R": (0x07, 0x00),
    "CW-U": (0x03, 0x00),
    "CW-L": (0x07, 0x00),
    "DV": (0x17, 0x00),
    "DIGI": (0x01, 0x01),   # USB-D1
    "DATA-U": (0x01, 0x01),
    "DATA-L": (0x00, 0x01),
    "USB-D": (0x01, 0x01),
    "LSB-D": (0x00, 0x01),
    "FM-D": (0x05, 0x01),
}

FILTER_HZ = {1: 3000, 2: 2400, 3: 1800}

# Model prefix -> standard Icom default CI-V address (for targeted probing when CI-V Transceive = OFF)
ICOM_DEFAULT_CIV_ADDRS: Tuple[Tuple[str, int], ...] = (
    ("IC-7300", 0x94),
    ("IC-9700", 0xA2),
    ("IC-7610", 0x98),
    ("IC-705", 0xA4),
    ("IC-7100", 0x88),
    ("IC-7600", 0x7A),
    ("IC-9100", 0x7C),
    ("IC-785", 0x8E),
    ("IC-7700", 0x74),
    ("IC-7760", 0xB2),
    ("IC-905", 0xAC),
    ("IC-7410", 0x80),
    ("IC-7200", 0x76),
    ("IC-7000", 0x70),
    ("IC-R8600", 0x96),
    ("IC-7800", 0x6A),
    ("IC-756PRO3", 0x6E),
    ("IC-756PRO2", 0x64),
    ("IC-756PRO", 0x5C),
    ("IC-746PRO", 0x66),
    ("IC-7400", 0x66),
    ("IC-718", 0x5E),
    ("IC-706MK2G", 0x58),
)

MORSE_UNITS: Dict[str, int] = {
    "A": 5, "B": 9, "C": 11, "D": 7, "E": 1, "F": 9, "G": 9, "H": 7, "I": 3,
    "J": 13, "K": 9, "L": 9, "M": 7, "N": 5, "O": 11, "P": 11, "Q": 13, "R": 7,
    "S": 5, "T": 3, "U": 7, "V": 9, "W": 9, "X": 11, "Y": 13, "Z": 11,
    "0": 19, "1": 17, "2": 15, "3": 13, "4": 11, "5": 9, "6": 11, "7": 13, "8": 15, "9": 17,
    "/": 13, "?": 15, ".": 17, "-": 15, ",": 17, ":": 17, "'": 17, "(": 15,
    ")": 19, "=": 11, "+": 11, "\"": 15, "@": 17,
}
_CW_ALLOWED_CHARS = set(MORSE_UNITS.keys()) | {" ", "^"}


def _cw_duration_seconds(text: str, wpm: int) -> float:
    """Compute International Morse duration (seconds) using standard PARIS 1.2/WPM unit."""
    units = 0
    prev_was_char = False
    for ch in text:
        if ch == " ":
            units += 4 if prev_was_char else 7
            prev_was_char = False
        elif ch == "^":
            prev_was_char = False
        elif ch in MORSE_UNITS:
            if prev_was_char:
                units += 3
            units += MORSE_UNITS[ch]
            prev_was_char = True
    return (max(1, units) * 1.2) / max(6, min(48, int(wpm or 20)))


def bcd_to_int(b: int) -> int:
    return ((b >> 4) & 0x0F) * 10 + (b & 0x0F)


def int_to_bcd(v: int) -> int:
    v = max(0, min(99, int(v)))
    return ((v // 10) << 4) | (v % 10)


def decode_bcd_freq(data: bytes) -> int:
    """Decode 5-byte LSB-first Icom BCD frequency to Hz."""
    if len(data) < 5:
        return 0
    hz = 0
    mult = 1
    for b in data[:5]:
        hz += bcd_to_int(b) * mult
        mult *= 100
    return hz


def encode_bcd_freq(hz: int) -> bytes:
    """Encode integer Hz into 5-byte LSB-first Icom BCD frequency."""
    val = max(0, int(round(hz)))
    out = bytearray(5)
    for i in range(5):
        pair = val % 100
        val //= 100
        out[i] = int_to_bcd(pair)
    return bytes(out)


def decode_bcd_level(data: bytes) -> int:
    """Decode 2-byte big-endian BCD level (0000..0255) to integer 0..255."""
    if len(data) < 2:
        return 0
    return bcd_to_int(data[0]) * 100 + bcd_to_int(data[1])


def encode_bcd_level(val_255: int) -> bytes:
    """Encode integer 0..255 to 2-byte big-endian BCD."""
    v = max(0, min(255, int(round(val_255))))
    return bytes([int_to_bcd(v // 100), int_to_bcd(v % 100)])


def encode_offset_bcd(offset_hz: int) -> bytes:
    """Encode repeater shift offset in Hz into 3-byte little-endian BCD (100 Hz resolution)."""
    steps = max(0, min(999999, abs(int(round(offset_hz))) // 100))
    return bytes([
        int_to_bcd(steps % 100),
        int_to_bcd((steps // 100) % 100),
        int_to_bcd((steps // 10000) % 100),
    ])


def encode_ctcss_bcd(freq_hz: float) -> bytes:
    """Encode CTCSS / tone frequency in Hz into 3-byte big-endian BCD (0.1 Hz resolution)."""
    deci = max(0, min(999999, int(round(float(freq_hz) * 10.0))))
    return bytes([
        int_to_bcd((deci // 10000) % 100),
        int_to_bcd((deci // 100) % 100),
        int_to_bcd(deci % 100),
    ])


def band_of_hz(freq_hz: int) -> str:
    """Return canonical amateur band tag for dual-RX band collision checks."""
    f = int(freq_hz or 0)
    if 28_000_000 <= f <= 30_000_000:
        return "10m"
    if 50_000_000 <= f <= 54_000_000:
        return "6m"
    if 144_000_000 <= f <= 148_000_000:
        return "2m"
    if 430_000_000 <= f <= 440_000_000:
        return "70cm"
    if 1_240_000_000 <= f <= 1_300_000_000:
        return "23cm"
    return "other"


class IcomCivRadio:
    """Manages a single physical Icom transceiver over USB CI-V serial."""

    def __init__(
        self,
        slot: int,
        on_state_change: Optional[Callable[[int], None]] = None,
        on_scope_frame: Optional[Callable[[int, int, List[float], int, int], None]] = None,
    ) -> None:
        self.slot = slot
        self.on_state_change = on_state_change
        self.on_scope_frame = on_scope_frame

        self.com_port: str = ""
        self.baud_rate: int = 115200
        self.civ_addr: int = 0x00

        self._serial: Optional[serial.Serial] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._io_lock = asyncio.Lock()
        self._last_civ_tx_ts: float = 0.0
        self._pending_ack: Optional[asyncio.Future] = None
        self._rx_buf = bytearray()
        self._cw_task: Optional[asyncio.Task] = None
        self._cw_abort_ev = asyncio.Event()
        self._cw_release_ptt_on_abort: bool = True
        self._cw_live_queue: List[str] = []
        self._swr_task: Optional[asyncio.Task] = None
        self._swr_abort_ev = asyncio.Event()
        self._suspend_poll: int = 0

        # Scope multi-division assembly per receiver (0=Main, 1=Sub)
        self._scope_asm: Dict[int, Dict[str, Any]] = {
            0: {"divs": {}, "tot": 0, "center": 0, "span": 100000, "mode": 0},
            1: {"divs": {}, "tot": 0, "center": 0, "span": 100000, "mode": 0},
        }

        self.state: Dict[str, Any] = {
            "slot": slot,
            "connected": False,
            "error": "",
            "com_port": "",
            "baud_rate": 115200,
            "civ_addr": 0,
            "usb_model": "",
            "usb_serial": "",
            "audio_in_device": "",
            "audio_out_device": "",
            "supports_dual_rx": False,
            "supports_dualwatch": False,
            "supports_tuner": False,
            "supports_pbt": False,
            "supports_29_prefix": False,
            "supports_vfo_25": False,
            "tuner_on": False,
            "tuning": False,
            "dualwatch": False,
            "active_band": "MAIN",  # "MAIN" or "SUB" (or VFO A / B)
            "main": {
                "freq_hz": 0,
                "mode": "---",
                "data_mode": False,
                "filter": 1,
                "filter_hz": 2400,
                "s_raw": 0,
                "s_units": 0.0,
                "dbm": -127,
            },
            "sub": {
                "freq_hz": 0,
                "mode": "---",
                "data_mode": False,
                "filter": 1,
                "filter_hz": 2400,
                "s_raw": 0,
                "s_units": 0.0,
                "dbm": -127,
            },
            "tx": False,
            "split": False,
            "rit_on": False,
            "rit_hz": 0,
            "af_gain": 50,
            "rf_gain": 100,
            "sql": 15,
            "has_hw_sql": False,
            "sql_hw_open": True,
            "tx_power": 50,
            "preamp": 0,
            "att": 0,
            "agc": 2,
            "nb": False,
            "nb_level": 50,
            "nr": False,
            "nr_level": 50,
            "notch": False,
            "notch_pos": 50,
            "anf": False,
            "pbt1": 50,
            "pbt2": 50,
            "mic_gain": 50,
            "comp_on": False,
            "comp_level": 50,
            "mon_on": False,
            "mon_level": 50,
            "vox_on": False,
            "bkin": 0,
            "cw_wpm": 20,
            "cw_tx_slot": 0,
            "cw_sidetone_seq": 0,
            "cw_sidetone_text": "",
            "tone_on": False,
            "tone_mode": 0,
            "tsql_on": False,
            "active_repeater": {"main": None, "sub": None},
            "swr": 1.0,
            "pwr_meter": 0,
            "alc": 0,
            "comp_db": 0.0,
            "vd_volts": 0.0,
            "id_amps": 0.0,
            "scope_center_hz": 0,
            "scope_span_hz": 100000,
            "scope_mode": 0,
            "last_scope_ts": 0.0,
            "swr_scan": {
                "active": False,
                "progress": 0,
                "total": 0,
                "center_hz": 0,
                "span_hz": 0,
                "points": [],
                "error": "",
            },
        }

    def _notify_state(self) -> None:
        if self.on_state_change:
            self.on_state_change(self.slot)

    async def start(self, com_port: str, baud_rate: int, usb_meta: Optional[Dict[str, str]] = None) -> None:
        await self.stop()
        self.com_port = com_port
        self.baud_rate = int(baud_rate)
        self.state["com_port"] = com_port
        self.state["baud_rate"] = self.baud_rate
        self.state["error"] = ""
        if usb_meta:
            self.state["usb_model"] = usb_meta.get("usb_model", "")
            self.state["usb_serial"] = usb_meta.get("usb_serial", "")
        else:
            # Discover directly from USB bus for this COM port
            for p in usb_discovery.list_serial_ports_with_usb():
                if p["port"].upper() == com_port.upper():
                    self.state["usb_model"] = p.get("usb_model", "")
                    self.state["usb_serial"] = p.get("usb_serial", "")
                    break

        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        await self.abort_cw_macro(release_ptt=True)
        await self.abort_swr_scan()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close_serial()
        self.state["connected"] = False
        self.state["tx"] = False
        self._notify_state()

    async def _close_serial(self) -> None:
        if self._serial is not None:
            try:
                if self._serial.is_open and self.civ_addr:
                    # Turn off scope wave output cleanly on disconnect
                    self._serial.write(bytes([0xFE, 0xFE, self.civ_addr, 0xE0, 0x27, 0x11, 0x00, 0xFD]))
            except Exception:
                pass
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                if not self.com_port:
                    self.state["connected"] = False
                    self.state["error"] = "No COM port selected"
                    self._notify_state()
                    await asyncio.sleep(2.0)
                    continue

                # Refresh USB bus model & driver metadata before opening serial port
                port_meta = None
                for p in usb_discovery.list_serial_ports_with_usb():
                    if p["port"].upper() == self.com_port.upper():
                        port_meta = p
                        self.state["usb_model"] = p.get("usb_model", "")
                        self.state["usb_serial"] = p.get("usb_serial", "")
                        break

                vidpid = str((port_meta or {}).get("usb_vidpid") or "").upper()
                usb_model_up = str(self.state.get("usb_model") or "").upper()
                is_usb_rig = (
                    "VID_10C4" in vidpid
                    or "VID_0403" in vidpid
                    or "VID_0C26" in vidpid
                    or "IC-" in usb_model_up
                )

                # Open serial port in worker thread (keep DTR/RTS deasserted on native USB rigs so USB SEND/Keying isn't keyed)
                def _open_serial() -> serial.Serial:
                    ser = serial.Serial()
                    ser.port = self.com_port
                    ser.baudrate = self.baud_rate
                    ser.timeout = 0.02
                    ser.write_timeout = 0.5
                    if is_usb_rig:
                        ser.dtr = False
                        ser.rts = False
                    ser.open()
                    return ser

                self._serial = await asyncio.to_thread(_open_serial)
                self._rx_buf.clear()

                # Discover CI-V address via broadcast 0x19 0x00
                addr = await self._probe_civ_address()
                if addr == 0:
                    raise RuntimeError(f"Radio not responding to CI-V probe ({self.com_port})")

                self.civ_addr = addr
                self.state["civ_addr"] = addr
                self.state["connected"] = True
                self.state["error"] = ""

                # Probe capabilities (Dual-RX, Dualwatch) & initial full state sync
                await self._probe_capabilities()
                await self._poll_full_state()

                # Enable Spectrum Scope + Wave Data Output (0x27 0x10 0x01, 0x27 0x11 0x01)
                await self._send_cmd([0x27, 0x10, 0x01], wait_reply=False)
                await asyncio.sleep(0.05)
                await self._send_cmd([0x27, 0x11, 0x01], wait_reply=False)
                self._notify_state()

                # Round-robin single CI-V command per tick (prevents half-duplex collisions with 0x27 scope stream)
                if self.state["supports_dual_rx"]:
                    poll_cmds = [
                        [0x15, 0x02],  # Active band S-meter
                        [0x15, 0x05],  # Radio squelch open/closed
                        [0x03],        # Active band freq
                        [0x15, 0x02],
                        [0x04],        # Active band mode
                        [0x15, 0x05],
                        [0x15, 0x02],
                        [0x07, 0xD2],  # Main/Sub active check
                        [0x16, 0x59],  # Dualwatch status
                        [0x15, 0x05],
                        [0x15, 0x02],
                        [0x1C, 0x00],  # PTT
                        [0x14, 0x01],  # AF
                        [0x14, 0x02],  # RF
                        [0x14, 0x03],  # SQL
                        [0x15, 0x05],
                        [0x14, 0x0A],  # TX Power
                        [0x1C, 0x00],  # PTT
                        [0x0F],        # Split
                        [0x16, 0x22],  # NB
                        [0x16, 0x40],  # NR
                        [0x16, 0x48],  # Notch
                        [0x15, 0x05],
                        [0x16, 0x41],  # ANF
                        [0x16, 0x44],  # COMP
                        [0x16, 0x45],  # MON
                        [0x16, 0x46],  # VOX
                        [0x16, 0x47],  # BK-IN
                        [0x16, 0x42],  # TONE
                        [0x16, 0x43],  # TSQL
                        [0x15, 0x02],
                        [0x15, 0x05],
                        [0x14, 0x06],  # NR Level
                        [0x14, 0x12],  # NB Level
                        [0x14, 0x0B],  # MIC Gain
                        [0x14, 0x0E],  # COMP Level
                        [0x14, 0x15],  # MON Level
                        [0x14, 0x0C],  # CW WPM
                        [0x14, 0x0D],  # Notch Pos
                        [0x15, 0x15],  # VDD Voltage
                    ]
                else:
                    if self.state.get("supports_vfo_25", True):
                        freq_mode_cmds = [
                            [0x25, 0x00],  # Selected VFO freq
                            [0x15, 0x02],
                            [0x25, 0x01],  # Unselected VFO freq
                            [0x15, 0x05],
                            [0x15, 0x02],
                            [0x26, 0x00],  # Selected mode
                            [0x15, 0x02],
                            [0x26, 0x01],  # Unselected mode
                        ]
                    else:
                        freq_mode_cmds = [
                            [0x03],        # Universal operating frequency
                            [0x15, 0x02],
                            [0x04],        # Universal operating mode
                            [0x15, 0x05],
                            [0x15, 0x02],
                            [0x1A, 0x06],  # DATA mode status
                        ]
                    poll_cmds = [
                        [0x15, 0x02],  # S-meter
                        [0x15, 0x05],  # Radio squelch open/closed
                        *freq_mode_cmds,
                        [0x15, 0x05],
                        [0x15, 0x02],
                        [0x1C, 0x00],  # PTT
                        [0x14, 0x01],  # AF
                        [0x14, 0x02],  # RF
                        [0x14, 0x03],  # SQL
                        [0x15, 0x05],
                        [0x14, 0x0A],  # TX Power
                        [0x1C, 0x00],  # PTT
                        [0x0F],        # Split
                        [0x16, 0x22],  # NB
                        [0x16, 0x40],  # NR
                        [0x16, 0x48],  # Notch
                        [0x15, 0x05],
                        [0x16, 0x41],  # ANF
                        [0x16, 0x44],  # COMP
                        [0x16, 0x45],  # MON
                        [0x16, 0x46],  # VOX
                        [0x16, 0x47],  # BK-IN
                        [0x16, 0x42],  # TONE
                        [0x16, 0x43],  # TSQL
                        [0x15, 0x05],
                        [0x14, 0x06],  # NR Level
                        [0x14, 0x12],  # NB Level
                        [0x14, 0x0B],  # MIC Gain
                        [0x14, 0x0E],  # COMP Level
                        [0x14, 0x15],  # MON Level
                        [0x14, 0x0C],  # CW WPM
                        [0x14, 0x0D],  # Notch Pos
                        [0x15, 0x15],  # VDD Voltage
                    ]
                if self.state["supports_tuner"]:
                    poll_cmds.append([0x1C, 0x01])  # Tuner status
                if self.state["supports_pbt"]:
                    poll_cmds.extend([[0x14, 0x07], [0x14, 0x08]])

                poll_idx = 0
                while self._running and self._serial and self._serial.is_open:
                    if self._suspend_poll > 0 or self.state.get("swr_scan", {}).get("active"):
                        await asyncio.sleep(0.05)
                        continue
                    await self._read_available()

                    if self.state["tx"]:
                        tx_cycle = [[0x15, 0x12], [0x15, 0x11], [0x15, 0x13], [0x15, 0x12], [0x15, 0x14], [0x15, 0x16], [0x15, 0x15], [0x1C, 0x00]]
                        if self.state["supports_tuner"]:
                            tx_cycle.append([0x1C, 0x01])
                        await self._send_cmd(tx_cycle[poll_idx % len(tx_cycle)])
                    else:
                        await self._send_cmd(poll_cmds[poll_idx % len(poll_cmds)])

                    poll_idx += 1
                    await asyncio.sleep(0.045)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("Radio slot %s error (%s): %s", self.slot, self.com_port, exc)
                self.state["connected"] = False
                self.state["error"] = str(exc)
                self._notify_state()
                await self._close_serial()
                await asyncio.sleep(2.5)

    def _build_probe_candidates(self) -> List[int]:
        """Build ordered CI-V probe addresses: broadcast 0x00, USB model default, known defaults, then remaining 0x01..0xDF."""
        candidates: List[int] = [0x00]
        model_up = str(self.state.get("usb_model") or "").upper()
        for prefix, addr in ICOM_DEFAULT_CIV_ADDRS:
            if prefix in model_up and addr not in candidates:
                candidates.append(addr)
        for _, addr in ICOM_DEFAULT_CIV_ADDRS:
            if addr not in candidates:
                candidates.append(addr)
        return candidates

    async def _probe_single_target(self, target_addr: int, payload: List[int], timeout: float) -> int:
        """Send one CI-V probe with 4x 0xFE preambles (helps Auto-baud lock) and return responder CI-V address or 0."""
        if not self._serial:
            return 0
        # 4 preambles match Icom's Auto-baud wake recommendation at 115200 baud
        frame = bytes([0xFE, 0xFE, 0xFE, 0xFE, target_addr, 0xE0]) + bytes(payload) + bytes([0xFD])
        await asyncio.to_thread(self._serial.reset_input_buffer)
        self._rx_buf.clear()
        await asyncio.to_thread(self._serial.write, frame)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = await asyncio.to_thread(self._serial.read, 256)
            if chunk:
                self._rx_buf.extend(chunk)
                for frm in self._extract_frames():
                    if (
                        len(frm) >= 6
                        and frm[2] in (0xE0, 0x00)
                        and frm[3] not in (0x00, 0xE0)
                        and frm[4] in (0x19, 0x03, 0xFA, 0xFB)
                    ):
                        return frm[3]
            await asyncio.sleep(0.008)
        return 0

    async def _probe_civ_address(self) -> int:
        if not self._serial:
            return 0
        # 1. Try broadcast 0x00 (fast path when CI-V Transceive = ON)
        for _ in range(2):
            addr = await self._probe_single_target(0x00, [0x19, 0x00], timeout=0.14)
            if addr:
                return addr

        # 2. Try targeted USB-matched + known Icom default addresses (handles CI-V Transceive = OFF)
        priority = [a for a in self._build_probe_candidates() if a != 0x00]
        for cand in priority:
            addr = await self._probe_single_target(cand, [0x19, 0x00], timeout=0.05)
            if addr:
                return addr
            # Older Icom rigs without 0x19 support still respond to 0x03 Read Frequency
            addr = await self._probe_single_target(cand, [0x03], timeout=0.05)
            if addr:
                return addr

        # 3. Fallback sweep across remaining 0x01..0xDF addresses in case a custom CI-V address is set
        tested = set(priority) | {0x00, 0xE0}
        for cand in range(0x01, 0xE0):
            if cand in tested:
                continue
            addr = await self._probe_single_target(cand, [0x03], timeout=0.025)
            if addr:
                return addr
        return 0

    async def _probe_capabilities(self) -> None:
        # Check Dual-RX (Main/Sub band read 0x07 0xD2)
        resp_d2 = await self._send_and_expect([0x07, 0xD2], timeout=0.25)
        self.state["supports_dual_rx"] = bool(resp_d2 and resp_d2[4] != 0xFA)
        if self.state["supports_dual_rx"] and len(resp_d2) >= 7:
            self.state["active_band"] = "SUB" if resp_d2[6] == 0x01 else "MAIN"

        # Check Dualwatch (0x16 0x59)
        resp_dw = await self._send_and_expect([0x16, 0x59], timeout=0.25)
        self.state["supports_dualwatch"] = bool(resp_dw and resp_dw[4] != 0xFA)
        if self.state["supports_dualwatch"] and len(resp_dw) >= 7:
            self.state["dualwatch"] = resp_dw[6] == 0x01

        # Check Antenna Tuner capability (0x1C 0x01)
        resp_tun = await self._send_and_expect([0x1C, 0x01], timeout=0.25)
        self.state["supports_tuner"] = bool(resp_tun and resp_tun[4] != 0xFA)
        if self.state["supports_tuner"] and len(resp_tun) >= 7:
            self.state["tuner_on"] = resp_tun[6] != 0x00
            self.state["tuning"] = resp_tun[6] == 0x02
        else:
            self.state["tuner_on"] = False
            self.state["tuning"] = False

        # Check IF PBT support (0x14 0x07)
        resp_pbt = await self._send_and_expect([0x14, 0x07], timeout=0.25)
        self.state["supports_pbt"] = bool(resp_pbt and resp_pbt[4] != 0xFA)

        # Check 0x29 Main/Sub direct command prefix support, or 0x25/0x26 VFO read/set support (IC-7300 >= v1.40)
        if self.state["supports_dual_rx"]:
            resp_29 = await self._send_and_expect([0x29, 0x00, 0x03], timeout=0.2)
            self.state["supports_29_prefix"] = bool(resp_29 and resp_29[4] != 0xFA)
            self.state["supports_vfo_25"] = False
        else:
            self.state["supports_29_prefix"] = False
            resp_25 = await self._send_and_expect([0x25, 0x00], timeout=0.25)
            self.state["supports_vfo_25"] = bool(resp_25 and resp_25[4] == 0x25)
            log.info("Slot %s CI-V probe: supports_vfo_25=%s", self.slot, self.state["supports_vfo_25"])

    async def _poll_dual_rx_bands(self) -> None:
        """Query both MAIN and SUB bands explicitly on Dual-RX radios (e.g. IC-9700) without losing active band."""
        orig_band = self.state.get("active_band", "MAIN")
        self._suspend_poll += 1
        try:
            # Query MAIN
            self.state["active_band"] = "MAIN"
            await self._send_and_expect([0x07, 0xD0], timeout=0.2)
            await self._send_and_expect([0x03], timeout=0.2)
            await self._send_and_expect([0x04], timeout=0.2)
            await self._send_and_expect([0x15, 0x02], timeout=0.2)

            # Query SUB
            self.state["active_band"] = "SUB"
            await self._send_and_expect([0x07, 0xD1], timeout=0.2)
            await self._send_and_expect([0x03], timeout=0.2)
            await self._send_and_expect([0x04], timeout=0.2)
            await self._send_and_expect([0x15, 0x02], timeout=0.2)
        finally:
            # Restore original band cleanly
            self.state["active_band"] = orig_band
            await self._send_and_expect([0x07, 0xD1 if orig_band == "SUB" else 0xD0], timeout=0.2)
            self._suspend_poll = max(0, self._suspend_poll - 1)

    async def _poll_full_state(self) -> None:
        if self.state["supports_dual_rx"]:
            await self._poll_dual_rx_bands()
            cmds = [
                [0x14, 0x01], [0x14, 0x02], [0x14, 0x03], [0x14, 0x0A],
                [0x15, 0x05], [0x16, 0x02], [0x11], [0x16, 0x12],
                [0x16, 0x22], [0x16, 0x40], [0x16, 0x48],
                [0x16, 0x41], [0x16, 0x42], [0x16, 0x43], [0x16, 0x44],
                [0x16, 0x45], [0x16, 0x46], [0x16, 0x47],
                [0x14, 0x06], [0x14, 0x0B], [0x14, 0x0C], [0x14, 0x0D],
                [0x14, 0x0E], [0x14, 0x12], [0x14, 0x15], [0x15, 0x15],
                [0x0F], [0x1C, 0x00], [0x21, 0x00], [0x21, 0x01],
            ]
        else:
            if not self.state.get("supports_vfo_25", True):
                # Universal fallback (e.g. IC-7300 firmware < v1.40): sync active VFO A & VFO B cleanly once
                self.state["active_band"] = "SUB"
                await self._send_confirmed([0x07, 0x01])
                for c in ([0x03], [0x04], [0x1A, 0x06]):
                    await self._send_and_expect(c, timeout=0.2)
                self.state["active_band"] = "MAIN"
                await self._send_confirmed([0x07, 0x00])
                for c in ([0x03], [0x04], [0x1A, 0x06]):
                    await self._send_and_expect(c, timeout=0.2)
                cmds = [
                    [0x15, 0x02],
                    [0x14, 0x01], [0x14, 0x02], [0x14, 0x03], [0x14, 0x0A],
                    [0x15, 0x05], [0x16, 0x02], [0x11], [0x16, 0x12],
                    [0x16, 0x22], [0x16, 0x40], [0x16, 0x48],
                    [0x16, 0x41], [0x16, 0x42], [0x16, 0x43], [0x16, 0x44],
                    [0x16, 0x45], [0x16, 0x46], [0x16, 0x47],
                    [0x14, 0x06], [0x14, 0x0B], [0x14, 0x0C], [0x14, 0x0D],
                    [0x14, 0x0E], [0x14, 0x12], [0x14, 0x15], [0x15, 0x15],
                    [0x0F], [0x1C, 0x00], [0x21, 0x00], [0x21, 0x01],
                ]
            else:
                cmds = [
                    [0x25, 0x00], [0x25, 0x01],
                    [0x26, 0x00], [0x26, 0x01],
                    [0x15, 0x02],
                    [0x14, 0x01], [0x14, 0x02], [0x14, 0x03], [0x14, 0x0A],
                    [0x15, 0x05], [0x16, 0x02], [0x11], [0x16, 0x12],
                    [0x16, 0x22], [0x16, 0x40], [0x16, 0x48],
                    [0x16, 0x41], [0x16, 0x42], [0x16, 0x43], [0x16, 0x44],
                    [0x16, 0x45], [0x16, 0x46], [0x16, 0x47],
                    [0x14, 0x06], [0x14, 0x0B], [0x14, 0x0C], [0x14, 0x0D],
                    [0x14, 0x0E], [0x14, 0x12], [0x14, 0x15], [0x15, 0x15],
                    [0x0F], [0x1C, 0x00], [0x21, 0x00], [0x21, 0x01],
                ]
        if self.state["supports_pbt"]:
            cmds.extend([[0x14, 0x07], [0x14, 0x08]])
        if self.state["supports_tuner"]:
            cmds.append([0x1C, 0x01])
        for c in cmds:
            await self._send_cmd(c, wait_reply=False)
            await asyncio.sleep(0.035)
            await self._read_available()

    async def _read_available_locked(self) -> None:
        if not self._serial or not self._serial.is_open:
            return
        for _ in range(8):
            waiting = self._serial.in_waiting
            if waiting <= 0:
                break
            chunk = await asyncio.to_thread(self._serial.read, min(waiting, 4096))
            if not chunk:
                break
            self._rx_buf.extend(chunk)
        changed = False
        for frm in self._extract_frames():
            if len(frm) >= 6 and frm[3] != 0xE0:
                if self._handle_frame(frm):
                    changed = True
        if changed:
            self._notify_state()

    async def _read_available(self) -> None:
        async with self._io_lock:
            await self._read_available_locked()

    async def _send_cmd(self, payload: List[int], wait_reply: bool = False) -> None:
        if not self._serial or not self._serial.is_open:
            return
        addr = self.civ_addr or 0x00
        frame = bytes([0xFE, 0xFE, addr, 0xE0]) + bytes(payload) + bytes([0xFD])
        async with self._io_lock:
            since = time.monotonic() - self._last_civ_tx_ts
            if since < 0.015:
                await asyncio.sleep(0.015 - since)
            await self._read_available_locked()
            await asyncio.to_thread(self._serial.write, frame)
            self._last_civ_tx_ts = time.monotonic()

    async def _send_confirmed(self, payload: List[int], retries: int = 3) -> bool:
        """Send a critical CI-V control command and wait for 0xFB ACK with collision retry."""
        if not self._serial or not self._serial.is_open:
            return False
        addr = self.civ_addr or 0x00
        frame = bytes([0xFE, 0xFE, addr, 0xE0]) + bytes(payload) + bytes([0xFD])
        for attempt in range(retries):
            async with self._io_lock:
                since = time.monotonic() - self._last_civ_tx_ts
                if since < 0.015:
                    await asyncio.sleep(0.015 - since)
                await self._read_available_locked()
                await asyncio.to_thread(self._serial.write, frame)
                self._last_civ_tx_ts = time.monotonic()
                deadline = time.monotonic() + 0.14
                got_ng = False
                while time.monotonic() < deadline:
                    waiting = self._serial.in_waiting
                    if waiting > 0:
                        chunk = await asyncio.to_thread(self._serial.read, min(waiting, 2048))
                        if chunk:
                            self._rx_buf.extend(chunk)
                            for frm in self._extract_frames():
                                if len(frm) >= 6 and frm[3] != 0xE0:
                                    if frm[4] == 0xFB:
                                        return True
                                    if frm[4] == 0xFA:
                                        got_ng = True
                                        break
                                    if self._handle_frame(frm):
                                        self._notify_state()
                            if got_ng:
                                break
                    await asyncio.sleep(0.004)
            if attempt < retries - 1:
                await asyncio.sleep(0.018)
        return False

    async def _send_and_expect(self, payload: List[int], timeout: float = 0.25) -> Optional[bytes]:
        if not self._serial or not self._serial.is_open:
            return None
        addr = self.civ_addr or 0x00
        frame = bytes([0xFE, 0xFE, addr, 0xE0]) + bytes(payload) + bytes([0xFD])
        cmd_byte = payload[0]
        async with self._io_lock:
            since = time.monotonic() - self._last_civ_tx_ts
            if since < 0.015:
                await asyncio.sleep(0.015 - since)
            await self._read_available_locked()
            await asyncio.to_thread(self._serial.write, frame)
            self._last_civ_tx_ts = time.monotonic()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                waiting = self._serial.in_waiting
                if waiting > 0:
                    chunk = await asyncio.to_thread(self._serial.read, min(waiting, 2048))
                    if chunk:
                        self._rx_buf.extend(chunk)
                        for frm in self._extract_frames():
                            if len(frm) >= 6 and frm[3] != 0xE0:
                                self._handle_frame(frm)
                                if frm[4] in (cmd_byte, 0xFA, 0xFB):
                                    return frm
                await asyncio.sleep(0.006)
        return None

    async def raw_civ_exchange(self, raw_frame: bytes, retries: int = 3) -> Optional[bytes]:
        """Forward a binary Icom CI-V frame (e.g. N1MM Port=TCP) through _io_lock and return reply."""
        if not self._serial or not self._serial.is_open or len(raw_frame) < 6:
            return None
        orig_to = raw_frame[2]
        orig_from = raw_frame[3]
        req_cmd = raw_frame[4]
        target_addr = self.civ_addr or orig_to
        out_frame = bytes([0xFE, 0xFE, target_addr if orig_to != 0x00 else 0x00, orig_from]) + raw_frame[4:]

        for attempt in range(retries):
            async with self._io_lock:
                since = time.monotonic() - self._last_civ_tx_ts
                if since < 0.015:
                    await asyncio.sleep(0.015 - since)
                await self._read_available_locked()
                await asyncio.to_thread(self._serial.write, out_frame)
                self._last_civ_tx_ts = time.monotonic()
                deadline = time.monotonic() + 0.14
                while time.monotonic() < deadline:
                    waiting = self._serial.in_waiting
                    if waiting > 0:
                        chunk = await asyncio.to_thread(self._serial.read, min(waiting, 2048))
                        if chunk:
                            self._rx_buf.extend(chunk)
                            for frm in self._extract_frames():
                                if len(frm) >= 6 and frm[3] != orig_from:
                                    if frm[4] == 0x27:
                                        self._handle_frame(frm)
                                        continue
                                    if self._handle_frame(frm):
                                        self._notify_state()
                                    if frm[4] in (req_cmd, 0xFA, 0xFB):
                                        if req_cmd == 0x1C and len(raw_frame) >= 8 and raw_frame[5] == 0x00 and frm[4] == 0xFB:
                                            self.state["tx"] = raw_frame[6] == 0x01
                                            self._notify_state()
                                        if orig_to != 0x00 and orig_to != frm[3]:
                                            return bytes([0xFE, 0xFE, orig_from, orig_to]) + frm[4:]
                                        return frm
                    await asyncio.sleep(0.004)
            if attempt < retries - 1:
                await asyncio.sleep(0.018)
        return None

    def _extract_frames(self) -> List[bytes]:
        frames: List[bytes] = []
        while True:
            start = self._rx_buf.find(b"\xfe\xfe")
            if start < 0:
                self._rx_buf.clear()
                break
            if start > 0:
                del self._rx_buf[:start]
            while len(self._rx_buf) >= 3 and self._rx_buf[2] == 0xFE:
                del self._rx_buf[0]
            end = self._rx_buf.find(b"\xfd", 2)
            if end < 0:
                if len(self._rx_buf) > 4096:
                    self._rx_buf.clear()
                break
            frames.append(bytes(self._rx_buf[: end + 1]))
            del self._rx_buf[: end + 1]
        return frames

    def _s_raw_to_units(self, raw: int) -> Tuple[float, int]:
        """Convert Icom 0..255 S-meter reading to S-units (1..15, where 9=S9, 15=S9+60dB) and dBm."""
        if raw <= 120:
            s = 1.0 + (raw / 120.0) * 8.0
            dbm = int(round(-127 + s * 6))
        else:
            over_db = ((raw - 120) / 121.0) * 60.0
            s = 9.0 + over_db / 10.0
            dbm = int(round(-73 + over_db))
        return round(s, 1), dbm

    def _selected_target_key(self) -> str:
        return "sub" if self.state["active_band"] == "SUB" else "main"

    def _unselected_target_key(self) -> str:
        return "main" if self._selected_target_key() == "sub" else "sub"

    def _handle_frame(self, frm: bytes) -> bool:
        """Process a single CI-V frame from the radio; return True if state changed."""
        cmd = frm[4]
        payload = frm[5:-1]

        # 0x27 0x00 Spectrum Scope Waveform Data
        if cmd == 0x27 and len(payload) >= 4 and payload[0] == 0x00:
            self._handle_scope_packet(payload[1:])
            return False

        # 0x25 <00=selected, 01=unselected> <5B freq> (Single-RX VFO A/B only)
        if cmd == 0x25 and len(payload) >= 6:
            if self.state["supports_dual_rx"]:
                return False
            is_sel = payload[0] == 0x00
            freq = decode_bcd_freq(payload[1:6])
            key = self._selected_target_key() if is_sel else self._unselected_target_key()
            return self._apply_reported_freq(key, freq)

        # 0x00 / 0x03 Transceive / Read operating frequency
        if cmd in (0x00, 0x03) and len(payload) >= 5:
            freq = decode_bcd_freq(payload[:5])
            key = self._selected_target_key()
            return self._apply_reported_freq(key, freq)

        # 0x26 <00=sel, 01=unsel> <mode> [data] [filter] (Single-RX VFO A/B only)
        if cmd == 0x26 and len(payload) >= 2:
            if self.state["supports_dual_rx"]:
                return False
            is_sel = payload[0] == 0x00
            key = self._selected_target_key() if is_sel else self._unselected_target_key()
            m_byte = payload[1]
            d_byte = payload[2] if len(payload) >= 3 else 0
            f_byte = payload[3] if len(payload) >= 4 else 1
            mode_str = MODE_MAP.get(m_byte, f"0x{m_byte:02X}")
            if d_byte and mode_str in ("USB", "LSB", "FM", "AM"):
                mode_str = "DIGI" if mode_str == "USB" else f"{mode_str}-D"
            self.state[key]["mode"] = mode_str
            self.state[key]["data_mode"] = bool(d_byte)
            self.state[key]["filter"] = f_byte
            self.state[key]["filter_hz"] = FILTER_HZ.get(f_byte, 2400)
            return True

        # 0x01 / 0x04 Transceive or Read mode
        if cmd in (0x01, 0x04) and len(payload) >= 1:
            key = self._selected_target_key()
            base_mode = MODE_MAP.get(payload[0], self.state[key]["mode"])
            d_on = bool(self.state[key].get("data_mode", False))
            if d_on and base_mode in ("USB", "LSB", "FM", "AM"):
                mode_str = "DIGI" if base_mode == "USB" else f"{base_mode}-D"
            else:
                mode_str = base_mode
            self.state[key]["mode"] = mode_str
            if len(payload) >= 2:
                self.state[key]["filter"] = payload[1]
                self.state[key]["filter_hz"] = FILTER_HZ.get(payload[1], 2400)
            return True

        # 0x1A 0x06 DATA mode status
        if cmd == 0x1A and len(payload) >= 2 and payload[0] == 0x06:
            key = self._selected_target_key()
            d_on = payload[1] != 0x00
            self.state[key]["data_mode"] = d_on
            cur_m = str(self.state[key].get("mode", ""))
            base = "USB" if cur_m == "DIGI" else cur_m.replace("-D", "")
            if d_on and base in ("USB", "LSB", "FM", "AM"):
                self.state[key]["mode"] = "DIGI" if base == "USB" else f"{base}-D"
            elif not d_on and cur_m in ("DIGI", "USB-D", "LSB-D", "FM-D", "AM-D"):
                self.state[key]["mode"] = base or "USB"
            if len(payload) >= 3 and payload[2] in FILTER_HZ:
                self.state[key]["filter"] = payload[2]
                self.state[key]["filter_hz"] = FILTER_HZ[payload[2]]
            return True

        # 0x07 0xD2 Main/Sub band selection
        if cmd == 0x07 and len(payload) >= 2 and payload[0] == 0xD2:
            band = "SUB" if payload[1] == 0x01 else "MAIN"
            if self.state["active_band"] != band:
                self.state["active_band"] = band
                return True
            return False

        # 0x16 Subcommands
        if cmd == 0x16 and len(payload) >= 2:
            sub = payload[0]
            val = payload[1]
            if sub == 0x59:
                self.state["dualwatch"] = val == 0x01
                return True
            if sub == 0x02:
                self.state["preamp"] = val
                return True
            if sub == 0x12:
                self.state["agc"] = val
                return True
            if sub == 0x22:
                self.state["nb"] = val == 0x01
                return True
            if sub == 0x40:
                self.state["nr"] = val == 0x01
                return True
            if sub == 0x48:
                self.state["notch"] = val == 0x01
                return True
            if sub == 0x41:
                self.state["anf"] = val == 0x01
                return True
            if sub == 0x42:
                self.state["tone_mode"] = val
                self.state["tone_on"] = val != 0x00
                self.state["tsql_on"] = val == 0x02
                return True
            if sub == 0x43:
                self.state["tsql_on"] = val == 0x01
                return True
            if sub == 0x44:
                self.state["comp_on"] = val == 0x01
                return True
            if sub == 0x45:
                self.state["mon_on"] = val == 0x01
                return True
            if sub == 0x46:
                self.state["vox_on"] = val == 0x01
                return True
            if sub == 0x47:
                self.state["bkin"] = val
                return True

        # 0x11 Attenuator
        if cmd == 0x11 and len(payload) >= 1:
            self.state["att"] = bcd_to_int(payload[0])
            return True

        # 0x15 0x01 / 0x05 Squelch condition (0=closed, 1=open)
        if cmd == 0x15 and len(payload) >= 2 and payload[0] in (0x01, 0x05):
            if payload[0] == 0x05:
                self.state["has_hw_sql"] = True
                self.state["sql_hw_open"] = bool(payload[1])
            elif not self.state.get("has_hw_sql"):
                self.state["sql_hw_open"] = bool(payload[1])
            return True

        # 0x15 Meters
        if cmd == 0x15 and len(payload) >= 3:
            sub = payload[0]
            raw = decode_bcd_level(payload[1:3])
            if sub == 0x02:  # S-meter
                key = self._selected_target_key()
                s_u, dbm = self._s_raw_to_units(raw)
                self.state[key]["s_raw"] = raw
                self.state[key]["s_units"] = s_u
                self.state[key]["dbm"] = dbm
                return True
            if sub == 0x11:  # RF Power meter
                self.state["pwr_meter"] = round((raw / 255.0) * 100)
                return True
            if sub == 0x12:  # SWR
                if raw <= 48:
                    swr = 1.0 + (raw / 48.0) * 0.5
                elif raw <= 80:
                    swr = 1.5 + ((raw - 48) / 32.0) * 0.5
                elif raw <= 120:
                    swr = 2.0 + ((raw - 80) / 40.0) * 1.0
                else:
                    swr = 3.0 + ((raw - 120) / 135.0) * 3.0
                self.state["swr"] = round(swr, 2)
                return True
            if sub == 0x13:  # ALC
                self.state["alc"] = round((raw / 255.0) * 100)
                return True
            if sub == 0x14:  # COMP (dB)
                self.state["comp_db"] = round((raw / 241.0) * 30.0, 1)
                return True
            if sub == 0x15:  # VDD Voltage (13=10V, 241=16V)
                if raw <= 13:
                    v = (raw / 13.0) * 10.0
                else:
                    v = 10.0 + ((raw - 13) / 228.0) * 6.0
                self.state["vd_volts"] = round(v, 1)
                return True
            if sub == 0x16:  # PA IDD Current (0..25A)
                self.state["id_amps"] = round((raw / 241.0) * 25.0, 1)
                return True

        # 0x14 Levels
        if cmd == 0x14 and len(payload) >= 3:
            sub = payload[0]
            raw = decode_bcd_level(payload[1:3])
            pct = round((raw / 255.0) * 100)
            if sub == 0x01:
                self.state["af_gain"] = pct
            elif sub == 0x02:
                self.state["rf_gain"] = pct
            elif sub == 0x03:
                self.state["sql"] = pct
            elif sub == 0x06:
                self.state["nr_level"] = pct
            elif sub == 0x07:
                self.state["pbt1"] = pct
            elif sub == 0x08:
                self.state["pbt2"] = pct
            elif sub == 0x0A:
                self.state["tx_power"] = max(1, pct)
            elif sub == 0x0B:
                self.state["mic_gain"] = pct
            elif sub == 0x0C:
                self.state["cw_wpm"] = round(6 + (raw / 255.0) * 42)
            elif sub == 0x0D:
                self.state["notch_pos"] = pct
            elif sub == 0x0E:
                self.state["comp_level"] = pct
            elif sub == 0x12:
                self.state["nb_level"] = pct
            elif sub == 0x15:
                self.state["mon_level"] = pct
            return True

        # 0x0F Split
        if cmd == 0x0F and len(payload) >= 1:
            self.state["split"] = payload[0] == 0x01
            return True

        # 0x1C 0x00 PTT / 0x1C 0x01 Antenna Tuner
        if cmd == 0x1C and len(payload) >= 2:
            if payload[0] == 0x00:
                self.state["tx"] = payload[1] == 0x01
                return True
            if payload[0] == 0x01:
                self.state["tuner_on"] = payload[1] != 0x00
                self.state["tuning"] = payload[1] == 0x02
                return True

        # 0x21 RIT
        if cmd == 0x21 and len(payload) >= 2:
            if payload[0] == 0x01:
                self.state["rit_on"] = payload[1] == 0x01
                return True
            if payload[0] == 0x00 and len(payload) >= 4:
                # 2 bytes BCD (10Hz/1Hz, 1kHz/100Hz) + sign byte (00=+, 01=-)
                mag = bcd_to_int(payload[1]) + bcd_to_int(payload[2]) * 100
                sign = -1 if payload[3] == 0x01 else 1
                self.state["rit_hz"] = sign * mag
                return True

        return False

    def _handle_scope_packet(self, body: bytes) -> None:
        """Assemble multi-division 0x27 0x00 spectrum scope waveform packets."""
        if len(body) < 3:
            return
        rx = 1 if body[0] == 0x01 else 0
        div_cur = bcd_to_int(body[1])
        div_tot = bcd_to_int(body[2])
        asm = self._scope_asm[rx]

        if div_cur == 1:
            asm["divs"].clear()
            asm["tot"] = div_tot
            if len(body) >= 14:
                scope_mode = body[3]
                f1 = decode_bcd_freq(body[4:9])
                f2 = decode_bcd_freq(body[9:14])
                asm["mode"] = scope_mode
                if scope_mode in (0, 2):  # Center / Scroll-C: f1=center, f2=half-span
                    asm["center"] = f1
                    asm["span"] = max(5000, f2 * 2)
                else:  # Fixed / Scroll-F: f1=start, f2=end
                    asm["center"] = (f1 + f2) // 2
                    asm["span"] = max(5000, abs(f2 - f1))
                self.state["scope_center_hz"] = asm["center"]
                self.state["scope_span_hz"] = asm["span"]
                self.state["scope_mode"] = scope_mode
            if len(body) > 15:
                asm["divs"][1] = body[15:]
            return

        if div_cur >= 2:
            asm["divs"][div_cur] = body[3:]
            if asm["tot"] > 0 and div_cur >= asm["tot"]:
                pixels = bytearray()
                for d in range(1, asm["tot"] + 1):
                    chunk = asm["divs"].get(d)
                    if chunk:
                        pixels.extend(chunk)
                if pixels:
                    # Normalize Icom 0..160 amplitude scale into 0..1 floats
                    norm = [min(1.0, b / 160.0) for b in pixels]
                    self.state["last_scope_ts"] = time.time()
                    if self.on_scope_frame:
                        self.on_scope_frame(
                            self.slot,
                            rx,
                            norm,
                            asm["center"] or self.state["main"]["freq_hz"],
                            asm["span"],
                        )

    # --- Control Write Operations ---

    def _apply_reported_freq(self, key: str, freq: int) -> bool:
        """Apply polled/transceived frequency while protecting active repeater TX shift and resetting on QSY."""
        if freq <= 0:
            return False
        rep = self.state.get("active_repeater", {}).get(key)
        if rep:
            rx_target = int(rep.get("rx_hz") or 0)
            tx_target = int(rep.get("tx_hz") or rx_target)
            shift_hz = int(rep.get("shift_hz") or 0)
            in_tx = bool(self.state.get("tx"))
            near_rx = rx_target > 0 and abs(freq - rx_target) <= 1000
            near_tx = tx_target > 0 and abs(freq - tx_target) <= 1000
            if in_tx or (shift_hz != 0 and near_tx and not near_rx):
                # Do not reset repeater or overwrite UI RX frequency during repeater TX
                return False
            if not near_rx and not near_tx:
                # VFO moved > +-1 kHz away from both RX and TX -> auto-return to Simplex
                self.state["active_repeater"][key] = None
                self.state[key]["freq_hz"] = freq
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._reset_repeater_to_simplex(key))
                except RuntimeError:
                    pass
                return True
        if self.state[key]["freq_hz"] != freq:
            self.state[key]["freq_hz"] = freq
            return True
        return False

    async def _send_to_rx(self, payload: List[int], key: str, confirmed: bool = True) -> bool:
        """Send CI-V command targeted to 'main' or 'sub' using 0x29 prefix or 0x07 0xD2 switch-and-restore."""
        if self.state["supports_dual_rx"]:
            rx_code = 0x01 if key == "sub" else 0x00
            if self.state.get("supports_29_prefix"):
                full = [0x29, rx_code] + payload
                return await self._send_confirmed(full) if confirmed else (await self._send_cmd(full) or True)
            orig_band = self.state.get("active_band", "MAIN")
            orig_code = 0x01 if orig_band == "SUB" else 0x00
            need_switch = (key == "sub" and orig_band != "SUB") or (key == "main" and orig_band != "MAIN")
            if need_switch:
                await self._send_confirmed([0x07, 0xD2, rx_code])
            ok = await self._send_confirmed(payload) if confirmed else True
            if not confirmed:
                await self._send_cmd(payload)
            if need_switch:
                await self._send_confirmed([0x07, 0xD2, orig_code])
            return ok
        return await self._send_confirmed(payload) if confirmed else (await self._send_cmd(payload) or True)

    async def _reset_repeater_to_simplex(self, key: str = "selected") -> None:
        """Send 0x0F 0x10 (Simplex), 0x16 0x42 0x00 (Tone OFF), 0x0F 0x00 (Split OFF)."""
        tgt_key = self._selected_target_key() if key == "selected" else ("sub" if key == "sub" else "main")
        self.state["active_repeater"][tgt_key] = None
        await self._send_to_rx([0x0F, 0x10], tgt_key, confirmed=True)
        await asyncio.sleep(0.02)
        await self._send_to_rx([0x16, 0x42, 0x00], tgt_key, confirmed=True)
        await asyncio.sleep(0.02)
        await self._send_to_rx([0x0F, 0x00], tgt_key, confirmed=True)
        self.state["split"] = False
        self.state["tone_on"] = False
        self.state["tone_mode"] = 0
        self.state["tsql_on"] = False
        self._notify_state()

    async def tune_repeater(self, rep: Dict[str, Any], target: str = "selected") -> None:
        """Tune target receiver to a repeater with dual-RX same-band collision avoidance and full CI-V setup."""
        rx_hz = int(rep.get("rx_hz") or 0)
        if rx_hz <= 0:
            return
        tx_hz = int(rep.get("tx_hz") or rx_hz)
        raw_shift = rep.get("offset_hz") if rep.get("offset_hz") is not None else rep.get("shift_hz")
        shift_hz = int(raw_shift if raw_shift is not None else (tx_hz - rx_hz))
        if abs(shift_hz) <= 100:
            shift_hz = 0
            tx_hz = rx_hz

        self._suspend_poll += 1
        try:
            if self.state["supports_dual_rx"]:
                # Ensure fresh hardware frequencies on both MAIN and SUB before band-collision check
                await self._poll_dual_rx_bands()
                if target == "sub":
                    key = "sub"
                elif target in ("main", "selected"):
                    if self.state.get("active_band") == "SUB":
                        await self.set_active_band("MAIN")
                    key = "main"
                elif target == "unselected":
                    key = self._unselected_target_key()
                else:
                    key = "main"

                # Dual-RX same-band collision guard: swap MAIN <-> SUB if the other RX is already on this band
                other_key = "main" if key == "sub" else "sub"
                target_band = band_of_hz(rx_hz)
                other_band = band_of_hz(int(self.state[other_key].get("freq_hz") or 0))
                if target_band != "other" and target_band == other_band:
                    await self.swap_vfo()
                    await asyncio.sleep(0.04)
            else:
                if target == "sub":
                    key = "sub"
                elif target == "main":
                    key = "main"
                elif target == "unselected":
                    key = self._unselected_target_key()
                else:
                    key = self._selected_target_key()

            # Normalize modes & subaudible tone settings (prefer FM on hybrid repeaters e.g. DMR/DSTAR/FM)
            ctcss_hz = float(rep.get("ctcss_hz") or rep.get("tone_hz") or 0.0)
            tsql_hz = float(rep.get("tsql_hz") or 0.0)
            tone_type = str(rep.get("tone_type") or "").upper()
            tone_1750 = bool(rep.get("tone_1750")) or ("1750" in tone_type)
            is_tsql = (tsql_hz > 0.0) or (("TSQL" in tone_type) and ctcss_hz > 0.0)
            rx_tsql_hz = tsql_hz if tsql_hz > 0.0 else ctcss_hz
            tx_tone_hz = ctcss_hz if ctcss_hz > 0.0 else tsql_hz

            modes_raw = str(rep.get("mode") or rep.get("modes") or "FM").upper()
            mode_tokens = [t.strip() for t in re.split(r"[,/|\s]+", modes_raw) if t.strip()]
            has_fm = ("FM" in mode_tokens) or (ctcss_hz > 0.0) or (tsql_hz > 0.0) or tone_1750
            is_dstar_only = (not has_fm) and (
                bool(rep.get("is_dstar")) or any(t in ("DSTAR", "D-STAR", "DV") for t in mode_tokens)
            )
            rep_mode = "DV" if is_dstar_only else "FM"

            # Store active repeater record before tuning so frequency updates don't clear it
            self.state["active_repeater"][key] = {
                "callsign": str(rep.get("callsign") or ""),
                "qth": str(rep.get("qth") or ""),
                "rx_hz": rx_hz,
                "tx_hz": tx_hz,
                "shift_hz": shift_hz,
                "ctcss_hz": ctcss_hz,
                "tone_type": tone_type,
                "tone_1750": tone_1750,
                "mode": rep_mode,
                "modes_raw": modes_raw,
                "qrv": bool(rep.get("qrv", True)),
            }

            # Step 1: Receiver select
            if self.state["supports_dual_rx"]:
                await self.set_active_band("SUB" if key == "sub" else "MAIN")
                await asyncio.sleep(0.02)

            # Step 2: RX frequency (collision already resolved cleanly above)
            await self.set_frequency(rx_hz, target=key, _allow_band_swap=False)
            await asyncio.sleep(0.02)

            # Step 3: Mode (FM preferred on hybrid/analog, DV only for D-STAR-only)
            await self.set_mode(rep_mode, target=key)
            await asyncio.sleep(0.02)

            # Step 4: Offset + Duplex direction
            if shift_hz != 0:
                off_bcd = list(encode_offset_bcd(abs(shift_hz)))
                await self._send_to_rx([0x0D] + off_bcd, key, confirmed=True)
                await asyncio.sleep(0.02)
                dup_code = 0x11 if shift_hz < 0 else 0x12
                await self._send_to_rx([0x0F, dup_code], key, confirmed=True)
            else:
                await self._send_to_rx([0x0F, 0x10], key, confirmed=True)
            await asyncio.sleep(0.02)

            # Step 5: CTCSS / TSQL / 1750 tone frequency & tone mode (0x1B 0x00 / 0x01 + 0x16 0x42)
            if is_tsql and tx_tone_hz > 0.0:
                tx_bcd = list(encode_ctcss_bcd(tx_tone_hz))
                rx_bcd = list(encode_ctcss_bcd(rx_tsql_hz))
                await self._send_to_rx([0x1B, 0x00] + tx_bcd, key, confirmed=True)
                await asyncio.sleep(0.02)
                await self._send_to_rx([0x1B, 0x01] + rx_bcd, key, confirmed=True)
                await asyncio.sleep(0.02)
                await self._send_to_rx([0x16, 0x42, 0x02], key, confirmed=True)
                self.state["tone_mode"] = 2
                self.state["tone_on"] = True
                self.state["tsql_on"] = True
            elif ctcss_hz > 0.0:
                t_bcd = list(encode_ctcss_bcd(ctcss_hz))
                await self._send_to_rx([0x1B, 0x00] + t_bcd, key, confirmed=True)
                await asyncio.sleep(0.02)
                await self._send_to_rx([0x16, 0x42, 0x01], key, confirmed=True)
                self.state["tone_mode"] = 1
                self.state["tone_on"] = True
                self.state["tsql_on"] = False
            elif tone_1750:
                t_bcd = list(encode_ctcss_bcd(1750.0))
                await self._send_to_rx([0x1B, 0x00] + t_bcd, key, confirmed=True)
                await asyncio.sleep(0.02)
                await self._send_to_rx([0x16, 0x42, 0x00], key, confirmed=True)
                self.state["tone_mode"] = 0
                self.state["tone_on"] = False
                self.state["tsql_on"] = False
            else:
                await self._send_to_rx([0x16, 0x42, 0x00], key, confirmed=True)
                self.state["tone_mode"] = 0
                self.state["tone_on"] = False
                self.state["tsql_on"] = False

            if self.state["supports_dual_rx"]:
                await self._poll_dual_rx_bands()
            self._notify_state()
        finally:
            self._suspend_poll = max(0, self._suspend_poll - 1)

    async def set_frequency(self, freq_hz: int, target: str = "selected", _allow_band_swap: bool = True) -> None:
        bcd = encode_bcd_freq(freq_hz)
        if self.state["supports_dual_rx"]:
            cur_band = self.state.get("active_band", "MAIN")
            if target == "selected":
                key = self._selected_target_key()
                other_key = self._unselected_target_key()
                t_band = band_of_hz(freq_hz)
                if _allow_band_swap and t_band != "other" and t_band == band_of_hz(int(self.state[other_key].get("freq_hz") or 0)):
                    await self.swap_vfo()
                    key = self._selected_target_key()
                await self._send_confirmed([0x05] + list(bcd))
            elif target == "unselected":
                key = self._unselected_target_key()
                tgt_band = "MAIN" if cur_band == "SUB" else "SUB"
                await self._send_confirmed([0x07, 0xD1 if tgt_band == "SUB" else 0xD0])
                await self._send_confirmed([0x05] + list(bcd))
                await self._send_confirmed([0x07, 0xD1 if cur_band == "SUB" else 0xD0])
            else:
                key = "sub" if target == "sub" else "main"
                tgt_band = key.upper()
                if tgt_band != cur_band:
                    await self._send_confirmed([0x07, 0xD1 if tgt_band == "SUB" else 0xD0])
                await self._send_confirmed([0x05] + list(bcd))
                if tgt_band != cur_band:
                    await self._send_confirmed([0x07, 0xD1 if cur_band == "SUB" else 0xD0])
            self.state[key]["freq_hz"] = int(freq_hz)
            rep = self.state.get("active_repeater", {}).get(key)
            if rep and not self.state.get("tx"):
                rx_target = int(rep.get("rx_hz") or 0)
                tx_target = int(rep.get("tx_hz") or rx_target)
                if abs(int(freq_hz) - rx_target) > 1000 and abs(int(freq_hz) - tx_target) > 1000:
                    await self._reset_repeater_to_simplex(key)
                    return
            self._notify_state()
            return

        if self.state.get("supports_vfo_25", True):
            if target == "selected":
                await self._send_confirmed([0x25, 0x00] + list(bcd))
                key = self._selected_target_key()
            elif target == "unselected":
                await self._send_confirmed([0x25, 0x01] + list(bcd))
                key = self._unselected_target_key()
            elif target == "sub":
                sub_code = 0x00 if self._selected_target_key() == "sub" else 0x01
                await self._send_confirmed([0x25, sub_code] + list(bcd))
                key = "sub"
            else:  # main
                main_code = 0x00 if self._selected_target_key() == "main" else 0x01
                await self._send_confirmed([0x25, main_code] + list(bcd))
                key = "main"
        else:
            sel_key = self._selected_target_key()
            if target == "selected":
                key = sel_key
            elif target == "unselected":
                key = self._unselected_target_key()
            else:
                key = "sub" if target == "sub" else "main"
            if key == sel_key:
                await self._send_confirmed([0x05] + list(bcd))
            else:
                tgt_code = 0x01 if key == "sub" else 0x00
                cur_code = 0x01 if sel_key == "sub" else 0x00
                await self._send_confirmed([0x07, tgt_code])
                await self._send_confirmed([0x05] + list(bcd))
                await self._send_confirmed([0x07, cur_code])
        self.state[key]["freq_hz"] = int(freq_hz)
        rep = self.state.get("active_repeater", {}).get(key)
        if rep and not self.state.get("tx"):
            rx_target = int(rep.get("rx_hz") or 0)
            tx_target = int(rep.get("tx_hz") or rx_target)
            if abs(int(freq_hz) - rx_target) > 1000 and abs(int(freq_hz) - tx_target) > 1000:
                await self._reset_repeater_to_simplex(key)
                return
        self._notify_state()

    async def set_mode(self, mode_str: str, filter_num: Optional[int] = None, target: str = "selected") -> None:
        m_upper = mode_str.upper()
        m_byte, d_byte = MODE_REV.get(m_upper, (0x01, 0x00))
        if self.state["supports_dual_rx"]:
            cur_band = self.state.get("active_band", "MAIN")
            if target == "selected":
                key = self._selected_target_key()
            elif target == "unselected":
                key = self._unselected_target_key()
            else:
                key = "sub" if target == "sub" else "main"
            flt = filter_num if filter_num in (1, 2, 3) else self.state[key]["filter"]
            tgt_band = key.upper()
            if tgt_band != cur_band:
                await self._send_confirmed([0x07, 0xD1 if tgt_band == "SUB" else 0xD0])
            await self._send_confirmed([0x06, m_byte, flt])
            if d_byte:
                await self._send_confirmed([0x1A, 0x06, 0x01, 0x01])
            if tgt_band != cur_band:
                await self._send_confirmed([0x07, 0xD1 if cur_band == "SUB" else 0xD0])
            self.state[key]["mode"] = m_upper
            self.state[key]["data_mode"] = bool(d_byte)
            self.state[key]["filter"] = flt
            self.state[key]["filter_hz"] = FILTER_HZ.get(flt, 2400)
            self._notify_state()
            return

        if self.state.get("supports_vfo_25", True):
            if target == "selected":
                sel_code = 0x00
                key = self._selected_target_key()
            elif target == "unselected":
                sel_code = 0x01
                key = self._unselected_target_key()
            elif target == "sub":
                sel_code = 0x00 if self._selected_target_key() == "sub" else 0x01
                key = "sub"
            else:
                sel_code = 0x00 if self._selected_target_key() == "main" else 0x01
                key = "main"

            flt = filter_num if filter_num in (1, 2, 3) else self.state[key]["filter"]
            await self._send_confirmed([0x26, sel_code, m_byte, d_byte, flt])
        else:
            sel_key = self._selected_target_key()
            if target == "selected":
                key = sel_key
            elif target == "unselected":
                key = self._unselected_target_key()
            else:
                key = "sub" if target == "sub" else "main"
            flt = filter_num if filter_num in (1, 2, 3) else self.state[key]["filter"]
            need_swap = key != sel_key
            if need_swap:
                await self._send_confirmed([0x07, 0x01 if key == "sub" else 0x00])
            await self._send_confirmed([0x06, m_byte, flt])
            if m_byte in (0x00, 0x01, 0x02, 0x05):
                await self._send_confirmed([0x1A, 0x06, 0x01 if d_byte else 0x00, flt if d_byte else 0x00])
            if need_swap:
                await self._send_confirmed([0x07, 0x01 if sel_key == "sub" else 0x00])
        self.state[key]["mode"] = m_upper
        self.state[key]["data_mode"] = bool(d_byte)
        self.state[key]["filter"] = flt
        self.state[key]["filter_hz"] = FILTER_HZ.get(flt, 2400)
        self._notify_state()

    async def set_ptt(self, on: bool) -> None:
        ok = await self._send_confirmed([0x1C, 0x00, 0x01 if on else 0x00], retries=4)
        if not ok and not on:
            # Safety fallback: force extra RX unkey attempts and read back true PTT status
            await self._send_confirmed([0x1C, 0x00, 0x00], retries=2)
        resp = await self._send_and_expect([0x1C, 0x00], timeout=0.12)
        if resp and len(resp) >= 7 and resp[4] == 0x1C and resp[5] == 0x00:
            self.state["tx"] = resp[6] == 0x01
        else:
            self.state["tx"] = bool(on) if ok else self.state["tx"]
        self._notify_state()

    async def set_tuner(self, on: bool) -> None:
        if not self.state["supports_tuner"]:
            return
        await self._send_cmd([0x1C, 0x01, 0x01 if on else 0x00])
        self.state["tuner_on"] = bool(on)
        self.state["tuning"] = False
        self._notify_state()

    async def start_tune(self) -> None:
        if not self.state["supports_tuner"]:
            return
        await self._send_cmd([0x1C, 0x01, 0x02])
        self.state["tuner_on"] = True
        self.state["tuning"] = True
        self._notify_state()

    async def equalize_vfo(self) -> None:
        src_key = self._selected_target_key()
        src = self.state[src_key]
        if src.get("freq_hz", 0) > 0:
            await self.set_frequency(int(src["freq_hz"]), target="unselected")
            await asyncio.sleep(0.02)
        if src.get("mode") and src["mode"] != "---":
            await self.set_mode(src["mode"], src.get("filter"), target="unselected")

    async def set_rit_hz(self, hz: int) -> None:
        clamped = max(-9999, min(9999, int(hz)))
        mag = abs(clamped)
        sign_b = 0x01 if clamped < 0 else 0x00
        await self._send_cmd([0x21, 0x00, int_to_bcd(mag % 100), int_to_bcd(mag // 100), sign_b])
        self.state["rit_hz"] = clamped
        self._notify_state()

    async def set_active_band(self, band: str) -> None:
        b = band.upper()
        if self.state["supports_dual_rx"]:
            code = 0xD1 if b == "SUB" else 0xD0
            await self._send_confirmed([0x07, code])
            self.state["active_band"] = "SUB" if b == "SUB" else "MAIN"
        else:
            # VFO A / B selection
            code = 0x01 if b in ("B", "SUB") else 0x00
            await self._send_confirmed([0x07, code])
            self.state["active_band"] = "SUB" if code == 0x01 else "MAIN"
        self._notify_state()

    async def swap_vfo(self) -> None:
        self._suspend_poll += 1
        try:
            if self.state["supports_dual_rx"] and self.state.get("active_band") == "SUB":
                await self._send_confirmed([0x07, 0xD0])
                self.state["active_band"] = "MAIN"
            reps = self.state.get("active_repeater")
            if isinstance(reps, dict):
                reps["main"], reps["sub"] = reps.get("sub"), reps.get("main")
            await self._send_confirmed([0x07, 0xB0])
            if self.state["supports_dual_rx"]:
                await asyncio.sleep(0.04)
                await self._poll_dual_rx_bands()
                self._notify_state()
            elif self.state.get("supports_vfo_25", True):
                await self._send_cmd([0x25, 0x00])
                await asyncio.sleep(0.03)
                await self._send_cmd([0x25, 0x01])
                await asyncio.sleep(0.03)
                await self._send_cmd([0x26, 0x00])
                await asyncio.sleep(0.03)
                await self._send_cmd([0x26, 0x01])
            else:
                self.state["main"], self.state["sub"] = self.state["sub"], self.state["main"]
                await self._send_cmd([0x03])
                await asyncio.sleep(0.03)
                await self._send_cmd([0x04])
                await asyncio.sleep(0.03)
                await self._send_cmd([0x1A, 0x06])
                self._notify_state()
        finally:
            self._suspend_poll = max(0, self._suspend_poll - 1)

    async def set_dualwatch(self, on: bool) -> None:
        if self.state["supports_dualwatch"]:
            await self._send_confirmed([0x16, 0x59, 0x01 if on else 0x00])
            self.state["dualwatch"] = bool(on)
            self._notify_state()

    async def set_split(self, on: bool) -> None:
        await self._send_confirmed([0x0F, 0x01 if on else 0x00])
        self.state["split"] = bool(on)
        self._notify_state()

    async def set_level(self, name: str, pct: int) -> None:
        if name == "cw_wpm":
            wpm = max(6, min(48, int(pct)))
            val_255 = int(round(((wpm - 6) / 42.0) * 255))
            await self._send_cmd([0x14, 0x0C] + list(encode_bcd_level(val_255)))
            self.state["cw_wpm"] = wpm
            self._notify_state()
            return

        pct = max(0, min(100, int(pct)))
        val_255 = int(round((pct / 100.0) * 255))
        bcd = encode_bcd_level(val_255)
        sub_map = {
            "af_gain": 0x01,
            "rf_gain": 0x02,
            "sql": 0x03,
            "nr_level": 0x06,
            "pbt1": 0x07,
            "pbt2": 0x08,
            "tx_power": 0x0A,
            "mic_gain": 0x0B,
            "notch_pos": 0x0D,
            "comp_level": 0x0E,
            "nb_level": 0x12,
            "mon_level": 0x15,
        }
        if name in sub_map:
            await self._send_cmd([0x14, sub_map[name]] + list(bcd))
            self.state[name] = pct
            if name == "sql":
                await asyncio.sleep(0.02)
                await self._send_cmd([0x15, 0x05], wait_reply=False)
            self._notify_state()

    async def reset_pbt(self) -> None:
        if not self.state["supports_pbt"]:
            return
        bcd = list(encode_bcd_level(128))
        await self._send_cmd([0x14, 0x07] + bcd)
        await asyncio.sleep(0.02)
        await self._send_cmd([0x14, 0x08] + bcd)
        self.state["pbt1"] = 50
        self.state["pbt2"] = 50
        self._notify_state()

    async def set_dsp_toggle(self, name: str, value: Any) -> None:
        if name == "nb":
            on = bool(value)
            await self._send_cmd([0x16, 0x22, 0x01 if on else 0x00])
            self.state["nb"] = on
        elif name == "nr":
            on = bool(value)
            await self._send_cmd([0x16, 0x40, 0x01 if on else 0x00])
            self.state["nr"] = on
        elif name == "notch":
            on = bool(value)
            await self._send_cmd([0x16, 0x48, 0x01 if on else 0x00])
            self.state["notch"] = on
        elif name == "anf":
            on = bool(value)
            await self._send_cmd([0x16, 0x41, 0x01 if on else 0x00])
            self.state["anf"] = on
        elif name == "tone_on":
            on = bool(value)
            await self._send_cmd([0x16, 0x42, 0x01 if on else 0x00])
            self.state["tone_on"] = on
        elif name == "tsql_on":
            on = bool(value)
            await self._send_cmd([0x16, 0x43, 0x01 if on else 0x00])
            self.state["tsql_on"] = on
        elif name == "comp_on":
            on = bool(value)
            await self._send_cmd([0x16, 0x44, 0x01 if on else 0x00])
            self.state["comp_on"] = on
        elif name == "mon_on":
            on = bool(value)
            await self._send_cmd([0x16, 0x45, 0x01 if on else 0x00])
            self.state["mon_on"] = on
        elif name == "vox_on":
            on = bool(value)
            await self._send_cmd([0x16, 0x46, 0x01 if on else 0x00])
            self.state["vox_on"] = on
        elif name == "bkin":
            v = int(value) % 3
            await self._send_cmd([0x16, 0x47, v])
            self.state["bkin"] = v
        elif name == "preamp":
            v = int(value) % 3
            await self._send_cmd([0x16, 0x02, v])
            self.state["preamp"] = v
        elif name == "att":
            v = int(value)
            await self._send_cmd([0x11, int_to_bcd(v)])
            self.state["att"] = v
        elif name == "agc":
            v = max(1, min(3, int(value)))
            await self._send_cmd([0x16, 0x12, v])
            self.state["agc"] = v
        elif name == "rit_on":
            on = bool(value)
            await self._send_cmd([0x21, 0x01, 0x01 if on else 0x00])
            self.state["rit_on"] = on
        self._notify_state()

    async def set_scope_span(self, total_span_hz: int) -> None:
        """Set scope center-mode half-span via 0x27 0x15."""
        half_span = max(2500, min(500000, int(total_span_hz) // 2))
        bcd = encode_bcd_freq(half_span)
        await self._send_cmd([0x27, 0x14, 0x00, 0x00])  # Ensure center mode
        await asyncio.sleep(0.02)
        await self._send_cmd([0x27, 0x15, 0x00] + list(bcd))
        self.state["scope_span_hz"] = half_span * 2
        self._notify_state()

    @staticmethod
    def sanitize_cw_text(text: str) -> str:
        up = str(text or "").upper().strip()
        cleaned = "".join(c for c in up if c in _CW_ALLOWED_CHARS)
        return " ".join(cleaned.split())

    async def abort_cw_macro(self, release_ptt: bool = True) -> None:
        """Immediately halt active CI-V 0x17 CW keying and restore original SSB mode/freq if shifted."""
        self._cw_live_queue.clear()
        task = self._cw_task
        if task is None or task.done():
            self.state["cw_tx_slot"] = 0
            return
        self._cw_release_ptt_on_abort = bool(release_ptt)
        self._cw_abort_ev.set()
        if self._serial and self._serial.is_open:
            try:
                await self._send_cmd([0x17, 0xFF], wait_reply=False)
            except Exception:
                pass
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except Exception:
            task.cancel()
        self._cw_task = None
        self.state["cw_tx_slot"] = 0
        self._notify_state()

    async def send_cw_macro(self, macro_idx: int, text: str) -> None:
        """Key CW macro (1..4) or live free-text (5) via CI-V 0x17 with +-1 kHz SSB zero-beat shift."""
        if macro_idx not in (1, 2, 3, 4, 5):
            return
        clean = self.sanitize_cw_text(text)

        # Free-text live keying (slot 5) appends seamlessly if already sending live CW
        if macro_idx == 5 and self._cw_task is not None and not self._cw_task.done() and self.state.get("cw_tx_slot") == 5:
            if clean:
                self._cw_live_queue.append(clean)
            return

        was_same = (self._cw_task is not None and not self._cw_task.done() and self.state.get("cw_tx_slot") == macro_idx)
        if self._cw_task is not None and not self._cw_task.done():
            await self.abort_cw_macro(release_ptt=True)
            if was_same:
                return

        if not clean:
            return

        self._cw_live_queue.clear()
        self._cw_abort_ev.clear()
        self._cw_release_ptt_on_abort = True
        self._cw_task = asyncio.create_task(self._run_cw_macro(macro_idx, clean))

    async def _run_cw_macro(self, macro_idx: int, clean_text: str) -> None:
        key = self._selected_target_key()
        orig_mode = str(self.state[key].get("mode", "")).upper()
        orig_freq = int(self.state[key].get("freq_hz", 0))
        orig_filter = int(self.state[key].get("filter", 2))

        is_lsb = orig_mode in ("LSB", "LSB-D", "DATA-L")
        is_usb = orig_mode in ("USB", "USB-D", "DIGI", "DATA-U")
        restore_needed = False
        forced_ptt = False

        try:
            self.state["cw_tx_slot"] = macro_idx
            self._notify_state()

            if (is_lsb or is_usb) and orig_freq > 0:
                restore_needed = True
                # LSB receiver hears f_dial - f_carrier (+1000 Hz audio -> carrier at f_dial - 1000 Hz)
                # USB receiver hears f_carrier - f_dial (+1000 Hz audio -> carrier at f_dial + 1000 Hz)
                cw_freq = max(30000, orig_freq - 1000) if is_lsb else (orig_freq + 1000)
                await self.set_mode("CW", orig_filter, target="selected")
                await asyncio.sleep(0.03)
                await self.set_frequency(cw_freq, target="selected")
                await asyncio.sleep(0.07)

            if self._cw_abort_ev.is_set():
                return

            if int(self.state.get("bkin", 0)) == 0:
                await self.set_ptt(True)
                forced_ptt = True
                await asyncio.sleep(0.04)

            pending_msgs = [clean_text]
            while pending_msgs and not self._cw_abort_ev.is_set():
                msg_now = pending_msgs.pop(0)
                words = msg_now.split(" ")
                chunks: List[str] = []
                cur = ""
                for w in words:
                    while len(w) > 30:
                        if cur:
                            chunks.append(cur)
                            cur = ""
                        chunks.append(w[:30])
                        w = w[30:]
                    cand = f"{cur} {w}".strip() if cur else w
                    if len(cand) <= 30:
                        cur = cand
                    else:
                        chunks.append(cur)
                        cur = w
                if cur:
                    chunks.append(cur)

                for ci, chunk in enumerate(chunks):
                    if self._cw_abort_ev.is_set():
                        break
                    wpm = max(6, min(48, int(self.state.get("cw_wpm", 20))))
                    payload = [0x17] + [ord(c) for c in chunk]
                    self.state["cw_sidetone_text"] = chunk
                    self.state["cw_sidetone_seq"] = int(self.state.get("cw_sidetone_seq", 0)) + 1
                    self._notify_state()
                    await self._send_cmd(payload, wait_reply=False)
                    has_more = (ci < len(chunks) - 1) or bool(pending_msgs) or bool(self._cw_live_queue)
                    dur_s = _cw_duration_seconds(chunk, wpm) + (0.25 if has_more else 0.35)
                    deadline = time.monotonic() + dur_s
                    while time.monotonic() < deadline:
                        if self._cw_abort_ev.is_set():
                            break
                        await asyncio.sleep(0.04)
                if not self._cw_abort_ev.is_set() and self._cw_live_queue:
                    pending_msgs.append(self._cw_live_queue.pop(0))

            # Wait briefly for semi break-in release before restoring SSB frequency/mode
            if not self._cw_abort_ev.is_set() and not forced_ptt and int(self.state.get("bkin", 0)) > 0:
                rel_deadline = time.monotonic() + 0.9
                while time.monotonic() < rel_deadline and self.state.get("tx"):
                    if self._cw_abort_ev.is_set():
                        break
                    await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            pass
        finally:
            self._cw_live_queue.clear()
            aborted = self._cw_abort_ev.is_set()
            if aborted and self._serial and self._serial.is_open:
                try:
                    await self._send_cmd([0x17, 0xFF], wait_reply=False)
                except Exception:
                    pass
            if forced_ptt and (not aborted or self._cw_release_ptt_on_abort):
                try:
                    await self.set_ptt(False)
                except Exception:
                    pass
            if restore_needed and orig_freq > 0:
                try:
                    await asyncio.sleep(0.04)
                    await self.set_frequency(orig_freq, target="selected")
                    await asyncio.sleep(0.03)
                    await self.set_mode(orig_mode, orig_filter, target="selected")
                except Exception:
                    pass
            self.state["cw_tx_slot"] = 0
            self.state["cw_sidetone_text"] = ""
            self._notify_state()

    def clear_swr_scan(self) -> None:
        self.state["swr_scan"] = {
            "active": False,
            "progress": 0,
            "total": 0,
            "center_hz": 0,
            "span_hz": 0,
            "points": [],
            "error": "",
        }
        self._notify_state()

    async def abort_swr_scan(self) -> None:
        task = self._swr_task
        if task is None or task.done():
            if self.state.get("swr_scan", {}).get("active"):
                self.state["swr_scan"]["active"] = False
                self._notify_state()
            return
        self._swr_abort_ev.set()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except Exception:
            task.cancel()
        self._swr_task = None
        self.state["swr_scan"]["active"] = False
        self._notify_state()

    async def start_swr_scan(
        self,
        center_hz: int = 0,
        span_hz: int = 0,
        on_scan_audio_mute: Optional[Callable[[bool], None]] = None,
    ) -> None:
        if self._swr_task is not None and not self._swr_task.done():
            await self.abort_swr_scan()
            return
        if self._cw_task is not None and not self._cw_task.done():
            await self.abort_cw_macro(release_ptt=True)
        if not self.state.get("connected"):
            return

        key = self._selected_target_key()
        c_hz = int(center_hz or 0) or int(self.state[key].get("freq_hz", 0)) or int(self.state.get("scope_center_hz", 0))
        sp_hz = int(span_hz or 0) or int(self.state.get("scope_span_hz", 100000))
        if c_hz <= 0 or sp_hz <= 0:
            return

        f_min = max(30000, c_hz - sp_hz // 2)
        f_max = c_hz + sp_hz // 2
        n_pts = 17 if sp_hz <= 100000 else 21
        self._swr_abort_ev.clear()
        self._swr_task = asyncio.create_task(
            self._run_swr_scan(c_hz, sp_hz, f_min, f_max, n_pts, on_scan_audio_mute)
        )

    async def _run_swr_scan(
        self,
        center_hz: int,
        span_hz: int,
        f_min: int,
        f_max: int,
        n_pts: int,
        on_scan_audio_mute: Optional[Callable[[bool], None]] = None,
    ) -> None:
        key = self._selected_target_key()
        orig_freq = int(self.state[key].get("freq_hz", 0))
        orig_mode = str(self.state[key].get("mode", "")).upper()
        orig_data = bool(self.state[key].get("data_mode", False))
        orig_filter = int(self.state[key].get("filter", 2))
        orig_power = int(self.state.get("tx_power", 50))

        points: List[Dict[str, Any]] = []
        self.state["swr_scan"] = {
            "active": True,
            "progress": 0,
            "total": n_pts,
            "center_hz": center_hz,
            "span_hz": span_hz,
            "points": points,
            "error": "",
        }
        self._notify_state()

        try:
            if on_scan_audio_mute:
                on_scan_audio_mute(True)

            if orig_mode != "RTTY" or orig_data:
                m_byte, d_byte = MODE_REV["RTTY"]
                if self.state["supports_dual_rx"] or not self.state.get("supports_vfo_25", True):
                    ok_rtty = await self._send_confirmed([0x06, m_byte, orig_filter], retries=2)
                else:
                    sel_code = 0x00
                    ok_rtty = await self._send_confirmed([0x26, sel_code, m_byte, d_byte, orig_filter], retries=2)
                if ok_rtty:
                    self.state[key]["mode"] = "RTTY"
                    self.state[key]["data_mode"] = False
                else:
                    await self.set_mode("FM", orig_filter, target="selected")
                await asyncio.sleep(0.04)

            for i in range(n_pts):
                if self._swr_abort_ev.is_set() or not self._running or not self.state.get("connected"):
                    break
                f_hz = int(round(f_min + (f_max - f_min) * (i / max(1, n_pts - 1))))
                await self.set_frequency(f_hz, target="selected")
                await asyncio.sleep(0.025)

                await self.set_ptt(True)
                swr_val: Optional[float] = None
                samples: List[float] = []
                meter_pwr = 0
                if self.state.get("tx"):
                    # Wait for PA ramp + Icom ballistic SWR meter bridge to fully settle
                    await asyncio.sleep(0.15)
                    await self._send_and_expect([0x15, 0x11], timeout=0.14)
                    meter_pwr = int(self.state.get("pwr_meter", 0))
                    for _poll_i, wait_s in enumerate((0.0, 0.07)):
                        if wait_s > 0:
                            await asyncio.sleep(wait_s)
                        resp_swr = await self._send_and_expect([0x15, 0x12], timeout=0.14)
                        if resp_swr and len(resp_swr) >= 8 and resp_swr[4] == 0x15 and resp_swr[5] == 0x12:
                            if meter_pwr > 0:
                                samples.append(float(self.state.get("swr", 1.0)))
                    # If SWR meter was still climbing between 150ms and 220ms, take one more settled sample
                    if len(samples) >= 2 and samples[-1] > samples[-2] + 0.12:
                        await asyncio.sleep(0.07)
                        resp_swr = await self._send_and_expect([0x15, 0x12], timeout=0.14)
                        if resp_swr and len(resp_swr) >= 8 and resp_swr[4] == 0x15 and resp_swr[5] == 0x12:
                            if meter_pwr > 0:
                                samples.append(float(self.state.get("swr", 1.0)))
                    if samples:
                        swr_val = samples[-1]

                await self.set_ptt(False)
                await asyncio.sleep(0.02)

                log.info(
                    "SWR scan pt %d Hz: tx_pwr=%d%% meter_pwr=%d%% samples=%s swr=%s",
                    f_hz, orig_power, meter_pwr, samples, swr_val,
                )
                points.append({
                    "freq_hz": f_hz,
                    "swr": round(swr_val, 2) if swr_val is not None else None,
                })
                self.state["swr_scan"]["progress"] = i + 1
                self._notify_state()

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.state["swr_scan"]["error"] = str(exc)
        finally:
            try:
                await self.set_ptt(False)
            except Exception:
                pass
            if orig_freq > 0:
                try:
                    await asyncio.sleep(0.03)
                    await self.set_frequency(orig_freq, target="selected")
                except Exception:
                    pass
            if orig_mode and orig_mode != "---":
                try:
                    await asyncio.sleep(0.03)
                    restore_mode = f"{orig_mode}-D" if orig_data and "-D" not in orig_mode and "DATA" not in orig_mode and "DIGI" not in orig_mode and f"{orig_mode}-D" in MODE_REV else orig_mode
                    await self.set_mode(restore_mode, orig_filter, target="selected")
                except Exception:
                    pass
            if on_scan_audio_mute:
                try:
                    on_scan_audio_mute(False)
                except Exception:
                    pass
            self.state["swr_scan"]["active"] = False
            self._notify_state()
