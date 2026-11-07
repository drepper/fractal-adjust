"""GTK 4 / libadwaita interface.

All hub I/O runs on one worker thread so the window never blocks; results come back to the
GTK main loop through GLib.idle_add.  Changes made here take effect on the hub when their
"Store" button is pressed (the LED switch acts immediately); "Save" writes the settings
shown in the window to the configuration file, which is applied again on the next start.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import math
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

from . import __version__, config, remote  # noqa: E402
from .effects import BOOT_MODES, MODES, REGULAR_MODES, EffectSettings, Rgb  # noqa: E402
from .hub import MAX_CURVE_POINTS, PORT_COUNT, Hub, HubError, PortStatus, SubDevice, open_hub  # noqa: E402
from .mqtt import MqttBridge, MqttError  # noqa: E402
from .service import HubService  # noqa: E402

APP_ID = "org.akkadia.FractalAdjust"
STATUS_INTERVAL = 2
PREVIEW_DELAY_MS = 300
MAX_COLORS = 6


def _to_rgba(rgb: Rgb) -> Gdk.RGBA:
    """Convert an RGB triple to Gdk.RGBA."""
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue, rgba.alpha = rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, 1.0
    return rgba


def _from_rgba(rgba: Gdk.RGBA) -> Rgb:
    """Convert Gdk.RGBA to an RGB triple."""
    return (round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255))


class Worker:
    """GTK-side front of the HubService: runs jobs on the hub thread, results in the main loop."""

    def __init__(self, service: HubService, on_error: Callable[[str], None]) -> None:
        """Report failures through @p on_error, called in the main loop."""
        self.service = service
        self.pending = 0
        self._on_error = on_error

    @property
    def hub(self) -> Hub | None:
        """The open hub, if any."""
        return self.service.hub

    def submit(self, what: str, fn: Callable[[], Any], done: Callable[[Any], None] | None = None) -> None:
        """Run @p fn on the hub thread; pass its result to @p done in the main loop."""
        self.pending += 1
        future = self.service.submit_plain(fn)
        future.add_done_callback(lambda f: GLib.idle_add(self._finish, f, what, done))

    def submit_hub(self, what: str, fn: Callable[[Hub], Any], done: Callable[[Any], None] | None = None) -> None:
        """Like submit(), for operations that need an already connected hub."""
        def job() -> Any:
            if self.service.hub is None:
                raise HubError("no hub connected")
            return fn(self.service.hub)
        self.submit(what, job, done)

    def _finish(self, future: concurrent.futures.Future[Any], what: str,
                done: Callable[[Any], None] | None) -> bool:
        """Main-loop side of a finished job."""
        self.pending -= 1
        try:
            result = future.result()
        except (HubError, ValueError, OSError) as e:
            if isinstance(e, HubError) and "failed:" in str(e):
                self.service.submit_plain(self.service.drop)
            self._on_error(f"{what}: {e}")
        else:
            if done is not None:
                done(result)
        return GLib.SOURCE_REMOVE

    def shutdown(self, final: Callable[[Hub], None] | None = None) -> None:
        """Run @p final, close the hub and stop the thread."""
        self.service.shutdown(final)


class CurveEditor(Gtk.DrawingArea):
    """Interactive fan curve: drag points, double-click to add, right-click to remove."""

    MARGIN_LEFT, MARGIN_RIGHT, MARGIN_TOP, MARGIN_BOTTOM = 36, 14, 10, 24
    HANDLE = 6
    GRAB = 14

    def __init__(self, on_change: Callable[[], None]) -> None:
        """Call @p on_change after every user edit."""
        super().__init__()
        self.points: list[tuple[int, int]] = [(0, 0), (100, 100)]
        self.marker: int | None = None
        self._on_change = on_change
        self._drag: int | None = None
        self._origin = (0.0, 0.0)
        self.set_content_height(180)
        self.set_hexpand(True)
        self.set_tooltip_text("Drag points to shape the curve. Double-click adds a point, "
                              "right-click removes one.")
        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)
        click = Gtk.GestureClick()
        click.connect("pressed", self._double_click)
        self.add_controller(click)
        right = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right.connect("pressed", self._right_click)
        self.add_controller(right)

    def set_points(self, points: list[tuple[int, int]]) -> None:
        """Replace the curve without notifying."""
        self.points = list(points)
        self.queue_draw()

    def set_marker(self, duty: int | None) -> None:
        """Show the current motherboard duty as a vertical line."""
        self.marker = duty
        self.queue_draw()

    def _plot(self) -> tuple[float, float, float, float]:
        """Plot rectangle: x0, y0, width, height."""
        w, h = self.get_width(), self.get_height()
        return (self.MARGIN_LEFT, self.MARGIN_TOP, max(1, w - self.MARGIN_LEFT - self.MARGIN_RIGHT),
                max(1, h - self.MARGIN_TOP - self.MARGIN_BOTTOM))

    def _to_screen(self, x: float, y: float) -> tuple[float, float]:
        x0, y0, pw, ph = self._plot()
        return x0 + x / 100 * pw, y0 + (1 - y / 100) * ph

    def _from_screen(self, sx: float, sy: float) -> tuple[int, int]:
        x0, y0, pw, ph = self._plot()
        x = round((sx - x0) / pw * 100)
        y = round((1 - (sy - y0) / ph) * 100)
        return min(100, max(0, x)), min(100, max(0, y))

    def _nearest(self, sx: float, sy: float) -> int | None:
        best, best_d = None, float(self.GRAB)
        for i, (x, y) in enumerate(self.points):
            px, py = self._to_screen(x, y)
            if (d := math.hypot(px - sx, py - sy)) <= best_d:
                best, best_d = i, d
        return best

    def _draw(self, _area: Gtk.DrawingArea, cr: Any, _w: int, _h: int) -> None:
        fg = self.get_color()
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        x0, y0, pw, ph = self._plot()
        cr.set_line_width(1)
        cr.set_font_size(10)
        for v in range(0, 101, 20):
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
            sx, sy = self._to_screen(v, v)
            cr.move_to(sx, y0)
            cr.line_to(sx, y0 + ph)
            cr.move_to(x0, sy)
            cr.line_to(x0 + pw, sy)
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
            cr.move_to(sx - 8, y0 + ph + 14)
            cr.show_text(f"{v}")
            cr.move_to(4, sy + 4)
            cr.show_text(f"{v}%")
        if self.marker is not None:
            sx, _ = self._to_screen(self.marker, 0)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.5)
            cr.set_dash([4, 3])
            cr.move_to(sx, y0)
            cr.line_to(sx, y0 + ph)
            cr.stroke()
            cr.set_dash([])
        cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
        cr.set_line_width(2)
        for i, (x, y) in enumerate(self.points):
            sx, sy = self._to_screen(x, y)
            if i == 0:
                cr.move_to(sx, sy)
            else:
                cr.line_to(sx, sy)
        cr.stroke()
        for x, y in self.points:
            sx, sy = self._to_screen(x, y)
            cr.arc(sx, sy, self.HANDLE, 0, 2 * math.pi)
            cr.fill()

    def _drag_begin(self, _g: Gtk.GestureDrag, sx: float, sy: float) -> None:
        self._drag = self._nearest(sx, sy)
        self._origin = (sx, sy)

    def _drag_update(self, _g: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._drag is None:
            return
        i = self._drag
        x, y = self._from_screen(self._origin[0] + dx, self._origin[1] + dy)
        if i == 0:
            x = 0
        elif i == len(self.points) - 1:
            x = 100
        else:
            x = min(max(x, self.points[i - 1][0] + 1), self.points[i + 1][0] - 1)
        self.points[i] = (x, y)
        self.queue_draw()

    def _drag_end(self, _g: Gtk.GestureDrag, _dx: float, _dy: float) -> None:
        if self._drag is not None:
            self._drag = None
            self._on_change()

    def _double_click(self, _g: Gtk.GestureClick, n_press: int, sx: float, sy: float) -> None:
        if n_press != 2 or len(self.points) >= MAX_CURVE_POINTS or self._nearest(sx, sy) is not None:
            return
        x, y = self._from_screen(sx, sy)
        if 0 < x < 100 and all(px != x for px, _ in self.points):
            self.points = sorted([*self.points, (x, y)])
            self.queue_draw()
            self._on_change()

    def _right_click(self, _g: Gtk.GestureClick, _n: int, sx: float, sy: float) -> None:
        i = self._nearest(sx, sy)
        if i is not None and 0 < i < len(self.points) - 1:
            del self.points[i]
            self.queue_draw()
            self._on_change()


class ScaleRow(Adw.ActionRow):
    """A preferences row with a 0-100 slider."""

    def __init__(self, title: str, on_change: Callable[[], None]) -> None:
        super().__init__(title=title)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale.set_draw_value(True)
        self.scale.set_value_pos(Gtk.PositionType.LEFT)
        self.scale.set_hexpand(True)
        self.scale.set_size_request(220, -1)
        self.scale.connect("value-changed", lambda _s: on_change())
        self.add_suffix(self.scale)

    @property
    def value(self) -> int:
        return round(self.scale.get_value())

    @value.setter
    def value(self, v: int) -> None:
        self.scale.set_value(v)


class EffectEditor:
    """Rows that edit one EffectSettings, added to a preferences group."""

    def __init__(self, group: Adw.PreferencesGroup, modes: tuple[str, ...],
                 on_change: Callable[[], None]) -> None:
        """Populate @p group with controls for @p modes; @p on_change fires on user edits."""
        self._modes = modes
        self._on_change = on_change
        self._quiet = False
        self.mode_row = Adw.ComboRow(title="Effect", model=Gtk.StringList.new([MODES[m].label for m in modes]))
        self.mode_row.connect("notify::selected", lambda *_: self._changed(mode=True))
        group.add(self.mode_row)
        self.speed_row = ScaleRow("Speed", self._changed)
        group.add(self.speed_row)
        self.brightness_row = ScaleRow("Brightness", self._changed)
        group.add(self.brightness_row)
        self.colors_row = Adw.ActionRow(title="Colors")
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.color_buttons: list[Gtk.ColorDialogButton] = []
        for _ in range(MAX_COLORS):
            button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False))
            button.connect("notify::rgba", lambda *_: self._changed())
            box.append(button)
            self.color_buttons.append(button)
        self.colors_row.add_suffix(box)
        group.add(self.colors_row)
        self.wave_rows: dict[str, Adw.SpinRow] = {}
        for key, title, hi in (("up_wave", "Leading edge (LEDs)", 50), ("top_wave", "Crest (LEDs)", 50),
                               ("down_wave", "Trailing edge (LEDs)", 50), ("frequency", "Frequency", 100)):
            row = Adw.SpinRow.new_with_range(0, hi, 1)
            row.set_title(title)
            row.connect("notify::value", lambda *_: self._changed())
            group.add(row)
            self.wave_rows[key] = row
        self._update_visibility()

    @property
    def mode(self) -> str:
        return self._modes[self.mode_row.get_selected()]

    def _update_visibility(self) -> None:
        info = MODES[self.mode]
        self.speed_row.set_visible(info.has_speed)
        self.colors_row.set_visible(info.colors > 0)
        for i, button in enumerate(self.color_buttons):
            button.set_visible(i < info.colors)
        for row in self.wave_rows.values():
            row.set_visible(info.name == "wave-ripple")

    def _changed(self, mode: bool = False) -> None:
        if mode:
            self._update_visibility()
        if not self._quiet:
            self._on_change()

    def get(self) -> EffectSettings:
        """The settings currently shown."""
        settings = EffectSettings(self.mode, speed=self.speed_row.value, brightness=self.brightness_row.value,
                                  colors=[_from_rgba(b.get_rgba()) for b in self.color_buttons])
        settings.colors = settings.effective_colors() if settings.info.colors else []
        for key, row in self.wave_rows.items():
            setattr(settings, key, round(row.get_value()))
        return settings

    def set(self, settings: EffectSettings) -> None:
        """Show @p settings without reporting a change."""
        self._quiet = True
        try:
            self.mode_row.set_selected(self._modes.index(settings.mode))
            self.speed_row.value = settings.speed
            self.brightness_row.value = settings.brightness
            colors = settings.effective_colors() if settings.info.colors else []
            current = [_from_rgba(b.get_rgba()) for b in self.color_buttons]
            for i, button in enumerate(self.color_buttons):
                button.set_rgba(_to_rgba(colors[i] if i < len(colors) else current[i]))
            for key, row in self.wave_rows.items():
                row.set_value(getattr(settings, key))
            self._update_visibility()
        finally:
            self._quiet = False


class MainWindow(Adw.ApplicationWindow):
    """The application window."""

    def __init__(self, app: Adw.Application, args: argparse.Namespace) -> None:
        super().__init__(application=app, title="Fractal Adjust", default_width=760, default_height=720)
        self._args = args
        self._config_path: Path | None = args.config
        self._worker = Worker(HubService(lambda: open_hub(args.device, args.serial, args.timeout)), self._error)
        self._bridge: MqttBridge | None = None
        self._preview_timer = 0
        self._loading = False
        self._close_confirmed = False
        self._subdevices: list[SubDevice] = []
        self._status_timer = 0

        try:
            fresh = self._config_path is not None and not self._config_path.exists()
            self._saved = config.defaults() if fresh else config.load(self._config_path)
            load_error = None
        except config.ConfigError as e:
            self._saved = config.defaults()
            load_error = str(e)

        self._toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self._stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        self._save_button = Gtk.Button(label="Save", css_classes=["suggested-action"],
                                       tooltip_text=f"Write these settings to {self._target_path()}")
        self._save_button.connect("clicked", lambda _b: self._save())
        header.pack_end(self._save_button)
        revert = Gtk.Button(icon_name="document-revert-symbolic", tooltip_text="Revert to the saved settings")
        revert.connect("clicked", lambda _b: self._revert())
        header.pack_start(revert)
        view.add_top_bar(header)
        self._banner = Adw.Banner(title="Looking for the hub…", button_label="Retry")
        self._banner.connect("button-clicked", lambda _b: self._connect())
        view.add_top_bar(self._banner)
        view.set_content(self._stack)
        self._toasts.set_child(view)
        self.set_content(self._toasts)

        self._build_lighting_page()
        self._build_fans_page()
        self._build_device_page()
        self._show_config(self._saved)
        self.connect("close-request", self._close_request)
        if load_error is not None:
            self._error(f"{load_error}; using the defaults")
        self._connect()

    # Pages.

    def _build_lighting_page(self) -> None:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="LEDs")
        self._light_row = Adw.SwitchRow(title="LEDs on", subtitle="Switches all lighting on the hub")
        self._light_row.connect("notify::active", lambda *_: self._light_toggled())
        group.add(self._light_row)
        page.add(group)

        group = Adw.PreferencesGroup(title="Effect", description="Shown while the system runs")
        suffix = Gtk.Box(spacing=6)
        self._preview_button = Gtk.ToggleButton(label="Live preview", valign=Gtk.Align.CENTER,
                                                tooltip_text="Show changes on the hub without storing them")
        self._preview_button.connect("toggled", lambda _b: self._preview_toggled())
        suffix.append(self._preview_button)
        store = Gtk.Button(label="Store on hub", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        store.connect("clicked", lambda _b: self._store_effect(boot=False))
        suffix.append(store)
        group.set_header_suffix(suffix)
        self._effect = EffectEditor(group, REGULAR_MODES, self._effect_changed)
        page.add(group)

        group = Adw.PreferencesGroup(title="Boot effect", description="Shown at power-up")
        store = Gtk.Button(label="Store on hub", valign=Gtk.Align.CENTER)
        store.connect("clicked", lambda _b: self._store_effect(boot=True))
        group.set_header_suffix(store)
        self._boot = EffectEditor(group, BOOT_MODES, self._dirty_changed)
        page.add(group)
        self._stack.add_titled_with_icon(page, "lighting", "Lighting", "weather-clear-symbolic")

    def _build_fans_page(self) -> None:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Fan curves",
                                     description="Fan duty as a function of the motherboard's PWM duty")
        store = Gtk.Button(label="Store on hub", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        store.connect("clicked", lambda _b: self._store_curves())
        group.set_header_suffix(store)
        self._mobo_row = Adw.ActionRow(title="Motherboard PWM input", subtitle="–")
        group.add(self._mobo_row)
        page.add(group)
        self._curves: dict[int, CurveEditor] = {}
        self._port_groups: dict[int, Adw.PreferencesGroup] = {}
        for port in range(1, PORT_COUNT + 1):
            g = Adw.PreferencesGroup(title=f"Port {port}", description="–")
            linear = Gtk.Button(label="Linear", valign=Gtk.Align.CENTER, css_classes=["flat"])
            linear.connect("clicked", lambda _b, p=port: self._reset_curve(p))
            g.set_header_suffix(linear)
            editor = CurveEditor(self._dirty_changed)
            frame = Gtk.Frame(child=editor, margin_top=6)
            g.add(frame)
            page.add(g)
            self._curves[port] = editor
            self._port_groups[port] = g
        self._stack.add_titled_with_icon(page, "fans", "Fans", "weather-windy-symbolic")

    def _build_device_page(self) -> None:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Hub")
        self._info_rows: dict[str, Adw.ActionRow] = {}
        for key, title in (("device", "Device"), ("firmware", "Firmware"), ("serial", "Serial number")):
            row = Adw.ActionRow(title=title, subtitle="–", subtitle_selectable=True)
            row.add_css_class("property")
            group.add(row)
            self._info_rows[key] = row
        page.add(group)
        self._device_group = Adw.PreferencesGroup(title="Connected devices")
        page.add(self._device_group)
        self._device_rows: list[Adw.ActionRow] = []
        group = Adw.PreferencesGroup(title="Startup")
        self._minimized_row = Adw.SwitchRow(title="Start minimized",
                                            subtitle="Takes effect at the next start; --minimized and "
                                                     "--no-minimized override it")
        self._minimized_row.connect("notify::active", lambda *_: self._dirty_changed())
        group.add(self._minimized_row)
        page.add(group)
        group = Adw.PreferencesGroup(title="Remote control")
        self._mqtt_row = Adw.ActionRow(title="MQTT", subtitle="disabled in the configuration file",
                                       subtitle_selectable=True)
        self._mqtt_row.add_css_class("property")
        group.add(self._mqtt_row)
        page.add(group)
        about = Adw.PreferencesGroup()
        about_row = Adw.ActionRow(title="fractal-adjust", subtitle=f"Version {__version__}")
        about_row.add_prefix(Gtk.Image(icon_name=APP_ID, pixel_size=48))
        about.add(about_row)
        page.add(about)
        self._stack.add_titled_with_icon(page, "device", "Device", "computer-symbolic")

    # Hub connection.

    def _connect(self) -> None:
        self._banner.set_title("Looking for the hub…")
        self._banner.set_revealed(True)
        apply_cfg = None if self._args.no_apply or self._worker.hub is not None else copy.deepcopy(self._saved)

        def job() -> tuple[list[str], dict[str, str], list[SubDevice]]:
            hub = self._worker.service.open()
            log: list[str] = []
            if apply_cfg is not None:
                config.apply(hub, apply_cfg, log.append)
            info = {"firmware": hub.firmware_version(), "serial": hub.serial_number()}
            return log, info, hub.subdevices()

        def done(result: tuple[list[str], dict[str, str], list[SubDevice]]) -> None:
            log, info, devs = result
            self._banner.set_revealed(False)
            if apply_cfg is not None:
                changed = [line for line in log if "already" not in line and not line.startswith("LEDs")]
                self._toast("Settings applied" + (": " + "; ".join(changed) if changed else ""))
            self._info_rows["firmware"].set_subtitle(info["firmware"])
            self._info_rows["serial"].set_subtitle(info["serial"])
            self._show_subdevices(devs)
            if self._status_timer == 0:
                self._status_timer = GLib.timeout_add_seconds(STATUS_INTERVAL, self._poll_status)
            self._poll_status()
            self._start_mqtt()

        self._worker.submit("connect", job, done)

    def _show_subdevices(self, devs: list[SubDevice]) -> None:
        self._subdevices = devs
        for row in self._device_rows:
            self._device_group.remove(row)
        self._device_rows = []
        hub = self._worker.hub
        if hub is not None:
            self._info_rows["device"].set_subtitle(self._args.device or "auto-detected")
        for d in devs:
            row = Adw.ActionRow(title=f"Port {d.port} · {d.type_name or 'Device'}" + (f" ({d.name})" if d.name else ""),
                                subtitle=f"ID {d.id:#04x} · {d.led_count} LEDs · {d.argb_version}"
                                         + ("" if d.led_count else " · no effects"))
            self._device_group.add(row)
            self._device_rows.append(row)

    def _start_mqtt(self) -> None:
        """Serve MQTT requests while the window is open, if the configuration enables it."""
        settings = self._saved.mqtt
        if self._bridge is not None or not settings.enabled:
            return
        def handle(payload: bytes) -> concurrent.futures.Future[str]:
            return remote.execute(self._worker.service, payload, self._config_path)

        def state(text: str) -> None:
            GLib.idle_add(lambda: (self._mqtt_row.set_subtitle(text), GLib.SOURCE_REMOVE)[1])
        try:
            self._bridge = MqttBridge(settings, handle, state)
            self._bridge.start()
        except MqttError as e:
            self._bridge = None
            self._mqtt_row.set_subtitle(str(e))
            self._error(f"MQTT: {e}")

    def _poll_status(self) -> bool:
        if self._worker.hub is not None and self._worker.pending == 0:
            self._worker.submit_hub("status", lambda hub: hub.fan_status(), self._show_status)
        return GLib.SOURCE_CONTINUE

    def _show_status(self, status: list[PortStatus]) -> None:
        mobo = status[0]
        self._mobo_row.set_subtitle(f"{mobo.duty} % duty" + ("" if mobo.state == 0 else " (not connected)"))
        for s in status[1:]:
            port = int(s.port)
            self._port_groups[port].set_description(f"{s.rpm} RPM at {s.duty} % duty")
            self._curves[port].set_marker(mobo.duty)

    # Settings <-> widgets.

    def _current(self) -> config.Config:
        """The configuration the window currently shows."""
        return config.Config(light_on=self._light_row.get_active(), effect=self._effect.get(),
                             boot=self._boot.get(),
                             fan_curves={p: list(e.points) for p, e in self._curves.items()},
                             mqtt=copy.copy(self._saved.mqtt),
                             gui=config.GuiSettings(minimized=self._minimized_row.get_active()))

    @property
    def start_minimized(self) -> bool:
        """The [gui] minimized setting of the configuration file the window was started with."""
        return self._saved.gui.minimized

    def _show_config(self, cfg: config.Config) -> None:
        self._loading = True
        try:
            self._light_row.set_active(cfg.light_on)
            self._effect.set(cfg.effect)
            self._boot.set(cfg.boot)
            self._minimized_row.set_active(cfg.gui.minimized)
            for port, editor in self._curves.items():
                editor.set_points(cfg.fan_curves.get(port, [(0, 0), (100, 100)]))
        finally:
            self._loading = False
        self._dirty_changed()

    def _comparable(self, cfg: config.Config) -> str:
        """Normalized form for change detection: what Save would write."""
        return config.dump(cfg)

    def _dirty_changed(self) -> None:
        dirty = self._comparable(self._current()) != self._comparable(self._saved)
        self._save_button.set_sensitive(dirty)
        self.set_title("Fractal Adjust" + (" (unsaved)" if dirty else ""))

    def _target_path(self) -> Path:
        return self._config_path if self._config_path is not None else config.user_config_path()

    def _save(self) -> bool:
        cfg = self._current()
        try:
            path = config.save(cfg, self._target_path())
        except (OSError, config.ConfigError) as e:
            self._error(f"saving failed: {e}")
            return False
        self._saved = copy.deepcopy(cfg)
        self._dirty_changed()
        self._toast(f"Saved to {path}")
        return True

    def _revert(self) -> None:
        if self._preview_button.get_active():
            self._preview_button.set_active(False)
        self._show_config(self._saved)

    # Actions.

    def _light_toggled(self) -> None:
        self._dirty_changed()
        if self._loading or self._worker.hub is None:
            return
        on = self._light_row.get_active()
        self._worker.submit_hub("switching LEDs",
                                lambda hub: hub.turn_light(on, [d.id for d in hub.subdevices(details=False)]))

    def _effect_changed(self) -> None:
        self._dirty_changed()
        if self._preview_button.get_active() and not self._loading:
            if self._preview_timer:
                GLib.source_remove(self._preview_timer)
            self._preview_timer = GLib.timeout_add(PREVIEW_DELAY_MS, self._send_preview)

    def _send_preview(self) -> bool:
        self._preview_timer = 0
        settings = self._effect.get()
        self._worker.submit_hub("preview", lambda hub: hub.upload_effect(settings, preview=True))
        return GLib.SOURCE_REMOVE

    def _preview_toggled(self) -> None:
        if self._preview_button.get_active():
            self._send_preview()
        else:
            if self._preview_timer:
                GLib.source_remove(self._preview_timer)
                self._preview_timer = 0
            self._worker.submit_hub("ending preview", lambda hub: hub.cancel_preview())

    def _store_effect(self, boot: bool) -> None:
        settings = (self._boot if boot else self._effect).get()
        if not boot and self._preview_button.get_active():
            self._preview_button.set_active(False)
        what = "boot effect" if boot else "effect"
        def job(hub: Hub) -> None:
            hub.upload_effect(settings, boot=boot)
            self._worker.service.stored_effect_changed(hub)
        self._worker.submit_hub(f"storing the {what}", job,
                                lambda _r: self._toast(f"{what.capitalize()} stored on the hub"))

    def _store_curves(self) -> None:
        curves = {p: list(e.points) for p, e in self._curves.items()}

        def job(hub: Hub) -> None:
            for port, points in curves.items():
                current = hub.fan_curve(port)
                if current.points != points:
                    hub.set_fan_curve(port, current.profile, points)
        self._worker.submit_hub("storing fan curves", job, lambda _r: self._toast("Fan curves stored on the hub"))

    def _reset_curve(self, port: int) -> None:
        self._curves[port].set_points([(x, x) for x in range(0, 101, 20)])
        self._dirty_changed()

    # Messages and closing.

    def _toast(self, text: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def _error(self, text: str) -> None:
        if text.startswith("connect: "):
            self._banner.set_title(text.removeprefix("connect: "))
            self._banner.set_revealed(True)
        elif self._worker.hub is None and not self._banner.get_revealed():
            self._banner.set_title(f"Hub not available ({text})")
            self._banner.set_revealed(True)
        else:
            self._toast(text)

    def _close_request(self, _w: Gtk.Window) -> bool:
        if self._close_confirmed or not self._save_button.get_sensitive():
            self._shutdown()
            return False
        dialog = Adw.AlertDialog(heading="Save changes?",
                                 body="The settings differ from the configuration file.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("discard", "Discard")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def response(_d: Adw.AlertDialog, answer: str) -> None:
            if answer == "cancel" or answer == "save" and not self._save():
                return
            self._close_confirmed = True
            self.close()
        dialog.connect("response", response)
        dialog.present(self)
        return True

    def _shutdown(self) -> None:
        if self._status_timer:
            GLib.source_remove(self._status_timer)
            self._status_timer = 0
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None
        previewing = self._preview_button.get_active()
        self._worker.shutdown((lambda hub: hub.cancel_preview()) if previewing else None)


def _install_icon() -> None:
    """Make the application icon available under the application ID.

    Installed packages put it into the hicolor theme.  When running from the source tree the
    packaged copy is placed, named after APP_ID, in a directory added to the icon theme's
    search path, which GTK also searches for icons outside any theme.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return
    theme = Gtk.IconTheme.get_for_display(display)
    if not theme.has_icon(APP_ID):
        directory = Path(GLib.get_user_runtime_dir()) / "fractal-adjust" / "icons"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            icon = resources.files(__package__).joinpath("data/icon.svg").read_bytes()
            (directory / f"{APP_ID}.svg").write_bytes(icon)
        except OSError:
            return
        theme.add_search_path(str(directory))
    Gtk.Window.set_default_icon_name(APP_ID)


def run(args: argparse.Namespace) -> int:
    """Start the GUI with the command line options in @p args."""
    app = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(application: Adw.Application) -> None:
        _install_icon()
        window = MainWindow(application, args)
        if args.minimized if args.minimized is not None else window.start_minimized:
            # Minimizing before the window is mapped is ignored, so do it on the first map.
            def minimize(_w: Gtk.Window) -> None:
                window.disconnect(handler)
                GLib.idle_add(lambda: (window.minimize(), GLib.SOURCE_REMOVE)[1])
            handler = window.connect("map", minimize)
        window.present()
    app.connect("activate", activate)
    return app.run([])
