# Architecture & File Map

| File | Role |
|---|---|
| `app.py` | Application entrypoint (pywebview frameless desktop UI / `--browser` / `--headless` / `--client IP:PORT`) and `live_app` frozen bootstrapper. |
| `controller.py` | Central `AppController` coordinating CI-V radios, `AudioEngine`, `HamlibServerManager`, `GlobalPttMonitor`, `RepeaterDatabase`, Host/Client `RemoteLink`, Voice Memory timer, and GitHub Auto-Update. |
| `radio_civ.py` | Asynchronous Icom CI-V driver (`IcomCivRadio`): USB auto-discovery, Dual-RX Main/Sub, `0x27` spectrum scope, `0x17` CW macro keying, SWR sweep scan (`start_swr_scan`), meters & levels. |
| `audio_engine.py` | Real-time 48 kHz `sounddevice` + `StereoRingBuffer` engine: per-radio RX/TX streams, Windows speaker routing, mobile mute suppression, QSO WAV recording, Voice Memory playback/recording, CW software sidetone, and Audio Passthrough mixing. |
| `wasapi_loopback.py` | Pure-`ctypes` Windows WASAPI shared loopback capture (`WasapiLoopbackCapture`) for streaming Windows Default Playback audio into Radio TX without external dependencies. |
| `remote_link.py` | Host `RemoteLinkServer` (`/ws/link`) and `RemoteLinkClient` for tunneling UI state, commands, waterfalls, and RX/TX PCM audio to remote PCs. |
| `web_server.py` | Local desktop HTTP + WebSocket UI server (`/ws/ui`, `/api/*`, `/api/app_update/*`) and Mobile HTTPS server (`/mobile`). |
| `hamlib_server.py` | TCP `rigctld`-compatible emulator (ports `4532`–`4535`) for WSJT-X, FT8, N1MM, and loggers. |
| `ptt_hook.py` | Optional global Windows hotkey / mouse button PTT monitor (`GlobalPttMonitor`). |
| `repeaters.py` | Nordic/Finnish repeater database (`repeaters_cache.json`) with live frequency match & offset/CTCSS/1750 Hz helper. |
| `usb_discovery.py` | Automatic USB serial port & USB Audio CODEC matching. |
| `tls_util.py` | Self-signed certificate helper for Mobile HTTPS. |
| `config.py` | Persistent configuration loader/merger (`config.json`, stored in `%APPDATA%\DualRigRemote` when frozen). |
| `static/index.html`, `styles.css`, `app.js`, `waterfall3d.js` | Desktop/Web cockpit UI & 60 fps Dual 3D/2D Scope + Waterfall Canvas renderer. |
| `static/mobile.html`, `mobile.js` | Mobile HTTPS Web UI. |
