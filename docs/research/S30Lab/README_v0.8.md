# S30Lab GUI v0.8 — navigation + dual live camera preview

Upgrade: stop the running server; replace ONLY `server.py`, `app.js`, `index.html` in `S30Lab/gui`. **Keep `telemetry.sqlite3`**. Run `python gui/server.py` and hard-refresh the page (Ctrl+F5).

The navigation splits Dashboard, Live Cameras, Telemetry, Experiments, Inspector, and System Logs. Existing control, session recording, state machine, watchlist and history remain in place.

The Live Cameras page offers opt-in browser MJPEG previews for MAIN (RTSP 4554) and WIDE (RTSP 4555). It does not start Scenery or move the telescope. Start Scenery separately when the mount is stationary and physically clear. The preview only opens when the polled state reports RTSP working. The server binds to localhost and proxies RTSP; the camera IP defaults to `10.160.154.146` and can be overridden by setting environment variable `S30LAB_SEESTAR_HOST` before starting the server. Requires `opencv-python-headless` in the active Python environment. Two simultaneous previews maximum. Image frames are not saved.

Known limits: MJPEG is CPU/network intensive; the preview uses an individual RTSP decoder per browser image. If a camera stream drops, stop/restart preview after RTSP recovers. This is not a full capture or exposure control interface. No physical telescope integration was available for this package test.
