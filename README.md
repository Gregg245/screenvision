# Screen Vision for Sapphire

Lets Sapphire see your screen. Ask her what is on your screen, have her monitor it in the background, or get live commentary on what you are doing.

Works on Windows and Linux (X11 and Wayland).

---

## How to enable

1. Open Sapphire in your browser
2. Go to **Settings → Plugins**
3. Find **Screen Vision** and switch it on

---

## Requirements

```
pip install mss Pillow
```

**Linux — Wayland (Fedora):**
```
sudo dnf install grim xrandr
```

**Linux — Wayland (Debian/Ubuntu):**
```
sudo apt install grim x11-xserver-utils
```

**Linux — X11 (Fedora):**
```
sudo dnf install xrandr
```

**Linux — X11 (Debian/Ubuntu):**
```
sudo apt install x11-xserver-utils
```

**Windows** — no extra system installs needed.

---

## How to use

Ask Sapphire things like:
- "Take a screenshot"
- "What is on my screen?"
- "What monitors do I have?"
- "Capture monitor 2"
- "Start monitoring my screen"
- "Stop screen monitoring"
- "Start game commentary on monitor 1"
- "What is happening in the game?"
- "Stop game commentary"

---

## Settings

Go to **Settings → Plugins → Screen Vision** (gear icon):

| Setting | What it does |
|---|---|
| Monitor Number | Which monitor to capture. 1 = main, 2 = second, 0 = all monitors combined |
| Capture Interval | How often to take a background screenshot (1–15 seconds) |
| Auto-start monitoring | Start capturing automatically when Sapphire starts |
| JPEG Quality | Image quality for background captures (1–95) |
| On-demand Capture Format | PNG = best for reading text, JPEG = smaller file size |
| Auto-sharpen captures | Sharpen the image to make small text easier to read |
| Inject status hint | Reminds Sapphire that screen capture is available when monitoring is on |

---

## Notes

**Repository:** [github.com/Gregg245/screenvision_plugin](https://github.com/Gregg245/screenvision_plugin)

**KDE Wayland users:** `grim` is the recommended screenshot tool and works on all Wayland setups. `spectacle` is a KDE-only alternative.

---

## Known Core Compatibility Issue — Vision Not Reaching the LLM

**Affects:** Sapphire v2.7.0 (introduced 2026-05-15)

**Symptom:** Sapphire takes the screenshot and displays it in chat, but describes it vaguely or atmospherically ("dark room", "someone at a computer") instead of reading the actual screen content. Text in Discord, browsers, terminals, etc. is not read.

**Root cause:** v2.7.0 added a provider vision guard (`supports_images` check in `core/chat/chat_tool_calling.py`). For local/custom LLM providers (LM Studio, llama.cpp, etc.), vision was only enabled if the model name contained keywords like `llava`, `vl`, `vision`. Models with names that don't match those keywords — including Qwen3.5, Gemma 4, and many others — were silently treated as text-only. The image was replaced by a CLIP-based "vibe" description instead of being sent to the LLM.

A second bug caused the `supports_vision` config key to be dropped when the provider instance was constructed (`core/chat/llm_providers/__init__.py` only forwarded a fixed set of config keys).

**Fix — two core files must be patched:**

### 1. `core/chat/llm_providers/__init__.py`
In the `llm_config` dict (around line 200), add:
```python
'supports_vision': config.get('supports_vision'),
```

### 2. `core/chat/llm_providers/openai_compat.py`
At the top of `_supports_multimodal()`, add an explicit override check:
```python
if 'supports_vision' in self.config:
    return bool(self.config['supports_vision'])
```
Also expand the local model vision indicators to include modern VLMs:
```python
vision_indicators = [
    'llava', 'vision', 'vl', 'bakllava', 'cogvlm', 'minicpm-v',
    'gemma-3', 'gemma-4', 'gemma3', 'gemma4',
    'moondream', 'internvl', 'smolvlm', 'multimodal',
    'phi-4', 'phi4',
]
```

### 3. `user/settings.json` — LM Studio provider entry
Add `"supports_vision": true` to your LM Studio provider config:
```json
"lmstudio": {
    "template": "openai",
    "base_url": "http://127.0.0.1:1234/v1",
    ...
    "supports_vision": true
}
```

**After applying:** restart Sapphire. Screen Vision will send the actual PNG to the LLM and read screen content as before.
