# plugins/screenvision/daemon.py
# Lifecycle hooks — auto-start monitoring when the plugin loads if configured.

import logging

logger = logging.getLogger(__name__)


def start(plugin_loader, settings):
    """Called by Sapphire when the plugin is loaded/enabled."""
    auto_start = settings.get('auto_start', False)
    if str(auto_start).lower() in ('true', '1', 'yes'):
        try:
            from plugins.screenvision.tools.screenvision_tools import (
                _start_monitoring, _clamp_interval, _clamp_quality, _state
            )
            monitor_idx = int(settings.get('monitor', 1) or 1)
            interval = _clamp_interval(settings.get('interval', 5))
            quality = _clamp_quality(settings.get('quality', 70))
            msg, ok = _start_monitoring(monitor_idx, interval, quality)
            logger.info(f"[screenvision] daemon start: {msg}")
        except Exception as e:
            logger.error(f"[screenvision] daemon start failed: {e}", exc_info=True)


def stop():
    """Called by Sapphire when the plugin is disabled/unloaded."""
    try:
        from plugins.screenvision.tools.screenvision_tools import _stop_monitoring, _state
        state = _state()
        if state.running:
            msg, _ = _stop_monitoring()
            logger.info(f"[screenvision] daemon stop: {msg}")
    except Exception as e:
        logger.error(f"[screenvision] daemon stop failed: {e}", exc_info=True)


import atexit as _atexit

def _atexit_stop():
    try:
        import sys as _sys
        s = _sys.modules.get('screenvision_state')
        if s is None:
            return
        if getattr(s, 'running', False):
            se = getattr(s, 'stop_event', None)
            if se:
                se.set()
            t = getattr(s, 'thread', None)
            if t and t.is_alive():
                t.join(timeout=20)
            s.running = False
        if getattr(s, 'game_running', False):
            gse = getattr(s, 'game_stop_event', None)
            if gse:
                gse.set()
            gt = getattr(s, 'game_thread', None)
            if gt and gt.is_alive():
                gt.join(timeout=35)
            s.game_running = False
    except Exception:
        pass

_atexit.register(_atexit_stop)
