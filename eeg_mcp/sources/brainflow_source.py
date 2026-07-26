"""Live acquisition from any BrainFlow-supported board.

BrainFlow gives us ~50 amplifiers behind one API, plus two boards that need no
hardware at all:

* ``SYNTHETIC_BOARD`` -- generated signal, for developing without a rig.
* ``PLAYBACK_FILE_BOARD`` -- replays a BrainFlow CSV at true wall-clock rate.

Note the division of labour with :mod:`eeg_mcp.sources.replay_source`: the
playback board is the right choice when you want BrainFlow's own pacing and
looping of a BrainFlow-format recording; the replay source is the right choice
when you need speed control, seeking, or any of the formats MNE can read.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..utils import now
from .base import RawEvent, SourceChunk, SourceInfo, StreamSource

# Params on BrainFlowInputParams that a caller may set. Anything else is
# rejected rather than silently ignored, so a typo does not become a mystery
# connection failure.
_ALLOWED_PARAMS = {
    "serial_port",
    "mac_address",
    "ip_address",
    "ip_port",
    "ip_protocol",
    "other_info",
    "serial_number",
    "file",
    "master_board",
    "timeout",
}


def quiet_brainflow() -> None:
    """Silence BrainFlow's native logger.

    It writes device chatter and probe failures straight to the process
    streams, which is noise at best and corrupts a stdio MCP transport at
    worst. Safe to call repeatedly.
    """
    try:
        from brainflow.board_shim import BoardShim

        BoardShim.disable_board_logger()
    except Exception:
        pass


def resolve_board_id(board: str | int) -> int:
    """Map a board name ('synthetic', 'CYTON_BOARD') or numeric id to an id."""
    from brainflow.board_shim import BoardIds

    if isinstance(board, int):
        return int(board)
    key = str(board).strip().upper().replace("-", "_").replace(" ", "_")
    if key.isdigit() or (key.startswith("-") and key[1:].isdigit()):
        return int(key)
    for candidate in (key, f"{key}_BOARD"):
        if candidate in BoardIds.__members__:
            return int(BoardIds.__members__[candidate].value)
    available = sorted(name for name in BoardIds.__members__)
    raise ValueError(
        f"Unknown board '{board}'. Use a numeric id or one of: {', '.join(available)}"
    )


def list_boards() -> list[dict[str, Any]]:
    """All board ids BrainFlow exposes, for discovery from the agent side."""
    from brainflow.board_shim import BoardIds

    # Probing every board id makes BrainFlow log an error for each one that has
    # no static description; that is expected here, so keep it quiet.
    quiet_brainflow()
    out = []
    for name, member in BoardIds.__members__.items():
        entry: dict[str, Any] = {"name": name, "board_id": int(member.value)}
        try:
            entry["sfreq"] = BoardShimSafe.sampling_rate(int(member.value))
            entry["n_eeg_channels"] = len(
                BoardShimSafe.eeg_channels(int(member.value))
            )
        except Exception:
            # Many board ids have no static description (they need a master
            # board, or are placeholders). Listing them is still useful.
            entry["sfreq"] = None
            entry["n_eeg_channels"] = None
        out.append(entry)
    return sorted(out, key=lambda e: e["board_id"])


class BoardShimSafe:
    """Static BoardShim lookups that raise a readable error instead of a code."""

    @staticmethod
    def sampling_rate(board_id: int) -> float:
        from brainflow.board_shim import BoardShim

        return float(BoardShim.get_sampling_rate(board_id))

    @staticmethod
    def eeg_channels(board_id: int) -> list[int]:
        from brainflow.board_shim import BoardShim

        return list(BoardShim.get_eeg_channels(board_id))

    @staticmethod
    def channel_names(board_id: int, n_channels: int) -> list[str]:
        from brainflow.board_shim import BoardShim

        try:
            names = list(BoardShim.get_eeg_names(board_id))
            if len(names) == n_channels:
                return names
        except Exception:
            pass
        return [f"ch{i}" for i in range(n_channels)]


class BrainFlowSource(StreamSource):
    """Polls a BoardShim and hands back EEG rows, timestamps, and markers."""

    def __init__(
        self,
        board: str | int = "synthetic",
        params: dict[str, Any] | None = None,
        ring_buffer_size: int = 450_000,
        channels: list[int] | None = None,
    ) -> None:
        from brainflow.board_shim import BoardShim, BrainFlowInputParams

        self.board_id = resolve_board_id(board)
        self._requested_params = dict(params or {})

        unknown = set(self._requested_params) - _ALLOWED_PARAMS
        if unknown:
            raise ValueError(
                f"Unknown BrainFlow params: {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(_ALLOWED_PARAMS))}"
            )

        bf_params = BrainFlowInputParams()
        for key, value in self._requested_params.items():
            if key == "master_board":
                value = resolve_board_id(value)
            setattr(bf_params, key, value)

        quiet_brainflow()
        self._board = BoardShim(self.board_id, bf_params)
        self._ring_buffer_size = int(ring_buffer_size)
        self._started = False

        # Playback and streaming boards carry a master board whose channel
        # layout is the one that actually describes the data.
        self._layout_id = self._resolve_layout_id()

        all_eeg = BoardShimSafe.eeg_channels(self._layout_id)
        self._eeg_rows = list(channels) if channels else all_eeg
        invalid = [c for c in self._eeg_rows if c not in all_eeg]
        if invalid:
            raise ValueError(
                f"Rows {invalid} are not EEG channels for board {self._layout_id}. "
                f"EEG rows are {all_eeg}."
            )

        from brainflow.board_shim import BoardShim as _BS

        self._ts_row = int(_BS.get_timestamp_channel(self._layout_id))
        try:
            self._marker_row: int | None = int(
                _BS.get_marker_channel(self._layout_id)
            )
        except Exception:
            self._marker_row = None

        names = BoardShimSafe.channel_names(self._layout_id, len(all_eeg))
        idx_by_row = {row: i for i, row in enumerate(all_eeg)}
        ch_names = [
            names[idx_by_row[row]] if idx_by_row[row] < len(names) else f"ch{row}"
            for row in self._eeg_rows
        ]

        self.info = SourceInfo(
            kind="brainflow",
            name=f"board:{self.board_id}",
            sfreq=BoardShimSafe.sampling_rate(self._layout_id),
            ch_names=ch_names,
            metadata={
                "board_id": self.board_id,
                "layout_board_id": self._layout_id,
                "eeg_rows": self._eeg_rows,
                "has_marker_channel": self._marker_row is not None,
                "params": {
                    k: v for k, v in self._requested_params.items() if k != "file"
                },
            },
        )

    def _resolve_layout_id(self) -> int:
        """Board id whose channel map describes the data we will receive."""
        master = self._requested_params.get("master_board")
        if master:
            return resolve_board_id(master)
        try:
            # For playback/streaming boards BrainFlow returns the master id here.
            return int(self._board.get_board_id())
        except Exception:
            return self.board_id

    def start(self) -> None:
        if self._started:
            return
        try:
            self._board.prepare_session()
        except Exception as exc:
            # BrainFlow's error codes are opaque; translate the ones a user will
            # actually hit into something that names the fix.
            message = str(exc)
            if "ANOTHER_BOARD_IS_CREATED" in message:
                raise RuntimeError(
                    f"Board {self.board_id} is already open in this process. "
                    "BrainFlow allows only one session per board at a time -- "
                    "including the synthetic board. Call list_sessions to find "
                    "the session holding it and stop_stream that one first, or "
                    "use start_replay if you wanted a second independent source."
                ) from exc
            if "UNABLE_TO_OPEN_PORT" in message:
                port = self._requested_params.get("serial_port", "(unset)")
                raise RuntimeError(
                    f"Could not open serial port '{port}' for board "
                    f"{self.board_id}. Check the port name, that no other "
                    "software holds it, and that you have permission (on Linux, "
                    "add yourself to the 'dialout' group)."
                ) from exc
            if "BOARD_NOT_READY" in message:
                raise RuntimeError(
                    f"Board {self.board_id} did not become ready. It may be off, "
                    "out of range, or still connected to another application."
                ) from exc
            raise
        self._board.start_stream(self._ring_buffer_size)
        self._started = True

    def poll(self) -> SourceChunk:
        if not self._started:
            return SourceChunk(
                data=np.zeros((len(self._eeg_rows), 0)), timestamps=np.zeros(0)
            )
        # get_board_data drains BrainFlow's internal buffer, so each poll
        # returns exactly the samples that arrived since the previous one.
        raw = self._board.get_board_data()
        if raw.size == 0 or raw.shape[1] == 0:
            return SourceChunk(
                data=np.zeros((len(self._eeg_rows), 0)), timestamps=np.zeros(0)
            )

        data = raw[self._eeg_rows, :].astype(np.float64, copy=True)
        timestamps = raw[self._ts_row, :].astype(np.float64, copy=True)
        # Boards that have not synced a clock yet emit zero timestamps; fall
        # back to local time so downstream time arithmetic stays sane.
        if not np.all(timestamps > 0):
            timestamps = np.full(raw.shape[1], now(), dtype=np.float64)

        events: list[RawEvent] = []
        if self._marker_row is not None:
            markers = raw[self._marker_row, :]
            for idx in np.flatnonzero(markers):
                events.append(
                    RawEvent(
                        timestamp=float(timestamps[idx]),
                        label=f"marker_{int(markers[idx])}",
                        origin="marker",
                        value=float(markers[idx]),
                        payload={"board_id": self.board_id},
                    )
                )
        return SourceChunk(data=data, timestamps=timestamps, events=events)

    def insert_marker(self, value: float, label: str) -> bool:
        """Write a marker into the board's own marker channel.

        This is the highest-fidelity event path available: the marker is
        interleaved with the samples by BrainFlow itself, so it needs no clock
        join later. BrainFlow rejects 0.0, which it reserves for "no marker".
        """
        if self._marker_row is None or not self._started:
            return False
        if float(value) == 0.0:
            raise ValueError(
                "BrainFlow reserves marker value 0 for 'no marker'; use a nonzero value."
            )
        self._board.insert_marker(float(value))
        return True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            if self._board.is_prepared():
                self._board.stop_stream()
        finally:
            try:
                if self._board.is_prepared():
                    self._board.release_session()
            finally:
                self._started = False
