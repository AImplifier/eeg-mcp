"""Signal sources: anything that can produce timestamped EEG chunks + events."""

from .base import RawEvent, SourceChunk, SourceInfo, StreamSource

__all__ = ["RawEvent", "SourceChunk", "SourceInfo", "StreamSource"]
