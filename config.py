"""Persistent configuration management for Dual-Rig Remote."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

IS_FROZEN = bool(getattr(sys, "frozen", False))
if IS_FROZEN:
    _base_appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    APP_DIR = Path(_base_appdata) / "DualRigRemote"
    APP_DIR.mkdir(parents=True, exist_ok=True)
    EXE_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
    EXE_DIR = APP_DIR

LIVE_APP_DIR = APP_DIR / "live_app"
_override_res = os.environ.get("DUALRIG_RESOURCE_DIR", "").strip()
RESOURCE_DIR = Path(_override_res) if _override_res else Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
CONFIG_PATH = APP_DIR / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "mode": "host",  # "host" (connected to USB radios) or "client" (connects to remote host IP:port)
    "client": {
        "server_ip": "192.168.1.100",
        "server_port": 8090,
        "use_tls": False,
    },
    "server": {
        "enabled": False,
        "port": 8090,
    },
    "mobile": {
        "enabled": True,
        "port": 8085,
    },
    "desktop_http_port": 8080,
    "github_repo": "jorksx/RemoteHam",
    "github_branch": "main",
    "qth_locator": "KP20",
    "pc_audio": {
        "speaker_device": "",
        "mic_device": "",
        "master_volume": 85,
    },
    "audio_routing": {
        "radio1_channel": "right",  # "left", "right", "both", "mute"
        "radio2_channel": "left",
        "tx_interlock": True,
    },
    "ptt_global": False,  # False = only when app window is active, True = works globally
    "ui": {
        "dual_view": False,
        "wf_track": False,
        "cw_macros": ["", "", "", ""],
        "smeter_style": 0,
        "passthrough_mic_ignored": True,
        "vm_timer_sec": 5,
    },
    "radios": [
        {
            "slot": 1,
            "enabled": True,
            "com_port": "COM8",
            "baud_rate": 115200,
            "audio_in_device": "",   # empty = auto-match from USB radio name
            "audio_out_device": "",  # empty = auto-match from USB radio name
            "hamlib_port": 4532,
            "hamlib_port_2": 4533,
            "sql_threshold": 15,     # 0..100 software/radio SQL
            "win_volume": 85,        # 0..100 Windows local speaker volume
            "win_mute": False,
            "ptt_bind": {"vk": 0, "label": ""},
        },
        {
            "slot": 2,
            "enabled": True,
            "com_port": "COM9",
            "baud_rate": 115200,
            "audio_in_device": "",
            "audio_out_device": "",
            "hamlib_port": 4534,
            "hamlib_port_2": 4535,
            "sql_threshold": 15,
            "win_volume": 85,
            "win_mute": False,
            "ptt_bind": {"vk": 0, "label": ""},
        },
    ],
}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge override values into a copy of base."""
    result = json.loads(json.dumps(base))
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config() -> Dict[str, Any]:
    """Load configuration from config.json (in AppData when frozen), creating defaults if missing."""
    if not CONFIG_PATH.exists():
        # Optional one-time import if user placed config.json beside DualRigRemote.exe
        if IS_FROZEN:
            sidecar_cfg = EXE_DIR / "config.json"
            if sidecar_cfg.is_file():
                try:
                    raw = json.loads(sidecar_cfg.read_text(encoding="utf-8"))
                    merged = deep_merge(DEFAULT_CONFIG, raw)
                    save_config(merged)
                    return merged
                except Exception:
                    pass
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return deep_merge(DEFAULT_CONFIG, raw)
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: Dict[str, Any]) -> None:
    """Persist configuration to config.json."""
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
