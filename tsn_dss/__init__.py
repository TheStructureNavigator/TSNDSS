"""Curated top-level exports for TSN DSS.

The root package exposes the domain model plus the main service entrypoints
used by the local application. Lower-level helpers stay inside submodules so
the package surface remains easier to reason about during future hardware work.
"""

from .domain.models import (
    AcquisitionPlan,
    AcquisitionSequence,
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
    Site,
    TelescopeState,
    Target,
)
from .engine.sqlite import (
    DatasetRepository,
    EXPECTED_USER_VERSION,
    FrameRepository,
    MosaicRepository,
    ObservationRepository,
    PlanningRepository,
    ProcessingRunRepository,
    SchemaVersionError,
    ValidationError,
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
from .gui import create_http_server, run_server

__all__ = [
    "EXPECTED_USER_VERSION",
    "SchemaVersionError",
    "Target",
    "Site",
    "Equipment",
    "AcquisitionPlan",
    "AcquisitionSequence",
    "Dataset",
    "Frame",
    "Observation",
    "ObservationEquipmentAssignment",
    "ProcessingRun",
    "TelescopeState",
    "ImagingProfile",
    "PlannedPointing",
    "MosaicPlan",
    "MosaicPanel",
    "PlanningRepository",
    "ObservationRepository",
    "FrameRepository",
    "MosaicRepository",
    "DatasetRepository",
    "ProcessingRunRepository",
    "ValidationError",
    "connect_database",
    "foreign_key_violations",
    "initialize_database",
    "integrity_check",
    "transaction",
    "ProjectStorage",
    "DEFAULT_SIRIL_EXECUTABLE",
    "ProjectRunManager",
    "ProjectRunSnapshot",
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
