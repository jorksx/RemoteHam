"""Pure-ctypes Windows WASAPI loopback capture for live default playback passthrough (no virtual cable needed)."""
from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, byref, c_void_p, wintypes
import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("wasapi_loopback")


class GUID(Structure):
    _fields_ = [
        ("Data1", wintypes.ULONG),
        ("Data2", wintypes.USHORT),
        ("Data3", wintypes.USHORT),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, s: str) -> "GUID":
        import uuid
        u = uuid.UUID(s)
        d4 = (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:])
        return cls(u.time_low, u.time_mid, u.time_hi_version, d4)

    def to_bytes(self) -> bytes:
        return bytes(self)


class WAVEFORMATEX(Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


CLSID_MMDeviceEnumerator = GUID.from_str("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID.from_str("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IAudioClient = GUID.from_str("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID.from_str("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
SUBTYPE_IEEE_FLOAT_BYTES = GUID.from_str("{00000003-0000-0010-8000-00AA00389B71}").to_bytes()
SUBTYPE_PCM_BYTES = GUID.from_str("{00000001-0000-0010-8000-00AA00389B71}").to_bytes()

CLSCTX_ALL = 23
COINIT_MULTITHREADED = 0x0
E_RENDER = 0
E_CONSOLE = 0
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_E_DEVICE_INVALIDATED = 0x88890004


def _vcall(obj_ptr: c_void_p, idx: int, restype, *argtypes):
    vtbl = ctypes.cast(obj_ptr, POINTER(POINTER(c_void_p))).contents
    fn_type = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return fn_type(vtbl[idx])


def _release(obj_ptr: Optional[c_void_p]) -> None:
    if obj_ptr and obj_ptr.value:
        try:
            _vcall(obj_ptr, 2, wintypes.ULONG)(obj_ptr)
        except Exception:
            pass


class WasapiLoopbackCapture:
    """Captures Windows default playback endpoint via WASAPI loopback and invokes callback(stereo_48k)."""

    def __init__(self, on_audio: Callable[[np.ndarray], None], target_rate: int = 48000) -> None:
        self.on_audio = on_audio
        self.target_rate = int(target_rate)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.active_device_id: str = ""
        self.sample_rate: int = 0
        self.channels: int = 0
        self.error: str = ""

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.error = ""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def _run(self) -> None:
        ole32 = ctypes.windll.ole32
        hr_co = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        did_co_init = hr_co in (0, 1)
        try:
            while self._running:
                try:
                    self._capture_session(ole32)
                except Exception as exc:
                    self.error = str(exc)
                    log.warning("WASAPI loopback session ended: %s", exc)
                    if self._running:
                        time.sleep(0.5)
        finally:
            if did_co_init:
                try:
                    ole32.CoUninitialize()
                except Exception:
                    pass

    def _capture_session(self, ole32) -> None:
        enum_ptr = c_void_p()
        dev_ptr = c_void_p()
        ac_ptr = c_void_p()
        cap_ptr = c_void_p()
        fmt_ptr = c_void_p()

        try:
            hr = ole32.CoCreateInstance(
                byref(CLSID_MMDeviceEnumerator),
                None,
                CLSCTX_ALL,
                byref(IID_IMMDeviceEnumerator),
                byref(enum_ptr),
            )
            if hr != 0:
                raise OSError(f"CoCreateInstance MMDeviceEnumerator failed: 0x{hr & 0xFFFFFFFF:08X}")

            hr = _vcall(enum_ptr, 4, ctypes.c_uint32, ctypes.c_int, ctypes.c_int, POINTER(c_void_p))(
                enum_ptr, E_RENDER, E_CONSOLE, byref(dev_ptr)
            )
            if hr != 0:
                raise OSError(f"GetDefaultAudioEndpoint failed: 0x{hr & 0xFFFFFFFF:08X}")

            hr = _vcall(dev_ptr, 3, ctypes.c_uint32, POINTER(GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p))(
                dev_ptr, byref(IID_IAudioClient), CLSCTX_ALL, None, byref(ac_ptr)
            )
            if hr != 0:
                raise OSError(f"IMMDevice.Activate IAudioClient failed: 0x{hr & 0xFFFFFFFF:08X}")

            hr = _vcall(ac_ptr, 8, ctypes.c_uint32, POINTER(c_void_p))(ac_ptr, byref(fmt_ptr))
            if hr != 0 or not fmt_ptr.value:
                raise OSError(f"GetMixFormat failed: 0x{hr & 0xFFFFFFFF:08X}")

            wfx = ctypes.cast(fmt_ptr, POINTER(WAVEFORMATEX)).contents
            ch = int(wfx.nChannels)
            sr = int(wfx.nSamplesPerSec)
            bits = int(wfx.wBitsPerSample)
            tag = int(wfx.wFormatTag)
            is_float = (tag == 3)
            if tag == 0xFFFE and int(wfx.cbSize) >= 22:
                raw_sub = ctypes.string_at(fmt_ptr.value + 24, 16)
                is_float = (raw_sub == SUBTYPE_IEEE_FLOAT_BYTES)

            self.channels = ch
            self.sample_rate = sr

            # 100 ms shared-mode loopback buffer (100ns units)
            hr = _vcall(
                ac_ptr,
                3,
                ctypes.c_uint32,
                ctypes.c_int,
                wintypes.DWORD,
                ctypes.c_int64,
                ctypes.c_int64,
                c_void_p,
                c_void_p,
            )(ac_ptr, AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK, 1_000_000, 0, fmt_ptr, None)
            if hr != 0:
                raise OSError(f"IAudioClient.Initialize loopback failed: 0x{hr & 0xFFFFFFFF:08X}")

            hr = _vcall(ac_ptr, 14, ctypes.c_uint32, POINTER(GUID), POINTER(c_void_p))(
                ac_ptr, byref(IID_IAudioCaptureClient), byref(cap_ptr)
            )
            if hr != 0:
                raise OSError(f"GetService IAudioCaptureClient failed: 0x{hr & 0xFFFFFFFF:08X}")

            hr = _vcall(ac_ptr, 10, ctypes.c_uint32)(ac_ptr)
            if hr != 0:
                raise OSError(f"IAudioClient.Start failed: 0x{hr & 0xFFFFFFFF:08X}")

            get_next = _vcall(cap_ptr, 5, ctypes.c_uint32, POINTER(wintypes.UINT))
            get_buf = _vcall(
                cap_ptr,
                3,
                ctypes.c_uint32,
                POINTER(c_void_p),
                POINTER(wintypes.UINT),
                POINTER(wintypes.DWORD),
                POINTER(ctypes.c_uint64),
                POINTER(ctypes.c_uint64),
            )
            rel_buf = _vcall(cap_ptr, 4, ctypes.c_uint32, wintypes.UINT)

            pkt_frames = wintypes.UINT()
            data_ptr = c_void_p()
            num_frames = wintypes.UINT()
            flags = wintypes.DWORD()
            dev_pos = ctypes.c_uint64()
            qpc_pos = ctypes.c_uint64()
            block_align = int(wfx.nBlockAlign)

            log.info("WASAPI loopback started (%d Hz, %d ch, float=%s)", sr, ch, is_float)
            self.error = ""

            while self._running:
                hr = get_next(cap_ptr, byref(pkt_frames))
                if hr == AUDCLNT_E_DEVICE_INVALIDATED:
                    log.info("Default playback device changed; restarting WASAPI loopback")
                    break
                if hr != 0:
                    raise OSError(f"GetNextPacketSize failed: 0x{hr & 0xFFFFFFFF:08X}")

                if pkt_frames.value == 0:
                    time.sleep(0.01)
                    continue

                while pkt_frames.value > 0 and self._running:
                    hr = get_buf(cap_ptr, byref(data_ptr), byref(num_frames), byref(flags), byref(dev_pos), byref(qpc_pos))
                    if hr == AUDCLNT_E_DEVICE_INVALIDATED:
                        return
                    if hr != 0:
                        raise OSError(f"GetBuffer failed: 0x{hr & 0xFFFFFFFF:08X}")

                    n_fr = int(num_frames.value)
                    if n_fr > 0:
                        if (flags.value & AUDCLNT_BUFFERFLAGS_SILENT) or not data_ptr.value:
                            mono = np.zeros(n_fr, dtype=np.float32)
                        else:
                            raw = ctypes.string_at(data_ptr.value, n_fr * block_align)
                            if is_float and bits == 32:
                                arr = np.frombuffer(raw, dtype="<f4").reshape(n_fr, ch)
                            elif bits == 16:
                                arr = (np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0).reshape(n_fr, ch)
                            elif bits == 32:
                                arr = (np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0).reshape(n_fr, ch)
                            else:
                                arr = np.zeros((n_fr, ch), dtype=np.float32)
                            # Use primary Front Left/Right channels so 7.1 headsets keep full YouTube/media level
                            if ch >= 2:
                                mono = (arr[:, 0] + arr[:, 1]) * 0.5
                            else:
                                mono = arr[:, 0]

                        rel_buf(cap_ptr, num_frames.value)

                        if sr != self.target_rate and mono.size > 1:
                            tgt_len = max(1, int(round(mono.size * (self.target_rate / sr))))
                            x_old = np.linspace(0.0, 1.0, mono.size, endpoint=False)
                            x_new = np.linspace(0.0, 1.0, tgt_len, endpoint=False)
                            mono = np.interp(x_new, x_old, mono).astype(np.float32)

                        stereo = np.column_stack([mono, mono]).astype(np.float32)
                        try:
                            self.on_audio(stereo)
                        except Exception:
                            pass
                    else:
                        rel_buf(cap_ptr, num_frames.value)

                    hr = get_next(cap_ptr, byref(pkt_frames))
                    if hr != 0:
                        break
        finally:
            if ac_ptr.value:
                try:
                    _vcall(ac_ptr, 11, ctypes.c_uint32)(ac_ptr)  # Stop()
                except Exception:
                    pass
            if fmt_ptr.value:
                try:
                    ole32.CoTaskMemFree.argtypes = [c_void_p]
                    ole32.CoTaskMemFree(fmt_ptr)
                except Exception:
                    pass
            _release(cap_ptr)
            _release(ac_ptr)
            _release(dev_ptr)
            _release(enum_ptr)
