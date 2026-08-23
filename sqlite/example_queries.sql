PRAGMA foreign_keys = ON;

-- 1) Planned/finished observations
SELECT *
FROM v_observation_summary
ORDER BY observation_number;

-- 2) Survey progress
SELECT *
FROM v_survey_progress;

-- 3) Best accepted light frames for one observation
SELECT
    id,
    file_path,
    exposure_s,
    fwhm_arcsec,
    eccentricity,
    guiding_rms_arcsec
FROM frames
WHERE observation_id = 'obs:0001'
  AND frame_type = 'light'
  AND accepted = 1
ORDER BY
    fwhm_arcsec IS NULL ASC,
    fwhm_arcsec ASC,
    eccentricity IS NULL ASC,
    eccentricity ASC;

-- 4) Total accepted integration by target
SELECT
    t.catalog_id,
    t.name,
    ROUND(
        SUM(
            CASE
                WHEN f.frame_type = 'light'
                 AND f.accepted = 1
                THEN COALESCE(f.exposure_s, 0)
                ELSE 0
            END
        ) / 3600.0,
        2
    ) AS accepted_integration_hours
FROM targets t
JOIN observations o ON o.target_id = t.id
LEFT JOIN frames f ON f.observation_id = o.id
GROUP BY t.id
ORDER BY accepted_integration_hours DESC;

-- 5) Correlation-ready frame quality dataset
SELECT
    captured_at,
    guiding_rms_arcsec,
    fwhm_arcsec,
    eccentricity,
    star_count,
    background_median,
    camera_temp_c
FROM frames
WHERE observation_id = 'obs:0001'
  AND frame_type = 'light'
ORDER BY captured_at;

-- 6) Guiding RMS telemetry time series
SELECT
    measured_at,
    value_real AS guiding_rms_arcsec
FROM telemetry
WHERE observation_id = 'obs:0001'
  AND metric = 'guiding.total_rms_arcsec'
ORDER BY measured_at;

-- 7) Processing history of a dataset
SELECT
    version_label,
    engine_name,
    engine_version,
    status,
    final_image_path,
    started_at,
    finished_at
FROM processing_runs
WHERE dataset_id = 'dataset:m42:first-light'
ORDER BY created_at;
