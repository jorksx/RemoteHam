"""Host Server & Client PC WebSocket link for full remote control, waterfalls, audio & FT8/Hamlib tunneling."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Set

import aiohttp
from aiohttp import web

log = logging.getLogger("remote_link")

MSG_RX_AUDIO = 0x01
MSG_TX_AUDIO = 0x02


class RemoteLinkServer:
    """Runs on the radio-connected Host PC when Server mode is enabled in Settings."""

    def __init__(
        self,
        get_full_state: Callable[[], Dict[str, Any]],
        exec_cmd: Callable[[int, str, Dict[str, Any]], Awaitable[None]],
        on_tx_audio: Callable[[int, bytes], None],
    ) -> None:
        self.get_full_state = get_full_state
        self.exec_cmd = exec_cmd
        self.on_tx_audio = on_tx_audio
        self._runner: Optional[web.AppRunner] = None
        self._clients: Set[web.WebSocketResponse] = set()
        self.port: int = 8090
        self.running: bool = False

    async def start(self, port: int) -> None:
        await self.stop()
        self.port = int(port)
        app = web.Application()
        app.router.add_get("/ws/link", self._ws_handler)
        app.router.add_get("/health", self._health_handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        self.running = True
        log.info("Remote Link Server listening on port %d", self.port)

    async def stop(self) -> None:
        self.running = False
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
            self._runner = None

    def client_count(self) -> int:
        return len(self._clients)

    async def _health_handler(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "clients": len(self._clients)})

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        self._clients.add(ws)
        log.info("Remote PC Client connected from %s", request.remote)

        try:
            await ws.send_json({"type": "state", "data": self.get_full_state()})
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("type") == "cmd":
                            await self.exec_cmd(
                                int(data.get("slot", 1)),
                                str(data.get("action", "")),
                                data.get("params") or {},
                            )
                    except Exception as exc:
                        log.warning("Remote link command error: %s", exc)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    raw = msg.data
                    if len(raw) > 2 and raw[0] == MSG_TX_AUDIO:
                        slot = raw[1]
                        self.on_tx_audio(slot, raw[2:])
        finally:
            self._clients.discard(ws)
            log.info("Remote PC Client disconnected")
        return ws

    async def broadcast_json(self, payload: Dict[str, Any]) -> None:
        if not self._clients:
            return
        text = json.dumps(payload)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_str(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    async def broadcast_rx_audio(self, slot: int, pcm_bytes: bytes) -> None:
        if not self._clients:
            return
        pkt = bytes([MSG_RX_AUDIO, slot]) + pcm_bytes
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_bytes(pkt)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


class RemoteLinkClient:
    """Runs on the second PC when Client mode is active, tunneling UI, Hamlib, waterfalls & audio."""

    def __init__(
        self,
        on_remote_state: Callable[[Dict[str, Any]], None],
        on_remote_scope: Callable[[Dict[str, Any]], None],
        on_rx_audio: Callable[[int, bytes], None],
    ) -> None:
        self.on_remote_state = on_remote_state
        self.on_remote_scope = on_remote_scope
        self.on_rx_audio = on_rx_audio
        self._task: Optional[asyncio.Task] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        self.connected = False
        self.error = ""
        self.server_ip = ""
        self.server_port = 8090

    async def start(self, server_ip: str, server_port: int) -> None:
        await self.stop()
        self.server_ip = server_ip.strip()
        self.server_port = int(server_port)
        self._running = True
        self._task = asyncio.create_task(self._connect_loop())

    async def stop(self) -> None:
        self._running = False
        self.connected = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _connect_loop(self) -> None:
        while self._running:
            url = f"ws://{self.server_ip}:{self.server_port}/ws/link"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=10.0) as ws:
                        self._ws = ws
                        self.connected = True
                        self.error = ""
                        log.info("Connected to Remote Host Server %s", url)
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                mtype = data.get("type")
                                if mtype == "state":
                                    self.on_remote_state(data.get("data", {}))
                                elif mtype == "scope":
                                    self.on_remote_scope(data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                raw = msg.data
                                if len(raw) > 2 and raw[0] == MSG_RX_AUDIO:
                                    self.on_rx_audio(raw[1], raw[2:])
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.connected = False
                self.error = str(exc)
                self._ws = None
                await asyncio.sleep(2.5)

    async def send_command(self, slot: int, action: str, params: Dict[str, Any]) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.send_json({"type": "cmd", "slot": slot, "action": action, "params": params})

    async def send_tx_audio(self, slot: int, pcm_bytes: bytes) -> None:
        if self._ws and not self._ws.closed:
            try:
                await self._ws.send_bytes(bytes([MSG_TX_AUDIO, slot]) + pcm_bytes)
            except Exception:
                pass
