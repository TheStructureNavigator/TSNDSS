"""Curated engine-level exports for the TSN DSS package.

This package is used by the local app rather than by third-party consumers,
so the public surface is intentionally small. Service-level entrypoints stay
exported here, while lower-level implementation details remain in their
own modules.
"""

from .sqlite import (
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
    connect_database,
    foreign_key_violations,
    initialize_database,
    integrity_check,
    new_session_id,
    transaction,
)
from .projects import ProjectStorage
from .project_processing import DEFAULT_SIRIL_EXECUTABLE, ProjectRunManager, ProjectRunSnapshot
from .openngc import (
    OPENNGC_SOURCE_VERSION,
    build_openngc_registration_plan,
    load_bundled_openngc_records,
    register_bundled_openngc_catalog,
)
from .catalog_service import CatalogService, MAX_CATALOG_LIMIT
from .siril import (
    DEFAULT_OSC_SCRIPT_PATH,
    SirilOutputNotFoundError,
    SirilProcessingService,
)
from .telescope import (
    build_default_telescope_adapter_registry,
    TelescopeAdapterCapabilities,
    TelescopeAdapterDescriptor,
    TelescopeAdapterRegistry,
    TelescopeSnapshot,
    TelescopeStateService,
)

__all__ = [
    "EXPECTED_USER_VERSION",
    "SchemaVersionError",
    "PlanningRepository",
    "CatalogRepository",
    "ObservationRepository",
    "FrameRepository",
    "MosaicRepository",
    "DatasetRepository",
    "ProcessingRunRepository",
    "SessionRepository",
    "ValidationError",
    "connect_database",
    "foreign_key_violations",
    "initialize_database",
    "integrity_check",
    "new_session_id",
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
]
