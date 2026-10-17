#!/usr/bin/env python3
"""Run only the standalone NSURLSession probe, against a loopback HTTP fixture.

Uses a fresh private DPREFIX and never shuts down the user's normal prefix.
No Riot processes, account data, client APIs or client input are used.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
from threading import Thread
import time

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--probe', type=Path, default=root / 'build/riot-urlsession-check')
parser.add_argument('--frameworks', type=Path, help='Host directory containing the installed framework overlays')
parser.add_argument('--libraries', type=Path, help='Host directory containing the installed dylib overlays')
parser.add_argument('--log', type=Path, default=root / 'build/urlsession-check.log')
args = parser.parse_args()
if not args.probe.is_file():
    parser.error('build the probe first')
config_file = root / 'build/config.json'
config = json.loads(config_file.read_text()) if config_file.exists() else {}
guest_user = config.get('guest_user', os.environ.get('USER', ''))
runtime = Path.home() / '.darling/Users' / guest_user
frameworks = args.frameworks or runtime / 'RuntimeFrameworks'
libraries = args.libraries or runtime / 'RuntimeLibraries'


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(0.05)  # Ensure finishTasksAndInvalidate sees an in-flight task.
        body = b'darling-session-ok'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *unused):
        pass


server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
reserved = socket.socket()
reserved.bind(('127.0.0.1', 0))  # Reserved but not listening: deterministic connection refusal.
Thread(target=server.serve_forever, daemon=True).start()
(root / 'build').mkdir(exist_ok=True)
prefix = Path(tempfile.mkdtemp(prefix='urlsession-prefix-', dir=root / 'build'))
env = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'DISPLAY',
       'XAUTHORITY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'LANG') if key in os.environ}
env.update(DPREFIX=str(prefix), EGL_PLATFORM='x11', LC_ALL='en_US.UTF-8',
           DYLD_FRAMEWORK_PATH='/Volumes/SystemRoot' + str(frameworks.resolve()),
           DYLD_LIBRARY_PATH='/Volumes/SystemRoot' + str(libraries.resolve()))
log = args.log
result = None
try:
    with log.open('w') as stream:
        os.chmod(log, 0o600)
        result = subprocess.run(['darling', str(args.probe.resolve()),
                                 f'http://127.0.0.1:{server.server_port}/payload',
                                 f'http://127.0.0.1:{reserved.getsockname()[1]}/payload'],
                                env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=90)
    text = log.read_text(errors='replace')
    for line in text.splitlines():
        if line.startswith(('LIFECYCLE_PASS:', 'URLSESSION_PASS:', 'LIFECYCLE_FAIL', 'FAIL ', 'TIMEOUT')):
            print(line)
    if result.returncode or 'LIFECYCLE_PASS:' not in text or 'URLSESSION_PASS:' not in text:
        raise SystemExit(f'Probe failed (exit {result.returncode}); inspect private log {log}')
finally:
    server.shutdown()
    server.server_close()
    reserved.close()
    try:
        stopped = subprocess.run(['darling', 'shutdown'], env=env, capture_output=True, timeout=25)
        if stopped.returncode == 0:
            shutil.rmtree(prefix)
        else:
            print('Private test prefix retained for inspection:', prefix)
    except (OSError, subprocess.SubprocessError):
        print('Private test prefix retained for inspection:', prefix)
