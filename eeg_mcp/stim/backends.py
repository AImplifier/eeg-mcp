"""Concrete stimulation targets, software and hardware.

A deliberate design note on the hardware backends: they are **generic
transports driven by user-supplied command templates**, not reverse-engineered
vendor drivers. Writing a fake "Magstim driver" that has never been tested
against a Magstim would be worse than useless -- it would look authoritative
while silently doing the wrong thing to a device pointed at someone's head.

So instead: you supply the byte or the command string your device's manual
documents, and this layer handles timing, gating, logging, and getting it onto
the wire. That keeps the dangerous, device-specific knowledge where it belongs
-- with the operator who has the manual and the approvals.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .base import SafetyError, StimBackend, StimCommand


class LogBackend(StimBackend):
    """Default backend: records the command and delivers nothing.

    Not a stub to be replaced -- it is the correct target while designing a
    protocol, and it makes the whole stimulation path testable with no device
    and no risk.
    """

    name = "log"
    is_hardware = False

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_hardware": False,
            "kinds": ["marker", "pulse", "train", "ramp", "custom"],
            "supports_intensity": True,
            "requires_arm": False,
            "note": "Accepts everything, emits nothing. Safe for dry runs.",
        }

    def _dispatch(self, command: StimCommand) -> dict[str, Any]:
        return {"delivered": False, "reason": "log backend performs no output"}


class LSLMarkerBackend(StimBackend):
    """Publishes markers on an LSL outlet for other software to consume.

    This is the interoperability path: PsychoPy, OpenViBE, LabRecorder, and any
    other LSL client on the network sees these markers and can timestamp them
    against their own streams.
    """

    name = "lsl"
    is_hardware = False

    def __init__(self, stream_name: str = "eeg-mcp-markers", source_id: str = "eeg-mcp",
                 **options: Any) -> None:
        super().__init__(**options)
        self.stream_name = stream_name
        self.source_id = source_id
        self._outlet = None

    def _open_impl(self) -> None:
        try:
            from pylsl import StreamInfo, StreamOutlet
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RuntimeError(
                "The 'lsl' backend needs pylsl. Install it with: pip install eeg-mcp[lsl]"
            ) from exc
        # Irregular rate (0) is correct for markers: they are events, not a
        # sampled signal, and LSL uses this to skip rate-drift correction.
        info = StreamInfo(
            self.stream_name, "Markers", 1, 0, "string", self.source_id
        )
        self._outlet = StreamOutlet(info)

    def _close_impl(self) -> None:
        self._outlet = None

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_hardware": False,
            "kinds": ["marker"],
            "supports_intensity": False,
            "requires_arm": False,
            "stream_name": self.stream_name,
        }

    def _dispatch(self, command: StimCommand) -> dict[str, Any]:
        if self._outlet is None:
            raise RuntimeError("LSL outlet is not open")
        payload = f"{command.label}:{command.value:g}"
        self._outlet.push_sample([payload])
        return {"delivered": True, "payload": payload, "stream": self.stream_name}


class BrainFlowMarkerBackend(StimBackend):
    """Writes the event into the acquisition board's own marker channel.

    The highest-fidelity option when it is available: BrainFlow interleaves the
    marker with the samples, so the event needs no clock join afterwards -- the
    recording is self-describing.
    """

    name = "brainflow_marker"
    is_hardware = False

    def __init__(self, marker_fn: Callable[[float, str], bool] | None = None,
                 **options: Any) -> None:
        super().__init__(**options)
        self._marker_fn = marker_fn

    def bind(self, marker_fn: Callable[[float, str], bool]) -> None:
        """Attach the live source's marker writer (done by the session)."""
        self._marker_fn = marker_fn

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_hardware": False,
            "kinds": ["marker"],
            "supports_intensity": False,
            "requires_arm": False,
            "bound": self._marker_fn is not None,
        }

    def _dispatch(self, command: StimCommand) -> dict[str, Any]:
        if self._marker_fn is None:
            raise RuntimeError(
                "No live source bound. This backend only works while a BrainFlow "
                "stream is running; start_stream first."
            )
        if float(command.value) == 0.0:
            raise ValueError("BrainFlow reserves marker value 0; use a nonzero value.")
        ok = self._marker_fn(float(command.value), command.label)
        return {
            "delivered": bool(ok),
            "reason": None if ok else "source has no marker channel",
        }


class SerialTTLBackend(StimBackend):
    """Byte-level triggers over a serial port to a trigger box or stimulator.

    The workhorse for hardware event marking. ``kind='pulse'`` writes the code,
    holds it for ``duration_ms``, then writes zero -- the standard convention
    for trigger boxes that latch until cleared.
    """

    name = "serial_ttl"
    is_hardware = True

    def __init__(self, port: str | None = None, baudrate: int = 115200,
                 pulse_ms: float = 10.0, **options: Any) -> None:
        super().__init__(**options)
        self.port = port
        self.baudrate = int(baudrate)
        self.pulse_ms = float(pulse_ms)
        self._serial = None

    def _open_impl(self) -> None:
        if not self.port:
            raise ValueError(
                "serial_ttl needs a port (e.g. 'COM3' or '/dev/ttyUSB0'). Pass it as "
                "an option or set EEG_MCP_SERIAL_PORT."
            )
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RuntimeError(
                "The serial backends need pyserial. Install: pip install eeg-mcp[serial]"
            ) from exc
        self._serial = serial.Serial(self.port, self.baudrate, timeout=1.0)

    def _close_impl(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_hardware": True,
            "kinds": ["marker", "pulse"],
            "supports_intensity": False,
            "requires_arm": True,
            "port": self.port,
            "baudrate": self.baudrate,
        }

    def _dispatch(self, command: StimCommand) -> dict[str, Any]:
        if self._serial is None:
            raise RuntimeError("Serial port is not open")
        code = int(command.value) & 0xFF
        self._serial.write(bytes([code]))
        self._serial.flush()
        if command.kind == "pulse":
            width = command.duration_ms or self.pulse_ms
            time.sleep(width / 1000.0)
            self._serial.write(bytes([0]))
            self._serial.flush()
            return {"delivered": True, "code": code, "pulse_ms": width}
        return {"delivered": True, "code": code}


class TemplateSerialBackend(StimBackend):
    """Text-protocol serial backend driven by user-supplied command templates.

    Many stimulators (tES units, programmable TMS controllers, custom rigs)
    expose an ASCII command protocol. Rather than guess at any one vendor's
    dialect, you provide the templates from your device's manual:

    ``{"fire": "TRIG\\r\\n", "set_intensity": "AMP {intensity:.1f}\\r\\n"}``

    Placeholders are filled from the :class:`StimCommand` fields via
    ``str.format`` -- no expression evaluation, so a template cannot execute code.
    """

    name = "template_serial"
    is_hardware = True

    def __init__(
        self,
        port: str | None = None,
        baudrate: int = 115200,
        templates: dict[str, str] | None = None,
        read_reply: bool = False,
        encoding: str = "ascii",
        **options: Any,
    ) -> None:
        super().__init__(**options)
        self.port = port
        self.baudrate = int(baudrate)
        self.templates = dict(templates or {})
        self.read_reply = bool(read_reply)
        self.encoding = encoding
        self._serial = None

    def _open_impl(self) -> None:
        if not self.port:
            raise ValueError(
                f"{self.name} needs a port (e.g. 'COM3'). Pass it as an option or "
                "set EEG_MCP_SERIAL_PORT."
            )
        if not self.templates:
            raise ValueError(
                f"{self.name} needs at least a 'fire' template describing the command "
                "your device expects, e.g. templates={'fire': 'TRIG\\r\\n'}. Take it "
                "from the device manual."
            )
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RuntimeError(
                "The serial backends need pyserial. Install: pip install eeg-mcp[serial]"
            ) from exc
        self._serial = serial.Serial(self.port, self.baudrate, timeout=1.0)

    def _close_impl(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_hardware": True,
            "kinds": ["marker", "pulse", "train", "ramp", "custom"],
            "supports_intensity": True,
            "requires_arm": True,
            "port": self.port,
            "templates": sorted(self.templates),
        }

    def _render(self, key: str, command: StimCommand) -> str:
        template = self.templates.get(key)
        if template is None:
            raise ValueError(
                f"No '{key}' template configured for {self.name}. Configured: "
                f"{sorted(self.templates) or 'none'}"
            )
        fields = command.as_dict()
        fields.update(command.params)
        try:
            return template.format(**fields)
        except KeyError as exc:
            raise ValueError(
                f"Template '{key}' references {exc} which is not a StimCommand field. "
                f"Available: {sorted(fields)}"
            ) from exc

    def _write(self, text: str) -> str | None:
        if self._serial is None:
            raise RuntimeError("Serial port is not open")
        self._serial.write(text.encode(self.encoding))
        self._serial.flush()
        if self.read_reply:
            return self._serial.readline().decode(self.encoding, errors="replace").strip()
        return None

    def _dispatch(self, command: StimCommand) -> dict[str, Any]:
        sent: list[str] = []
        replies: list[str | None] = []
        # Set the dose first, then trigger -- the ordering every stimulator
        # protocol assumes, and the one that fails safe if the second step errors.
        if command.intensity is not None and "set_intensity" in self.templates:
            text = self._render("set_intensity", command)
            replies.append(self._write(text))
            sent.append(text)
        key = command.kind if command.kind in self.templates else "fire"
        text = self._render(key, command)
        replies.append(self._write(text))
        sent.append(text)
        return {
            "delivered": True,
            "commands_sent": sent,
            "replies": [r for r in replies if r is not None],
        }


class TMSBackend(TemplateSerialBackend):
    """TMS stimulator: intensity in %MSO, fired by trigger.

    Identical machinery to :class:`TemplateSerialBackend` with TMS-appropriate
    defaults and a tighter default interval, since inter-pulse interval is the
    parameter with real safety consequences in repetitive protocols.
    """

    name = "tms"

    def capabilities(self) -> dict[str, Any]:
        caps = super().capabilities()
        caps.update({"name": self.name, "intensity_unit": "%MSO",
                     "kinds": ["pulse", "train", "marker"]})
        return caps

    def _dispatch(self, command: StimCommand) -> dict[str, Any]:
        if command.intensity is not None and command.intensity > 100:
            raise SafetyError(
                f"TMS intensity is expressed as %MSO; {command.intensity} > 100 is "
                "not a meaningful value."
            )
        return super()._dispatch(command)


class TESBackend(TemplateSerialBackend):
    """tES (tDCS/tACS/tRNS) stimulator: current in mA, with ramping.

    The default intensity ceiling is 4 mA, the conventional upper bound in
    published human tES safety guidance. It is a configured default, not a
    guarantee -- your protocol's approved limit governs.
    """

    name = "tes"

    def __init__(self, *args: Any, max_current_ma: float = 4.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_current_ma = float(max_current_ma)

    def capabilities(self) -> dict[str, Any]:
        caps = super().capabilities()
        caps.update({
            "name": self.name,
            "intensity_unit": "mA",
            "kinds": ["ramp", "train", "custom", "marker"],
            "max_current_ma": self.max_current_ma,
        })
        return caps

    def _dispatch(self, command: StimCommand) -> dict[str, Any]:
        if command.intensity is not None and command.intensity > self.max_current_ma:
            raise SafetyError(
                f"tES current {command.intensity} mA exceeds this backend's ceiling "
                f"of {self.max_current_ma} mA."
            )
        return super()._dispatch(command)
