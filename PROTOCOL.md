# Fractal Adjust Pro Hub HID protocol

Source: Fractal's WebHID application (`https://adjust.fractal-design.com`, bundle
`main.e2372c083cc6b94f.js`, classes `P911Base`/`Hub`). The firmware calls this protocol "P911".
The hub also reports product ID `0x1002`, which the web app treats as the same device. Every
command below that `fractal_adjust.py` implements was checked against a hub (36bc:1001, firmware 1.1.5).

## Transport

- The hub has one vendor HID interface (usage page `0xFF00`, usage 1), report ID **2**, with
  63-byte input and output reports.
- Request: `02 <type> <cmd> <args…>`, zero-padded to 63 payload bytes.
- Reply: `02 <type> <cmd> <status> <data…>`. A status of `0` means success. The app accepts PWM
  replies whatever their status.
- Commands are strictly request/reply. Streaming (`A5`) and reboot get no reply.

## Command groups (`type` byte)

| type | group |
|------|-------|
| `A4` | lighting effects / sub-devices |
| `A5` | real-time RGB streaming |
| `B5` | firmware upgrade |
| `D1` | fans (PWM) |
| `F1` | factory / device info |

Offsets below count from the `type` byte, so the reply status is at offset 2.

### F1 – device info
| cmd | request args | reply |
|-----|--------------|-------|
| `01` | – | firmware version, ASCII NUL-terminated at offset 3 (16 bytes) |
| `02` | – | serial, ASCII at offset 6 (17 bytes) |
| `14` | – | Windows Dynamic Lighting opt-in: `[2]==1 or [3]==1` |
| `1C` | – | memory OK: `[2]==1 or [3]!=1` |

### D1 – fans
Ports are 0-based on the wire (0–3).

| cmd | request args | reply |
|-----|--------------|-------|
| `01` | – | state: `[3]` motherboard PWM input (0 = connected), `[4..7]` ports |
| `02` | port | curve: `[3]` profile ID, `[4]` n points, `[5..]` n × (x, y) |
| `03` | port, profile, n, n × (x, y) | store curve; x = motherboard duty %, y = fan duty % |
| `04` | – | duty %: `[3]` motherboard, `[4..7]` ports |
| `05` | – | tacho RPM, big-endian u16 at `[3]`, `[5]`, `[7]`, `[9]` |

On the test hub, port state was `1` on the only port with a spinning fan and `0` on the
others. The app models only `0 = FAN_OK, 1 = FAN_ERROR`, so the per-port meaning is uncertain.
The web app's own curves always run from x=0 to x=100 in 20 % steps.

### A4 – lighting
| cmd | request args | reply / meaning |
|-----|--------------|-----------------|
| `01`–`03` | | frame-based effect upload (headset; the hub uses `11`–`13`) |
| `04` | slot (0 = boot effect, ≤ 3) | select active effect slot |
| `05` | slot type, offset hi, offset lo | read the *active* effect (a running preview included, preset `0x99`); metadata at reply `[10..51]`; status 1 if none |
| `08` | 0 | sub-devices: `[3]` count, then count × (id, LED count, ARGB ver; 0 = ARGB2) |
| `09` | n | set LED count of the selected sub-device |
| `0A` | 1, id | select sub-device for the following commands |
| `0B` | 0/1 | restart animation (sent after storing a synchronized effect) |
| `0C` | on, n, n × id | switch LEDs of the listed sub-devices |
| `0D`/`0E` | id, len, text / 0 | set / get sub-device name (reply `[3]` len, `[4..]` text) |
| `0F`/`10` | id, len, text / 0 | set / get sub-device type name ("Fan", "Legacy device", …) |
| `11` | see below | timestamp effect header |
| `12` | len, data | timestamp effect data chunk (≤ 60 bytes) |
| `13` | – | commit timestamp effect |
| `14`/`15` | – / 4 × type | get / set port ARGB type (1 = ARGB1, 2 = ARGB2, 5 = ARGB1 fallback) |
| `16`/`17` | – / id, rot | get / set rotation of the selected sub-device (eighths: 0, 2, 4, 6) |
| `18`/`19` | – / id, 0/1 | get / set mirroring |
| `1A` | – | Windows Dynamic Lighting active: `[3]==1` |
| `1B` | – | per-port ARGB2 compatibility `[3..6]`; `[2]==1` means all compatible |
| `78` | – | stop loading (sent once after storing effects) |

The sub-device ID's high nibble is the 0-based port; the low nibble is its position in the chain.

#### Effects (timestamp-based)

Each ARGB sub-device stores two effects, addressed by *slot type*: 1 = regular and 0 = boot.
To store one, the app does this for every sub-device in the batch:

1. `A4 0A 01 <id>` selects the sub-device. `A4 16` and `A4 18` read its rotation and mirroring.
2. `A4 11 <slot type> <checksum> <len hi> <len lo> <preview> <count> <metadata…>`
   - `checksum` is the 8-bit sum of the keyframe data.
   - `count` is the number of sub-devices in the batch.
   - `preview` = 1 shows the effect without storing it. The preset byte of the metadata is then `0x99`.
   - With `count` = 0, or for the `instant` boot effect, nothing more is sent.
3. The keyframe data in `A4 12` chunks of up to 60 bytes.
4. `A4 13` commits the effect, except in a preview.

After the batch the app sends `A4 78`, and then `A4 0B 01` for effects that must run in sync
(breathe, two-color fade). Re-sending a preview header with `count` 0 and a black "still"
effect cancels a preview, and the stored effect shows again.

Legacy ARGB1 sub-devices report 0 LEDs and answer `A4 11` with status 1. The app's flow can't
handle that, and `fractal-adjust` leaves them out of effect batches.

**Keyframes:** each keyframe is `<time hi> <time lo>` followed by one RGB triplet per LED. The
firmware interpolates between keyframes. The tick unit is unknown; the app's still effect uses
the times 0, 562 and 1124. Colors are already scaled by brightness (`c × b / 100`).

**Metadata** (stored with the effect and returned by `A4 05`):
`<preset> <mode> <speed> <colors…> [extra] <b>`
- `b` is half the app's 0–100 brightness slider, so 100 % is stored as 50.
- The colors are the unscaled `#rrggbb` values.
- Speed is 100 for modes without a speed control.

| mode | name | colors | extra |
|------|------|--------|-------|
| 0 | shift | 6 | – |
| 1 | still | 1 | – |
| 2 | breathe | 2 | – |
| 3 | meshify (boot) | 1 | – |
| 4 | fade-in (boot) | 1 | – |
| 5 | instant (boot) | 1 | – |
| 6 | lava lamp | 6 | – |
| 7 | wave ripple | 2 | after the colors: up, top, down (LEDs), 100 − frequency; after `b`: scaled second color |
| 8 | two-color fade | 2 | speed, repeated before `b` |
| 9 | radius (pump only) | 1 | – |
| 10 | off | 1 (black) | – |
| 11 | torrent (case only) | 6 | – |

The app's preset IDs are the default presets, e.g. 0 = shift "Northern lights", 6 = still
"Fractal blue", 8 = meshify "White" and 31 = off. As shipped, the hub stores
`00 00 28 <Northern lights colors> 32` as the regular effect and `08 03 64 ff ff ff 32` as the boot effect.

`fractal_adjust/effects.py` reproduces the keyframe generators of each mode, including their
randomness for shift and lava lamp.

While a preview runs, `A4 05` returns the preview's metadata. Previews are never written to flash: once a preview ends, the stored effect reads back unchanged.

**Blink** is not a firmware mode; `fractal-adjust` adds it. It is stored as a breathe effect
(mode 2) with black as the second color, under the private preset `0xB1` so that it decodes
back as blink. The keyframes are color at 0 and 32, black at 33 and 65, and color at 66, i.e.
⅓ s phases. The 1-tick steps make the transitions hard, as breathe does. The speed byte is 92;
if the firmware rendered breathe from the metadata, that speed would give about the same cycle.
This assumes a 10 ms tick, which was inferred from the app's effect timings and not measured.

### A5 – streaming
`F0` initialize, `01` RGB data, `F1` shutdown. Each takes the args `flag, brightness(FF)`, and
`01` adds raw RGB triplets after them. These commands get no reply.
