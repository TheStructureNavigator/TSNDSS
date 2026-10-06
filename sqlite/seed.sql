PRAGMA foreign_keys = ON;

-- ============================================================
-- TSN DSS v0.1 seed/demo data
-- No precise private coordinates are included.
-- ============================================================

INSERT OR IGNORE INTO targets (
    id, catalog, catalog_id, name, object_type,
    ra_deg, dec_deg, angular_major_arcmin, angular_minor_arcmin,
    distance_ly, constellation
) VALUES
(
    'target:m31', 'MESSIER', 'M31', 'Andromeda Galaxy', 'galaxy',
    10.6847, 41.2692, 190.0, 60.0,
    2537000, 'Andromeda'
),
(
    'target:m42', 'MESSIER', 'M42', 'Orion Nebula', 'emission_nebula',
    83.8221, -5.3911, 85.0, 60.0,
    1344, 'Orion'
),
(
    'target:m45', 'MESSIER', 'M45', 'Pleiades', 'open_cluster_reflection_nebula',
    56.75, 24.1167, 110.0, 110.0,
    444, 'Taurus'
);

INSERT OR IGNORE INTO sites (
    id, name, sqm_mag_arcsec2, bortle_class, south_horizon_open, notes
) VALUES
(
    'site:tsn-01',
    'TSN Observation Site 01',
    21.27,
    4,
    1,
    'Candidate rural field site. Exact coordinates intentionally not stored in seed data.'
);

INSERT OR IGNORE INTO equipment (
    id, equipment_type, manufacturer, model, properties_json, notes
) VALUES
(
    'mount:sagti-001',
    'mount',
    'Sky-Watcher',
    'Star Adventurer GTi',
    json_object(
        'mount_type', 'equatorial_goto',
        'tracking', 1,
        'guiding_capable', 1,
        'payload_kg_nominal', 5
    ),
    'Planned v0.1 mount'
),
(
    'telescope:sw72ed-001',
    'telescope',
    'Sky-Watcher',
    'Evostar 72ED',
    json_object(
        'optical_design', 'refractor_ed',
        'aperture_mm', 72,
        'focal_length_mm', 420,
        'f_ratio', 5.83
    ),
    'Planned v0.1 telescope'
),
(
    'camera:canon600d-001',
    'camera',
    'Canon',
    'EOS 600D',
    json_object(
        'sensor_type', 'APS-C CMOS',
        'sensor_width_mm', 22.3,
        'sensor_height_mm', 14.9,
        'resolution_x', 5184,
        'resolution_y', 3456,
        'pixel_size_um', 4.3
    ),
    'Example/planned DSLR for first light'
),
(
    'filter:l-pro-001',
    'filter',
    'Optolong',
    'L-Pro',
    json_object(
        'broadband', 1
    ),
    'Example broadband filter for first light'
);

INSERT OR IGNORE INTO acquisition_plans (
    id, target_id, name, description, status
) VALUES
(
    'plan:m42:first-light',
    'target:m42',
    'M42 First Light',
    'Initial TSN DSS first-light acquisition plan: longer subs for nebulosity and short subs for core.',
    'ready'
);

INSERT OR IGNORE INTO acquisition_sequences (
    plan_id, sequence_order, name, frame_type,
    exposure_s, frame_count, iso, filter_id
) VALUES
(
    'plan:m42:first-light', 10, 'Nebulosity', 'light',
    30, 120, 800, 'filter:l-pro-001'
),
(
    'plan:m42:first-light', 20, 'Core', 'light',
    5, 40, 800, 'filter:l-pro-001'
);


INSERT OR IGNORE INTO sessions (
    id, title, state, started_at, operator_id, site_id, notes
) VALUES
(
    'session:seed-m42-first-light',
    'Seed M42 first light session',
    'planned',
    '2026-01-14 20:00:00+00:00',
    NULL,
    'site:tsn-01',
    'Seed/demo Session for obs:0001. This is not the legacy compatibility bucket.'
);

INSERT OR IGNORE INTO observations (
    id, observation_number, session_id, target_id, site_id,
    acquisition_plan_id, status, started_at, finished_at, operator_notes
) VALUES
(
    'obs:0001',
    1,
    'session:seed-m42-first-light',
    'target:m42',
    'site:tsn-01',
    'plan:m42:first-light',
    'completed',
    '2026-01-14 20:10:00',
    '2026-01-14 22:45:00',
    'TSN DSS Observation #0001 — completed first light run.'
);

INSERT OR IGNORE INTO observation_equipment (
    observation_id, equipment_id, role
) VALUES
('obs:0001', 'mount:sagti-001', 'mount'),
('obs:0001', 'telescope:sw72ed-001', 'main_telescope'),
('obs:0001', 'camera:canon600d-001', 'main_camera');

-- Demo Project and Capture that own the demo frames below. They exist only in the database
-- (there are no files on disk for this demo data).
INSERT OR IGNORE INTO projects (
    id, display_name, dir_key, target_label
) VALUES
(
    'project:demo-m42',
    'M42 first light (demo)',
    'M42FirstLight',
    'M42'
);

INSERT OR IGNORE INTO captures (
    id, project_id, name, rel_path, source_kind
) VALUES
(
    'capture:demo-m42-first-light',
    'project:demo-m42',
    'M42FirstLight',
    'captures/M42FirstLight',
    'legacy_registered'
);

INSERT OR IGNORE INTO frames (
    project_id, capture_id, observation_id, sequence_id, frame_type, rel_path, captured_at,
    exposure_s, iso, filter_id, accepted, rejection_reason,
    fwhm_arcsec, eccentricity, star_count, guiding_rms_arcsec, camera_temp_c
) VALUES
(
    'project:demo-m42',
    'capture:demo-m42-first-light',
    'obs:0001',
    (SELECT id FROM acquisition_sequences WHERE plan_id = 'plan:m42:first-light' AND sequence_order = 10),
    'light',
    'captures/M42FirstLight/lights/light_0030_a.fit',
    '2026-01-14 21:01:00',
    30,
    800,
    'filter:l-pro-001',
    1,
    NULL,
    2.1,
    0.42,
    154,
    0.78,
    -4.5
),
(
    'project:demo-m42',
    'capture:demo-m42-first-light',
    'obs:0001',
    (SELECT id FROM acquisition_sequences WHERE plan_id = 'plan:m42:first-light' AND sequence_order = 10),
    'light',
    'captures/M42FirstLight/lights/light_0030_reject.fit',
    '2026-01-14 21:07:00',
    30,
    800,
    'filter:l-pro-001',
    0,
    'Clouds',
    4.8,
    0.71,
    62,
    1.95,
    -4.4
),
(
    'project:demo-m42',
    'capture:demo-m42-first-light',
    'obs:0001',
    (SELECT id FROM acquisition_sequences WHERE plan_id = 'plan:m42:first-light' AND sequence_order = 20),
    'light',
    'captures/M42FirstLight/lights/light_0005_a.fit',
    '2026-01-14 21:43:00',
    5,
    800,
    'filter:l-pro-001',
    1,
    NULL,
    1.9,
    0.39,
    168,
    0.74,
    -4.2
),
(
    'project:demo-m42',
    'capture:demo-m42-first-light',
    'obs:0001',
    (SELECT id FROM acquisition_sequences WHERE plan_id = 'plan:m42:first-light' AND sequence_order = 20),
    'light',
    'captures/M42FirstLight/lights/light_0005_pending.fit',
    '2026-01-14 21:47:00',
    5,
    800,
    'filter:l-pro-001',
    NULL,
    NULL,
    NULL,
    NULL,
    NULL,
    0.81,
    -4.2
);

INSERT OR IGNORE INTO datasets (
    id, target_id, name, status, total_light_integration_s, notes
) VALUES
(
    'dataset:m42:first-light',
    'target:m42',
    'M42 First Light Dataset',
    'complete',
    35,
    'Accepted light frames from Observation #0001.'
);

INSERT OR IGNORE INTO dataset_observations (
    dataset_id, observation_id
) VALUES
(
    'dataset:m42:first-light',
    'obs:0001'
);

INSERT OR IGNORE INTO dataset_frames (
    dataset_id, frame_id
) VALUES
(
    'dataset:m42:first-light',
    (SELECT id FROM frames WHERE observation_id = 'obs:0001' AND rel_path ='captures/M42FirstLight/lights/light_0030_a.fit')
),
(
    'dataset:m42:first-light',
    (SELECT id FROM frames WHERE observation_id = 'obs:0001' AND rel_path ='captures/M42FirstLight/lights/light_0005_a.fit')
);

INSERT OR IGNORE INTO processing_runs (
    id, dataset_id, version_label,
    engine_name, engine_version,
    pipeline_json, parameters_json,
    status, started_at, finished_at, notes
) VALUES
(
    'processing:m42:first-light:v001',
    'dataset:m42:first-light',
    'v001',
    'Siril',
    NULL,
    json_array(
        'calibration',
        'registration',
        'stacking',
        'background_extraction',
        'color_calibration',
        'stretch',
        'noise_reduction'
    ),
    json_object(
        'background_extraction', 1,
        'stretch', 'asinh'
    ),
    'completed',
    '2026-01-14 23:10:00',
    '2026-01-14 23:24:00',
    'Initial completed processing run.'
);

UPDATE processing_runs
SET
    linear_stack_path = 'output/m42_first_light_linear.fit',
    preview_path = 'output/m42_first_light_preview.jpg',
    final_image_path = 'output/m42_first_light_final.tif'
WHERE id = 'processing:m42:first-light:v001';

INSERT OR IGNORE INTO surveys (
    id, name, catalog, description, status
) VALUES
(
    'survey:messier',
    'TSN Messier Survey',
    'MESSIER',
    'Long-term TSN DSS challenge: photograph all 110 Messier objects.',
    'active'
);

-- v0.1 demo includes the first three favorite targets.
-- Full Messier 110 catalog can be seeded later from a verified catalog source.
INSERT OR IGNORE INTO survey_targets (
    survey_id, target_id, target_order, status, best_dataset_id
) VALUES
('survey:messier', 'target:m31', 31, 'planned', NULL),
('survey:messier', 'target:m42', 42, 'completed', 'dataset:m42:first-light'),
('survey:messier', 'target:m45', 45, 'planned', NULL);
