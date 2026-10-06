"""Curated top-level exports for TSN DSS.

The root package exposes the domain model plus the main service entrypoints
used by the local application. Lower-level helpers stay inside submodules so
the package surface remains easier to reason about during future hardware work.
"""

from .domain.models import (
    AcquisitionPlan,
    AcquisitionSequence,
    CatalogObject,
    CatalogObjectAlias,
    CatalogResolutionResult,
    Dataset,
    Equipment,
    Frame,
    ImagingProfile,
    MosaicPanel,
    MosaicPlan,
    Observation,
    ObservationEquipmentAssignment,
    PlannedPointing,
    ProcessingRun,
    Session,
    Site,
    TelescopeState,
    Target,
)
from .engine.sqlite import (
    CatalogRepository,
    DatasetRepository,
    EXPECTED_USER_VERSION,
    FrameRepository,
    MosaicRepository,
    ObservationRepository,
    PlanningRepository,
    ProcessingRunRepository,
    SchemaVersionError,
    SessionRepository,
    ValidationError,
    new_session_id,
)
from .engine.sqlite.db import (
    connect_database,
    foreign_key_violations,
    initialize_database,
    integrity_check,
    transaction,
)
from .engine.project_processing import (
    DEFAULT_SIRIL_EXECUTABLE,
    ProjectRunManager,
    ProjectRunSnapshot,
)
from .engine.openngc import (
    OPENNGC_SOURCE_VERSION,
    build_openngc_registration_plan,
    load_bundled_openngc_records,
    register_bundled_openngc_catalog,
)
from .engine.catalog_service import CatalogService, MAX_CATALOG_LIMIT
from .engine.projects import ProjectStorage
from .engine.siril import (
    DEFAULT_OSC_SCRIPT_PATH,
    SirilOutputNotFoundError,
    SirilProcessingService,
)
from .engine.telescope import (
    build_default_telescope_adapter_registry,
    TelescopeAdapterCapabilities,
    TelescopeAdapterDescriptor,
    TelescopeAdapterRegistry,
    TelescopeSnapshot,
    TelescopeStateService,
)


def __getattr__(name: str):
    if name in {"create_http_server", "run_server"}:
        from .gui import create_http_server, run_server

        return {"create_http_server": create_http_server, "run_server": run_server}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "EXPECTED_USER_VERSION",
    "SchemaVersionError",
    "Target",
    "CatalogObject",
    "CatalogObjectAlias",
    "CatalogResolutionResult",
    "Site",
    "Equipment",
    "AcquisitionPlan",
    "AcquisitionSequence",
    "Dataset",
    "Frame",
    "Observation",
    "ObservationEquipmentAssignment",
    "ProcessingRun",
    "Session",
    "TelescopeState",
    "ImagingProfile",
    "PlannedPointing",
    "MosaicPlan",
    "MosaicPanel",
    "PlanningRepository",
    "CatalogRepository",
    "ObservationRepository",
    "FrameRepository",
    "MosaicRepository",
    "DatasetRepository",
    "ProcessingRunRepository",
    "SessionRepository",
    "ValidationError",
    "new_session_id",
    "connect_database",
    "foreign_key_violations",
    "initialize_database",
    "integrity_check",
    "transaction",
    "ProjectStorage",
    "DEFAULT_SIRIL_EXECUTABLE",
    "ProjectRunManager",
    "ProjectRunSnapshot",
    "OPENNGC_SOURCE_VERSION",
    "build_openngc_registration_plan",
    "load_bundled_openngc_records",
    "register_bundled_openngc_catalog",
    "CatalogService",
    "MAX_CATALOG_LIMIT",
    "DEFAULT_OSC_SCRIPT_PATH",
    "SirilProcessingService",
    "SirilOutputNotFoundError",
    "TelescopeAdapterCapabilities",
    "TelescopeAdapterDescriptor",
    "TelescopeAdapterRegistry",
    "TelescopeSnapshot",
    "TelescopeStateService",
    "build_default_telescope_adapter_registry",
    "create_http_server",
    "run_server",
]
