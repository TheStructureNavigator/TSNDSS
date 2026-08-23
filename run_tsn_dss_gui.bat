@echo off
setlocal

set "TSN_ROOT=%~dp0"
set "TSN_DSS_SIRIL_EXECUTABLE=C:\Program Files\Siril\bin\siril-cli.exe"

start "TSN DSS API" cmd /k "cd /d "%TSN_ROOT%" && py -m tsn_dss.gui.http_api --projects-root projects"
start "TSN DSS Frontend" cmd /k "cd /d "%TSN_ROOT%frontend" && npm run dev"

endlocal
