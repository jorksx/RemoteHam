"""Central coordinator tying together USB CI-V radios, audio, Hamlib, Desktop UI, Mobile HTTPS & Remote Link."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import aiohttp
from aiohttp import web

import config as cfg_mod
import tls_util
import usb_discovery
from audio_engine import AudioEngine, sql_pct_to_s_pct
from hamlib_server import HamlibServerManager
from ptt_hook import GlobalPttMonitor
from radio_civ import IcomCivRadio
from remote_link import RemoteLinkClient, RemoteLinkServer
from repeaters import RepeaterDatabase

log = logging.getLogger("controller")


class MobileSession:
    """Independent per-phone audio & SQL state so mobile adjustments never affect Windows."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.listen_radios: Set[int] = set()  # both radios OFF/muted by default
        self.mobile_sql: int = 15
        self.mobile_af: int = 85
        self.sql_open_until: Dict[int, float] = {1: 0.0, 2: 0.0}
        self.sample_rate: int = 48000
        self.last_seen: float = time.monotonic()


class AppController:
    def __init__(self) -> None:
        self.cfg: Dict[str, Any] = cfg_mod.load_config()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        self.audio = AudioEngine()
        self.radios: Dict[int, IcomCivRadio] = {}
        self.mirrored_states: Dict[int, Dict[str, Any]] = {}
        self.mirrored_voice_memory: Optional[Dict[str, Any]] = None
        self.mirrored_dtmf: Optional[Dict[str, Any]] = None

        self.ui_clients: Set[web.WebSocketResponse] = set()
        self.mobile_clients: Dict[web.WebSocketResponse, MobileSession] = {}
        self._prev_mobile_suppressed: bool = False

        self.hamlib = HamlibServerManager(
            get_radio_state=self.get_radio_state,
            exec_radio_cmd=self.execute_radio_command,
            exec_raw_civ=self.execute_raw_civ,
        )
        self.link_server = RemoteLinkServer(
            get_full_state=self.get_full_state,
            exec_cmd=self.execute_radio_command,
            on_tx_audio=self._on_client_pc_tx_audio,
        )
        self.link_client = RemoteLinkClient(
            on_remote_state=self._on_remote_state_received,
            on_remote_scope=self._on_remote_scope_received,
            on_rx_audio=self._on_remote_rx_audio,
        )
        self.ptt_hook = GlobalPttMonitor(on_ptt=self._on_hotkey_ptt)
        self.repeaters = RepeaterDatabase()
        self._1750_started_at: Dict[int, float] = {}
        self._1750_release_tasks: Dict[int, asyncio.Task] = {}
        self._dtmf_task: Optional[asyncio.Task] = None
        self._dtmf_tx_slot: int = 0

        # Voice Memory auto-repeat Timer state (Host authority)
        self._vm_timer_enabled: bool = False
        self._vm_timer_sec: int = max(1, min(10, int(self.cfg.get("ui", {}).get("vm_timer_sec", 5) or 5)))
        self._vm_timer_slot: int = 0
        self._vm_timer_radio: int = 1
        self._vm_timer_remaining: float = 0.0
        self._vm_timer_task: Optional[asyncio.Task] = None

        self.audio.passthrough_mic_ignored = bool(self.cfg.get("ui", {}).get("passthrough_mic_ignored", True))

        self._state_dirty = False
        self._broadcaster_task: Optional[asyncio.Task] = None
        self._update_check_task: Optional[asyncio.Task] = None
        self.update_status: Dict[str, Any] = {
            "checked": False,
            "update_available": False,
            "local_sha": "",
            "remote_sha": "",
            "remote_msg": "",
            "repo": str(self.cfg.get("github_repo", "jorksx/RemoteHam")),
            "error": "",
        }
        self.request_restart_callback: Optional[Callable[[], None]] = None

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()

        self.radios = {
            1: IcomCivRadio(1, on_state_change=self._on_radio_state_changed, on_scope_frame=self._on_scope_frame),
            2: IcomCivRadio(2, on_state_change=self._on_radio_state_changed, on_scope_frame=self._on_scope_frame),
        }
        self.mirrored_states = {
            1: json.loads(json.dumps(self.radios[1].state)),
            2: json.loads(json.dumps(self.radios[2].state)),
        }

        self.audio.on_radio_rx_frame = self._on_local_radio_rx_frame
        self.audio.on_client_tx_frame = self._on_local_client_tx_frame
        self.audio.on_voice_play_finished = self._on_voice_play_finished
        self.audio.start(self.loop, self.cfg)

        await self._apply_runtime_mode()
        self.ptt_hook.start()
        asyncio.create_task(self.repeaters.ensure_fresh(force=False))
        self._broadcaster_task = asyncio.create_task(self._state_broadcast_loop())
        self._update_check_task = asyncio.create_task(self._background_update_loop())

    async def stop(self) -> None:
        self._cancel_vm_timer_countdown(disable=True)
        await self._abort_dtmf_tx(release_ptt=False)
        if self._update_check_task:
            self._update_check_task.cancel()
            try:
                await self._update_check_task
            except asyncio.CancelledError:
                pass
        if self._broadcaster_task:
            self._broadcaster_task.cancel()
            try:
                await self._broadcaster_task
            except asyncio.CancelledError:
                pass
        await self.ptt_hook.stop()
        await self.hamlib.stop()
        await self.link_server.stop()
        await self.link_client.stop()
        for r in self.radios.values():
            await r.stop()
        self.audio.stop()

    async def _apply_runtime_mode(self) -> None:
        mode = self.cfg.get("mode", "host")
        self.audio.apply_config(self.cfg)
        self._sync_ptt_hook_cfg()
        await self.hamlib.start(self.cfg)

        if mode == "host":
            await self.link_client.stop()
            ports_info = {p["port"].upper(): p for p in usb_discovery.list_serial_ports_with_usb()}
            for rcfg in self.cfg.get("radios", []):
                slot = int(rcfg.get("slot", 1))
                radio = self.radios[slot]
                if rcfg.get("enabled", True) and rcfg.get("com_port"):
                    com = rcfg["com_port"]
                    baud = int(rcfg.get("baud_rate", 115200))
                    meta = ports_info.get(com.upper())
                    await radio.start(com, baud, usb_meta=meta)
                else:
                    await radio.stop()

            srv_cfg = self.cfg.get("server", {})
            if srv_cfg.get("enabled", False):
                await self.link_server.start(int(srv_cfg.get("port", 8090)))
            else:
                await self.link_server.stop()
        else:
            # Client mode: stop local COM ports & server, connect to remote server IP:port
            for r in self.radios.values():
                await r.stop()
            await self.link_server.stop()
            cl = self.cfg.get("client", {})
            await self.link_client.start(
                cl.get("server_ip", "127.0.0.1"),
                int(cl.get("server_port", 8090)),
            )

        self._state_dirty = True

    async def update_config(self, new_cfg: Dict[str, Any]) -> Dict[str, Any]:
        self.cfg = cfg_mod.deep_merge(self.cfg, new_cfg)
        cfg_mod.save_config(self.cfg)
        await self._apply_runtime_mode()
        return self.cfg

    def get_radio_state(self, slot: int) -> Dict[str, Any]:
        if self.cfg.get("mode") == "client":
            return self.mirrored_states.get(slot, {})
        r = self.radios.get(slot)
        return r.state if r else {}

    async def execute_raw_civ(self, slot: int, raw_frame: bytes) -> Optional[bytes]:
        radio = self.radios.get(slot)
        if not radio:
            return None
        return await radio.raw_civ_exchange(raw_frame)

    def get_full_state(self) -> Dict[str, Any]:
        mode = self.cfg.get("mode", "host")
        r1 = self.get_radio_state(1)
        r2 = self.get_radio_state(2)
        return {
            "mode": mode,
            "radios": {"1": r1, "2": r2},
            "audio_routing": self.cfg.get("audio_routing", {}),
            "windows_mute": dict(self.audio.windows_mute),
            "windows_volume": dict(self.audio.windows_volume),
            "windows_sql": dict(self.audio.windows_sql),
            "windows_suppressed_by_mobile": self.audio.is_windows_suppressed_by_mobile(),
            "ptt_global": bool(self.cfg.get("ptt_global", False)),
            "ptt_binds": {
                "1": (self.cfg.get("radios", [{}, {}])[0].get("ptt_bind") or {"vk": 0, "label": ""}),
                "2": (self.cfg.get("radios", [{}, {}])[1].get("ptt_bind") or {"vk": 0, "label": ""}),
            },
            "hamlib_ports": self.hamlib.get_active_ports(),
            "audio_status": {
                "pc_out_ok": bool(self.audio._pc_out_stream),
                "pc_out_dev": self.audio.pc_out_device_name,
                "r1_in_ok": 1 in self.audio._radio_in_streams,
                "r2_in_ok": 2 in self.audio._radio_in_streams,
                "r1_rms": round(self.audio.rx_rms.get(1, 0.0), 4),
                "r2_rms": round(self.audio.rx_rms.get(2, 0.0), 4),
            },
            "voice_memory": self._get_voice_memory_payload(mode),
            "dtmf": self._get_dtmf_payload(mode),
            "qso_rec": self.audio.get_qso_rec_status(),
            "passthrough": self.audio.get_passthrough_status(),
            "update": dict(self.update_status),
            "qth_locator": str(self.cfg.get("qth_locator", "KP20")),
            "ui": dict(self.cfg.get("ui", {"dual_view": False, "wf_track": False, "cw_macros": ["", "", "", ""], "smeter_style": 0})),
            "server": {
                "enabled": bool(self.cfg.get("server", {}).get("enabled", False)),
                "running": self.link_server.running,
                "port": int(self.cfg.get("server", {}).get("port", 8090)),
                "clients": self.link_server.client_count(),
            },
            "client": {
                "server_ip": self.cfg.get("client", {}).get("server_ip", ""),
                "server_port": int(self.cfg.get("client", {}).get("server_port", 8090)),
                "connected": self.link_client.connected,
                "error": self.link_client.error,
            },
            "mobile": {
                "enabled": True,
                "port": int(self.cfg.get("mobile", {}).get("port", 8085)),
                "clients": len(self.mobile_clients),
                "urls": [f"https://{ip}:{int(self.cfg.get('mobile', {}).get('port', 8085))}" for ip in tls_util.get_local_ipv4_addresses()],
            },
        }

    def _sync_ptt_hook_cfg(self) -> None:
        radios_cfg = self.cfg.get("radios", [{}, {}])
        b1 = int((radios_cfg[0].get("ptt_bind") or {}).get("vk", 0)) if len(radios_cfg) > 0 else 0
        b2 = int((radios_cfg[1].get("ptt_bind") or {}).get("vk", 0)) if len(radios_cfg) > 1 else 0
        self.ptt_hook.configure(bool(self.cfg.get("ptt_global", False)), b1, b2)

    async def _on_hotkey_ptt(self, slot: int, down: bool) -> None:
        await self.execute_radio_command(slot, "set_ptt", {"tx": down, "source": "pc"})

    # --- Radio Command Execution (from Desktop UI, Mobile UI, Hamlib, or Remote PC Client) ---

    async def execute_radio_command(self, slot: int, action: str, params: Dict[str, Any]) -> None:
        # Windows-local audio mute / routing / PTT-bind actions are handled locally even in client mode
        if action == "set_ptt_global":
            self.cfg["ptt_global"] = bool(params.get("ptt_global", False))
            cfg_mod.save_config(self.cfg)
            self._sync_ptt_hook_cfg()
            self._state_dirty = True
            return

        if action == "set_ptt_bind":
            vk = int(params.get("vk", 0) or 0)
            label = str(params.get("label", ""))
            for rc in self.cfg.get("radios", []):
                if int(rc.get("slot", 0)) == slot:
                    rc["ptt_bind"] = {"vk": vk, "label": label}
            cfg_mod.save_config(self.cfg)
            self._sync_ptt_hook_cfg()
            self._state_dirty = True
            return

        if action == "set_binding_mode":
            self.ptt_hook.binding_in_progress = bool(params.get("active", False))
            return

        if action == "set_ui_focus":
            self.ptt_hook.ui_focused = bool(params.get("focused", True))
            return

        if action == "set_windows_mute":
            mute = bool(params.get("mute", False))
            self.audio.windows_mute[slot] = mute
            for rc in self.cfg.get("radios", []):
                if int(rc.get("slot", 0)) == slot:
                    rc["win_mute"] = mute
            cfg_mod.save_config(self.cfg)
            self._state_dirty = True
            return

        if action == "set_windows_volume":
            vol = max(0, min(100, int(params.get("volume", 85))))
            self.audio.windows_volume[slot] = vol
            for rc in self.cfg.get("radios", []):
                if int(rc.get("slot", 0)) == slot:
                    rc["win_volume"] = vol
            cfg_mod.save_config(self.cfg)
            self._state_dirty = True
            return

        if action == "set_windows_sql":
            sql = max(0, min(100, int(params.get("sql", 15))))
            self.audio.windows_sql[slot] = sql
            for rc in self.cfg.get("radios", []):
                if int(rc.get("slot", 0)) == slot:
                    rc["sql_threshold"] = sql
            cfg_mod.save_config(self.cfg)
            self._state_dirty = True
            return

        if action == "set_ui_prefs":
            ui = self.cfg.setdefault("ui", {"dual_view": False, "wf_track": False, "cw_macros": ["", "", "", ""], "smeter_style": 0})
            if "dual_view" in params:
                ui["dual_view"] = bool(params["dual_view"])
            if "wf_track" in params:
                ui["wf_track"] = bool(params["wf_track"])
            if "cw_macros" in params and isinstance(params["cw_macros"], list):
                ui["cw_macros"] = [str(x or "")[:60] for x in (params["cw_macros"] + ["", "", "", ""])[:4]]
            if "smeter_style" in params:
                ui["smeter_style"] = max(0, min(3, int(params["smeter_style"] or 0)))
            cfg_mod.save_config(self.cfg)
            self._state_dirty = True
            return

        if action == "qso_rec_toggle":
            if self.audio.qso_rec_active:
                self.audio.stop_qso_rec()
            else:
                self.audio.start_qso_rec()
            self._state_dirty = True
            return

        if action == "passthrough_toggle":
            if "enabled" in params:
                self.audio.set_passthrough(bool(params.get("enabled")))
            else:
                self.audio.set_passthrough(not self.audio.passthrough_active)
            self._state_dirty = True
            return

        if action == "set_passthrough_mic":
            ignored = bool(params.get("mic_ignored", True))
            self.audio.set_passthrough_mic_ignored(ignored)
            self.cfg.setdefault("ui", {})["passthrough_mic_ignored"] = ignored
            cfg_mod.save_config(self.cfg)
            self._state_dirty = True
            return

        if action == "set_audio_routing":
            ar = self.cfg.setdefault("audio_routing", {})
            if "radio1_channel" in params:
                ar["radio1_channel"] = params["radio1_channel"]
            if "radio2_channel" in params:
                ar["radio2_channel"] = params["radio2_channel"]
            if "tx_interlock" in params:
                ar["tx_interlock"] = bool(params["tx_interlock"])
            cfg_mod.save_config(self.cfg)
            self.audio.apply_config(self.cfg)
            self._state_dirty = True
            return

        if self.cfg.get("mode") == "client":
            await self.link_client.send_command(slot, action, params)
            return

        radio = self.radios.get(slot)
        if not radio:
            return

        if action == "set_freq":
            await radio.set_frequency(int(params["freq_hz"]), params.get("target", "selected"))
        elif action == "set_mode":
            await radio.set_mode(str(params["mode"]), params.get("filter"), params.get("target", "selected"))
        elif action == "set_ptt":
            tx_on = bool(params.get("tx", False))
            src = str(params.get("source", "pc"))
            # Manual PTT immediately halts any active Voice Memory, repeat timer, CW macro, or SWR scan
            if src != "voice_mem":
                if tx_on:
                    self._cancel_vm_timer_countdown(disable=True)
                if self.audio.voice_play_active:
                    self.audio.stop_voice_play()
                    self._state_dirty = True
            if radio._cw_task is not None and not radio._cw_task.done():
                await radio.abort_cw_macro(release_ptt=not tx_on)
            if radio._swr_task is not None and not radio._swr_task.done():
                await radio.abort_swr_scan()
            if self._dtmf_task is not None and not self._dtmf_task.done():
                await self._abort_dtmf_tx(release_ptt=not tx_on)
            if tx_on and self.cfg.get("audio_routing", {}).get("tx_interlock", True):
                other = self.radios.get(2 if slot == 1 else 1)
                if other and other.state.get("tx"):
                    await other.set_ptt(False)
            self.audio.active_tx_source = src
            if src != "pc":
                self.audio._radio_tx_rings[slot].clear()
            if not tx_on:
                self.audio.set_tone_1750(slot, False)
                self.audio._radio_tx_rings[slot].clear()
            await radio.set_ptt(tx_on)
        elif action == "cw_send":
            macro_idx = int(params.get("macro", 0))
            txt = str(params.get("text", ""))
            if self.cfg.get("audio_routing", {}).get("tx_interlock", True):
                other = self.radios.get(2 if slot == 1 else 1)
                if other and other.state.get("tx"):
                    await other.abort_cw_macro(release_ptt=True)
                    await other.set_ptt(False)
            await radio.send_cw_macro(macro_idx, txt)
        elif action == "cw_abort":
            await radio.abort_cw_macro(release_ptt=True)
        elif action == "voice_rec_start":
            self._cancel_vm_timer_countdown()
            self.audio.start_voice_rec(int(params.get("mem", 1)))
            self._state_dirty = True
        elif action == "voice_rec_stop":
            self.audio.stop_voice_rec()
            self._state_dirty = True
        elif action == "voice_timer_toggle":
            self._vm_timer_enabled = not self._vm_timer_enabled
            if not self._vm_timer_enabled:
                self._cancel_vm_timer_countdown(disable=True)
                if self.audio.voice_play_active:
                    stopped_r = self.audio.stop_voice_play()
                    if stopped_r in self.radios:
                        await self.radios[stopped_r].set_ptt(False)
            elif self.audio.voice_play_active and self.audio.voice_play_slot in (1, 2, 3, 4):
                self._vm_timer_slot = self.audio.voice_play_slot
                self._vm_timer_radio = self.audio.voice_play_radio or slot
            self._state_dirty = True
        elif action == "voice_timer_interval":
            sec = max(1, min(10, int(params.get("sec", 5) or 5)))
            self._vm_timer_sec = sec
            self.cfg.setdefault("ui", {})["vm_timer_sec"] = sec
            cfg_mod.save_config(self.cfg)
            if self._vm_timer_remaining > sec:
                self._vm_timer_remaining = float(sec)
            self._state_dirty = True
        elif action == "voice_play":
            mem = int(params.get("mem", 1))
            if self.audio.voice_play_active and self.audio.voice_play_slot == mem:
                self._cancel_vm_timer_countdown()
                stopped_r = self.audio.stop_voice_play()
                if stopped_r in self.radios:
                    await self.radios[stopped_r].set_ptt(False)
            else:
                await self._trigger_voice_play(slot, mem)
            self._state_dirty = True
        elif action == "voice_stop":
            self._cancel_vm_timer_countdown()
            stopped_r = self.audio.stop_voice_play()
            if stopped_r in self.radios:
                await self.radios[stopped_r].set_ptt(False)
            self._state_dirty = True
        elif action == "set_1750":
            on = bool(params.get("active", False))
            pending = self._1750_release_tasks.pop(slot, None)
            if pending and not pending.done():
                pending.cancel()
            if on:
                if self.cfg.get("audio_routing", {}).get("tx_interlock", True):
                    other = self.radios.get(2 if slot == 1 else 1)
                    if other and other.state.get("tx"):
                        self.audio.set_tone_1750(2 if slot == 1 else 1, False)
                        await other.set_ptt(False)
                self._1750_started_at[slot] = time.monotonic()
                self.audio.set_tone_1750(slot, True)
                await radio.set_ptt(True)
            else:
                elapsed = time.monotonic() - self._1750_started_at.get(slot, 0.0)
                if 0.0 < elapsed < 0.65:
                    wait_s = 0.65 - elapsed
                    async def _delayed_1750_off(s: int = slot, r: Any = radio, w: float = wait_s) -> None:
                        try:
                            await asyncio.sleep(w)
                            self.audio.set_tone_1750(s, False)
                            await r.set_ptt(False)
                        except asyncio.CancelledError:
                            pass
                    self._1750_release_tasks[slot] = asyncio.create_task(_delayed_1750_off())
                else:
                    self.audio.set_tone_1750(slot, False)
                    await radio.set_ptt(False)
        elif action == "tune_repeater":
            repeater = params.get("repeater") or {}
            target = str(params.get("target", "selected"))
            if hasattr(radio, "tune_repeater"):
                await radio.tune_repeater(repeater, target=target)
        elif action == "send_dtmf":
            digits = str(params.get("digits", "")).strip()
            await self._start_dtmf_tx(slot, digits)
        elif action == "abort_dtmf":
            await self._abort_dtmf_tx(release_ptt=True)
        elif action == "set_active_band":
            await radio.set_active_band(str(params.get("band", "MAIN")))
        elif action == "swap_vfo":
            await radio.swap_vfo()
        elif action == "equalize_vfo":
            await radio.equalize_vfo()
        elif action == "set_rit_hz":
            await radio.set_rit_hz(int(params.get("hz", 0)))
        elif action == "set_dualwatch":
            await radio.set_dualwatch(bool(params.get("dualwatch", False)))
        elif action == "set_split":
            await radio.set_split(bool(params.get("split", False)))
        elif action == "set_level":
            name = str(params.get("name", ""))
            val = int(params.get("value", 0))
            await radio.set_level(name, val)
            if name == "sql":
                for rc in self.cfg.get("radios", []):
                    if int(rc.get("slot", 0)) == slot:
                        rc["sql_threshold"] = val
                cfg_mod.save_config(self.cfg)
        elif action == "set_dsp":
            await radio.set_dsp_toggle(str(params.get("name", "")), params.get("value"))
        elif action == "reset_pbt":
            await radio.reset_pbt()
        elif action == "set_tuner":
            await radio.set_tuner(bool(params.get("on", False)))
        elif action == "start_tune":
            await radio.start_tune()
        elif action == "set_scope_span":
            await radio.set_scope_span(int(params.get("span_hz", 100000)))
        elif action == "swr_scan_start":
            self._cancel_vm_timer_countdown(disable=True)
            if self.audio.voice_play_active:
                self.audio.stop_voice_play()
            if self.cfg.get("audio_routing", {}).get("tx_interlock", True):
                other = self.radios.get(2 if slot == 1 else 1)
                if other and other.state.get("tx"):
                    await other.abort_swr_scan()
                    await other.abort_cw_macro(release_ptt=True)
                    await other.set_ptt(False)
            def _mute_cb(muted: bool, _s: int = slot) -> None:
                self.audio.swr_scan_mute_slot = _s if muted else 0
            await radio.start_swr_scan(
                center_hz=int(params.get("center_hz", 0) or 0),
                span_hz=int(params.get("span_hz", 0) or 0),
                on_scan_audio_mute=_mute_cb,
            )
            self._state_dirty = True
        elif action == "swr_scan_abort":
            await radio.abort_swr_scan()
            self._state_dirty = True
        elif action == "swr_scan_clear":
            radio.clear_swr_scan()
            self._state_dirty = True

    # --- Internal Callbacks ---

    def _get_voice_memory_payload(self, mode: str) -> Dict[str, Any]:
        if mode == "client" and isinstance(self.mirrored_voice_memory, dict):
            return dict(self.mirrored_voice_memory)
        vm = self.audio.get_voice_memory_status()
        vm["timer_enabled"] = self._vm_timer_enabled
        vm["timer_sec"] = self._vm_timer_sec
        vm["timer_slot"] = self._vm_timer_slot
        vm["timer_remaining"] = round(max(0.0, self._vm_timer_remaining), 1)
        return vm

    def _get_dtmf_payload(self, mode: str) -> Dict[str, Any]:
        if mode == "client" and isinstance(self.mirrored_dtmf, dict):
            return dict(self.mirrored_dtmf)
        active = bool(self._dtmf_task is not None and not self._dtmf_task.done())
        return {"active": active, "slot": self._dtmf_tx_slot if active else 0}

    async def _abort_dtmf_tx(self, release_ptt: bool = True) -> None:
        self.audio.stop_dtmf_play()
        task = self._dtmf_task
        slot_was = self._dtmf_tx_slot
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1.5)
            except Exception:
                pass
        self._dtmf_task = None
        self._dtmf_tx_slot = 0
        if release_ptt and slot_was in self.radios:
            try:
                await self.radios[slot_was].set_ptt(False)
            except Exception:
                pass
        self._state_dirty = True

    async def _start_dtmf_tx(self, slot: int, digits: str) -> None:
        radio = self.radios.get(slot)
        if not radio or not radio.state.get("connected"):
            return
        await self._abort_dtmf_tx(release_ptt=False)
        self._cancel_vm_timer_countdown(disable=True)
        if self.audio.voice_play_active:
            self.audio.stop_voice_play()
        if radio._cw_task is not None and not radio._cw_task.done():
            await radio.abort_cw_macro(release_ptt=True)
        if radio._swr_task is not None and not radio._swr_task.done():
            await radio.abort_swr_scan()
        if self.cfg.get("audio_routing", {}).get("tx_interlock", True):
            other = self.radios.get(2 if slot == 1 else 1)
            if other and other.state.get("tx"):
                await other.abort_cw_macro(release_ptt=True)
                await other.set_ptt(False)

        from audio_engine import render_dtmf_sequence
        if render_dtmf_sequence(digits).size == 0:
            return
        self._dtmf_tx_slot = slot
        self._state_dirty = True

        async def _runner() -> None:
            try:
                self.audio.active_tx_source = "dtmf"
                self.audio.set_dtmf_standby(slot)
                await radio.set_ptt(True)
                dur_s = self.audio.start_dtmf_play(slot, digits)
                if dur_s > 0.0:
                    await asyncio.sleep(dur_s + 0.08)
            except asyncio.CancelledError:
                pass
            finally:
                self.audio.stop_dtmf_play()
                try:
                    await radio.set_ptt(False)
                except Exception:
                    pass
                if self._dtmf_tx_slot == slot:
                    self._dtmf_tx_slot = 0
                self._state_dirty = True

        self._dtmf_task = asyncio.create_task(_runner())

    @staticmethod
    def _current_async_task() -> Optional[asyncio.Task]:
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    def _cancel_vm_timer_countdown(self, disable: bool = False) -> None:
        task = self._vm_timer_task
        cur_task = self._current_async_task()
        if task is not None and not task.done() and task is not cur_task:
            task.cancel()
        self._vm_timer_task = None
        self._vm_timer_remaining = 0.0
        self._vm_timer_slot = 0
        if disable:
            self._vm_timer_enabled = False

    async def _trigger_voice_play(self, slot: int, mem: int) -> bool:
        radio = self.radios.get(slot)
        if not radio:
            return False
        cur_task = self._current_async_task()
        if self._vm_timer_task is not None and not self._vm_timer_task.done() and self._vm_timer_task is not cur_task:
            self._vm_timer_task.cancel()
        self._vm_timer_task = None
        self._vm_timer_remaining = 0.0

        if self.audio.voice_play_active:
            old_r = self.audio.stop_voice_play()
            if old_r in self.radios and old_r != slot:
                await self.radios[old_r].set_ptt(False)
        if self.cfg.get("audio_routing", {}).get("tx_interlock", True):
            other = self.radios.get(2 if slot == 1 else 1)
            if other and other.state.get("tx"):
                await other.set_ptt(False)

        if self.audio.start_voice_play(slot, mem):
            if self._vm_timer_enabled:
                self._vm_timer_slot = mem
                self._vm_timer_radio = slot
            else:
                self._vm_timer_slot = 0
            self.audio.active_tx_source = "voice_mem"
            await radio.set_ptt(True)
            self._state_dirty = True
            return True
        return False

    async def _vm_countdown_and_retrigger(self) -> None:
        try:
            deadline = time.monotonic() + float(max(1, min(10, self._vm_timer_sec)))
            while self._vm_timer_enabled and self._vm_timer_slot in (1, 2, 3, 4):
                rem = deadline - time.monotonic()
                if rem <= 0.0:
                    break
                self._vm_timer_remaining = rem
                self._state_dirty = True
                await asyncio.sleep(min(0.1, rem))
            self._vm_timer_remaining = 0.0
            self._vm_timer_task = None
            if self._vm_timer_enabled and self._vm_timer_slot in (1, 2, 3, 4):
                await self._trigger_voice_play(self._vm_timer_radio, self._vm_timer_slot)
            else:
                self._state_dirty = True
        except asyncio.CancelledError:
            self._vm_timer_remaining = 0.0

    def _on_radio_state_changed(self, slot: int) -> None:
        radio = self.radios.get(slot)
        if radio:
            self.audio.update_radio_state(slot, radio.state)
        self._state_dirty = True

    def _on_voice_play_finished(self, slot: int) -> None:
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._finish_voice_tx(slot), self.loop)

    async def _finish_voice_tx(self, slot: int) -> None:
        if not self.audio.voice_play_active:
            radio = self.radios.get(slot)
            if radio and radio.state.get("tx"):
                await radio.set_ptt(False)
            if self._vm_timer_enabled and self._vm_timer_slot in (1, 2, 3, 4):
                if self._vm_timer_task is not None and not self._vm_timer_task.done():
                    self._vm_timer_task.cancel()
                self._vm_timer_task = asyncio.create_task(self._vm_countdown_and_retrigger())
        self._state_dirty = True

    def _on_scope_frame(self, slot: int, rx: int, bins: List[float], center_hz: int, span_hz: int) -> None:
        if not self.loop:
            return
        payload = {
            "type": "scope",
            "slot": slot,
            "rx": rx,
            "center_hz": center_hz,
            "span_hz": span_hz,
            "bins": [round(x, 3) for x in bins],
        }
        asyncio.run_coroutine_threadsafe(self._broadcast_scope(payload), self.loop)

    async def _broadcast_scope(self, payload: Dict[str, Any]) -> None:
        text = json.dumps(payload)
        dead_ui = []
        for ws in list(self.ui_clients):
            try:
                await ws.send_str(text)
            except Exception:
                dead_ui.append(ws)
        for ws in dead_ui:
            self.ui_clients.discard(ws)

        for sess in list(self.mobile_clients.values()):
            try:
                await sess.ws.send_str(text)
            except Exception:
                self._drop_mobile_client(sess.ws)

        if self.link_server.running:
            await self.link_server.broadcast_json(payload)

    def _on_local_radio_rx_frame(self, slot: int, pcm_bytes: bytes, rms: float) -> None:
        if not self.loop:
            return
        if not self.mobile_clients and not (self.link_server.running and self.link_server.client_count() > 0):
            return
        asyncio.run_coroutine_threadsafe(
            self._fanout_rx_audio(slot, pcm_bytes, rms),
            self.loop,
        )

    def _drop_mobile_client(self, ws: web.WebSocketResponse) -> None:
        if self.mobile_clients.pop(ws, None) is not None:
            self.audio.set_mobile_connected(bool(self.mobile_clients))
            self._state_dirty = True

    async def _fanout_rx_audio(self, slot: int, pcm_bytes: bytes, rms: float) -> None:
        # 1. Remote PC Clients (Client Mode gets raw stream for FT8/WSJT-X & local speaker)
        if self.link_server.running and self.link_server.client_count() > 0:
            await self.link_server.broadcast_rx_audio(slot, pcm_bytes)

        # 2. Mobile HTTPS Clients (Windows stays muted whenever connected; mobile SQL still gates phone audio)
        if self.mobile_clients:
            now = time.monotonic()
            s_pct = self.audio.radio_s_pct.get(slot, 0.0)
            audio_pct = min(100.0, rms * 450.0)

            for ws, sess in list(self.mobile_clients.items()):
                if ws.closed or (now - sess.last_seen) > 4.5:
                    self._drop_mobile_client(ws)
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    continue
                if slot not in sess.listen_radios:
                    continue

                # Check independent mobile SQL threshold (calibrated: <34 noise SQL, 34=S1, 50=S5, 80=S9+20, 100=Max)
                sql = sess.mobile_sql
                sql_thresh = sql_pct_to_s_pct(sql)
                eff_thresh = max(0.4, sql_thresh)
                r_connected = self.audio.radio_connected.get(slot, False)
                has_hw_sql = self.audio.radio_has_hw_sql.get(slot, False)
                hw_open = self.audio.radio_hw_sql_open.get(slot, False)
                radio_sql = self.audio.radio_hw_sql_level.get(slot, 0)
                if sql <= 1:
                    is_open = True
                elif r_connected:
                    is_open = (s_pct >= eff_thresh) or (has_hw_sql and hw_open and sql <= radio_sql)
                else:
                    is_open = audio_pct >= sql_thresh
                if is_open:
                    sess.sql_open_until[slot] = now + 0.25
                elif now < sess.sql_open_until.get(slot, 0.0):
                    is_open = True

                if is_open:
                    pkt = bytes([slot]) + pcm_bytes
                    try:
                        await asyncio.wait_for(ws.send_bytes(pkt), timeout=0.8)
                    except Exception:
                        self._drop_mobile_client(ws)
                        try:
                            await ws.close()
                        except Exception:
                            pass

    def _on_local_client_tx_frame(self, slot: int, pcm_bytes: bytes) -> None:
        if self.loop and self.cfg.get("mode") == "client":
            asyncio.run_coroutine_threadsafe(
                self.link_client.send_tx_audio(slot, pcm_bytes),
                self.loop,
            )

    def _on_client_pc_tx_audio(self, slot: int, pcm_bytes: bytes) -> None:
        self.audio.inject_remote_tx_pcm(slot, pcm_bytes, source="client")

    def _on_remote_state_received(self, state_data: Dict[str, Any]) -> None:
        radios_map = state_data.get("radios", {})
        for s_key in ("1", "2"):
            slot = int(s_key)
            if s_key in radios_map:
                self.mirrored_states[slot] = radios_map[s_key]
                self.audio.update_radio_state(slot, radios_map[s_key])
        if "voice_memory" in state_data:
            self.mirrored_voice_memory = state_data["voice_memory"]
        if "dtmf" in state_data:
            self.mirrored_dtmf = state_data["dtmf"]
        self._state_dirty = True

    def _on_remote_scope_received(self, payload: Dict[str, Any]) -> None:
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._broadcast_scope(payload), self.loop)

    def _on_remote_rx_audio(self, slot: int, pcm_bytes: bytes) -> None:
        self.audio.inject_client_rx_pcm(slot, pcm_bytes)

    async def _state_broadcast_loop(self) -> None:
        last_sent = 0.0
        while True:
            await asyncio.sleep(0.08)
            now = time.monotonic()
            for ws, sess in list(self.mobile_clients.items()):
                if ws.closed or (now - sess.last_seen) > 4.5:
                    self._drop_mobile_client(ws)
                    try:
                        await ws.close()
                    except Exception:
                        pass
            sup = self.audio.is_windows_suppressed_by_mobile()
            if sup != self._prev_mobile_suppressed:
                self._prev_mobile_suppressed = sup
                self._state_dirty = True
            if not self._state_dirty and (now - last_sent < 0.45):
                continue
            self._state_dirty = False
            last_sent = now

            msg = json.dumps({"type": "state", "data": self.get_full_state()})
            for ws in list(self.ui_clients):
                try:
                    await ws.send_str(msg)
                except Exception:
                    self.ui_clients.discard(ws)

            for ws in list(self.mobile_clients.keys()):
                try:
                    await asyncio.wait_for(ws.send_str(msg), timeout=0.8)
                except Exception:
                    self._drop_mobile_client(ws)

            if self.link_server.running and self.cfg.get("mode") == "host":
                await self.link_server.broadcast_json({"type": "state", "data": self.get_full_state()})

    @staticmethod
    def _read_git_head_sha(root: Path) -> Tuple[str, float]:
        git_dir = root / ".git"
        head_file = git_dir / "HEAD"
        if not head_file.is_file():
            return "", 0.0
        try:
            head_txt = head_file.read_text(encoding="ascii").strip()
            mtime = head_file.stat().st_mtime
            if head_txt.startswith("ref: "):
                ref_rel = head_txt[5:].strip()
                ref_file = git_dir / ref_rel
                if ref_file.is_file():
                    sha = ref_file.read_text(encoding="ascii").strip()
                    mtime = max(mtime, ref_file.stat().st_mtime)
                    return (sha if len(sha) == 40 else ""), mtime
                packed = git_dir / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if line.startswith("#") or " " not in line:
                            continue
                        p_sha, p_ref = line.split(" ", 1)
                        if p_ref.strip() == ref_rel and len(p_sha.strip()) == 40:
                            return p_sha.strip(), max(mtime, packed.stat().st_mtime)
            elif len(head_txt) == 40:
                return head_txt, mtime
        except Exception:
            pass
        return "", 0.0

    def _get_local_github_sha(self) -> str:
        candidates: List[Tuple[float, str]] = []
        sha_paths = [
            cfg_mod.LIVE_APP_DIR / ".github_sha",
            cfg_mod.RESOURCE_DIR / ".github_sha",
            cfg_mod.APP_DIR / ".github_sha",
            Path(__file__).resolve().parent / ".github_sha",
        ]
        for sp in sha_paths:
            if sp.is_file():
                try:
                    val = sp.read_text(encoding="ascii").strip()
                    if len(val) == 40:
                        candidates.append((sp.stat().st_mtime, val))
                except Exception:
                    pass
        if not getattr(sys, "frozen", False):
            for root in (cfg_mod.APP_DIR, Path(__file__).resolve().parent):
                git_sha, git_mtime = self._read_git_head_sha(root)
                if git_sha:
                    candidates.append((git_mtime, git_sha))
        if not candidates:
            return ""
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    async def _background_update_loop(self) -> None:
        await asyncio.sleep(0.5)
        while True:
            try:
                await self.check_app_update()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("Background GitHub update check failed: %s", exc)
            await asyncio.sleep(600)

    async def check_app_update(self) -> Dict[str, Any]:
        """Compare local version SHA against latest GitHub commit without local Host update."""
        repo = str(self.cfg.get("github_repo", "jorksx/RemoteHam") or "jorksx/RemoteHam").strip()
        branch = str(self.cfg.get("github_branch", "main") or "main").strip()
        my_sha = self._get_local_github_sha()
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "RemoteHam-Updater"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                url = f"https://api.github.com/repos/{repo}/commits/{branch}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status != 200:
                        err = f"GitHub returned HTTP {resp.status}"
                        self.update_status = {
                            "checked": True,
                            "update_available": False,
                            "local_sha": my_sha,
                            "remote_sha": "",
                            "remote_msg": "",
                            "repo": repo,
                            "error": err,
                        }
                        self._state_dirty = True
                        return {"ok": False, **self.update_status}
                    info = await resp.json()

                remote_sha = str(info.get("sha") or "").strip()
                remote_msg = str((info.get("commit") or {}).get("message") or "").splitlines()[0].strip()

                update_available = False
                if remote_sha and remote_sha != my_sha:
                    if len(my_sha) == 40:
                        cmp_url = f"https://api.github.com/repos/{repo}/compare/{my_sha}...{remote_sha}"
                        try:
                            async with session.get(cmp_url, timeout=aiohttp.ClientTimeout(total=5)) as c_resp:
                                if c_resp.status == 200:
                                    c_data = await c_resp.json()
                                    update_available = str(c_data.get("status") or "") != "behind"
                                else:
                                    update_available = True
                        except Exception:
                            update_available = True
                    else:
                        update_available = True

            self.update_status = {
                "checked": True,
                "update_available": update_available,
                "local_sha": my_sha,
                "remote_sha": remote_sha,
                "remote_msg": remote_msg[:90],
                "repo": repo,
                "error": "",
            }
            self._state_dirty = True
            return {"ok": True, **self.update_status}
        except Exception as exc:
            self.update_status = {
                "checked": True,
                "update_available": False,
                "local_sha": my_sha,
                "remote_sha": "",
                "remote_msg": "",
                "repo": repo,
                "error": str(exc),
            }
            self._state_dirty = True
            return {"ok": False, **self.update_status}

    @staticmethod
    def _is_updatable_rel_path(rel: str) -> bool:
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            return False
        if len(p.parts) == 1:
            name = p.parts[0]
            return name.endswith(".py") or name in (
                "repeaters_cache.json",
                "README.md",
                "DualRigRemote.spec",
                "start_remote.bat",
            )
        return p.parts[0] == "static" and "__pycache__" not in p.parts

    async def perform_app_update(self) -> Dict[str, Any]:
        """Download latest archive from GitHub, install safely & restart cleanly."""
        status = await self.check_app_update()
        remote_sha = str(status.get("remote_sha") or "").strip()
        repo = str(status.get("repo") or self.cfg.get("github_repo", "jorksx/RemoteHam")).strip()
        if not status.get("ok") or not remote_sha:
            return {"ok": False, "error": status.get("error") or "GitHub update unavailable"}

        zip_url = f"https://codeload.github.com/{repo}/zip/{remote_sha}"
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": "RemoteHam-Updater"}) as session:
                async with session.get(zip_url, timeout=aiohttp.ClientTimeout(total=45)) as resp:
                    if resp.status != 200:
                        return {"ok": False, "error": f"GitHub ZIP HTTP {resp.status}"}
                    zbytes = await resp.read()
        except Exception as exc:
            return {"ok": False, "error": f"Download error: {exc}"}

        extracted: Dict[str, bytes] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(zbytes), "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    parts = Path(info.filename).parts
                    if len(parts) < 2:
                        continue
                    rel_posix = "/".join(parts[1:])
                    if not self._is_updatable_rel_path(rel_posix):
                        continue
                    extracted[rel_posix] = zf.read(info.filename)
        except Exception as exc:
            return {"ok": False, "error": f"Invalid GitHub archive: {exc}"}

        if "app.py" not in extracted:
            return {"ok": False, "error": "GitHub archive missing app.py"}

        is_frozen = bool(getattr(sys, "frozen", False))
        if is_frozen:
            next_dir = cfg_mod.APP_DIR / "live_app_next"
            live_dir = cfg_mod.LIVE_APP_DIR
            try:
                if next_dir.exists():
                    shutil.rmtree(next_dir, ignore_errors=True)
                next_dir.mkdir(parents=True, exist_ok=True)
                for rel_posix, data in extracted.items():
                    dst = next_dir / rel_posix
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(data)
                (next_dir / ".github_sha").write_text(remote_sha, encoding="ascii")

                if live_dir.exists():
                    old_dir = cfg_mod.APP_DIR / "live_app_old"
                    if old_dir.exists():
                        shutil.rmtree(old_dir, ignore_errors=True)
                    try:
                        live_dir.rename(old_dir)
                    except Exception:
                        shutil.rmtree(live_dir, ignore_errors=True)
                next_dir.rename(live_dir)
                old_dir = cfg_mod.APP_DIR / "live_app_old"
                if old_dir.exists():
                    shutil.rmtree(old_dir, ignore_errors=True)
            except Exception as exc:
                return {"ok": False, "error": f"Install live_app failed: {exc}"}
        else:
            target_dir = cfg_mod.APP_DIR
            try:
                for rel_posix, data in extracted.items():
                    dst = target_dir / rel_posix
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(data)
                (target_dir / ".github_sha").write_text(remote_sha, encoding="ascii")
            except Exception as exc:
                return {"ok": False, "error": f"Install update failed: {exc}"}

        cmd_file = cfg_mod.APP_DIR / "restart_app.cmd"
        if is_frozen:
            launch_line = f'start "" /D "{cfg_mod.APP_DIR}" "{Path(sys.executable).resolve()}"'
        else:
            arg_str = subprocess.list2cmdline([str(Path(sys.executable).resolve())] + list(sys.argv))
            launch_line = f'start "" /D "{cfg_mod.APP_DIR}" {arg_str}'

        cmd_script = (
            "@echo off\r\n"
            "setlocal\r\n"
            'set "_MEIPASS="\r\n'
            'set "_MEIPASS2="\r\n'
            'set "_PYI_ARCHIVE_FILE="\r\n'
            'set "_PYI_APPLICATION_HOME_DIR="\r\n'
            'set "DUALRIG_BOOTSTRAPPED="\r\n'
            'set "DUALRIG_RESOURCE_DIR="\r\n'
            "timeout /t 2 /nobreak >nul\r\n"
            f"{launch_line}\r\n"
            'del "%~f0"\r\n'
        )
        cmd_file.write_text(cmd_script, encoding="ascii")

        clean_env = os.environ.copy()
        for k in list(clean_env.keys()):
            if k.startswith("_MEI") or k.startswith("_PYI") or k.startswith("DUALRIG_"):
                clean_env.pop(k, None)
        if "PATH" in clean_env:
            clean_env["PATH"] = os.pathsep.join(
                p for p in clean_env["PATH"].split(os.pathsep) if "_MEI" not in p
            )

        flags = 0x00000008 | 0x00000200 | 0x08000000
        subprocess.Popen(
            ["cmd.exe", "/c", str(cmd_file)],
            creationflags=flags,
            env=clean_env,
            cwd=str(cfg_mod.APP_DIR),
            close_fds=True,
        )

        if self.loop and self.request_restart_callback:
            self.loop.call_later(0.4, self.request_restart_callback)

        return {
            "ok": True,
            "restarting": True,
            "size": len(zbytes),
            "sha256": remote_sha,
            "file_count": len(extracted),
        }
