# MQTT interface

While the `fractal-adjust` GUI is running, it can take commands over MQTT. A request is a
command line in the same syntax as the CLI. The command runs against the hub, and its output
is published as the reply. Only the GUI serves requests; the CLI commands never connect to a
broker.

## Setup

MQTT is configured in the `[mqtt]` section of the configuration file. The GUI's Save button
writes `~/.config/fractal-adjust/config.toml`, and settings missing there fall back to
`/etc/fractal-adjust/config.toml` and the packaged defaults.

```toml
[mqtt]
enabled = true            # connect while the GUI runs (default: false)
host = "localhost"
port = 1883
topic = "fractal_adjust"  # requests are read from this topic
reply_topic = ""          # replies go here; empty means "<topic>/reply"
username = ""             # empty: connect anonymously
password = ""
tls = false               # TLS with the system's CA certificates
client_id = ""            # empty: the client library picks a random ID
keepalive = 60            # seconds
```

`topic` must be set and must not contain the wildcards `+` or `#`; the same goes for
`reply_topic` when given. The connection state is shown on the GUI's **Device** page under
**Remote control**. The client reconnects on its own, waiting between 1 and 60 seconds between
attempts. A changed `[mqtt]` section takes effect when the GUI next starts.

Save writes the configuration file with mode 0600, because it may contain the broker password.

## Topics and messages

| direction | topic | QoS | payload |
|-----------|-------|-----|---------|
| client → GUI | `topic` (default `fractal_adjust`) | 1 | request: a command line, UTF-8 |
| GUI → client | `reply_topic` (default `fractal_adjust/reply`) | 1 | reply text, UTF-8 |

Replies are not retained. Every request produces exactly one reply. Requests run one at a time,
in the order they arrive, on the same thread the GUI uses for the hub, so replies come back in
request order.

Replies carry no correlation ID. If several clients send requests at the same time, they cannot
tell which reply belongs to whom. In that case give each client its own `reply_topic`, which
needs one GUI configuration per client, or have them take turns.

## Requests

A request is split into words like a shell command line (Python's `shlex`). Quoting with `'`
or `"` works, and `#` is an ordinary character, so colors need no quoting:

```
effect blink #ff0000 --timeout 30
```

The only option allowed before the command is `--json`, which switches the commands that print
tables to JSON output. Global options such as `--device`, `--serial`, `--config`,
`--hid-timeout`, `--no-apply` or `--minimized` are rejected.

### Accepted commands

| request | effect | reply |
|---------|--------|-------|
| `info` | firmware, serial, memory state, port types, ARGB2 compatibility | one `key: value` line each |
| `status` | fan duty and RPM per port | table (`port duty rpm state`) |
| `devices` | the ARGB sub-devices on the ports | one line per device |
| `effects` | stored (or currently shown temporary) effect of each sub-device | two lines per device |
| `curve [PORTS]` | fan curves; `PORTS` is e.g. `1,3` or `all` (default) | one line per port |
| `set-curve PORTS POINTS [--profile N]` | store a fan curve, e.g. `set-curve 2 0:20,50:40,100:100` | the curve read back, per port |
| `light on\|off [ID…]` | LEDs on or off; IDs in hex, default all | `LEDs on` / `LEDs off` |
| `slot N` | activate stored effect slot `N` (0–3) | `slot N active` |
| `effect MODE [COLOR…] [options]` | store an effect, or show it temporarily | see below |
| `apply [--force]` | apply the GUI's configuration file to the hub | one line per item |

The `effect` options are the CLI's:

- `--speed 0-100`, default 50.
- `--brightness 0-100`, default 100.
- `--id ID`, repeatable: only these sub-devices, in hex.
- `--timeout SECONDS`, or its alias `--preview SECONDS`.

The modes are `off`, `still`, `breathe`, `blink`, `shift`, `two-color-fade`, `lava-lamp` and
`wave-ripple`, plus the boot effects `meshify`, `fade-in` and `instant`. Colors are written as
`#rrggbb`. Modes that need more colors than are given fill up from a default palette.

`apply` reads the configuration file the GUI was started with and writes only what differs
from the hub's state.

**Not accepted:** `gui`, `config`, `list`, `raw` (raw protocol access, including firmware
commands) and `status --watch`. `--help` and `--version` are refused as well.

**No request ever writes the configuration file,** and requests do not change the settings shown
in the GUI or its unsaved-changes state. Stored effects, curves and the LED switch do change on
the hub itself.

### Permanent and temporary effects

Without `--timeout`, `effect` writes the effect into the hub's memory and replies
`stored <description>`. It is still shown after a power cycle.

With `--timeout SECONDS`, the effect is shown as a *preview*, which the hub does not store, and
the reply is `showing <description> for <SECONDS> s`. When the time is up, the previous effect
returns:

- Temporary effects stack. If a second one arrives while the first is still running, it is
  shown on top. When it expires, the first is shown again if it has not expired too.
  Otherwise the hub returns to its stored effect.
- Storing a permanent effect while temporary ones run changes what remains once they are all
  gone; the temporary effects stay on top until they expire.
- Closing the GUI ends all temporary effects immediately.
- Boot effects (`meshify`, `fade-in`, `instant`) cannot be shown temporarily.

While a temporary effect runs, `effects` reports it with the suffix `(temporary)`. The stored
effect is untouched and reads back again once the temporary one has ended.

## Replies

A reply is the text the command would print on the CLI, without the trailing newline. A
successful command without output replies `ok`.

Errors are replied as a single message starting with `error: `. A failed command may first
include the lines it printed before the error.

| situation | example reply |
|-----------|---------------|
| unknown or disallowed command | `error: command 'raw' is not accepted over MQTT; allowed: apply, curve, …` |
| syntax error | `error: argument --id: invalid _byte value: 'zz'` |
| invalid value | `error: brightness must be between 0 and 100` |
| hub not connected | `error: no Fractal Adjust Pro Hub found` |
| request not UTF-8 | `error: request is not UTF-8 text` |
| empty request | `error: empty request` |

If reading from or writing to the hub fails, for example because it was unplugged, the GUI closes
it and opens it again on the next request.

## Examples

With the mosquitto clients, listen for replies in one terminal:

```
mosquitto_sub -h localhost -t fractal_adjust/reply
```

and send requests from another:

```
mosquitto_pub -h localhost -t fractal_adjust -m 'status'
mosquitto_pub -h localhost -t fractal_adjust -m 'light off'
mosquitto_pub -h localhost -t fractal_adjust -m 'effect blink #ff0000 --timeout 30'
mosquitto_pub -h localhost -t fractal_adjust -m 'effect still #0074c5 --brightness 60'
mosquitto_pub -h localhost -t fractal_adjust -m 'set-curve 1,2 0:20,40:30,70:60,100:100'
mosquitto_pub -h localhost -t fractal_adjust -m '--json status'
```

Replies to `status` and `effect blink #ff0000 --timeout 4`:

```
port  duty    rpm  state
mobo   35%      -  0
1      35%    675  1
2      35%      0  0
3      35%      0  0
4      35%      0  0
```

```
showing blink, brightness 100, #ff0000 for 4 s
```

`--json status` gives a list of objects with the fields `port`, `duty`, `rpm` (`null` for the
motherboard input) and `state`.

## Security

Anyone who can publish to the request topic can control the hub. That includes writing effects
and fan curves into its memory, for example setting every fan curve to 0 %. MQTT has no
authentication of its own beyond the broker's, so restrict access on the broker:

- Require a username and password, or client certificates.
- Use ACLs so that only trusted clients may publish to `topic`.
- Enable `tls` when the broker is not on the local machine.

Raw protocol access and firmware commands are not available over MQTT.
