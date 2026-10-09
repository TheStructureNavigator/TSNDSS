"""Seestar preview integration (ROADMAP_DEVICE_BACKEND.md DB-03, Wave 3, offline).

Vendor-specific glue between the read-only Seestar Provider (DB-02) and the
vendor-neutral preview runtime (``device_runtime.preview_*``). Importing this
package performs no network access, starts no thread and imports no decoder
library. It has no Command surface: nothing here starts, stops, arms, parks or
moves anything. A concrete decoder is injected from outside (Wave 4).
"""

import sys as _sys

from .config import SeestarPreviewConfig, StreamEndpoint
from .integration import build_preview_manager
from .readiness import SeestarReadinessEvidenceProvider, camera_availability
from .source import DecoderError, ImageDecoder, RtspPreviewSource, make_source_factory

__all__ = [
    name
    for name, value in list(globals().items())
    if not name.startswith("_") and not isinstance(value, type(_sys))
]
