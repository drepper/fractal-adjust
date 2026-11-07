"""MQTT bridge: receive JSON requests on a topic and publish the JSON replies (see MQTT.md)."""
from __future__ import annotations

import concurrent.futures
import json
from collections.abc import Callable
from typing import Any

from .config import MqttSettings

#: Takes a request payload and returns a future with the JSON text to publish as reply.
type RequestHandler = Callable[[bytes], concurrent.futures.Future[str]]


class MqttError(Exception):
    """The MQTT connection could not be set up."""


class MqttBridge:
    """Subscribes to the request topic and answers on the reply topic.

    Used by the GUI only.  paho-mqtt runs its network loop on its own thread and reconnects
    automatically; requests are handed to @p handler, whose futures complete on the hub thread.
    """

    def __init__(self, settings: MqttSettings, handler: RequestHandler,
                 on_state: Callable[[str], None] = lambda _: None) -> None:
        """Connect with @p settings; report connection changes as text to @p on_state."""
        try:
            import paho.mqtt.client as paho
            from paho.mqtt.enums import CallbackAPIVersion
        except ImportError as e:
            raise MqttError("MQTT support needs the paho-mqtt package") from e
        self._settings = settings
        self._handler = handler
        self._on_state = on_state
        self._client = paho.Client(CallbackAPIVersion.VERSION2, client_id=settings.client_id)
        if settings.username:
            self._client.username_pw_set(settings.username, settings.password or None)
        if settings.tls:
            self._client.tls_set()
        self._client.reconnect_delay_set(1, 60)
        self._client.on_connect = self._connected
        self._client.on_disconnect = self._disconnected
        self._client.on_message = self._message

    @property
    def description(self) -> str:
        """Human-readable summary of the connection."""
        s = self._settings
        return f"{s.host}:{s.port}, topic {s.topic}, replies on {s.effective_reply_topic}"

    def start(self) -> None:
        """Connect in the background; failures are retried."""
        try:
            self._client.connect_async(self._settings.host, self._settings.port, self._settings.keepalive)
        except (OSError, ValueError) as e:
            raise MqttError(f"cannot connect to {self._settings.host}:{self._settings.port}: {e}") from e
        self._client.loop_start()
        self._on_state(f"connecting to {self._settings.host}:{self._settings.port}")

    def stop(self) -> None:
        """Disconnect and stop the network thread."""
        self._client.disconnect()
        self._client.loop_stop()

    def _connected(self, client: Any, _userdata: Any, _flags: Any, reason: Any, _props: Any) -> None:
        if reason.is_failure:
            self._on_state(f"connection refused: {reason}")
            return
        client.subscribe(self._settings.topic, qos=1)
        self._on_state(f"connected to {self.description}")

    def _disconnected(self, _client: Any, _userdata: Any, _flags: Any, reason: Any, _props: Any) -> None:
        self._on_state(f"disconnected ({reason}), retrying")

    def _message(self, _client: Any, _userdata: Any, message: Any) -> None:
        future = self._handler(bytes(message.payload))

        def done(f: concurrent.futures.Future[str]) -> None:
            error = f.exception()
            self.reply(f.result() if error is None
                       else json.dumps({"id": None, "ok": False, "error": str(error)}))
        future.add_done_callback(done)

    def reply(self, text: str) -> None:
        """Publish @p text on the reply topic."""
        self._client.publish(self._settings.effective_reply_topic, text, qos=1)
