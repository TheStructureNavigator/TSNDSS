# TSN Deep Space System v0.1 — ERD

```mermaid
erDiagram
    TARGETS ||--o{ ACQUISITION_PLANS : has
    TARGETS ||--o{ OBSERVATIONS : observed_as
    TARGETS ||--o{ DATASETS : produces
    TARGETS ||--o{ SURVEY_TARGETS : belongs_to

    SITES ||--o{ OBSERVATIONS : hosts

    ACQUISITION_PLANS ||--o{ ACQUISITION_SEQUENCES : contains
    ACQUISITION_PLANS ||--o{ OBSERVATIONS : executed_by

    OBSERVATIONS ||--o{ OBSERVATION_EQUIPMENT : uses
    EQUIPMENT ||--o{ OBSERVATION_EQUIPMENT : assigned_as

    OBSERVATIONS ||--o{ FRAMES : captures
    ACQUISITION_SEQUENCES ||--o{ FRAMES : creates
    EQUIPMENT ||--o{ FRAMES : filter

    DATASETS ||--o{ DATASET_OBSERVATIONS : aggregates
    OBSERVATIONS ||--o{ DATASET_OBSERVATIONS : contributes_to
    DATASETS ||--o{ DATASET_FRAMES : materializes
    FRAMES ||--o{ DATASET_FRAMES : included_in

    DATASETS ||--o{ PROCESSING_RUNS : processed_by

    SURVEYS ||--o{ SURVEY_TARGETS : tracks
    DATASETS ||--o{ SURVEY_TARGETS : best_result

    OBSERVATIONS ||--o{ EVENTS : emits
    OBSERVATIONS ||--o{ TELEMETRY : measures
```

## Core idea

Najważniejszy artefakt v0.1 to `dataset`, nie finalny obraz.

- `dataset_observations` opisuje pochodzenie materiału z sesji
- `dataset_frames` opisuje dokładny skład datasetu
- `processing_runs` pozwalają wielokrotnie przetwarzać ten sam dataset

To rozdzielenie jest kluczowe dla odtwarzalności workflow.
