"""Command line interface; without a command the GUI starts."""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import IO, Any

from . import __version__, config
from .effects import (BOOT_MODES, MODES, PREVIEW_PRESET, REGULAR_MODES, EffectSettings, format_color,
                      parse_color, settings_from_metadata)
from .hub import (DEFAULT_TIMEOUT, MAX_EFFECT_SLOT, PORT_COUNT, VENDOR_ID, Hub, HubError, PortStatus,
                  SlotType, check_port, find_hubs, open_hub, validate_curve)


def parse_curve(text: str) -> list[tuple[int, int]]:
    """Parse "x:y,x:y,..." into a validated list of points."""
    try:
        points = [(int(x), int(y)) for x, y in (p.split(":") for p in text.split(","))]
    except ValueError as e:
        raise ValueError(f"malformed curve {text!r}; expected x:y,x:y,...") from e
    validate_curve(points)
    return points


def parse_ports(text: str) -> list[int]:
    """Parse "1,3" or "all" into a list of 1-based ports."""
    if text == "all":
        return list(range(1, PORT_COUNT + 1))
    ports = [int(p) for p in text.split(",")]
    for p in ports:
        check_port(p)
    return ports


def _open(args: argparse.Namespace) -> Hub:
    """Open the hub selected by the options."""
    return open_hub(args.device, args.serial, args.timeout)


def _out(args: argparse.Namespace) -> IO[str]:
    """Where command output goes."""
    out: IO[str] = args.out
    return out


def _emit(args: argparse.Namespace, obj: Any, text: Iterator[str]) -> None:
    """Print @p obj as JSON with --json, otherwise the lines of @p text."""
    if args.json:
        print(json.dumps(obj, indent=2, default=dataclasses.asdict), file=_out(args))
    else:
        for line in text:
            print(line, file=_out(args))


def cmd_list(args: argparse.Namespace) -> None:
    """Show all hubs without opening them."""
    hubs = find_hubs()
    _emit(args, hubs, (f"{h.devnode}  {VENDOR_ID:04x}:{h.product_id:04x}  {h.serial}" for h in hubs))


def cmd_info(args: argparse.Namespace) -> None:
    """Show firmware, serial number and lighting configuration."""
    with _open(args) as hub:
        info = {
            "firmware": hub.firmware_version(),
            "serial": hub.serial_number(),
            "memory_ok": hub.memory_ok(),
            "windows_dynamic_lighting_opt_in": hub.windows_dynamic_lighting_opt_in(),
            "dynamic_lighting_on": hub.dynamic_lighting_on(),
            "port_types": hub.port_types(),
            "argb2_compatible": hub.argb2_compatible_ports(),
        }
    _emit(args, info, (f"{k.replace('_', ' ')}: {v}" for k, v in info.items()))


def _status_lines(status: list[PortStatus]) -> Iterator[str]:
    """Format fan status as a table."""
    yield "port  duty    rpm  state"
    for s in status:
        rpm = "-" if s.rpm is None else str(s.rpm)
        yield f"{s.port:<4}  {s.duty:3d}%  {rpm:>5}  {s.state}"


def cmd_status(args: argparse.Namespace) -> None:
    """Show live fan duty and speed, optionally repeatedly."""
    with _open(args) as hub:
        while True:
            status = hub.fan_status()
            _emit(args, status, _status_lines(status))
            if args.watch is None:
                break
            _out(args).flush()
            time.sleep(args.watch)
            if not args.json:
                print(file=_out(args))


def cmd_devices(args: argparse.Namespace) -> None:
    """List the ARGB sub-devices."""
    with _open(args) as hub:
        devs = hub.subdevices()
    _emit(args, devs, (f"id {d.id:#04x}  port {d.port}  {d.led_count:3d} LEDs  {d.argb_version:<14}"
                       f"  {d.type_name}{f' ({d.name})' if d.name else ''}" for d in devs))


def cmd_curve(args: argparse.Namespace) -> None:
    """Show the fan curves of the selected ports."""
    with _open(args) as hub:
        curves = [hub.fan_curve(p) for p in args.ports]
    _emit(args, curves, (f"port {c.port}  profile {c.profile}  "
                         + " ".join(f"{x}:{y}" for x, y in c.points) for c in curves))


def cmd_set_curve(args: argparse.Namespace) -> None:
    """Store a fan curve on the selected ports."""
    with _open(args) as hub:
        for port in args.ports:
            profile = args.profile if args.profile is not None else hub.fan_curve(port).profile
            hub.set_fan_curve(port, profile, args.points)
            print(f"port {port}: " + " ".join(f"{x}:{y}" for x, y in hub.fan_curve(port).points),
                  file=_out(args))


def cmd_light(args: argparse.Namespace) -> None:
    """Switch LEDs on or off."""
    with _open(args) as hub:
        ids = args.ids if args.ids else [d.id for d in hub.subdevices(details=False)]
        hub.turn_light(args.state == "on", ids)
    print(f"LEDs {args.state}", file=_out(args))


def cmd_slot(args: argparse.Namespace) -> None:
    """Activate a stored lighting effect slot."""
    with _open(args) as hub:
        hub.select_effect_slot(args.slot)
    print(f"slot {args.slot} active", file=_out(args))


def _describe(e: EffectSettings | None) -> str:
    """One-line summary of an effect."""
    if e is None:
        return "(none)"
    parts = [e.mode]
    if e.info.has_speed:
        parts.append(f"speed {e.speed}")
    parts.append(f"brightness {e.brightness}")
    if e.info.colors:
        parts.append(" ".join(format_color(c) for c in e.effective_colors()))
    return ", ".join(parts)


def cmd_effects(args: argparse.Namespace) -> None:
    """Show the effects stored for each sub-device."""
    with _open(args) as hub:
        rows = []
        for d in hub.subdevices(details=False):
            if d.led_count == 0:
                continue
            meta = hub.read_effect_metadata(d.id, SlotType.REGULAR)
            temporary = meta is not None and meta[0] == PREVIEW_PRESET
            rows.append((d, None if meta is None else settings_from_metadata(meta), temporary,
                         hub.read_effect(d.id, SlotType.BOOT)))
    _emit(args, [{"id": d.id, "effect": e, "temporary": t, "boot": b} for d, e, t, b in rows],
          (f"id {d.id:#04x}  effect: {_describe(e)}{' (temporary)' if t else ''}\n"
           f"       boot:   {_describe(b)}" for d, e, t, b in rows))


def cmd_effect(args: argparse.Namespace) -> None:
    """Store an effect, or with --timeout show it for a while and return to the previous one."""
    settings = EffectSettings(args.mode, speed=args.speed, brightness=args.brightness,
                              colors=args.colors or [])
    settings.validate()
    timeout: float | None = args.effect_timeout
    if timeout is not None and settings.info.boot:
        raise ValueError("boot effects cannot be shown temporarily")
    with _open(args) as hub:
        targets = [d for d in hub.subdevices(details=False) if not args.ids or d.id in args.ids]
        if timeout is None:
            hub.upload_effect(settings, boot=settings.info.boot, subdevices=targets)
            print(f"stored {_describe(settings)}", file=_out(args))
        else:
            hub.upload_effect(settings, preview=True, subdevices=targets)
            try:
                time.sleep(timeout)
            finally:
                hub.cancel_preview(targets)


def cmd_apply(args: argparse.Namespace) -> None:
    """Apply the configuration file to the hub."""
    cfg = config.load(args.config)
    with _open(args) as hub:
        config.apply(hub, cfg, lambda line: print(line, file=_out(args)), force=args.force)


def cmd_config(args: argparse.Namespace) -> None:
    """Show where the configuration comes from and the effective settings."""
    sources = [args.config] if args.config is not None else config.config_sources()
    print("# defaults: packaged")
    for source in sources:
        print(f"# overlaid with: {source}{'' if source.exists() else ' (absent)'}")
    print(config.dump(config.load(args.config)), end="")


def cmd_raw(args: argparse.Namespace) -> None:
    """Send arbitrary bytes and dump the reply; for protocol exploration."""
    if len(args.bytes) < 2:
        raise ValueError("raw needs at least a device type and a command byte")
    with _open(args) as hub:
        data = hub.transact(args.bytes[0], args.bytes[1], bytes(args.bytes[2:]), check=False)
    print(data.rstrip(b"\0").hex(" ") or "(empty)")


def cmd_gui(args: argparse.Namespace) -> None:
    """Start the graphical interface."""
    try:
        from .gui import run
    except ImportError as e:
        raise HubError(f"the GUI needs PyGObject with GTK 4 and libadwaita: {e}") from e
    sys.exit(run(args))


def _byte(text: str) -> int:
    """argparse type: a byte given in hex, with or without 0x."""
    value = int(text, 16)
    if not 0 <= value <= 0xFF:
        raise argparse.ArgumentTypeError(f"{text} is not a byte")
    return value


def _positive(text: str) -> float:
    """argparse type: a positive number of seconds."""
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError(f"{text} is not a positive number")
    return value


def _wrap[T](fn: Callable[[str], T]) -> Callable[[str], T]:
    """Turn ValueError from a parser function into an argparse error."""
    def inner(text: str) -> T:
        try:
            return fn(text)
        except ValueError as e:
            raise argparse.ArgumentTypeError(str(e)) from e
    inner.__name__ = fn.__name__
    return inner


def _gui_options(parser: argparse.ArgumentParser, inherit: bool = False) -> None:
    """Options of the GUI, accepted both before and after the "gui" command.

    With @p inherit the options default to "not given", so the subcommand does not reset
    values set before it.
    """
    parser.add_argument("--no-apply", action="store_true", default=argparse.SUPPRESS if inherit else False,
                        help="GUI: do not apply the configuration on startup")
    parser.add_argument("--minimized", action=argparse.BooleanOptionalAction,
                        default=argparse.SUPPRESS if inherit else None,
                        help="GUI: start with the window minimized, or not (default: [gui] minimized "
                             "in the configuration file)")


def build_parser(parser_class: type[argparse.ArgumentParser] = argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Create the command line parser."""
    parser = parser_class(prog="fractal-adjust",
                          description="Control the Fractal Adjust Pro Hub. Without a command the GUI starts.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-d", "--device", help="hidraw node to use, e.g. /dev/hidraw11")
    parser.add_argument("-s", "--serial", help="select the hub with this USB serial number")
    parser.add_argument("-c", "--config", type=Path,
                        help=f"configuration file (default: {config.user_config_path()})")
    parser.add_argument("-j", "--json", action="store_true", help="machine-readable output")
    parser.add_argument("-t", "--hid-timeout", dest="timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds to wait for each reply from the hub (default %(default)s)")
    _gui_options(parser)
    parser.set_defaults(func=cmd_gui, command="gui")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("gui", help="start the graphical interface (default); serves MQTT requests")
    _gui_options(p, inherit=True)
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("apply", help="apply the configuration file to the hub")
    p.add_argument("-f", "--force", action="store_true", help="write effects and curves even if unchanged")
    p.set_defaults(func=cmd_apply)

    sub.add_parser("config", help="show the effective configuration").set_defaults(func=cmd_config)
    sub.add_parser("list", help="list connected hubs").set_defaults(func=cmd_list)
    sub.add_parser("info", help="firmware, serial and lighting configuration").set_defaults(func=cmd_info)

    p = sub.add_parser("status", help="fan duty cycles and speeds")
    p.add_argument("-w", "--watch", type=float, metavar="SECONDS", help="repeat every SECONDS")
    p.set_defaults(func=cmd_status)

    sub.add_parser("devices", help="list ARGB sub-devices").set_defaults(func=cmd_devices)
    sub.add_parser("effects", help="show the stored effects").set_defaults(func=cmd_effects)

    p = sub.add_parser("effect", help="store a lighting effect, or show it for a limited time",
                       description="Regular effects: " + ", ".join(REGULAR_MODES)
                       + ". Boot effects: " + ", ".join(BOOT_MODES) + ".")
    p.add_argument("mode", choices=list(MODES))
    p.add_argument("colors", nargs="*", type=_wrap(parse_color), metavar="COLOR", help="#rrggbb")
    p.add_argument("--speed", type=int, default=50, help="0-100 (default %(default)s)")
    p.add_argument("--brightness", type=int, default=100, help="0-100 (default %(default)s)")
    p.add_argument("--id", dest="ids", type=_byte, action="append", help="only this sub-device (hex)")
    p.add_argument("--timeout", "--preview", dest="effect_timeout", type=_positive, metavar="SECONDS",
                   help="show the effect for SECONDS without storing it, then return to the previous "
                        "effect (default: store it permanently)")
    p.set_defaults(func=cmd_effect)

    p = sub.add_parser("curve", help="show fan curves")
    p.add_argument("ports", nargs="?", type=_wrap(parse_ports), default=parse_ports("all"),
                   help="comma-separated ports 1-4 or 'all' (default)")
    p.set_defaults(func=cmd_curve)

    p = sub.add_parser("set-curve", help="store a fan curve (motherboard duty %% -> fan duty %%)")
    p.add_argument("ports", type=_wrap(parse_ports), help="comma-separated ports 1-4 or 'all'")
    p.add_argument("points", type=_wrap(parse_curve), help="e.g. 0:20,40:30,60:50,80:80,100:100")
    p.add_argument("--profile", type=int, help="profile ID to store (default: keep current)")
    p.set_defaults(func=cmd_set_curve)

    p = sub.add_parser("light", help="switch LEDs on or off")
    p.add_argument("state", choices=("on", "off"))
    p.add_argument("ids", nargs="*", type=_byte, help="sub-device IDs in hex (default: all)")
    p.set_defaults(func=cmd_light)

    p = sub.add_parser("slot", help="activate a stored effect slot")
    p.add_argument("slot", type=int, choices=range(MAX_EFFECT_SLOT + 1))
    p.set_defaults(func=cmd_slot)

    p = sub.add_parser("raw", help="send raw command bytes (hex) and print the reply")
    p.add_argument("bytes", nargs="+", type=_byte, metavar="BYTE")
    p.set_defaults(func=cmd_raw)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Program entry point."""
    args = build_parser().parse_args(argv)
    args.out = sys.stdout
    try:
        args.func(args)
    except (HubError, ValueError, config.ConfigError) as e:
        print(f"fractal-adjust: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
