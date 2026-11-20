"""HTTP desktop UI server (port 8080) and HTTPS mobile responsive server (port 8085)."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import web

import config as cfg_mod
import tls_util
import usb_discovery
from controller import AppController, MobileSession

log = logging.getLogger("web_server")
STATIC_DIR = cfg_mod.RESOURCE_DIR / "static"


class WebServerManager:
    def __init__(self, ctrl: AppController) -> None:
        self.ctrl = ctrl
        self._desktop_runner: Optional[web.AppRunner] = None
        self._mobile_runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        desktop_port = int(self.ctrl.cfg.get("desktop_http_port", 8080))
        mobile_port = int(self.ctrl.cfg.get("mobile", {}).get("port", 8085))

        # 1. Desktop UI HTTP server
        d_app = web.Application()
        self._register_common_routes(d_app, is_mobile_site=False)
        d_app.router.add_get("/ws/ui", self._ws_ui_handler)
        d_app.router.add_static("/static", STATIC_DIR)

        self._desktop_runner = web.AppRunner(d_app)
        await self._desktop_runner.setup()
        await web.TCPSite(self._desktop_runner, "127.0.0.1", desktop_port).start()
        log.info("Desktop UI serving on http://127.0.0.1:%d", desktop_port)

        # 2. Mobile HTTPS server on port 8085
        m_app = web.Application()
        self._register_common_routes(m_app, is_mobile_site=True)
        m_app.router.add_get("/ws/mobile", self._ws_mobile_handler)
        m_app.router.add_static("/static", STATIC_DIR)

        ssl_ctx = tls_util.create_ssl_context()
        self._mobile_runner = web.AppRunner(m_app)
        await self._mobile_runner.setup()
        await web.TCPSite(self._mobile_runner, "0.0.0.0", mobile_port, ssl_context=ssl_ctx).start()
        log.info("Mobile HTTPS UI serving on https://0.0.0.0:%d", mobile_port)

    async def stop(self) -> None:
        for runner in (self._desktop_runner, self._mobile_runner):
            if runner:
                try:
                    await runner.cleanup()
                except Exception:
                    pass
        self._desktop_runner = None
        self._mobile_runner = None

    def _register_common_routes(self, app: web.Application, is_mobile_site: bool) -> None:
        async def index_handler(request: web.Request) -> web.FileResponse:
            fname = "mobile.html" if is_mobile_site else "index.html"
            return web.FileResponse(STATIC_DIR / fname)

        async def mobile_handler(request: web.Request) -> web.FileResponse:
            return web.FileResponse(STATIC_DIR / "mobile.html")

        async def desktop_handler(request: web.Request) -> web.FileResponse:
            return web.FileResponse(STATIC_DIR / "index.html")

        async def get_devices(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "serial_ports": usb_discovery.list_serial_ports_with_usb(),
                    "audio": usb_discovery.list_audio_devices(),
                }
            )

        async def get_config(request: web.Request) -> web.Response:
            return web.json_response(self.ctrl.cfg)

        async def post_config(request: web.Request) -> web.Response:
            data = await request.json()
            updated = await self.ctrl.update_config(data)
            return web.json_response({"ok": True, "config": updated, "state": self.ctrl.get_full_state()})

        async def get_state(request: web.Request) -> web.Response:
            return web.json_response(self.ctrl.get_full_state())

        async def get_repeaters(request: web.Request) -> web.Response:
            force = request.query.get("refresh") in ("1", "true", "yes")
            if force or self.ctrl.repeaters.is_expired():
                await self.ctrl.repeaters.ensure_fresh(force=force)
            return web.json_response(self.ctrl.repeaters.get_payload())

        async def post_cmd(request: web.Request) -> web.Response:
            data = await request.json()
            await self.ctrl.execute_radio_command(
                int(data.get("slot", 1)),
                str(data.get("action", "")),
                data.get("params") or {},
            )
            return web.json_response({"ok": True})

        async def get_app_update_status(request: web.Request) -> web.Response:
            return web.json_response(await self.ctrl.check_app_update())

        async def post_app_update(request: web.Request) -> web.Response:
            return web.json_response(await self.ctrl.perform_app_update())

        async def post_qso_rec_stop(request: web.Request) -> web.Response:
            fname = self.ctrl.audio.stop_qso_rec()
            self.ctrl._state_dirty = True
            return web.json_response({"ok": bool(fname), "filename": fname})

        async def get_qso_rec_download(request: web.Request) -> web.StreamResponse:
            fname = Path(str(request.query.get("file", ""))).name
            if not fname:
                return web.Response(status=404, text="Missing file")
            fpath = cfg_mod.APP_DIR / "recordings" / fname
            if not fpath.is_file():
                return web.Response(status=404, text="Not found")
            return web.FileResponse(
                fpath,
                headers={"Content-Disposition": f'attachment; filename="{fname}"'},
            )

        app.router.add_get("/", index_handler)
        app.router.add_get("/mobile", mobile_handler)
        app.router.add_get("/desktop", desktop_handler)
        app.router.add_get("/api/devices", get_devices)
        app.router.add_get("/api/config", get_config)
        app.router.add_post("/api/config", post_config)
        app.router.add_get("/api/state", get_state)
        app.router.add_get("/api/repeaters", get_repeaters)
        app.router.add_post("/api/cmd", post_cmd)
        app.router.add_get("/api/app_update/status", get_app_update_status)
        app.router.add_post("/api/app_update", post_app_update)
        app.router.add_post("/api/qso_rec/stop", post_qso_rec_stop)
        app.router.add_get("/api/qso_rec/download", get_qso_rec_download)

        async def _no_cache_headers(request: web.Request, response: web.StreamResponse) -> None:
            if request.path in ("/", "/mobile", "/desktop") or (
                request.path.startswith("/static/") and not request.path.startswith("/static/vendor/")
            ):
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"

        app.on_response_prepare.append(_no_cache_headers)

    async def _ws_ui_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024)
        await ws.prepare(request)
        self.ctrl.ui_clients.add(ws)
        try:
            await ws.send_json({"type": "state", "data": self.ctrl.get_full_state()})
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "cmd":
                        await self.ctrl.execute_radio_command(
                            int(data.get("slot", 1)),
                            str(data.get("action", "")),
                            data.get("params") or {},
                        )
        finally:
            self.ctrl.ui_clients.discard(ws)
        return ws

    async def _ws_mobile_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024, heartbeat=2.0)
        await ws.prepare(request)
        sess = MobileSession(ws)
        self.ctrl.mobile_clients[ws] = sess
        self.ctrl.audio.set_mobile_connected(True)
        self.ctrl._state_dirty = True
        log.info("Mobile HTTPS client connected from %s", request.remote)

        try:
            await ws.send_json({"type": "state", "data": self.ctrl.get_full_state()})
            async for msg in ws:
                sess.last_seen = time.monotonic()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    mtype = data.get("type")
                    if mtype == "ping":
                        continue
                    if mtype == "mobile_cfg":
                        if "listen_radios" in data:
                            sess.listen_radios = {int(x) for x in (data["listen_radios"] or []) if int(x) in (1, 2)}
                        elif "audio_enabled" in data:
                            if not bool(data["audio_enabled"]):
                                sess.listen_radios = set()
                            else:
                                lr = int(data.get("listen_radio", 0))
                                sess.listen_radios = {1, 2} if lr == 0 else {lr}
                        if "mobile_sql" in data:
                            sess.mobile_sql = max(0, min(100, int(data["mobile_sql"])))
                        if "mobile_af" in data:
                            sess.mobile_af = max(0, min(100, int(data["mobile_af"])))
                        if "sample_rate" in data:
                            sess.sample_rate = max(8000, min(96000, int(data["sample_rate"])))
                    elif mtype == "cmd":
                        params = dict(data.get("params") or {})
                        if data.get("action") == "set_ptt":
                            params["source"] = "mobile"
                        await self.ctrl.execute_radio_command(
                            int(data.get("slot", 1)),
                            str(data.get("action", "")),
                            params,
                        )
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    raw = msg.data
                    if len(raw) > 2:
                        slot = raw[0]
                        self.ctrl.audio.inject_remote_tx_pcm(
                            slot, raw[1:], source="mobile", sample_rate=sess.sample_rate
                        )
        finally:
            self.ctrl._drop_mobile_client(ws)
            log.info("Mobile HTTPS client disconnected")
        return ws
