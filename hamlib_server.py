"""Hamlib rigctld-compatible multi-port TCP server for WSJT-X, N1MM, and loggers."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("hamlib_server")

# Map internal mode strings <-> Hamlib rigctld mode names
TO_HAMLIB_MODE = {
    "LSB": "LSB",
    "USB": "USB",
    "CW": "CW",
    "CW-R": "CWR",
    "FM": "FM",
    "WFM": "WFM",
    "AM": "AM",
    "RTTY": "RTTY",
    "RTTY-R": "RTTYR",
    "DIGI": "PKTUSB",
    "USB-D": "PKTUSB",
    "LSB-D": "PKTLSB",
    "FM-D": "PKTFM",
    "DV": "DSTAR",
}

FROM_HAMLIB_MODE = {
    "LSB": "LSB",
    "USB": "USB",
    "CW": "CW",
    "CWR": "CW-R",
    "FM": "FM",
    "WFM": "WFM",
    "AM": "AM",
    "RTTY": "RTTY",
    "RTTYR": "RTTY-R",
    "PKTUSB": "DIGI",
    "PKTLSB": "LSB-D",
    "PKTFM": "FM-D",
    "DIG": "DIGI",
    "DSTAR": "DV",
}


class HamlibServerManager:
    """Runs multiple rigctld TCP servers (2 configurable ports per radio, multi-client)."""

    def __init__(
        self,
        get_radio_state: Callable[[int], Dict[str, Any]],
        exec_radio_cmd: Callable[[int, str, Dict[str, Any]], Awaitable[None]],
        exec_raw_civ: Optional[Callable[[int, bytes], Awaitable[Optional[bytes]]]] = None,
    ) -> None:
        self.get_radio_state = get_radio_state
        self.exec_radio_cmd = exec_radio_cmd
        self.exec_raw_civ = exec_raw_civ
        self._servers: List[asyncio.AbstractServer] = []
        self._bound_ports: Dict[int, List[int]] = {1: [], 2: []}

    async def start(self, cfg: Dict[str, Any]) -> None:
        await self.stop()
        for rcfg in cfg.get("radios", []):
            slot = int(rcfg.get("slot", 1))
            if not rcfg.get("enabled", True):
                continue
            ports = []
            for key in ("hamlib_port", "hamlib_port_2"):
                p = int(rcfg.get(key, 0) or 0)
                if p > 0 and p not in ports:
                    ports.append(p)
            for port in ports:
                try:
                    srv = await asyncio.start_server(
                        lambda r, w, _slot=slot: self._handle_client(r, w, _slot),
                        host="0.0.0.0",
                        port=port,
                    )
                    self._servers.append(srv)
                    self._bound_ports[slot].append(port)
                    log.info("Hamlib rigctld listening on port %d -> Radio %d", port, slot)
                except Exception as exc:
                    log.warning("Failed to bind Hamlib port %d for Radio %d: %s", port, slot, exc)

    async def stop(self) -> None:
        for srv in self._servers:
            srv.close()
            try:
                await srv.wait_closed()
            except Exception:
                pass
        self._servers.clear()
        self._bound_ports = {1: [], 2: []}

    def get_active_ports(self) -> Dict[int, List[int]]:
        return dict(self._bound_ports)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, slot: int
    ) -> None:
        try:
            first = await reader.read(2048)
            if not first:
                return

            # Auto-detect Binary Icom CI-V over TCP (N1MM Logger+ Port=TCP) vs ASCII Hamlib rigctld (WSJT-X)
            if first[0] == 0xFE:
                log.info("Binary CI-V TCP client connected -> Radio %d", slot)
                await self._handle_binary_civ_client(first, reader, writer, slot)
                return

            # ASCII Hamlib rigctld mode
            text_buf = first
            while True:
                while b"\n" not in text_buf:
                    more = await reader.read(2048)
                    if not more:
                        return
                    text_buf += more
                line_bytes, text_buf = text_buf.split(b"\n", 1)
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                # Extended protocol prefix (+ or ; or |)
                ext_sep: Optional[str] = None
                if line[0] in ("+", ";", "|"):
                    ext_sep = "\n" if line[0] == "+" else line[0]
                    line = line[1:].strip()

                if not line:
                    continue

                if line in ("q", "quit", "\\quit"):
                    writer.write(b"RPRT 0\n")
                    await writer.drain()
                    break

                resp_lines, rprt, append_rprt = await self._dispatch(slot, line)
                if ext_sep is not None:
                    # Extended response echoes long/short command name and appends RPRT <code>
                    parts = line.split()
                    cname = parts[0].lstrip("\\")
                    arg_str = " ".join(parts[1:])
                    body = f"{cname}: {arg_str}".rstrip() + ext_sep
                    if resp_lines:
                        body += ext_sep.join(resp_lines) + ext_sep
                    body += f"RPRT {rprt}\n"
                    writer.write(body.encode("utf-8"))
                else:
                    if resp_lines:
                        payload_out = "\n".join(resp_lines) + "\n"
                        if append_rprt:
                            payload_out += f"RPRT {rprt}\n"
                        writer.write(payload_out.encode("utf-8"))
                    else:
                        writer.write(f"RPRT {rprt}\n".encode("utf-8"))
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.debug("Hamlib client error (slot %d): %s", slot, exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, slot: int, line: str) -> tuple[List[str], int, bool]:
        parts = line.split()
        cmd = parts[0]
        args = parts[1:]
        st = self.get_radio_state(slot)
        active_key = "sub" if st.get("active_band") == "SUB" else "main"
        vfo_state = st.get(active_key, st.get("main", {}))

        # --- GET FREQUENCY ---
        if cmd in ("f", "\\get_freq"):
            return [str(int(vfo_state.get("freq_hz", 0)))], 0, False

        # --- SET FREQUENCY ---
        if cmd in ("F", "\\set_freq") and len(args) >= 1:
            try:
                hz = int(round(float(args[0])))
                await self.exec_radio_cmd(slot, "set_freq", {"freq_hz": hz, "target": "selected"})
                return [], 0, False
            except Exception:
                return [], -1, False

        # --- GET MODE ---
        if cmd in ("m", "\\get_mode"):
            m = vfo_state.get("mode", "USB")
            if vfo_state.get("data_mode") and m in ("USB", "LSB", "FM", "DIGI"):
                h_mode = TO_HAMLIB_MODE.get("DIGI" if m == "USB" else m, "PKTUSB")
            else:
                h_mode = TO_HAMLIB_MODE.get(m, "USB")
            pb = int(vfo_state.get("filter_hz", 2400))
            return [h_mode, str(pb)], 0, False

        # --- SET MODE ---
        if cmd in ("M", "\\set_mode") and len(args) >= 1:
            h_mode = args[0].upper()
            pb = int(float(args[1])) if len(args) >= 2 else -1
            internal_mode = FROM_HAMLIB_MODE.get(h_mode, h_mode)
            flt = 1
            if pb > 0:
                if pb <= 1800:
                    flt = 3
                elif pb <= 2400:
                    flt = 2
                else:
                    flt = 1
            await self.exec_radio_cmd(slot, "set_mode", {"mode": internal_mode, "filter": flt, "target": "selected"})
            return [], 0, False

        # --- GET PTT ---
        if cmd in ("t", "\\get_ptt"):
            return ["1" if st.get("tx") else "0"], 0, False

        # --- SET PTT ---
        if cmd in ("T", "\\set_ptt") and len(args) >= 1:
            on = args[0] not in ("0", "false", "OFF", "off")
            await self.exec_radio_cmd(slot, "set_ptt", {"tx": on, "source": "hamlib"})
            return [], 0, False

        # --- GET VFO ---
        if cmd in ("v", "\\get_vfo"):
            if st.get("supports_dual_rx"):
                return ["Sub" if st.get("active_band") == "SUB" else "Main"], 0, False
            return ["VFOB" if st.get("active_band") == "SUB" else "VFOA"], 0, False

        # --- SET VFO ---
        if cmd in ("V", "\\set_vfo") and len(args) >= 1:
            v = args[0].upper()
            band = "SUB" if v in ("VFOB", "SUB", "B") else "MAIN"
            await self.exec_radio_cmd(slot, "set_active_band", {"band": band})
            return [], 0, False

        # --- GET SPLIT VFO ---
        if cmd in ("s", "\\get_split_vfo"):
            if not st.get("split"):
                return ["0", "None"], 0, False
            tx_vfo = "Sub" if st.get("supports_dual_rx") else "VFOB"
            return ["1", tx_vfo], 0, False

        # --- SET SPLIT VFO ---
        if cmd in ("S", "\\set_split_vfo") and len(args) >= 1:
            on = args[0] not in ("0", "false")
            await self.exec_radio_cmd(slot, "set_split", {"split": on})
            return [], 0, False

        # --- GET SPLIT FREQ ---
        if cmd in ("i", "\\get_split_freq"):
            other = st.get("main" if active_key == "sub" else "sub", {})
            return [str(int(other.get("freq_hz", 0)))], 0, False

        # --- SET SPLIT FREQ ---
        if cmd in ("I", "\\set_split_freq") and len(args) >= 1:
            hz = int(round(float(args[0])))
            await self.exec_radio_cmd(slot, "set_freq", {"freq_hz": hz, "target": "unselected"})
            return [], 0, False

        # --- GET SPLIT MODE ---
        if cmd in ("x", "\\get_split_mode"):
            other = st.get("main" if active_key == "sub" else "sub", {})
            m = other.get("mode", "USB")
            if other.get("data_mode") and m in ("USB", "LSB", "FM", "DIGI"):
                h_mode = TO_HAMLIB_MODE.get("DIGI" if m == "USB" else m, "PKTUSB")
            else:
                h_mode = TO_HAMLIB_MODE.get(m, "USB")
            pb = int(other.get("filter_hz", 2400))
            return [h_mode, str(pb)], 0, False

        # --- SET SPLIT MODE ---
        if cmd in ("X", "\\set_split_mode") and len(args) >= 1:
            h_mode = args[0].upper()
            pb = int(float(args[1])) if len(args) >= 2 else -1
            internal_mode = FROM_HAMLIB_MODE.get(h_mode, h_mode)
            flt = 3 if (0 < pb <= 1800) else (2 if (0 < pb <= 2400) else 1)
            await self.exec_radio_cmd(slot, "set_mode", {"mode": internal_mode, "filter": flt, "target": "unselected"})
            return [], 0, False

        # --- GET VFO INFO ---
        if cmd == "\\get_vfo_info":
            tgt = vfo_state
            if args:
                vu = args[0].upper()
                if vu in ("VFOB", "SUB"):
                    tgt = st.get("sub", tgt)
                elif vu in ("VFOA", "MAIN"):
                    tgt = st.get("main", tgt)
            m = tgt.get("mode", "USB")
            h_mode = TO_HAMLIB_MODE.get("DIGI" if (tgt.get("data_mode") and m == "USB") else m, "USB")
            return [
                str(int(tgt.get("freq_hz", 0))),
                h_mode,
                str(int(tgt.get("filter_hz", 2400))),
                "1" if st.get("split") else "0",
                "0",
            ], 0, False

        # --- GET LEVEL ---
        if cmd in ("l", "\\get_level") and len(args) >= 1:
            lvl = args[0].upper()
            if lvl == "STRENGTH":
                s_u = float(vfo_state.get("s_units", 0.0))
                db_rel_s9 = round((s_u - 9.0) * (10.0 if s_u > 9.0 else 6.0), 1)
                return [str(db_rel_s9)], 0, False
            if lvl == "RAWSTR":
                return [str(int(vfo_state.get("s_raw", 0)))], 0, False
            if lvl == "KEYSPD":
                return [str(int(st.get("cw_wpm", 20)))], 0, False
            if lvl == "RFPOWER":
                return [f"{st.get('tx_power', 50) / 100.0:.2f}"], 0, False
            if lvl == "SQL":
                return [f"{st.get('sql', 0) / 100.0:.2f}"], 0, False
            if lvl == "AF":
                return [f"{st.get('af_gain', 50) / 100.0:.2f}"], 0, False
            if lvl == "SWR":
                return [f"{st.get('swr', 1.0):.2f}"], 0, False
            return ["0"], 0, False

        # --- SET LEVEL ---
        if cmd in ("L", "\\set_level") and len(args) >= 2:
            lvl = args[0].upper()
            if lvl == "KEYSPD":
                await self.exec_radio_cmd(slot, "set_level", {"name": "cw_wpm", "value": int(round(float(args[1])))})
                return [], 0, False
            val = max(0, min(100, int(round(float(args[1]) * 100))))
            map_name = {"RFPOWER": "tx_power", "SQL": "sql", "AF": "af_gain", "RF": "rf_gain"}.get(lvl)
            if map_name:
                await self.exec_radio_cmd(slot, "set_level", {"name": map_name, "value": val})
            return [], 0, False

        # --- WSJT-X / N1MM HANDSHAKES ---
        if cmd == "\\chk_vfo":
            return ["0"], 0, False

        if cmd == "\\get_powerstat":
            return ["1" if st.get("connected") else "0"], 0, False

        if cmd == "\\set_powerstat":
            return [], 0, False

        if cmd in ("_", "\\get_info"):
            model = st.get("usb_model") or f"Radio {slot}"
            return [f"Icom {model} ({st.get('com_port', '')})"], 0, False

        if cmd in ("\\dump_state", "\\dump_caps"):
            lines = [
                "1",      # protocol version
                "3073",   # generic Icom CI-V rig model ID
                "1",      # ITU region
                "1800000.000000 60000000.000000 0x1ff -1 -1 0x3 0xf",
                "144000000.000000 148000000.000000 0x1ff -1 -1 0x3 0xf",
                "430000000.000000 440000000.000000 0x1ff -1 -1 0x3 0xf",
                "0 0 0 0 0 0 0",
                "1800000.000000 60000000.000000 0x1ff 5000 100000 0x3 0xf",
                "144000000.000000 148000000.000000 0x1ff 5000 100000 0x3 0xf",
                "430000000.000000 440000000.000000 0x1ff 5000 100000 0x3 0xf",
                "0 0 0 0 0 0 0",
                "0x1ff 1",
                "0x1ff 0",
                "0 0",
                "0x1e 2400",
                "0x1e 1800",
                "0x2 500",
                "0x10 15000",
                "0 0",
                "9990",
                "9990",
                "0",
                "0",
                "1",
                "1",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "0",
                "ptt_type=0x1",
                "has_set_vfo=1",
                "has_get_vfo=1",
                "has_set_freq=1",
                "has_get_freq=1",
                "has_get_info=1",
                "done",
            ]
            return lines, 0, False

        if cmd == "\\get_lock_mode":
            # Hamlib 4.x rigctld appends RPRT 0 after lock value even without + prefix
            return ["0"], 0, True

        if cmd == "\\set_lock_mode":
            return [], 0, False

        return [], -4, False

    async def _handle_binary_civ_client(
        self,
        initial: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        slot: int,
    ) -> None:
        """Transparently bridge raw binary Icom CI-V TCP frames (N1MM Port=TCP -> IC-7300/IC-9700)."""
        buf = bytearray(initial)
        while True:
            while True:
                start = buf.find(b"\xfe\xfe")
                if start < 0:
                    buf.clear()
                    break
                if start > 0:
                    del buf[:start]
                while len(buf) >= 3 and buf[2] == 0xFE:
                    del buf[0]
                end = buf.find(b"\xfd", 2)
                if end < 0:
                    if len(buf) > 4096:
                        buf.clear()
                    break
                frm = bytes(buf[: end + 1])
                del buf[: end + 1]
                if len(frm) >= 6 and self.exec_raw_civ:
                    reply = await self.exec_raw_civ(slot, frm)
                    if reply:
                        writer.write(reply)
                        await writer.drain()

            chunk = await reader.read(2048)
            if not chunk:
                break
            buf.extend(chunk)
