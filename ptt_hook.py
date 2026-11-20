"""Windows global & window-scoped keyboard/mouse PTT hotkey monitor via ctypes user32."""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import sys
from typing import Awaitable, Callable, Dict

log = logging.getLogger("ptt_hook")


class GlobalPttMonitor:
    """Polls bound Windows VK codes (keyboard or mouse buttons) for momentary push-to-talk."""

    def __init__(self, on_ptt: Callable[[int, bool], Awaitable[None]]) -> None:
        self.on_ptt = on_ptt
        self.ptt_global: bool = False
        self.binds: Dict[int, int] = {1: 0, 2: 0}  # slot -> Windows VK code
        self.ui_focused: bool = True
        self.binding_in_progress: bool = False

        self._pressed: Dict[int, bool] = {1: False, 2: False}
        self._running = False
        self._task: asyncio.Task | None = None
        self._my_pid = os.getpid()

        self._user32 = ctypes.windll.user32 if sys.platform == "win32" else None

    def configure(self, ptt_global: bool, bind1_vk: int, bind2_vk: int) -> None:
        self.ptt_global = bool(ptt_global)
        self.binds[1] = int(bind1_vk or 0)
        self.binds[2] = int(bind2_vk or 0)

    def start(self) -> None:
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _is_app_foreground(self) -> bool:
        if self._user32 is None:
            return self.ui_focused
        try:
            hwnd = self._user32.GetForegroundWindow()
            if hwnd:
                pid = ctypes.c_ulong(0)
                self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == self._my_pid:
                    return True
        except Exception:
            pass
        return self.ui_focused

    def _is_vk_down(self, vk: int) -> bool:
        if not vk or self._user32 is None:
            return False
        try:
            return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)
        except Exception:
            return False

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                if not self.binding_in_progress:
                    allowed = self.ptt_global or self._is_app_foreground()
                    for slot in (1, 2):
                        vk = self.binds.get(slot, 0)
                        down = bool(allowed and vk and self._is_vk_down(vk))
                        if down != self._pressed[slot]:
                            self._pressed[slot] = down
                            await self.on_ptt(slot, down)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.debug("PTT monitor error: %s", exc)
            await asyncio.sleep(0.016)
