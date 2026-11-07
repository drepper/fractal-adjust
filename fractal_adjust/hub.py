"""Protocol layer for the Fractal Adjust Pro Hub (USB 36bc:1001) over Linux hidraw.

The protocol was taken from Fractal's WebHID application at
https://adjust.fractal-design.com and is described in PROTOCOL.md.
"""
from __future__ import annotations

import dataclasses
import fcntl
import os
import random
import select
import time
from collections.abc import Sequence
from enum import IntEnum
from pathlib import Path
from typing import Protocol

from .effects import PREVIEW_PRESET, EffectSettings, keyframes, metadata, settings_from_metadata

VENDOR_ID = 0x36BC
PRODUCT_IDS = frozenset({0x1001, 0x1002})
REPORT_ID = 2
REPORT_SIZE = 63
PORT_COUNT = 4
MAX_EFFECT_SLOT = 3
MAX_CURVE_POINTS = (REPORT_SIZE - 5) // 2
MAX_CHUNK = 60
METADATA_OFFSET = 10
METADATA_LENGTH = 42
DEFAULT_TIMEOUT = 1.0


class DeviceType(IntEnum):
    """First payload byte: selects the command group."""

    EFFECT = 0xA4
    STREAMING = 0xA5
    FW_UPGRADE = 0xB5
    PWM = 0xD1
    FACTORY = 0xF1


class EffectCmd(IntEnum):
    """Commands of the EFFECT (lighting) group."""

    SELECT_EFFECT_SLOT = 0x04
    READ_EFFECT = 0x05
    GET_SUBDEVICE_INFO = 0x08
    SELECT_SUBDEVICE = 0x0A
    RESTART_ANIMATION = 0x0B
    TURN_LIGHT = 0x0C
    GET_SUBDEVICE_NAME = 0x0E
    GET_SUBDEVICE_TYPE = 0x10
    SET_TIMESTAMP_EFFECT = 0x11
    SEND_TIMESTAMP_EFFECT = 0x12
    COMMIT_TIMESTAMP_EFFECT = 0x13
    GET_PORT_TYPES = 0x14
    GET_ROTATION = 0x16
    GET_MIRROR = 0x18
    IS_DYNAMIC_LIGHTING_ON = 0x1A
    ARE_PORTS_ARGB2_COMPATIBLE = 0x1B
    STOP_LOADING = 0x78


class PwmCmd(IntEnum):
    """Commands of the PWM (fan) group."""

    GET_STATUS = 0x01
    GET_MAP = 0x02
    SET_MAP = 0x03
    GET_DUTY = 0x04
    GET_TACHO = 0x05


class FactoryCmd(IntEnum):
    """Commands of the FACTORY (device information) group."""

    GET_FW_VERSION = 0x01
    GET_SERIAL = 0x02
    GET_WDL_SWITCH = 0x14
    IS_MEMORY_OK = 0x1C


class ArgbVersion(IntEnum):
    """ARGB protocol generation of a port or sub-device."""

    ARGB1 = 1
    ARGB2 = 2
    ARGB1_FALLBACK = 5


class SlotType(IntEnum):
    """Which of a sub-device's two stored effects a command addresses."""

    BOOT = 0
    REGULAR = 1


class HubError(Exception):
    """Communication with the hub failed or the hub reported an error."""


class Transport(Protocol):
    """Raw report channel to the device; reports include the report ID byte."""

    def write(self, report: bytes) -> None:
        """Send one output report."""
        ...

    def read(self, timeout: float) -> bytes | None:
        """Return one input report, or None if none arrives within @p timeout seconds."""
        ...

    def close(self) -> None:
        """Release the device."""
        ...


class HidrawTransport:
    """Transport over a /dev/hidrawN node, locked against concurrent users."""

    def __init__(self, path: str) -> None:
        """Open @p path read/write and take an exclusive advisory lock on it."""
        try:
            self._fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        except PermissionError as e:
            raise HubError(f"no permission to open {path}; install the udev rule "
                           "70-fractal-adjust.rules") from e
        except OSError as e:
            raise HubError(f"cannot open {path}: {e.strerror}") from e
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            os.close(self._fd)
            raise HubError(f"{path} is in use by another fractal-adjust process") from e

    def write(self, report: bytes) -> None:
        try:
            os.write(self._fd, report)
        except OSError as e:
            raise HubError(f"write to hub failed: {e.strerror}") from e

    def read(self, timeout: float) -> bytes | None:
        ready, _, _ = select.select([self._fd], [], [], max(timeout, 0.0))
        if not ready:
            return None
        try:
            return os.read(self._fd, REPORT_SIZE + 1)
        except OSError as e:
            raise HubError(f"read from hub failed: {e.strerror}") from e

    def close(self) -> None:
        os.close(self._fd)


@dataclasses.dataclass(frozen=True, slots=True)
class HubInfo:
    """A hub found in sysfs."""

    devnode: str
    product_id: int
    serial: str


@dataclasses.dataclass(slots=True)
class SubDevice:
    """An ARGB device daisy-chained on one of the hub's ports."""

    id: int
    port: int
    led_count: int
    argb_version: str
    type_name: str = ""
    name: str = ""


@dataclasses.dataclass(slots=True)
class FanCurve:
    """Mapping from the motherboard PWM duty (x, %) to the fan duty (y, %)."""

    port: int
    profile: int
    points: list[tuple[int, int]]


@dataclasses.dataclass(slots=True)
class PortStatus:
    """Live fan data for one hub port, or for the motherboard PWM input."""

    port: str
    duty: int
    rpm: int | None
    state: int


def find_hubs(sysfs: Path = Path("/sys/class/hidraw")) -> list[HubInfo]:
    """Return all Adjust Pro hubs known to the hidraw subsystem."""
    hubs: list[HubInfo] = []
    for node in sorted(sysfs.glob("hidraw*")):
        try:
            uevent = (node / "device" / "uevent").read_text()
        except OSError:
            continue
        fields = dict(line.split("=", 1) for line in uevent.splitlines() if "=" in line)
        try:
            _, vid, pid = (int(part, 16) for part in fields.get("HID_ID", "").split(":"))
        except ValueError:
            continue
        if vid == VENDOR_ID and pid in PRODUCT_IDS:
            hubs.append(HubInfo(f"/dev/{node.name}", pid, fields.get("HID_UNIQ", "")))
    return hubs


def _cstring(data: bytes) -> str:
    """Decode a NUL-terminated ASCII string."""
    return data.split(b"\0", 1)[0].decode("ascii", "replace")


def _argb_name(value: int) -> str:
    """Name an ARGB version code; the hub reports 0 for ARGB2 sub-devices."""
    if value == 0:
        return ArgbVersion.ARGB2.name
    try:
        return ArgbVersion(value).name
    except ValueError:
        return f"unknown({value})"


def check_port(port: int) -> None:
    """Reject port numbers outside 1..PORT_COUNT."""
    if not 1 <= port <= PORT_COUNT:
        raise ValueError(f"port must be between 1 and {PORT_COUNT}")


def validate_curve(points: Sequence[tuple[int, int]]) -> None:
    """Check that @p points form a curve over 0..100 % with strictly increasing x."""
    if not 2 <= len(points) <= MAX_CURVE_POINTS:
        raise ValueError(f"a curve needs between 2 and {MAX_CURVE_POINTS} points")
    if any(not (0 <= x <= 100 and 0 <= y <= 100) for x, y in points):
        raise ValueError("curve values must be between 0 and 100")
    if any(a[0] >= b[0] for a, b in zip(points, points[1:])):
        raise ValueError("curve x values must be strictly increasing")
    if points[0][0] != 0 or points[-1][0] != 100:
        raise ValueError("curve must start at x=0 and end at x=100")


class Hub:
    """High-level access to one Adjust Pro Hub."""

    def __init__(self, transport: Transport, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Wrap an open @p transport; replies must arrive within @p timeout seconds."""
        self._transport = transport
        self._timeout = timeout

    def __enter__(self) -> Hub:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the device."""
        self._transport.close()

    def transact(self, dev_type: int, cmd: int, args: bytes = b"", *, check: bool = True) -> bytes:
        """Send one command and return the reply payload (without the report ID).

        The reply starts with the echoed @p dev_type and @p cmd, then a status byte which
        is 0 on success.  A nonzero status raises HubError unless @p check is false.
        """
        payload = bytes((dev_type, cmd)) + args
        if len(payload) > REPORT_SIZE:
            raise ValueError(f"command payload of {len(payload)} bytes exceeds {REPORT_SIZE}")
        while self._transport.read(0) is not None:
            pass
        self._transport.write(bytes((REPORT_ID,)) + payload.ljust(REPORT_SIZE, b"\0"))
        deadline = time.monotonic() + self._timeout
        while (remaining := deadline - time.monotonic()) > 0:
            report = self._transport.read(remaining)
            if report is None:
                break
            if len(report) < 4 or report[0] != REPORT_ID:
                continue
            data = report[1:]
            if data[0] != dev_type or data[1] != cmd:
                continue
            if check and data[2] != 0:
                raise HubError(f"command {dev_type:#04x}/{cmd:#04x} failed with status {data[2]}")
            return data.ljust(REPORT_SIZE, b"\0")
        raise HubError(f"no reply to command {dev_type:#04x}/{cmd:#04x}")

    # Device information.

    def firmware_version(self) -> str:
        """Return the firmware version string, e.g. "1.1.5"."""
        return _cstring(self.transact(DeviceType.FACTORY, FactoryCmd.GET_FW_VERSION)[3:19])

    def serial_number(self) -> str:
        """Return the serial number stored in the device."""
        return _cstring(self.transact(DeviceType.FACTORY, FactoryCmd.GET_SERIAL)[6:23])

    def memory_ok(self) -> bool:
        """Report whether the hub's settings memory is healthy."""
        data = self.transact(DeviceType.FACTORY, FactoryCmd.IS_MEMORY_OK, check=False)
        return data[2] == 1 or data[3] != 1

    def windows_dynamic_lighting_opt_in(self) -> bool:
        """Report whether the hub is opted in to Windows Dynamic Lighting."""
        data = self.transact(DeviceType.FACTORY, FactoryCmd.GET_WDL_SWITCH, check=False)
        return data[2] == 1 or data[3] == 1

    def dynamic_lighting_on(self) -> bool:
        """Report whether Windows Dynamic Lighting currently controls the LEDs."""
        return self.transact(DeviceType.EFFECT, EffectCmd.IS_DYNAMIC_LIGHTING_ON)[3] == 1

    def argb2_compatible_ports(self) -> list[bool]:
        """Return, per port, whether everything attached supports ARGB2."""
        data = self.transact(DeviceType.EFFECT, EffectCmd.ARE_PORTS_ARGB2_COMPATIBLE, check=False)
        if data[2] == 1:
            return [True] * PORT_COUNT
        return [b == 1 for b in data[3:3 + PORT_COUNT]]

    def port_types(self) -> list[str]:
        """Return the configured ARGB protocol of each port."""
        data = self.transact(DeviceType.EFFECT, EffectCmd.GET_PORT_TYPES)
        return [_argb_name(b) for b in data[3:3 + PORT_COUNT]]

    # Sub-devices and lighting.

    def subdevices(self, *, details: bool = True) -> list[SubDevice]:
        """List the ARGB sub-devices; with @p details also query their type and name."""
        data = self.transact(DeviceType.EFFECT, EffectCmd.GET_SUBDEVICE_INFO, b"\0")
        count = data[3]
        if 4 + 3 * count > len(data):
            raise HubError(f"implausible sub-device count {count}")
        result = [SubDevice(id=data[off], port=(data[off] >> 4) + 1, led_count=data[off + 1],
                            argb_version=_argb_name(data[off + 2]))
                  for off in range(4, 4 + 3 * count, 3)]
        if details:
            for dev in result:
                self.select_subdevice(dev.id)
                dev.type_name = self._length_prefixed(EffectCmd.GET_SUBDEVICE_TYPE)
                dev.name = self._length_prefixed(EffectCmd.GET_SUBDEVICE_NAME)
        return result

    def _length_prefixed(self, cmd: EffectCmd) -> str:
        """Fetch a string reply laid out as length byte at offset 3 followed by the text."""
        data = self.transact(DeviceType.EFFECT, cmd, b"\0")
        return data[4:4 + data[3]].decode("utf-8", "replace")

    def select_subdevice(self, sub_id: int) -> None:
        """Direct the following per-sub-device commands to @p sub_id."""
        self.transact(DeviceType.EFFECT, EffectCmd.SELECT_SUBDEVICE, bytes((1, sub_id)))

    def rotation(self) -> int:
        """Rotation of the selected sub-device in eighths of a turn (0, 2, 4, 6)."""
        data = self.transact(DeviceType.EFFECT, EffectCmd.GET_ROTATION, check=False)
        return data[3] if data[2] == 0 else 0

    def mirrored(self) -> bool:
        """Whether the selected sub-device's LED order is mirrored."""
        data = self.transact(DeviceType.EFFECT, EffectCmd.GET_MIRROR, check=False)
        return data[2] == 0 and data[3] != 0

    def turn_light(self, on: bool, ids: Sequence[int]) -> None:
        """Switch the LEDs of the sub-devices @p ids on or off."""
        if len(ids) > REPORT_SIZE - 4:
            raise ValueError("too many sub-device IDs")
        self.transact(DeviceType.EFFECT, EffectCmd.TURN_LIGHT, bytes((int(on), len(ids), *ids)))

    def select_effect_slot(self, slot: int) -> None:
        """Make the effect stored in @p slot (0 is the boot effect) the active one."""
        if not 0 <= slot <= MAX_EFFECT_SLOT:
            raise ValueError(f"effect slot must be between 0 and {MAX_EFFECT_SLOT}")
        self.transact(DeviceType.EFFECT, EffectCmd.SELECT_EFFECT_SLOT, bytes((slot,)))

    def read_effect_metadata(self, sub_id: int, slot: SlotType) -> bytes | None:
        """Return the stored metadata block of one sub-device's effect, or None if it has none."""
        self.select_subdevice(sub_id)
        data = self.transact(DeviceType.EFFECT, EffectCmd.READ_EFFECT, bytes((slot, 0, 0)), check=False)
        if data[2] != 0:
            return None
        return data[METADATA_OFFSET:METADATA_OFFSET + METADATA_LENGTH]

    def read_effect(self, sub_id: int, slot: SlotType) -> EffectSettings | None:
        """Decode the effect stored for @p sub_id in @p slot."""
        meta = self.read_effect_metadata(sub_id, slot)
        return None if meta is None else settings_from_metadata(meta)

    def _effect_targets(self, subdevices: Sequence[SubDevice] | None) -> list[SubDevice]:
        """Sub-devices that accept effects; legacy ARGB1 devices report no LEDs and reject them."""
        devs = subdevices if subdevices is not None else self.subdevices(details=False)
        return [d for d in devs if d.led_count > 0]

    def _send_timestamp_effect(self, slot: SlotType, meta: bytes, frames: bytes, count: int,
                               preview: bool, header_only: bool) -> None:
        """Upload one effect to the selected sub-device (A4 11, A4 12…, A4 13)."""
        if len(frames) > 0xFFFF:
            raise ValueError("effect data exceeds 64 KiB; use fewer LEDs or a simpler effect")
        header = bytes((slot, sum(frames) & 0xFF, len(frames) >> 8, len(frames) & 0xFF,
                        int(preview), count)) + meta
        self.transact(DeviceType.EFFECT, EffectCmd.SET_TIMESTAMP_EFFECT, header)
        if header_only or count == 0:
            return
        for off in range(0, len(frames), MAX_CHUNK):
            chunk = frames[off:off + MAX_CHUNK]
            self.transact(DeviceType.EFFECT, EffectCmd.SEND_TIMESTAMP_EFFECT, bytes((len(chunk),)) + chunk)
        if not preview:
            self.transact(DeviceType.EFFECT, EffectCmd.COMMIT_TIMESTAMP_EFFECT)

    def upload_effect(self, settings: EffectSettings, *, boot: bool = False, preview: bool = False,
                      subdevices: Sequence[SubDevice] | None = None,
                      rng: random.Random | None = None) -> None:
        """Store (or with @p preview only show) an effect on the given or all sub-devices.

        A preview is not written to the hub's memory; cancel_preview() returns to the stored
        effect.  Uploads follow the web app: header, data chunks and commit per sub-device,
        then "stop loading" and, for effects that must run in sync, an animation restart.
        """
        settings.validate()
        if boot != settings.info.boot:
            raise ValueError(f"{settings.mode} is not a {'boot' if boot else 'regular'} effect")
        devs = self._effect_targets(subdevices)
        meta = metadata(settings)
        if preview:
            meta = bytes((PREVIEW_PRESET,)) + meta[1:]
        slot = SlotType.BOOT if boot else SlotType.REGULAR
        for dev in devs:
            self.select_subdevice(dev.id)
            frames = keyframes(settings, dev.led_count, self.rotation(), self.mirrored(), rng)
            self._send_timestamp_effect(slot, meta, frames, len(devs), preview,
                                        header_only=settings.mode == "instant")
        if not preview:
            self.transact(DeviceType.EFFECT, EffectCmd.STOP_LOADING)
            if settings.info.sync:
                self.transact(DeviceType.EFFECT, EffectCmd.RESTART_ANIMATION, b"\1", check=False)

    def cancel_preview(self, subdevices: Sequence[SubDevice] | None = None) -> None:
        """End a preview and show the stored effects again."""
        devs = self._effect_targets(subdevices)
        blank = EffectSettings("still", brightness=100, colors=[(0, 0, 0)])
        meta = bytes((PREVIEW_PRESET,)) + metadata(blank)[1:]
        frames = keyframes(blank, 0)
        for dev in devs:
            self.select_subdevice(dev.id)
            self._send_timestamp_effect(SlotType.REGULAR, meta, frames, 0, True, header_only=True)

    # Fans.

    def fan_status(self) -> list[PortStatus]:
        """Return duty cycle, speed and state of the motherboard input and each port."""
        state = self.transact(DeviceType.PWM, PwmCmd.GET_STATUS, check=False)
        duty = self.transact(DeviceType.PWM, PwmCmd.GET_DUTY, check=False)
        tacho = self.transact(DeviceType.PWM, PwmCmd.GET_TACHO, check=False)
        result = [PortStatus("mobo", duty[3], None, state[3])]
        result.extend(PortStatus(str(p + 1), duty[4 + p], int.from_bytes(tacho[3 + 2 * p:5 + 2 * p]),
                                 state[4 + p])
                      for p in range(PORT_COUNT))
        return result

    def fan_curve(self, port: int) -> FanCurve:
        """Read the curve of @p port (1-based)."""
        check_port(port)
        data = self.transact(DeviceType.PWM, PwmCmd.GET_MAP, bytes((port - 1,)), check=False)
        count = min(data[4], MAX_CURVE_POINTS)
        return FanCurve(port, data[3], [(data[5 + 2 * i], data[6 + 2 * i]) for i in range(count)])

    def set_fan_curve(self, port: int, profile: int, points: Sequence[tuple[int, int]]) -> None:
        """Store a curve for @p port (1-based) in the hub's memory."""
        check_port(port)
        validate_curve(points)
        if not 0 <= profile <= 0xFF:
            raise ValueError("profile ID must fit in one byte")
        flat = bytes(v for point in points for v in point)
        self.transact(DeviceType.PWM, PwmCmd.SET_MAP, bytes((port - 1, profile, len(points))) + flat)


def open_hub(device: str | None = None, serial: str | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> Hub:
    """Open the hub at @p device, the one with @p serial, or the only one present."""
    if device is not None:
        return Hub(HidrawTransport(device), timeout)
    hubs = find_hubs()
    if serial is not None:
        hubs = [h for h in hubs if h.serial == serial]
    if not hubs:
        raise HubError("no Fractal Adjust Pro Hub found")
    if len(hubs) > 1:
        raise HubError("several hubs found, select one with --device or --serial: "
                       + ", ".join(f"{h.devnode} ({h.serial})" for h in hubs))
    return Hub(HidrawTransport(hubs[0].devnode), timeout)
