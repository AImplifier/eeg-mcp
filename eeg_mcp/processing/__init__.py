"""Pluggable real-time processors.

Bring your own feature extractor, classifier, artifact gate, or tokenizer; it
runs on the same footing as the built-ins. See ``docs/extending.md``.
"""

from .base import Processor, ProcessorContext, ProcessorOutput
from .registry import ENTRY_POINT_GROUP, available, create, load, load_errors

__all__ = [
    "ENTRY_POINT_GROUP",
    "Processor",
    "ProcessorContext",
    "ProcessorOutput",
    "available",
    "create",
    "load",
    "load_errors",
]
