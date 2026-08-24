# TSN Deep Space System

TSN DSS to mały, działający fundament domenowy dla prywatnego systemu obserwacyjnego i astrofotograficznego.

Aktualny cel v0.1 został zamknięty:

`Target -> AcquisitionPlan -> Observation -> Frames -> Dataset -> ProcessingRun -> Output`

## Struktura repo

- `tsn_dss/domain/` — modele domenowe
- `tsn_dss/engine/sqlite/` — aktualny engine oparty o SQLite
- `tsn_dss/engine/projects.py` — układ projektów, capture i workspace pod processing
- `tsn_dss/engine/siril.py` — minimalne podpięcie Sirila jako zewnętrznego engine
- `frontend/` — GUI webowe oparte o Vite + TypeScript
- `tsn_dss/gui/` — zarezerwowane miejsce pod przyszłe GUI
- `tsn_dss/__init__.py` — publiczne API pakietu
- `tests/` — testy `unittest`
- `sqlite/` — schemat SQLite, seed, przykładowe query, ERD i przykładowa baza
- `projects/` — lokalne projekty, raw capture i artefakty processingu

## Co już działa

- inicjalizacja i kontrola wersji SQLite
- CRUD dla target, site, equipment, plan i sequence
- observation lifecycle i assignment equipment
- frame review oraz walidacje domenowe
- dataset z `dataset_observations` i `dataset_frames`
- processing run z wersjonowaniem i outputem
- storage projektu z odseparowaniem raw capture od run workspace
- headless uruchamianie Sirila przez `siril-cli -d ... -s ...`
- frontend shell pod przyszłe GUI i integrację z Aladin Lite
- lokalne HTTP API dla GUI: health + listing projektów

## Jak uruchomić testy

```bash
py -m pip install -r requirements.txt
py -m unittest discover -s tests -v
```

## Jak odpalić lokalne GUI

W jednym terminalu uruchom lokalne API:

```bash
py -m pip install -r requirements.txt
py -m tsn_dss.gui.http_api --projects-root projects
```

W drugim terminalu uruchom frontend:

```bash
cd frontend
npm run dev
```

Albo po prostu uruchom z root repo:

```bash
run_tsn_dss_gui.bat
```

Przed uruchomieniem GUI możesz ustawić ścieżkę do lokalnego Sirila w:

- [run_tsn_dss_gui.bat](C:\Users\treze\OneDrive\Desktop\TSN_DSS\run_tsn_dss_gui.bat)

Linia:

```bat
set "TSN_DSS_SIRIL_EXECUTABLE=C:\PATH\TO\siril-cli.exe"
```

Ta wartość będzie automatycznie podstawiana w formularzu uruchamiania runu.

Aktualnie z GUI możesz już:

- utworzyć nowy projekt,
- zaimportować istniejący folder capture zawierający `biases/`, `darks/`, `flats/`, `lights/`,
- podejrzeć szczegóły projektu i listę capture / runów,
- przeglądać capture w siatce miniaturek ładowanych z lokalnego cache.

## Gdzie patrzeć dalej

- model SQLite: [sqlite/README.md](./sqlite/README.md)
- publiczne API modułu: [tsn_dss/__init__.py](./tsn_dss/__init__.py)
- engine SQLite: [tsn_dss/engine/sqlite](./tsn_dss/engine/sqlite)
- modele domenowe: [tsn_dss/domain/models.py](./tsn_dss/domain/models.py)
- test pełnego workflow: [tests/test_processing_repository.py](./tests/test_processing_repository.py)

## Uwaga

Repo jest nadal świadomie małe. Nie ma tu jeszcze CLI, GUI ani integracji sprzętowych. Kolejne kroki można budować na aktualnym modelu bez przebudowy fundamentów.
