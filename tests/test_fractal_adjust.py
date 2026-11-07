"""Tests for fractal_adjust using a fake hub that replays replies captured from real hardware."""
from __future__ import annotations

import random
import sys
import tomllib
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fractal_adjust import cli, config, effects  # noqa: E402
from fractal_adjust import hub as fa  # noqa: E402
from fractal_adjust.effects import DEFAULT_PALETTE, EffectSettings  # noqa: E402

# Replies of a hub with firmware 1.1.5, keyed by the first bytes of the request payload.
CAPTURED: dict[bytes, str] = {
    bytes.fromhex("f101"): "f1 01 00 31 2e 31 2e 35",
    bytes.fromhex("f102"): "f1 02 00 30 30 30 32 35 32 36 46 44 39 30 38 34 32 34 30 30 32 33 35",
    bytes.fromhex("f11c"): "f1 1c 00 00",
    bytes.fromhex("f114"): "f1 14 00 00",
    bytes.fromhex("d101"): "d1 01 00 00 01 00 00 00",
    bytes.fromhex("d104"): "d1 04 00 23 23 23 23 23",
    bytes.fromhex("d105"): "d1 05 00 02 3f 00 00 00 00 00 00",
    bytes.fromhex("d102"): "d1 02 00 01 06 00 00 14 14 28 28 3c 3c 50 50 64 64",
    bytes.fromhex("a41a"): "a4 1a 00 00",
    bytes.fromhex("a41b"): "a4 1b 00 01 01 00 01",
    bytes.fromhex("a414"): "a4 14 00 02 02 01 05",
    bytes.fromhex("a408"): "a4 08 00 03 01 14 00 02 14 00 11 00 01",
    bytes.fromhex("a410"): "a4 10 00 03 46 61 6e",
    bytes.fromhex("a40e"): "a4 0e 00 00",
    bytes.fromhex("a416"): "a4 16 00 00",
    bytes.fromhex("a418"): "a4 18 00 00",
    # Stored effects: regular slot "Northern lights" shift, boot slot white meshify.
    bytes.fromhex("a40501"): "a4 05 00 00 00 00 00 00 00 00 00 00 28 00 74 c5 6e 00 fa 00 fa eb c9 40 bd"
                             " 95 fd e4 00 f0 3c 32",
    bytes.fromhex("a40500"): "a4 05 00 00 00 00 00 00 00 00 08 03 64 ff ff ff 32",
}
NORTHERN_LIGHTS = EffectSettings("shift", speed=40, brightness=100, colors=list(DEFAULT_PALETTE))
WHITE_MESHIFY = EffectSettings("meshify", brightness=100, colors=[(255, 255, 255)])


class FakeTransport:
    """Answers requests from a table; records every report written."""

    def __init__(self, replies: dict[bytes, str], *, stale: list[bytes] | None = None) -> None:
        self.replies = replies
        self.written: list[bytes] = []
        self.pending: deque[bytes] = deque(stale or [])
        self.closed = False

    def write(self, report: bytes) -> None:
        assert len(report) == fa.REPORT_SIZE + 1
        assert report[0] == fa.REPORT_ID
        self.written.append(report)
        payload = report[1:]
        for prefix in sorted(self.replies, key=len, reverse=True):
            if payload.startswith(prefix):
                self.pending.append(bytes((fa.REPORT_ID,)) + bytes.fromhex(self.replies[prefix]))
                return
        # Default: echo header with success status, like the SET commands do.
        self.pending.append(bytes((fa.REPORT_ID, payload[0], payload[1], 0)))

    def read(self, timeout: float) -> bytes | None:
        return self.pending.popleft() if self.pending else None

    def close(self) -> None:
        self.closed = True


def make_hub(replies: dict[bytes, str] | None = None) -> fa.Hub:
    return fa.Hub(FakeTransport(CAPTURED if replies is None else replies), timeout=0.05)


@pytest.fixture
def hub() -> fa.Hub:
    return make_hub()


def payloads(hub: fa.Hub) -> list[bytes]:
    transport = hub._transport
    assert isinstance(transport, FakeTransport)
    return [r[1:].rstrip(b"\0") for r in transport.written]


# Protocol.

def test_identity(hub: fa.Hub) -> None:
    assert hub.firmware_version() == "1.1.5"
    assert hub.serial_number() == "2526FD90842400235"
    assert hub.memory_ok()
    assert not hub.windows_dynamic_lighting_opt_in()
    assert not hub.dynamic_lighting_on()


def test_port_configuration(hub: fa.Hub) -> None:
    assert hub.argb2_compatible_ports() == [True, True, False, True]
    assert hub.port_types() == ["ARGB2", "ARGB2", "ARGB1", "ARGB1_FALLBACK"]


def test_fan_status(hub: fa.Hub) -> None:
    status = hub.fan_status()
    assert status[0] == fa.PortStatus("mobo", 35, None, 0)
    assert status[1] == fa.PortStatus("1", 35, 575, 1)
    assert [s.rpm for s in status[2:]] == [0, 0, 0]


def test_fan_curve(hub: fa.Hub) -> None:
    curve = hub.fan_curve(1)
    assert curve.profile == 1
    assert curve.points == [(0, 0), (20, 20), (40, 40), (60, 60), (80, 80), (100, 100)]


def test_set_fan_curve_packet(hub: fa.Hub) -> None:
    hub.set_fan_curve(3, 7, [(0, 30), (50, 40), (100, 100)])
    assert payloads(hub)[-1] == bytes.fromhex("d103 02 07 03 001e 3228 6464")


def test_subdevices(hub: fa.Hub) -> None:
    devs = hub.subdevices()
    assert [(d.id, d.port, d.led_count, d.argb_version) for d in devs] == [
        (0x01, 1, 20, "ARGB2"), (0x02, 1, 20, "ARGB2"), (0x11, 2, 0, "ARGB1")]
    assert all(d.type_name == "Fan" and d.name == "" for d in devs)
    assert bytes.fromhex("a40a0111") in payloads(hub)


def test_turn_light_and_slot(hub: fa.Hub) -> None:
    hub.turn_light(False, [1, 2, 0x11])
    hub.select_effect_slot(2)
    assert payloads(hub) == [bytes.fromhex("a40c 00 03 010211"), bytes.fromhex("a404 02")]
    with pytest.raises(ValueError):
        hub.select_effect_slot(4)


def test_stale_reports_are_skipped() -> None:
    stale = [bytes.fromhex("02 d1 04 00 63"), bytes.fromhex("01 ff")]
    assert fa.Hub(FakeTransport(CAPTURED, stale=stale), timeout=0.05).firmware_version() == "1.1.5"


def test_error_status_and_timeout() -> None:
    hub = make_hub({bytes.fromhex("a404"): "a4 04 01"})
    with pytest.raises(fa.HubError, match="status 1"):
        hub.select_effect_slot(1)

    class Silent(FakeTransport):
        def write(self, report: bytes) -> None:
            self.written.append(report)

    with pytest.raises(fa.HubError, match="no reply"):
        fa.Hub(Silent({}), timeout=0.05).firmware_version()


def test_find_hubs(tmp_path: Path) -> None:
    for name, hid_id, uniq in (("hidraw3", "0003:000036BC:00001001", "ABC"),
                               ("hidraw4", "0003:0000046D:0000C52B", "X"),
                               ("hidraw5", "0003:000036BC:00001002", "DEF")):
        dev = tmp_path / name / "device"
        dev.mkdir(parents=True)
        (dev / "uevent").write_text(f"DRIVER=hid-generic\nHID_ID={hid_id}\nHID_UNIQ={uniq}\n")
    assert fa.find_hubs(tmp_path) == [fa.HubInfo("/dev/hidraw3", 0x1001, "ABC"),
                                      fa.HubInfo("/dev/hidraw5", 0x1002, "DEF")]


# Effects.

def test_metadata_matches_hardware() -> None:
    assert effects.metadata(NORTHERN_LIGHTS) == bytes.fromhex(CAPTURED[bytes.fromhex("a40501")])[10:]
    assert effects.metadata(WHITE_MESHIFY) == bytes.fromhex(CAPTURED[bytes.fromhex("a40500")])[10:]


@pytest.mark.parametrize("mode", list(effects.MODES))
def test_metadata_round_trip(mode: str) -> None:
    info = effects.MODES[mode]
    settings = EffectSettings(mode, speed=37 if info.has_speed else 50, brightness=80,
                              colors=list(DEFAULT_PALETTE[:info.colors]), up_wave=7, frequency=30)
    decoded = effects.settings_from_metadata(effects.metadata(settings))
    assert decoded is not None
    assert effects.metadata(decoded) == effects.metadata(settings)


def frames_of(data: bytes, leds: int) -> list[tuple[int, bytes]]:
    size = 2 + 3 * leds
    assert len(data) % size == 0
    return [(int.from_bytes(data[i:i + 2]), data[i + 2:i + size]) for i in range(0, len(data), size)]


@pytest.mark.parametrize("mode", [m for m in effects.MODES if m != "instant"])
@pytest.mark.parametrize("leds", [1, 3, 20, 76])
def test_keyframes_shape(mode: str, leds: int) -> None:
    info = effects.MODES[mode]
    settings = EffectSettings(mode, speed=60, colors=list(DEFAULT_PALETTE[:info.colors]))
    frames = frames_of(effects.keyframes(settings, leds, rotation=2, mirror=True, rng=random.Random(1)), leds)
    assert len(frames) >= 2
    if mode != "lava-lamp":
        times = [t for t, _ in frames]
        assert times == sorted(times)


def test_still_brightness_scaling() -> None:
    frames = frames_of(effects.keyframes(EffectSettings("still", brightness=100, colors=[(200, 100, 0)]), 2), 2)
    assert [t for t, _ in frames] == [0, 562, 1124]
    assert all(f == bytes((100, 50, 0)) * 2 for _, f in frames)


def test_breathe_timeline() -> None:
    settings = EffectSettings("breathe", speed=50, brightness=100, colors=[(200, 0, 0), (0, 0, 200)])
    times = [t for t, _ in frames_of(effects.keyframes(settings, 1), 1)]
    assert times == [0, 120, 160, 161, 201, 321, 361, 362, 402]


def test_blink() -> None:
    settings = EffectSettings("blink", brightness=100, colors=[(200, 0, 100)])
    frames = frames_of(effects.keyframes(settings, 3), 3)
    assert [t for t, _ in frames] == [0, 32, 33, 65, 66]
    assert [f for _, f in frames] == [bytes((100, 0, 50)) * 3] * 2 + [bytes(9)] * 2 + [bytes((100, 0, 50)) * 3]
    meta = effects.metadata(settings)
    assert meta == bytes((effects.BLINK_PRESET, effects.MODES["breathe"].id, 92, 200, 0, 100, 0, 0, 0, 50))
    assert effects.settings_from_metadata(meta) == settings
    assert effects.settings_from_metadata(bytes((7,)) + meta[1:]).mode == "breathe"  # type: ignore[union-attr]
    assert effects.settings_from_metadata(bytes((effects.PREVIEW_PRESET,)) + meta[1:]) == settings


def test_instant_has_no_frames() -> None:
    assert effects.keyframes(EffectSettings("instant", colors=[(1, 2, 3)]), 20) == b""


def test_rotation_and_mirror() -> None:
    b = effects._Builder(4, rotation=2, mirror=False)
    frame = bytes(range(12))
    assert b.transform(frame) == frame[9:] + frame[:9]
    b = effects._Builder(4, rotation=0, mirror=True)
    assert b.transform(frame) == frame[0:3] + frame[9:12] + frame[6:9] + frame[3:6]


def test_colors_and_validation() -> None:
    assert effects.parse_color("#0074C5") == (0, 0x74, 0xC5)
    assert effects.format_color((0, 0x74, 0xC5)) == "#0074c5"
    with pytest.raises(ValueError):
        effects.parse_color("#12345")
    assert len(EffectSettings("shift", colors=[(1, 1, 1)]).effective_colors()) == 6
    with pytest.raises(ValueError):
        EffectSettings("still", brightness=101).validate()


def test_upload_sequence(hub: fa.Hub) -> None:
    devs = [fa.SubDevice(0x01, 1, 20, "ARGB2"), fa.SubDevice(0x11, 2, 0, "ARGB1")]
    hub.upload_effect(EffectSettings("breathe", speed=50, colors=[(255, 0, 0), (0, 0, 255)]), subdevices=devs)
    sent = payloads(hub)
    frames = effects.keyframes(EffectSettings("breathe", speed=50, colors=[(255, 0, 0), (0, 0, 255)]), 20)
    assert sent[0] == bytes.fromhex("a40a0101")
    header = sent[3]
    assert header[:2] == bytes.fromhex("a411")
    assert header[2:8] == bytes((1, sum(frames) & 0xFF, len(frames) >> 8, len(frames) & 0xFF, 0, 1))
    transport = hub._transport
    assert isinstance(transport, FakeTransport)
    chunks = [r[1:] for r in transport.written if r[1:3] == bytes.fromhex("a412")]
    assert b"".join(p[3:3 + p[2]] for p in chunks) == frames
    assert all(p[2] <= fa.MAX_CHUNK for p in chunks)
    assert sent[-3:] == [bytes.fromhex("a413"), bytes.fromhex("a478"), bytes.fromhex("a40b01")]
    assert bytes.fromhex("a40a0111") not in sent


def test_preview_does_not_commit(hub: fa.Hub) -> None:
    devs = [fa.SubDevice(0x01, 1, 3, "ARGB2")]
    hub.upload_effect(EffectSettings("still", colors=[(1, 2, 3)]), preview=True, subdevices=devs)
    sent = payloads(hub)
    header = next(p for p in sent if p[:2] == bytes.fromhex("a411"))
    assert header[6] == 1 and header[8] == effects.PREVIEW_PRESET
    assert not any(p[:2] in (bytes.fromhex("a413"), bytes.fromhex("a478")) for p in sent)
    hub.cancel_preview(devs)
    last = payloads(hub)[-1]
    assert last[:2] == bytes.fromhex("a411") and last[7] == 0


def test_boot_mode_mismatch(hub: fa.Hub) -> None:
    with pytest.raises(ValueError):
        hub.upload_effect(EffectSettings("still"), boot=True, subdevices=[])


# Configuration.

def test_default_config_is_valid() -> None:
    cfg = config.defaults()
    assert cfg.light_on
    assert config.dump(cfg) == config.dump(config.parse(tomllib.loads(config.dump(cfg))))
    assert effects.metadata(cfg.effect) == effects.metadata(NORTHERN_LIGHTS)
    assert effects.metadata(cfg.boot) == effects.metadata(WHITE_MESHIFY)
    assert sorted(cfg.fan_curves) == [1, 2, 3, 4]


def test_user_file_overrides_defaults(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text("[lighting]\non = false\n[lighting.effect]\nmode = \"still\"\ncolors = [\"#ff0000\"]\n")
    cfg = config.load(path)
    assert not cfg.light_on
    assert cfg.effect.mode == "still" and cfg.effect.colors == [(255, 0, 0)]
    assert cfg.boot.mode == "meshify"
    assert cfg.fan_curves[1][-1] == (100, 100)


@pytest.mark.parametrize("text", [
    "[lighting]\non = 1\n",
    "[lighting.effect]\nmode = \"meshify\"\n",
    "[lighting.effect]\nmode = \"still\"\nbrightness = 300\n",
    "[lighting.effect]\nmode = \"still\"\ncolors = [\"red\"]\n",
    "[fans]\nport1 = [[0, 0], [50, 50]]\n",
    "[fans]\nport9 = [[0, 0], [100, 100]]\n",
    "[lighting\n",
])
def test_invalid_config(tmp_path: Path, text: str) -> None:
    path = tmp_path / "c.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError):
        config.load(path)


def test_save_and_reload(tmp_path: Path) -> None:
    cfg = config.defaults()
    cfg.light_on = False
    cfg.effect = EffectSettings("wave-ripple", speed=30, brightness=60, colors=[(1, 2, 3), (4, 5, 6)],
                                up_wave=5, frequency=70)
    cfg.fan_curves[2] = [(0, 30), (100, 100)]
    path = config.save(cfg, tmp_path / "sub" / "config.toml")
    assert config.dump(config.load(path)) == config.dump(cfg)
    assert list(path.parent.iterdir()) == [path]


def test_apply_skips_unchanged(hub: fa.Hub) -> None:
    cfg = config.defaults()
    log: list[str] = []
    config.apply(hub, cfg, log.append)
    sent = payloads(hub)
    assert not any(p[:2] in (bytes.fromhex("a411"), bytes.fromhex("d103")) for p in sent)
    assert sent[-1] == bytes.fromhex("a40c 01 03 010211")
    assert "effect: shift already stored" in log


def test_apply_writes_differences(hub: fa.Hub) -> None:
    cfg = config.defaults()
    cfg.light_on = False
    cfg.effect = EffectSettings("still", colors=[(255, 0, 0)])
    cfg.fan_curves = {2: [(0, 40), (100, 100)]}
    config.apply(hub, cfg)
    sent = payloads(hub)
    headers = [p for p in sent if p[:2] == bytes.fromhex("a411")]
    assert len(headers) == 2 and all(h[2] == fa.SlotType.REGULAR for h in headers)
    assert bytes.fromhex("d103 01 01 02 0028 6464") in sent
    assert sent[-1] == bytes.fromhex("a40c 00 03 010211")


# Command line parsing.

@pytest.mark.parametrize("text", [
    "0:0", "0:0,100:101", "0:0,50:50,50:60,100:100", "10:0,100:100", "0:0,90:100", "a:b", "0-0,100:100",
])
def test_parse_curve_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        cli.parse_curve(text)


def test_parse_curve_and_ports() -> None:
    assert cli.parse_curve("0:20,60:50,100:100") == [(0, 20), (60, 50), (100, 100)]
    assert cli.parse_ports("all") == [1, 2, 3, 4]
    assert cli.parse_ports("2,4") == [2, 4]
    with pytest.raises(ValueError):
        cli.parse_ports("0")


def test_default_command_is_gui() -> None:
    args = cli.build_parser().parse_args([])
    assert args.func is cli.cmd_gui


@pytest.mark.parametrize("argv", [["--minimized"], ["gui", "--minimized"], ["--minimized", "gui"]])
def test_minimized_option(argv: list[str]) -> None:
    args = cli.build_parser().parse_args(argv)
    assert args.func is cli.cmd_gui and args.minimized and not args.no_apply


def test_gui_option_defaults() -> None:
    args = cli.build_parser().parse_args(["gui"])
    assert not args.minimized and not args.no_apply
    assert cli.build_parser().parse_args(["--no-apply", "gui"]).no_apply


# MQTT requests (JSON) and timed effects.

from fractal_adjust import remote, service as svc  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402


def make_service() -> tuple[svc.HubService, fa.Hub]:
    hub = make_hub()
    return svc.HubService(lambda: hub), hub


def headers(hub: fa.Hub) -> list[bytes]:
    return [p for p in payloads(hub) if p[:2] == bytes.fromhex("a411")]


def request(service: svc.HubService, obj: object) -> Any:
    payload = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return json.loads(remote.execute(service, payload).result(2))


@pytest.fixture
def service() -> Iterator[svc.HubService]:
    s, _ = make_service()
    yield s
    s.shutdown()


@pytest.mark.parametrize("payload, message", [
    (b"status", "not valid JSON"),
    (b"\xff", "not UTF-8"),
    (b"[1, 2]", "JSON object"),
    (b"{}", "missing \"command\""),
    (b'{"command": "raw"}', "unknown command"),
    (b'{"command": "gui"}', "unknown command"),
    (b'{"command": "status", "watch": 1}', "unknown parameters: watch"),
    (b'{"command": "effect", "mode": "sparkle"}', "unknown mode"),
    (b'{"command": "effect"}', "missing parameter 'mode'"),
    (b'{"command": "effect", "mode": "blink", "timeout": 0}', "positive number"),
    (b'{"command": "effect", "mode": "blink", "colors": ["red"]}', "colors"),
    (b'{"command": "effect", "mode": "still", "brightness": 300}', "brightness"),
    (b'{"command": "effect", "mode": "meshify", "timeout": 5}', "boot effects"),
    (b'{"command": "light", "on": "yes"}', "true or false"),
    (b'{"command": "light"}', "missing parameter 'on'"),
    (b'{"command": "slot", "slot": 7}', "between 0 and 3"),
    (b'{"command": "set-curve", "ports": [1], "points": [[0, 0], [50, 50]]}', "x=100"),
    (b'{"command": "set-curve", "ports": [9], "points": [[0, 0], [100, 100]]}', "ports"),
    (b'{"command": "curve", "ports": [1], "id": [1]}', "id must be"),
])
def test_rejected_requests(service: svc.HubService, payload: bytes, message: str) -> None:
    reply = request(service, payload)
    assert reply["ok"] is False and message in reply["error"]
    assert "result" not in reply


def test_reply_structure(service: svc.HubService) -> None:
    reply = request(service, {"id": "abc", "command": "status"})
    assert reply["id"] == "abc" and reply["command"] == "status" and reply["ok"] is True
    assert reply["result"][0] == {"port": "mobo", "duty": 35, "rpm": None, "state": 0}
    assert reply["result"][1]["rpm"] == 575
    assert request(service, {"command": "info"})["id"] is None
    assert request(service, {"id": 7, "command": "curve", "ports": [1]})["result"] == [
        {"port": 1, "profile": 1, "points": [[0, 0], [20, 20], [40, 40], [60, 60], [80, 80], [100, 100]]}]
    devices = request(service, {"command": "devices"})["result"]
    assert devices[2] == {"id": 0x11, "port": 2, "led_count": 0, "argb_version": "ARGB1", "type_name": "Fan",
                          "name": "", "id_hex": "0x11"}
    effects_reply = request(service, {"command": "effects"})["result"]
    assert effects_reply[0]["effect"]["mode"] == "shift" and effects_reply[0]["boot"]["mode"] == "meshify"
    assert effects_reply[0]["temporary"] is False


def test_commands_reach_the_hub() -> None:
    service, hub = make_service()
    try:
        assert request(service, {"command": "light", "on": False})["result"] == {"on": False, "ids": [1, 2, 0x11]}
        assert payloads(hub)[-1] == bytes.fromhex("a40c 00 03 010211")
        assert request(service, {"command": "light", "on": True, "ids": ["0x02", 1]})["result"]["ids"] == [2, 1]
        assert request(service, {"command": "slot", "slot": 2})["ok"]
        reply = request(service, {"command": "set-curve", "ports": [3], "points": [[0, 30], [100, 100]], "profile": 4})
        assert reply["ok"] and bytes.fromhex("d103 02 04 02 001e 6464") in payloads(hub)
        reply = request(service, {"command": "effect", "mode": "breathe", "colors": ["#ff0000", "#0000ff"],
                                  "speed": 30, "ids": [1]})
        assert reply["result"] == {"effect": {"mode": "breathe", "speed": 30, "brightness": 100,
                                              "colors": ["#ff0000", "#0000ff"]},
                                   "boot": False, "stored": True, "timeout": None, "ids": [1]}
        assert bytes.fromhex("a413") in payloads(hub)
    finally:
        service.shutdown()


def test_request_never_writes_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    service, _ = make_service()
    try:
        for obj in ({"command": "effect", "mode": "still", "colors": ["#ff0000"]}, {"command": "light", "on": False},
                    {"command": "set-curve", "ports": [2], "points": [[0, 10], [100, 100]]}, {"command": "apply"}):
            assert request(service, obj)["ok"]
    finally:
        service.shutdown()
    assert list(tmp_path.iterdir()) == []


def test_timed_effect_returns_to_previous() -> None:
    service, hub = make_service()
    try:
        first = request(service, {"command": "effect", "mode": "still", "colors": ["#ff0000"], "timeout": 0.4})
        assert first["result"]["stored"] is False and first["result"]["timeout"] == 0.4
        assert request(service, {"command": "effect", "mode": "blink", "colors": ["#00ff00"], "timeout": 0.1})["ok"]
        assert service.submit_plain(lambda: service.temporary_count).result(2) == 2
        stacked = len(headers(hub))
        time.sleep(0.25)
        # The blink expired: preview cancelled (count 0) and the red still shown again.
        after = headers(hub)[stacked:]
        assert [h[7] for h in after[:1]] == [0]
        assert all(h[6] == 1 and h[8] == effects.PREVIEW_PRESET for h in after)
        assert any(h[9] == effects.MODES["still"].id for h in after[1:])
        assert service.submit_plain(lambda: service.temporary_count).result(2) == 1
        time.sleep(0.35)
        assert service.submit_plain(lambda: service.temporary_count).result(2) == 0
        assert headers(hub)[-1][7] == 0
        assert not any(p[:2] == bytes.fromhex("a413") for p in payloads(hub))
    finally:
        service.shutdown()


def test_shutdown_cancels_timed_effects() -> None:
    service, hub = make_service()
    request(service, {"command": "effect", "mode": "still", "colors": ["#ff0000"], "timeout": 60})
    service.shutdown()
    assert headers(hub)[-1][7] == 0
    assert isinstance(hub._transport, FakeTransport) and hub._transport.closed


def test_missing_hub_is_reported() -> None:
    def no_hub() -> fa.Hub:
        raise fa.HubError("no Fractal Adjust Pro Hub found")
    service = svc.HubService(no_hub)
    try:
        reply = json.loads(remote.execute(service, b'{"id": 1, "command": "status"}').result(2))
        assert reply == {"id": 1, "command": "status", "ok": False, "error": "no Fractal Adjust Pro Hub found"}
    finally:
        service.shutdown()


def test_mqtt_config(tmp_path: Path) -> None:
    cfg = config.defaults()
    assert not cfg.mqtt.enabled and cfg.mqtt.topic == "fractal_adjust"
    assert cfg.mqtt.effective_reply_topic == "fractal_adjust/reply"
    path = tmp_path / "c.toml"
    path.write_text('[mqtt]\nenabled = true\nhost = "broker"\nport = 8883\ntls = true\npassword = "p\\"w"\n')
    loaded = config.load(path)
    assert loaded.mqtt.enabled and loaded.mqtt.port == 8883 and loaded.mqtt.password == 'p"w'
    assert config.dump(config.load(config.save(loaded, tmp_path / "out.toml"))) == config.dump(loaded)
    assert (tmp_path / "out.toml").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("text", [
    "[mqtt]\nport = 0\n", "[mqtt]\ntopic = \"a/#\"\n", "[mqtt]\nhots = \"x\"\n", "[mqtt]\nenabled = \"yes\"\n",
])
def test_invalid_mqtt_config(tmp_path: Path, text: str) -> None:
    path = tmp_path / "c.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError):
        config.load(path)
