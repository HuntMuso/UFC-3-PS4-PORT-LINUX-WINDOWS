# UFC 3 modern launcher

An Epic-Games-Launcher-style front end (sidebar, hero banner, big Play button) for the native port.
Standard library Python plus the Edge/Chrome already on the PC; nothing to compile.

## What it keeps
Every option of `packaging/windows/sacredfire_launcher.c`: game folder (Browse), player name, language,
controller (+ rescan), the keyboard/gamepad Controls editor, Offline / Join / Host (address scan, Copy IP),
game resolution, display resolution, present mode, FPS limit, upscaling + sharpening, Fullscreen, PS4 Pro
mode, FPS counter, skip intro videos, clear shader cache, frame statistics, Vulkan validation, menu trace,
log folder, Open logs / Open data, Play / Stop, live log, Add to Steam.

It reads and writes the same `<data>\sacredfire.ini` and `bbport.ini` as the old window, so both can be
used on the same install. On Windows, Play runs the existing `UFC3.exe --play` (the proven preparation
pipeline is untouched); on Linux it uses `launcher/ufc3_online.py` like the GTK launcher.

## Install
Copy `launcher/modern/`, `packaging/windows/UFC3-Launcher.cmd` and `tests/test_modern_launcher.py` into the
fork. Package `launcher\modern\` next to `UFC3.exe` and `UFC3-Launcher.cmd` at the package root.
Linux: `launcher/modern/ufc3-modern-launcher.sh`. Headless check: `--no-window` prints the local URL.

## DLSS 4 (replaces nothing until the engine has it)
The option is a card in Video -> Upscaling. It is unlocked only when ALL of these hold:
1. an NVIDIA GeForce RTX GPU is present (AMD, Intel, GTX: greyed out with the reason shown);
2. `share/engine-features.json` contains `{"dlss4": true}` (written by an engine build that has the module);
3. `nvngx_dlss.dll` is in `bin\`;
4. the crash guard is not tripped.
Until the engine implements DLSS, check 2 fails and the card stays locked, so a selection can never reach an
engine that cannot honour it. If a saved DLSS choice becomes unavailable (GPU swap, guard), the game runs with
FSR 1.0 and the UI says so.

Crash guard: if a DLSS session ends with exit code 3, 23, 142, an NTSTATUS fault (>= 0xC0000000) or a signal, or
the launcher never saw it end (PC/driver crash), DLSS switches itself off until the user clicks Re-enable.
Stop by the user, launcher errors (code 2) and non-DLSS crashes never touch it.

### Contract for the engine's DLSS module
Environment set by the launcher only when DLSS is active: `BB_UPSCALER=dlss4`,
`BB_DLSS_MODE=quality|balanced|performance|dlaa`, `BB_DLSS_FALLBACK=fsr1`, `BB_FSR1=0`.
The engine must (a) fall back to the FSR 1.0 present path itself when NGX/Streamline fails to initialise
instead of aborting, (b) feed DLSS jitter, depth and motion vectors for the UFC 3 scene (the current
temporal-upscaler code is Bloodborne-specific and off for this game, see gpu/shim/bbport_settings.cpp),
(c) ship `nvngx_dlss.dll` under NVIDIA's redistribution terms, and (d) write
`share/engine-features.json` when built with the module. FSR 1.0 stays in the list as the safe fallback
and the only upscaler for non-RTX cards; removing it is one entry in `upscaler_options()`.

## Tests
`python -m unittest tests.test_modern_launcher` (37 tests: ini compatibility, injection, DLSS gates, env
mapping, crash guard with a fake game, HTTP token/Host/CSRF checks).
