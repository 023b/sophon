"""Screens, kitty windows and tmux sessions."""
import os
import re
import subprocess
import time

from .config import BOARD, FONT_BIG, FONT_LAPTOP, LEAD, PANEL
from .util import load, run_dir, sh, state


def monitors():
    out = sh("xrandr", "--listmonitors")
    mons = []
    for m in re.finditer(r"(\d+)/\d+x(\d+)/\d+\+(\d+)\+(\d+)\s+(\S+)$", out, re.M):
        w, h, x, y, name = int(m[1]), int(m[2]), int(m[3]), int(m[4]), m[5]
        mons.append({"name": name, "x": x, "y": y, "w": w, "h": h})
    mons.sort(key=lambda m: m["w"] * m["h"], reverse=True)
    big = mons[0]
    small = mons[1] if len(mons) > 1 else mons[0]
    return big, small


def workarea_top():
    m = re.search(r"=\s*(\d+),\s*(\d+)", sh("xprop", "-root", "_NET_WORKAREA"))
    return int(m[2]) if m else 0


def window_id(name, timeout=15):
    cls = f"sw-{name}"
    end = time.time() + timeout
    while time.time() < end:
        for line in sh("wmctrl", "-lx").splitlines():
            parts = line.split()
            if len(parts) > 2 and parts[2] == f"{cls}.{cls}":
                return parts[0]
        time.sleep(0.3)
    return None


def open_window(name, screen):
    if window_id(name, timeout=0.1):
        return
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    font = FONT_LAPTOP if screen == "laptop" else FONT_BIG
    title = {PANEL: "SOPHON control panel", LEAD: "SOPHON", BOARD: "swarm board"}.get(name, f"swarm: {name}")
    subprocess.Popen(["kitty", "-o", "linux_display_server=x11",
                      "-o", f"font_size={font}", "-o", "confirm_os_window_close=0",
                      "-o", "background_opacity=1", "-o", "background=#000000", "-o", "window_padding_width=0",
                      "--class", f"sw-{name}", "-T", title,
                      "tmux", "attach", "-t", f"=sw-{name}"],
                     env=env, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    window_id(name)


def frame_extents(wid):
    """(left, right, top, bottom) of the window manager frame (title bar and borders) around a window."""
    m = re.search(r"=\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)", sh("xprop", "-id", wid, "_NET_FRAME_EXTENTS"))
    return tuple(int(v) for v in m.groups()) if m else (0, 0, 0, 0)


def place(wid, x, y, w, h, maximise=False):
    """Fit the whole window, title bar included, exactly into the cell (x, y, w, h)."""
    sh("wmctrl", "-i", "-r", wid, "-b", "remove,maximized_vert,maximized_horz")
    l, r, t, b = frame_extents(wid)
    sh("wmctrl", "-i", "-r", wid, "-e", f"0,{x},{y},{max(200, w - l - r)},{max(120, h - t - b)}")
    if maximise:
        sh("wmctrl", "-i", "-r", wid, "-b", "add,maximized_vert,maximized_horz")


def single_screen():
    big, small = monitors()
    return big is small or big == small


def show_worker_windows():
    """Worker windows open automatically on two screens; on one screen only when SWARM_WINDOWS=all."""
    return os.environ.get("SWARM_WINDOWS", "auto") == "all" or (
        os.environ.get("SWARM_WINDOWS", "auto") == "auto" and not single_screen())


def tile(run, only=None):
    """Lay windows out. With only=NAME, place just that window in its slot and leave every other window alone."""
    st = load(run)
    if single_screen():
        return tile_single(run, only)
    big, small = monitors()
    top = workarea_top()
    names = [n for n, a in st["agents"].items() if a["screen"] == "big" and a.get("alive", True)]
    names += [BOARD] if alive(BOARD) and window_id(BOARD, timeout=0.2) else []
    y0 = max(big["y"], top)
    full_h = big["h"] - (y0 - big["y"])
    x0, width = big["x"], big["w"]
    names = [n for n in names if window_id(n, timeout=0.2)]
    # Fixed layout so a new window never forces others to move: the panel owns the left 2/5 of the larger screen,
    # worker windows fill a stable grid of slots on the right (first free slot wins), extras cascade.
    pw = big["w"] * 2 // 5
    if alive(PANEL) and only in (None, PANEL):
        wid = window_id(PANEL, timeout=3)
        if wid:
            place(wid, big["x"], y0, pw, full_h)
    x0, width = (big["x"] + pw, big["w"] - pw) if alive(PANEL) else (big["x"], big["w"])
    cols, rows = int(os.environ.get("SWARM_GRID_COLS", 2)), int(os.environ.get("SWARM_GRID_ROWS", 3))
    W, H = width // cols, full_h // rows
    slots = st.setdefault("slots", {})
    slots = {k: v for k, v in slots.items() if k in names}
    for name in names:
        if name not in slots:
            free = [i for i in range(cols * rows) if i not in slots.values()]
            slots[name] = free[0] if free else cols * rows + len(slots)
    st["slots"] = slots
    with state(run) as s_:
        s_["slots"] = slots
    for name in names:
        if only not in (None, name):
            continue
        wid = window_id(name, timeout=3)
        if not wid:
            continue
        i = slots[name]
        if i < cols * rows:
            r, c = divmod(i, cols)
            place(wid, x0 + c * W, y0 + r * H, W, H)
        else:  # more windows than slots: cascade over the grid
            k = i - cols * rows
            place(wid, x0 + 60 * (k % 10), y0 + 60 * (k % 10), W, H)
    for name, a in st["agents"].items():
        if a["screen"] == "laptop" and only in (None, name):
            wid = window_id(name, timeout=3)
            if wid:
                stop = max(small["y"], top)
                place(wid, small["x"], stop, small["w"], small["h"] - (stop - small["y"]))


def tile_single(run, only=None):
    """One screen: Sophon on the left, the panel on the right; worker windows (if any) cascade over the panel side."""
    st = load(run)
    scr, _ = monitors()
    top = max(scr["y"], workarea_top())
    h = scr["h"] - (top - scr["y"])
    lw = scr["w"] * 2 // 5
    plan = {LEAD: (scr["x"], top, lw, h), "manager": (scr["x"], top, lw, h), PANEL: (scr["x"] + lw, top, scr["w"] - lw, h)}
    workers = [n for n, a in st["agents"].items() if n not in plan and a.get("alive", True)]
    for i, n in enumerate(workers):
        plan[n] = (scr["x"] + lw + 40 * (i % 8), top + 40 * (i % 8), (scr["w"] - lw) * 3 // 4, h * 3 // 4)
    for n, geo in plan.items():
        if only in (None, n):
            wid = window_id(n, timeout=0.5)
            if wid:
                place(wid, *geo)


def tmux(*args):
    return sh("tmux", *args)


def alive(name):
    return subprocess.run(["tmux", "has-session", "-t", f"=sw-{name}"],
                          capture_output=True).returncode == 0


def screen_text(name, lines=60):
    return tmux("capture-pane", "-p", "-t", f"=sw-{name}:", "-S", f"-{lines}")


def dialog_flag(run, name):
    return run / "status" / f"{name}.dialog"


def busy_with_dialog(name, run=None):
    """True if the agent shows a permission/choice dialog, where typed keys would pick options.
    Claude agents started with hooks report prompts themselves (Notification hook -> status/NAME.dialog), so the
    screen is only read for the folder-trust prompt, which appears before hooks load. Agents without hooks fall
    back to the old screen regex."""
    try:
        run = run or run_dir()
    except SystemExit:
        run = None
    tail = "\n".join(screen_text(name, 30).splitlines()[-25:])
    if run is not None:
        f = dialog_flag(run, name)
        if f.exists() and time.time() - f.stat().st_mtime < 1800:
            return True
        if (run / "hooks.json").exists() and load(run).get("agents", {}).get(name, {}).get("engine") == "claude":
            return "Do you trust" in tail
    return bool(re.search(r"Do you want to|Do you trust|❯ 1\.|Yes, and don't ask", tail))


def type_into(name, text):
    t = f"=sw-{name}:"
    tmux("send-keys", "-t", t, "-l", text)
    time.sleep(0.4)
    tmux("send-keys", "-t", t, "Enter")


def style_session(name, colour, run=None):
    t = f"=sw-{name}"
    run = run or run_dir()
    for opt in (["status-position", "top"], ["status-style", f"bg={colour},fg=black,bold"],
                ["status-left", f" {name.upper()} "], ["status-left-length", "40"],
                ["status-right", f" #(cat {run}/status/{name} 2>/dev/null) %H:%M "],
                ["status-right-length", "80"], ["status-interval", "5"], ["mouse", "on"],
                ["history-limit", "50000"], ["window-status-format", ""],
                ["window-status-current-format", ""]):
        tmux("set-option", "-t", t, *opt)
