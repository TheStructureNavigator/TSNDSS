PRAGMA foreign_keys = ON;

-- ============================================================
-- TSN Deep Space System (DSS) v0.1
-- SQLite domain schema
-- ============================================================

CREATE TABLE IF NOT EXISTS targets (
    id TEXT PRIMARY KEY,
    catalog TEXT NOT NULL,
    catalog_id TEXT NOT NULL,
    name TEXT NOT NULL,
    object_type TEXT,
    ra_deg REAL NOT NULL CHECK (ra_deg >= 0 AND ra_deg < 360),
    dec_deg REAL NOT NULL CHECK (dec_deg >= -90 AND dec_deg <= 90),
    angular_major_arcmin REAL CHECK (angular_major_arcmin IS NULL OR angular_major_arcmin > 0),
    angular_minor_arcmin REAL CHECK (angular_minor_arcmin IS NULL OR angular_minor_arcmin > 0),
    distance_ly REAL CHECK (distance_ly IS NULL OR distance_ly > 0),
    constellation TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (catalog, catalog_id)
);

CREATE INDEX IF NOT EXISTS idx_targets_radec
ON targets (ra_deg, dec_deg);


CREATE TABLE IF NOT EXISTS sites (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    latitude_deg REAL CHECK (latitude_deg IS NULL OR latitude_deg BETWEEN -90 AND 90),
    longitude_deg REAL CHECK (longitude_deg IS NULL OR longitude_deg BETWEEN -180 AND 180),
    elevation_m REAL,
    sqm_mag_arcsec2 REAL,
    bortle_class INTEGER CHECK (bortle_class IS NULL OR bortle_class BETWEEN 1 AND 9),
    south_horizon_open INTEGER NOT NULL DEFAULT 0 CHECK (south_horizon_open IN (0, 1)),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE IF NOT EXISTS equipment (
    id TEXT PRIMARY KEY,
    equipment_type TEXT NOT NULL CHECK (
        equipment_type IN (
            'mount',
            'telescope',
            'camera',
            'guide_scope',
            'guide_camera',
            'focuser',
            'filter_wheel',
            'filter',
            'reducer_flattener',
            'barlow',
            'power',
            'computer',
            'other'
        )
    ),
    manufacturer TEXT,
    model TEXT,
    serial_number TEXT,
    properties_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(properties_json)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_equipment_type
ON equipment (equipment_type);


CREATE TABLE IF NOT EXISTS acquisition_plans (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'ready', 'archived')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (target_id) REFERENCES targets(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_acquisition_plans_target
ON acquisition_plans (target_id);


CREATE TABLE IF NOT EXISTS acquisition_sequences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    sequence_order INTEGER NOT NULL CHECK (sequence_order >= 0),
    name TEXT,
    frame_type TEXT NOT NULL
        CHECK (frame_type IN ('light', 'dark', 'flat', 'bias', 'dark_flat')),
    exposure_s REAL CHECK (exposure_s IS NULL OR exposure_s > 0),
    frame_count INTEGER NOT NULL CHECK (frame_count > 0),
    iso INTEGER CHECK (iso IS NULL OR iso > 0),
    gain REAL,
    offset_value REAL,
    binning_x INTEGER NOT NULL DEFAULT 1 CHECK (binning_x > 0),
    binning_y INTEGER NOT NULL DEFAULT 1 CHECK (binning_y > 0),
    filter_id TEXT,
    notes TEXT,
    FOREIGN KEY (plan_id) REFERENCES acquisition_plans(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    FOREIGN KEY (filter_id) REFERENCES equipment(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    UNIQUE (plan_id, sequence_order)
);

CREATE INDEX IF NOT EXISTS idx_acquisition_sequences_filter
ON acquisition_sequences (filter_id);


CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    observation_number INTEGER UNIQUE,
    target_id TEXT NOT NULL,
    site_id TEXT,
    acquisition_plan_id TEXT,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (
            status IN (
                'planned',
                'preparing',
                'running',
                'paused',
                'completed',
                'aborted',
                'failed'
            )
        ),
    started_at TEXT,
    finished_at TEXT,
    operator_notes TEXT,
    weather_notes TEXT,
    moon_illumination_pct REAL
        CHECK (
            moon_illumination_pct IS NULL OR
            moon_illumination_pct BETWEEN 0 AND 100
        ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (target_id) REFERENCES targets(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,
    FOREIGN KEY (site_id) REFERENCES sites(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    FOREIGN KEY (acquisition_plan_id) REFERENCES acquisition_plans(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    CHECK (
        finished_at IS NULL OR
        started_at IS NULL OR
        finished_at >= started_at
    )
);

CREATE INDEX IF NOT EXISTS idx_observations_target
ON observations (target_id);

CREATE INDEX IF NOT EXISTS idx_observations_site
ON observations (site_id);

CREATE INDEX IF NOT EXISTS idx_observations_status
ON observations (status);

CREATE INDEX IF NOT EXISTS idx_observations_started_at
ON observations (started_at);

CREATE INDEX IF NOT EXISTS idx_observations_acquisition_plan
ON observations (acquisition_plan_id);


CREATE TABLE IF NOT EXISTS observation_equipment (
    observation_id TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (
        role IN (
            'mount',
            'main_telescope',
            'main_camera',
            'guide_scope',
            'guide_camera',
            'focuser',
            'filter_wheel',
            'reducer_flattener',
            'barlow',
            'power',
            'computer',
            'other'
        )
    ),
    PRIMARY KEY (observation_id, role),
    FOREIGN KEY (observation_id) REFERENCES observations(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    FOREIGN KEY (equipment_id) REFERENCES equipment(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_observation_equipment_equipment
ON observation_equipment (equipment_id);


CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id TEXT NOT NULL,
    sequence_id INTEGER,
    frame_type TEXT NOT NULL
        CHECK (frame_type IN ('light', 'dark', 'flat', 'bias', 'dark_flat')),
    file_path TEXT NOT NULL,
    captured_at TEXT,
    exposure_s REAL CHECK (exposure_s IS NULL OR exposure_s > 0),
    iso INTEGER CHECK (iso IS NULL OR iso > 0),
    gain REAL,
    offset_value REAL,
    filter_id TEXT,
    accepted INTEGER CHECK (accepted IS NULL OR accepted IN (0, 1)),
    rejection_reason TEXT,

    -- Basic image-quality metrics
    fwhm_px REAL CHECK (fwhm_px IS NULL OR fwhm_px >= 0),
    fwhm_arcsec REAL CHECK (fwhm_arcsec IS NULL OR fwhm_arcsec >= 0),
    eccentricity REAL CHECK (eccentricity IS NULL OR eccentricity >= 0),
    star_count INTEGER CHECK (star_count IS NULL OR star_count >= 0),
    background_median REAL,
    background_sigma REAL,
    snr_estimate REAL,

    -- Optional acquisition telemetry snapshot
    mount_ra_deg REAL,
    mount_dec_deg REAL,
    guiding_rms_arcsec REAL,
    camera_temp_c REAL,

    metadata_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(metadata_json)),

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (observation_id) REFERENCES observations(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    FOREIGN KEY (sequence_id) REFERENCES acquisition_sequences(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    FOREIGN KEY (filter_id) REFERENCES equipment(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    UNIQUE (observation_id, file_path)
);

CREATE INDEX IF NOT EXISTS idx_frames_observation
ON frames (observation_id);

CREATE INDEX IF NOT EXISTS idx_frames_quality
ON frames (observation_id, accepted, fwhm_arcsec, guiding_rms_arcsec);

CREATE INDEX IF NOT EXISTS idx_frames_captured_at
ON frames (captured_at);

CREATE INDEX IF NOT EXISTS idx_frames_sequence
ON frames (sequence_id);

CREATE INDEX IF NOT EXISTS idx_frames_filter
ON frames (filter_id);


CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'complete', 'archived')),
    total_light_integration_s REAL NOT NULL DEFAULT 0
        CHECK (total_light_integration_s >= 0),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (target_id) REFERENCES targets(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_datasets_target
ON datasets (target_id);


CREATE TABLE IF NOT EXISTS dataset_observations (
    dataset_id TEXT NOT NULL,
    observation_id TEXT NOT NULL,
    PRIMARY KEY (dataset_id, observation_id),
    FOREIGN KEY (dataset_id) REFERENCES datasets(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    FOREIGN KEY (observation_id) REFERENCES observations(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_dataset_observations_observation
ON dataset_observations (observation_id);


CREATE TABLE IF NOT EXISTS dataset_frames (
    dataset_id TEXT NOT NULL,
    frame_id INTEGER NOT NULL,
    PRIMARY KEY (dataset_id, frame_id),
    FOREIGN KEY (dataset_id) REFERENCES datasets(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    FOREIGN KEY (frame_id) REFERENCES frames(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_dataset_frames_frame
ON dataset_frames (frame_id);


CREATE TABLE IF NOT EXISTS processing_runs (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    version_label TEXT NOT NULL,
    engine_name TEXT NOT NULL,
    engine_version TEXT,
    pipeline_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(pipeline_json)),
    parameters_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(parameters_json)),
    linear_stack_path TEXT,
    preview_path TEXT,
    final_image_path TEXT,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'running', 'completed', 'failed')),
    started_at TEXT,
    finished_at TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (dataset_id) REFERENCES datasets(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    UNIQUE (dataset_id, version_label)
);

CREATE TABLE IF NOT EXISTS surveys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    catalog TEXT,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('planned', 'active', 'completed', 'archived')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE IF NOT EXISTS survey_targets (
    survey_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_order INTEGER,
    status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (status IN ('not_started', 'planned', 'in_progress', 'completed')),
    best_dataset_id TEXT,
    completed_at TEXT,
    notes TEXT,
    PRIMARY KEY (survey_id, target_id),
    FOREIGN KEY (survey_id) REFERENCES surveys(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES targets(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,
    FOREIGN KEY (best_dataset_id) REFERENCES datasets(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_survey_targets_status
ON survey_targets (survey_id, status);

CREATE INDEX IF NOT EXISTS idx_survey_targets_target
ON survey_targets (target_id);

CREATE INDEX IF NOT EXISTS idx_survey_targets_best_dataset
ON survey_targets (best_dataset_id);


-- Event log: discrete facts ("what happened")
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    observation_id TEXT,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info'
        CHECK (severity IN ('debug', 'info', 'warning', 'error', 'critical')),
    payload_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(payload_json)),
    FOREIGN KEY (observation_id) REFERENCES observations(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_observation_time
ON events (observation_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_events_type
ON events (event_type, occurred_at);


-- Telemetry: high-frequency measurements
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    measured_at TEXT NOT NULL,
    observation_id TEXT,
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    value_real REAL,
    value_text TEXT,
    unit TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(metadata_json)),
    FOREIGN KEY (observation_id) REFERENCES observations(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,
    CHECK (
        value_real IS NOT NULL OR
        value_text IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_telemetry_observation_metric_time
ON telemetry (observation_id, metric, measured_at);


-- ============================================================
-- Useful views
-- ============================================================

CREATE VIEW IF NOT EXISTS v_observation_summary AS
SELECT
    o.id AS observation_id,
    o.observation_number,
    o.status,
    t.catalog_id AS target_catalog_id,
    t.name AS target_name,
    s.name AS site_name,
    o.started_at,
    o.finished_at,
    COUNT(f.id) AS frame_count,
    SUM(
        CASE
            WHEN f.frame_type = 'light' AND f.accepted = 1
            THEN COALESCE(f.exposure_s, 0)
            ELSE 0
        END
    ) AS accepted_light_integration_s
FROM observations o
JOIN targets t ON t.id = o.target_id
LEFT JOIN sites s ON s.id = o.site_id
LEFT JOIN frames f ON f.observation_id = o.id
GROUP BY
    o.id,
    o.observation_number,
    o.status,
    t.catalog_id,
    t.name,
    s.name,
    o.started_at,
    o.finished_at;


CREATE VIEW IF NOT EXISTS v_survey_progress AS
SELECT
    s.id AS survey_id,
    s.name,
    COUNT(st.target_id) AS total_targets,
    SUM(CASE WHEN st.status = 'completed' THEN 1 ELSE 0 END) AS completed_targets,
    ROUND(
        100.0 * SUM(CASE WHEN st.status = 'completed' THEN 1 ELSE 0 END)
        / NULLIF(COUNT(st.target_id), 0),
        1
    ) AS completion_pct
FROM surveys s
LEFT JOIN survey_targets st ON st.survey_id = s.id
GROUP BY s.id, s.name;


PRAGMA user_version = 1;
