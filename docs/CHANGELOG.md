# Changelog

- **2026-11-20 — GitHub Update & Documentation Cleanup:** Replaced Host LAN `/api/update/*` with background GitHub commit checks against `jorksx/RemoteHam` (`⬇ Update Available` shown only when a different/newer version is available, auto-download & restart for both `.exe` and source runs); cleaned up `README.md` and added `screenshots/` (`single-view.png`, `dual-view.png`, `swr-view.png`, `mobile-view.png`).
- **2026-11-20 — High-SWR Warning Overlay:** Added centered blinking `⚠ HIGH SWR WARNING` (`SWR > 2.5`) on the waterfall with 5s fade-out after PTT release and doubled TX SWR CI-V polling frequency.
- **2026-11-20 — SWR Scan on Waterfall:** Added `SWR SCAN` button sweeping the visible waterfall span at the radio's active TX Power setting and plotting the live SWR curve directly over the 2D waterfall (`✕ SWR` to clear).
- **2026-11-20 — Voice Memory Timer:** Added `TIMER` toggle + `1–10s` interval selector with live countdown and automatic repeat, cancelled cleanly on manual PTT or `TIMER` toggle (fixed asyncio self-cancel bug on re-keying PTT).
- **2026-11-20 — Audio Passthrough + Mic Ignore Selector:** Added Windows WASAPI loopback passthrough (`AUDIO PASSTHROUGH`), `Mic Ignored` / `Mic Not Ignored` dropdown, and yellow `!` warning indicators beside `PTT` and `PTT HOLD`.
- **Earlier Milestones:** Combined 3D + 2D waterfall (`WF Track`), 4-style clickable S-meter, CW memory buttons + live free-text sender + software sidetone, RX/TX QSO WAV recorder with save dialog, Mobile HTTPS UI, Hamlib `rigctld`, Nordic Repeaters + 1750 Hz tone burst.
