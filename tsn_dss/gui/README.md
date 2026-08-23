# TSN DSS GUI

Ten katalog jest przygotowany pod przyszłą warstwę GUI.

Na tym etapie katalog pozostaje lekki celowo.

Aktualne webowe GUI zostało wydzielone do:

- `frontend/` — Vite + TypeScript, bez frameworka

Ten katalog `tsn_dss/gui/` zawiera teraz także minimalne lokalne HTTP API:

- `http_api.py` — endpointy `GET /api/health` oraz `GET /api/projects`

To API jest celowo cienkie i służy obecnie jako most między frontendem a lokalnym `engine`.

Uruchomienie:

```bash
py -m tsn_dss.gui.http_api --projects-root projects
```

Ten katalog `tsn_dss/gui/` można dalej traktować jako miejsce na:

- ewentualne pythonowe adaptery GUI
- integrację aplikacyjną między frontendem a `engine`
- przyszłą warstwę desktopową, jeśli kiedykolwiek wejdzie w zakres

Gdy GUI naprawdę wejdzie do zakresu prac, można tu wydzielić osobny frontend albo lekką warstwę desktop/web bez ruszania bieżącego `engine`.
