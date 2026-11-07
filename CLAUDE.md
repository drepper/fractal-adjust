# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`fractal-adjust` controls the Fractal Adjust Pro Hub (USB `36bc:1001`, also `1002`) through
Linux `/dev/hidrawN`. Without a command it starts a GTK 4/libadwaita GUI; there is also a CLI.
The wire protocol was reverse-engineered from Fractal's WebHID app and is documented in
`PROTOCOL.md`. Keep that file in sync when adding commands. The MQTT request interface is
documented in `MQTT.md`; update it when `remote.HANDLERS` or a handler's parameters change.

## Commands

- Run from the tree: `python3 -m fractal_adjust [command]` (no command → GUI)
- Tests: `make check`, which runs pytest and strict mypy. One test:
  `python3 -m pytest -q -k upload_sequence`.
- Packaging: `make spec` / `make dist` / `make rpm` (the RPM ends up in `rpms/`).
- Every packaging value has exactly one source. Version: `__version__` in
  `fractal_adjust/__init__.py`. Name, summary, license, author, Python dependencies and the
  module name: `pyproject.toml`. App ID: `APP_ID` in `gui.py`, which also names the installed
  desktop file. Release: the `%global baserelease` line in `fractal-adjust.spec.in`.
  `tools/gen-spec.py` renders `fractal-adjust.spec` from the template, failing on unknown
  placeholders, and gives the Makefile name and version (`--get KEY`). Never put these values
  into the template or the Makefile literally; `fractal-adjust.spec` is generated and must not
  be edited.

## Architecture

- `hub.py`: `Transport` protocol → `HidrawTransport` (flock'd hidraw fd). `Hub.transact` is the
  only place that touches the wire: it sends report ID 2 plus 63 bytes and matches the reply by
  its type/cmd echo. Tests swap in `FakeTransport`, which replays replies captured from real
  hardware (`CAPTURED`).
- `effects.py`: a port of the web app's effect classes. `metadata()` builds the stored
  settings block, and `keyframes()` builds the per-LED keyframe data. Rounding mimics JS
  (`Math.round`, `Uint8Array` truncation), so output matches the app. `Hub.upload_effect`
  sends the result per sub-device. Legacy sub-devices (0 LEDs) are skipped because they reject
  effects.
- `config.py`: the packaged `fractal_adjust/data/config.toml` is the single source of defaults.
  `/etc/fractal-adjust/config.toml` and then `~/.config/fractal-adjust/config.toml` are
  deep-merged over it. `dump()` writes a commented TOML file, and the GUI's Save uses it.
  `apply()` writes effects and curves only when they differ from the hub's stored state, to
  spare its flash.
- `gui.py`: all hub I/O runs on one worker thread (`Worker`), and results return via
  `GLib.idle_add`. Startup applies the config. The LED switch acts immediately, effects and
  curves need "Store on hub", "Live preview" uses non-persistent preview uploads, and Save
  writes the config file.
- `service.py`: `HubService` owns the single hub thread used by the GUI and MQTT, and the
  stack of timed effects (`effect --timeout`). These are non-persistent previews; when one
  expires, the remaining ones are replayed or the preview is cancelled.
- `mqtt.py` + `remote.py`: only the GUI serves MQTT, and only when `[mqtt] enabled`. A request
  is a JSON object (`command`, optional `id`, parameters as members). `remote.HANDLERS` maps
  commands to handlers that call the hub layer directly and return JSON-serializable results.
  `Params` rejects wrong types and unknown members. The reply is
  `{"id", "command", "ok", "result" | "error"}`. Requests never write the config.
- `A4 05` reads the *active* effect, a running preview included (preset `0x99`).
- User-facing ports are 1-based and the wire uses 0-based. The device brightness byte is half
  the 0–100 UI value.

## Hardware caution

Effects, curves, port types and names are written to the hub's persistent memory. When
testing against the real device, prefer previews (`effect … --preview SECONDS`), read-only
commands, or writes that restore the current value. Never send firmware-upgrade (`B5`) or
factory-reset commands.
