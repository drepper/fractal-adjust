# MQTT interface

While the `fractal-adjust` GUI is running, it can take requests over MQTT. A request is a
JSON object naming a command and its parameters. The GUI runs it against the hub and
publishes a JSON reply with the result. Only the GUI serves requests; the CLI commands never
connect to a broker.

## Setup

MQTT is configured in the `[mqtt]` section of the configuration file. The GUI's Save button
writes `$XDG_CONFIG_HOME/fractal-adjust/config.toml` (normally
`~/.config/fractal-adjust/config.toml`). Settings missing there fall back to the system-wide
files in `$XDG_CONFIG_DIRS` (normally `/etc/xdg/fractal-adjust/config.toml`) and then to the
packaged defaults.

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

## Topics

| direction | topic | QoS | payload |
|-----------|-------|-----|---------|
| client → GUI | `topic` (default `fractal_adjust`) | 1 | request: JSON object, UTF-8 |
| GUI → client | `reply_topic` (default `fractal_adjust/reply`) | 1 | reply: JSON object, UTF-8 |

Replies are not retained. Every request produces exactly one reply, including requests that
cannot be parsed. Requests run one at a time, in the order they arrive, on the same thread the
GUI uses for the hub.

## Requests

```json
{"id": 42, "command": "effect", "mode": "blink", "colors": ["#ff0000"], "timeout": 30}
```

| member | type | meaning |
|--------|------|---------|
| `command` | string | required; one of the commands below |
| `id` | string, number or null | optional; copied into the reply so it can be matched to its request |
| *parameters* | see each command | further members are the command's parameters |

Unknown commands and unknown parameters are rejected, so a misspelled parameter is reported
instead of silently ignored. A parameter given as `null` counts as absent.

**Value formats:**

- Colors are strings `"#rrggbb"`.
- Ports are integers 1–4.
- Sub-device IDs (see `devices`) are integers 0–255 or hex strings such as `"0x31"`. The high
  hex digit is the port, counted from 0.
- Brightness and speed are integers 0–100.
- A fan curve is a list of `[motherboard duty %, fan duty %]` integer pairs. It runs from x = 0
  to x = 100 with increasing x.

### Commands

| command | parameters | result |
|---------|------------|--------|
| `info` | – | object: `firmware`, `serial`, `memory_ok`, `windows_dynamic_lighting_opt_in`, `dynamic_lighting_on`, `port_types` (list), `argb2_compatible` (list) |
| `status` | – | list of `{"port", "duty", "rpm", "state"}`; the first entry is the motherboard input (`"port": "mobo"`, `"rpm": null`), then ports `"1"`–`"4"` |
| `devices` | – | list of `{"id", "id_hex", "port", "led_count", "argb_version", "type_name", "name"}` |
| `effects` | – | list of `{"id", "id_hex", "effect", "temporary", "boot"}` for each device with LEDs; `effect` and `boot` are effect objects (see below) or null |
| `effect` | `mode` (required), `colors`, `speed`, `brightness`, `ids`, `timeout`; for `wave-ripple` also `up_wave`, `top_wave`, `down_wave`, `frequency` | `{"effect", "boot", "stored", "timeout", "ids"}` |
| `curve` | `ports` (default: all) | list of `{"port", "profile", "points"}` |
| `set-curve` | `ports` (required), `points` (required), `profile` | the stored curves, read back, as for `curve` |
| `light` | `on` (required, boolean), `ids` (default: all devices) | `{"on", "ids"}` |
| `slot` | `slot` (required, 0–3) | `{"slot"}` |
| `apply` | `force` (boolean, default false) | `{"log": [...]}`: one line per item written or found unchanged |

**Effect modes** (`mode`): `off`, `still`, `breathe`, `blink`, `shift`, `two-color-fade`,
`lava-lamp` and `wave-ripple`, plus the boot effects `meshify`, `fade-in` and `instant`.

- **Colors:** still, blink and the boot effects use one color. Breathe, two-color-fade and
  wave-ripple use two, shift and lava-lamp six. Missing colors are filled from a default
  palette.
- **Defaults:** speed defaults to 50 and brightness to 100.
- **Effect objects** in results have `mode` and `brightness`. They add `speed` only for modes
  with a speed control, `colors` for modes that use colors, and the wave parameters for
  `wave-ripple`.

`apply` reads the configuration file the GUI was started with and writes only what differs
from the hub's state.

Not available over MQTT: raw protocol access, including firmware commands, and anything
that shows or changes the configuration file. **No request ever writes the configuration
file,** and requests do not change the settings shown in the GUI or its unsaved-changes state.
Stored effects, curves and the LED switch do change on the hub itself.

### Permanent and temporary effects

Without `timeout`, `effect` writes the effect into the hub's memory, and the result has
`"stored": true`. The effect is still shown after a power cycle.

With `"timeout": SECONDS`, a positive number, the effect is shown as a *preview*, which the hub
does not store. The result has `"stored": false`, and when the time is up the previous effect
returns:

- Temporary effects stack. If a second one arrives while the first is still running, it is
  shown on top. When it expires, the first is shown again if it has not expired too.
  Otherwise the hub returns to its stored effect.
- Storing a permanent effect while temporary ones run changes what remains once they are all
  gone; the temporary effects stay on top until they expire.
- Closing the GUI ends all temporary effects immediately.
- Boot effects cannot be shown temporarily.

While a temporary effect runs, `effects` reports it with `"temporary": true`. The stored effect
is untouched and reads back again once the temporary one has ended.

## Replies

Success:

```json
{"id": 42, "command": "effect", "ok": true, "result": {...}}
```

Failure:

```json
{"id": 42, "command": "effect", "ok": false, "error": "brightness must be between 0 and 100"}
```

- **`id`:** the request's `id`, or `null` if it had none or could not be parsed.
- **`command`:** present when the request named a known command.
- **`result`:** present exactly when `ok` is true.
- **`error`:** present exactly when `ok` is false.

Typical errors:

| situation | `error` |
|-----------|---------|
| payload not JSON | `request is not valid JSON: …` |
| payload not UTF-8 | `request is not UTF-8 text` |
| not an object, or no `command` | `request must be a JSON object` / `missing "command" string` |
| unknown command | `unknown command 'raw'; allowed: apply, curve, devices, …` |
| misspelled or extra member | `unknown parameters: colour` |
| missing or invalid value | `missing parameter 'mode'`, `slot must be between 0 and 3`, … |
| hub not available | `no Fractal Adjust Pro Hub found` |

If reading from or writing to the hub fails, for example because it was unplugged, the GUI closes
it and opens it again on the next request.

## Examples

With the mosquitto clients, listen for replies in one terminal:

```
mosquitto_sub -h localhost -t fractal_adjust/reply
```

and send requests from another:

```
mosquitto_pub -h localhost -t fractal_adjust -m '{"id": 1, "command": "status"}'
mosquitto_pub -h localhost -t fractal_adjust -m '{"command": "light", "on": false}'
mosquitto_pub -h localhost -t fractal_adjust -m '{"command": "effect", "mode": "blink", "colors": ["#ff0000"], "timeout": 30}'
mosquitto_pub -h localhost -t fractal_adjust -m '{"command": "effect", "mode": "still", "colors": ["#0074c5"], "brightness": 60}'
mosquitto_pub -h localhost -t fractal_adjust -m '{"command": "set-curve", "ports": [1, 2], "points": [[0, 20], [40, 30], [70, 60], [100, 100]]}'
```

Replies:

```json
{"id": 1, "command": "status", "ok": true, "result": [{"port": "mobo", "duty": 36, "rpm": null, "state": 0}, {"port": "1", "duty": 36, "rpm": 650, "state": 1}, {"port": "2", "duty": 36, "rpm": 0, "state": 0}, {"port": "3", "duty": 36, "rpm": 0, "state": 0}, {"port": "4", "duty": 36, "rpm": 0, "state": 0}]}
{"id": null, "command": "light", "ok": true, "result": {"on": false, "ids": [1, 2, 3, 17, 33, 49, 50, 51]}}
{"id": null, "command": "effect", "ok": true, "result": {"effect": {"mode": "blink", "brightness": 100, "colors": ["#ff0000"]}, "boot": false, "stored": false, "timeout": 30.0, "ids": [1, 2, 3, 49, 50, 51]}}
```

The `ids` of `effect` lists only devices with LEDs. Legacy ARGB1 devices reject effects, but
the LED switch still covers them.

## Security

Anyone who can publish to the request topic can control the hub. That includes writing effects
and fan curves into its memory, for example setting every fan curve to 0 %. MQTT has no
authentication of its own beyond the broker's, so restrict access on the broker:

- Require a username and password, or client certificates.
- Use ACLs so that only trusted clients may publish to `topic`.
- Enable `tls` when the broker is not on the local machine.

Raw protocol access and firmware commands are not available over MQTT.
