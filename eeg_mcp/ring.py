"""Fixed-capacity circular buffer for continuous multichannel signals.

One producer thread (the acquisition poller) writes; MCP tool calls read. The
buffer never reallocates and never grows, so a stream that runs for hours costs
the same memory as one that ran for a second -- the oldest samples are simply
overwritten.

Reads are "last N samples" because that is what every real-time question
reduces to: *what is happening now*. An absolute sample index is also tracked
(``total_written``) so callers can detect that they fell behind and silently
lost samples, rather than being handed a gap they cannot see.
"""

from __future__ import annotations

import threading

import numpy as np


class RingBuffer:
    """Circular (n_channels, capacity) sample store with parallel timestamps."""

    def __init__(self, n_channels: int, capacity: int, start_index: int = 0) -> None:
        if n_channels <= 0:
            raise ValueError("n_channels must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.n_channels = n_channels
        self.capacity = capacity
        self._data = np.zeros((n_channels, capacity), dtype=np.float64)
        self._ts = np.zeros(capacity, dtype=np.float64)
        self._write = 0  # next slot to write
        self._n_written = 0  # samples this buffer has actually stored
        # Absolute index of the first sample this buffer will accept. A buffer
        # created mid-stream (the filtered one, when filters are set later)
        # starts here so that a sample's absolute index means the same thing in
        # every buffer of a session -- otherwise event sample_index lookups
        # silently target the wrong samples.
        self.start_index = int(start_index)
        self._lock = threading.Lock()

    @property
    def total_written(self) -> int:
        """Absolute index just past the newest sample."""
        with self._lock:
            return self.start_index + self._n_written

    def write(self, chunk: np.ndarray, timestamps: np.ndarray | None = None) -> int:
        """Append ``chunk`` (n_channels, n_samples); returns samples stored.

        A chunk longer than the whole buffer is truncated to its tail -- keeping
        the most recent samples is always the right choice for a live view.
        """
        if chunk.ndim != 2 or chunk.shape[0] != self.n_channels:
            raise ValueError(
                f"chunk must be ({self.n_channels}, n_samples), got {chunk.shape}"
            )
        n = chunk.shape[1]
        if n == 0:
            return 0
        if timestamps is None:
            timestamps = np.zeros(n, dtype=np.float64)
        if timestamps.shape[0] != n:
            raise ValueError("timestamps length must match chunk sample count")

        if n > self.capacity:
            chunk = chunk[:, -self.capacity :]
            timestamps = timestamps[-self.capacity :]
            n = self.capacity

        with self._lock:
            start = self._write
            end = start + n
            if end <= self.capacity:
                self._data[:, start:end] = chunk
                self._ts[start:end] = timestamps
            else:
                # Wraps: split into the tail of the buffer and the head.
                split = self.capacity - start
                self._data[:, start:] = chunk[:, :split]
                self._ts[start:] = timestamps[:split]
                self._data[:, : n - split] = chunk[:, split:]
                self._ts[: n - split] = timestamps[split:]
            self._write = end % self.capacity
            self._n_written += n
        return n

    def read_last(self, n_samples: int) -> tuple[np.ndarray, np.ndarray]:
        """Return the most recent ``n_samples`` as (data, timestamps).

        Returns fewer samples than requested if the stream is still filling.
        The returned arrays are copies, so callers may filter them in place.
        """
        with self._lock:
            available = min(self._n_written, self.capacity)
            n = int(min(max(n_samples, 0), available))
            if n == 0:
                return (
                    np.zeros((self.n_channels, 0), dtype=np.float64),
                    np.zeros(0, dtype=np.float64),
                )
            start = (self._write - n) % self.capacity
            end = start + n
            if end <= self.capacity:
                return self._data[:, start:end].copy(), self._ts[start:end].copy()
            split = self.capacity - start
            data = np.concatenate(
                (self._data[:, start:], self._data[:, : n - split]), axis=1
            )
            ts = np.concatenate((self._ts[start:], self._ts[: n - split]))
            return data, ts

    @property
    def n_available(self) -> int:
        """Samples currently retrievable (caps at capacity once wrapped)."""
        with self._lock:
            return int(min(self._n_written, self.capacity))

    @property
    def oldest_index(self) -> int:
        """Absolute index of the oldest sample still retrievable."""
        with self._lock:
            return self.start_index + self._n_written - min(
                self._n_written, self.capacity
            )

    def clear(self) -> None:
        """Discard contents while keeping absolute indices monotonic.

        The counter is *not* reset to zero: events already logged carry absolute
        sample indices, and restarting the count would silently make them point
        at unrelated samples. Instead the buffer simply becomes empty, and those
        older events correctly read as having scrolled out.
        """
        with self._lock:
            self._data.fill(0.0)
            self._ts.fill(0.0)
            self._write = 0
            self.start_index += self._n_written
            self._n_written = 0
