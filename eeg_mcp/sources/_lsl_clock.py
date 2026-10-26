"""Shared logic for reconciling an LSL stream's sample clock with Unix epoch.

Used by both LSLSource (continuous EEG-shaped acquisition) and
eeg_mcp.markers.LSLMarkerInlet (irregular-rate event streams) -- the ambiguity
described below is a property of how the *producer* chose to timestamp its
samples, not of what the samples mean, so both need the identical fix.

LSL timestamps are nominally on pylsl's own clock (``local_clock()``), not
Unix epoch. In practice, several real-world bridges -- muselsl confirmed
against a live Muse S -- stamp samples with something already close to Unix
epoch instead of calling ``local_clock()`` themselves, which would make a
blind correction double the timestamp rather than fix it. There is no
portable way to ask an outlet which convention it used, so this is decided
empirically from one real sample: a value already in Unix-epoch range (after
~2001) is left alone; anything smaller is assumed to be on pylsl's clock and
gets an offset applied.
"""

from __future__ import annotations

from ..utils import now

# Seconds-since-epoch value below which a timestamp cannot plausibly be Unix
# time (this is year 2001).
UNIX_EPOCH_FLOOR = 1e9


def local_clock() -> float:
    from pylsl import local_clock as _local_clock

    return _local_clock()


def candidate_offset() -> float:
    """Call once, right when an inlet opens: ``now() - local_clock()`` at
    that instant, in case the stream turns out to need it."""
    return now() - local_clock()


def resolve_offset(first_raw_timestamp: float, candidate: float) -> float:
    """Decide which convention a stream uses, from its first real sample."""
    return 0.0 if first_raw_timestamp >= UNIX_EPOCH_FLOOR else candidate
