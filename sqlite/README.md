# TSN Deep Space System v0.1

Minimalny fundament domenowy TSN DSS.

Zakres v0.1 jest wąski i celowy:

`Target -> AcquisitionPlan -> Observation -> Frames -> Dataset -> ProcessingRun -> Output`

Repozytorium zawiera dwa poziomy:

- `sqlite/` — źródło prawdy dla modelu SQLite
- `tsn_dss/` — mała warstwa Python standard library nad SQLite

## Co jest w środku

- `schema.sql` — autorytatywny schemat SQLite v0.1
- `seed.sql` — mały, spójny seed z przykładowym workflow M42
- `tsn_dss_v0_1.db` — przykładowa baza wygenerowana z `schema.sql` i `seed.sql`
- `example_queries.sql` — przykładowe zapytania analityczne
- `ERD.md` — diagram relacji
- `tsn_dss/` — moduły Python dla planning, observation, frames, datasets i processing
- `tests/` — testy `unittest`

## Zasady modelu

- SQLite jest źródłem prawdy.
- Każde połączenie włącza `PRAGMA foreign_keys = ON`.
- Pliki RAW/FITS/TIFF/JPEG nie trafiają do SQLite jako BLOB.
- `dataset` opisuje materiał obserwacyjny.
- `processing_run` opisuje jedną wersję przetwarzania datasetu.
- `dataset_observations` mówi, z jakich sesji pochodzi materiał.
- `dataset_frames` utrwala dokładny skład datasetu.

## Szybki start

Świeża baza:

```bash
sqlite3 tsn_dss_v0_1.db < schema.sql
sqlite3 tsn_dss_v0_1.db < seed.sql
```

Szybkie sprawdzenie:

```sql
PRAGMA foreign_keys = ON;
SELECT * FROM v_observation_summary;
SELECT * FROM v_survey_progress;
```

Testy:

```bash
py -m unittest discover -s tests -v
```

## Aktualny stan

v0.1 ma już działający i przetestowany workflow M42:

- target
- plan z sekwencjami
- observation
- frames z review
- dataset z exact membership
- processing run z outputem

Integracje sprzętowe, GUI, CLI, events i telemetry pozostają poza tym krokiem porządkowym.
