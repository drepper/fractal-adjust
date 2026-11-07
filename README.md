# fractal-adjust

Control the **Fractal Adjust Pro Hub** (USB `36bc:1001`), a combined fan and ARGB lighting
hub, from Linux without a browser.

Fractal's own software is the web application at adjust.fractal-design.com, which talks to
the hub over WebHID. `fractal-adjust` speaks the same protocol directly through Linux
`hidraw`. The protocol was reverse-engineered from that application and is documented in
[`PROTOCOL.md`](PROTOCOL.md).

This is an independent project, not affiliated with or supported by Fractal Design.

## Features

- **GTK 4 / libadwaita interface**, started by default:
  - LED switch.
  - Effect editor with live preview.
  - Startup ("boot") effect.
  - Draggable fan curves with the current RPM and duty.
  - Device information.
- **Lighting effects** designed on the computer and stored in the hub:
  - off, still, breathe, blink, shift, two-color fade, lava lamp and wave ripple;
  - startup effects meshify, fade-in and instant;
  - colour, speed and brightness for each.

  Effects can also be shown temporarily, after which the previous one returns.
- **Fan curves** per port, mapping the motherboard's PWM duty to the fan duty.
- **Configuration file** (TOML) that is applied whenever the program starts. Only settings
  that differ from the hub's state are written, which spares the hub's flash memory.
- **Command line interface** for scripting, with JSON output.
- **MQTT remote control** while the GUI runs, with JSON requests and replies; see
  [`MQTT.md`](MQTT.md).

## Requirements

- Linux with `hidraw`, and Python 3.14 or later.
- PyGObject with GTK 4 and libadwaita, and pycairo, for the GUI.
- paho-mqtt (version 2 or later) for MQTT.
- Access to the hub's hidraw device; see below.

## Installation

### RPM (Fedora)

```
make rpm
sudo dnf install ./rpms/noarch/fractal-adjust-*.noarch.rpm
```

The package installs:
- the program;
- a udev rule that gives the logged-in user access to the hub;
- the desktop entry and icon;
- the default configuration in `/etc/fractal-adjust/config.toml`;
- an optional systemd user unit that applies the configuration at login
  (`systemctl --user enable fractal-adjust`).

To pick up the udev rule without re-plugging, replug the hub or run
`sudo udevadm trigger --subsystem-match=hidraw`.

### From the source tree

```
python3 -m fractal_adjust            # GUI
python3 -m fractal_adjust status     # CLI
```

Without the package's udev rule, the hidraw device is usually accessible only to root.
Install the rule from the tree:

```
sudo cp data/70-fractal-adjust.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw
```

## Usage

### GUI

```
fractal-adjust [--minimized] [--no-apply]
```

On startup the GUI finds the hub and applies the configuration file. The window has three
pages:

- **Lighting:**
  - the LED switch, which acts immediately;
  - the effect, with **Live preview** (shown, not stored) and **Store on hub**;
  - the startup effect.
- **Fans:** one curve per port. Drag a point to move it, double-click to add one and
  right-click to remove one. The dashed line marks the motherboard's current duty.
  **Store on hub** writes the curves.
- **Device:** firmware, serial number, connected devices and the MQTT connection state.

**Save** writes the settings shown in the window to `~/.config/fractal-adjust/config.toml`.
They are applied again at the next start.

### Command line

| command | purpose |
|---------|---------|
| `list` | list connected hubs |
| `info` | firmware, serial, port types |
| `status [-w SECONDS]` | fan duty and RPM, optionally repeated |
| `devices` | ARGB devices daisy-chained on each port |
| `effects` | effects stored for each device |
| `effect MODE [COLOR…] [--speed N] [--brightness N] [--id ID] [--timeout SECONDS]` | store an effect, or show it for a while |
| `curve [PORTS]` | show fan curves |
| `set-curve PORTS POINTS` | store a fan curve |
| `light on\|off [ID…]` | switch the LEDs |
| `slot N` | activate a stored effect slot |
| `apply [--force]` | apply the configuration file |
| `config` | show the effective configuration and where it comes from |
| `raw TYPE CMD [ARG…]` | send a raw protocol command (for protocol exploration only) |

Global options:

- `-d DEVICE`, `-s SERIAL`: select the hub if there is more than one.
- `-c CONFIG`: use a different configuration file.
- `-j`: JSON output.
- `-t SECONDS`: how long to wait for each reply from the hub.

Examples:

```
fractal-adjust status
fractal-adjust effect shift --speed 40
fractal-adjust effect breathe '#0074c5' '#000000' --speed 30 --brightness 80
fractal-adjust effect blink '#ff0000' --timeout 30      # 30 s, then back to the previous effect
fractal-adjust effect meshify '#ffffff'                 # startup effect
fractal-adjust set-curve 1,2 0:20,40:30,70:60,100:100
fractal-adjust light off
fractal-adjust --json status
```

Ports are numbered 1–4. Device IDs (shown by `devices`) are hexadecimal; the high digit is the
port, counted from 0. Brightness and speed range from 0 to 100. A fan curve is a list of
`motherboard%:fan%` points from 0 to 100.

## Configuration

The defaults are part of the package (`fractal_adjust/data/config.toml`). The following
files override individual keys, each one taking precedence over the files before it:

1. `/etc/fractal-adjust/config.toml`
2. `~/.config/fractal-adjust/config.toml`, which the GUI's Save button writes

```toml
[lighting]
on = true                      # LEDs on or off

[lighting.effect]              # effect while the system runs
mode = "shift"
speed = 40
brightness = 100
colors = ["#0074c5", "#6e00fa", "#00faeb", "#c940bd", "#95fde4", "#00f03c"]

[lighting.boot]                # effect at power-up
mode = "meshify"
brightness = 100
colors = ["#ffffff"]

[fans]                         # [motherboard duty %, fan duty %] per port
port1 = [[0, 0], [20, 20], [40, 40], [60, 60], [80, 80], [100, 100]]

[mqtt]                         # see MQTT.md
enabled = false
host = "localhost"
topic = "fractal_adjust"
```

`fractal-adjust config` prints the effective configuration. Because the file can contain an
MQTT password, Save writes it readable only by its owner.

## MQTT

When `[mqtt] enabled = true`, the running GUI subscribes to the configured topic. Each message
is a JSON object naming a command and its parameters, for example:

```json
{"id": 1, "command": "effect", "mode": "blink", "colors": ["#ff0000"], "timeout": 30}
```

The reply is published to `<topic>/reply` as JSON: `{"id": 1, "command": "effect", "ok": true,
"result": {...}}`, or `"ok": false` with an `"error"` message. The commands are the hub
commands of the CLI. Requests never change the configuration file.

Anyone who can publish to the topic controls the hub, so restrict access on the broker.
[`MQTT.md`](MQTT.md) has the details.

## Notes and limitations

- **Legacy devices:** ARGB1 devices report no addressable LEDs and the hub rejects effects for
  them. They are skipped when effects are uploaded, but the LED switch still covers them.
- **Effect timing:** the hub's keyframe time unit is assumed to be 10 ms. That is inferred from
  the official application's effects, not measured.
- **Flash wear:** effects, curves and port settings are written to the hub's flash memory.
  Previews and timed effects are not stored.
- **Untested firmware:** the tool was developed against firmware 1.1.5. Other versions should
  work if they speak the same protocol; product ID `0x1002` is accepted as well.
- **Temporary effects in `effects`:** while a temporary effect runs, the hub reports it in
  place of the stored effect. `effects` marks it "(temporary)".

## Development

```
make check            # pytest and strict mypy
make spec             # render fractal-adjust.spec from fractal-adjust.spec.in
make dist / make rpm  # source tarball / binary RPM in rpms/
```

The tests use a fake hub that replays replies captured from real hardware, so no device is
needed.

Every packaging value has exactly one source:

- version: `fractal_adjust/__init__.py`;
- name, summary, license, author and dependencies: `pyproject.toml`;
- application ID: `fractal_adjust/gui.py`;
- release: the spec template.

`tools/gen-spec.py` fills them into the spec.

| file | contents |
|------|----------|
| `fractal_adjust/hub.py` | protocol layer (hidraw transport, commands, effect upload) |
| `fractal_adjust/effects.py` | effect design: metadata and keyframes, as the official app does it |
| `fractal_adjust/config.py` | configuration loading, saving and applying |
| `fractal_adjust/service.py` | single hub thread and timed effects for the GUI and MQTT |
| `fractal_adjust/mqtt.py` | MQTT bridge |
| `fractal_adjust/remote.py` | JSON request handling for MQTT |
| `fractal_adjust/cli.py`, `gui.py` | user interfaces |
| `PROTOCOL.md`, `MQTT.md` | protocol and MQTT documentation |

## License

[CC BY-NC-ND 4.0](LICENSE). Copyright Ulrich Drepper.
