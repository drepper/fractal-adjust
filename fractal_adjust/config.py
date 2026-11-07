"""Configuration file handling: load, validate, save and apply settings."""
from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import tomllib
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from .effects import BOOT_MODES, REGULAR_MODES, EffectSettings, format_color, metadata, parse_color
from .hub import PORT_COUNT, Hub, SlotType, SubDevice, validate_curve

SYSTEM_CONFIG = Path("/etc/fractal-adjust/config.toml")

type Curve = list[tuple[int, int]]


class ConfigError(Exception):
    """The configuration file is malformed or contains invalid values."""


@dataclasses.dataclass(slots=True)
class MqttSettings:
    """Connection to an MQTT broker that delivers command requests."""

    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    topic: str = "fractal_adjust"
    reply_topic: str = ""
    username: str = ""
    password: str = ""
    tls: bool = False
    client_id: str = ""
    keepalive: int = 60

    @property
    def effective_reply_topic(self) -> str:
        """Where replies go; defaults to "<topic>/reply"."""
        return self.reply_topic or f"{self.topic}/reply"


@dataclasses.dataclass(slots=True)
class Config:
    """All settings the configuration file can hold."""

    light_on: bool
    effect: EffectSettings
    boot: EffectSettings
    fan_curves: dict[int, Curve]
    mqtt: MqttSettings = dataclasses.field(default_factory=MqttSettings)


def user_config_path() -> Path:
    """The per-user configuration file that Save writes."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "fractal-adjust" / "config.toml"


def default_text() -> str:
    """The packaged default configuration file."""
    return resources.files(__package__).joinpath("data/config.toml").read_text(encoding="utf-8")


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge TOML tables; values in @p over win."""
    result = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _get[T](table: dict[str, Any], key: str, kind: type[T], where: str) -> T:
    """Fetch @p key from @p table and check its type."""
    if key not in table:
        raise ConfigError(f"{where}: missing key {key!r}")
    value = table[key]
    if kind is int and isinstance(value, bool) or not isinstance(value, kind):
        raise ConfigError(f"{where}.{key}: expected {kind.__name__}, got {type(value).__name__}")
    return value


def _effect(table: Any, where: str, modes: tuple[str, ...]) -> EffectSettings:
    """Parse one effect table."""
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: expected a table")
    mode = _get(table, "mode", str, where)
    if mode not in modes:
        raise ConfigError(f"{where}.mode: {mode!r} is not one of {', '.join(modes)}")
    colors = table.get("colors", [])
    if not isinstance(colors, list) or not all(isinstance(c, str) for c in colors):
        raise ConfigError(f"{where}.colors: expected a list of \"#rrggbb\" strings")
    try:
        settings = EffectSettings(mode, colors=[parse_color(c) for c in colors])
    except ValueError as e:
        raise ConfigError(f"{where}.colors: {e}") from e
    for key in ("speed", "brightness", "up_wave", "top_wave", "down_wave", "frequency"):
        if key in table:
            setattr(settings, key, _get(table, key, int, where))
    try:
        settings.validate()
    except ValueError as e:
        raise ConfigError(f"{where}: {e}") from e
    return settings


def _curve(value: Any, where: str) -> Curve:
    """Parse one fan curve."""
    if not isinstance(value, list) or not all(
            isinstance(p, list) and len(p) == 2 and all(type(v) is int for v in p) for p in value):
        raise ConfigError(f"{where}: expected a list of [x, y] integer pairs")
    points = [(p[0], p[1]) for p in value]
    try:
        validate_curve(points)
    except ValueError as e:
        raise ConfigError(f"{where}: {e}") from e
    return points


def _mqtt(table: Any) -> MqttSettings:
    """Parse the [mqtt] table."""
    if not isinstance(table, dict):
        raise ConfigError("[mqtt] must be a table")
    settings = MqttSettings()
    for field in dataclasses.fields(MqttSettings):
        if field.name in table:
            kind = type(getattr(settings, field.name))
            setattr(settings, field.name, _get(table, field.name, kind, "mqtt"))
    unknown = set(table) - {f.name for f in dataclasses.fields(MqttSettings)}
    if unknown:
        raise ConfigError(f"mqtt: unknown keys {', '.join(sorted(unknown))}")
    if not 1 <= settings.port <= 65535:
        raise ConfigError("mqtt.port must be between 1 and 65535")
    if settings.keepalive < 5:
        raise ConfigError("mqtt.keepalive must be at least 5 seconds")
    for name in ("topic", "reply_topic"):
        value = getattr(settings, name)
        if any(c in value for c in "+#") or (name == "topic" and not value):
            raise ConfigError(f"mqtt.{name} must be a non-wildcard topic")
    if not settings.host:
        raise ConfigError("mqtt.host must not be empty")
    return settings


def parse(data: dict[str, Any]) -> Config:
    """Turn a merged TOML document into a Config."""
    lighting = data.get("lighting", {})
    fans = data.get("fans", {})
    if not isinstance(lighting, dict) or not isinstance(fans, dict):
        raise ConfigError("[lighting] and [fans] must be tables")
    curves: dict[int, Curve] = {}
    for key, value in fans.items():
        if not (key.startswith("port") and key[4:].isdigit() and 1 <= int(key[4:]) <= PORT_COUNT):
            raise ConfigError(f"fans.{key}: expected port1 … port{PORT_COUNT}")
        curves[int(key[4:])] = _curve(value, f"fans.{key}")
    return Config(light_on=_get(lighting, "on", bool, "lighting"),
                  effect=_effect(lighting.get("effect"), "lighting.effect", REGULAR_MODES),
                  boot=_effect(lighting.get("boot"), "lighting.boot", BOOT_MODES),
                  fan_curves=dict(sorted(curves.items())),
                  mqtt=_mqtt(data.get("mqtt", {})))


def _read(path: Path) -> dict[str, Any]:
    """Read and parse one TOML file."""
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from e


def load(path: Path | None = None) -> Config:
    """Load the defaults overlaid with @p path, or with the system and user files."""
    data = tomllib.loads(default_text())
    sources = [path] if path is not None else [SYSTEM_CONFIG, user_config_path()]
    for source in sources:
        if path is not None or source.exists():
            data = _merge(data, _read(source))
    try:
        return parse(data)
    except ConfigError as e:
        origin = path if path is not None else "configuration"
        raise ConfigError(f"{origin}: {e}") from e


def defaults() -> Config:
    """The packaged default configuration."""
    return parse(tomllib.loads(default_text()))


def _toml_effect(e: EffectSettings) -> list[str]:
    """Serialize an effect table body."""
    lines = [f'mode = "{e.mode}"']
    if e.info.has_speed:
        lines.append(f"speed = {e.speed}")
    lines.append(f"brightness = {e.brightness}")
    if e.info.colors:
        lines.append("colors = [" + ", ".join(f'"{format_color(c)}"' for c in e.effective_colors()) + "]")
    if e.mode == "wave-ripple":
        lines += [f"up_wave = {e.up_wave}", f"top_wave = {e.top_wave}", f"down_wave = {e.down_wave}",
                  f"frequency = {e.frequency}"]
    return lines


def _toml_scalar(value: bool | int | str) -> str:
    """Format a TOML bool, integer or basic string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def dump(cfg: Config) -> str:
    """Render @p cfg as a commented TOML document."""
    lines = [
        "# Fractal Adjust Pro Hub settings, written by fractal-adjust.  They are applied when the",
        "# program starts; keys missing here fall back to the packaged defaults.",
        "",
        "[lighting]",
        "# Switch the LEDs of all devices connected to the hub on (true) or off (false).",
        f"on = {'true' if cfg.light_on else 'false'}",
        "",
        "# Effect while the system runs: " + ", ".join(REGULAR_MODES),
        "[lighting.effect]",
        *_toml_effect(cfg.effect),
        "",
        "# Effect at power-up: " + ", ".join(BOOT_MODES),
        "[lighting.boot]",
        *_toml_effect(cfg.boot),
        "",
        "# Fan curves: [motherboard PWM duty %, fan duty %] pairs from x=0 to x=100.",
        "[fans]",
        *(f"port{port} = [" + ", ".join(f"[{x}, {y}]" for x, y in pts) + "]"
          for port, pts in cfg.fan_curves.items()),
        "",
        "# MQTT: requests on <topic> are JSON objects such as {\"command\": \"light\", \"on\": false};",
        "# JSON replies go to reply_topic (empty: \"<topic>/reply\").  Served by the GUI while",
        "# enabled is true.",
        "[mqtt]",
        *(f"{f.name} = {_toml_scalar(getattr(cfg.mqtt, f.name))}" for f in dataclasses.fields(MqttSettings)),
    ]
    return "\n".join(lines) + "\n"


def save(cfg: Config, path: Path | None = None) -> Path:
    """Atomically write @p cfg to @p path (default: the user file) and return the path."""
    target = path if path is not None else user_config_path()
    text = dump(cfg)
    parse(tomllib.loads(text))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, target)
    except BaseException:
        os.unlink(tmp)
        raise
    return target


def effect_matches(hub: Hub, settings: EffectSettings, slot: SlotType, devs: list[SubDevice]) -> bool:
    """Whether every sub-device in @p devs already stores @p settings in @p slot."""
    expected = metadata(settings)
    for dev in devs:
        stored = hub.read_effect_metadata(dev.id, slot)
        if stored is None or stored[:len(expected)] != expected:
            return False
    return True


def apply(hub: Hub, cfg: Config, log: Callable[[str], None] = lambda _: None, *,
          force: bool = False) -> None:
    """Bring the hub in line with @p cfg.

    Effects and curves are only written when they differ from what the hub stores, so
    starting the program repeatedly does not wear out the hub's flash memory.
    """
    devs = hub.subdevices(details=False)
    lit = [d for d in devs if d.led_count > 0]
    for settings, slot, what in ((cfg.effect, SlotType.REGULAR, "effect"), (cfg.boot, SlotType.BOOT, "boot effect")):
        if force or not effect_matches(hub, settings, slot, lit):
            hub.upload_effect(settings, boot=slot == SlotType.BOOT, subdevices=lit)
            log(f"{what}: stored {settings.mode}")
        else:
            log(f"{what}: {settings.mode} already stored")
    for port, points in cfg.fan_curves.items():
        current = hub.fan_curve(port)
        if force or current.points != points:
            hub.set_fan_curve(port, current.profile, points)
            log(f"fan port {port}: curve stored")
    hub.turn_light(cfg.light_on, [d.id for d in devs])
    log(f"LEDs {'on' if cfg.light_on else 'off'}")
