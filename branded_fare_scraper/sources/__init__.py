"""Source adapters (pluggable, priority-ordered).

Importing this package registers the built-in adapters. Add a new carrier by
creating a module here with a ``SourceAdapter`` subclass decorated with
``@register`` — the runner picks it up automatically by carrier + priority.
"""

from __future__ import annotations

from .base import SourceAdapter, get_sources_for, register, registered_sources

# Import adapter modules for their registration side effects (order = priority).
from . import turkish_airlines  # noqa: F401,E402  (official, priority 1)
from . import tripcom           # noqa: F401,E402  (primary OTA, priority 2)
from . import ubfly             # noqa: F401,E402  (fallback + cross-check, priority 3)
from . import enuygun           # noqa: F401,E402  (gap-filler, priority 3)

__all__ = ["SourceAdapter", "get_sources_for", "register", "registered_sources"]
