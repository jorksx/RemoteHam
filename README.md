# RemoteHam — Dual-Rig Amateur Radio Remote

Desktop, Remote PC Client, Mobile HTTPS, and Hamlib `rigctld` remote control suite for **Icom CI-V** transceivers (e.g., IC-7300 + IC-9700) over USB/Serial with real-time 3D/2D waterfall, audio routing, CW keying, Voice Memory, SWR antenna scanning, and Windows audio passthrough.

> **Git / Push Policy:** Changes are kept local until the repository owner explicitly asks to push to GitHub (`https://github.com/jorksx/RemoteHam`). Do **not** auto-push on edits.

---

## Architecture & File Map

| File | Role |
|---|---|
| `app.py` | Application entrypoint (pywebview frameless desktop UI / `--browser` / `--headless` / `--client IP:PORT`) and `live_app` frozen bootstrapper. |
| `controller.py` | Central `AppController` coordinating CI-V radios, `AudioEngine`, `HamlibServerManager`, `GlobalPttMonitor`, `RepeaterDatabase`, Host/Client `RemoteLink`, Voice Memory timer, and **GitHub Auto-Update**. |
| `radio_civ.py` | Asynchronous Icom CI-V driver (`IcomCivRadio`): USB auto-discovery, Dual-RX Main/Sub, `0x27` spectrum scope, `0x17` CW macro keying, SWR sweep scan (`start_swr_scan`), meters & levels. |
| `audio_engine.py` | Real-time 48 kHz `sounddevice` + `StereoRingBuffer` engine: per-radio RX/TX streams, Windows speaker routing (left/right/both/mute), mobile mute suppression, QSO WAV recording, Voice Memory playback/recording, CW software sidetone, and Audio Passthrough mixing. |
| `wasapi_loopback.py` | Pure-`ctypes` Windows WASAPI shared loopback capture (`WasapiLoopbackCapture`) for streaming Windows Default Playback audio (e.g., browser/YouTube) into Radio TX without external dependencies. |
| `remote_link.py` | Host `RemoteLinkServer` (`/ws/link`) and `RemoteLinkClient` for tunneling UI state, commands, waterfalls, and RX/TX PCM audio to remote PCs. |
| `web_server.py` | Local desktop HTTP + WebSocket UI server (`/ws/ui`, `/api/*`, `/api/app_update/*`) and Mobile HTTPS server (`/mobile`). |
| `hamlib_server.py` | TCP `rigctld`-compatible emulator (ports `4532`–`4535`) for WSJT-X, FT8, N1MM, loggers, etc. |
| `ptt_hook.py` | Optional global Windows hotkey / mouse button PTT monitor (`GlobalPttMonitor`). |
| `repeaters.py` | Nordic/Finnish repeater database (`repeaters_cache.json`) with live frequency match & offset/CTCSS/1750 Hz helper. |
| `usb_discovery.py` | Automatic USB serial port & USB Audio CODEC matching. |
| `tls_util.py` | Self-signed certificate helper for Mobile HTTPS. |
| `config.py` | Persistent configuration loader/merger (`config.json`, stored in `%APPDATA%\DualRigRemote` when frozen). |
| `static/index.html`, `styles.css`, `app.js`, `waterfall3d.js` | Desktop/Web cockpit UI & 60 fps Dual 3D/2D Scope + Waterfall Canvas renderer. |
| `static/mobile.html`, `mobile.js` | Mobile HTTPS Web UI. |

---

## Key Features

### 1. Dual-Rig Control & 60 fps Waterfall (`radio_civ.py`, `waterfall3d.js`)
- Simultaneous or single-focus control of two Icom transceivers over CI-V (`IC-7300`, `IC-9700`, etc.).
- Combined **Yaesu 3DSS-style 3D spectrum** + crisp **2D Icom waterfall** with:
  - Smooth **WF Track** mode shifting waterfall history smoothly when scrolling VFO (without lag).
  - Click-to-tune, mouse-wheel tuning, hover frequency crosshair, QRV repeater markers, and adjustable Span (`±25k` to `±250k`).
- **4 Switchable S-Meter Designs:** Click the S-meter canvas to cycle across 4 distinct visual meter styles (`ANALOG`, `CURVE`, `VU`, `DIGITAL`).

### 2. SWR Antenna Sweep & High-SWR Warning (`radio_civ.py`, `waterfall3d.js`, `app.js`)
- **`SWR SCAN`** (waterfall toolbar): Sweeps across the exact frequency span currently visible in the waterfall (`17` or `21` points) using the transceiver's active TX Power slider setting, samples settled SWR values (`0x15 0x12`), draws the live color-coded SWR curve (`1.0–3.5`) directly over the 2D waterfall with `MIN` badge & hover SWR readout, and restores original frequency and mode cleanly. Includes `✕ SWR` clear button.
- **High-SWR Warning Overlay:** Whenever TX SWR (or any TX peak) exceeds **`2.5`**, a centered blinking **`⚠ HIGH SWR WARNING`** + **`SWR X.XX`** box appears over the waterfall for the entire duration of PTT, then smoothly fades out over **5 seconds** after PTT release.

### 3. CW Keyer & Local Sidetone (`radio_civ.py`, `audio_engine.py`)
- **4 Programmable CW Memory Buttons (`CW1`–`CW4`)** (right-click to edit, left-click to key/abort) + **Full-Width Live Free-Text CW Input + `SEND` button** (supports Enter to send & keep input focused for continuous QSO typing, plus `ADD TO TX` / `STOP CW` when already keying).
- Automatic **±1 kHz SSB zero-beat mode/frequency shift** when keying CW while parked in `LSB`/`USB`, automatically restoring original SSB mode/frequency afterward.
- Left-sidebar **CW WPM Speed slider** above `BK-IN / TONE / TSQL`.
- **Windows Software CW Sidetone (`render_cw_sidetone`)**: Plays clean 700 Hz CW sidetone in Windows speakers synchronized with live CI-V `0x17` transmissions.

### 4. Voice Memory + Auto-Repeat Timer (`audio_engine.py`, `controller.py`)
- 4 recordable Voice Memory slots (`V1`–`V4`) saved as 48 kHz mono WAVs in `voice_memories/`.
- Combined single `● REC` / red `■ STOP` button.
- **`TIMER` + `1s–10s` Interval Dropdown:** When enabled and a Voice Memory slot is activated, runs a visible countdown after each transmission ends (`V1 IN X.Xs` / `⏱ X.Xs`) and automatically re-keys TX until cancelled by manual PTT or toggling `TIMER` off.

### 5. Audio Passthrough + RX/TX WAV Recorder (`wasapi_loopback.py`, `audio_engine.py`)
- **`AUDIO PASSTHROUGH`**: Captures Windows Default Playback device via pure-`ctypes` WASAPI loopback and routes audio straight into Radio TX on PTT (on both Host PC and Remote Client PC).
- Includes **`Mic Ignored` / `Mic Not Ignored`** dropdown when active (ignoring PC microphone so background VoIP/Discord voice does not transmit, or mixing PC mic with loopback when desired).
- Displays yellow warning triangles (`!`) with **`Passthrough Active`** hover tooltip beside **`PTT`** and **`PTT HOLD`**.
- **`● REC` (RX + TX Recorder)**: Records combined live RX + TX audio to WAV and prompts with a Windows Save File dialog upon stopping.

### 6. GitHub Automatic Version Check & Update (`controller.py`, `web_server.py`, `app.js`)
- Local LAN/Host bundle update has been removed; **all instances (Host & Client)** check `https://github.com/jorksx/RemoteHam` (`main` branch) automatically in the background on startup and every 10 minutes.
- If the local version matches GitHub (or is ahead with local commits), **no update button is shown**.
- If GitHub has a newer commit, **`⬇ Update Available`** appears in the top bar; clicking it downloads the ZIP directly from GitHub, safely updates application files (preserving local `config.json`, `certs/`, `recordings/`, `voice_memories/`), and restarts automatically.

---

## Running Locally

```bat
python app.py
```
Or launch in Windows desktop mode with `start_remote.bat`, browser UI (`python app.py --browser`), or Client mode (`python app.py --client <HOST_IP>:8090`).

---

## Recent Changelog
- **2026-11-20 — GitHub Update Migration & Docs:** Removed Host LAN `/api/update/*` endpoints; added startup/background GitHub version check against `jorksx/RemoteHam` showing `⬇ Update Available` only when a different/newer version exists; auto-downloads & restarts cleanly in both frozen `.exe` and source runs; added `.gitignore` and `README.md`.
- **2026-11-20 — High-SWR Warning Overlay:** Added centered blinking `⚠ HIGH SWR WARNING` (`SWR > 2.5`) on the waterfall with 5s fade-out after PTT release and faster TX SWR CI-V polling.
- **2026-11-20 — SWR Scan on Waterfall:** Added `SWR SCAN` button sweeping the visible waterfall span at the radio's configured TX Power and plotting the live SWR curve directly over the 2D waterfall (`✕ SWR` to clear).
- **2026-11-20 — Voice Memory Timer:** Added `TIMER` toggle + `1–10s` interval selector with live countdown and automatic repeat, cancelled cleanly on manual PTT or `TIMER` toggle (fixed asyncio self-cancel bug on re-keying PTT).
- **2026-11-20 — Audio Passthrough + Mic Ignore Selector:** Added Windows WASAPI loopback passthrough (`AUDIO PASSTHROUGH`), `Mic Ignored` / `Mic Not Ignored` dropdown, and yellow `!` warning indicators beside `PTT` and `PTT HOLD`.
- **Previous Milestones:** Combined 3D + 2D waterfall (`WF Track`), 4-style clickable S-meter, CW memory buttons + live free-text sender + software sidetone, RX/TX QSO WAV recorder with save dialog, Mobile HTTPS UI, Hamlib `rigctld`, Nordic Repeaters + 1750 Hz tone burst.
