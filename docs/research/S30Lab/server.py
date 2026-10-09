"""S30Lab local diagnostic GUI for Seestar S30 Pro (seestarpy).
Run: python gui/server.py   (from S30Lab, with venv-seestarpy active)
Localhost only. Do not expose this HTTP server to a network.
"""
import contextlib
import copy
import io
import json
import sqlite3
import csv
import uuid
import secrets
import threading
import time
import os
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / 'telemetry.sqlite3'
SESSION_ID = datetime.now().strftime('%Y%m%dT%H%M%S')
DB_LOCK = threading.RLock()
RECORDING = {"id": None, "name": None, "started_at": None}
POLL_SECONDS = 2
KEY = HERE.parents[1] / 'seestar_alp' / 'firmware-cache' / 'interop.pem'
HOST, PORT = '127.0.0.1', 8765
SEESTAR_HOST = os.environ.get('S30LAB_SEESTAR_HOST', '10.160.154.146')
CAMERA_LIMIT = threading.BoundedSemaphore(2)
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
EVENTS = deque(maxlen=600)
CHANGES = deque(maxlen=500)
RAW_CHANGES = deque(maxlen=1200)
STATE = {'connected': False, 'updated_at': None, 'data': {}, 'error': None}
PREVIOUS = None
SENSITIVE = ('password', 'passwd', 'secret', 'token', 'credential', 'private', 'ssid', 'key_mgmt', 'cpuId', 'serial', 'location_lon_lat', 'gateway', 'netmask', 'mac', 'sn')


# Fixed, reviewed telemetry allowlist. No credentials, location or network IDs.
METRICS = {
    'device_state.pi_status.temp': 'Temperature (C)',
    'device_state.pi_status.battery_temp': 'Battery temperature (C)',
    'device_state.pi_status.battery_capacity': 'Battery (%)',
    'device_state.station.sig_lev': 'Wi-Fi signal (dBm)',
    'device_state.focuser.step': 'MAIN focuser step',
    'device_state.second_focuser.step': 'WIDE focuser step',
    'device_state.balance_sensor.data.angle': 'Tilt angle (deg)',
    'device_state.compass_sensor.data.direction': 'Compass direction (deg)',
}

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            ts TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_samples_metric_time ON samples(metric, ts)')
        db.execute('''CREATE TABLE IF NOT EXISTS session_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            ts TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, started_at TEXT NOT NULL,
            ended_at TEXT)''')
        db.execute('''CREATE TABLE IF NOT EXISTS experiment_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL,
            ts TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_exp_samples ON experiment_samples(experiment_id, id)')
        db.execute('''CREATE TABLE IF NOT EXISTS experiment_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id TEXT NOT NULL,
            ts TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_exp_events ON experiment_events(experiment_id, id)')

def record_samples(flat, timestamp):
    rows = [(SESSION_ID, timestamp, path, float(flat[path])) for path in METRICS
            if type(flat.get(path)) in (int, float)]
    if rows:
        with DB_LOCK, sqlite3.connect(DB_PATH) as db:
            db.executemany('INSERT INTO samples(session_id,ts,metric,value) VALUES(?,?,?,?)', rows)
            if RECORDING['id']:
                db.executemany('INSERT INTO experiment_samples(experiment_id,ts,metric,value) VALUES(?,?,?,?)',
                               [(RECORDING['id'], t, metric, value) for _, t, metric, value in rows])

def read_history(metric, limit=300):
    if metric not in METRICS:
        raise ValueError('Unsupported metric')
    with DB_LOCK, sqlite3.connect(DB_PATH) as db:
        rows = db.execute('SELECT ts,value FROM samples WHERE metric=? ORDER BY id DESC LIMIT ?',
                          (metric, limit)).fetchall()
    return [{'time': t, 'value': v} for t, v in reversed(rows)]

def export_rows():
    with DB_LOCK, sqlite3.connect(DB_PATH) as db:
        return db.execute('SELECT session_id,ts,metric,value FROM samples ORDER BY id').fetchall()


def recording_status():
    with DB_LOCK:
        return dict(RECORDING)

def begin_experiment(name):
    name = str(name).strip()[:80]
    if not name:
        raise ValueError('Experiment name required')
    with DB_LOCK, sqlite3.connect(DB_PATH) as db:
        if RECORDING['id']:
            raise ValueError('Recording already active')
        eid = uuid.uuid4().hex
        ts = datetime.now().isoformat(timespec='seconds')
        db.execute('INSERT INTO experiments(id,name,started_at) VALUES(?,?,?)', (eid,name,ts))
        RECORDING.update(id=eid,name=name,started_at=ts)
        result = dict(RECORDING)
    log('recording', 'Recording started: '+name)
    return result

def end_experiment():
    with DB_LOCK, sqlite3.connect(DB_PATH) as db:
        if not RECORDING['id']:
            raise ValueError('No active recording')
        name = RECORDING['name']
        log_recording_id = RECORDING['id']
        db.execute('UPDATE experiments SET ended_at=? WHERE id=?',
                   (datetime.now().isoformat(timespec='seconds'), log_recording_id))
        RECORDING.update(id=None,name=None,started_at=None)
        result = dict(RECORDING)
    log('recording', 'Recording stopped: '+name)
    return result

def list_experiments():
    with DB_LOCK, sqlite3.connect(DB_PATH) as db:
        rows=db.execute('SELECT id,name,started_at,ended_at FROM experiments ORDER BY started_at DESC LIMIT 100').fetchall()
    return [dict(zip(('id','name','started_at','ended_at'),r)) for r in rows]

def experiment_data(eid):
    with DB_LOCK, sqlite3.connect(DB_PATH) as db:
        info=db.execute('SELECT id,name,started_at,ended_at FROM experiments WHERE id=?',(eid,)).fetchone()
        if not info:
            raise ValueError('Experiment not found')
        events=db.execute('SELECT ts,kind,message FROM experiment_events WHERE experiment_id=? ORDER BY id LIMIT 10000',(eid,)).fetchall()
        samples=db.execute('SELECT ts,metric,value FROM experiment_samples WHERE experiment_id=? ORDER BY id LIMIT 50000',(eid,)).fetchall()
    payload = {'experiment':dict(zip(('id','name','started_at','ended_at'),info)),
            'events':[dict(zip(('time','kind','message'),r)) for r in events],
            'samples':[dict(zip(('time','metric','value'),r)) for r in samples],
            'truncated':len(events)==10000 or len(samples)==50000}
    payload['analysis'] = analyze_experiment(payload)
    return payload

# v0.6 semantic transitions are derived from observed state, never from RPC acceptance.
def semantic_events(previous, current):
    if previous is None:
        return []
    events = []
    def emit(code):
        if code not in events:
            events.append(code)
    pm = (previous.get('device_state') or {}).get('mount') or {}
    cm = (current.get('device_state') or {}).get('mount') or {}
    old_move, move = pm.get('move_type'), cm.get('move_type')
    if move == 'ScopeMoveToHorizon' and old_move != move:
        emit('ARM_OPENING')
    if move == 'ScopeHome' and old_move != move:
        emit('PARKING')
    if cm.get('close') is False and move == 'none' and (pm.get('close') is not False or old_move != 'none'):
        emit('ARM_OPEN')
    if cm.get('close') is True and move == 'none' and (pm.get('close') is not True or old_move != 'none'):
        emit('PARKED')
    pa, ca = previous.get('app_state') or {}, current.get('app_state') or {}
    def working(a, name):
        return (a.get(name) or {}).get('mode') == 'scenery' and (a.get(name) or {}).get('state') == 'working'
    def ready(a, name):
        cam = a.get(name) or {}
        return working(a, name) and (cam.get('RTSP') or {}).get('state') == 'working' and cam.get('stage') == 'RTSP'
    if any(working(ca,n) and not working(pa,n) for n in ('View','SecondView')):
        emit('SCENERY_STARTING')
    for name, label in (('View','MAIN_RTSP_READY'),('SecondView','WIDE_RTSP_READY')):
        if ready(ca,name) and not ready(pa,name):
            emit(label)
    if all(ready(ca,n) for n in ('View','SecondView')) and not all(ready(pa,n) for n in ('View','SecondView')):
        emit('SCENERY_READY')
    if any(working(pa,n) and not working(ca,n) for n in ('View','SecondView')):
        emit('SCENERY_STOPPING')
    if all((ca.get(n) or {}).get('state') in ('cancel','complete','idle') and
           ((ca.get(n) or {}).get('RTSP') or {}).get('state') in ('cancel','complete','idle')
           for n in ('View','SecondView')) and any(working(pa,n) for n in ('View','SecondView')):
        emit('SCENERY_STOPPED')
    return events

# Historical v0.5 recordings can be analyzed without changing their stored rows.
def analyze_experiment(payload):
    import re
    events = payload['events']
    semantic = []
    for e in events:
        if e['kind'] == 'semantic':
            semantic.append({'time':e['time'],'code':e['message']})
    # For old recordings infer semantic milestones from recorded state transitions.
    if not semantic:
        for e in events:
            if e['kind'] != 'state':
                continue
            msg = e['message'].replace('\\.', '.')
            code = None
            if 'mount.move_type:' in msg:
                if '-> ScopeMoveToHorizon' in msg: code='ARM_OPENING'
                elif '-> ScopeHome' in msg: code='PARKING'
                elif '-> none' in msg:
                    # Confirmation also needs close; infer from latest observed close value.
                    pass
            if 'app_state.View.RTSP.state:' in msg and '-> working' in msg: code='MAIN_RTSP_READY'
            if 'app_state.SecondView.RTSP.state:' in msg and '-> working' in msg: code='WIDE_RTSP_READY'
            if code: semantic.append({'time':e['time'],'code':code})
        for i,e in enumerate(events):
            if e['kind']!='state' or 'mount.move_type:' not in e['message'] or '-> none' not in e['message']:continue
            last_close=None
            for prev in events[:i+1]:
                if prev['kind']=='state' and 'mount.close:' in prev['message']:
                    if '-> False' in prev['message']:last_close=False
                    elif '-> True' in prev['message']:last_close=True
            # Initial close state is unknown; never infer a completed movement without it.
            if last_close is not None:
                semantic.append({'time':e['time'],'code':'PARKED' if last_close else 'ARM_OPEN'})
    semantic.sort(key=lambda x:x['time'])
    durations=[]
    for start,end,label in [('ARM_OPENING','ARM_OPEN','Arm opening'),('PARKING','PARKED','Parking'),('SCENERY_STARTING','SCENERY_READY','Scenery ready')]:
        pending=None
        for e in semantic:
            if e['code']==start: pending=e
            elif e['code']==end and pending:
                seconds=round((datetime.fromisoformat(e['time'])-datetime.fromisoformat(pending['time'])).total_seconds(),1)
                if seconds>=0:durations.append({'operation':label,'start':pending['time'],'end':e['time'],'seconds':seconds})
                pending=None
    rejected=[e for e in events if e['kind']=='error' and 'Command failed:' in e['message']]
    return {'milestones':semantic,'durations':durations,'rejected_commands':len(rejected),
            'mount_moving_rejections':sum('Mount is moving' in e['message'] for e in rejected),
            'parked_confirmed':any(e['code']=='PARKED' for e in semantic),
            'analysis_note':'Derived from polled states; timestamps are observation times. Historical inference may be incomplete.'}

def poll_loop():
    while True:
        snapshot()
        time.sleep(POLL_SECONDS)

def log(kind, message):
    ts = datetime.now().isoformat(timespec='seconds')
    msg = str(message)[:500]
    EVENTS.append({'time': ts, 'kind': kind, 'message': msg})
    with DB_LOCK, sqlite3.connect(DB_PATH) as db:
        db.execute('INSERT INTO session_events(session_id,ts,kind,message) VALUES(?,?,?,?)', (SESSION_ID,ts,kind,msg))
        if RECORDING['id']:
            db.execute('INSERT INTO experiment_events(experiment_id,ts,kind,message) VALUES(?,?,?,?)',
                       (RECORDING['id'],ts,kind,msg))

def sanitize(obj):
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items() if not any(s.lower() in k.lower() for s in SENSITIVE)}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj

def flatten(value, prefix=''):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from flatten(v, f'{prefix}.{k}' if prefix else k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from flatten(v, f'{prefix}[{i}]')
    else:
        yield prefix, value

def rpc(method, *args, **kwargs):
    from seestarpy import raw
    # seestarpy prints verbose RPC payloads, including confidential settings.
    # Never forward those raw payloads to the browser or event log.
    with contextlib.redirect_stdout(io.StringIO()):
        response = getattr(raw, method)(*args, **kwargs)
    if not isinstance(response, dict) or response.get('code') != 0:
        raise RuntimeError(f'{method}: RPC code={response.get("code")!r}, result_type={type(response.get("result")).__name__}' if isinstance(response, dict) else f'{method}: invalid RPC response type')
    return response.get('result')

# Small numerical fluctuations remain visible in the live inspector but are
# suppressed in the significant-change stream.
NOISY_PREFIXES = (
    'device_state.balance_sensor.data.',
    'device_state.compass_sensor.data.',
)
THRESHOLDS = {
    'device_state.pi_status.temp': 0.5,
    'device_state.pi_status.battery_temp': 0.5,
    'device_state.pi_status.battery_capacity': 1,
    'device_state.station.sig_lev': 5,
}

def significant(path, old, new):
    if old == new:
        return False
    if path.startswith(NOISY_PREFIXES):
        return False
    threshold = THRESHOLDS.get(path)
    if threshold is not None and type(old) in (int, float) and type(new) in (int, float):
        return abs(new - old) >= threshold
    return True

def snapshot():
    global PREVIOUS
    with LOCK:
        try:
            device = rpc('get_device_state')
            app = rpc('iscope_get_app_state')
            if not isinstance(device, dict) or not isinstance(app, dict):
                raise RuntimeError('Invalid device/app state')
            data = sanitize({'device_state': device, 'app_state': app})
            flat = dict(flatten(data))
            old_state = STATE['data'] if STATE['connected'] else None
            if PREVIOUS is not None:
                for path in sorted(set(flat) | set(PREVIOUS)):
                    old, new = PREVIOUS.get(path), flat.get(path)
                    if old == new:
                        continue
                    entry = {'time': datetime.now().isoformat(timespec='seconds'),
                             'path': path, 'old': old, 'new': new}
                    RAW_CHANGES.append(entry)
                    if significant(path, old, new):
                        CHANGES.append(entry)
                        # Only discrete changes are events. Continuous telemetry
                        # belongs to the changes panel, not the event log.
                        if not (type(old) in (int, float) and type(new) in (int, float)):
                            log('state', f'{path}: {str(old)[:70]} -> {str(new)[:70]}')
            for code in semantic_events(old_state, data):
                log('semantic', code)
            record_samples(flat, datetime.now().isoformat(timespec="milliseconds"))
            PREVIOUS = flat
            if not STATE['connected']:
                log('info', 'Connected to Seestar')
            STATE.update(connected=True, updated_at=datetime.now().isoformat(timespec='seconds'), data=data, error=None)
        except Exception as exc:
            if STATE['connected'] or STATE['error'] != str(exc):
                log('error', f'Connection/read failed: {exc}')
            STATE.update(connected=False, error=str(exc))
        return copy.deepcopy(STATE)

def camera_stopped(app):
    for name in ('View', 'SecondView'):
        cam = app.get(name) or {}
        rtsp = cam.get('RTSP') or {}
        if cam.get('state') not in ('cancel', 'complete', 'idle'):
            return False
        if rtsp.get('state') not in ('cancel', 'complete', 'idle'):
            return False
    return True

def set_heater(enabled):
    with LOCK:
        before = rpc("get_device_state")
        setting = before.get("setting", {})
        if not isinstance(setting.get("heater_enable"), bool):
            raise RuntimeError("Heater state unavailable")
        if setting["heater_enable"] != enabled:
            rpc("pi_output_set2", enabled, 30 if enabled else 0)
        after = rpc("get_device_state")
        actual = after.get("setting", {}).get("heater_enable")
        if actual is not enabled:
            raise RuntimeError("Heater change not verified")
        log("action", f"Heater verified: {enabled}")
        return actual

def act(action):
    with LOCK:
        mount = rpc('get_device_state').get('mount', {})
        if mount.get('move_type') != 'none':
            raise RuntimeError('Mount is moving; command refused')
        if action == 'open':
            if mount.get('close') is True:
                rpc('scope_move_to_horizon')
            elif mount.get('close') is not False:
                raise RuntimeError('Unknown arm state; command refused')
        elif action == 'stop':
            rpc('iscope_stop_view')
        elif action == 'park':
            app = rpc('iscope_get_app_state')
            if not camera_stopped(app):
                raise RuntimeError('Stop both cameras before parking')
            if mount.get('close') is False:
                rpc('scope_park')
            elif mount.get('close') is not True:
                raise RuntimeError('Unknown arm state; command refused')
        elif action == 'scenery':
            if mount.get('close') is not False:
                raise RuntimeError('Open arm before starting Scenery')
            app = rpc('iscope_get_app_state')
            for name in ('View', 'SecondView'):
                if (app.get(name) or {}).get('state') == 'working':
                    raise RuntimeError('Camera already active; stop current mode first')
            from seestarpy import raw
            cmd = {'method': 'iscope_start_view', 'params': {'mode': 'scenery', 'target_ra_dec': [None, None], 'target_name': 'Unknown', 'lp_filter': False, 'cam_id': 1}}
            with contextlib.redirect_stdout(io.StringIO()):
                response = raw.send_command(cmd)
            if not isinstance(response, dict) or response.get('code') != 0 or response.get('result') != 0:
                raise RuntimeError('Scenery start rejected')
        else:
            raise ValueError('Unsupported action')
        log('action', f'Accepted command: {action} (verify final state in telemetry)')

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def reply(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    def camera_stream(self, cam):
        if cam not in ('main', 'wide'):
            return self.reply(400, {'error': 'Unknown camera'})
        if not CAMERA_LIMIT.acquire(blocking=False):
            return self.reply(429, {'error': 'Two camera streams already active'})
        cap = None
        try:
            try:
                import cv2
            except ImportError:
                return self.reply(503, {'error': 'Install opencv-python-headless'})
            port = 4554 if cam == 'main' else 4555
            key = 'View' if cam == 'main' else 'SecondView'
            with LOCK:
                state = copy.deepcopy(STATE)
            entry = (state.get('data', {}).get('app_state') or {}).get(key) or {}
            if not (state.get('connected') and entry.get('mode') == 'scenery' and entry.get('state') == 'working' and (entry.get('RTSP') or {}).get('state') == 'working'):
                return self.reply(409, {'error': 'RTSP not ready'})
            cap = cv2.VideoCapture(f'rtsp://{SEESTAR_HOST}:{port}/stream', cv2.CAP_FFMPEG)
            if not cap.isOpened():
                return self.reply(503, {'error': 'RTSP stream unavailable'})
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            while True:
                with LOCK:
                    st = STATE
                    a = (st.get('data', {}).get('app_state') or {}).get(key) or {}
                    ready = st.get('connected') and a.get('mode') == 'scenery' and a.get('state') == 'working' and (a.get('RTSP') or {}).get('state') == 'working'
                if not ready:
                    break
                ok, frame = cap.read()
                if not ok:
                    break
                # Keep the browser preview lightweight; no image data stored in SQLite.
                height, width = frame.shape[:2]
                if width > 960:
                    frame = cv2.resize(frame, (960, int(height * 960 / width)))
                encoded_ok, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
                if not encoded_ok:
                    break
                jpg = buffer.tobytes()
                self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' + str(len(jpg)).encode() + b'\r\n\r\n' + jpg + b'\r\n')
                self.wfile.flush()
                time.sleep(0.15)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as exc:
            log('error', f'Camera preview {cam} stopped: {type(exc).__name__}')
        finally:
            if cap is not None:
                cap.release()
            CAMERA_LIMIT.release()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/camera.mjpg':
            cam = parse_qs(urlparse(self.path).query).get('cam', [''])[0]
            return self.camera_stream(cam)
        if path == '/api/state':
            with LOCK:
                state = copy.deepcopy(STATE)
            return self.reply(200, {'state': state, 'events': list(EVENTS)[-120:], 'changes': list(CHANGES)[-120:], 'raw_changes': list(RAW_CHANGES)[-120:], 'token': TOKEN})
        if path == '/api/experiments':
            return self.reply(200, {'recording':recording_status(), 'experiments':list_experiments()})
        if path == '/api/experiment':
            eid = parse_qs(urlparse(self.path).query).get('id',[''])[0]
            try:
                return self.reply(200, experiment_data(eid))
            except ValueError as exc:
                return self.reply(404, {'error':str(exc)})
        if path == '/api/metrics':
            return self.reply(200, {'metrics': METRICS, 'session_id': SESSION_ID, 'poll_seconds': POLL_SECONDS})
        if path == '/api/history':
            query = parse_qs(urlparse(self.path).query)
            metric = query.get('metric', ['device_state.pi_status.temp'])[0]
            try:
                limit = min(2000, max(1, int(query.get('limit', ['300'])[0])))
                return self.reply(200, {'metric': metric, 'points': read_history(metric, limit)})
            except (ValueError, TypeError) as exc:
                return self.reply(400, {'error': str(exc)})
        if path == '/api/export.csv':
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(['session_id', 'timestamp', 'metric', 'value'])
            writer.writerows(export_rows())
            body = output.getvalue().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="s30lab_telemetry.csv"')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path in ('/', '/index.html', '/app.js'):
            name = 'index.html' if path == '/' else path.lstrip('/')
            body = (HERE / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self.reply(404, {'error': 'Not found'})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ('/api/action', '/api/recording'):
            return self.reply(404, {'error': 'Not found'})
        origin = self.headers.get('Origin')
        if origin not in (None, f'http://{HOST}:{PORT}'):
            return self.reply(403, {'error': 'Invalid origin'})
        if self.headers.get('X-S30Lab-Token') != TOKEN:
            return self.reply(403, {'error': 'Invalid token'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 2048:
                raise ValueError('Invalid request size')
            payload = json.loads(self.rfile.read(length))
            action = payload.get('action')
            if path == '/api/recording':
                if action == 'start':
                    return self.reply(200, {'ok':True, 'recording':begin_experiment(payload.get('name',''))})
                if action == 'stop':
                    return self.reply(200, {'ok':True, 'recording':end_experiment()})
                raise ValueError('Unsupported recording action')
            if action not in ('open', 'park', 'scenery', 'stop', 'heater_on', 'heater_off'):
                raise ValueError('Unsupported action')
            log('request', f'Command requested: {action}')
            if action in ("heater_on", "heater_off"):
                set_heater(action == "heater_on")
            else:
                act(action)
            self.reply(200, {'ok': True, 'message': f'{action} accepted; watch telemetry'})
        except Exception as exc:
            log('error', f'Command failed: {str(exc)[:200]}')
            self.reply(400, {'ok': False, 'error': str(exc)[:200]})

def main():
    if not KEY.is_file():
        raise SystemExit(f'RSA key not found: {KEY}')
    from seestarpy import auth
    auth.set_key_path(str(KEY))
    init_db()
    log('info', 'S30Lab GUI started')
    threading.Thread(target=poll_loop, daemon=True, name='seestar-telemetry').start()
    print(f'S30Lab GUI: http://{HOST}:{PORT}')
    print('Local access only. Ctrl+C to stop.')
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopping GUI server')
    finally:
        if RECORDING['id']:
            end_experiment()
        server.server_close()

if __name__ == '__main__':
    main()
