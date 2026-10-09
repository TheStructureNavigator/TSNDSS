"""Seestar preview integration (ROADMAP_DEVICE_BACKEND.md DB-03, Wave 3, offline).

Vendor-specific glue between the read-only Seestar Provider (DB-02) and the
vendor-neutral preview runtime (``device_runtime.preview_*``). Importing this
package performs no network access, starts no thread and imports no decoder
library. It has no Command surface: nothing here starts, stops, arms, parks or
moves anything.

Official API. A preview stream is opened one way only:
``build_preview_manager(...)`` returns a ``PreviewStreamManager``, and
``PreviewStreamManager.open_stream(camera)`` opens a stream only after the
readiness gate allowed it. The names exported here do not open anything: a
configuration, a read-only evidence provider, the decoder interface that an
adapter implements (Wave 4), and ``build_preview_manager``.

``source.RtspPreviewSource`` and ``source.make_source_factory`` are internal
building blocks. They are deliberately not exported here and are imported from
``.source`` by tests and by ``integration`` only. Python cannot forbid a caller
from importing them, so this is a documented API boundary, not a security
boundary (see docs/DB-03_SEESTAR_PREVIEW_INTEGRATION.md).
"""

import sys as _sys

from .config import SeestarPreviewConfig, StreamEndpoint
from .integration import build_preview_manager
from .readiness import SeestarReadinessEvidenceProvider, camera_availability
from .source import DecoderError, ImageDecoder

__all__ = [
    name
    for name, value in list(globals().items())
    if not name.startswith("_") and not isinstance(value, type(_sys))
]
