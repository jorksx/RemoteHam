"""Nordic & Finnish amateur radio repeater database aggregator with 30-day local cache."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config as cfg_mod

log = logging.getLogger("repeaters")

CACHE_PATH = cfg_mod.APP_DIR / "repeaters_cache.json"
CACHE_TTL_SEC = 30 * 86400  # 30 days


def maidenhead_to_latlon(locator: str) -> Optional[Tuple[float, float]]:
    """Convert a 4- or 6-char Maidenhead grid locator (e.g. KP20 or KP20MH) to (lat, lon)."""
    loc = (locator or "").strip().upper()
    if len(loc) < 4:
        return None
    if not ("A" <= loc[0] <= "R" and "A" <= loc[1] <= "R" and loc[2].isdigit() and loc[3].isdigit()):
        return None
    lon = (ord(loc[0]) - ord("A")) * 20.0 - 180.0 + int(loc[2]) * 2.0
    lat = (ord(loc[1]) - ord("A")) * 10.0 - 90.0 + int(loc[3]) * 1.0
    if len(loc) >= 6 and "A" <= loc[4] <= "X" and "A" <= loc[5] <= "X":
        lon += (ord(loc[4]) - ord("A") + 0.5) * (2.0 / 24.0)
        lat += (ord(loc[5]) - ord("A") + 0.5) * (1.0 / 24.0)
    else:
        lon += 1.0
        lat += 0.5
    return lat, lon


def distance_bearing_km(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, int]:
    """Return great-circle distance in km and initial azimuth bearing (0..359 deg)."""
    r_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2.0) ** 2
    dist = 2.0 * r_km * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    y = math.sin(dlmb) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    brg = int(round((math.degrees(math.atan2(y, x)) + 360.0) % 360.0))
    return round(dist, 1), brg


def _fetch_json(url: str, timeout: float = 18.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "DualRigRemote/1.0 (ham-repeater-cache)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _freq_to_band(hz: int) -> str:
    if 28_000_000 <= hz <= 30_000_000:
        return "10m"
    if 50_000_000 <= hz <= 54_000_000:
        return "6m"
    if 69_000_000 <= hz <= 72_000_000:
        return "4m"
    if 144_000_000 <= hz <= 148_000_000:
        return "2m"
    if 420_000_000 <= hz <= 450_000_000:
        return "70cm"
    if 1_240_000_000 <= hz <= 1_300_000_000:
        return "23cm"
    return "Other"


def _is_nordic_coords(lat: float, lon: float) -> bool:
    if lat == 0.0 and lon == 0.0:
        return False
    # Mainland Nordics (FI, SE, NO, DK) + Iceland
    return (54.5 <= lat <= 71.5 and 4.5 <= lon <= 32.0) or (63.0 <= lat <= 67.0 and -25.0 <= lon <= -13.0)


def _fetch_and_merge_nordic_repeaters() -> List[Dict[str, Any]]:
    """Fetch Finnish SRAL, Nordic HearHam, and Nordic BrandMeister repeaters and merge cleanly."""
    merged: Dict[Tuple[str, int], Dict[str, Any]] = {}

    def _add(item: Dict[str, Any]) -> None:
        rx_hz = int(item.get("rx_hz", 0))
        if rx_hz < 28_000_000:
            return
        band = _freq_to_band(rx_hz)
        if band == "Other":
            return
        item["band"] = band
        call = str(item.get("callsign", "")).strip().upper()
        if not call:
            return
        item["callsign"] = call
        bucket = int(round(rx_hz / 5000.0))
        key = (call, bucket)
        if key not in merged:
            merged[key] = item
        else:
            # Enrich missing tone/locator metadata without overwriting higher-priority SRAL entry
            cur = merged[key]
            if not cur.get("ctcss_hz") and item.get("ctcss_hz"):
                cur["ctcss_hz"] = item["ctcss_hz"]
            if not cur.get("tsql_hz") and item.get("tsql_hz"):
                cur["tsql_hz"] = item["tsql_hz"]
            if not cur.get("tone_1750") and item.get("tone_1750"):
                cur["tone_1750"] = True
            if not cur.get("locator") and item.get("locator"):
                cur["locator"] = item["locator"]

    # 1. Finnish SRAL official automatic.sral.fi API (highest accuracy for OH)
    mode_map = {1: "FM", 2: "DMR", 3: "DSTAR", 4: "C4FM", 5: "P25", 6: "TETRA", 10: "CW", 11: "SSB", 12: "AM"}
    try:
        sral = _fetch_json("https://automatic.sral.fi/api/stations")
        if isinstance(sral, list):
            for st in sral:
                # Repeaters: station_type == 1 or has rep_shift
                if st.get("rep_shift") is None and st.get("station_type") != 1:
                    continue
                try:
                    rx_hz = int(round(float(st["tx_freq"]) * 1_000_000))
                except Exception:
                    continue
                try:
                    offset_hz = int(round(float(st.get("rep_shift") or 0) * 1_000_000))
                except Exception:
                    offset_hz = 0
                try:
                    lat = float(st.get("wgs84_lat") or 0)
                    lon = float(st.get("wgs84_lon") or 0)
                except Exception:
                    lat, lon = 0.0, 0.0
                if (lat == 0.0 and lon == 0.0) and st.get("mh_locator"):
                    ll = maidenhead_to_latlon(st["mh_locator"])
                    if ll:
                        lat, lon = ll

                rm_ids = st.get("radio_modes") or []
                modes = [mode_map[m] for m in rm_ids if m in mode_map]
                mode_str = "/".join(modes) if modes else "FM"

                ctcss_hz = 0.0
                tone_1750 = False
                for acc in st.get("repeater_access") or []:
                    tc = str(acc.get("type_code", "")).upper()
                    cv = str(acc.get("code_value", "")).strip()
                    if tc == "CTCSS":
                        try:
                            ctcss_hz = float(cv)
                        except Exception:
                            pass
                    elif tc == "TONE" and "1750" in cv:
                        tone_1750 = True

                _add({
                    "callsign": st.get("callsign", ""),
                    "rx_hz": rx_hz,
                    "offset_hz": offset_hz,
                    "tx_hz": rx_hz + offset_hz,
                    "mode": mode_str,
                    "is_dstar": ("FM" not in modes) and ("DSTAR" in mode_str.upper()),
                    "ctcss_hz": ctcss_hz,
                    "tsql_hz": 0.0,
                    "tone_1750": tone_1750,
                    "status": str(st.get("status") or "QRV").upper(),
                    "city": str(st.get("qth_city") or st.get("station_name") or ""),
                    "locator": str(st.get("mh_locator") or "").upper(),
                    "lat": round(lat, 5),
                    "lon": round(lon, 5),
                    "country": "FI",
                    "source": "SRAL",
                    "channel": str(st.get("channel_number") or ""),
                })
    except Exception as exc:
        log.warning("SRAL repeater fetch failed: %s", exc)

    # 2. HearHam API filtered to Nordic coordinates (SE, NO, DK, FI, IS)
    try:
        hh = _fetch_json("https://hearham.com/api/repeaters/v1", timeout=22.0)
        if isinstance(hh, list):
            for st in hh:
                try:
                    lat = float(st.get("latitude") or 0)
                    lon = float(st.get("longitude") or 0)
                except Exception:
                    continue
                if not _is_nordic_coords(lat, lon):
                    continue
                try:
                    rx_hz = int(round(float(st.get("frequency") or 0)))
                    offset_hz = int(round(float(st.get("offset") or 0)))
                except Exception:
                    continue
                mode_raw = str(st.get("mode") or "FM").upper()
                if "APRS" in mode_raw or "PACKET" in mode_raw:
                    continue
                city_str = str(st.get("city") or "").strip()
                notes_str = f"{st.get('callsign', '')} {city_str} {st.get('notes', '')}".upper()
                if abs(offset_hz) <= 1000 and ("DMR" in mode_raw or any(k in notes_str for k in ("HOTSPOT", "OPENSPOT", "MICRO-NODE", "PIRLP", "SPOT", "MMDVM"))):
                    continue
                ctcss = 0.0
                tsql = 0.0
                tone_1750 = False
                enc = str(st.get("encode") or "").strip()
                dec = str(st.get("decode") or "").strip()
                for s_val, tgt in ((enc, "enc"), (dec, "dec")):
                    if "1750" in s_val:
                        tone_1750 = True
                    else:
                        try:
                            f_t = float(s_val)
                            if 60.0 <= f_t <= 260.0:
                                if tgt == "enc":
                                    ctcss = f_t
                                else:
                                    tsql = f_t
                        except Exception:
                            pass
                country = "SE" if "SWEDEN" in city_str.upper() else (
                    "NO" if "NORWAY" in city_str.upper() else (
                        "DK" if "DENMARK" in city_str.upper() else (
                            "FI" if "FINLAND" in city_str.upper() else "NORDIC"
                        )
                    )
                )
                _add({
                    "callsign": st.get("callsign", ""),
                    "rx_hz": rx_hz,
                    "offset_hz": offset_hz,
                    "tx_hz": rx_hz + offset_hz,
                    "mode": mode_raw,
                    "is_dstar": ("FM" not in mode_raw) and ("DSTAR" in mode_raw or "D-STAR" in mode_raw),
                    "ctcss_hz": ctcss,
                    "tsql_hz": tsql if tsql != ctcss else 0.0,
                    "tone_1750": tone_1750,
                    "status": "QRV" if st.get("operational", 1) else "QRT",
                    "city": city_str.split(",")[0].strip(),
                    "locator": "",
                    "lat": round(lat, 5),
                    "lon": round(lon, 5),
                    "country": country,
                    "source": "HearHam",
                    "channel": "",
                })
    except Exception as exc:
        log.warning("HearHam Nordic fetch failed: %s", exc)

    # 3. BrandMeister Nordic repeaters (FI 244, SE 240, NO 242, DK 238, IS 274)
    try:
        bm = _fetch_json("https://api.brandmeister.network/v2/device", timeout=22.0)
        mcc_map = {"244": "FI", "240": "SE", "242": "NO", "238": "DK", "274": "IS"}
        if isinstance(bm, list):
            for st in bm:
                sid = str(st.get("id") or "").strip()
                prefix = sid[:3]
                # True BrandMeister repeaters use 6-digit IDs (MCC + 3 digits); 7-9 digit IDs are personal hotspots
                if prefix not in mcc_map or len(sid) != 6:
                    continue
                hw = str(st.get("hardware") or "").upper()
                if any(k in hw for k in ("OPENSPOT", "MMDVM_DMO", "MMDVM_HS_HAT", "PI-STAR")):
                    continue
                try:
                    rx_hz = int(round(float(st.get("tx") or 0) * 1_000_000))
                    tx_in_hz = int(round(float(st.get("rx") or 0) * 1_000_000))
                except Exception:
                    continue
                if rx_hz < 28_000_000 or tx_in_hz < 28_000_000:
                    continue
                offset_hz = tx_in_hz - rx_hz
                # Exclude simplex/zero-shift hotspots
                if abs(offset_hz) <= 1000:
                    continue
                try:
                    lat = float(st.get("lat") or 0)
                    lon = float(st.get("lng") or 0)
                except Exception:
                    lat, lon = 0.0, 0.0
                _add({
                    "callsign": st.get("callsign", ""),
                    "rx_hz": rx_hz,
                    "offset_hz": offset_hz,
                    "tx_hz": rx_hz + offset_hz,
                    "mode": "DMR",
                    "is_dstar": False,
                    "ctcss_hz": 0.0,
                    "tsql_hz": 0.0,
                    "tone_1750": False,
                    "status": "QRV" if int(st.get("status") or 0) > 0 else "QRT",
                    "city": str(st.get("city") or "").strip(),
                    "locator": "",
                    "lat": round(lat, 5),
                    "lon": round(lon, 5),
                    "country": mcc_map[prefix],
                    "source": "BrandMeister",
                    "channel": f"CC{st.get('colorcode', 1)}",
                })
    except Exception as exc:
        log.warning("BrandMeister Nordic fetch failed: %s", exc)

    stations = sorted(merged.values(), key=lambda x: (x["rx_hz"], x["callsign"]))
    log.info("Merged %d unique Finnish & Nordic repeaters", len(stations))
    return stations


class RepeaterDatabase:
    """Manages local 30-day cached Nordic repeater list without blocking audio/CI-V threads."""

    def __init__(self) -> None:
        self.fetched_at: float = 0.0
        self.stations: List[Dict[str, Any]] = []
        self._refreshing: bool = False
        self._load_disk_cache()

    def _load_disk_cache(self) -> None:
        if not CACHE_PATH.is_file():
            seed = cfg_mod.RESOURCE_DIR / "repeaters_cache.json"
            if seed.is_file():
                try:
                    CACHE_PATH.write_bytes(seed.read_bytes())
                except Exception:
                    pass
        src = CACHE_PATH if CACHE_PATH.is_file() else (cfg_mod.RESOURCE_DIR / "repeaters_cache.json")
        if not src.is_file():
            return
        try:
            raw = json.loads(src.read_text(encoding="utf-8"))
            self.fetched_at = float(raw.get("fetched_at", 0.0))
            self.stations = list(raw.get("stations") or [])
            log.info("Loaded %d cached Nordic repeaters from %s", len(self.stations), CACHE_PATH.name)
        except Exception as exc:
            log.warning("Failed reading repeater cache: %s", exc)

    def is_expired(self) -> bool:
        if not self.stations or self.fetched_at <= 0:
            return True
        return (time.time() - self.fetched_at) >= CACHE_TTL_SEC

    async def ensure_fresh(self, force: bool = False) -> None:
        if self._refreshing:
            return
        if not force and not self.is_expired():
            return
        self._refreshing = True
        try:
            stations = await asyncio.to_thread(_fetch_and_merge_nordic_repeaters)
            if stations:
                self.stations = stations
                self.fetched_at = time.time()
                payload = {"fetched_at": self.fetched_at, "stations": self.stations}
                await asyncio.to_thread(
                    CACHE_PATH.write_text,
                    json.dumps(payload, ensure_ascii=False),
                    "utf-8",
                )
        finally:
            self._refreshing = False

    def get_payload(self) -> Dict[str, Any]:
        age_days = round(max(0.0, time.time() - self.fetched_at) / 86400.0, 1) if self.fetched_at else 999.0
        return {
            "fetched_at": int(self.fetched_at),
            "cache_age_days": age_days,
            "ttl_days": 30,
            "count": len(self.stations),
            "stations": self.stations,
        }
