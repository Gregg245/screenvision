# plugins/screenvision/hooks/prompt_inject.py
# Injects a brief status line into the system prompt when monitoring is active.

import time


def prompt_inject(event):
    try:
        settings = event.config or {}
        if not settings.get('inject_hint', True):
            return

        from plugins.screenvision.tools.screenvision_tools import _state
        state = _state()

        if not state.running:
            return

        ago = time.time() - state.last_capture_time if state.last_capture_time else None
        ago_str = f"{ago:.0f}s ago" if ago is not None else "not yet captured"

        label = "all monitors" if state.monitor == 0 else f"Monitor {state.monitor}"
        hint = (
            f"[Screen Vision] Monitoring active — {label} every {state.interval}s "
            f"(last capture: {ago_str}). Call capture_screen() to see the current screen."
        )
        event.context_parts.append(hint)
    except Exception:
        pass
