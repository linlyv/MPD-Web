#!/usr/bin/env python3
"""MPD Web Controller"""
import subprocess, re, logging, socket, io, mimetypes, ipaddress, shlex, tempfile, secrets, os, hmac, zipfile, threading, time, json
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from flask import Flask, render_template_string, jsonify, request, send_file, session, redirect, url_for, has_request_context, Response

logging.getLogger('werkzeug').setLevel(logging.ERROR)
app = Flask(__name__)
app.secret_key = os.environ.get('MPD_WEB_SECRET_KEY')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')

@app.before_request
def require_login():
  if request.endpoint == 'login':
    return None
  configured = all(os.environ.get(name) for name in (
    'MPD_WEB_USERNAME', 'MPD_WEB_PASSWORD', 'MPD_WEB_SECRET_KEY'
  ))
  if not configured:
    if request.path.startswith('/api/'):
      return jsonify(ok=False, error='登录未配置，请设置 MPD_WEB_USERNAME、MPD_WEB_PASSWORD 和 MPD_WEB_SECRET_KEY'), 503
    return redirect(url_for('login', next=request.full_path))
  if session.get('authenticated') is True:
    return None
  if request.path.startswith('/api/'):
    return jsonify(ok=False, error='请先登录'), 401
  return redirect(url_for('login', next=request.full_path))

# ---------------- 背景图存储 ----------------
BACKGROUND_DIR = Path(__file__).resolve().parent / 'background'
ADB_PAGE_FILE = Path(__file__).resolve().parent / 'adb.html'
LOGIN_PAGE_FILE = Path(__file__).resolve().parent / 'login.html'
VIDEO_PAGE_FILE = Path(__file__).resolve().parent / 'video.html'
BACKGROUND_MAX_SIZE = 8 * 1024 * 1024
PLAYLIST_MAX_SIZE = 4 * 1024 * 1024
ADB_APK_MAX_SIZE = 256 * 1024 * 1024
BACKGROUND_TYPES = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
    '.bmp': 'image/bmp',
}

def find_background():
    try:
        if not BACKGROUND_DIR.is_dir():
            return None
        for path in sorted(BACKGROUND_DIR.glob('custom.*')):
            if (path.is_file()
                    and path.suffix.lower() in BACKGROUND_TYPES
                    and path.stat().st_size <= BACKGROUND_MAX_SIZE):
                return path
    except OSError:
        return None
    return None

def guess_background_extension(data):
    if data[:3] == b'\xff\xd8\xff':
        return '.jpg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return '.png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return '.webp'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif'
    if data[:2] == b'BM':
        return '.bmp'
    return None

def remove_background_files():
    if not BACKGROUND_DIR.is_dir():
        return
    for old in BACKGROUND_DIR.glob('custom.*'):
        try:
            old.unlink()
        except OSError:
            pass

SERVICE_CONNECTIONS = {
  'mpd': {
    'name': 'MPD',
    'host': os.environ.get('MPD_HOST', '127.0.0.1'),
    'port': os.environ.get('MPD_PORT', '6601'),
  },
  'owntone': {
    'name': 'OwnTone',
    'host': os.environ.get('OWNTONE_HOST', '127.0.0.1'),
    'port': os.environ.get('OWNTONE_PORT', '6600'),
  },
}
OWNTONE_API_PORT = os.environ.get('OWNTONE_API_PORT', '3689')

def get_selected_service():
  selected = session.get('audio_service', 'mpd') if has_request_context() else 'mpd'
  return selected if selected in SERVICE_CONNECTIONS else 'mpd'

def mpc_command(*args, service=None):
  selected = service or get_selected_service()
  connection = SERVICE_CONNECTIONS[selected]
  return [
    'mpc', '-h', connection['host'], '-p', connection['port'], *args
  ]

def mpc(*args):
    try:
        r = subprocess.run(
            mpc_command(*args), capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=30
        )
        return r.stdout
    except: return ''

def run_mpc_checked(*args):
  try:
    result = subprocess.run(
      mpc_command(*args), capture_output=True, text=True,
      encoding='utf-8', errors='replace', timeout=30
    )
  except FileNotFoundError:
    return False, '未找到 mpc 命令'
  except subprocess.TimeoutExpired:
    return False, 'MPD 命令执行超时'
  output = '\n'.join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
  failed = result.returncode != 0 or bool(
    re.search(r'(?im)^(?:error:|ack\s|failed|no such)', output)
  )
  return (False, output or f'mpc {shlex.join(args)} 执行失败') if failed else (True, output)

def owntone_api(method, path, params=None):
  host = SERVICE_CONNECTIONS['owntone']['host']
  if ':' in host and not host.startswith('['):
    host = f'[{host}]'
  url = f'http://{host}:{OWNTONE_API_PORT}{path}'
  if params:
    flat = []
    for k, v in params.items():
      if isinstance(v, list):
        flat.append((k, ','.join(str(item) for item in v)))
      elif isinstance(v, bool):
        flat.append((k, 'true' if v else 'false'))
      else:
        flat.append((k, str(v)))
    query = urlencode(flat)
    if query:
      url += '?' + query
  print(f'[API] {method} {url}', flush=True)
  try:
    req = Request(url, method=method)
    with urlopen(req, timeout=10) as response:
      payload = response.read()
    result = json.loads(payload) if payload else {}
    print(f'[API] → status={response.status} body={json.dumps(result, ensure_ascii=False)[:500]}', flush=True)
    return result
  except HTTPError as error:
    detail = error.read().decode('utf-8', 'replace').strip()
    print(f'[API] ERROR HTTP {error.code}: {detail}', flush=True)
    raise RuntimeError(detail or f'OwnTone API 返回 HTTP {error.code}') from error
  except (URLError, TimeoutError, OSError) as error:
    print(f'[API] ERROR 连接失败: {error}', flush=True)
    raise RuntimeError(f'无法连接 OwnTone JSON API（{OWNTONE_API_PORT}）：{error}') from error

def get_owntone_playlists():
  result = owntone_api('GET', '/api/library/playlists', {'limit': -1})
  return result.get('items', [])

def get_owntone_playlist(identifier):
  match = re.fullmatch(r'owntone:(\d+)', str(identifier))
  if not match:
    return None
  playlist_id = match.group(1)
  return next(
    (playlist for playlist in get_owntone_playlists()
     if str(playlist.get('id')) == playlist_id),
    None,
  )

def normalize_track_uri(uri):
  uri = uri.replace('\\', '/').strip()
  if uri.startswith('file:'):
    uri = uri[5:]
  while uri.startswith('/music/'):
    uri = uri[len('/music/'):]
  return uri

def ensure_owntone_uri(uri):
  if not uri:
    return ''
  uri = uri.strip()
  if uri.startswith('library:') or uri.startswith('file:') or uri.startswith('/music/') or uri.startswith('/'):
    return uri
  normalized = normalize_track_uri(uri)
  if not normalized or normalized.startswith('.'):
    return ''
  return '/music/' + normalized

def owntone_playlist_tracks(playlist):
  result = owntone_api(
    'GET', f"/api/library/playlists/{playlist['id']}/tracks", {'limit': -1}
  )
  return result.get('items', [])

MPD_WATCHDOG_INTERVAL = 15
MPD_WATCHDOG_FAILURE_LIMIT = 3
MPD_WATCHDOG_RESTART_COOLDOWN = 60
MPD_WATCHDOG_RESTART_TIMEOUT = 45

def mpd_watchdog():
  failures = 0
  last_restart = 0
  logger = logging.getLogger('mpd-watchdog')

  while True:
    try:
      check = subprocess.run(
        mpc_command('status', service='mpd'), capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=5
      )
    except FileNotFoundError:
      logger.error('mpc command not found; MPD watchdog stopped')
      return
    except subprocess.TimeoutExpired:
      check = None

    if check is not None and check.returncode == 0:
      if failures:
        logger.warning('MPD connection recovered')
      failures = 0
    else:
      failures += 1
      now = time.monotonic()
      if failures >= MPD_WATCHDOG_FAILURE_LIMIT and now - last_restart >= MPD_WATCHDOG_RESTART_COOLDOWN:
        logger.warning('MPD health check failed %s times; restarting MPD', failures)
        try:
          restart = subprocess.run(
            ['/etc/init.d/mpd', 'restart'], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=MPD_WATCHDOG_RESTART_TIMEOUT
          )
          if restart.returncode == 0:
            logger.warning('MPD restart command completed')
          else:
            detail = (restart.stderr or restart.stdout).strip()
            logger.error('MPD restart failed: %s', detail or f'exit code {restart.returncode}')
        except (OSError, subprocess.TimeoutExpired) as error:
          logger.error('MPD restart could not complete: %s', error)
        last_restart = now
        failures = 0

    time.sleep(MPD_WATCHDOG_INTERVAL)

def get_playlist():
  return [line.strip() for line in mpc('-f', '%file%', 'playlist').splitlines() if line.strip()]

def get_music_directory():
  config = Path('/etc/mpd.conf')
  if config.exists():
    for line in config.read_text(errors='replace').splitlines():
      match = re.match(r'^\s*music_directory\s+(?:"([^"]+)"|(\S+))', line)
      if match:
        return Path(match.group(1) or match.group(2))
  return Path('/var/lib/mpd/music')

def get_playlist_directory():
  config = Path('/etc/mpd.conf')
  if config.exists():
    for line in config.read_text(errors='replace').splitlines():
      match = re.match(r'^\s*playlist_directory\s+(?:"([^"]+)"|(\S+))', line)
      if match:
        return Path(match.group(1) or match.group(2))
  return Path('/var/lib/mpd/playlists')

def music_relative_path(uri):
  value = str(uri or '').replace('\\', '/')
  for prefix in ('file:/music/', '/music/'):
    if value.startswith(prefix):
      return Path(value[len(prefix):])
  return Path(value)

def display_music_uri(uri):
  value = str(uri or '').replace('\\', '/')
  for prefix in ('file:/music/', '/music/'):
    if value.startswith(prefix):
      return value[len(prefix):]
  return value

def fix_owntone_text(text):
  text = str(text or '')
  if not text:
    return text
  try:
    encoded = text.encode('latin-1')
    decoded = encoded.decode('utf-8')
    if decoded != text:
      return decoded
  except (UnicodeDecodeError, UnicodeEncodeError):
    pass
  return text

def run_adb(*args, display_command=None, timeout=20):
  command = ['adb', *args]
  display_command = display_command or shlex.join(command)
  try:
    result = subprocess.run(
      command, capture_output=True, text=True, encoding='utf-8',
      errors='replace', timeout=timeout
    )
  except FileNotFoundError:
    return {'ok': False, 'command': display_command, 'output': '未找到 adb，请确认已安装且在 PATH 中'}, 503
  except subprocess.TimeoutExpired:
    return {'ok': False, 'command': display_command, 'output': 'ADB 命令执行超时'}, 504
  output = '\n'.join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
  command_failed = bool(re.search(r'(?im)^(?:error:|exception|unable to connect|failure)', output))
  ok = result.returncode == 0 and not command_failed
  return {
    'ok': ok,
    'command': display_command,
    'output': output or ('命令执行成功' if ok else 'ADB 命令执行失败'),
    'returncode': result.returncode,
  }, (200 if ok else 502)

def valid_adb_serial(value):
  serial = str(value or '').strip()
  if not serial or len(serial) > 128 or serial.startswith('-'):
    return None
  if not re.fullmatch(r'[A-Za-z0-9_.:%\-\[\]]+', serial):
    return None
  return serial

def adb_json_result(result):
  payload, status = result
  return jsonify(payload), status

@app.route('/adb')
def adb_page():
  return send_file(ADB_PAGE_FILE)

@app.route('/video')
def video_page():
  return send_file(VIDEO_PAGE_FILE)

@app.route('/api/adb/devices')
def api_adb_devices():
  result, status = run_adb('devices', '-l')
  if status != 200:
    return jsonify(result), status
  devices = []
  for line in result['output'].splitlines():
    parts = line.split()
    if len(parts) >= 2 and parts[0] != 'List' and not parts[0].startswith('*'):
      devices.append({'serial': parts[0], 'state': parts[1], 'details': ' '.join(parts[2:])})
  result['devices'] = devices
  return jsonify(result)

@app.route('/api/adb/connect', methods=['POST'])
def api_adb_connect():
  data = request.get_json(silent=True) or {}
  try:
    address = ipaddress.ip_address(str(data.get('host', '')).strip())
    port = int(data.get('port', 5555))
  except (ValueError, TypeError):
    return jsonify(ok=False, error='请输入有效的 IP 地址和端口'), 400
  if not 1 <= port <= 65535:
    return jsonify(ok=False, error='端口范围必须为 1 到 65535'), 400
  host = f'[{address.compressed}]' if address.version == 6 else address.compressed
  return adb_json_result(run_adb('connect', f'{host}:{port}'))

@app.route('/api/adb/disconnect', methods=['POST'])
def api_adb_disconnect():
  data = request.get_json(silent=True) or {}
  serial = valid_adb_serial(data.get('serial'))
  if not serial:
    return jsonify(ok=False, error='请选择有效的设备'), 400
  return adb_json_result(run_adb('disconnect', serial))

@app.route('/api/adb/apps', methods=['POST'])
def api_adb_apps():
  data = request.get_json(silent=True) or {}
  serial = valid_adb_serial(data.get('serial'))
  if not serial:
    return jsonify(ok=False, error='请选择有效的设备'), 400
  args = ['-s', serial, 'shell', 'pm', 'list', 'packages']
  if data.get('third_party'):
    args.append('-3')
  result, status = run_adb(*args)
  if status != 200 or 'error: closed' in (result.get('output') or '').lower():
    args = ['-s', serial, 'shell', 'dumpsys', 'package']
    result, status = run_adb(*args)
    if status != 200:
      return jsonify(result), status
    packages = set()
    for line in result['output'].splitlines():
      m = re.match(r'\s*Package\s+\[([^\]]+)\]', line)
      if m:
        pkg = m.group(1)
        if not data.get('third_party') or (not pkg.startswith('android.') and not pkg.startswith('com.android.') and not pkg.startswith('com.google.')):
          packages.add(pkg)
    result['packages'] = sorted(packages, key=str.casefold)
  else:
    result['packages'] = sorted(
      (line.partition(':')[2].strip() for line in result['output'].splitlines() if line.startswith('package:')),
      key=str.casefold,
    )
  return jsonify(result)

@app.route('/api/adb/open-url', methods=['POST'])
def api_adb_open_url():
  data = request.get_json(silent=True) or {}
  serial = valid_adb_serial(data.get('serial'))
  url = str(data.get('url', '')).strip()
  parsed = urlsplit(url)
  package = str(data.get('package', '')).strip()
  if not serial:
    return jsonify(ok=False, error='请选择有效的设备'), 400
  if parsed.scheme not in ('http', 'https') or not parsed.netloc:
    return jsonify(ok=False, error='网址必须以 http:// 或 https:// 开头'), 400
  if package and not re.fullmatch(r'[A-Za-z0-9_.]+', package):
    return jsonify(ok=False, error='应用包名格式无效'), 400
  args = ['-s', serial, 'shell', 'am', 'start', '-a', 'android.intent.action.VIEW', '-d', url]
  if package:
    args.extend(['-p', package])
  display_args = ['adb', '-s', serial, 'shell', 'am', 'start', '-a', 'android.intent.action.VIEW', '-d', url]
  if package:
    display_args.extend(['-p', package])
  return adb_json_result(run_adb(*args, display_command=shlex.join(display_args)))

@app.route('/api/adb/launch-app', methods=['POST'])
def api_adb_launch_app():
  data = request.get_json(silent=True) or {}
  serial = valid_adb_serial(data.get('serial'))
  package = str(data.get('package', '')).strip()
  if not serial:
    return jsonify(ok=False, error='请选择有效的设备'), 400
  if not re.fullmatch(r'[A-Za-z0-9_.]+', package):
    return jsonify(ok=False, error='应用包名格式无效'), 400
  return adb_json_result(run_adb('-s', serial, 'shell', 'monkey', '-p', package, '1'))

@app.route('/api/adb/key', methods=['POST'])
def api_adb_key():
  data = request.get_json(silent=True) or {}
  serial = valid_adb_serial(data.get('serial'))
  keycodes = {
    'HOME': '3', 'BACK': '4', 'DPAD_UP': '19', 'DPAD_DOWN': '20',
    'DPAD_LEFT': '21', 'DPAD_RIGHT': '22', 'DPAD_CENTER': '23',
    'ENTER': '66', 'MENU': '82', 'VOLUME_UP': '24', 'VOLUME_DOWN': '25',
  }
  key = str(data.get('key', '')).upper()
  if not serial:
    return jsonify(ok=False, error='请选择有效的设备'), 400
  if key not in keycodes:
    return jsonify(ok=False, error='不支持该按键'), 400
  return adb_json_result(run_adb('-s', serial, 'shell', 'input', 'keyevent', keycodes[key]))

@app.route('/api/adb/install-apk', methods=['POST'])
def api_adb_install_apk():
  serial = valid_adb_serial(request.form.get('serial'))
  upload = request.files.get('file')
  if not serial:
    return jsonify(ok=False, error='请选择有效的设备'), 400
  if upload is None or not upload.filename or Path(upload.filename).suffix.lower() != '.apk':
    return jsonify(ok=False, error='请选择 APK 文件'), 400
  if request.content_length and request.content_length > ADB_APK_MAX_SIZE + 1024 * 1024:
    return jsonify(ok=False, error='APK 文件不能超过 256MB'), 413

  temp_path = None
  try:
    with tempfile.NamedTemporaryFile(prefix='mpd-web-', suffix='.apk', delete=False) as temp_file:
      temp_path = Path(temp_file.name)
      total_size = 0
      while True:
        chunk = upload.stream.read(1024 * 1024)
        if not chunk:
          break
        total_size += len(chunk)
        if total_size > ADB_APK_MAX_SIZE:
          return jsonify(ok=False, error='APK 文件不能超过 256MB'), 413
        temp_file.write(chunk)
    if not zipfile.is_zipfile(temp_path):
      return jsonify(ok=False, error='文件不是有效的 APK/ZIP 文件'), 400
    display_command = shlex.join(['adb', '-s', serial, 'install', '-r', Path(upload.filename).name])
    result, status = run_adb(
      '-s', serial, 'install', '-r', str(temp_path),
      display_command=display_command, timeout=300,
    )
    return jsonify(result), status
  except OSError:
    return jsonify(ok=False, error='无法暂存 APK，请检查 iStoreOS 临时空间'), 500
  finally:
    if temp_path:
      temp_path.unlink(missing_ok=True)

@app.route('/api/adb/command', methods=['POST'])
def api_adb_command():
  data = request.get_json(silent=True) or {}
  serial = valid_adb_serial(data.get('serial'))
  command_text = str(data.get('command', '')).strip()
  if not serial:
    return jsonify(ok=False, error='请选择有效的设备'), 400
  if not command_text or len(command_text) > 1024:
    return jsonify(ok=False, error='请输入不超过 1024 个字符的 ADB 子命令'), 400
  try:
    args = shlex.split(command_text)
  except ValueError:
    return jsonify(ok=False, error='命令引号不匹配'), 400
  allowed = {'shell', 'install', 'uninstall', 'push', 'pull', 'logcat', 'reboot'}
  if not args or args[0] not in allowed:
    return jsonify(ok=False, error='仅支持 shell、install、uninstall、push、pull、logcat、reboot 子命令'), 400
  full_command = ['adb', '-s', serial, *args]
  return adb_json_result(run_adb('-s', serial, *args, display_command=shlex.join(full_command)))

@app.route('/api/adb/screenshot')
def api_adb_screenshot():
  serial = valid_adb_serial(request.args.get('serial'))
  if not serial:
    return jsonify(ok=False, error='请选择有效的设备'), 400

  remote_path = f'/sdcard/adb-screen-{secrets.token_hex(8)}.png'
  capture_command = ['adb', '-s', serial, 'shell', 'screencap', '-p', remote_path]
  pull_command = ['adb', '-s', serial, 'pull', remote_path]
  with tempfile.TemporaryDirectory() as temp_dir:
    local_path = Path(temp_dir) / 'screen.png'
    try:
      capture, capture_status = run_adb('-s', serial, 'shell', 'screencap', '-p', remote_path)
      if capture_status != 200:
        return jsonify(capture), capture_status
      pull, pull_status = run_adb('-s', serial, 'pull', remote_path, str(local_path))
      if pull_status != 200:
        pull['command'] = shlex.join(pull_command + [str(local_path)])
        return jsonify(pull), pull_status
      image = local_path.read_bytes()
      if not image.startswith(b'\x89PNG\r\n\x1a\n') or len(image) > 16 * 1024 * 1024:
        return jsonify(ok=False, error='电视返回的截图无效或超过 16MB'), 502
    except OSError:
      return jsonify(ok=False, error='读取电视截图失败'), 502
    finally:
      run_adb('-s', serial, 'shell', 'rm', '-f', remote_path)

  response = send_file(io.BytesIO(image), mimetype='image/png', max_age=0)
  response.headers['X-ADB-Command'] = shlex.join(capture_command) + ' && ' + shlex.join(pull_command + ['screen.png'])
  return response

@app.route('/api/adb/uninstall-app', methods=['POST'])
def api_adb_uninstall_app():
  data = request.get_json(silent=True) or {}
  serial = valid_adb_serial(data.get('serial'))
  package = str(data.get('package', '')).strip()
  if not serial:
    return jsonify(ok=False, error='请选择有效的设备'), 400
  if not re.fullmatch(r'[A-Za-z0-9_.]+', package):
    return jsonify(ok=False, error='应用包名格式无效'), 400
  return adb_json_result(run_adb('-s', serial, 'shell', 'pm', 'uninstall', package))

def resolve_m3u_entries(text):
  music_dir = get_music_directory().resolve()
  library = [line.strip().replace('\\', '/') for line in mpc('listall').splitlines() if line.strip()]
  library_set = set(library)
  by_name = {}
  for uri in library:
    by_name.setdefault(uri.rsplit('/', 1)[-1], []).append(uri)

  resolved = []
  missing = []
  for line_number, line in enumerate(text.splitlines(), 1):
    entry = line.strip()
    if not entry or entry.startswith('#'):
      continue
    entry = unquote(entry).replace('\\', '/')
    if entry.lower().startswith('/music/'):
      candidate = entry[len('/music/'):]
    elif Path(entry).is_absolute():
      try:
        candidate = Path(entry).resolve().relative_to(music_dir).as_posix()
      except (OSError, ValueError):
        candidate = entry.lstrip('/')
    else:
      candidate = entry
    candidate = Path(candidate).as_posix()
    if candidate.startswith('/') or '..' in Path(candidate).parts:
      missing.append(line_number)
      continue
    if candidate in library_set:
      resolved.append(candidate)
      continue
    matches = by_name.get(candidate.rsplit('/', 1)[-1], [])
    if len(matches) == 1:
      resolved.append(matches[0])
    else:
      missing.append(line_number)
  return resolved, missing

def read_mpd_picture(uri, command_name):
  selected = get_selected_service()
  host = SERVICE_CONNECTIONS[selected]['host']
  port = int(SERVICE_CONNECTIONS[selected]['port'])
  if selected == 'mpd':
    config = Path('/etc/mpd.conf')
    if config.exists():
      for line in config.read_text(errors='replace').splitlines():
        match = re.match(r'^\s*port\s+(\d+)', line)
        if match:
          port = int(match.group(1))
          break

  for host in (host,):
    try:
      with socket.create_connection((host, port), timeout=4) as connection:
        connection.settimeout(4)
        stream = connection.makefile('rb')
        if not stream.readline().startswith(b'OK MPD '):
          continue
        escaped_uri = uri.replace('\\', '\\\\').replace('"', '\\"')
        image = bytearray()
        total_size = None
        image_type = None
        offset = 0
        while offset < 8 * 1024 * 1024:
          command = f'{command_name} "{escaped_uri}" {offset}\n'.encode('utf-8')
          connection.sendall(command)
          binary_size = None
          response_done = False
          while True:
            line = stream.readline()
            if not line or line.startswith(b'ACK'):
              return None
            if line.startswith(b'size: '):
              total_size = int(line.split(b':', 1)[1].strip())
              if total_size > 8 * 1024 * 1024:
                return None
            elif line.startswith(b'type: '):
              image_type = line.split(b':', 1)[1].strip().decode('ascii', 'ignore')
            elif line.startswith(b'binary: '):
              binary_size = int(line.split(b':', 1)[1].strip())
              break
            elif line == b'OK\n':
              response_done = True
              break
          if response_done:
            break
          if not binary_size:
            return None
          chunk = stream.read(binary_size)
          if len(chunk) != binary_size:
            return None
          image.extend(chunk)
          if stream.read(1) != b'\n':
            return None
          while True:
            line = stream.readline()
            if not line:
              return None
            if line == b'OK\n':
              break
            if line.startswith(b'ACK'):
              return None
          offset += binary_size
          if total_size is None or offset >= total_size:
            break
        return (bytes(image), image_type) if image else None
    except (OSError, ValueError, TimeoutError):
      continue
  return None

def find_cover(uri):
  for command_name in ('readpicture', 'albumart'):
    picture = read_mpd_picture(uri, command_name)
    if picture:
      return picture
  music_dir = get_music_directory().resolve()
  track_path = (music_dir / music_relative_path(uri)).resolve()
  if not track_path.is_relative_to(music_dir):
    return None
  try:
    for name in (
      'cover.jpg', 'cover.jpeg', 'cover.png', 'cover.webp',
      'folder.jpg', 'folder.jpeg', 'folder.png', 'folder.webp',
      'front.jpg', 'front.jpeg', 'front.png', 'front.webp',
      'album.jpg', 'album.jpeg', 'album.png', 'album.webp',
    ):
      candidate = track_path.parent / name
      if candidate.is_file() and candidate.stat().st_size <= 8 * 1024 * 1024:
        image_type = mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream'
        return candidate.read_bytes(), image_type
  except OSError:
    return None
  picture = read_id3_picture(track_path)
  if picture:
    return picture
  return None

def read_id3_picture(path):
  try:
    with path.open('rb') as audio_file:
      header = audio_file.read(10)
      if len(header) != 10 or header[:3] != b'ID3' or header[3] not in (2, 3, 4):
        return None
      tag_size_bytes = header[6:10]
      if any(byte & 0x80 for byte in tag_size_bytes):
        return None
      tag_size = sum(byte << shift for byte, shift in zip(tag_size_bytes, (21, 14, 7, 0)))
      if not tag_size or tag_size > 16 * 1024 * 1024:
        return None
      tag = audio_file.read(tag_size)
  except OSError:
    return None
  if len(tag) != tag_size:
    return None
  if header[5] & 0x80:
    tag = tag.replace(b'\xff\x00', b'\xff')

  version = header[3]
  offset = 0
  pictures = []
  front_picture = None
  while offset < len(tag):
    frame_header_size = 6 if version == 2 else 10
    frame_header = tag[offset:offset + frame_header_size]
    if len(frame_header) < frame_header_size or not frame_header.strip(b'\0'):
      break
    if version == 2:
      frame_id = frame_header[:3]
      frame_size = int.from_bytes(frame_header[3:6], 'big')
    else:
      frame_id = frame_header[:4]
      size_bytes = frame_header[4:8]
      if version == 4:
        if any(byte & 0x80 for byte in size_bytes):
          break
        frame_size = sum(byte << shift for byte, shift in zip(size_bytes, (21, 14, 7, 0)))
      else:
        frame_size = int.from_bytes(size_bytes, 'big')
    offset += frame_header_size
    if frame_size <= 0 or offset + frame_size > len(tag):
      break
    if frame_id in (b'APIC', b'PIC'):
      frame = tag[offset:offset + frame_size]
      if len(frame) > 8 * 1024 * 1024:
        offset += frame_size
        continue
      encoding = frame[0]
      if frame_id == b'PIC':
        if len(frame) < 5:
          offset += frame_size
          continue
        image_format = frame[1:4].decode('ascii', 'ignore').lower()
        mime_type = 'image/png' if image_format == 'png' else 'image/jpeg'
        description_start = 5
      else:
        mime_end = frame.find(b'\0', 1)
        if mime_end < 0 or mime_end + 1 >= len(frame):
          offset += frame_size
          continue
        mime_type = frame[1:mime_end].decode('ascii', 'ignore') or 'image/jpeg'
        description_start = mime_end + 2
      terminator = b'\0\0' if encoding in (1, 2) else b'\0'
      description_end = frame.find(terminator, description_start)
      if description_end >= 0:
        image_start = description_end + len(terminator)
        image = frame[image_start:]
        if image:
          picture = (image, mime_type)
          if frame[description_start - 1] == 3:
            front_picture = picture
          else:
            pictures.append(picture)
    offset += frame_size
  return front_picture or (pictures[0] if pictures else None)

def read_lrc(uri):
  music_dir = get_music_directory().resolve()
  track_path = (music_dir / music_relative_path(uri)).resolve()
  if not track_path.is_relative_to(music_dir):
    return None
  lyric_path = track_path.with_suffix('.lrc')
  try:
    if not lyric_path.is_file() or lyric_path.stat().st_size > 1024 * 1024:
      return None
    raw = lyric_path.read_bytes()
  except OSError:
    return None

  text = None
  for encoding in ('utf-8-sig', 'gb18030', 'big5'):
    try:
      text = raw.decode(encoding)
      break
    except UnicodeDecodeError:
      continue
  if text is None:
    text = raw.decode('utf-8', errors='replace')

  offset_match = re.search(r'^\[offset:\s*(-?\d+)\s*\]', text, re.MULTILINE | re.IGNORECASE)
  offset = int(offset_match.group(1)) / 1000 if offset_match else 0
  timestamp = re.compile(r'\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]')
  entries = []
  for line in text.splitlines():
    matches = list(timestamp.finditer(line))
    if not matches:
      continue
    lyric = timestamp.sub('', line).strip()
    if not lyric:
      continue
    for match in matches:
      fraction = float('0.' + (match.group(3) or '0'))
      seconds = max(0, int(match.group(1)) * 60 + int(match.group(2)) + fraction + offset)
      entries.append({'time': seconds, 'text': lyric})
  if not entries:
    return None
  return sorted(entries, key=lambda entry: entry['time'])

def get_status():
    out = mpc('status').splitlines()
    raw_song = display_music_uri(out[0]) if out else ''
    if get_selected_service() == 'owntone':
        raw_song = fix_owntone_text(raw_song)
    s = {'song':raw_song, 'state':'paused','pos':'','time':'','progress':0,'volume':0,'repeat':False,'random':False,'audio_format':''}
    if not out: return s
    if len(out)>1:
        l=out[1]
        if '[playing]' in l: s['state']='playing'
        elif '[paused]' in l: s['state']='paused'
        m=re.search(r'#(\d+)/(\d+)',l);  s['pos']=f"{m.group(1)}/{m.group(2)}" if m else ''
        m=re.search(r'((?:\d+:)?\d+:\d+)/((?:\d+:)?\d+:\d+)',l); s['time']=f"{m.group(1)} / {m.group(2)}" if m else ''
        m=re.search(r'\((\d+)%\)',l); s['progress']=int(m.group(1)) if m else 0
    status_text = '\n'.join(out)
    m=re.search(r'volume:\s*(\d+)%',status_text)
    s['volume']=int(m.group(1)) if m else 0
    s['repeat']='repeat: on' in status_text
    s['random']='random: on' in status_text
    if not s['audio_format'] and get_selected_service()=='owntone':
        try:
            player_info=owntone_api('GET','/api/player')
            current_item_id=player_info.get('item_id')
            if current_item_id:
                queue_info=owntone_api('GET','/api/queue')
                items=queue_info.get('items',[])
                for item in items:
                    if item.get('id')==current_item_id:
                        sr=item.get('samplerate',0)
                        br=item.get('bitrate',0)
                        ch=item.get('channels',0)
                        fmt_parts=[]
                        if sr>=1000: fmt_parts.append(f"{sr/1000:.1f}kHz")
                        elif sr>0: fmt_parts.append(f"{sr}Hz")
                        ch_map={1:'Mono',2:'Stereo',6:'5.1',8:'7.1'}
                        if ch in ch_map: fmt_parts.append(ch_map[ch])
                        elif ch>0: fmt_parts.append(f"{ch}ch")
                        if br>0: fmt_parts.append(f"{br}kbps")
                        s['audio_format']=' | '.join(fmt_parts) if fmt_parts else ''
                        break
        except Exception:
            pass
    am=re.search(r'audio:\s*(\d+):(\d+):(\d+)',status_text)
    bm=re.search(r'bitrate:\s*(\d+)\s*kbps',status_text)
    if am:
        sample_rate=int(am.group(1))
        bits=int(am.group(2))
        channels=int(am.group(3))
        fmt_parts=[]
        if sample_rate>=1000:
            fmt_parts.append(f"{sample_rate/1000:.1f}kHz")
        else:
            fmt_parts.append(f"{sample_rate}Hz")
        fmt_parts.append(f"{bits}bit")
        ch_map={1:'Mono',2:'Stereo',6:'5.1',8:'7.1'}
        fmt_parts.append(ch_map.get(channels, f"{channels}ch"))
        if bm:
            fmt_parts.append(f"{bm.group(1)}kbps")
        s['audio_format']=' | '.join(fmt_parts)
    elif bm:
        s['audio_format']=f"{bm.group(1)}kbps"
    return s

@app.route('/api/status')
def api_status():
  status = get_status()
  status['uri'] = mpc('-f', '%file%', 'current').strip()
  return jsonify(status=status, playlist=get_playlist())

@app.route('/api/service', methods=['GET', 'POST'])
def api_service():
  if request.method == 'POST':
    data = request.get_json(silent=True) or {}
    selected = data.get('service')
    if selected not in SERVICE_CONNECTIONS:
      return jsonify(ok=False, error='请选择 MPD 或 OwnTone'), 400
    session['audio_service'] = selected
  selected = get_selected_service()
  return jsonify(
    ok=True,
    service=selected,
    name=SERVICE_CONNECTIONS[selected]['name'],
    playlists_api=selected == 'owntone',
  )

@app.route('/api/cover')
def api_cover():
  uri = request.args.get('uri', '')
  current_uri = mpc('-f', '%file%', 'current').strip()
  if not uri or uri != current_uri:
    return '', 404
  cover = find_cover(uri)
  if not cover:
    return '', 404
  image, image_type = cover
  return send_file(io.BytesIO(image), mimetype=image_type or 'image/jpeg', max_age=300)

@app.route('/api/lyrics')
def api_lyrics():
  uri = request.args.get('uri', '')
  current_uri = mpc('-f', '%file%', 'current').strip()
  if not uri or uri != current_uri:
    return jsonify(lyrics=[]), 404
  lyrics = read_lrc(uri)
  if not lyrics:
    return jsonify(lyrics=[]), 404
  return jsonify(lyrics=lyrics)

@app.route('/api/library')
def api_library():
  config = Path('/etc/mpd.conf')
  music_dir = Path('/var/lib/mpd/music')
  if config.exists():
    for line in config.read_text(errors='replace').splitlines():
      match = re.match(r'^\s*music_directory\s+(?:"([^"]+)"|(\S+))', line)
      if match:
        music_dir = Path(match.group(1) or match.group(2))
        break
  lib = []
  for uri in (line.strip() for line in mpc('listall').splitlines() if line.strip()):
    try:
      details = (music_dir / music_relative_path(uri)).stat()
      created = getattr(details, 'st_birthtime', None)
      lib.append({
        'uri': uri,
        'display_name': display_music_uri(uri),
        'size': details.st_size,
        'time': created if created is not None else details.st_mtime,
        'time_kind': '创建时间' if created is not None else '修改时间',
      })
    except (OSError, ValueError):
      lib.append({'uri': uri, 'display_name': display_music_uri(uri), 'size': 0, 'time': 0, 'time_kind': '不可用'})
  return jsonify(library=lib)

# ---------------- 背景图接口 ----------------
@app.route('/api/background', methods=['GET'])
def api_background_get():
  path = find_background()
  if not path:
    return '', 404
  return send_file(
    path,
    mimetype=BACKGROUND_TYPES.get(path.suffix.lower(), 'application/octet-stream'),
    max_age=0,
  )

@app.route('/api/background', methods=['POST'])
def api_background_upload():
  upload = request.files.get('image')
  if upload is None or not upload.filename:
    return jsonify(ok=False, error='未选择图片文件'), 400
  data = upload.read(BACKGROUND_MAX_SIZE + 1)
  if not data:
    return jsonify(ok=False, error='图片内容为空'), 400
  if len(data) > BACKGROUND_MAX_SIZE:
    return jsonify(ok=False, error='图片不能超过 8MB'), 400
  extension = guess_background_extension(data)
  if extension is None:
    return jsonify(ok=False, error='仅支持 JPG / PNG / WEBP / GIF / BMP 图片'), 400
  try:
    BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    remove_background_files()
    (BACKGROUND_DIR / ('custom' + extension)).write_bytes(data)
  except OSError:
    return jsonify(ok=False, error='无法保存背景图，请检查目录权限'), 500
  return jsonify(ok=True)

@app.route('/api/background', methods=['DELETE'])
def api_background_delete():
  try:
    remove_background_files()
  except OSError:
    return jsonify(ok=False, error='无法删除背景图'), 500
  return jsonify(ok=True)

def get_audio_devices():
  usb_devices = []
  cards_file = Path('/proc/asound/cards')
  if cards_file.exists():
    for line in cards_file.read_text(errors='replace').splitlines():
      match = re.match(r'\s*(\d+)\s+\[([^]]+)\]:\s*(.*)', line)
      if not match:
        continue
      card_id, card_name, description = match.groups()
      device_path = Path(f'/sys/class/sound/card{card_id}/device').resolve()
      if re.search(r'/usb\d+/', str(device_path), re.IGNORECASE):
        usb_devices.append({
          'id': card_id,
          'name': card_name.strip(),
          'description': description.strip(),
        })

  outputs = []
  current = None
  for line in mpc('outputs').splitlines():
    match = re.match(
      r'\s*Output\s+(\d+)\s+\((.*?)\)(?:\s+is\s+(enabled|disabled))?\s*$',
      line,
    )
    if match:
      current = {
        'id': match.group(1),
        'name': match.group(2),
        'enabled': match.group(3) == 'enabled',
      }
      outputs.append(current)
      if match.group(3):
        current = None
    elif current and line.strip().lower() in ('enabled', 'disabled'):
      current['enabled'] = line.strip().lower() == 'enabled'
      current = None
  return usb_devices, outputs

def _audio_output_value(block, key):
  match = re.search(
    rf'(?m)^[ \t]*{re.escape(key)}[ \t]+(?:"((?:\\.|[^"])*)"|(\S+))',
    block,
  )
  return (match.group(1) or match.group(2)) if match else None

def _card_id_for_output_device(device, usb_devices):
  for card in usb_devices:
    card_id = card['id']
    card_name = card['name']
    if device in (f'hw:{card_id},0', f'plughw:{card_id},0',
                  f'hw:CARD={card_name},DEV=0', f'plughw:CARD={card_name},DEV=0',
                  f'hw:{card_name},0', f'plughw:{card_name},0'):
      return card_id
  return None

def _configured_audio_devices(config_text, usb_devices):
  configured = set()
  pattern = re.compile(r'(?ms)^[ \t]*audio_output[ \t]*\{.*?^[ \t]*\}')
  for match in pattern.finditer(config_text):
    device = _audio_output_value(match.group(), 'device')
    card_id = _card_id_for_output_device(device, usb_devices) if device else None
    if card_id is not None:
      configured.add(card_id)
  return configured

@app.route('/api/audio/configure', methods=['POST'])
def api_audio_configure():
  if get_selected_service() != 'mpd':
    return jsonify(ok=False, error='USB 输出配置仅适用于 MPD'), 400
  usb_devices, _ = get_audio_devices()
  if not usb_devices:
    return jsonify(ok=False, error='未检测到 USB 声卡'), 404

  config_path = Path('/etc/mpd.conf')
  try:
    config_text = config_path.read_text(encoding='utf-8', errors='replace')
  except OSError:
    return jsonify(ok=False, error='无法读取 /etc/mpd.conf，请检查服务权限'), 500

  configured = _configured_audio_devices(config_text, usb_devices)
  missing = [card for card in usb_devices if card['id'] not in configured]
  if not missing:
    return jsonify(ok=True, added=0, message='所有 USB 声卡都已配置')

  blocks = []
  for card in missing:
    card_id = card['id']
    output_name = re.sub(r'^[^-]+\s+-\s+', '', card['description']).strip()
    output_name = (output_name or card['name']).replace('\\', '\\\\').replace('"', '\\"')
    blocks.append(
      'audio_output {\n'
      '    type "alsa"\n'
      f'    name "{output_name}"\n'
      f'    device "hw:{card_id},0"\n'
      '    mixer_type "software"\n'
      '    enabled "no"\n'
      '}\n'
    )

  updated_config = config_text.rstrip() + '\n\n' + '\n'.join(blocks)
  try:
    config_path.write_text(updated_config, encoding='utf-8')
    result = subprocess.run(
      ['/etc/init.d/mpd', 'restart'], capture_output=True, text=True,
      encoding='utf-8', errors='replace', timeout=30
    )
  except (OSError, subprocess.TimeoutExpired):
    return jsonify(ok=False, error='配置已写入，但重启 MPD 失败，请检查 /etc/init.d/mpd'), 500
  if result.returncode != 0:
    detail = (result.stderr or result.stdout).strip()
    return jsonify(ok=False, error=detail or 'MPD 重启失败，请检查配置和服务日志'), 500
  return jsonify(ok=True, added=len(missing), message='USB 输出已配置并重启 MPD')

@app.route('/api/audio')
def api_audio():
  usb_devices, outputs = get_audio_devices()
  return jsonify(usb_devices=usb_devices, outputs=outputs)

@app.route('/api/audio/select', methods=['POST'])
def api_audio_select():
  data = request.get_json(silent=True) or {}
  output_name = str(data.get('name', '')).strip()
  enabled = data.get('enabled')
  if not output_name:
    return jsonify(ok=False, error='output name is required'), 400
  if not isinstance(enabled, bool):
    return jsonify(ok=False, error='enabled must be a boolean'), 400
  _, outputs = get_audio_devices()
  selected_output = next(
    (output for output in outputs if output['name'] == output_name), None
  )
  if not selected_output:
    return jsonify(ok=False, error='output is not configured in the selected service'), 404
  mpc('enable' if enabled else 'disable', selected_output['id'])
  _, updated_outputs = get_audio_devices()
  selected = next(
    (output for output in updated_outputs if output['name'] == output_name),
    None,
  )
  if not selected or selected['enabled'] != enabled:
    return jsonify(ok=False, error='无法切换输出状态，请检查 mpc outputs 和服务日志'), 502
  return jsonify(ok=True, message='MPD 输出状态已更新')

@app.route('/api/control/<cmd>')
def api_control(cmd):
    if cmd in ('play','pause','toggle','stop','next','prev','repeat','random'):
        mpc(cmd)
    return jsonify(ok=True)

@app.route('/api/volume/<int:v>')
def api_vol(v):
    mpc('volume', str(max(0,min(100,v)))); return jsonify(ok=True)

@app.route('/api/seek', methods=['POST'])
def api_seek():
  data = request.get_json(silent=True) or {}
  try:
    percent = float(data.get('percent'))
  except (TypeError, ValueError):
    return jsonify(ok=False, error='percent must be a number'), 400
  if not 0 <= percent <= 100:
    return jsonify(ok=False, error='percent must be between 0 and 100'), 400
  mpc('seek', f'{percent:.1f}%')
  return jsonify(ok=True)

@app.route('/api/play', methods=['POST'])
def api_play():
  uri = request.get_json(force=True).get('uri', '').strip()
  if uri:
    queued = get_playlist()
    positions = [
      index for index, queued_uri in enumerate(queued)
      if queued_uri == uri
    ]
    if not positions:
      mpc('add', uri)
      queued = get_playlist()
      positions = [
        index for index, queued_uri in enumerate(queued)
        if queued_uri == uri
      ]
    if not positions:
      return jsonify(ok=False, error='无法在播放队列中定位该歌曲'), 502
    mpc('play', str(positions[0] + 1))
    return jsonify(ok=True)
  return jsonify(ok=False, error='uri is required'), 400

@app.route('/api/queue/play', methods=['POST'])
def api_queue_play():
  data = request.get_json(silent=True) or {}
  try:
    position = int(data.get('position'))
  except (TypeError, ValueError):
    return jsonify(ok=False, error='position must be a non-negative integer'), 400
  playlist = get_playlist()
  if position < 0 or position >= len(playlist):
    return jsonify(ok=False, error='queue position is out of range'), 404
  mpc('play', str(position + 1))
  return jsonify(ok=True)

@app.route('/api/add', methods=['POST'])
def api_add():
    uri = request.get_json(force=True).get('uri','')
    if uri: mpc('add', uri)
    return jsonify(ok=True)

@app.route('/api/add-all')
def api_add_all():
    mpc('add', '/')
    return jsonify(ok=True)

@app.route('/api/clear')
def api_clear():
    mpc('clear'); return jsonify(ok=True)

# 歌单
@app.route('/api/playlists')
def api_pls():
    if get_selected_service() == 'owntone':
      try:
        playlists = get_owntone_playlists()
      except RuntimeError as error:
        return jsonify(ok=False, error=str(error)), 502
      return jsonify(
        playlists=[f"owntone:{item['id']}" for item in playlists],
        playlist_items=[
          {'id': f"owntone:{item['id']}", 'name': item['name']}
          for item in playlists
        ],
      )
    pls = [l for l in mpc('lsplaylists').splitlines() if l.strip()]
    return jsonify(playlists=pls, playlist_items=[{'id': name, 'name': name} for name in pls])

@app.route('/api/playlists/save', methods=['POST'])
def api_pl_save():
    data = request.get_json(silent=True) or {}
    n = str(data.get('name', '')).strip()
    if not n or n in ('.', '..') or '/' in n or '\\' in n or any(ord(char) < 32 for char in n):
      return jsonify(ok=False, error='请输入有效的歌单名称'), 400
    ok, output = run_mpc_checked('save', n)
    if not ok:
      service_name = SERVICE_CONNECTIONS[get_selected_service()]['name']
      detail = output or f'{service_name} 保存当前队列失败'
      if get_selected_service() == 'owntone':
        detail += '。请检查 OwnTone 配置中的 allow_modifying_stored_playlists 和歌单目录写权限'
      return jsonify(ok=False, error=detail), 502
    return jsonify(ok=True, name=n, message=f'已保存歌单「{n}」')

@app.route('/api/playlists/import', methods=['POST'])
def api_pl_import():
    upload = request.files.get('file')
    if upload is None or not upload.filename:
      return jsonify(ok=False, error='请选择 M3U 歌单文件'), 400
    data = upload.read(PLAYLIST_MAX_SIZE + 1)
    if len(data) > PLAYLIST_MAX_SIZE:
      return jsonify(ok=False, error='歌单文件不能超过 4MB'), 400
    try:
      text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
      return jsonify(ok=False, error='歌单必须使用 UTF-8 编码'), 400

    tracks, missing = resolve_m3u_entries(text)
    if missing:
      return jsonify(ok=False, error='以下行无法唯一匹配到 MPD 曲库：' + ', '.join(map(str, missing[:20]))), 400
    if not tracks:
      return jsonify(ok=False, error='歌单中没有可导入的歌曲'), 400

    name_match = re.search(r'^#PLAYLIST:(.*)$', text, re.MULTILINE | re.IGNORECASE)
    name = (name_match.group(1).strip() if name_match else Path(upload.filename.replace('\\', '/')).name)
    name = re.sub(r'\.m3u8?$', '', name, flags=re.IGNORECASE).strip()
    name = name.replace('\\', '/').rsplit('/', 1)[-1].strip()
    if not name or name in ('.', '..') or any(ord(char) < 32 for char in name):
      return jsonify(ok=False, error='歌单名称无效'), 400

    playlist_dir = get_playlist_directory()
    target = playlist_dir / (name + '.m3u')
    if target.exists():
      return jsonify(ok=False, error='同名歌单已存在，请先删除或重命名'), 409
    try:
      playlist_dir.mkdir(parents=True, exist_ok=True)
      target.write_text('#EXTM3U\n' + '\n'.join(tracks) + '\n', encoding='utf-8')
    except OSError:
      return jsonify(ok=False, error='无法写入 MPD 歌单目录，请检查网页服务的目录权限'), 500
    return jsonify(ok=True, name=name, count=len(tracks))

@app.route('/api/playlists/load', methods=['POST'])
def api_pl_load():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    if not name:
      return jsonify(ok=False, error='请选择要播放的歌单'), 400
    if get_selected_service() == 'owntone':
      try:
        playlist = get_owntone_playlist(name)
        print(f'[API] api_pl_load: name={name} playlist={playlist}', flush=True)
        if not playlist:
          return jsonify(ok=False, error='OwnTone 歌单不存在，请刷新列表'), 404
        player = owntone_api('GET', '/api/player')
        was_shuffle = player.get('shuffle', False)
        print(f'[API] api_pl_load: was_shuffle={was_shuffle}', flush=True)
        result = owntone_api('POST', '/api/queue/items/add', {
          'uris': [playlist['uri']], 'clear': True, 'playback': 'start', 'shuffle': False,
        })
        if was_shuffle:
          owntone_api('PUT', '/api/player/shuffle', {'state': True})
          print(f'[API] api_pl_load: shuffle restored to true', flush=True)
      except RuntimeError as error:
        return jsonify(ok=False, error=str(error)), 502
      return jsonify(ok=True, count=result.get('count', 0))
    expected_tracks = [normalize_track_uri(line) for line in mpc('-f', '%file%', 'playlist', name).splitlines() if line.strip()]
    if not expected_tracks:
      return jsonify(ok=False, error='歌单为空或 MPD 无法读取该歌单'), 404
    run_mpc_checked('clear')
    for track in expected_tracks:
      mpc('add', track)
    run_mpc_checked('play')
    loaded_tracks = get_playlist()
    if len(loaded_tracks) != len(expected_tracks):
      loaded_counts = {}
      for uri in loaded_tracks:
        loaded_counts[uri] = loaded_counts.get(uri, 0) + 1
      missing_tracks = []
      for uri in expected_tracks:
        if loaded_counts.get(uri, 0):
          loaded_counts[uri] -= 1
        else:
          missing_tracks.append(uri)
      missing_detail = ', '.join(missing_tracks[:10])
      return jsonify(
        ok=False,
        error=(
          f'歌单应有 {len(expected_tracks)} 首，但播放队列只有 {len(loaded_tracks)} 首。'
          f'未加入队列的歌曲：{missing_detail or "无法识别"}'
        ),
      ), 502
    return jsonify(ok=True, count=len(loaded_tracks))

@app.route('/api/playlists/delete', methods=['POST'])
def api_pl_del():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    if not name:
      return jsonify(ok=False, error='请选择要删除的歌单'), 400
    if get_selected_service() == 'owntone':
      try:
        playlist = get_owntone_playlist(name)
        if not playlist:
          return jsonify(ok=False, error='OwnTone 歌单不存在，请刷新列表'), 404
        owntone_api('DELETE', f"/api/library/playlists/{playlist['id']}")
      except RuntimeError as error:
        return jsonify(ok=False, error=str(error)), 502
      return jsonify(ok=True)
    ok, output = run_mpc_checked('rm', name)
    if not ok:
      return jsonify(ok=False, error=output or 'MPD 删除歌单失败'), 502
    playlists = [line.strip() for line in mpc('lsplaylists').splitlines() if line.strip()]
    if name in playlists:
      return jsonify(ok=False, error='MPD 命令已执行，但歌单仍存在；请检查歌单目录权限和文件状态'), 502
    return jsonify(ok=True)

@app.route('/api/playlists/addtrack', methods=['POST'])
def api_pl_addtr():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    uri = str(data.get('uri', '')).replace('\\', '/').strip()
    if not name or not uri:
      return jsonify(ok=False, error='请选择歌单和歌曲'), 400
    if name in ('.', '..') or '/' in name or '\\' in name or any(ord(char) < 32 for char in name):
      return jsonify(ok=False, error='歌单名称无效'), 400
    if get_selected_service() == 'owntone':
      uri = normalize_track_uri(uri)
      if not uri or uri.startswith('/') or '..' in Path(uri).parts:
        return jsonify(ok=False, error='歌曲路径无效'), 400
      try:
        playlist = get_owntone_playlist(name)
        playlist_name = str(playlist.get('name', '')).strip() if playlist else name
        if not playlist_name or '/' in playlist_name or '\\' in playlist_name or any(ord(char) < 32 for char in playlist_name):
          return jsonify(ok=False, error='OwnTone 歌单名称无效'), 400
        playlist_dir = get_playlist_directory().resolve()
        playlist_filename = playlist_name
        if Path(playlist_filename).suffix.lower() not in ('.m3u', '.m3u8'):
          playlist_filename += '.m3u'
        target = (playlist_dir / playlist_filename).resolve()
        if target.parent != playlist_dir:
          return jsonify(ok=False, error='OwnTone 歌单路径无效'), 400
        is_new = not target.is_file()
        track_path = f'/music/{uri}'
        if is_new:
          with target.open('w', encoding='utf-8', newline='\n') as playlist_file:
            playlist_file.write(track_path + '\n')
        else:
          text = target.read_text(encoding='utf-8-sig')
          existing_tracks = {
            music_relative_path(line.strip()).as_posix()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith('#')
          }
          if music_relative_path(track_path).as_posix() in existing_tracks:
            return jsonify(ok=True)
          with target.open('a', encoding='utf-8', newline='\n') as playlist_file:
            if text and not text.endswith(('\n', '\r')):
              playlist_file.write('\n')
            playlist_file.write(track_path + '\n')
      except (OSError, UnicodeError) as error:
        return jsonify(ok=False, error=f'更新 OwnTone 歌单文件失败：{error}'), 502
      except RuntimeError as error:
        return jsonify(ok=False, error=str(error)), 502
      return jsonify(ok=True, created=is_new)
    ok, output = run_mpc_checked('addplaylist', name, uri)
    if not ok:
      return jsonify(ok=False, error=output or '添加歌曲到歌单失败'), 502
    return jsonify(ok=True)

@app.route('/api/playlists/<name>/tracks')
def api_pl_tracks(name):
    if get_selected_service() == 'owntone':
      try:
        playlist = get_owntone_playlist(name)
        if not playlist:
          return jsonify(ok=False, error='OwnTone 歌单不存在，请刷新列表'), 404
        tracks = owntone_playlist_tracks(playlist)
        track_list = []
        for item in tracks:
          title = item.get('title', '')
          uri = item.get('uri', '') or ''
          if title:
            track_list.append(title)
          elif uri:
            normalized = normalize_track_uri(uri)
            track_list.append(normalized.rsplit('/', 1)[-1] if '/' in normalized else normalized)
          else:
            track_list.append('(未知歌曲)')
        return jsonify(tracks=track_list)
      except Exception as error:
        return jsonify(ok=False, error=str(error)), 502
    tracks = [normalize_track_uri(line) for line in mpc('-f', '%file%', 'playlist', name).splitlines() if line.strip()]
    return jsonify(tracks=tracks)
@app.route('/api/playlists/removetrack', methods=['POST'])
def api_pl_removetr():
    d = request.get_json(force=True)
    if d.get('name') and d.get('pos') is not None:
      mpc('delplaylist', d['name'], str(d['pos']+1))
    return jsonify(ok=True)

@app.route('/api/playlists/playtrack', methods=['POST'])
def api_pl_playtrack():
  d = request.get_json(force=True)
  name = d.get('name', '').strip()
  try:
    position = int(d.get('pos'))
  except (TypeError, ValueError):
    return jsonify(ok=False, error='pos must be a non-negative integer'), 400
  if not name or position < 0:
    return jsonify(ok=False, error='name and a non-negative pos are required'), 400

  if get_selected_service() == 'owntone':
    try:
      playlist = get_owntone_playlist(name)
      print(f'[API] api_pl_playtrack: name={name} pos={position}', flush=True)
      if not playlist:
        return jsonify(ok=False, error='OwnTone 歌单不存在，请刷新列表'), 404
      tracks = owntone_playlist_tracks(playlist)
      if position >= len(tracks):
        return jsonify(ok=False, error='歌曲序号超出歌单范围，请重新展开歌单后再试'), 404

      uris = [t.get('uri') or '' for t in tracks]
      if not any(uris):
        return jsonify(ok=False, error='歌单中没有有效的歌曲'), 500
      if not uris[position]:
        return jsonify(ok=False, error=f'目标歌曲（第{position+1}首）没有有效的 URI，无法播放'), 400

      valid_uris = [u for u in uris if u]
      valid_position = sum(1 for i in range(position) if uris[i])
      player = owntone_api('GET', '/api/player')
      was_shuffle = player.get('shuffle', False)
      result = owntone_api('POST', '/api/queue/items/add', {
        'uris': valid_uris, 'clear': True, 'playback': 'start',
        'playback_from_position': str(valid_position), 'shuffle': False,
      })
      if was_shuffle:
        owntone_api('PUT', '/api/player/shuffle', {'state': True})
        print(f'[API] api_pl_playtrack: shuffle restored to true', flush=True)
      return jsonify(ok=True, count=result.get('count', len(uris)))
    except RuntimeError as error:
      return jsonify(ok=False, error=str(error)), 502

  tracks = [normalize_track_uri(line) for line in mpc('-f', '%file%', 'playlist', name).splitlines() if line.strip()]
  if position >= len(tracks):
    return jsonify(ok=False, error='歌曲序号超出歌单范围，请重新展开歌单后再试'), 404

  run_mpc_checked('clear')
  for track in tracks:
    mpc('add', track)
  run_mpc_checked('play', str(position + 1))
  return jsonify(ok=True)

PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MPD</title><style>
*{margin:0;padding:0;box-sizing:border-box}
:root{color-scheme:dark;--bg:#101216;--panel:#191d23;--panel2:#20252c;--line:#30363e;--text:#f4f2ed;--muted:#a5a7aa;--accent:#ed3157;--accent2:#fa6681}
body{font-family:"Microsoft YaHei", "Noto Sans CJK SC", sans-serif;background:radial-gradient(ellipse at 28% 16%,#31202a 0,#19191f 44%,var(--bg) 100%);color:var(--text);height:100vh;min-height:520px;display:flex;flex-direction:column;overflow:hidden}
button,select,input{font:inherit;color:inherit}
button{cursor:pointer}
.topline{height:48px;display:flex;align-items:center;justify-content:space-between;padding:0 22px;border-bottom:1px solid #ffffff0b;color:var(--muted);font-size:13px;letter-spacing:1.5px;text-transform:uppercase}
.brand{font-size:16px;letter-spacing:2px;color:var(--text);font-weight:700}
.brand i{color:var(--accent);font-style:normal}
.top-right{display:flex;align-items:center;gap:12px}
.top-actions{display:contents}
.top-actions>*{order:3}
.top-actions>:first-child{order:1}
.mobile-menu-toggle{display:none}
.volume-control{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:11px;white-space:nowrap}
.volume-control{order:2}
.volume-control input{width:78px;accent-color:var(--accent)}
.modal-open-btn{border:1px solid var(--line);background:var(--panel2);color:#ddd;padding:6px 12px;border-radius:5px;font-size:12px;cursor:pointer;transition:background .2s}
.modal-open-btn:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
.service-select{border:1px solid var(--line);background:var(--panel2);color:#ddd;padding:6px 8px;border-radius:5px;font-size:12px}
.workspace{flex:1;min-height:0;display:grid;grid-template-columns:1fr 1fr;gap:28px;padding:24px 28px 18px;position:relative;overflow:hidden}
.listen{min-width:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;position:relative;padding:20px 32px 30px}
.turntable{width:min(38vw,380px);aspect-ratio:1;position:relative;flex:none;margin-bottom:34px}
.artwork{position:absolute;inset:0;border-radius:50%;display:grid;place-items:center;background:repeating-radial-gradient(circle,#1a1a1a 0 1px,#222 1px 2px,#1a1a1a 2px 3px);box-shadow:0 20px 60px rgba(0,0,0,0.5),0 0 0 8px rgba(255,255,255,0.05),0 0 0 12px rgba(255,255,255,0.08),inset 0 0 80px rgba(0,0,0,0.4);animation:turn 18s linear infinite;animation-play-state:paused;overflow:hidden}
.artwork.playing{animation-play-state:running}
.artwork:before{content:"";position:absolute;inset:0;border-radius:50%;background:repeating-radial-gradient(circle,#1a1a1a 0 1px,#222 1px 2px,#1a1a1a 2px 3px);z-index:0}
.art-label{z-index:1;width:80%;height:80%;display:grid;place-items:center;border-radius:50%;background:#17161d;color:#ffd5dc;font-size:12px;line-height:1.35;padding:8px;overflow:hidden;word-break:break-word;text-align:center;position:relative;margin:auto}
.cover-image{display:none;position:absolute;z-index:2;inset:10%;width:80%;height:80%;object-fit:cover;border-radius:50%}
.tonearm{position:absolute;z-index:5;top:-20%;right:-6%;width:55%;height:60%;pointer-events:none;transform:rotate(-108deg);transform-origin:88% 17%;transition:transform .9s cubic-bezier(.2,.75,.25,1)}
.tonearm-pivot{position:absolute;top:10%;right:6%;width:18%;aspect-ratio:1;border:5px solid #d7d9d8;border-radius:50%;background:radial-gradient(circle,#fff 0 25%,#aeb2b2 30% 48%,#25292a 54%);box-shadow:0 2px 8px #0009}
.tonearm-shaft{position:absolute;top:17%;right:12%;width:64%;height:4%;min-height:6px;border-radius:99px;background:linear-gradient(180deg,#fff,#c9ccca 65%,#777d7d);box-shadow:0 2px 5px #0009;transform:rotate(-40deg);transform-origin:100% 50%}
.tonearm-head{position:absolute;left:30%;top:46%;width:20%;height:8%;border-radius:3px 8px 8px 3px;background:linear-gradient(180deg,#fff,#c5c9c8);box-shadow:0 2px 5px #0009;transform:rotate(50deg)}
.tonearm-head:after{content:"";position:absolute;left:12%;bottom:-4px;width:5px;height:8px;border-radius:0 0 3px 3px;background:#f6f5ed}
.artwork.playing+.tonearm{transform:rotate(-12deg)}
@keyframes turn{to{transform:rotate(360deg)}}
.track-title{max-width:800px;font-size:clamp(22px,3vw,36px);line-height:1.35;font-weight:700;overflow-wrap:anywhere}
.track-sub{margin-top:8px;color:var(--muted);font-size:15px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.audio-format-inline{color:var(--accent);font-size:11px;letter-spacing:.5px;opacity:.8}
.track-pos{margin-top:18px;color:#7f8186;font-size:12px;letter-spacing:1px}
.controls{display:flex;align-items:center;gap:24px;margin-top:28px}
.icon-btn{width:42px;height:42px;border:0;background:transparent;color:#c1c3c6;font-size:21px;transition:color .18s,transform .18s}
.icon-btn:hover{color:#fff;transform:scale(1.08)}
.icon-btn.active{color:var(--accent2)}
.play-btn{width:58px;height:58px;border:0;border-radius:50%;background:var(--accent);color:#fff;font-size:24px;box-shadow:0 8px 24px #ed315744}
.play-btn:hover{background:var(--accent2)}
.seekrow{width:min(100%,650px);display:grid;grid-template-columns:42px 1fr 42px;gap:12px;align-items:center;margin-top:26px;color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.stream-control{display:flex;align-items:center;justify-content:center;gap:10px;min-height:32px;margin-top:8px;color:var(--muted);font-size:12px}
.stream-control button{border:1px solid var(--line);border-radius:5px;background:var(--panel2);color:#ddd;padding:5px 9px;font-size:12px}
.stream-control button:hover{border-color:var(--accent);color:#fff}
.stream-control button:disabled{opacity:.5;cursor:not-allowed}
.pb{height:4px;border-radius:3px;background:#ffffff22;cursor:pointer;touch-action:none;outline:none}
.pf{height:100%;border-radius:3px;background:var(--accent);width:0;position:relative}
.pf:after{content:"";position:absolute;right:-5px;top:50%;width:10px;height:10px;transform:translateY(-50%);border-radius:50%;background:#fff;box-shadow:0 0 0 3px #ed315755}
.lyric-offset{min-width:42px;text-align:center;font-variant-numeric:tabular-nums}
.lyrics-pane{min-width:0;min-height:0;display:flex;flex-direction:column;background:transparent;border:none;box-shadow:none;overflow:hidden;position:relative}
/* 修改：限制高度，隐藏滚动条，增加边缘渐隐 */
.lyrics-view{
  --lyric-font-size:15px;
  flex:1;
  min-height:0;
  overflow-y:auto;
  display:flex;
  flex-direction:column;
  gap:18px;
  padding: 40vh 20px;
  text-align:center;
  scrollbar-width:none; 
  -ms-overflow-style:none;
  mask-image: linear-gradient(to bottom, transparent 0%, black 10%, black 90%, transparent 100%);
  -webkit-mask-image: linear-gradient(to bottom, transparent 0%, black 10%, black 90%, transparent 100%);
}
.lyrics-view::-webkit-scrollbar { display: none; }
.lyrics-view::before,.lyrics-view::after{content:''; margin:auto; }
.lyric-line{color:#8e9196;font-size:var(--lyric-font-size);line-height:1.6;transition:color .2s,transform .2s;text-shadow:0 2px 8px #0008}
.lyric-line.active{color:var(--accent2);font-size:calc(var(--lyric-font-size) * 1.33);font-weight:800;transform:scale(1.04);text-shadow:0 0 18px #ed315755,0 2px 8px #000}
.lyric-empty{color:var(--muted);font-size:14px;text-align:center;margin-top:40px}

/* --- 新增的歌词控制栏样式 --- */
.lyrics-header{
  display:flex;
  justify-content:center;
  align-items:center;
  padding:20px 0 10px;
  width:100%;
  flex-shrink:0;
}
.lyrics-zoom{
  display:flex;
  align-items:center;
  gap:4px;
  background:rgba(25, 29, 35, 0.85);
  border:1px solid var(--line);
  border-radius:20px;
  padding:4px 14px;
  box-shadow:0 4px 12px rgba(0,0,0,0.15);
  backdrop-filter:blur(8px);
}
.lyrics-zoom button{
  border:0;
  background:transparent;
  color:var(--muted);
  font-size:13px;
  padding:6px 8px;
  border-radius:4px;
  cursor:pointer;
  transition:all .2s;
}
.lyrics-zoom button:hover{
  background:#ffffff15;
  color:#fff;
}
.lyric-offset{
  min-width:45px;
  text-align:center;
  color:var(--text);
  font-size:13px;
  font-weight:bold;
}

/* 浮动媒体库窗口 */
.library-modal{position:fixed;top:60px;right:20px;bottom:20px;width:420px;background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:0 10px 40px #000c;z-index:100;display:flex;flex-direction:column;transition:transform .3s ease,opacity .3s ease;overflow:hidden}
.library-modal.hide{transform:translateX(110%);opacity:0;pointer-events:none}
.modal-head{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--panel2)}
.modal-tabs{display:flex;gap:16px}
.modal-tabs .tab{padding:0;border-bottom:2px solid transparent;background:none;color:var(--muted);font-size:13px;cursor:pointer}
.modal-tabs .tab.on{color:var(--accent2);border-color:var(--accent)}
.modal-close{background:transparent;border:0;font-size:24px;color:var(--muted);cursor:pointer;line-height:1}
.modal-close:hover{color:#fff}
.modal-content{flex:1;min-height:0;display:flex;flex-direction:column}
.audio{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 14px;border-bottom:1px solid var(--line);font-size:11px;color:var(--muted)}
.audio strong{color:#ddd}
.audio button{border:0;border-radius:5px;padding:7px 10px;background:var(--accent);color:#fff;font-size:12px}
.audio-note{color:#8e9196}
.output-options{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.output-option{display:inline-flex;align-items:center;gap:5px;color:#ddd;white-space:nowrap;cursor:pointer}
.output-option input{accent-color:var(--accent)}
.stream-status{font-size:10px}
.panel-tools{padding:10px 14px;display:grid;gap:8px;border-bottom:1px solid var(--line)}
.srch input,.plrow input{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:5px;background:#111419;color:#fff;font-size:13px;outline:0}
.srch input:focus,.plrow input:focus{border-color:#d84b65}
.sortbar{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:12px;flex-wrap:wrap}
.sortbar button{padding:6px 9px;background:var(--panel2);border:1px solid var(--line);border-radius:5px;color:#ddd;font-size:12px}
.sortbar select{max-width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:5px;background:var(--panel2);font-size:12px}
.sortbar .audio-note{flex:1;min-width:100%;font-size:10px}
.lib-actions{display:flex;justify-content:flex-end}
.lib-actions button{border:0;border-radius:5px;padding:7px 10px;background:var(--accent);color:#fff;font-size:12px}
.scrollarea{flex:1;min-height:0;overflow-y:auto;scrollbar-color:#4a4d52 transparent;scrollbar-width:thin}
.it{min-height:50px;padding:10px 14px;cursor:pointer;border-bottom:1px solid #ffffff0a;display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:15px;color:#dbdce0}
.it:hover{background:#ffffff0a}
.it.cur{background:#ed31571a;color:#fff;border-left:2px solid var(--accent)}
.it>span:first-child{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.it .op{display:flex;gap:4px;flex-shrink:0}
.it .op button{background:transparent;border:0;color:#aaa;cursor:pointer;font-size:15px;padding:4px 6px}
.it .op button:hover{color:var(--accent2)}
.song-info{width:min(460px,calc(100vw - 32px));background:var(--panel);border:1px solid var(--line);border-radius:8px;box-shadow:0 12px 40px #000b;padding:18px}
.song-info h3{font-size:16px;color:var(--text);margin-bottom:14px;overflow-wrap:anywhere}
.song-info-row{display:grid;grid-template-columns:88px 1fr;gap:12px;padding:9px 0;border-top:1px solid var(--line);font-size:13px;line-height:1.5}
.song-info-row span:first-child{color:var(--muted)}
.song-info-row span:last-child{overflow-wrap:anywhere}
.plbox{padding:12px;overflow-y:auto}
.plrow{display:flex;gap:8px;margin-bottom:10px}
.plitem{border-bottom:1px solid #ffffff12}
.plitem .plrow{display:flex;justify-content:space-between;align-items:center;padding:12px 5px;cursor:pointer;margin:0}
.plrow .pname{font-size:14px;flex:1}
.plrow .pa{display:flex;gap:6px}
.plrow .pa button{border:1px solid var(--line);border-radius:5px;padding:6px 9px;cursor:pointer;font-size:11px;background:var(--panel2);color:#ddd}
.plrow .pa .play{background:var(--accent);color:#fff;border-color:var(--accent)}
.pltrs{display:none;padding:4px 8px 8px}
.pltrs.open{display:block}
.pltr{font-size:12px;color:#aaa;padding:8px 0;display:flex;justify-content:space-between;gap:8px;border-bottom:1px solid #ffffff0b;cursor:pointer}
.pltr span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pltr button{background:transparent;border:0;color:#999;cursor:pointer;font-size:16px}
.pltr button:hover{color:var(--accent2)}
.q{display:flex;flex-direction:column;min-height:0}
.q .qh{display:flex;justify-content:space-between;align-items:center;padding:14px 16px 9px;border-bottom:1px solid var(--line)}
.q .qh h4{font-size:13px;color:#ddd}
.q .qh span{display:flex;gap:6px}
.q .qh button{padding:6px 8px;background:transparent;border:1px solid var(--line);color:#bbb;border-radius:5px}
.qi{font-size:15px;padding:12px 14px;color:#b9bbc0;border-bottom:1px solid #ffffff0a;overflow-wrap:anywhere;cursor:pointer}
.qi:hover{background:#ffffff08}
.qi.c{color:var(--accent2);font-weight:600;background:#ed315710}
.hide{display:none!important}
#ovl{display:none;position:fixed;inset:0;background:#0009;z-index:999;align-items:center;justify-content:center}
.ovl-box{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:20px;min-width:260px;max-width:90%}
.ovl-box h3{font-size:16px;color:#ddd;margin-bottom:12px}
.ovl-item{padding:10px 14px;background:var(--panel2);border-radius:5px;margin-bottom:6px;cursor:pointer;font-size:14px}
.ovl-item:hover{background:var(--accent);color:#fff}
.ovl-cancel{width:100%;padding:8px;border:1px solid var(--line);border-radius:5px;background:transparent;color:#bbb;cursor:pointer;margin-top:6px}

@media(max-width:820px){
  body{height:auto;min-height:100dvh;overflow-x:hidden;overflow-y:auto}
  .topline{height:40px;padding:0 14px}
  .top-right{gap:8px}
  .volume-control input{width:64px}
  .workspace{flex:none;min-height:0;grid-template-columns:1fr;grid-template-rows:auto auto;gap:14px;padding:12px;overflow:visible}
  .listen{padding:10px 8px 20px}
  .turntable{width:min(38vh,220px);margin-bottom:18px}
  .track-title{font-size:22px}
  .controls{margin-top:16px;gap:18px}
  .play-btn{width:50px;height:50px}
  .seekrow{margin-top:14px}
  .lyrics-pane{min-height:300px}
  .sortbar .audio-note{display:none}
  .library-modal{top:auto;bottom:0;left:0;right:0;width:100%;height:70vh;border-radius:16px 16px 0 0;transform:translateY(0)}
  .library-modal.hide{transform:translateY(100%)}
    /* --- 新增：限制手机端歌词框高度，只显示约 5 行 --- */
  .lyrics-view {
    flex: none;            /* 取消弹性拉伸 */
    height: 320px;         /* 固定高度，大约显示 5~6 行 */
    padding: 20px 16px;    /* 缩小手机端内边距，让空间更充裕 */
  }
}

@media(max-width:600px){.brand{font-size:13px;letter-spacing:1px}.topline{padding:0 8px}.top-right{gap:5px}.top-actions>span{display:none}.modal-open-btn{padding:5px 7px}.volume-control{gap:4px;font-size:10px}.volume-control input{width:54px}}
@media(max-width:430px){.workspace{padding:8px;gap:8px}.listen{padding:8px}.turntable{width:min(32vh,180px);margin-bottom:12px}.track-title{font-size:19px}.track-sub{font-size:13px}.controls{gap:12px}.audio{gap:6px;padding:8px;font-size:10px}}
@media(max-height:850px) and (min-height:500px) and (min-width:821px){body{height:100vh;min-height:0;overflow:hidden}.workspace{flex:1;min-height:0;overflow:hidden;padding:8px 20px}.listen{justify-content:center;padding:20px 16px 8px}.turntable{width:min(34vw,calc(100vh - 300px),340px);margin-bottom:8px}.track-title{font-size:clamp(20px,2.4vw,30px)}.track-sub{margin-top:4px;font-size:13px}.track-pos{margin-top:8px}.controls{margin-top:12px;gap:16px}.play-btn{width:50px;height:50px}.seekrow{margin-top:12px}}
@media(max-height:499px){body{height:auto;min-height:100vh;overflow-x:hidden;overflow-y:auto}.workspace{flex:none;min-height:0;overflow:visible}.listen{justify-content:flex-start}}
@media(orientation:landscape) and (max-height:780px) and (min-width:769px){body{height:auto;min-height:100dvh;overflow-x:hidden;overflow-y:auto}.workspace{flex:none;min-height:0;height:calc(100dvh - 80px);overflow:hidden;grid-template-columns:1fr 1fr;gap:16px;padding:16px 20px}.listen{padding:12px 16px 8px;display:flex;flex-direction:column;min-height:0}.turntable{width:min(28vh,260px);margin-bottom:12px}.track-title{font-size:clamp(18px,2.2vw,28px)}.track-sub{font-size:12px;margin-top:4px}.track-pos{margin-top:6px;font-size:11px}.controls{margin-top:14px;gap:14px}.play-btn{width:48px;height:48px;font-size:22px}.icon-btn{width:36px;height:36px;font-size:18px}.seekrow{margin-top:10px}.lyrics-pane{min-height:0;overflow:hidden;display:flex;flex-direction:column}.lyrics-view{padding:2vh 16px}}
@media(max-width:820px){.topline{position:relative;z-index:120;height:48px;flex:none;padding:0 8px;letter-spacing:0}.brand{white-space:nowrap}.top-right{gap:8px}.mobile-menu-toggle{display:inline-flex;align-items:center;justify-content:center;width:38px;height:34px;padding:0;border:1px solid var(--line);border-radius:5px;background:var(--panel2);color:#ddd;font-size:20px;line-height:1}.top-actions{display:none;position:absolute;top:calc(100% + 6px);right:8px;z-index:130;width:min(220px,calc(100vw - 16px));padding:6px;background:var(--panel);border:1px solid var(--line);border-radius:6px;box-shadow:0 10px 28px #000b;flex-direction:column;align-items:stretch;gap:5px}.top-actions.open{display:flex}.top-actions>*{order:initial}.top-actions .modal-open-btn{width:100%;text-align:left;white-space:nowrap}}
</style></head><body>

<div class="topline">
  <div class="brand">MPD <i>STUDIO</i></div>
  <div class="top-right">
    <label class="volume-control" title="音量"><span>🔊</span><input type="range" min="0" max="100" id="vol" onchange="sv(this.value)"><span id="vl">--%</span></label>
    <button class="mobile-menu-toggle" id="mobileMenuToggle" type="button" aria-label="更多操作" aria-expanded="false" onclick="toggleMobileMenu()">☰</button>
    <div class="top-actions" id="topActions">
      <select class="service-select" id="serviceSelect" aria-label="选择音乐服务" onchange="changeService(this.value)"><option value="mpd">MPD</option><option value="owntone">OwnTone</option></select>
      <button class="modal-open-btn" onclick="openBgPicker()">🖼 背景</button>
      <a class="modal-open-btn" href="/video" target="_blank" rel="noopener" style="text-decoration:none">🎬 视频</a>
      <a class="modal-open-btn" href="/adb" target="_blank" rel="noopener" style="text-decoration:none">ADB 控制</a>
      <button class="modal-open-btn" onclick="logout()">退出</button>
      <button class="modal-open-btn" id="btnOpenLibrary" onclick="toggleLibrary()">📚 媒体库</button>
      <span>LOCAL MUSIC PLAYER</span>
    </div>
  </div>
</div>
<main class="workspace">
  <!-- 左侧播放器 -->
  <section class="listen">
    <div class="turntable"><div class="artwork" id="artwork"><div class="art-label" id="artlabel">MPD<br>PLAYER</div><img class="cover-image" id="cover" alt="当前歌曲专辑封面" onload="coverLoaded()" onerror="coverMissing()"></div><div class="tonearm" aria-hidden="true"><span class="tonearm-pivot"></span><span class="tonearm-shaft"></span><span class="tonearm-head"></span></div></div>
    <div class="track-title" id="sn">未播放</div>
    <div class="track-sub" id="sm"><span id="playStatus">--:-- / --:--</span><span id="audioFormat" class="audio-format-inline"></span></div>
    <div class="track-pos" id="trackpos">MPD MUSIC LIBRARY</div>
    <div class="controls">
      <button class="icon-btn" id="brn" title="随机播放" onclick="cmd('random')">⤨</button>
      <button class="icon-btn" title="上一首" onclick="cmd('prev')">|◀</button>
      <button class="play-btn" id="pb" title="播放/暂停" onclick="cmd('toggle')">▶</button>
      <button class="icon-btn" title="下一首" onclick="cmd('next')">▶|</button>
      <button class="icon-btn" id="br" title="重复播放" onclick="cmd('repeat')">↻</button>
    </div>
    <div class="seekrow"><span id="elapsed">0:00</span><div class="pb" id="progress" role="slider" tabindex="0" aria-label="播放进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" onclick="seekTo(event)" onkeydown="seekByKey(event)"><div class="pf" id="pf"></div></div><span id="duration">0:00</span></div>
    <div class="stream-control"><span id="streamNotice">网页音频状态检测中</span><button id="streamReconnect" type="button" onclick="connectStream()">连接 / 重连声音</button></div>
    <audio id="streamAudio" preload="none"></audio>
  </section>

  <!-- 右侧歌词区域 -->
   <section class="lyrics-pane">
      <div class="lyrics-header">
            <span class="lyrics-zoom">
            <button onclick="adjustLyricOffset(-0.5)" title="歌词提前 0.5 秒" aria-label="歌词提前 0.5 秒">-0.5s</button>
            <span class="lyric-offset" id="lyricOffsetValue">0.0s</span>
            <button onclick="adjustLyricOffset(0.5)" title="歌词延后 0.5 秒" aria-label="歌词延后 0.5 秒">+0.5s</button>
            <button onclick="changeFontSize(-1)" title="减小字号">A-</button>
            <button onclick="changeFontSize(1)" title="增大字号">A+</button>
        </span>
      </div>
    <div class="lyrics-view" id="lyrics">
      <div class="lyric-empty">暂无歌词</div>
    </div>
  </section>
</main>

<!-- 浮动媒体库窗口 -->
<div class="library-modal hide" id="libraryModal">
  <div class="modal-head">
    <div class="modal-tabs">
      <button class="tab on" id="t_lib" onclick="sw('lib')">曲库</button>
      <button class="tab" id="t_pl" onclick="sw('pl')">歌单</button>
      <button class="tab" id="t_queue" onclick="sw('queue')">播放列表</button>
    </div>
    <button class="modal-close" onclick="toggleLibrary()">×</button>
  </div>
  <div class="audio"><strong>USB</strong><span id="usbstate" class="audio-note">检测中...</span><strong id="outputHeading">MPD 输出</strong><div id="outputOptions" class="output-options">读取中...</div><span id="streamStatus" class="stream-status"></span><button id="configureUsbOutput" onclick="configureAudio()">配置 USB 输出</button></div>
  <div class="modal-content">
    <div class="panel-tools" id="libtools">
      <div class="srch" id="sbox"><input type="text" id="sq" placeholder="搜索曲库" oninput="flt()"></div>
      <div class="sortbar" id="sortbar"><label for="sortby">排序</label><select id="sortby" onchange="flt()"><option value="name">文件名</option><option value="size">文件大小</option><option value="time">文件时间</option></select><button id="sortdir" onclick="toggleSort()">升序</button><span id="timehint" class="audio-note">创建时间优先，缺失时按修改时间排序</span><div class="lib-actions"><button onclick="addAll()">整库入队</button></div></div>
    </div>
    <div class="scrollarea" id="lib"></div>
    <div class="scrollarea plbox hide" id="plbox"><div class="plrow"><input type="text" id="pln" placeholder="保存当前队列为歌单"><button onclick="sp()">保存</button><button onclick="document.getElementById('plfile').click()">导入 M3U</button><input type="file" id="plfile" accept=".m3u,.m3u8,audio/x-mpegurl" style="display:none" onchange="importM3u(this.files[0]);this.value=''"></div><div id="pllist"></div></div>
    <div class="scrollarea q hide" id="queuepane"><div class="qh"><h4>当前队列</h4><span><button onclick="clrc()">清空</button><button onclick="saveq()">保存歌单</button></span></div><div id="ql"></div></div>
  </div>
</div>
<div id="ovl"></div>
<input type="file" id="bgfile" accept="image/*" style="display:none" onchange="uploadBackground(this.files[0]);this.value=''">

<script>
let files=[];
let sortDescending=false;
let currentPlayingUri='';
let displayedCoverUri='';
let selectedService='mpd';
let lyrics=[];
let lyricsUri='';
let activeLyricIndex=-2;
let playbackTimeSeconds=0;
let playbackDurationSeconds=0;
let playbackUpdatedAt=performance.now();
let playbackRunning=false;
let streamAutoplayAttempted=false;
let httpOutputEnabled=false;
let lyricFontSize=15;
const HTTP_STREAM_BUFFER_SECONDS=6;
const DEFAULT_HTTP_LYRIC_DELAY_SECONDS=HTTP_STREAM_BUFFER_SECONDS;
let lyricOffsetSeconds=0;
try{
  const savedFineTune=localStorage.getItem('mpdLyricFineTune');
  const savedLyricOffset=localStorage.getItem('mpdLyricOffset');
  const savedValue=savedFineTune!==null
    ?Number(savedFineTune)
    :savedLyricOffset!==null
      ?Number(savedLyricOffset)-DEFAULT_HTTP_LYRIC_DELAY_SECONDS
      :0;
  if(Number.isFinite(savedValue))lyricOffsetSeconds=Math.max(-10,Math.min(10,savedValue));
  if(savedFineTune===null&&savedLyricOffset!==null){
    localStorage.setItem('mpdLyricFineTune',String(lyricOffsetSeconds));
  }
}catch(error){}

function toggleLibrary(){
  const modal=document.getElementById('libraryModal');
  modal.classList.toggle('hide');
  if(!modal.classList.contains('hide')){
    sw('lib');
    document.getElementById('sq').value='';
    loadLibrary(true);
  }
}
function toggleMobileMenu(){
  const menu=document.getElementById('topActions');
  const button=document.getElementById('mobileMenuToggle');
  const isOpen=menu.classList.toggle('open');
  button.setAttribute('aria-expanded',String(isOpen));
}
function closeMobileMenu(){
  document.getElementById('topActions').classList.remove('open');
  document.getElementById('mobileMenuToggle').setAttribute('aria-expanded','false');
}
document.getElementById('topActions').addEventListener('click',function(e){
  if(e.target.closest('button,a'))closeMobileMenu();
});
async function logout(){
  const response=await fetch('/logout',{method:'POST'});
  if(response.ok)location.assign('/login');
}
async function changeService(service){
  const response=await fetch('/api/service',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service})});
  if(!response.ok){alert('切换音乐服务失败');return}
  location.reload();
}

/* ---------------- 歌词字号调整 ---------------- */
function changeFontSize(delta){
  lyricFontSize += delta * 2;
  if(lyricFontSize < 10) lyricFontSize = 10;
  if(lyricFontSize > 40) lyricFontSize = 40;
  document.getElementById('lyrics').style.setProperty('--lyric-font-size', lyricFontSize + 'px');
}
function adjustLyricOffset(delta){
  lyricOffsetSeconds=Math.max(-10,Math.min(10,Math.round((lyricOffsetSeconds+delta)*2)/2));
  try{localStorage.setItem('mpdLyricFineTune',String(lyricOffsetSeconds));}catch(error){}
  updateLyricOffsetDisplay();
  const elapsed=playbackTimeSeconds+(playbackRunning?(performance.now()-playbackUpdatedAt)/1000:0);
  renderLyrics(elapsed);
}
function effectiveLyricOffset(){
  return lyricOffsetSeconds+(httpOutputEnabled?DEFAULT_HTTP_LYRIC_DELAY_SECONDS:0);
}
function updateLyricOffsetDisplay(){
  const offset=effectiveLyricOffset();
  const sign=offset>0?'+':'';
  document.getElementById('lyricOffsetValue').textContent=sign+offset.toFixed(1)+'s';
}

/* ---------------- 背景图 ---------------- */
function applyBackground(){
  const url='/api/background?t='+Date.now();
  const probe=new Image();
  probe.onload=function(){
    document.body.style.backgroundImage='linear-gradient(rgba(10,11,14,.55),rgba(10,11,14,.55)),url("'+url+'")';
    document.body.style.backgroundSize='cover,cover';
    document.body.style.backgroundPosition='center,center';
    document.body.style.backgroundRepeat='no-repeat,no-repeat';
  };
  probe.onerror=function(){
    document.body.style.backgroundImage='';
    document.body.style.backgroundSize='';
    document.body.style.backgroundPosition='';
    document.body.style.backgroundRepeat='';
  };
  probe.src=url;
}
function openBgPicker(){
  var ov=document.getElementById('ovl');
  ov.innerHTML='<div class="ovl-box"><h3>更换背景图</h3>'+
    '<div class="ovl-item" id="bgpick">📁 选择本地图片</div>'+
    '<div class="ovl-item" id="bgclear">↺ 恢复默认背景</div>'+
    '<button class="ovl-cancel" id="bgcancel">取消</button></div>';
  ov.style.display='flex';
  ov.onclick=function(e){
    if(e.target===ov||e.target.id==='bgcancel'){ov.style.display='none';return}
    if(e.target.id==='bgpick'){ov.style.display='none';document.getElementById('bgfile').click();}
    else if(e.target.id==='bgclear'){ov.style.display='none';clearBackground();}
  };
}
function uploadBackground(file){
  if(!file)return;
  if(file.size>8*1024*1024){alert('图片不能超过 8MB');return}
  const form=new FormData();
  form.append('image',file);
  fetch('/api/background',{method:'POST',body:form})
    .then(r=>r.json().then(d=>({ok:r.ok,data:d})))
    .then(res=>{
      if(!res.ok){alert((res.data&&res.data.error)||'上传失败');return}
      applyBackground();
    })
    .catch(()=>alert('上传失败，请检查服务是否可用'));
}
function clearBackground(){
  fetch('/api/background',{method:'DELETE'}).then(()=>{
    document.body.style.backgroundImage='';
    document.body.style.backgroundSize='';
    document.body.style.backgroundPosition='';
    document.body.style.backgroundRepeat='';
  }).catch(()=>{});
}

async function startStreamPlayback(){
  const streamAudio=document.getElementById('streamAudio');
  streamAutoplayAttempted=true;
  setStreamStatus('正在连接 MPD 音频流...');
  try{
    await streamAudio.play();
  }catch(error){
    const message=error.name==='NotAllowedError'
      ?'浏览器拦截自动播放，请点击“连接 / 重连声音”'
      :'HTTP 音频连接失败，请检查 8000 端口和 MPD 输出';
    setStreamStatus(message);
  }
}
function setStreamStatus(message){
  document.getElementById('streamStatus').textContent=message;
  document.getElementById('streamNotice').textContent=message;
}
function connectStream(){
  if(!httpOutputEnabled){
    setStreamStatus('请先在媒体库启用 MPD HTTP Stream 输出');
    return;
  }
  resetStreamSource();
  streamAutoplayAttempted=false;
  startStreamPlayback();
}
function resetStreamSource(){
  const streamAudio=document.getElementById('streamAudio');
  streamAudio.pause();
  streamAudio.removeAttribute('src');
  streamAudio.load();
  streamAudio.src='http://'+window.location.hostname+':8000/';
  streamAudio.load();
}
function cmd(c){
  if(c==='toggle'){
    const streamAudio=document.getElementById('streamAudio');
    if(playbackRunning&&httpOutputEnabled&&(streamAudio.paused||streamAudio.error)){
      if(streamAudio.error)resetStreamSource();
      streamAutoplayAttempted=false;
      startStreamPlayback();
      return;
    }
    if(playbackRunning&&httpOutputEnabled){
      streamAudio.pause();
      setStreamStatus('本机接收已暂停');
    }else if(!playbackRunning&&httpOutputEnabled){
      resetStreamSource();
      streamAutoplayAttempted=false;
      startStreamPlayback();
    }
  }
  fetch('/api/control/'+c).then(()=>rd());
}
document.addEventListener('keydown',function(e){
  if(e.repeat||e.target.closest('input, textarea, select, button, a, [contenteditable="true"], [role="slider"]'))return;
  if(e.code==='Space'){
    e.preventDefault();
    cmd('toggle');
  } else if(e.key.toLowerCase()==='n'){
    cmd('next');
  }
});
function sv(v){
  fetch('/api/volume/'+v);
  document.getElementById('vl').textContent=v+'%';
  document.getElementById('streamAudio').volume=Math.max(0,Math.min(100,Number(v)))/100;
}
function displayMusicUri(uri){
  const value=String(uri||'');
  if(value.startsWith('file:/music/'))return value.slice(12);
  if(value.startsWith('/music/'))return value.slice(7);
  return value;
}
function seekTo(e){
  const bar=e.currentTarget.getBoundingClientRect();
  sendSeek((e.clientX-bar.left)/bar.width*100);
}
function seekByKey(e){
  const bar=e.currentTarget;
  let value=Number(bar.getAttribute('aria-valuenow'));
  if(e.key==='ArrowLeft')value-=5;
  else if(e.key==='ArrowRight')value+=5;
  else if(e.key==='Home')value=0;
  else if(e.key==='End')value=100;
  else return;
  e.preventDefault();sendSeek(value);
}
function parseTimecode(value){
  const parts=value.split(':').map(Number);
  if(parts.some(part=>!Number.isFinite(part)))return 0;
  if(parts.length===2)return parts[0]*60+parts[1];
  if(parts.length===3)return parts[0]*3600+parts[1]*60+parts[2];
  return 0;
}
function formatTimecode(seconds){
  const whole=Math.max(0,Math.floor(seconds));
  const hours=Math.floor(whole/3600);
  const minutes=Math.floor((whole%3600)/60);
  const remainder=whole%60;
  return hours
    ?hours+':'+String(minutes).padStart(2,'0')+':'+String(remainder).padStart(2,'0')
    :Math.floor(whole/60)+':'+String(remainder).padStart(2,'0');
}
function updatePlaybackProgress(){
  if(playbackDurationSeconds<=0)return;
  const mpdElapsed=playbackTimeSeconds+(playbackRunning?(performance.now()-playbackUpdatedAt)/1000:0);
  const elapsed=Math.max(0,Math.min(
    playbackDurationSeconds,
    mpdElapsed-(httpOutputEnabled?HTTP_STREAM_BUFFER_SECONDS:0),
  ));
  const progress=elapsed/playbackDurationSeconds*100;
  document.getElementById('elapsed').textContent=formatTimecode(elapsed);
  document.getElementById('pf').style.width=progress+'%';
  document.getElementById('progress').setAttribute('aria-valuenow',String(Math.round(progress)));
}
function sendSeek(percent){
  const value=Math.max(0,Math.min(100,percent));
  fetch('/api/seek',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({percent:value})}).then(()=>rd());
}
function pf(u){fetch('/api/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uri:u})}).then(()=>rd())}
function playQueue(position){
  fetch('/api/queue/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({position})}).then(()=>rd());
}
function af(u){fetch('/api/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uri:u})}).then(()=>rd())}
function coverLoaded(){
  document.getElementById('cover').style.display='block';
  document.getElementById('artlabel').style.display='none';
}
function coverMissing(){
  document.getElementById('cover').style.display='none';
  document.getElementById('artlabel').style.display='grid';
}

async function loadLyrics(uri){
  lyricsUri=uri;
  lyrics=[];
  activeLyricIndex=-2;
  if(uri){
    try{
      const response=await fetch('/api/lyrics?uri='+encodeURIComponent(uri));
      if(response.ok){
        const data=await response.json();
        if(lyricsUri===uri){
          lyrics=data.lyrics||[];
        }
      }
    }catch(error){
      lyrics=[];
    }
  }
  if(lyricsUri!==uri)return;
  renderLyrics(0);
}

function renderLyrics(elapsed){
  const container=document.getElementById('lyrics');

  if(!lyrics.length){
    if(container.innerHTML!=='<div class="lyric-empty">暂无歌词</div>'){
      container.innerHTML = '<div class="lyric-empty">暂无歌词</div>';
    }
    return;
  }

  const lyricTime=elapsed-effectiveLyricOffset();
  let activeIndex=-1;
  for(let index=0;index<lyrics.length;index++){
    if(lyrics[index].time<=lyricTime)activeIndex=index;
    else break;
  }

  // 检查是否需要重建节点（只有在切换歌曲时才会触发，不会每 150ms 重建）
  let needsRebuild = false;
  if (container.children.length !== lyrics.length) {
    needsRebuild = true;
  } else {
    // 简单抽样检查，防止内容错乱
    for (let i = 0; i < lyrics.length; i++) {
      if (container.children[i].textContent !== lyrics[i].text) {
        needsRebuild = true;
        break;
      }
    }
  }

  // 如果节点数量不对，重建 DOM
  if (needsRebuild) {
    container.replaceChildren();
    lyrics.forEach(line=>{
      const row=document.createElement('div');
      row.className='lyric-line';
      row.textContent=line.text;
      container.appendChild(row);
    });
  }

  // 如果当前高亮行没变，直接返回，避免无意义的滚动
  if(activeIndex===activeLyricIndex)return;
  activeLyricIndex=activeIndex;

  // 更新高亮样式
  Array.from(container.children).forEach((row,index)=>{
    row.classList.toggle('active',index===activeIndex);
  });

  // 平滑滚动到当前激活的歌词
  const active=container.children[activeIndex];
  if(active){
    const containerHeight = container.clientHeight;
    const activeTop = active.offsetTop;
    const activeHeight = active.clientHeight;
    container.scrollTo({
      top: activeTop - containerHeight / 2 + activeHeight / 2,
      behavior: 'smooth'
    });
  }
}


function addAll(){
  fetch('/api/add-all').then(()=>rd()).catch(()=>alert('加入失败，请检查 MPD 服务'));
}
async function loadAudio(){
  const isMpd=selectedService==='mpd';
  document.querySelector('.audio').style.display='flex';
  document.querySelector('.stream-control').style.display=isMpd?'flex':'none';
  document.getElementById('streamAudio').style.display=isMpd?'':'none';
  document.getElementById('outputHeading').textContent=isMpd?'MPD 输出':'OwnTone 输出';
  document.getElementById('configureUsbOutput').style.display=isMpd?'':'none';
  let data;
  try{
    const response=await fetch('/api/audio');
    data=await response.json();
  }catch(error){
    document.getElementById('usbstate').textContent='设备检测失败';
    document.getElementById('outputOptions').textContent='无法读取 MPD 输出';
    return;
  }
  const usb=document.getElementById('usbstate');
  usb.textContent=data.usb_devices.length
    ?data.usb_devices.map(device=>device.name+'（'+device.description+'）').join('、')
    :'未检测到 USB 声卡';
  const options=document.getElementById('outputOptions');
  options.replaceChildren();
  if(!data.outputs.length){
    options.textContent='MPD 未配置输出设备';
    return;
  }
  data.outputs.forEach(output=>{
    const label=document.createElement('label');
    label.className='output-option';
    const checkbox=document.createElement('input');
    checkbox.type='checkbox';
    checkbox.checked=output.enabled;
    checkbox.addEventListener('change',()=>selectOutput(output.name,checkbox.checked));
    const name=document.createElement('span');
    name.textContent=output.name;
    label.append(checkbox,name);
    options.appendChild(label);
  });
  if(!isMpd){
    httpOutputEnabled=false;
    document.getElementById('streamAudio').pause();
    streamAutoplayAttempted=false;
    updateLyricOffsetDisplay();
    return;
  }
  const httpOutput=data.outputs.find(output=>output.name==='MPD HTTP Stream');
  const wasHttpOutputEnabled=httpOutputEnabled;
  httpOutputEnabled=Boolean(httpOutput&&httpOutput.enabled);
  updateLyricOffsetDisplay();
  if(!httpOutputEnabled){
    document.getElementById('streamAudio').pause();
    streamAutoplayAttempted=false;
    setStreamStatus('HTTP 流未启用');
    document.getElementById('streamReconnect').disabled=true;
  }else if(!wasHttpOutputEnabled&&playbackRunning){
    document.getElementById('streamReconnect').disabled=false;
    streamAutoplayAttempted=false;
    startStreamPlayback();
  }else{
    document.getElementById('streamReconnect').disabled=false;
    if(!playbackRunning)setStreamStatus('HTTP 输出已启用，等待 MPD 播放');
  }
}
async function selectOutput(name,enabled){
  const isHttpOutput=name==='MPD HTTP Stream';
  if(isHttpOutput){
    if(enabled){
      streamAutoplayAttempted=false;
      startStreamPlayback();
    }else{
      document.getElementById('streamAudio').pause();
      setStreamStatus('HTTP 流未启用');
    }
  }
  const response=await fetch('/api/audio/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,enabled})});
  const result=await response.json();
  if(!response.ok){alert(result.error||'设置输出失败');await loadAudio();return}
  if(isHttpOutput&&enabled){
    httpOutputEnabled=true;
    resetStreamSource();
    if(playbackRunning){
      streamAutoplayAttempted=false;
      startStreamPlayback();
    }else{
      setStreamStatus('HTTP 输出已启用，等待 MPD 播放');
    }
  }
  await loadAudio();
}
async function configureAudio(){
  try{
    const response=await fetch('/api/audio/configure',{method:'POST'});
    const result=await response.json();
    if(!response.ok){alert(result.error||'配置 USB 输出失败');return}
    await loadAudio();
    alert(result.message||'USB 输出已配置');
  }catch(error){
    alert('配置失败，请检查网页服务权限和 MPD 服务状态');
  }
}
function ap(u){
  fetch('/api/playlists').then(r=>r.json()).then(d=>{
    const playlists=d.playlist_items||d.playlists.map(name=>({id:name,name}));
    var ov=document.getElementById('ovl');
    ov.innerHTML='<div class="ovl-box"><h3>加到哪个歌单？</h3><div class="ovl-items"></div>'+
      '<div style="padding:8px 12px;border-top:1px solid #444;display:flex;gap:8px;">'+
      '<input type="text" id="newPlName" placeholder="新建歌单名称..." style="flex:1;padding:6px 10px;background:#2a2a2a;border:1px solid #555;color:#eee;border-radius:4px;font-size:13px;">'+
      '<button id="createPlBtn" style="padding:6px 16px;background:#c44;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:13px;">新建</button></div>'+
      '<button class="ovl-cancel" id="ovc">取消</button></div>';
    const items=ov.querySelector('.ovl-items');
    playlists.forEach(playlist=>{
      const item=document.createElement('div');
      item.className='ovl-item';
      item.dataset.name=playlist.id;
      item.dataset.label=playlist.name;
      item.textContent=playlist.name;
      items.appendChild(item);
    });
    const newPlInput=document.getElementById('newPlName');
    const createBtn=document.getElementById('createPlBtn');
    function doAdd(n,label){
      fetch('/api/playlists/addtrack',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,uri:u})})
        .then(async response=>{
          const result=await response.json();
          if(!response.ok){alert(result.error||'添加歌曲到歌单失败');return}
          ov.style.display='none';
          if(result.created){alert('已创建歌单「'+label+'」并加入歌曲')}
          else{alert('已加到「'+label+'」')}
        })
        .catch(()=>alert('添加歌曲到歌单失败，请检查音乐服务连接'));
    }
    createBtn.onclick=function(){
      const n=newPlInput.value.trim();
      if(!n){alert('请输入歌单名称');return}
      var bs=String.fromCharCode(92);
      if(n.indexOf('/')>=0||n.indexOf(bs)>=0){alert('歌单名称不能包含 '+bs+' 或 /');return}
      doAdd(n,n);
    };
    newPlInput.onkeydown=function(e){if(e.key==='Enter')createBtn.click()};
    ov.style.display='flex';
    ov.onclick=function(e){
      if(e.target.id==='ovc'||e.target===ov){ov.style.display='none';return}
      var item=e.target.closest('.ovl-item');
      if(item){
        doAdd(item.dataset.name,item.dataset.label);
      }
    };
  });
}
function flt(){
  const q=document.getElementById('sq').value.toLowerCase();
  const sortBy=document.getElementById('sortby').value;
  const visible=files.filter(file=>file.uri.toLowerCase().includes(q));
  visible.sort((a,b)=>{
    let comparison=sortBy==='name'?a.uri.localeCompare(b.uri,undefined,{numeric:true,sensitivity:'base'}):a[sortBy]-b[sortBy];
    if(sortBy==='time'&&a.time===0)return 1;
    if(sortBy==='time'&&b.time===0)return -1;
    return sortDescending?-comparison:comparison;
  });
  render(visible);
}
async function loadLibrary(focusCurrent=false){
  try{
    const requests=[fetch('/api/library')];
    if(focusCurrent)requests.push(fetch('/api/status'));
    const responses=await Promise.all(requests);
    if(!responses[0].ok)return;
    const data=await responses[0].json();
    if(focusCurrent&&responses[1].ok){
      const statusData=await responses[1].json();
      currentPlayingUri=statusData.status.uri||'';
    }
    files=data.library||[];
    flt();
    if(focusCurrent)focusCurrentTrack();
  }catch(error){}
}
function focusCurrentTrack(){
  const container=document.getElementById('lib');
  const rows=Array.from(container.children);
  const row=rows.find(item=>item.dataset.uri===currentPlayingUri);
  if(row){
    const rowBounds=row.getBoundingClientRect();
    const containerBounds=container.getBoundingClientRect();
    container.scrollTop+=rowBounds.top-containerBounds.top-(container.clientHeight-row.clientHeight)/2;
  }
}
function toggleSort(){
  sortDescending=!sortDescending;
  document.getElementById('sortdir').textContent=sortDescending?'降序':'升序';
  flt();
}
function render(list){
  const container=document.getElementById('lib');
  container.replaceChildren();
  list.forEach(file=>{
    const row=document.createElement('div');
    row.className='it'+(file.uri===currentPlayingUri?' cur':'');
    row.dataset.uri=file.uri;
    const name=document.createElement('span');
    name.textContent=file.display_name||file.uri;
    const actions=document.createElement('span');
    actions.className='op';
    const playlistButton=document.createElement('button');
    playlistButton.className='ap';
    playlistButton.title='加到歌单';
    playlistButton.textContent='♪';
    actions.appendChild(playlistButton);
    const queueButton=document.createElement('button');
    queueButton.className='af';
    queueButton.title='加入队列并播放';
    queueButton.textContent='▶';
    actions.appendChild(queueButton);
    row.append(name,actions);
    container.appendChild(row);
  });
}
document.getElementById('lib').onclick=function(e){
  const item=e.target.closest('.it');
  if(!item)return;
  const file=files.find(entry=>entry.uri===item.dataset.uri);
  if(!file)return;
  const u=file.uri;
  if(e.target.classList.contains('ap')){ap(u)}
  else if(e.target.classList.contains('af')){pf(u)}
  else{showSongInfo(file)}
};
function showSongInfo(file){
  const overlay=document.getElementById('ovl');
  const box=document.createElement('div');
  box.className='song-info';
  const heading=document.createElement('h3');
  heading.textContent=(file.display_name||file.uri).split('/').pop()||file.display_name||file.uri;
  box.appendChild(heading);
  const details=[
    ['文件路径',file.display_name||file.uri],
    ['文件大小',file.size?formatFileSize(file.size):'不可用'],
    [file.time_kind||'文件时间',file.time?new Date(file.time*1000).toLocaleString():'不可用'],
  ];
  details.forEach(([label,value])=>{
    const row=document.createElement('div');
    row.className='song-info-row';
    const labelElement=document.createElement('span');
    labelElement.textContent=label;
    const valueElement=document.createElement('span');
    valueElement.textContent=value;
    row.append(labelElement,valueElement);
    box.appendChild(row);
  });
  const close=document.createElement('button');
  close.className='ovl-cancel';
  close.textContent='关闭';
  close.onclick=function(event){
    event.stopPropagation();
    closeSongInfo();
  };
  box.appendChild(close);
  overlay.replaceChildren(box);
  overlay.style.display='flex';
  overlay.onclick=function(event){
    if(event.target===overlay)closeSongInfo();
  };
}
function closeSongInfo(){
  const overlay=document.getElementById('ovl');
  overlay.style.display='none';
  overlay.replaceChildren();
  overlay.onclick=null;
}
function formatFileSize(bytes){
  const units=['B','KB','MB','GB'];
  let size=bytes;
  let unit=0;
  while(size>=1024&&unit<units.length-1){size/=1024;unit++;}
  return size.toFixed(unit===0?0:1)+' '+units[unit];
}
async function loadpl(){
  const r=await fetch('/api/playlists');const d=await r.json();
  const el=document.getElementById('pllist');
  el.replaceChildren();
  if(!r.ok){el.textContent=d.error||'读取歌单失败';return}
  const playlists=d.playlist_items||[];
  if(!playlists.length){el.textContent='暂无歌单';return}
  playlists.forEach(playlist=>{
    const item=document.createElement('div');
    item.className='plitem';
    item.dataset.name=playlist.id;
    const row=document.createElement('div');
    row.className='plrow';
    const name=document.createElement('span');
    name.className='pname';
    name.textContent='▶ '+playlist.name;
    const actions=document.createElement('span');
    actions.className='pa';
    const play=document.createElement('button');
    play.className='play';
    play.textContent='播放';
    const remove=document.createElement('button');
    remove.className='del';
    remove.textContent='删除';
    actions.append(play,remove);
    row.append(name,actions);
    const tracks=document.createElement('div');
    tracks.className='pltrs';
    item.append(row,tracks);
    el.appendChild(item);
  });
  document.getElementById('pln').disabled=selectedService==='owntone';
  document.querySelector('#plbox .plrow button').disabled=selectedService==='owntone';
  document.querySelector('#plbox .plrow button:nth-of-type(2)').disabled=selectedService==='owntone';
}
document.getElementById('pllist').onclick=function(e){
  var item=e.target.closest('.plitem'); if(!item)return;
  var n=item.dataset.name;
  var trs=item.querySelector('.pltrs');
  if(e.target.classList.contains('play')){ll(n)}
  else if(e.target.classList.contains('del')){dp(n)}
  else if(e.target.closest('.pltr button')){
    const track=e.target.closest('.pltr');
    fetch('/api/playlists/removetrack',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,pos:Number(track.dataset.i)})})
      .then(()=>loadTrs(n,trs,true));
  }
  else if(e.target.closest('.pltr')){
    const track=e.target.closest('.pltr');
    pt(n,Number(track.dataset.i));
  }
  else if(e.target.classList.contains('pname')||e.target.classList.contains('plrow')){
    if(trs.classList.contains('open')){
      trs.classList.remove('open');
    } else {
      trs.classList.add('open');
      loadTrs(n,trs);
    }
  }
};
async function loadTrs(n,trs,refresh=false){
  if(trs.dataset.loading==='true'||(!refresh&&trs.dataset.loaded==='true'))return;
  trs.dataset.loading='true';
  const loading=document.createElement('div');
  loading.className='pltr';
  loading.textContent='读取中...';
  trs.replaceChildren(loading);
  try{
    const response=await fetch('/api/playlists/'+encodeURIComponent(n)+'/tracks');
    if(!response.ok)throw new Error('playlist request failed');
    const data=await response.json();
    const tracks=data.tracks||[];
    trs.replaceChildren();
    for(let start=0;start<tracks.length;start+=100){
      const fragment=document.createDocumentFragment();
      tracks.slice(start,start+100).forEach((track,index)=>{
        const position=start+index;
        const row=document.createElement('div');
        row.className='pltr';
        row.dataset.i=position;
        const label=document.createElement('span');
        label.textContent=(position+1)+'. '+track;
        row.appendChild(label);
        if(selectedService==='mpd'){
          const remove=document.createElement('button');
          remove.type='button';
          remove.title='从歌单移除';
          remove.textContent='×';
          row.appendChild(remove);
        }
        fragment.appendChild(row);
      });
      trs.appendChild(fragment);
      if(start+100<tracks.length){
        await new Promise(resolve=>requestAnimationFrame(resolve));
      }
    }
    if(!tracks.length)trs.textContent='歌单为空';
    trs.dataset.loaded='true';
  }catch(error){
    trs.textContent='读取歌单失败，请重试';
    trs.dataset.loaded='false';
  }finally{
    trs.dataset.loading='false';
  }
}
function sp(){
  const n=document.getElementById('pln').value.trim();
  if(!n)return;
  fetch('/api/playlists/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})})
  .then(()=>{document.getElementById('pln').value='';loadpl()});
}
async function importM3u(file){
  if(!file)return;
  const form=new FormData();
  form.append('file',file);
  const response=await fetch('/api/playlists/import',{method:'POST',body:form});
  const result=await response.json();
  if(!response.ok){alert(result.error||'导入歌单失败');return}
  await loadpl();
  alert('已导入「'+result.name+'」，共 '+result.count+' 首');
}
document.getElementById('ql').onclick=function(e){
  const row=e.target.closest('.qi');
  if(row)playQueue(Number(row.dataset.position));
};
async function saveq(){
  const n=prompt('歌单名称：');if(!n)return;
  try{
    const response=await fetch('/api/playlists/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});
    const result=await response.json();
    if(!response.ok){alert(result.error||'保存歌单失败');return}
    await loadpl();
    alert(result.message||'歌单已保存');
  }catch(error){
    alert('保存歌单失败，请检查音乐服务连接');
  }
}
function clrc(){
  if(!confirm('清空当前队列？'))return;
  fetch('/api/clear').then(()=>rd());
}
async function ll(n){
  try{
    const response=await fetch('/api/playlists/load',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});
    const result=await response.json();
    if(!response.ok){alert(result.error||'加载歌单失败');return}
    streamAutoplayAttempted=false;
    await rd();
    if(httpOutputEnabled){resetStreamSource();startStreamPlayback()}
  }catch(error){
    alert('加载歌单失败，请检查 MPD 连接');
  }
}
async function pt(n,pos){
  try{
    const response=await fetch('/api/playlists/playtrack',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,pos})});
    const result=await response.json();
    if(!response.ok){alert(result.error||'播放歌单歌曲失败');return}
    streamAutoplayAttempted=false;
    await rd();
    if(httpOutputEnabled){resetStreamSource();startStreamPlayback()}
  }catch(error){
    alert('播放歌单歌曲失败，请检查 MPD 连接');
  }
}
async function dp(n){
  if(!confirm('删除歌单「'+n+'」？'))return;
  try{
    const response=await fetch('/api/playlists/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});
    const result=await response.json();
    if(!response.ok){
      await loadpl();
      alert((result.error||'删除歌单失败')+'；已刷新歌单列表');
      return;
    }
    await loadpl();
  }catch(error){
    alert('删除歌单失败，请检查 MPD 连接');
  }
}
function sw(t){
  ['lib','pl','queue'].forEach(tab=>{
    const el = document.getElementById('t_'+tab);
    if (el) el.classList.toggle('on', tab===t);
  });
  const libtools = document.getElementById('libtools');
  if (libtools) libtools.classList.toggle('hide', t!=='lib');
  const libEl = document.getElementById('lib');
  if (libEl) libEl.classList.toggle('hide', t!=='lib');
  const plbox = document.getElementById('plbox');
  if (plbox) plbox.classList.toggle('hide', t!=='pl');
  const queuepane = document.getElementById('queuepane');
  if (queuepane) queuepane.classList.toggle('hide', t!=='queue');

  if(t==='pl') {
    loadpl();
  } else if(t==='queue') {
    // 队列数量更新在 rd() 中处理
  } else if(t==='lib') {
    // 曲库数量在初始化时处理
  }
}

async function rd(){
  const r=await fetch('/api/status');const d=await r.json();const s=d.status;
  currentPlayingUri=s.uri||'';
  document.querySelectorAll('#lib .it').forEach(row=>{
    row.classList.toggle('cur',row.dataset.uri===currentPlayingUri);
  });
  document.getElementById('sn').textContent=s.song||'未播放';
  const times=(s.time||'0:00 / 0:00').split(' / ');
  document.getElementById('duration').textContent=times[1]||'0:00';
  document.getElementById('playStatus').textContent=s.state==='playing'?'正在播放':'MPD 音乐库';
  document.getElementById('audioFormat').textContent=s.audio_format?' · '+s.audio_format:'';
  document.getElementById('trackpos').textContent=s.pos?'播放列表 '+s.pos:'MPD MUSIC LIBRARY';
  const playButton=document.getElementById('pb');
  playButton.textContent=s.state==='playing'?'⏸':'▶';
  document.getElementById('artwork').classList.toggle('playing',s.state==='playing');
  document.getElementById('artlabel').textContent=s.song?(s.song.split('/').pop()):'MPD PLAYER';
  if(s.uri!==displayedCoverUri){
    displayedCoverUri=s.uri;
    coverMissing();
    if(s.uri)document.getElementById('cover').src='/api/cover?uri='+encodeURIComponent(s.uri);
    else document.getElementById('cover').removeAttribute('src');
  }
  if(s.uri!==lyricsUri)loadLyrics(s.uri);
  document.getElementById('vol').value=s.volume;
  document.getElementById('vl').textContent=s.volume+'%';
  document.getElementById('streamAudio').volume=Math.max(0,Math.min(100,s.volume))/100;
  document.getElementById('br').classList.toggle('active',s.repeat);
  document.getElementById('brn').classList.toggle('active',s.random);
  
  const wasRunning=playbackRunning;
  playbackTimeSeconds=parseTimecode(times[0]||'0:00');
  playbackDurationSeconds=parseTimecode(times[1]||'0:00');
  playbackUpdatedAt=performance.now();
  playbackRunning=s.state==='playing';
  if(wasRunning&&!playbackRunning){streamAutoplayAttempted=false}
  updatePlaybackProgress();
  const streamAudio=document.getElementById('streamAudio');
  const needsStreamGesture=playbackRunning&&httpOutputEnabled&&(streamAudio.paused||streamAudio.error);
  if(needsStreamGesture){
    playButton.textContent='▶';
    playButton.title='连接网页音频流，或点击下方连接按钮';
  }else{
    playButton.title='播放/暂停';
  }
  if(playbackRunning&&httpOutputEnabled&&!streamAutoplayAttempted){resetStreamSource();startStreamPlayback()}
  
  renderLyrics(playbackTimeSeconds);

  const currentIndex=Number.parseInt((s.pos||'').split('/')[0],10)-1;
  const queue=document.getElementById('ql');
  queue.replaceChildren();
  
  const currentTab = document.querySelector('.modal-tabs .tab.on') ? document.querySelector('.modal-tabs .tab.on').id : 't_queue';
  
  d.playlist.forEach((uri,index)=>{
    const row=document.createElement('div');
    row.className='qi'+(index===currentIndex?' c':'');
    row.dataset.position=index;
    row.textContent=(index===currentIndex?'▶ ':'')+(index+1)+'. '+displayMusicUri(uri);
    queue.appendChild(row);
  });
}
async function init(){
  try{
    const serviceResponse=await fetch('/api/service');
    const serviceData=await serviceResponse.json();
    selectedService=serviceData.service||'mpd';
    document.getElementById('serviceSelect').value=selectedService;
  }catch(error){}
  const streamAudio=document.getElementById('streamAudio');
  streamAudio.src='http://'+window.location.hostname+':8000/';
  streamAudio.addEventListener('playing',()=>{
    setStreamStatus('正在接收 MPD 音频流');
  });
  streamAudio.addEventListener('error',()=>{
    if(httpOutputEnabled)setStreamStatus('音频流连接失败，请点击“连接 / 重连声音”或检查 MPD 的 8000 端口');
  });
  applyBackground();
  await loadLibrary();
  const timeKinds=[...new Set(files.map(file=>file.time_kind))];
  if(timeKinds.includes('修改时间'))document.getElementById('timehint').textContent='本系统未提供文件创建时间，按修改时间排序';
  if(timeKinds.includes('不可用')&&timeKinds.length===1)document.getElementById('timehint').textContent='文件时间不可读取';
  rd();loadAudio();setInterval(rd,3000);setInterval(loadAudio,10000);
  setInterval(()=>{
    const modal=document.getElementById('libraryModal');
    const libraryTab=document.getElementById('t_lib');
    if(!modal.classList.contains('hide')&&libraryTab.classList.contains('on'))loadLibrary();
  },30000);
  setInterval(()=>{
    const elapsed=playbackTimeSeconds+(playbackRunning?(performance.now()-playbackUpdatedAt)/1000:0);
    updatePlaybackProgress();
    renderLyrics(elapsed);
  },150);
  sw('lib');

  // 点击媒体库外部区域自动关闭
  document.addEventListener('click', function(e) {
    const modal = document.getElementById('libraryModal');
    const openBtn = document.getElementById('btnOpenLibrary');
    const menu = document.getElementById('topActions');
    const menuButton = document.getElementById('mobileMenuToggle');
    if (menu.classList.contains('open') && !menu.contains(e.target) && !menuButton.contains(e.target)) {
      closeMobileMenu();
    }
    if (!modal.classList.contains('hide') && !modal.contains(e.target) && !openBtn.contains(e.target)) {
      modal.classList.add('hide');
    }
  });
}
init();
</script></body></html>"""

def safe_login_target(value):
  target = str(value or '/')
  if not target.startswith('/') or target.startswith('//') or '\\' in target:
    return '/'
  return target

@app.route('/login', methods=['GET', 'POST'])
def login():
  configured = all(os.environ.get(name) for name in (
    'MPD_WEB_USERNAME', 'MPD_WEB_PASSWORD', 'MPD_WEB_SECRET_KEY'
  ))
  if request.method == 'GET':
    if configured and session.get('authenticated') is True:
      return redirect(safe_login_target(request.args.get('next')))
    page = LOGIN_PAGE_FILE.read_text(encoding='utf-8')
    return render_template_string(
      page,
      setup_required=not configured,
      next_path=safe_login_target(request.args.get('next')),
    )
  if not configured:
    return jsonify(ok=False, error='请先设置 MPD_WEB_USERNAME、MPD_WEB_PASSWORD 和 MPD_WEB_SECRET_KEY'), 503
  data = request.get_json(silent=True) or {}
  username = str(data.get('username', ''))
  password = str(data.get('password', ''))
  if not hmac.compare_digest(username, os.environ['MPD_WEB_USERNAME']) or not hmac.compare_digest(password, os.environ['MPD_WEB_PASSWORD']):
    return jsonify(ok=False, error='账号或密码错误'), 401
  session.clear()
  session['authenticated'] = True
  session.permanent = True
  return jsonify(ok=True, next=safe_login_target(data.get('next')))

@app.route('/logout', methods=['POST'])
def logout():
  session.clear()
  return jsonify(ok=True)
@app.route('/')
def index(): return render_template_string(PAGE)

# ---------------- 视频播放功能 ----------------
VIDEO_DIR = os.environ.get('VIDEO_DIR', '/mnt/usb1_2-1/Video')
VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.m4v', '.ts', '.mts'}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a', '.opus', '.ape', '.wv', '.aiff', '.alac'}
AUDIO_MIME = {
    '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.flac': 'audio/flac',
    '.aac': 'audio/aac', '.ogg': 'audio/ogg', '.m4a': 'audio/mp4',
    '.opus': 'audio/opus',
}
NATIVE_AUDIO = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.opus'}

@app.route('/api/video/list')
def api_video_list():
    path = request.args.get('path', '/')
    try:
        full_path = (Path(VIDEO_DIR) / path.lstrip('/')).resolve()
        if not str(full_path).startswith(str(Path(VIDEO_DIR).resolve())):
            return jsonify(ok=False, error='路径无效'), 403
        if not full_path.exists():
            return jsonify(ok=False, error='目录不存在'), 404
        items = []
        for item in sorted(full_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            rel_path = '/' + item.relative_to(Path(VIDEO_DIR)).as_posix()
            items.append({
                'name': item.name,
                'path': rel_path,
                'is_dir': item.is_dir(),
                'size': item.stat().st_size if item.is_file() else 0,
                'mtime': item.stat().st_mtime,
                'ext': item.suffix.lower()
            })
        return jsonify(ok=True, items=items, current_path=path)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.route('/api/video/play')
def api_video_play():
    path = request.args.get('path', '')
    try:
        full_path = (Path(VIDEO_DIR) / path.lstrip('/')).resolve()
        if not str(full_path).startswith(str(Path(VIDEO_DIR).resolve())):
            return jsonify(ok=False, error='路径无效'), 403
        if not full_path.is_file():
            return jsonify(ok=False, error='文件不存在'), 404
        ext = full_path.suffix.lower()
        if ext in ('.mp4', '.webm', '.m4v'):
            return send_file(full_path, mimetype=f'video/{ext[1:]}')
        elif ext in NATIVE_AUDIO:
            return send_file(full_path, mimetype=AUDIO_MIME.get(ext, 'audio/mpeg'))
        elif ext in VIDEO_EXTENSIONS:
            def generate():
                cmd = [
                    'ffmpeg', '-i', str(full_path),
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
                    '-c:a', 'aac', '-b:a', '128k',
                    '-movflags', 'frag_keyframe+empty_moov',
                    '-f', 'mp4', 'pipe:1'
                ]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                try:
                    while True:
                        chunk = proc.stdout.read(8192)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    proc.terminate()
            return Response(generate(), mimetype='video/mp4')
        elif ext in AUDIO_EXTENSIONS:
            def generate():
                cmd = [
                    'ffmpeg', '-i', str(full_path),
                    '-vn', '-c:a', 'libmp3lame', '-b:a', '192k',
                    '-f', 'mp3', 'pipe:1'
                ]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                try:
                    while True:
                        chunk = proc.stdout.read(8192)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    proc.terminate()
            return Response(generate(), mimetype='audio/mpeg')
        else:
            return jsonify(ok=False, error='不支持的媒体格式'), 400
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.route('/api/video/download')
def api_video_download():
    path = request.args.get('path', '')
    try:
        full_path = (Path(VIDEO_DIR) / path.lstrip('/')).resolve()
        if not str(full_path).startswith(str(Path(VIDEO_DIR).resolve())):
            return jsonify(ok=False, error='路径无效'), 403
        if not full_path.is_file():
            return jsonify(ok=False, error='文件不存在'), 404
        return send_file(full_path, as_attachment=True, download_name=full_path.name)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

if __name__=='__main__':
    threading.Thread(target=mpd_watchdog, name='mpd-watchdog', daemon=True).start()
    app.run(host='0.0.0.0',port=8800,debug=False,threaded=True)