"""Lighting effect design: turn effect settings into the hub's keyframe format.

The hub stores one effect per ARGB sub-device and slot (regular or boot).  An effect is
uploaded as a metadata block, which the hub keeps so the settings can be read back, and a
list of keyframes, each a 16-bit big-endian timestamp followed by one RGB triplet per LED.
The firmware interpolates between keyframes.  The generators below reproduce those of
Fractal's web application, including its rounding, so the result looks the same.
"""
from __future__ import annotations

import dataclasses
import math
import random
from collections.abc import Sequence

type Rgb = tuple[int, int, int]

BLACK: Rgb = (0, 0, 0)
MAX_BRIGHTNESS = 100
PREVIEW_PRESET = 153
#: Preset byte marking a blink effect, which is stored as a breathe effect (see BLINK_TICKS).
BLINK_PRESET = 0xB1
#: Keyframe time units per second.  Inferred from the durations of the web app's effects
#: (10 ms per tick); the hub does not report it.
TICKS_PER_SECOND = 100
#: Blink phase length: 1/3 s on, 1/3 s off.
BLINK_TICKS = round(TICKS_PER_SECOND / 3)
#: Speed byte stored with a blink.  Should the firmware ever render the breathe mode itself
#: instead of playing the keyframes, this speed gives about the same cycle: 8 * (100 - s) + 2
#: ticks equals 2 * BLINK_TICKS.
BLINK_FALLBACK_SPEED = 100 - (2 * BLINK_TICKS - 2) // 8
SHIFT_PERMUTATION = (4, 5, 3, 0, 1, 2)


@dataclasses.dataclass(frozen=True, slots=True)
class Mode:
    """Static properties of one effect type."""

    name: str
    id: int
    preset: int
    colors: int
    has_speed: bool
    boot: bool
    sync: bool = False
    label: str = ""


MODES: dict[str, Mode] = {m.name: m for m in (
    Mode("off", 10, 31, 0, False, False, label="Off"),
    Mode("still", 1, 6, 1, False, False, label="Still"),
    Mode("breathe", 2, 7, 2, True, False, sync=True, label="Breathe"),
    Mode("blink", 2, BLINK_PRESET, 1, False, False, sync=True, label="Blink"),
    Mode("shift", 0, 0, 6, True, False, label="Shift"),
    Mode("two-color-fade", 8, 14, 2, True, False, sync=True, label="Two-color fade"),
    Mode("lava-lamp", 6, 11, 6, True, False, label="Lava lamp"),
    Mode("wave-ripple", 7, 13, 2, True, False, label="Wave ripple"),
    Mode("meshify", 3, 8, 1, False, True, label="Meshify"),
    Mode("fade-in", 4, 9, 1, False, True, label="Fade in"),
    Mode("instant", 5, 10, 1, False, True, label="Instant"),
)}
MODES_BY_ID: dict[int, Mode] = {m.id: m for m in MODES.values() if m.name != "blink"}
REGULAR_MODES = tuple(m.name for m in MODES.values() if not m.boot)
BOOT_MODES = tuple(m.name for m in MODES.values() if m.boot)

#: Colors used to fill up a color set that is shorter than the mode needs ("Northern lights").
DEFAULT_PALETTE: tuple[Rgb, ...] = ((0x00, 0x74, 0xC5), (0x6E, 0x00, 0xFA), (0x00, 0xFA, 0xEB),
                                    (0xC9, 0x40, 0xBD), (0x95, 0xFD, 0xE4), (0x00, 0xF0, 0x3C))


def parse_color(text: str) -> Rgb:
    """Parse "#rrggbb" (the # is optional) into an RGB triple."""
    value = text.removeprefix("#")
    if len(value) != 6:
        raise ValueError(f"invalid color {text!r}; expected #rrggbb")
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError as e:
        raise ValueError(f"invalid color {text!r}; expected #rrggbb") from e


def format_color(rgb: Rgb) -> str:
    """Format an RGB triple as "#rrggbb"."""
    return "#{:02x}{:02x}{:02x}".format(*rgb)


@dataclasses.dataclass(slots=True)
class EffectSettings:
    """User-facing description of an effect; brightness uses the web app's 0-100 scale."""

    mode: str
    speed: int = 50
    brightness: int = 100
    colors: list[Rgb] = dataclasses.field(default_factory=list)
    up_wave: int = 10
    top_wave: int = 10
    down_wave: int = 4
    frequency: int = 50

    @property
    def info(self) -> Mode:
        """The Mode record of this effect."""
        return MODES[self.mode]

    def validate(self) -> None:
        """Raise ValueError if a field is out of range."""
        if self.mode not in MODES:
            raise ValueError(f"unknown effect mode {self.mode!r}; known: {', '.join(MODES)}")
        for name in ("speed", "brightness", "frequency"):
            if not 0 <= getattr(self, name) <= 100:
                raise ValueError(f"{name} must be between 0 and 100")
        for name in ("up_wave", "top_wave", "down_wave"):
            if not 0 <= getattr(self, name) <= 50:
                raise ValueError(f"{name} must be between 0 and 50")
        if any(not all(0 <= c <= 255 for c in rgb) for rgb in self.colors):
            raise ValueError("color components must be between 0 and 255")

    def effective_colors(self) -> list[Rgb]:
        """Return exactly as many colors as the mode uses, filling up from the palette."""
        count = self.info.colors
        if count == 0:
            return [BLACK]
        colors = list(self.colors[:count])
        colors.extend(DEFAULT_PALETTE[len(colors):count])
        return colors

    def device_brightness(self) -> int:
        """Brightness byte as stored on the hub; the web app halves its slider value."""
        return self.brightness // 2

    def effective_speed(self) -> int:
        """Speed byte; modes without a speed control always use 100."""
        return self.speed if self.info.has_speed else 100


def _jround(x: float) -> int:
    """JavaScript's Math.round: halves round towards +infinity."""
    return math.floor(x + 0.5)


def _clamp(x: float) -> int:
    """Clamp and round a color component the way tinycolor does."""
    return min(255, max(0, _jround(x)))


def _mix(a: Rgb, b: Rgb, amount: float) -> Rgb:
    """tinycolor.mix: blend from @p a to @p b by @p amount percent."""
    p = amount / 100
    return (_clamp((b[0] - a[0]) * p + a[0]), _clamp((b[1] - a[1]) * p + a[1]),
            _clamp((b[2] - a[2]) * p + a[2]))


def _scale(rgb: Rgb, brightness: float) -> Rgb:
    """changeColorViaBrightness: scale a color by @p brightness percent."""
    return (_jround(rgb[0] * brightness / MAX_BRIGHTNESS), _jround(rgb[1] * brightness / MAX_BRIGHTNESS),
            _jround(rgb[2] * brightness / MAX_BRIGHTNESS))


def _u8(x: float) -> int:
    """Store into a Uint8Array: truncate and wrap."""
    return int(x) & 0xFF


class _Builder:
    """Accumulates keyframes."""

    def __init__(self, led_count: int, rotation: int, mirror: bool) -> None:
        self.led_count = led_count
        self.rotation = rotation
        self.mirror = mirror
        self.data = bytearray()

    def uniform(self, rgb: Sequence[float]) -> bytes:
        """A frame with every LED set to @p rgb."""
        return bytes(_u8(c) for c in rgb) * self.led_count

    def transform(self, frame: bytes) -> bytes:
        """transformFrame: apply the sub-device's rotation (in eighths) and mirroring."""
        n = len(frame) // 3
        if n == 0:
            return frame
        out = bytearray(len(frame))
        offset = _jround(n * self.rotation / 8)
        for r in range(n):
            o = (r + offset) % n
            if self.mirror:
                o = (n - o) % n
            out[3 * o:3 * o + 3] = frame[3 * r:3 * r + 3]
        return bytes(out)

    def add(self, timestamp: int, frame: bytes) -> None:
        """Append one keyframe."""
        self.data += (timestamp & 0xFFFF).to_bytes(2) + frame


def _gradient(colors: Sequence[Rgb], led_count: int) -> bytes:
    """Shift's generateLedColors: spread @p colors over the LEDs with blending."""
    out = bytearray()
    step = len(colors) / led_count
    r = 0
    for i in range(led_count):
        frac = i * step - r
        if frac > 1:
            r += 1
            frac = i * step - r
        nxt = colors[r + 1] if r + 1 < len(colors) else colors[0]
        out += bytes(_mix(colors[r], nxt, 100 * frac))
    return bytes(out)


def _shift(b: _Builder, colors: list[Rgb], speed: int, rng: random.Random) -> None:
    start = math.floor(rng.random() * len(colors))
    current = colors[len(colors) - start:] + colors[:len(colors) - start]
    frames: list[bytes] = []
    for _ in SHIFT_PERMUTATION:
        frames.append(_gradient(current, b.led_count))
        current = [current[k] for k in SHIFT_PERMUTATION]
    shift = (math.floor(rng.random() * len(SHIFT_PERMUTATION)) * 3 * b.led_count) % len(frames)
    shifted = list(frames)
    for o, frame in enumerate(frames):
        shifted[(o + shift) % len(frames)] = frame
    step = 120 - speed
    d = math.floor(step * rng.random())
    mid = bytes(_u8(x + (y - x) * d / step) for x, y in zip(shifted[0], shifted[1]))
    b.add(0, b.transform(mid))
    b.add(step - d, b.transform(shifted[1]))
    for o in range(2, len(frames)):
        b.add(o * step, b.transform(shifted[o]))
    total = len(frames) * step
    b.add(total, b.transform(shifted[0]))
    b.add(total + d, b.transform(mid))


def _breathe(b: _Builder, main: Rgb, second: Rgb, speed: int) -> None:
    n = 100 - speed if 100 - speed > 0 else 1
    hold, fade = _jround(n / 50 * 120), _jround(n / 50 * 40)
    dim_main = tuple(math.ceil(c / 20) for c in main)
    dim_second = tuple(math.ceil(c / 20) for c in second)
    t = 0
    for delta, rgb in ((0, main), (hold, main), (fade, dim_main), (1, dim_second), (fade, second),
                       (hold, second), (fade, dim_second), (1, dim_main), (fade, main)):
        t += delta
        b.add(t, b.uniform(rgb))


def _two_color_fade(b: _Builder, main: Rgb, second: Rgb) -> None:
    n = b.led_count
    frame = bytearray()
    for i in range(n):
        amount = 0.0 if n == 1 else min(100.0, max(0.0, 125 * math.sin(i / (n - 1) * 2 * math.pi)))
        frame += bytes(_mix(main, second, amount))
    rotated = b.transform(bytes(frame))
    by = 62 if n == 76 else _jround(3 * n / 8)
    if n:
        by %= n
        rotated = rotated[3 * (n - by):] + rotated[:3 * (n - by)]
    b.add(0, rotated)
    b.add(20000, rotated)


def _lava_lamp(b: _Builder, colors: list[Rgb], speed: int, rng: random.Random) -> None:
    n = b.led_count
    frame = bytearray(3 * n)

    def pick() -> tuple[int, bool]:
        k = math.floor(12 * rng.random())
        return (k, False) if k < 6 else (k - 6, True)

    def fill() -> None:
        for i in range(0, 3 * n, 3):
            k, dim = pick()
            frame[i:i + 3] = bytes(_u8(c / 2) if dim else c for c in colors[k])
        if n > 15:
            for _ in range(_jround(n / 4)):
                k, dim = pick()
                pos = math.floor(rng.random() * (n - 3)) * 3
                div = (4, 2, 4) if dim else (2, 1, 2)
                for j, d in enumerate(div):
                    frame[pos + 3 * j:pos + 3 * j + 3] = bytes(_jround(c / d) & 0xFF for c in colors[k])

    step = 6 + _jround(100 - speed)
    t = 0
    fill()
    first = bytes(frame)
    b.add(t, first)
    t += step + math.floor(10 * rng.random()) - 5
    for _ in range(1, 14):
        fill()
        b.add(t, bytes(frame))
        t += step + math.floor(10 * rng.random()) - 5
    b.add(t, first)


def metadata(settings: EffectSettings) -> bytes:
    """The block the hub stores with the effect: preset, mode, speed, colors, brightness, …."""
    info = settings.info
    colors = settings.effective_colors()
    raw = [c for rgb in colors for c in rgb]
    speed = settings.effective_speed()
    bright = settings.device_brightness()
    match info.name:
        case "two-color-fade":
            body = [info.id, speed, *raw, speed, bright]
        case "blink":
            body = [info.id, BLINK_FALLBACK_SPEED, *raw, *BLACK, bright]
        case "wave-ripple":
            second = _scale(colors[1], bright)
            body = [info.id, speed, *raw, settings.up_wave, settings.top_wave, settings.down_wave,
                    100 - settings.frequency, bright, *second]
        case _:
            body = [info.id, speed, *raw, bright]
    return bytes((info.preset, *body))


def keyframes(settings: EffectSettings, led_count: int, rotation: int = 0, mirror: bool = False,
              rng: random.Random | None = None) -> bytes:
    """Render the keyframe list for a sub-device with @p led_count LEDs."""
    rng = rng if rng is not None else random.Random()
    bright = settings.device_brightness()
    colors = [_scale(c, bright) for c in settings.effective_colors()]
    b = _Builder(led_count, rotation, mirror)
    speed = settings.effective_speed()
    match settings.mode:
        case "off" | "still" | "wave-ripple":
            for t in (0, 562, 1124):
                b.add(t, b.uniform(colors[0]))
        case "breathe":
            _breathe(b, colors[0], colors[1], speed)
        case "blink":
            for t, rgb in ((0, colors[0]), (BLINK_TICKS - 1, colors[0]), (BLINK_TICKS, BLACK),
                           (2 * BLINK_TICKS - 1, BLACK), (2 * BLINK_TICKS, colors[0])):
                b.add(t, b.uniform(rgb))
        case "shift":
            _shift(b, colors, speed, rng)
        case "two-color-fade":
            _two_color_fade(b, colors[0], colors[1])
        case "lava-lamp":
            _lava_lamp(b, colors, speed, rng)
        case "meshify":
            for t, rgb in ((0, colors[0]), (138, colors[0]), (178, BLACK), (218, BLACK), (258, colors[0]),
                           (318, colors[0])):
                b.add(t, b.uniform(rgb))
        case "fade-in":
            third: tuple[float, ...] = tuple(c / 3 for c in colors[0])
            fade: tuple[tuple[int, Sequence[float]], ...] = ((0, BLACK), (80, third), (130, colors[0]),
                                                             (150, colors[0]))
            for t, level in fade:
                b.add(t, b.uniform(level))
        case "instant":
            pass
        case _:
            raise ValueError(f"unknown effect mode {settings.mode!r}")
    return bytes(b.data)


def settings_from_metadata(meta: bytes) -> EffectSettings | None:
    """Decode the stored metadata block (starting with the preset byte) back into settings."""
    if len(meta) < 6 or (info := MODES_BY_ID.get(meta[1])) is None:
        return None
    blink_preview = (meta[0] == PREVIEW_PRESET and meta[2] == BLINK_FALLBACK_SPEED
                     and meta[6:9] == bytes(BLACK))
    if (meta[0] == BLINK_PRESET or blink_preview) and info.name == "breathe":
        return EffectSettings("blink", colors=[(meta[3], meta[4], meta[5])], brightness=min(100, 2 * meta[9]))
    ncolors = max(info.colors, 1)
    colors = [(meta[3 + 3 * i], meta[4 + 3 * i], meta[5 + 3 * i]) for i in range(ncolors)]
    pos = 3 + 3 * ncolors
    settings = EffectSettings(info.name, speed=meta[2], colors=colors if info.colors else [])
    if info.name == "two-color-fade":
        pos += 1
    elif info.name == "wave-ripple":
        settings.up_wave, settings.top_wave, settings.down_wave = meta[pos], meta[pos + 1], meta[pos + 2]
        settings.frequency = 100 - meta[pos + 3]
        pos += 4
    settings.brightness = min(100, 2 * meta[pos])
    if not info.has_speed:
        settings.speed = 50
    return settings
