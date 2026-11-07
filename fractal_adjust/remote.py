"""JSON remote-control requests, as delivered over MQTT while the GUI runs.

A request is a JSON object with a "command", an optional "id" that is echoed in the reply,
and the command's parameters as further members.  The reply is a JSON object with "ok" and
either "result" or "error".  The format is documented in MQTT.md.  Requests never write the
configuration file.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config
from .effects import MODES, PREVIEW_PRESET, EffectSettings, format_color, parse_color, settings_from_metadata
from .hub import PORT_COUNT, Hub, HubError, SlotType, SubDevice, validate_curve
from .service import HubService


class RequestError(Exception):
    """The request is malformed or not allowed."""


class Params:
    """Typed access to a request's parameters; unused members are an error."""

    def __init__(self, members: dict[str, Any]) -> None:
        self._members = members
        self._used: set[str] = set()

    def _raw(self, name: str, required: bool) -> Any:
        self._used.add(name)
        if name not in self._members or self._members[name] is None:
            if required:
                raise RequestError(f"missing parameter {name!r}")
            return None
        return self._members[name]

    def get_int(self, name: str, default: int | None = None, *, required: bool = False,
                low: int | None = None, high: int | None = None) -> int | None:
        """An integer parameter, optionally range-checked."""
        value = self._raw(name, required)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise RequestError(f"{name} must be an integer")
        if (low is not None and value < low) or (high is not None and value > high):
            raise RequestError(f"{name} must be between {low} and {high}")
        return int(value)

    def get_bool(self, name: str, default: bool | None = None, *, required: bool = False) -> bool | None:
        """A boolean parameter."""
        value = self._raw(name, required)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise RequestError(f"{name} must be true or false")
        return value

    def get_str(self, name: str, *, required: bool = False) -> str | None:
        """A string parameter."""
        value = self._raw(name, required)
        if value is not None and not isinstance(value, str):
            raise RequestError(f"{name} must be a string")
        return value

    def get_number(self, name: str) -> float | None:
        """A positive finite number, or None."""
        value = self._raw(name, False)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                or value <= 0:
            raise RequestError(f"{name} must be a positive number")
        return float(value)

    def get_list(self, name: str, *, required: bool = False) -> list[Any] | None:
        """A list parameter."""
        value = self._raw(name, required)
        if value is not None and not isinstance(value, list):
            raise RequestError(f"{name} must be a list")
        return value

    def finish(self) -> None:
        """Reject members that no handler looked at."""
        unknown = sorted(set(self._members) - self._used)
        if unknown:
            raise RequestError(f"unknown parameters: {', '.join(unknown)}")


def _ids(params: Params) -> list[int] | None:
    """Sub-device IDs: integers or hex strings such as "0x31"."""
    raw = params.get_list("ids")
    if raw is None:
        return None
    ids = []
    for item in raw:
        if isinstance(item, str):
            try:
                item = int(item, 16)
            except ValueError as e:
                raise RequestError(f"invalid sub-device ID {item!r}") from e
        if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 0xFF:
            raise RequestError("ids must be integers 0-255 or hex strings")
        ids.append(item)
    return ids


def _ports(params: Params, required: bool) -> list[int]:
    """Port numbers 1-4; missing means all when not required."""
    raw = params.get_list("ports", required=required)
    if raw is None:
        return list(range(1, PORT_COUNT + 1))
    if not raw or any(isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= PORT_COUNT for p in raw):
        raise RequestError(f"ports must be a non-empty list of integers 1-{PORT_COUNT}")
    return raw


def effect_json(e: EffectSettings | None) -> dict[str, Any] | None:
    """Represent an effect as JSON."""
    if e is None:
        return None
    result: dict[str, Any] = {"mode": e.mode}
    if e.info.has_speed:
        result["speed"] = e.speed
    result["brightness"] = e.brightness
    if e.info.colors:
        result["colors"] = [format_color(c) for c in e.effective_colors()]
    if e.mode == "wave-ripple":
        result |= {"up_wave": e.up_wave, "top_wave": e.top_wave, "down_wave": e.down_wave,
                   "frequency": e.frequency}
    return result


def _device_json(d: SubDevice) -> dict[str, Any]:
    return dataclasses.asdict(d) | {"id_hex": f"{d.id:#04x}"}


# Command handlers: (hub, params, context) -> JSON-serializable result.

@dataclasses.dataclass(slots=True)
class Context:
    """What a handler may need besides the hub."""

    service: HubService
    config_path: Path | None


def _info(hub: Hub, p: Params, _c: Context) -> Any:
    p.finish()
    return {"firmware": hub.firmware_version(), "serial": hub.serial_number(), "memory_ok": hub.memory_ok(),
            "windows_dynamic_lighting_opt_in": hub.windows_dynamic_lighting_opt_in(),
            "dynamic_lighting_on": hub.dynamic_lighting_on(), "port_types": hub.port_types(),
            "argb2_compatible": hub.argb2_compatible_ports()}


def _status(hub: Hub, p: Params, _c: Context) -> Any:
    p.finish()
    return [dataclasses.asdict(s) for s in hub.fan_status()]


def _devices(hub: Hub, p: Params, _c: Context) -> Any:
    p.finish()
    return [_device_json(d) for d in hub.subdevices()]


def _effects(hub: Hub, p: Params, _c: Context) -> Any:
    p.finish()
    result = []
    for d in hub.subdevices(details=False):
        if d.led_count == 0:
            continue
        meta = hub.read_effect_metadata(d.id, SlotType.REGULAR)
        result.append({"id": d.id, "id_hex": f"{d.id:#04x}",
                       "effect": effect_json(None if meta is None else settings_from_metadata(meta)),
                       "temporary": meta is not None and meta[0] == PREVIEW_PRESET,
                       "boot": effect_json(hub.read_effect(d.id, SlotType.BOOT))})
    return result


def _effect(hub: Hub, p: Params, c: Context) -> Any:
    mode = p.get_str("mode", required=True)
    assert mode is not None
    if mode not in MODES:
        raise RequestError(f"unknown mode {mode!r}; known: {', '.join(MODES)}")
    colors_raw = p.get_list("colors") or []
    if not all(isinstance(x, str) for x in colors_raw):
        raise RequestError('colors must be "#rrggbb" strings')
    try:
        colors = [parse_color(x) for x in colors_raw]
    except ValueError as e:
        raise RequestError(f"colors: {e}") from e
    settings = EffectSettings(mode, colors=colors)
    for name in ("speed", "brightness", "up_wave", "top_wave", "down_wave", "frequency"):
        value = p.get_int(name)
        if value is not None:
            setattr(settings, name, value)
    ids = _ids(p)
    timeout = p.get_number("timeout")
    p.finish()
    settings.validate()
    if timeout is not None and settings.info.boot:
        raise RequestError("boot effects cannot be shown temporarily")
    targets = [d for d in hub.subdevices(details=False) if ids is None or d.id in ids]
    if timeout is None:
        hub.upload_effect(settings, boot=settings.info.boot, subdevices=targets)
        c.service.stored_effect_changed(hub)
    else:
        c.service.show_temporary(hub, settings, ids, timeout)
    return {"effect": effect_json(settings), "boot": settings.info.boot, "stored": timeout is None,
            "timeout": timeout, "ids": [d.id for d in targets if d.led_count > 0]}


def _curve(hub: Hub, p: Params, _c: Context) -> Any:
    ports = _ports(p, required=False)
    p.finish()
    return [dataclasses.asdict(hub.fan_curve(port)) for port in ports]


def _set_curve(hub: Hub, p: Params, _c: Context) -> Any:
    ports = _ports(p, required=True)
    raw = p.get_list("points", required=True)
    assert raw is not None
    if not all(isinstance(pt, list) and len(pt) == 2 and all(type(v) is int for v in pt) for pt in raw):
        raise RequestError("points must be a list of [x, y] integer pairs")
    points = [(pt[0], pt[1]) for pt in raw]
    profile = p.get_int("profile", low=0, high=255)
    p.finish()
    validate_curve(points)
    result = []
    for port in ports:
        hub.set_fan_curve(port, profile if profile is not None else hub.fan_curve(port).profile, points)
        result.append(dataclasses.asdict(hub.fan_curve(port)))
    return result


def _light(hub: Hub, p: Params, _c: Context) -> Any:
    on = p.get_bool("on", required=True)
    ids = _ids(p)
    p.finish()
    assert on is not None
    targets = ids if ids else [d.id for d in hub.subdevices(details=False)]
    hub.turn_light(on, targets)
    return {"on": on, "ids": targets}


def _slot(hub: Hub, p: Params, _c: Context) -> Any:
    slot = p.get_int("slot", required=True, low=0, high=3)
    p.finish()
    assert slot is not None
    hub.select_effect_slot(slot)
    return {"slot": slot}


def _apply(hub: Hub, p: Params, c: Context) -> Any:
    force = p.get_bool("force", False)
    p.finish()
    log: list[str] = []
    config.apply(hub, config.load(c.config_path), log.append, force=bool(force))
    return {"log": log}


HANDLERS: dict[str, Callable[[Hub, Params, Context], Any]] = {
    "info": _info, "status": _status, "devices": _devices, "effects": _effects, "effect": _effect,
    "curve": _curve, "set-curve": _set_curve, "light": _light, "slot": _slot, "apply": _apply,
}


def _reply(request_id: Any, command: str | None, *, result: Any = None, error: str | None = None) -> str:
    reply: dict[str, Any] = {"id": request_id}
    if command is not None:
        reply["command"] = command
    if error is None:
        reply |= {"ok": True, "result": result}
    else:
        reply |= {"ok": False, "error": error}
    return json.dumps(reply, ensure_ascii=False)


def _done(text: str) -> concurrent.futures.Future[str]:
    future: concurrent.futures.Future[str] = concurrent.futures.Future()
    future.set_result(text)
    return future


def execute(service: HubService, payload: bytes, config_path: Path | None = None) -> concurrent.futures.Future[str]:
    """Run one request on the service's hub thread; the future yields the JSON reply.

    @p config_path is the configuration file that "apply" reads.  Requests never write it.
    """
    request_id: Any = None
    try:
        try:
            request = json.loads(payload.decode("utf-8"))
        except UnicodeDecodeError as e:
            raise RequestError("request is not UTF-8 text") from e
        except json.JSONDecodeError as e:
            raise RequestError(f"request is not valid JSON: {e}") from e
        if not isinstance(request, dict):
            raise RequestError("request must be a JSON object")
        request_id = request.pop("id", None)
        if not isinstance(request_id, (str, int, float, type(None))) or isinstance(request_id, bool):
            request_id = None
            raise RequestError("id must be a string, a number or null")
        command = request.pop("command", None)
        if not isinstance(command, str):
            raise RequestError("missing \"command\" string")
        if command not in HANDLERS:
            raise RequestError(f"unknown command {command!r}; allowed: {', '.join(sorted(HANDLERS))}")
    except RequestError as e:
        return _done(_reply(request_id, None, error=str(e)))

    handler = HANDLERS[command]
    params = Params(request)
    context = Context(service, config_path)

    def job(hub: Hub) -> str:
        try:
            return _reply(request_id, command, result=handler(hub, params, context))
        except (RequestError, HubError, ValueError, config.ConfigError) as e:
            if isinstance(e, HubError) and "failed:" in str(e):
                service.drop()
            return _reply(request_id, command, error=str(e))

    future = service.submit(job)
    result: concurrent.futures.Future[str] = concurrent.futures.Future()

    def forward(f: concurrent.futures.Future[str]) -> None:
        error = f.exception()
        result.set_result(_reply(request_id, command, error=str(error)) if error is not None else f.result())
    future.add_done_callback(forward)
    return result
