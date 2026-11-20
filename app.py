"""Main entry point for Dual-Rig Amateur Radio Remote (Desktop App + Server + Mobile HTTPS)."""
from __future__ import annotations

import importlib.util
import os
import runpy
import sys
from pathlib import Path

if getattr(sys, "frozen", False) and os.environ.get("DUALRIG_BOOTSTRAPPED") != "1":
    _live_base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "DualRigRemote"
    _live_dir = _live_base / "live_app"
    if (_live_dir / "app.py").is_file():
        class _LiveOverrideFinder:
            def find_spec(self, fullname, path, target=None):
                if "." in fullname or path is not None:
                    return None
                cand = _live_dir / f"{fullname}.py"
                if cand.is_file():
                    return importlib.util.spec_from_file_location(fullname, str(cand))
                return None

        sys.meta_path.insert(0, _LiveOverrideFinder())
        sys.path.insert(0, str(_live_dir))
        os.environ["DUALRIG_BOOTSTRAPPED"] = "1"
        os.environ["DUALRIG_RESOURCE_DIR"] = str(_live_dir)
        runpy.run_path(str(_live_dir / "app.py"), run_name="__main__")
        sys.exit(0)

import argparse
import asyncio
import logging
import threading
import time
import webbrowser

import config as cfg_mod
from controller import AppController
from web_server import WebServerManager

handlers = [logging.FileHandler(cfg_mod.APP_DIR / "remote.log", encoding="utf-8")]
if sys.stderr is not None:
    handlers.append(logging.StreamHandler(sys.stderr))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=handlers,
)
log = logging.getLogger("app")


def main() -> None:
    parser = argparse.ArgumentParser(description="Dual-Rig Amateur Radio Remote")
    parser.add_argument("--headless", action="store_true", help="Run without opening a desktop GUI window")
    parser.add_argument("--browser", action="store_true", help="Open in default web browser instead of pywebview")
    parser.add_argument("--client", type=str, default="", help="Start in client mode connecting to IP:PORT")
    args = parser.parse_args()

    if args.client:
        cfg = cfg_mod.load_config()
        parts = args.client.split(":")
        cfg["mode"] = "client"
        cfg["client"]["server_ip"] = parts[0]
        if len(parts) > 1:
            cfg["client"]["server_port"] = int(parts[1])
        cfg_mod.save_config(cfg)

    ctrl = AppController()
    web_mgr = WebServerManager(ctrl)

    ready_event = threading.Event()
    stop_event_holder = {}

    def backend_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        stop_ev = asyncio.Event()
        stop_event_holder["loop"] = loop
        stop_event_holder["stop_ev"] = stop_ev

        async def runner() -> None:
            await ctrl.start()
            await web_mgr.start()
            ready_event.set()
            await stop_ev.wait()
            await web_mgr.stop()
            await ctrl.stop()

        try:
            loop.run_until_complete(runner())
        finally:
            loop.close()

    t = threading.Thread(target=backend_thread, daemon=True)
    t.start()
    ready_event.wait(timeout=10.0)

    def _trigger_restart() -> None:
        loop = stop_event_holder.get("loop")
        ev = stop_event_holder.get("stop_ev")
        try:
            import webview
            for w in list(getattr(webview, "windows", []) or []):
                try:
                    w.destroy()
                except Exception:
                    pass
        except Exception:
            pass
        if loop and ev:
            loop.call_soon_threadsafe(ev.set)

    ctrl.request_restart_callback = _trigger_restart

    desktop_url = f"http://127.0.0.1:{int(ctrl.cfg.get('desktop_http_port', 8080))}"
    log.info("Desktop UI ready at %s", desktop_url)
    for url in ctrl.get_full_state().get("mobile", {}).get("urls", []):
        log.info("Mobile HTTPS UI ready at %s", url)

    try:
        if args.headless:
            while t.is_alive():
                time.sleep(0.5)
        elif args.browser:
            webbrowser.open(desktop_url)
            while t.is_alive():
                time.sleep(0.5)
        else:
            try:
                import webview

                class _DesktopWindowApi:
                    def __init__(self) -> None:
                        self._win = None
                        self._maximized = False
                        self._last_save_dir = str(Path.home() / "Documents")

                    def attach(self, win) -> None:
                        self._win = win

                    def minimize(self) -> None:
                        if self._win:
                            self._win.minimize()

                    def toggle_maximize(self) -> bool:
                        if not self._win:
                            return False
                        if self._maximized:
                            self._win.restore()
                            self._maximized = False
                        else:
                            self._win.maximize()
                            self._maximized = True
                        return self._maximized

                    def close(self) -> None:
                        if self._win:
                            self._win.destroy()

                    def save_wav_dialog(self, filename: str) -> dict:
                        import shutil
                        safe_name = Path(str(filename or "")).name
                        if not safe_name:
                            return {"ok": False, "saved": False}
                        src = cfg_mod.APP_DIR / "recordings" / safe_name
                        if not src.is_file():
                            return {"ok": False, "saved": False}

                        init_dir = self._last_save_dir if Path(self._last_save_dir).is_dir() else str(Path.home())
                        result_holder = [None]

                        def _open_dialog() -> None:
                            result_holder[0] = self._win.create_file_dialog(
                                webview.FileDialog.SAVE,
                                directory=init_dir,
                                save_filename=safe_name,
                                file_types=("WAV Audio (*.wav)", "All files (*.*)"),
                            )

                        try:
                            import webview.platforms.winforms as wv_wf
                            from System import Action
                            bv = wv_wf.BrowserView.instances.get(self._win.uid)
                            if bv is not None:
                                bv.Invoke(Action(_open_dialog))
                            else:
                                _open_dialog()
                        except Exception:
                            _open_dialog()

                        chosen = result_holder[0]
                        dst_str = chosen[0] if isinstance(chosen, (tuple, list)) and chosen else (chosen if isinstance(chosen, str) else "")
                        if not dst_str:
                            src.unlink(missing_ok=True)
                            return {"ok": True, "saved": False}
                        if not dst_str.lower().endswith(".wav"):
                            dst_str += ".wav"
                        dst_path = Path(dst_str)
                        dst_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(src), str(dst_path))
                        src.unlink(missing_ok=True)
                        self._last_save_dir = str(dst_path.parent)
                        return {"ok": True, "saved": True, "path": str(dst_path)}

                win_api = _DesktopWindowApi()
                win = webview.create_window(
                    "Dual-Rig Amateur Radio Remote",
                    desktop_url,
                    js_api=win_api,
                    width=1480,
                    height=925,
                    min_size=(940, 620),
                    frameless=True,
                    easy_drag=False,
                    shadow=True,
                    background_color="#0b0c0e",
                )
                win_api.attach(win)
                wv_storage = cfg_mod.APP_DIR / "webview_cache"
                wv_storage.mkdir(parents=True, exist_ok=True)
                webview.start(private_mode=False, storage_path=str(wv_storage))
            except Exception as exc:
                log.warning("pywebview unavailable (%s), opening default browser", exc)
                webbrowser.open(desktop_url)
                while t.is_alive():
                    time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        loop = stop_event_holder.get("loop")
        ev = stop_event_holder.get("stop_ev")
        if loop and ev:
            loop.call_soon_threadsafe(ev.set)
        t.join(timeout=5.0)


if __name__ == "__main__":
    main()
