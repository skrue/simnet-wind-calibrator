# Changelog

## 2.4.2 — 2026-10-15

- Switch project licensing from MIT to Apache License 2.0.
- Move offline tests and terminal smoke test into `tests/`.
- Update test commands and GitHub Actions for the new layout.

## 2.4.1 — 2026-10-15

- Save session logs in `logs/` instead of the project root.
- Ask whether to enable logging during gateway setup (default Yes).
- Support `--logging` / `--no-logging`, `--no-prompt`, and optional `--log-dir`.
- Continue showing on-screen status when file logging is disabled.

## 2.4.0 — 2026-10-14

- Simplified the project to the calibration application and its essential CAN RAW ASCII transport.
- Added a real screenshot to the README.
- Added post-write readback verification and safeguards against device replacement at the same CAN address.
- Retained Windows, macOS, and Linux launchers and automatic gateway setup.

## 2.3.x — 2026-10-14

- Refined the English terminal UI, device identification, and live AWA/AWS tiles.
- Added startup gateway prompts and a Windows launcher.
