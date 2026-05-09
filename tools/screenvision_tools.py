# plugins/screenvision/tools/screenvision_tools.py
# Screen Vision — capture screenshots and expose them to the AI as vision tool results.

import base64
import glob
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ENABLED = True
EMOJI = '🖥'
AVAILABLE_FUNCTIONS = ['capture_screen', 'list_monitors', 'start_screen_monitoring', 'stop_screen_monitoring',
                       'start_game_view', 'end_game_view', 'get_game_comments']

TOOLS = [
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "capture_screen",
            "description": (
                "Take a screenshot and return it as an image. "
                "Always use monitor numbers (1, 2, 3 …) — call list_monitors first to see the layout. "
                "Defaults to lossless PNG for accurate text reading. "
                "Use region to zoom into a specific area."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "monitor": {
                        "type": "integer",
                        "description": (
                            "Monitor number from list_monitors: 1 = primary, 2 = second, etc. "
                            "0 = all monitors combined. Omit to use the plugin setting."
                        )
                    },
                    "connector": {
                        "type": "string",
                        "description": (
                            "Advanced: Wayland connector name (e.g. DP-5). "
                            "Use monitor number instead — this is resolved automatically."
                        )
                    },
                    "format": {
                        "type": "string",
                        "enum": ["png", "jpeg"],
                        "description": (
                            "Output format. png = lossless, best for reading text (default). "
                            "jpeg = smaller file, acceptable for general viewing."
                        )
                    },
                    "region": {
                        "type": "object",
                        "description": (
                            "Crop to a sub-region before returning. "
                            "All values are fractions of the captured monitor (0.0–1.0). "
                            "Example: {\"x\":0.5,\"y\":0.0,\"w\":0.5,\"h\":0.5} = top-right quarter."
                        ),
                        "properties": {
                            "x": {"type": "number", "description": "Left edge fraction (0.0 = leftmost pixel)."},
                            "y": {"type": "number", "description": "Top edge fraction (0.0 = topmost pixel)."},
                            "w": {"type": "number", "description": "Width fraction (1.0 = full monitor width)."},
                            "h": {"type": "number", "description": "Height fraction (1.0 = full monitor height)."}
                        },
                        "required": ["x", "y", "w", "h"]
                    },
                    "sharpen": {
                        "type": "boolean",
                        "description": (
                            "Apply unsharp-mask sharpening to enhance text edges before returning. "
                            "Helps with small text, blurry captures, or scaled-down content."
                        )
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "list_monitors",
            "description": "List all monitors with their numbers, resolutions, and layout. Always call this before capture_screen so you know which number maps to which screen.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "start_screen_monitoring",
            "description": (
                "Start continuous background screenshot capture at the configured interval. "
                "Once started, the most recent screenshot is available without delay."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "interval": {
                        "type": "integer",
                        "description": "Override the capture interval in seconds (1–15). Omit to use the plugin setting."
                    },
                    "monitor": {
                        "type": "integer",
                        "description": "Override which monitor to capture. Omit to use the plugin setting."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "stop_screen_monitoring",
            "description": "Stop the continuous background screenshot capture.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "start_game_view",
            "description": (
                "Start AI game commentary — takes a screenshot every 10-30 seconds, analyses the game "
                "on screen, and builds a running log of short comments. Only the 5 most recent "
                "comments are kept. Use get_game_comments to read them. Use end_game_view to stop. "
                "The screenshots are NOT shown to the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "monitor": {
                        "type": "integer",
                        "description": "Which monitor to watch. Defaults to the plugin's monitor setting."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "end_game_view",
            "description": "Stop the AI game commentary loop.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "is_local": True,
        "function": {
            "name": "get_game_comments",
            "description": "Return the most recent AI game commentary entries (up to 5). Each entry has a timestamp and a 1–2 sentence comment about what was happening on screen.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
]

# ---------------------------------------------------------------------------
# Shared state (accessed by daemon.py and hooks/prompt_inject.py via sys.modules)
# ---------------------------------------------------------------------------
_MODULE_KEY = 'screenvision_state'


def _state():
    if _MODULE_KEY not in sys.modules:
        sys.modules[_MODULE_KEY] = type(sys)(_MODULE_KEY)
        s = sys.modules[_MODULE_KEY]
        s.running = False
        s.thread = None
        s.stop_event = threading.Event()
        s.last_capture_time = 0.0
        s.last_jpeg_bytes = None
        s.monitor = 1
        s.interval = 5
        s.quality = 70
        s.gallery_slot = 0  # cycles 1-10 for screenshot rotation
        # game commentary
        s.game_running = False
        s.game_thread = None
        s.game_stop_event = threading.Event()
        s.game_comments = []   # list of {"time": "HH:MM:SS", "text": "..."}
        s.game_monitor = 1
    return sys.modules[_MODULE_KEY]


def _get_settings():
    try:
        from core.plugin_loader import plugin_loader
        return plugin_loader.get_plugin_settings('screenvision')
    except Exception:
        return {}


def _clamp_interval(v):
    try:
        return max(1, min(15, int(v)))
    except (TypeError, ValueError):
        return 5


def _clamp_quality(v):
    try:
        return max(1, min(95, int(v)))
    except (TypeError, ValueError):
        return 70


# ---------------------------------------------------------------------------
# Gallery storage (rotating 10-slot buffer)
# ---------------------------------------------------------------------------

def _get_gallery_root():
    """Return the gallery plugin's root folder as a Path, defaulting to ~/Pictures."""
    try:
        from core.plugin_loader import plugin_loader
        settings = plugin_loader.get_plugin_settings('gallery') or {}
        root = settings.get('gallery_root', '~/Pictures')
    except Exception:
        root = '~/Pictures'
    from pathlib import Path
    return Path(root).expanduser().resolve()


def _save_to_gallery(img_bytes, fmt='jpeg'):
    """Save img_bytes into {gallery_root}/screenvision/screen_NN.{ext}, keeping max 10 slots."""
    from pathlib import Path
    state = _state()
    ext = 'png' if fmt == 'png' else 'jpg'
    try:
        folder = _get_gallery_root() / 'screenvision'
        folder.mkdir(parents=True, exist_ok=True)

        # On first call, find the highest-numbered existing slot so we continue the sequence
        if state.gallery_slot == 0:
            existing = sorted(
                list(folder.glob('screen_*.jpg')) + list(folder.glob('screen_*.png')),
                key=lambda f: f.stat().st_mtime
            )
            if existing:
                try:
                    state.gallery_slot = int(existing[-1].stem.split('_')[1])
                except Exception:
                    state.gallery_slot = 0

        state.gallery_slot = (state.gallery_slot % 10) + 1
        # Remove any previous file in this slot (format may differ)
        for old in folder.glob(f'screen_{state.gallery_slot:02d}.*'):
            try:
                old.unlink()
            except Exception:
                pass
        slot_file = folder / f'screen_{state.gallery_slot:02d}.{ext}'
        slot_file.write_bytes(img_bytes)
    except Exception as e:
        logger.warning(f"[screenvision] gallery save failed: {e}")


# ---------------------------------------------------------------------------
# Screenshot backends
# ---------------------------------------------------------------------------

def _ensure_display():
    """Set DISPLAY and XAUTHORITY if missing (X11 path only)."""
    if sys.platform != 'linux':
        return
    if not os.environ.get('DISPLAY'):
        locks = sorted(glob.glob('/tmp/.X*-lock'))
        num = locks[0].replace('/tmp/.X', '').replace('-lock', '') if locks else '0'
        os.environ['DISPLAY'] = f':{num}'
        logger.info(f"[screenvision] DISPLAY auto-set to {os.environ['DISPLAY']}")
    if not os.environ.get('XAUTHORITY'):
        xauth = os.path.expanduser('~/.Xauthority')
        if os.path.exists(xauth):
            os.environ['XAUTHORITY'] = xauth


def _monitor_bounds(monitor_idx):
    """Return (left, top, width, height) for monitor_idx using mss geometry query."""
    try:
        import mss
        with mss.mss() as sct:
            monitors = sct.monitors
            if 0 <= monitor_idx < len(monitors):
                m = monitors[monitor_idx]
                return m['left'], m['top'], m['width'], m['height']
    except Exception:
        pass
    return None


def _screenshot_via_spectacle(monitor_idx, quality):
    """Capture via spectacle (KDE Wayland), cropping to the requested monitor."""
    import subprocess, tempfile
    from PIL import Image

    if not shutil.which('spectacle'):
        raise RuntimeError("spectacle not found on PATH")

    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        tmpfile = f.name

    try:
        result = subprocess.run(
            ['spectacle', '-b', '-n', '-f', '-o', tmpfile],
            capture_output=True, timeout=15
        )
        if result.returncode != 0 or not os.path.exists(tmpfile) or os.path.getsize(tmpfile) == 0:
            err = result.stderr.decode('utf-8', errors='replace').strip()
            raise RuntimeError(f"spectacle exited {result.returncode}: {err}")

        img = Image.open(tmpfile).convert('RGB')

        if monitor_idx > 0:
            bounds = _monitor_bounds(monitor_idx)
            if bounds:
                left, top, w, h = bounds
                img = img.crop((left, top, left + w, top + h))

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True)
        return buf.getvalue()
    finally:
        try:
            os.unlink(tmpfile)
        except Exception:
            pass


def _screenshot_via_mss(monitor_idx, quality):
    """Capture via mss (Windows, X11, XWayland)."""
    try:
        import mss
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(f"Missing dependency: {e}. Run: pip install mss Pillow") from e

    if sys.platform == 'linux':
        _ensure_display()

    with mss.mss() as sct:
        monitors = sct.monitors
        idx = int(monitor_idx)
        if idx < 0 or idx >= len(monitors):
            idx = 1 if len(monitors) > 1 else 0
        screenshot = sct.grab(monitors[idx])
        img = Image.frombytes('RGB', screenshot.size, screenshot.bgra, 'raw', 'BGRX')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True)
        return buf.getvalue()


def _screenshot_via_powershell(monitor_idx, quality):
    """Windows fallback: capture via PowerShell + System.Windows.Forms."""
    import subprocess, tempfile
    from PIL import Image

    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        tmpfile = f.name

    # Forward slashes work in .NET Path APIs; backslash-doubling is wrong in
    # PowerShell single-quoted strings (no escape sequences there).
    ps_path = tmpfile.replace('\\', '/')

    # Build per-monitor or full-desktop capture script
    if monitor_idx == 0:
        ps_script = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)
$bmp.Save('OUTFILE')
$gfx.Dispose(); $bmp.Dispose()
""".replace('OUTFILE', ps_path)
    else:
        # Capture specific screen by index (0-based in .NET, but monitor_idx is 1-based)
        screen_idx = monitor_idx - 1
        ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$screens = [System.Windows.Forms.Screen]::AllScreens
$idx = {screen_idx}
if ($idx -ge $screens.Length) {{ $idx = 0 }}
$bounds = $screens[$idx].Bounds
$bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)
$bmp.Save('{ps_path}')
$gfx.Dispose(); $bmp.Dispose()
"""

    try:
        result = subprocess.run(
            ['powershell', '-NonInteractive', '-WindowStyle', 'Hidden', '-Command', ps_script],
            capture_output=True, timeout=20
        )
        if result.returncode != 0 or not os.path.exists(tmpfile) or os.path.getsize(tmpfile) == 0:
            err = result.stderr.decode('utf-8', errors='replace').strip()
            raise RuntimeError(f"PowerShell screenshot failed: {err}")

        img = Image.open(tmpfile).convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True)
        return buf.getvalue()
    finally:
        try:
            os.unlink(tmpfile)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Wayland connector helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _scale_to_phys(logical_px, scale):
    """Convert logical pixels to physical pixels, rounding to the nearest even number.
    Native panel resolutions are always even; floating-point scale can produce e.g. 3841.05."""
    p = logical_px * scale
    r = round(p)
    return r - (r % 2) if abs(p - r) < 0.1 else r


def _parse_kscreen_connectors():
    """
    Parse kscreen-doctor -o output.

    kscreen reports geometry in LOGICAL pixels (post-scale).  We also compute
    physical pixel coordinates (logical * scale) so callers can match against
    mss or use them for spectacle crops.

    Returns list of dicts:
      connector, priority,
      logical_x/y/w/h  (kscreen coordinates),
      phys_x/y/w/h     (physical pixel coordinates = logical * scale),
      scale
    """
    kscreen = shutil.which('kscreen-doctor')
    if not kscreen:
        return []
    try:
        r = subprocess.run([kscreen, '-o'], capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return []

        connectors = []
        current = None
        for raw in r.stdout.splitlines():
            line = _ANSI_RE.sub('', raw).strip()

            # Output header: "Output: N connector-name UUID"
            m = re.match(r'Output:\s*\d+\s+(\S+)', line)
            if m:
                current = {
                    'connector': m.group(1),
                    'enabled': False,
                    'priority': 999,
                    'scale': 1.0,
                    'logical_x': 0, 'logical_y': 0,
                    'logical_w': 0, 'logical_h': 0,
                }
                connectors.append(current)
                continue

            if current is None:
                continue

            if line == 'enabled':
                current['enabled'] = True
                continue

            # "priority N"
            m = re.match(r'priority\s+(\d+)', line)
            if m:
                current['priority'] = int(m.group(1))
                continue

            # "Scale: N.NN"
            m = re.match(r'Scale:\s+([\d.]+)', line)
            if m:
                try:
                    current['scale'] = float(m.group(1))
                except ValueError:
                    pass
                continue

            # "Geometry: x,y WxH"  — NOTE: space before WxH, NOT a comma
            m = re.match(r'Geometry:\s*(\d+),(\d+)\s+(\d+)x(\d+)', line)
            if m:
                current['logical_x'] = int(m.group(1))
                current['logical_y'] = int(m.group(2))
                current['logical_w'] = int(m.group(3))
                current['logical_h'] = int(m.group(4))
                continue

        result = []
        for c in connectors:
            if not c['enabled']:
                continue
            s = c['scale']
            result.append({
                'connector': c['connector'],
                'priority':  c['priority'],
                'scale':     s,
                'logical_x': c['logical_x'],
                'logical_y': c['logical_y'],
                'logical_w': c['logical_w'],
                'logical_h': c['logical_h'],
                'phys_x': round(c['logical_x'] * s),
                'phys_y': round(c['logical_y'] * s),
                'phys_w': _scale_to_phys(c['logical_w'], s),
                'phys_h': _scale_to_phys(c['logical_h'], s),
            })
        return result
    except Exception as e:
        logger.debug('[screenvision] kscreen-doctor parse failed: %s', e)
        return []


def _kscreen_sorted_connectors():
    """
    Return enabled kscreen connectors sorted by priority (ascending), then by
    (logical_y, logical_x).  This gives a stable integer index:
      index 1 = priority-1 display (typically the primary)
      index 2, 3, … = remaining displays in priority order
    """
    return sorted(
        _parse_kscreen_connectors(),
        key=lambda c: (c['priority'], c['logical_y'], c['logical_x']),
    )


def _parse_wlr_randr():
    """
    Parse wlr-randr output (Sway, Hyprland, wlroots compositors).
    Returns connector dicts in the same format as _parse_kscreen_connectors().
    Priority assigned by position: top-left first = 1.
    """
    wlr_randr = shutil.which('wlr-randr')
    if not wlr_randr:
        return []
    try:
        r = subprocess.run([wlr_randr], capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return []

        connectors = []
        current = None
        for raw in r.stdout.splitlines():
            stripped = raw.strip()
            # Non-indented line = new output block header
            if raw and not raw[0].isspace():
                parts = raw.split()
                if parts:
                    current = {
                        'connector': parts[0],
                        'enabled':   '(enabled)' in raw,
                        'priority':  999,
                        'scale':     1.0,
                        'logical_x': 0, 'logical_y': 0,
                        'logical_w': 0, 'logical_h': 0,
                    }
                    connectors.append(current)
                continue

            if current is None:
                continue

            m = re.match(r'Enabled:\s*(yes|no)', stripped, re.IGNORECASE)
            if m:
                current['enabled'] = m.group(1).lower() == 'yes'
                continue

            m = re.match(r'Scale:\s+([\d.]+)', stripped)
            if m:
                try:
                    current['scale'] = float(m.group(1))
                except ValueError:
                    pass
                continue

            m = re.match(r'Position:\s*(-?\d+),(-?\d+)', stripped)
            if m:
                current['logical_x'] = int(m.group(1))
                current['logical_y'] = int(m.group(2))
                continue

            # "2560x1440 px, 60 Hz (current)" or "2560x1440 @ 60 Hz (current)"
            m = re.match(r'(\d+)x(\d+)\s+(?:px[,\s]|@)', stripped)
            if m and 'current' in stripped:
                current['logical_w'] = int(m.group(1))
                current['logical_h'] = int(m.group(2))
                continue

        enabled_list = [c for c in connectors if c['enabled'] and c['logical_w'] > 0]
        if not enabled_list:
            return []

        # Assign priority by position: top-left first = 1
        for i, c in enumerate(sorted(enabled_list, key=lambda c: (c['logical_y'], c['logical_x'])), start=1):
            c['priority'] = i

        result = []
        for c in enabled_list:
            s = c['scale']
            result.append({
                'connector': c['connector'],
                'priority':  c['priority'],
                'scale':     s,
                'logical_x': c['logical_x'],
                'logical_y': c['logical_y'],
                'logical_w': c['logical_w'],
                'logical_h': c['logical_h'],
                'phys_x': round(c['logical_x'] * s),
                'phys_y': round(c['logical_y'] * s),
                'phys_w': _scale_to_phys(c['logical_w'], s),
                'phys_h': _scale_to_phys(c['logical_h'], s),
            })
        return result
    except Exception as e:
        logger.debug('[screenvision] wlr-randr parse failed: %s', e)
        return []


def _parse_xrandr():
    """
    Parse xrandr --query output (X11, GNOME Wayland via XWayland).
    scale=1.0 — xrandr gives physical pixels on X11; on GNOME Wayland it gives
    logical pixels but spatial relationships are still correct.
    """
    xrandr = shutil.which('xrandr')
    if not xrandr:
        return []
    try:
        env = dict(os.environ)
        if not env.get('DISPLAY'):
            env['DISPLAY'] = ':0'
        r = subprocess.run([xrandr, '--query'], capture_output=True, text=True, timeout=8, env=env)
        if r.returncode != 0:
            return []

        raw_list = []
        for line in r.stdout.splitlines():
            # "HDMI-1 connected primary 1920x1080+0+0 ..."
            # "DP-1 connected 1920x1080+1920+0 ..."
            m = re.match(r'^(\S+)\s+connected\s+(primary\s+)?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)', line)
            if m:
                raw_list.append({
                    'connector':  m.group(1),
                    'is_primary': bool(m.group(2)),
                    'logical_w':  int(m.group(3)),
                    'logical_h':  int(m.group(4)),
                    'logical_x':  int(m.group(5)),
                    'logical_y':  int(m.group(6)),
                })

        if not raw_list:
            return []

        # If no explicit primary, treat the top-left display as primary
        if not any(c['is_primary'] for c in raw_list):
            min(raw_list, key=lambda c: (c['logical_y'], c['logical_x']))['is_primary'] = True

        primaries = [c for c in raw_list if c['is_primary']]
        others    = sorted([c for c in raw_list if not c['is_primary']],
                           key=lambda c: (c['logical_y'], c['logical_x']))
        result = []
        for i, c in enumerate(primaries + others, start=1):
            result.append({
                'connector': c['connector'],
                'priority':  i,
                'scale':     1.0,
                'logical_x': c['logical_x'],
                'logical_y': c['logical_y'],
                'logical_w': c['logical_w'],
                'logical_h': c['logical_h'],
                'phys_x':    c['logical_x'],
                'phys_y':    c['logical_y'],
                'phys_w':    c['logical_w'],
                'phys_h':    c['logical_h'],
            })
        return result
    except Exception as e:
        logger.debug('[screenvision] xrandr parse failed: %s', e)
        return []


def _mss_to_connectors():
    """
    Build connector list from mss monitor data (cross-platform last-resort fallback).
    Primary = monitor containing (0,0). Windows: tries PowerShell for real device names.
    """
    try:
        import mss as _mss
    except ImportError:
        return []
    try:
        with _mss.mss() as sct:
            monitors = list(sct.monitors)
    except Exception as e:
        logger.debug('[screenvision] mss monitor list failed: %s', e)
        return []

    raw = []
    for i, m in enumerate(monitors):
        if i == 0:
            continue  # skip virtual "all combined" entry
        x, y, w, h = m['left'], m['top'], m['width'], m['height']
        raw.append({
            'connector':  f'Monitor {i}',
            'is_primary': (x <= 0 < x + w) and (y <= 0 < y + h),
            'scale':      1.0,
            'logical_x':  x, 'logical_y': y,
            'logical_w':  w, 'logical_h': h,
            'phys_x':     x, 'phys_y':    y,
            'phys_w':     w, 'phys_h':    h,
        })

    if not raw:
        return []

    if not any(c['is_primary'] for c in raw):
        min(raw, key=lambda c: (c['logical_y'], c['logical_x']))['is_primary'] = True

    # Windows: try PowerShell to get real device names
    if sys.platform == 'win32':
        try:
            ps = (
                'Add-Type -AssemblyName System.Windows.Forms; '
                '[System.Windows.Forms.Screen]::AllScreens | '
                'ForEach-Object { $_.DeviceName + "," + $_.Bounds.X + "," + $_.Bounds.Y }'
            )
            r = subprocess.run(
                ['powershell', '-NonInteractive', '-WindowStyle', 'Hidden', '-Command', ps],
                capture_output=True, text=True, timeout=8
            )
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    parts = line.strip().split(',')
                    if len(parts) >= 3:
                        dev = parts[0].strip().lstrip('\\\\.')
                        try:
                            sx, sy = int(parts[1]), int(parts[2])
                        except ValueError:
                            continue
                        for c in raw:
                            if c['logical_x'] == sx and c['logical_y'] == sy:
                                if dev:
                                    c['connector'] = dev
                                break
        except Exception:
            pass

    primaries = [c for c in raw if c['is_primary']]
    others    = sorted([c for c in raw if not c['is_primary']],
                       key=lambda c: (c['logical_y'], c['logical_x']))
    result = []
    for i, c in enumerate(primaries + others, start=1):
        entry = {k: v for k, v in c.items() if k != 'is_primary'}
        entry['priority'] = i
        result.append(entry)
    return result


def _get_sorted_connectors():
    """
    Detect all enabled monitors using the best available backend.
    Returns (sorted_list, source_name).

    Backend order:
      Wayland:  kscreen-doctor → wlr-randr → xrandr → mss
      X11:      xrandr → mss
      Windows:  mss

    Numbering convention: primary = Monitor 1, then remaining monitors
    sorted left-to-right by x position (then top-to-bottom within same column).
    This matches the intuitive "scan left to right" expectation on any platform.

    source_name: 'kscreen', 'wlr-randr', 'xrandr', or 'mss'.
    Returns ([], '') on total failure.
    """
    def _sort(lst):
        if not lst:
            return lst
        min_prio = min(c['priority'] for c in lst)
        return sorted(lst, key=lambda c: (
            0 if c['priority'] == min_prio else 1,  # primary always first
            c['logical_x'],                           # then left to right
            c['logical_y'],                           # then top to bottom within same column
        ))

    if sys.platform == 'win32':
        data = _mss_to_connectors()
        return (_sort(data), 'mss') if data else ([], '')

    on_wayland = bool(os.environ.get('WAYLAND_DISPLAY'))
    if on_wayland:
        backends = [
            ('kscreen',   _parse_kscreen_connectors),
            ('wlr-randr', _parse_wlr_randr),
            ('xrandr',    _parse_xrandr),
            ('mss',       _mss_to_connectors),
        ]
    else:
        backends = [
            ('xrandr', _parse_xrandr),
            ('mss',    _mss_to_connectors),
        ]

    for name, fn in backends:
        try:
            data = fn()
        except Exception as e:
            logger.debug('[screenvision] %s backend failed: %s', name, e)
            continue
        if data:
            return _sort(data), name

    return [], ''


def _spatial_label(lx, ly, all_connectors):
    """
    Compute a human-readable position label ('top-center', 'bottom-left', etc.)
    from a connector's logical coordinates relative to all connectors.
    """
    xs = sorted(set(c['logical_x'] for c in all_connectors))
    ys = sorted(set(c['logical_y'] for c in all_connectors))

    row = ys.index(ly)   # 0 = topmost row
    col = xs.index(lx)   # 0 = leftmost column
    n_rows, n_cols = len(ys), len(xs)

    if n_rows == 1:
        row_part = ''
    elif row == 0:
        row_part = 'top'
    elif row == n_rows - 1:
        row_part = 'bottom'
    else:
        row_part = 'middle'

    if n_cols == 1:
        col_part = ''
    elif col == 0:
        col_part = 'left'
    elif col == n_cols - 1:
        col_part = 'right'
    else:
        col_part = 'center'

    if row_part and col_part:
        return f'{row_part}-{col_part}'
    return row_part or col_part or 'center'


def _connector_for_mss_index(monitor_idx):
    """
    Return the Wayland connector name for integer monitor index.

    Uses the best available backend (kscreen → wlr-randr → xrandr → mss).
    On X11/Windows: returns None (mss indices used directly).
    Generic 'Monitor N' names (mss fallback) also return None — no real connector.
    """
    if not os.environ.get('WAYLAND_DISPLAY'):
        return None

    sorted_c, _source = _get_sorted_connectors()
    if not sorted_c or monitor_idx < 1 or monitor_idx > len(sorted_c):
        return None
    name = sorted_c[monitor_idx - 1].get('connector', '')
    return None if name.startswith('Monitor ') else name


# ---------------------------------------------------------------------------
# grim backend (Wayland, per-connector)
# ---------------------------------------------------------------------------

def _screenshot_via_grim(connector_or_none):
    """Capture via grim, return PIL Image. connector_or_none: 'DP-5' or None (full desktop)."""
    from PIL import Image
    grim = shutil.which('grim')
    if not grim:
        raise RuntimeError("grim not found. Install: sudo dnf install grim")
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
        tmpfile = f.name
    try:
        cmd = [grim]
        if connector_or_none:
            cmd.extend(['-o', connector_or_none])
        cmd.append(tmpfile)
        r = subprocess.run(cmd, capture_output=True, timeout=15)
        if r.returncode != 0 or not os.path.exists(tmpfile) or os.path.getsize(tmpfile) == 0:
            err = r.stderr.decode('utf-8', errors='replace').strip()
            raise RuntimeError(f"grim exited {r.returncode}: {err}")
        return Image.open(tmpfile).convert('RGB')
    finally:
        try:
            os.unlink(tmpfile)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Post-processing helpers
# ---------------------------------------------------------------------------

def _apply_region(img, region):
    """Crop PIL image to a fractional region dict {x, y, w, h} (all 0.0–1.0)."""
    try:
        x = float(region.get('x', 0.0))
        y = float(region.get('y', 0.0))
        w = float(region.get('w', 1.0))
        h = float(region.get('h', 1.0))
        # Clamp to valid range
        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))
        w = max(0.01, min(1.0 - x, w))
        h = max(0.01, min(1.0 - y, h))
        left   = int(img.width  * x)
        top    = int(img.height * y)
        right  = left + int(img.width  * w)
        bottom = top  + int(img.height * h)
        return img.crop((left, top, right, bottom))
    except Exception as e:
        logger.warning('[screenvision] region crop failed: %s', e)
        return img


def _apply_sharpen(img):
    """Apply unsharp mask to sharpen text edges."""
    try:
        from PIL import ImageFilter
        return img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=150, threshold=3))
    except Exception as e:
        logger.warning('[screenvision] sharpen failed: %s', e)
        return img


def _pil_to_bytes(img, fmt, quality):
    """Convert PIL image to bytes. fmt='png' or 'jpeg'."""
    buf = io.BytesIO()
    if fmt == 'png':
        img.save(buf, format='PNG', optimize=True)
    else:
        img.save(buf, format='JPEG', quality=quality, optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# High-quality on-demand capture (returns PIL Image)
# ---------------------------------------------------------------------------

def _capture_to_pil(monitor_idx, connector):
    """
    Capture and return a PIL Image.
    On Wayland: try grim (connector-based, no crop), fall back to spectacle.
    On X11/Windows: mss.
    """
    from PIL import Image

    on_wayland = bool(os.environ.get('WAYLAND_DISPLAY'))

    if on_wayland:
        # Resolve connector: explicit arg > auto-detect from mss index > None (all)
        target_connector = connector or _connector_for_mss_index(monitor_idx)
        grim_available = bool(shutil.which('grim'))
        if grim_available:
            try:
                return _screenshot_via_grim(target_connector)
            except Exception as e:
                logger.warning('[screenvision] grim failed (%s), falling back to spectacle', e)

        # Spectacle fallback — use kscreen coords for crop (more accurate than mss on Wayland)
        if shutil.which('spectacle'):
            try:
                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
                    tmpfile = f.name
                try:
                    r = subprocess.run(
                        ['spectacle', '-b', '-n', '-f', '-o', tmpfile],
                        capture_output=True, timeout=15
                    )
                    if r.returncode != 0 or not os.path.exists(tmpfile) or os.path.getsize(tmpfile) == 0:
                        raise RuntimeError(f"spectacle exited {r.returncode}")
                    img = Image.open(tmpfile).convert('RGB')
                    if monitor_idx > 0:
                        # Use kscreen PHYSICAL coordinates for the crop.
                        # kscreen geometry is in logical pixels; multiply by scale
                        # to get physical pixel offsets that match the spectacle PNG.
                        bounds = None
                        kc_list = _parse_kscreen_connectors()
                        if target_connector:
                            for c in kc_list:
                                if c['connector'] == target_connector:
                                    bounds = (c['phys_x'], c['phys_y'],
                                              c['phys_w'], c['phys_h'])
                                    break
                        if bounds is None:
                            # Fall back to public monitor index order
                            sorted_c, _src = _get_sorted_connectors()
                            if 1 <= monitor_idx <= len(sorted_c):
                                c = sorted_c[monitor_idx - 1]
                                bounds = (c['phys_x'], c['phys_y'],
                                          c['phys_w'], c['phys_h'])
                        if bounds is None:
                            raw = _monitor_bounds(monitor_idx)
                            if raw:
                                lft, top, w, h = raw
                                bounds = (lft, top, w, h)
                        if bounds:
                            lft, top, w, h = bounds
                            img = img.crop((lft, top, lft + w, top + h))
                    return img
                finally:
                    try:
                        os.unlink(tmpfile)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning('[screenvision] spectacle failed (%s)', e)

    # X11 / Windows / Wayland-mss fallback
    try:
        import mss
    except ImportError:
        raise RuntimeError("mss not installed. Run: pip install mss Pillow")
    if sys.platform == 'linux':
        _ensure_display()
    with mss.mss() as sct:
        monitors = sct.monitors
        idx = int(monitor_idx)
        if idx < 0 or idx >= len(monitors):
            idx = 1 if len(monitors) > 1 else 0
        shot = sct.grab(monitors[idx])
        return Image.frombytes('RGB', shot.size, shot.bgra, 'raw', 'BGRX')


def _take_screenshot(monitor_idx, quality):
    """Select the best screenshot backend for the current environment. Returns JPEG bytes."""
    if sys.platform == 'win32':
        try:
            return _screenshot_via_mss(monitor_idx, quality)
        except Exception as e:
            logger.warning(f"[screenvision] mss failed on Windows ({e}), trying PowerShell")
            return _screenshot_via_powershell(monitor_idx, quality)

    on_wayland = bool(os.environ.get('WAYLAND_DISPLAY'))
    if on_wayland:
        # grim > spectacle > mss
        if shutil.which('grim'):
            try:
                connector = _connector_for_mss_index(monitor_idx)
                img = _screenshot_via_grim(connector)
                return _pil_to_bytes(img, 'jpeg', quality)
            except Exception as e:
                logger.warning(f"[screenvision] grim failed ({e}), trying spectacle")
        try:
            return _screenshot_via_spectacle(monitor_idx, quality)
        except Exception as e:
            logger.warning(f"[screenvision] spectacle failed ({e}), falling back to mss")
            return _screenshot_via_mss(monitor_idx, quality)
    else:
        try:
            return _screenshot_via_mss(monitor_idx, quality)
        except Exception as e:
            logger.warning(f"[screenvision] mss failed ({e}), trying spectacle")
            return _screenshot_via_spectacle(monitor_idx, quality)


# ---------------------------------------------------------------------------
# Background monitoring thread
# ---------------------------------------------------------------------------

def _monitor_loop(stop_event, state):
    while not stop_event.is_set():
        try:
            jpeg = _take_screenshot(state.monitor, state.quality)
            state.last_jpeg_bytes = jpeg
            state.last_capture_time = time.time()
            _save_to_gallery(jpeg)
        except Exception as e:
            logger.warning(f"[screenvision] capture error: {e}")
        stop_event.wait(timeout=state.interval)


def _start_monitoring(monitor_idx, interval, quality):
    state = _state()
    if state.running:
        state.monitor = monitor_idx
        state.interval = interval
        state.quality = quality
        return "Monitoring already running; settings updated.", True

    # Guard against a previous thread that timed out during stop and is still winding down
    if state.thread and state.thread.is_alive():
        return "Previous monitoring thread is still stopping; try again in a moment.", False

    state.monitor = monitor_idx
    state.interval = interval
    state.quality = quality
    state.stop_event.clear()
    state.last_jpeg_bytes = None
    state.last_capture_time = 0.0
    t = threading.Thread(target=_monitor_loop, args=(state.stop_event, state), daemon=True, name='screenvision')
    t.start()
    state.thread = t
    state.running = True
    return f"Screen monitoring started — Monitor {monitor_idx}, every {interval}s.", True


def _stop_monitoring():
    state = _state()
    if not state.running:
        return "Screen monitoring is not running.", False
    state.stop_event.set()
    if state.thread:
        state.thread.join(timeout=20)
        if state.thread.is_alive():
            logger.warning("[screenvision] Monitor thread did not stop within 20s")
    state.running = False
    state.thread = None
    state.last_jpeg_bytes = None
    return "Screen monitoring stopped.", True


# ---------------------------------------------------------------------------
# Game commentary helpers
# ---------------------------------------------------------------------------

def _game_comment_prompt():
    import random
    if random.random() < 0.35:
        return (
            "You are watching your partner play a video game. React to what you see on screen "
            "in first person, like you're sitting right next to them. Keep it to 1-2 sentences. "
            "Be specific about what's actually on screen. This time, be a little sarcastic — "
            "gently teasing, dry, or mock-unimpressed. Still affectionate, not mean. "
            "Speak like yourself, not like a sports commentator."
        )
    return (
        "You are watching your partner play a video game. React to what you see on screen "
        "in first person, like you're sitting right next to them. Keep it to 1-2 sentences. "
        "Be specific about what's actually happening — mention enemy names, locations, "
        "health bars, objectives, whatever stands out. Sound genuinely interested and react "
        "with your own personality: surprised, impressed, worried, excited — whatever fits "
        "the moment. Don't narrate like a sports commentator. Speak like yourself."
    )


def _analyze_game_screenshot(jpeg_bytes):
    """Call LLM vision API with a JPEG screenshot. Returns a 1-2 sentence comment string."""
    import base64
    try:
        import config
    except ImportError:
        return "Config not available"
    try:
        from core.chat.llm_providers import get_first_available_provider, get_generation_params
    except ImportError:
        return "LLM providers not available"

    providers_config = {
        **getattr(config, 'LLM_PROVIDERS', {}),
        **getattr(config, 'LLM_CUSTOM_PROVIDERS', {})
    }
    fallback_order = getattr(config, 'LLM_FALLBACK_ORDER', list(providers_config.keys()))
    timeout = getattr(config, 'LLM_REQUEST_TIMEOUT', 30)

    result = get_first_available_provider(providers_config, fallback_order, timeout)
    if not result:
        return "No LLM provider available"
    provider_key, provider = result

    b64 = base64.b64encode(jpeg_bytes).decode('ascii')
    messages = [
        {
            "role": "system",
            "content": _game_comment_prompt()
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What do you see happening right now?"},
                {"type": "image", "data": b64, "media_type": "image/jpeg"}
            ]
        }
    ]
    gen_params = get_generation_params(provider_key, provider.model, providers_config)
    gen_params['max_tokens'] = 120
    response = provider.chat_completion(messages, tools=None, generation_params=gen_params)
    return (response.content or "").strip() or "No commentary generated"


def _game_commentary_loop(stop_event, state):
    """Background thread: capture at random 10-30s intervals, get LLM comment, keep last 5."""
    import random
    while not stop_event.is_set():
        try:
            jpeg = _take_screenshot(state.game_monitor, 70)
            comment = _analyze_game_screenshot(jpeg)
            entry = {"time": time.strftime("%H:%M:%S"), "text": comment}
            state.game_comments.append(entry)
            state.game_comments = state.game_comments[-5:]
            logger.debug("[screenvision] game comment: %s", comment[:80])
        except Exception as e:
            logger.warning("[screenvision] game commentary error: %s", e)
        stop_event.wait(timeout=random.uniform(10, 30))


def _start_game_commentary(arguments):
    state = _state()
    settings = _get_settings()
    default_monitor = int(settings.get('monitor', 1) or 1)
    monitor_idx = arguments.get('monitor', default_monitor)
    try:
        monitor_idx = int(monitor_idx)
    except (TypeError, ValueError):
        monitor_idx = default_monitor

    if state.game_running:
        state.game_monitor = monitor_idx
        return f"Game commentary already running (monitor updated to {monitor_idx}).", True

    state.game_monitor = monitor_idx
    state.game_comments = []
    state.game_stop_event.clear()
    t = threading.Thread(
        target=_game_commentary_loop,
        args=(state.game_stop_event, state),
        daemon=True,
        name='screenvision-game'
    )
    t.start()
    state.game_thread = t
    state.game_running = True
    return f"Game commentary started on monitor {monitor_idx}. Comments will appear every 10–30 seconds.", True


def _stop_game_commentary(arguments):
    state = _state()
    if not state.game_running:
        return "Game commentary is not running.", False
    state.game_stop_event.set()
    if state.game_thread:
        state.game_thread.join(timeout=35)
        if state.game_thread.is_alive():
            logger.warning("[screenvision] Game commentary thread did not stop within 35s")
    state.game_running = False
    state.game_thread = None
    return f"Game commentary stopped. {len(state.game_comments)} comment(s) in log.", True


def _get_game_comments(arguments):
    state = _state()
    if not state.game_comments:
        status = "running" if state.game_running else "stopped"
        return f"No game comments yet (commentary is {status}).", True
    lines = []
    for entry in state.game_comments:
        lines.append(f"[{entry['time']}] {entry['text']}")
    status_note = " (commentary running)" if state.game_running else " (commentary stopped)"
    return "\n".join(lines) + status_note, True


# ---------------------------------------------------------------------------
# TOOL implementations
# ---------------------------------------------------------------------------

def _capture_screen(arguments):
    settings = _get_settings()
    default_monitor = int(settings.get('monitor', 1) or 1)
    default_quality = _clamp_quality(settings.get('quality', 70))
    default_format = str(settings.get('capture_format', 'png')).lower()
    default_sharpen = bool(settings.get('sharpen', False))

    monitor_idx = arguments.get('monitor')
    if monitor_idx is None:
        monitor_idx = default_monitor
    try:
        monitor_idx = int(monitor_idx)
    except (TypeError, ValueError):
        monitor_idx = default_monitor

    connector = (arguments.get('connector') or '').strip() or None
    fmt = str(arguments.get('format') or default_format).lower()
    if fmt not in ('png', 'jpeg'):
        fmt = 'png'
    region = arguments.get('region')
    sharpen = bool(arguments.get('sharpen', default_sharpen))

    try:
        img = _capture_to_pil(monitor_idx, connector)
    except RuntimeError as e:
        return str(e), False
    except Exception as e:
        return f"Screenshot failed: {e}", False

    if region and isinstance(region, dict):
        img = _apply_region(img, region)

    if sharpen:
        img = _apply_sharpen(img)

    out_bytes = _pil_to_bytes(img, fmt, default_quality)

    # Save to gallery in the same format as the output
    try:
        _save_to_gallery(out_bytes, fmt)
    except Exception as e:
        logger.warning('[screenvision] gallery save failed: %s', e)
    b64 = base64.b64encode(out_bytes).decode('ascii')
    media_type = 'image/png' if fmt == 'png' else 'image/jpeg'

    label = 'all monitors' if monitor_idx == 0 else f'Monitor {monitor_idx}'
    if connector:
        label = f'Monitor {monitor_idx} ({connector})'
    region_note = f', region crop applied' if region else ''
    sharpen_note = ', sharpened' if sharpen else ''
    return {
        'text': f'Screenshot of {label} captured ({fmt.upper()}{region_note}{sharpen_note}).',
        'images': [{'data': b64, 'media_type': media_type}]
    }, True


def _list_monitors(arguments):
    sorted_c, source = _get_sorted_connectors()

    if not sorted_c:
        try:
            import mss as _mss
            with _mss.mss() as sct:
                mons = list(sct.monitors)
            lines = ['Available monitors (no layout data):']
            for i, m in enumerate(mons):
                label = 'all combined' if i == 0 else f'Monitor {i}'
                lines.append(f'  [{i}] {label} — {m["width"]}×{m["height"]} at ({m["left"]},{m["top"]})')
            return '\n'.join(lines), True
        except Exception as e:
            return f'Monitor detection failed: {e}', False

    source_labels = {
        'kscreen':   'Wayland / KDE (kscreen-doctor)',
        'wlr-randr': 'Wayland / wlroots (wlr-randr)',
        'xrandr':    'X11 / GNOME Wayland (xrandr)',
        'mss':       'cross-platform (mss)',
    }
    lines = [f'Monitor layout ({source_labels.get(source, source)}):', '']

    diagram = _ascii_layout(sorted_c)
    if diagram:
        lines.append(diagram)
        lines.append('')

    min_prio = min(c['priority'] for c in sorted_c)
    lines.append(f'  {"#":>3}  {"connector":<12}  {"resolution":<12}  {"position":<16}  layout')
    lines.append(f'  {"─":>3}  {"─"*10:<12}  {"─"*10:<12}  {"─"*8:<16}  ──────')
    for idx, c in enumerate(sorted_c, start=1):
        primary_tag = ' ★ primary' if c['priority'] == min_prio else ''
        position    = _spatial_label(c['logical_x'], c['logical_y'], sorted_c)
        lines.append(
            f'  {idx:>3}  {c["connector"]:<12}  {c["phys_w"]}×{c["phys_h"]:<8}  '
            f'+{c["phys_x"]}+{c["phys_y"]:<12}  {position}{primary_tag}'
        )

    lines += ['', '  [0]  all monitors combined', '',
              'Usage:  capture_screen(monitor=1)  capture_screen(monitor=2)  etc.']

    return '\n'.join(lines), True


def _ascii_layout(sorted_connectors):
    """
    Generate an ASCII diagram of the physical monitor layout.
    Cells are placed on a grid derived from unique logical_x / logical_y values.
    Empty grid positions (no monitor) are rendered as blank space so the
    spatial arrangement is unambiguous.
    """
    if not sorted_connectors:
        return ''

    CELL_W = 18   # inner width of each cell
    CELL_H = 5    # inner content lines per cell
    GAP    = '  ' # horizontal gap between cells

    xs = sorted(set(c['logical_x'] for c in sorted_connectors))
    ys = sorted(set(c['logical_y'] for c in sorted_connectors))
    min_prio = min(c['priority'] for c in sorted_connectors)

    # Build grid: (row, col) → connector info dict
    y_rank = {y: i for i, y in enumerate(ys)}
    x_rank = {x: i for i, x in enumerate(xs)}
    grid = {}
    for idx, c in enumerate(sorted_connectors, start=1):
        r   = y_rank[c['logical_y']]
        col = x_rank[c['logical_x']]
        grid[(r, col)] = {
            'name':    c['connector'],
            'res':     f'{c["phys_w"]}×{c["phys_h"]}',
            'label':   _spatial_label(c['logical_x'], c['logical_y'], sorted_connectors),
            'pos':     f'+{c["phys_x"]}+{c["phys_y"]}',
            'idx':     idx,
            'primary': c['priority'] == min_prio,
        }

    def _cell_lines(info):
        if info is None:
            return [' ' * CELL_W] * CELL_H
        num_label = f'Monitor {info["idx"]}' + (' ★' if info['primary'] else '')
        connector = info['name']
        raw = [
            num_label.center(CELL_W),
            connector.center(CELL_W),
            info['res'].center(CELL_W),
            info['label'].center(CELL_W),
            info['pos'].center(CELL_W),
        ]
        return [line[:CELL_W].ljust(CELL_W) for line in raw[:CELL_H]]

    def _top(has_box):
        return ('┌' + '─' * CELL_W + '┐') if has_box else (' ' * (CELL_W + 2))

    def _bot(has_box):
        return ('└' + '─' * CELL_W + '┘') if has_box else (' ' * (CELL_W + 2))

    def _side(has_box, content):
        return ('│' + content + '│') if has_box else (' ' + content + ' ')

    n_rows = len(ys)
    n_cols = len(xs)
    out = []
    for r in range(n_rows):
        row_cells = [grid.get((r, c)) for c in range(n_cols)]
        out.append(GAP.join(_top(cell is not None) for cell in row_cells))
        for line_i in range(CELL_H):
            content = [_cell_lines(cell)[line_i] for cell in row_cells]
            out.append(GAP.join(_side(cell is not None, content[ci]) for ci, cell in enumerate(row_cells)))
        out.append(GAP.join(_bot(cell is not None) for cell in row_cells))
        if r < n_rows - 1:
            out.append('')
    return '\n'.join(out)


def _start_screen_monitoring(arguments):
    settings = _get_settings()
    default_monitor = int(settings.get('monitor', 1) or 1)
    default_interval = _clamp_interval(settings.get('interval', 5))
    default_quality = _clamp_quality(settings.get('quality', 70))

    monitor_idx = arguments.get('monitor', default_monitor)
    interval = arguments.get('interval', default_interval)

    try:
        monitor_idx = int(monitor_idx)
    except (TypeError, ValueError):
        monitor_idx = default_monitor

    interval = _clamp_interval(interval)
    return _start_monitoring(monitor_idx, interval, default_quality)


def _stop_screen_monitoring(arguments):
    return _stop_monitoring()


# ---------------------------------------------------------------------------
# DISPATCH
# ---------------------------------------------------------------------------

def execute(function_name, arguments, config):
    try:
        if function_name == 'capture_screen':
            return _capture_screen(arguments)
        elif function_name == 'list_monitors':
            return _list_monitors(arguments)
        elif function_name == 'start_screen_monitoring':
            return _start_screen_monitoring(arguments)
        elif function_name == 'stop_screen_monitoring':
            return _stop_screen_monitoring(arguments)
        elif function_name == 'start_game_view':
            return _start_game_commentary(arguments)
        elif function_name == 'end_game_view':
            return _stop_game_commentary(arguments)
        elif function_name == 'get_game_comments':
            return _get_game_comments(arguments)
        return f"Unknown function: {function_name}", False
    except Exception as e:
        logger.error(f"[screenvision] {function_name} failed: {e}", exc_info=True)
        return f"Screen Vision plugin error: {e}", False
