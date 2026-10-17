#!/usr/bin/env python3
"""UFC 3 modern launcher: an Epic-Games-Launcher-style front end for the native PC port.

Standard library only (it runs with the bundled embeddable Python on Windows). It serves the UI in
ui/index.html on 127.0.0.1 and opens it in an app-style Edge/Chrome window.

It does not replace the proven start-up pipeline:
  * Windows: Play runs `UFC3.exe --play` (the existing launcher, headless) after writing the same
    <data>\\sacredfire.ini and bbport.ini it reads, so the old window and this one share settings.
  * Linux:   Play runs launcher/ufc3_online.run_session (the GTK launcher's session code).

DLSS 4 is gated here and never passed to the game unless every check in dlss_status() passes
(NVIDIA RTX GPU, engine build advertising the DLSS module, nvngx_dlss.dll present, no crash guard).
After a crash of a DLSS session the launcher switches DLSS off by itself (crash guard).
"""
import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

IS_WIN = os.name == 'nt'
HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('UFC3_ROOT') or HERE.parent.parent).resolve()
UI_FILE = HERE / 'ui' / 'index.html'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# ---- choices (the same lists, in the same order, as packaging/windows/sacredfire_launcher.c) ----
RESOLUTIONS = ['1280x720', '1920x1080', '2560x1440 (2K, experimental)', '3840x2160 (4K, experimental)']
DISPLAYS = ['Same as the game', '1920x1080 (Full HD)', '2560x1440 (2K)', '3840x2160 (4K)']
FPS_LIMITS = [('60', '60 FPS'), ('90', '90 FPS'), ('120', '120 FPS'), ('144', '144 FPS'), ('240', '240 FPS'),
              ('0', 'Unlimited')]
LEGACY_FPS_INDEX = [0, 2, 3, 4, 5]  # sacredfire.ini before 90 FPS stored an index into 60/120/144/240/0
PRESENT_MODES = ['Mailbox (capped at 2x refresh on NVIDIA)', 'VSync (FIFO)', 'Immediate (uncapped, may tear)']
PRESENT_IDS = ['Mailbox', 'Fifo', 'Immediate']
SHARPNESS = ['Off', 'Low', 'Medium', 'High']
SHARPNESS_ATTENUATION = ['2000', '1000', '500', '200']
LANGUAGES = [('1', 'English (US)'), ('2', 'French'), ('4', 'German'), ('5', 'Italian'), ('3', 'Spanish (Spain)'),
             ('20', 'Spanish (Latin America)'), ('17', 'Portuguese (Brazil)'), ('7', 'Portuguese (Portugal)'),
             ('16', 'Polish'), ('8', 'Russian'), ('0', 'Japanese'), ('10', 'Chinese (Traditional)')]
ONLINE_MODES = ['offline', 'join', 'host']
DLSS_MODES = [('quality', 'Quality'), ('balanced', 'Balanced'), ('performance', 'Performance'), ('dlaa', 'DLAA (native)')]
UPSCALER_IDS = ['off', 'dlss4', 'fsr1']

# Controls: key.<input>= / pad.<input>= lines of bbport.ini (same tables as the Win32 launcher).
CTL_INPUTS = ['cross', 'circle', 'square', 'triangle', 'l1', 'r1', 'l2', 'r2', 'l3', 'r3', 'options', 'touchpad',
              'touchpad_right', 'up', 'down', 'left', 'right', 'move_up', 'move_down', 'move_left', 'move_right',
              'look_up', 'look_down', 'look_left', 'look_right']
CTL_LABELS = ['Cross (X)', 'Circle', 'Square', 'Triangle', 'L1', 'R1', 'L2', 'R2', 'L3 (left stick click)',
              'R3 (right stick click)', 'Options', 'Touchpad (left half)', 'Touchpad (right half)', 'D-pad up',
              'D-pad down', 'D-pad left', 'D-pad right', 'Left stick up', 'Left stick down', 'Left stick left',
              'Left stick right', 'Right stick up', 'Right stick down', 'Right stick left', 'Right stick right']
CTL_DEFAULT_KEYS = ['Space', 'Left Shift', 'E', 'Q', '1', '3', 'R', 'F', 'Z', 'C', 'Return', 'Tab', 'Backspace',
                    'I', 'K', 'J', 'L', 'W', 'S', 'A', 'D', 'Up', 'Down', 'Left', 'Right']
CTL_DEFAULT_PADS = ['a', 'b', 'x', 'y', 'leftshoulder', 'rightshoulder', 'lefttrigger', 'righttrigger', 'leftstick',
                    'rightstick', 'start', 'back,touchpad', '', 'dpup', 'dpdown', 'dpleft', 'dpright']
CTL_PAD_NAMES = ['', 'a', 'b', 'x', 'y', 'leftshoulder', 'rightshoulder', 'lefttrigger', 'righttrigger', 'leftstick',
                 'rightstick', 'start', 'back', 'guide', 'touchpad', 'dpup', 'dpdown', 'dpleft', 'dpright', 'misc1',
                 'paddle1', 'paddle2', 'paddle3', 'paddle4']
CTL_PAD_LABELS = ['(none)', 'A / Cross', 'B / Circle', 'X / Square', 'Y / Triangle', 'LB / L1', 'RB / R1',
                  'LT / L2', 'RT / R2', 'Left stick click', 'Right stick click', 'Start / Options', 'Back / Share',
                  'Guide / PS', 'Touchpad click', 'D-pad up', 'D-pad down', 'D-pad left', 'D-pad right',
                  'Misc (Share / Capture)', 'Paddle 1', 'Paddle 2', 'Paddle 3', 'Paddle 4']
CTL_PAD_ROWS = 17  # the first 17 inputs have a gamepad button; the rest are the sticks themselves

DEFAULTS = dict(game='', player='Player 1', language='1', online_mode='offline', join_address='', host_address='',
                gamepad='', neo=False, fullscreen=False, clear_cache=False, validation=False, frame_stats=False,
                menu_trace=False, resolution=1, display=0, fps_limit='0', present_mode=2, sharpness=2,
                skip_movies=True, log_dir='', show_fps=True, upscaler='off', dlss_mode='quality')
BOOL_KEYS = ('neo', 'fullscreen', 'clear_cache', 'validation', 'frame_stats', 'menu_trace', 'skip_movies', 'show_fps')


# ---- small helpers ----------------------------------------------------------------------------
def clean(text, limit=1024):
    """A single-line string: control characters would let a value inject ini lines."""
    return re.sub(r'[\x00-\x1f\x7f]', '', str(text))[:limit].strip()


def clamp(value, count, default=0):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if 0 <= number < count else default


def data_dir():
    env = os.environ.get('BB_DATA_DIR')
    if env:
        return Path(env).resolve()
    if IS_WIN:
        return Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local').resolve() / 'UFC3Native'
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share').resolve() / 'UFC3Native'


def read_ini(path):
    values = {}
    try:
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            key, sep, value = line.partition('=')
            if sep and not line.lstrip().startswith('#'):
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def ini_set_many(path, updates, header='# bbport settings (in-game menu: Insert / L3+R3)'):
    """Replaces the keys' lines or appends them, keeping every other line (as the game's menu does)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    try:
        lines = path.read_text(encoding='utf-8-sig').splitlines()
    except OSError:
        pass
    pending, out = dict(updates), []
    for line in lines:
        key = line.partition('=')[0].strip()
        if '=' in line and not line.lstrip().startswith('#') and key in pending:
            out.append('%s=%s' % (key, pending.pop(key)))
        else:
            out.append(line)
    if not lines and header:
        out.append(header)
    out.extend('%s=%s' % item for item in pending.items())
    temp = path.with_name(path.name + '.tmp')
    temp.write_text('\n'.join(out) + '\n', encoding='utf-8')
    os.replace(temp, path)


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    os.replace(temp, path)


def run_text(command, timeout=8):
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
                              encoding='utf-8', errors='replace')
        return done.stdout if done.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError, ValueError):
        return ''


# ---- settings (<data>\sacredfire.ini + bbport.ini show_fps + launcher-modern.json) --------------
def load_settings(data):
    ini = read_ini(Path(data) / 'sacredfire.ini')
    modern = read_json(Path(data) / 'launcher-modern.json')
    s = dict(DEFAULTS)
    s['player'] = ini.get('player', s['player'])[:16]
    s['game'] = ini.get('game', '')
    s['gamepad'] = ini.get('gamepad', '')
    s['join_address'] = ini.get('online_server', '')
    s['host_address'] = ini.get('host_address', '')
    s['log_dir'] = ini.get('log_dir', '')
    mode = ini.get('online_mode')
    s['online_mode'] = mode if mode in ONLINE_MODES else ('join' if s['join_address'] else 'offline')
    for key in ('neo', 'fullscreen', 'clear_cache', 'validation', 'frame_stats', 'menu_trace', 'skip_movies'):
        if key in ini:
            s[key] = ini[key] == '1'
    s['resolution'] = clamp(ini.get('resolution', s['resolution']), len(RESOLUTIONS), DEFAULTS['resolution'])
    s['display'] = clamp(ini.get('display', 0), len(DISPLAYS))
    s['present_mode'] = clamp(ini.get('present_mode', s['present_mode']), len(PRESENT_MODES), DEFAULTS['present_mode'])
    s['sharpness'] = clamp(ini.get('sharpness', s['sharpness']), len(SHARPNESS), DEFAULTS['sharpness'])
    if ini.get('fps_limit') in [v for v, _ in FPS_LIMITS]:
        s['fps_limit'] = ini['fps_limit']
    elif 'fps_mode' in ini:
        s['fps_limit'] = FPS_LIMITS[LEGACY_FPS_INDEX[clamp(ini['fps_mode'], 5)]][0]
    if ini.get('language') in [v for v, _ in LANGUAGES]:
        s['language'] = ini['language']
    fsr1 = ini.get('upscaler') == '1'
    wanted = modern.get('upscaler')
    # DLSS lives in launcher-modern.json (the Win32 launcher knows two upscaler values only) and is
    # consistent with sacredfire.ini when that says "no FSR 1.0"; otherwise the ini wins.
    s['upscaler'] = 'fsr1' if fsr1 else ('dlss4' if wanted == 'dlss4' else 'off')
    if modern.get('dlss_mode') in [v for v, _ in DLSS_MODES]:
        s['dlss_mode'] = modern['dlss_mode']
    show = read_ini(bbport_path(data)).get('show_fps')
    s['show_fps'] = show is None or show == '1'  # on until the menu or a launcher turns it off
    return s


def bbport_path(data):
    return Path(os.environ.get('BB_CONFIG') or Path(data) / 'bbport.ini')


def normalize(update, current):
    """Validated copy of `current` with the known keys of `update` applied."""
    s = dict(current)
    for key, value in update.items():
        if key not in DEFAULTS:
            continue
        if key in BOOL_KEYS:
            s[key] = bool(value)
        elif key in ('resolution', 'display', 'present_mode', 'sharpness'):
            limit = {'resolution': len(RESOLUTIONS), 'display': len(DISPLAYS), 'present_mode': len(PRESENT_MODES),
                     'sharpness': len(SHARPNESS)}[key]
            s[key] = clamp(value, limit, current[key])
        elif key == 'fps_limit':
            s[key] = str(value) if str(value) in [v for v, _ in FPS_LIMITS] else current[key]
        elif key == 'language':
            s[key] = str(value) if str(value) in [v for v, _ in LANGUAGES] else current[key]
        elif key == 'online_mode':
            s[key] = value if value in ONLINE_MODES else current[key]
        elif key == 'upscaler':
            s[key] = value if value in UPSCALER_IDS else current[key]
        elif key == 'dlss_mode':
            s[key] = value if value in [v for v, _ in DLSS_MODES] else current[key]
        elif key == 'player':  # the PS4 allows 16 characters; the game reads bytes: printable ASCII only
            s[key] = ''.join(c for c in str(value) if 0x20 <= ord(c) < 0x7f)[:16]
        else:
            s[key] = clean(value)
    if s['resolution'] >= 2:
        s['neo'] = True  # 2K / 4K always run in PS4 Pro mode
    return s


def save_settings(data, s):
    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    fsr1 = s['upscaler'] == 'fsr1'
    lines = [
        'player=%s' % s['player'], 'online_mode=%s' % s['online_mode'], 'online_server=%s' % s['join_address'],
        'host_address=%s' % s['host_address'], 'game=%s' % s['game'], 'gamepad=%s' % s['gamepad'],
        'neo=%d' % s['neo'], 'fullscreen=%d' % s['fullscreen'], 'clear_cache=%d' % s['clear_cache'],
        'validation=%d' % s['validation'], 'frame_stats=%d' % s['frame_stats'], 'menu_trace=%d' % s['menu_trace'],
        'resolution=%d' % s['resolution'], 'fps_limit=%s' % s['fps_limit'], 'present_mode=%d' % s['present_mode'],
        'upscaler=%d' % fsr1, 'sharpness=%d' % s['sharpness'], 'display=%d' % s['display'],
        'skip_movies=%d' % s['skip_movies'], 'language=%s' % s['language'], 'log_dir=%s' % s['log_dir']]
    # Lines of keys this launcher does not know (a newer UFC3.exe may add some) are kept as they are.
    ini_set_many(data / 'sacredfire.ini', dict(line.split('=', 1) for line in lines), header=None)
    ini_set_many(bbport_path(data), {'show_fps': '1' if s['show_fps'] else '0'})
    modern = read_json(data / 'launcher-modern.json')
    modern.update(upscaler=s['upscaler'], dlss_mode=s['dlss_mode'])
    write_json(data / 'launcher-modern.json', modern)


# ---- controls -----------------------------------------------------------------------------------
def load_controls(data):
    ini = read_ini(bbport_path(data))
    rows = []
    for i, name in enumerate(CTL_INPUTS):
        rows.append(dict(id=name, label=CTL_LABELS[i], key=ini.get('key.' + name, CTL_DEFAULT_KEYS[i]),
                         pad=(ini.get('pad.' + name, CTL_DEFAULT_PADS[i]) if i < CTL_PAD_ROWS else None)))
    return rows


def save_controls(data, rows):
    by_id = {r.get('id'): r for r in rows if isinstance(r, dict)}
    updates, seen = {}, {}
    for i, name in enumerate(CTL_INPUTS):
        row = by_id.get(name, {})
        key = clean(row.get('key', CTL_DEFAULT_KEYS[i]), 64)
        if key and key.lower() in seen:  # one input per key: the later duplicate is cleared
            key = ''
        seen[key.lower()] = True
        updates['key.' + name] = key
        if i < CTL_PAD_ROWS:
            pad = clean(row.get('pad', CTL_DEFAULT_PADS[i]), 64)
            first = pad.split(',')[0]
            updates['pad.' + name] = pad if first in CTL_PAD_NAMES else CTL_DEFAULT_PADS[i]
    ini_set_many(bbport_path(data), updates)


# ---- GPU detection and DLSS gating ----------------------------------------------------------------
def classify_gpu(name, vendor='', pnp='', driver=''):
    text = ' '.join((name, vendor, pnp)).lower()
    if 'ven_10de' in text or 'nvidia' in text or 'geforce' in text:
        kind = 'nvidia'
    elif 'ven_1002' in text or 'radeon' in text or 'advanced micro' in text or re.search(r'\bamd\b', text):
        kind = 'amd'
    elif 'ven_8086' in text or 'intel' in text:
        kind = 'intel'
    else:
        kind = 'other'
    return dict(name=clean(name, 120) or 'Unknown GPU', vendor=kind, rtx=kind == 'nvidia' and bool(re.search(r'\brtx\b', name, re.I)),
                driver=clean(driver, 40))


def nvidia_public_driver(windows_version):
    """32.0.15.6094 -> 560.94 (the number NVIDIA prints)."""
    parts = windows_version.split('.')
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        digits = (parts[2] + parts[3])[-5:]
        return '%d.%s' % (int(digits[:3]), digits[3:])
    return windows_version


def parse_lspci(text):
    """GPUs from `lspci -mm -nn` (the bracketed marketing name when there is one)."""
    gpus = []
    for line in text.splitlines():
        if re.search(r'VGA|3D controller|Display controller', line):
            fields = re.findall(r'"([^"]*)"', line)
            if len(fields) >= 3:
                bracket = re.search(r'\[([^\]]*[A-Za-z][^\]]*)\]\s*\[[0-9a-f]{4}\]$', fields[2])
                name = bracket.group(1) if bracket else re.sub(r'\s*\[[0-9a-f]{4}\]$', '', fields[2])
                gpus.append(classify_gpu(name, fields[1]))
    return gpus


def detect_gpus():
    fake = os.environ.get('UFC3_FAKE_GPU')  # tests and support: UFC3_FAKE_GPU="NVIDIA GeForce RTX 4070;Intel UHD"
    if fake is not None:
        return [classify_gpu(n.strip()) for n in fake.split(';') if n.strip()]
    gpus = []
    if IS_WIN:
        out = run_text(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                        'Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,'
                        'DriverVersion,PNPDeviceID | ConvertTo-Json -Compress'])
        try:
            data = json.loads(out) if out.strip() else []
        except ValueError:
            data = []
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get('Name'):
                gpu = classify_gpu(item['Name'], item.get('AdapterCompatibility') or '', item.get('PNPDeviceID') or '',
                                   item.get('DriverVersion') or '')
                if gpu['vendor'] == 'nvidia':
                    gpu['driver'] = nvidia_public_driver(gpu['driver'])
                gpus.append(gpu)
        return gpus
    gpus = parse_lspci(run_text(['lspci', '-mm', '-nn']))
    smi = run_text(['nvidia-smi', '--query-gpu=name,driver_version', '--format=csv,noheader'])
    for line in smi.splitlines():
        name, _, driver = line.partition(',')
        if name.strip():
            gpus = [g for g in gpus if g['vendor'] != 'nvidia'] + [classify_gpu(name.strip(), 'NVIDIA', driver=driver.strip())]
            break
    return gpus


def engine_features():
    return read_json(ROOT / 'share' / 'engine-features.json')


def find_dlss_dll():
    for folder in (ROOT / 'bin', ROOT / 'share', ROOT):
        if (folder / 'nvngx_dlss.dll').is_file():
            return folder / 'nvngx_dlss.dll'
    return None


REASONS = {
    'ok': 'Available',
    'no_nvidia': 'DLSS needs an NVIDIA GeForce RTX graphics card. It is not available on AMD or Intel GPUs.',
    'not_rtx': 'DLSS needs an RTX card (RTX 20 series or newer). GTX and older cards cannot run it.',
    'engine_missing': 'DLSS 4 is selected, but this build of the game engine has no DLSS module yet: the game '
                      'runs with AMD FSR 1.0 until the engine gets it.',
    'dll_missing': 'DLSS 4 is selected, but nvngx_dlss.dll was not found in bin\\: the game runs with AMD FSR 1.0 '
                   'until the file is there.',
    'crash_guard': 'DLSS 4 was switched off automatically because the last DLSS session did not end cleanly.',
}


def dlss_status(gpus, guard):
    """The option is open on any RTX card unless the crash guard tripped. A missing engine module or a
    missing nvngx_dlss.dll no longer locks it: the game then runs the FSR 1.0 present path (`ready` is
    False and `notice` says so), so a selection can never leave the image un-upscaled or crash."""
    nvidia = [g for g in gpus if g['vendor'] == 'nvidia']
    code = 'ok'
    if not nvidia:
        code = 'no_nvidia'
    elif not any(g['rtx'] for g in nvidia):
        code = 'not_rtx'
    elif guard.get('disabled'):
        code = 'crash_guard'
    engine_module = bool(engine_features().get('dlss4'))
    dll_found = find_dlss_dll() is not None
    notice = ''
    if code == 'ok' and not engine_module:
        notice = REASONS['engine_missing']
    elif code == 'ok' and not dll_found:
        notice = REASONS['dll_missing']
    return dict(available=code == 'ok', code=code, reason=REASONS[code], engine_module=engine_module, dll_found=dll_found,
                ready=code == 'ok' and engine_module and dll_found, notice=notice,
                multi_gpu=len([g for g in gpus if g['vendor'] in ('nvidia', 'amd', 'intel')]) > 1 and code == 'ok')


def upscaler_options(status):
    return [
        dict(id='off', label='Off', badge='', desc='Plain bilinear scaling. Lowest latency, no extra GPU work.', available=True),
        dict(id='dlss4', label='NVIDIA DLSS 4', badge='RTX only', available=status['available'],
             code=status['code'], reason='' if status['available'] else status['reason'],
             desc='AI upscaling with the transformer model: sharper than FSR at the same speed.'),
        dict(id='fsr1', label='AMD FSR 1.0', badge='Any GPU', available=True,
             desc='Spatial upscaler for 720p/1080p on a larger screen. Works on every graphics card.'),
    ]


def effective_upscaler(settings, status):
    """What really runs: DLSS 4 only when every gate is open, else the safe FSR 1.0 fallback."""
    if settings['upscaler'] == 'dlss4' and not status['available']:
        return 'fsr1'
    return settings['upscaler']


def game_env(settings, status, inherited=None):
    """(variables to set, variables to remove, log notes) for the game, beyond what UFC3.exe sets.
    A variable the user exported before starting the launcher always wins, as everywhere in this port."""
    base = os.environ if inherited is None else inherited
    mode = effective_upscaler(settings, status)
    notes, setv, drop = [], {}, []

    def put(key, value):
        if key in base:
            notes.append('%s is set in your environment and wins over the launcher.' % key)
        else:
            setv[key] = value

    if settings['upscaler'] == 'dlss4' and mode != 'dlss4':
        notes.append('DLSS 4 is not available (%s) Using AMD FSR 1.0 instead.' % status['reason'])
    if mode == 'dlss4':
        # Contract with the engine's DLSS module (see README): if NGX fails to initialise, the engine must
        # fall back to FSR 1.0 by itself instead of aborting.
        put('BB_UPSCALER', 'dlss4')
        put('BB_DLSS_MODE', settings['dlss_mode'])
        put('BB_DLSS_FALLBACK', 'fsr1')
        put('BB_FSR1', '0' if status.get('ready', True) else '1')  # FSR 1.0 stays on until the engine has DLSS
        notes.append('Upscaling: NVIDIA DLSS 4 (%s)' % settings['dlss_mode'])
        if status.get('notice'):
            notes.append(status['notice'])
    else:
        for key in ('BB_UPSCALER', 'BB_DLSS_MODE', 'BB_DLSS_FALLBACK'):
            if key not in base:
                drop.append(key)
        if mode == 'fsr1':
            put('BB_FSR1', '1')  # also when DLSS was chosen but fell back
        notes.append('Upscaling: %s' % ('AMD FSR 1.0' if mode == 'fsr1' else 'off'))
    return setv, drop, notes


# ---- addresses, gamepads, folder dialogs ----------------------------------------------------------------
def local_addresses():
    items = []
    if IS_WIN:
        out = run_text(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                        'Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike "127.*" -and '
                        '$_.IPAddress -notlike "169.254.*"} | Select-Object IPAddress,InterfaceAlias | ConvertTo-Json -Compress'])
        try:
            data = json.loads(out) if out.strip() else []
        except ValueError:
            data = []
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get('IPAddress'):
                items.append((item['IPAddress'], item.get('InterfaceAlias') or '?'))
    else:
        try:
            for dev in json.loads(run_text(['ip', '-j', '-4', 'address', 'show', 'up']) or '[]'):
                for a in dev.get('addr_info', []):
                    ip = a.get('local', '')
                    if ip and not ip.startswith(('127.', '169.254.')):
                        items.append((ip, dev.get('ifname', '?')))
        except (ValueError, AttributeError):
            pass
    out = []
    for ip, name in items:
        parts = [int(p) for p in ip.split('.')] if re.fullmatch(r'\d+\.\d+\.\d+\.\d+', ip) else [0, 0, 0, 0]
        low = name.lower()
        if any(w in low for w in ('tailscale', 'zerotier', 'hamachi', 'radmin', 'wireguard', 'netbird')) or (parts[0] == 100 and 64 <= parts[1] < 128):
            rank, label = 1, 'VPN'
        elif any(w in low for w in ('vethernet', 'virtualbox', 'vmware', 'hyper-v', 'wsl', 'bluetooth', 'docker')):
            rank, label = 3, 'virtual adapter'
        elif parts[0] == 10 or (parts[0] == 172 and 16 <= parts[1] < 32) or (parts[0] == 192 and parts[1] == 168):
            rank, label = 0, 'LAN'
        else:
            rank, label = 2, 'public'
        out.append((rank, dict(ip=ip, label='%s (%s: %s)' % (ip, label, name), kind=label)))
    return [item for _, item in sorted(out, key=lambda x: x[0])]


def list_gamepads():
    exe = ROOT / 'bin' / ('bb-gpu-capabilities.exe' if IS_WIN else 'bb-gpu-capabilities')
    if not exe.is_file():
        return []
    pads = []
    for line in run_text([str(exe), '--gamepads'], timeout=10).splitlines():
        guid, _, name = line.partition('\t')
        if guid.strip() and name.strip():
            pads.append(dict(guid=guid.strip(), name=name.strip()))
    return pads


def pick_folder(title):
    if IS_WIN:
        script = ('Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; '
                  '$d.Description = "%s"; if ($d.ShowDialog() -eq "OK") { $d.SelectedPath }' % title.replace('"', ''))
        return run_text(['powershell', '-NoProfile', '-NonInteractive', '-STA', '-Command', script], timeout=600).strip()
    for command in (['zenity', '--file-selection', '--directory', '--title', title], ['kdialog', '--getexistingdirectory', str(Path.home())]):
        if shutil.which(command[0]):
            return run_text(command, timeout=600).strip()
    return ''


def open_path(path):
    path = str(path)
    if IS_WIN:
        os.startfile(path)  # noqa: S606 - a folder of ours
    else:
        subprocess.Popen(['xdg-open', path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---- Steam shortcut ---------------------------------------------------------------------------------------
def add_to_steam(exe):
    sys.path.insert(0, str(ROOT / 'launcher'))
    import steam_shortcuts as ss
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
                root = Path(winreg.QueryValueEx(key, 'SteamPath')[0])
        except OSError:
            raise RuntimeError('Steam was not found for this user.')
        files = [u / 'config' / 'shortcuts.vdf' for u in sorted((root / 'userdata').glob('*'))
                 if u.name.isdigit() and u.name != '0' and (u / 'config').is_dir()]
        running = 'steam.exe' in run_text(['tasklist', '/FI', 'IMAGENAME eq steam.exe', '/NH']).lower()
    else:
        files, running = ss.shortcut_files(), ss.steam_running()
    if not files:
        raise RuntimeError('No Steam user was found. Start Steam once, then try again.')
    if running:
        raise RuntimeError('Close Steam first: it rewrites its shortcut list when it exits and would drop the new entry.')
    entry = ss.make_entry(str(exe), str(Path(exe).parent), name='EA SPORTS UFC 3', launch_options='--play')
    return sum(1 for f in files if ss.add_shortcut(f, entry)), len(files)


# ---- the game session ----------------------------------------------------------------------------------------
CRASH_CODES = {3, 23, 142}  # UFC3.exe: a fault, a GPU library assertion, the watchdog


def is_crash(code):
    return code in CRASH_CODES or code < 0 or code >= 0xC0000000


def engine_exe():
    override = os.environ.get('UFC3_LAUNCH_COMMAND')
    if override:
        return override
    for name in ('UFC3.exe', 'SacredFire.exe', 'ufc3.sh'):
        if (ROOT / name).is_file():
            return str(ROOT / name)
    return ''


class Session:
    def __init__(self, data):
        self.data = Path(data)
        self.lock = threading.Lock()
        self.chunks, self.length = [], 0
        self.proc = None
        self.thread = None
        self.started = 0.0
        self.exit_code = None
        self.user_stop = False
        self.dlss_active = False
        self.cancel = threading.Event()
        self.guard_notice = ''

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def add(self, text):
        with self.lock:
            self.chunks.append(text)
            self.length += len(text)
            while self.length > 2_000_000 and len(self.chunks) > 1:
                self.length -= len(self.chunks.pop(0))

    def read(self, offset):
        with self.lock:
            text = ''.join(self.chunks)
            base = self.length - len(text)
        return text[max(0, offset - base):], self.length

    def start(self, settings, status):
        if self.running:
            raise RuntimeError('The game is already running.')
        folder = Path(settings['game']).expanduser()
        if not settings['game'] or not (folder / 'eboot.bin').is_file():
            raise ValueError('Choose the UFC 3 game folder (the one containing eboot.bin).')
        if settings['online_mode'] == 'join' and not settings['join_address'] and 'BB_ONLINE_SERVER' not in os.environ:
            raise ValueError("Join server: enter the host's IP address.")
        if settings['online_mode'] == 'host' and not re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', settings['host_address']):
            raise ValueError("Host server: pick this PC's LAN or VPN address.")
        exe = engine_exe()
        if not exe:
            raise ValueError('UFC3.exe was not found next to the launcher: the package is incomplete.')
        setv, drop, notes = game_env(settings, status)
        env = dict(os.environ)
        env.update(setv)
        for key in drop:
            env.pop(key, None)
        save_settings(self.data, settings)
        with self.lock:
            self.chunks, self.length = [], 0
        self.exit_code, self.user_stop, self.cancel = None, False, threading.Event()
        self.dlss_active = effective_upscaler(settings, status) == 'dlss4'
        for note in notes:
            self.add('[launcher] %s\n' % note)
        if self.dlss_active:  # cleared on a normal end; still set next start = the PC or driver died under DLSS
            modern = read_json(self.data / 'launcher-modern.json')
            modern['dlss_session_pending'] = True
            write_json(self.data / 'launcher-modern.json', modern)
        self.started = time.time()
        self.thread = threading.Thread(target=self._run, args=(exe, env, settings), daemon=True)
        self.thread.start()

    def _command(self, exe, settings):
        if os.environ.get('UFC3_LAUNCH_COMMAND'):
            return exe.split()
        if IS_WIN or exe.endswith('.exe'):
            return [exe, '--play']
        return None  # Linux: run_session below

    def _log_dir(self, settings):
        return Path(os.environ.get('BB_LOG_DIR') or settings['log_dir'] or self.data / 'logs')

    def _run(self, exe, env, settings):
        logs = self._log_dir(settings)
        logs.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in logs.glob('*.log')}
        tail_stop = threading.Event()
        tailer = threading.Thread(target=self._tail, args=(logs, before, tail_stop), daemon=True)
        tailer.start()
        code = 1
        try:
            command = self._command(exe, settings)
            if command:
                self.proc = subprocess.Popen(command, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
                                             start_new_session=not IS_WIN)
                for raw in iter(self.proc.stdout.readline, b''):
                    self.add(raw.decode('utf-8', 'replace'))
                    if self.cancel.is_set():
                        break
                code = self.proc.wait()
            else:
                code = self._run_linux(env, settings)
        except Exception as error:  # noqa: BLE001 - shown in the log, the launcher must stay alive
            self.add('[launcher] %s\n' % error)
            code = 2
        finally:
            time.sleep(0.4)
            tail_stop.set()
            tailer.join(2)
        self.exit_code = code
        self._finish(code, time.time() - self.started)

    def _run_linux(self, env, settings):
        sys.path.insert(0, str(ROOT / 'launcher'))
        import ufc3_online as online
        mapped = dict(online.DEFAULTS, game=settings['game'], player=settings['player'], language=settings['language'],
                      mode=settings['online_mode'], join_address=settings['join_address'],
                      host_address=settings['host_address'], fps=settings['fps_limit'], fullscreen=settings['fullscreen'],
                      neo=settings['neo'], skip_movies=settings['skip_movies'],
                      present=PRESENT_IDS[settings['present_mode']],
                      resolution=['1280x720', '1920x1080', '2560x1440', '3840x2160'][settings['resolution']])
        extra = dict(BB_FSR1='1' if settings['upscaler'] == 'fsr1' else '0',
                     BB_RCAS='1' if settings['sharpness'] else '0',
                     BB_RCAS_ATTENUATION=SHARPNESS_ATTENUATION[settings['sharpness']])
        size = [None, '1920x1080', '2560x1440', '3840x2160'][settings['display']]
        if size:
            extra['BB_WINDOW_SIZE'] = size
        saved = dict(os.environ)
        os.environ.update(env)
        os.environ.update({k: v for k, v in extra.items() if k not in env})
        try:
            online.run_session(ROOT, self.data, mapped, self.cancel,
                               lambda text, error=False: self.add('[launcher] %s\n' % text))
            return 0
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def _tail(self, logs, before, stop):
        path, position = None, 0
        while True:
            last = stop.is_set()
            if path is None:
                fresh = [p for p in logs.glob('*.log') if p.name not in before and p.name != 'launcher.log'
                         and not p.name.startswith(('server-', 'host-check'))]
                if fresh:
                    path = max(fresh, key=lambda p: p.stat().st_mtime)
            if path is not None:
                try:
                    with path.open('rb') as handle:
                        handle.seek(position)
                        chunk = handle.read()
                        position += len(chunk)
                    if chunk:
                        self.add(chunk.decode('utf-8', 'replace'))
                except OSError:
                    pass
            if last:
                return
            stop.wait(0.4)

    def stop(self):
        self.user_stop = True
        self.cancel.set()
        proc = self.proc
        if proc and proc.poll() is None:
            if IS_WIN:
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, creationflags=NO_WINDOW)
            else:
                try:
                    os.killpg(proc.pid, 15)
                except OSError:
                    proc.terminate()

    def _finish(self, code, seconds):
        self.add('\n[launcher] Game exited with code %d.\n' % code)
        modern = read_json(self.data / 'launcher-modern.json')
        if self.dlss_active:
            modern.pop('dlss_session_pending', None)
            if not self.user_stop and is_crash(code):
                modern['dlss_guard'] = dict(disabled=True, code=code, seconds=int(seconds), when=time.strftime('%Y-%m-%d %H:%M:%S'))
                self.guard_notice = ('DLSS 4 was switched off after this session ended with code %d. '
                                     'The log of the run has the details; choose DLSS again to retry.' % code)
                self.add('[launcher] %s\n' % self.guard_notice)
        write_json(self.data / 'launcher-modern.json', modern)


def load_guard(data):
    """The crash guard: set by a crashed DLSS session, or by one that never reported its end."""
    modern = read_json(Path(data) / 'launcher-modern.json')
    guard = dict(modern.get('dlss_guard') or {})
    if modern.get('dlss_session_pending'):
        guard = dict(disabled=True, code=-1, when=time.strftime('%Y-%m-%d %H:%M:%S'), unclean=True)
        modern.pop('dlss_session_pending', None)
        modern['dlss_guard'] = guard
        write_json(Path(data) / 'launcher-modern.json', modern)
    return guard


def clear_guard(data):
    modern = read_json(Path(data) / 'launcher-modern.json')
    modern.pop('dlss_guard', None)
    modern.pop('dlss_session_pending', None)
    write_json(Path(data) / 'launcher-modern.json', modern)


# ---- the HTTP API -----------------------------------------------------------------------------------------------
class App:
    def __init__(self, data):
        self.data = Path(data)
        self.session = Session(self.data)
        self.token = secrets.token_urlsafe(16)
        self.gpus = detect_gpus()
        self.last_ping = time.time()
        self.guard = load_guard(self.data)

    def version(self):
        for name in ('VERSION', 'out/VERSION'):
            try:
                return (ROOT / name).read_text().strip()
            except OSError:
                continue
        return 'dev'

    def status(self):
        self.guard = read_json(self.data / 'launcher-modern.json').get('dlss_guard') or {}
        return dlss_status(self.gpus, self.guard)

    def state(self):
        settings = load_settings(self.data)
        status = self.status()
        game = Path(settings['game'])
        return dict(
            settings=settings, version=self.version(), platform='windows' if IS_WIN else 'linux',
            options=dict(resolutions=RESOLUTIONS, displays=DISPLAYS, fps=FPS_LIMITS, present=PRESENT_MODES,
                         sharpness=SHARPNESS, languages=LANGUAGES, dlss_modes=DLSS_MODES,
                         pad_names=CTL_PAD_NAMES, pad_labels=CTL_PAD_LABELS),
            gpus=self.gpus, dlss=status, upscalers=upscaler_options(status),
            effective_upscaler=effective_upscaler(settings, status),
            game_ok=bool(settings['game']) and (game / 'eboot.bin').is_file(),
            art=any((game / 'sce_sys' / n).is_file() for n in ('pic1.png', 'icon0.png')) if settings['game'] else False,
            running=self.session.running, exit_code=self.session.exit_code, guard_notice=self.session.guard_notice,
            guard=self.guard, env_overrides=[k for k in ('BB_ONLINE_SERVER', 'BB_UPSCALER', 'BB_FPS_LIMIT') if k in os.environ],
            data_dir=str(self.data), exe_found=bool(engine_exe()))

    def api(self, method, route, body, query):
        if route == 'state':
            return self.state()
        if route == 'ping':
            self.last_ping = time.time()
            return dict(running=self.session.running)
        if route == 'settings' and method == 'POST':
            current = load_settings(self.data)
            updated = normalize(body, current)
            status = self.status()
            if body.get('upscaler') == 'dlss4' and not status['available']:
                raise ValueError(status['reason'])
            save_settings(self.data, updated)
            return self.state()
        if route == 'play' and method == 'POST':
            settings = load_settings(self.data)
            self.session.guard_notice = ''
            self.session.start(settings, self.status())
            return dict(ok=True)
        if route == 'stop' and method == 'POST':
            self.session.stop()
            return dict(ok=True)
        if route == 'log':
            text, offset = self.session.read(int(query.get('offset', ['0'])[0] or 0))
            return dict(text=text, offset=offset, running=self.session.running, exit_code=self.session.exit_code,
                        notice=self.session.guard_notice)
        if route == 'controls':
            if method == 'POST':
                save_controls(self.data, body.get('rows', []))
            return dict(rows=load_controls(self.data), defaults=[dict(id=n, key=CTL_DEFAULT_KEYS[i], pad=CTL_DEFAULT_PADS[i] if i < CTL_PAD_ROWS else None)
                                                                  for i, n in enumerate(CTL_INPUTS)])
        if route == 'gamepads':
            return dict(pads=list_gamepads())
        if route == 'addresses':
            return dict(addresses=local_addresses())
        if route == 'pick-folder' and method == 'POST':
            title = 'The game folder (CUSA06534; CUSA06534-patch beside it is merged automatically)' if body.get('purpose') == 'game' else 'The log folder'
            return dict(path=pick_folder(title))
        if route == 'open' and method == 'POST':
            settings = load_settings(self.data)
            target = Path(os.environ.get('BB_LOG_DIR') or settings['log_dir'] or self.data / 'logs') if body.get('what') == 'logs' else self.data
            target.mkdir(parents=True, exist_ok=True)
            open_path(target)
            return dict(ok=True)
        if route == 'steam' and method == 'POST':
            exe = engine_exe()
            if not exe or not Path(exe).is_file():
                raise ValueError('UFC3.exe was not found next to the launcher.')
            added, users = add_to_steam(exe)
            return dict(message='Added to Steam (%d added, %d updated). Start Steam to see it.' % (added, users - added))
        if route == 'dlss/reset' and method == 'POST':
            clear_guard(self.data)
            self.session.guard_notice = ''
            return self.state()
        raise KeyError(route)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'UFC3Launcher'

        def log_message(self, *args):
            pass

        def _send(self, code, payload, kind='application/json'):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            port = self.server.server_address[1]
            if self.headers.get('Host', '') not in ('127.0.0.1:%d' % port, 'localhost:%d' % port):  # DNS rebinding
                return self._send(403, dict(error='bad host'))
            url = urlparse(self.path)
            prefix = '/' + app.token
            if not (url.path == prefix or url.path.startswith(prefix + '/')):
                return self._send(404, dict(error='not found'))
            route = url.path[len(prefix):].strip('/')
            try:
                if route == '':
                    return self._send(200, UI_FILE.read_bytes(), 'text/html; charset=utf-8')
                if route == 'art':
                    settings = load_settings(app.data)
                    for name in ('pic1.png', 'icon0.png'):
                        path = Path(settings['game']) / 'sce_sys' / name
                        if settings['game'] and path.is_file():
                            return self._send(200, path.read_bytes(), 'image/png')
                    return self._send(404, dict(error='no art'))
                if not route.startswith('api/'):
                    return self._send(404, dict(error='not found'))
                route = route[4:]
                body = {}
                if method == 'POST':
                    length = min(int(self.headers.get('Content-Length') or 0), 1_000_000)
                    if length:
                        body = json.loads(self.rfile.read(length).decode('utf-8'))
                        if not isinstance(body, dict):
                            raise ValueError('bad request')
                    if self.headers.get('X-UFC3') != '1':
                        return self._send(403, dict(error='missing header'))
                self._send(200, app.api(method, route, body, parse_qs(url.query)))
            except KeyError:
                self._send(404, dict(error='unknown route'))
            except (ValueError, RuntimeError) as error:
                self._send(400, dict(error=str(error)))
            except Exception as error:  # noqa: BLE001
                self._send(500, dict(error='%s: %s' % (type(error).__name__, error)))

        def do_GET(self):
            self._handle('GET')

        def do_POST(self):
            self._handle('POST')

    return Handler


def browser_command(url):
    candidates = []
    if IS_WIN:
        for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles'), os.environ.get('LOCALAPPDATA')):
            if base:
                candidates += [Path(base) / 'Microsoft/Edge/Application/msedge.exe', Path(base) / 'Google/Chrome/Application/chrome.exe']
    else:
        candidates = [Path(p) for p in (shutil.which(n) for n in ('microsoft-edge', 'google-chrome', 'chromium', 'chromium-browser')) if p]
    profile = Path(data_dir()) / 'launcher-ui-profile'
    for path in candidates:
        if path.is_file():
            return [str(path), '--app=' + url, '--window-size=1240,800', '--user-data-dir=' + str(profile),
                    '--no-first-run', '--disable-features=Translate']
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description='UFC 3 modern launcher')
    parser.add_argument('--no-window', action='store_true', help='serve only and print the URL')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args(argv)
    data = data_dir()
    data.mkdir(parents=True, exist_ok=True)
    lock = data / 'launcher-modern.lock'
    previous = read_json(lock)
    if previous.get('url'):  # a launcher is already open: show it instead of starting a second one
        try:
            with socket.create_connection(('127.0.0.1', previous['port']), timeout=0.5):
                pass
            if not args.no_window and (cmd := browser_command(previous['url'])):
                subprocess.Popen(cmd)
                return 0
        except OSError:
            pass
    app = App(data)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app))
    port = server.server_address[1]
    url = 'http://127.0.0.1:%d/%s/' % (port, app.token)
    write_json(lock, dict(port=port, url=url, pid=os.getpid()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(url, flush=True)
    if not args.no_window:
        command = browser_command(url)
        if command:
            subprocess.Popen(command)
        else:
            import webbrowser
            webbrowser.open(url)
    try:  # the page pings every 2 s; without it (window closed) and without a running game the launcher ends
        while True:
            time.sleep(1)
            if time.time() - app.last_ping > 20 and not app.session.running and not args.no_window:
                break
    except KeyboardInterrupt:
        pass
    server.shutdown()
    try:
        lock.unlink()
    except OSError:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
