# Screen Vision Plugin for Sapphire

**Repository:** [https://github.com/Gregg245/screenvision_plugin](https://github.com/Gregg245/screenvision_plugin)
**Author:** [Gregg245](https://github.com/Gregg245)

Let the AI see your screen. Captures screenshots on demand or continuously in the background. Works on Windows and Linux (X11 and Wayland).

## Setup

### 1. Enable the plugin
Settings > Plugins > Screen Vision > Enable

### 2. Install Python packages
```
pip install mss Pillow
```

### 3. Install system packages (Linux only)

**Wayland — Fedora:**
```
sudo dnf install grim xrandr
```

**Wayland — Debian/Ubuntu:**
```
sudo apt install grim x11-xserver-utils
```

**X11 — Fedora:**
```
sudo dnf install xrandr
```

**X11 — Debian/Ubuntu:**
```
sudo apt install x11-xserver-utils
```

Windows needs no system packages.

### Screenshot backends by environment

| Environment | Primary | Fallback |
|---|---|---|
| Linux Wayland (KDE) | `grim` | spectacle → mss |
| Linux Wayland (Sway/Hyprland) | `grim` | wlr-randr + mss |
| Linux Wayland (GNOME) | `grim` | mss |
| Linux X11 | `mss` | — |
| Windows | `mss` | PowerShell |

**`grim`** is the recommended Wayland screenshot tool — it works on all Wayland compositors and captures native Wayland windows. `spectacle` is a KDE-only alternative.

**`xrandr`** is used by `list_monitors` to detect monitor layout on X11 and GNOME Wayland. Without it, monitors show as generic names.

**`wlr-randr`** (optional) improves monitor detection on Sway and Hyprland:
```
# Fedora
sudo dnf install wlr-randr
# Debian/Ubuntu
sudo apt install wlr-randr
```

## Usage

Ask Sapphire things like:

- "Take a screenshot"
- "What's on my screen?"
- "What monitors do I have?"
- "Capture monitor 2"
- "Start monitoring my screen"
- "Stop screen monitoring"
- "Start game commentary on monitor 1"
- "What's happening in the game?"
- "Stop game commentary"

## Settings

- **Monitor Number** — which monitor to capture. 1 = primary, 2 = second, 0 = all monitors combined
- **Capture Interval** — how often to take a background screenshot (1–15 seconds)
- **Auto-start monitoring** — automatically begin capturing when Sapphire starts
- **JPEG Quality** — compression quality for background monitoring (1–95)
- **On-demand Capture Format** — PNG (lossless, best for reading text) or JPEG (smaller file)
- **Auto-sharpen captures** — apply unsharp-mask sharpening to improve small text legibility
- **Inject status hint** — reminds the AI that screen capture is available when monitoring is active
