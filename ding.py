#!/usr/bin/env python3
"""ding - a tiny cross-platform countdown reminder.

Examples
--------
    ding 2m "Pick up the kids"        ring in 2 minutes
    ding 1h30m "Take the cake out"    compound durations work
    ding 90s                          seconds; bare numbers mean minutes
    ding 45 "Stretch"                 bare numbers mean minutes
    ding --list                       show active timers
    ding --cancel all                 cancel everything

Every timer runs in its own detached process, so several can be pending at
the same time. When one fires, a window pops up with a "snooze 1-5 minutes"
choice. The last choice is remembered and preselected next time.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

APP = "ding"
__version__ = "1.1.0"

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"

DEVNULL = subprocess.DEVNULL


# --------------------------------------------------------------------------- #
# paths, config, logging
# --------------------------------------------------------------------------- #

def app_dir() -> Path:
    if IS_WIN:
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif IS_MAC:
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    p = root / APP
    p.mkdir(parents=True, exist_ok=True)
    return p


def pending_dir() -> Path:
    p = app_dir() / "pending"
    p.mkdir(parents=True, exist_ok=True)
    return p


DEFAULT_CONFIG = {"snooze_minutes": 1, "sound": True}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.loads((app_dir() / "config.json").read_text("utf-8"))
        if isinstance(data, dict):
            for key in DEFAULT_CONFIG:
                if key in data:
                    cfg[key] = data[key]
    except (OSError, ValueError):
        pass
    minutes = cfg.get("snooze_minutes")
    if not isinstance(minutes, int) or not 1 <= minutes <= 5:
        cfg["snooze_minutes"] = 1
    cfg["sound"] = bool(cfg.get("sound", True))
    return cfg


def save_config(cfg: dict) -> None:
    target = app_dir() / "config.json"
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cfg, indent=2), "utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        log(f"cannot save config: {exc}")


def log(message: str) -> None:
    """Best effort log file - the worker processes have no console."""
    try:
        path = app_dir() / "ding.log"
        if path.exists() and path.stat().st_size > 256_000:
            path.write_text("", "utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {message}\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# duration parsing
# --------------------------------------------------------------------------- #

UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)([a-z]+)")
_BARE_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_duration(raw: str):
    """Return the duration in seconds, or None when the text is not a duration.

    Bare numbers are minutes: "2" -> 120. Units may be combined: "1h30m".
    """
    text = raw.strip().lower().replace(" ", "")
    if not text:
        return None
    if _BARE_RE.fullmatch(text):
        return float(text) * 60.0
    parts = _TOKEN_RE.findall(text)
    if not parts or "".join(num + unit for num, unit in parts) != text:
        return None
    total = 0.0
    for num, unit in parts:
        if unit not in UNIT_SECONDS:
            return None
        total += float(num) * UNIT_SECONDS[unit]
    return total if total > 0 else None


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds <= 0:
        return "0s"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    out = ""
    if days:
        out += f"{days}d"
    if hours:
        out += f"{hours}h"
    if minutes:
        out += f"{minutes}m"
    if secs or not out:
        out += f"{secs}s"
    return out


# --------------------------------------------------------------------------- #
# timer records (one json file per timer, so parallel timers never collide)
# --------------------------------------------------------------------------- #

def record_path(rid: str) -> Path:
    return pending_dir() / f"{rid}.json"


def write_record(rec: dict) -> None:
    target = record_path(rec["id"])
    tmp = target.with_suffix(".json.tmp")
    payload = {k: v for k, v in rec.items() if not k.startswith("_")}
    tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    os.replace(tmp, target)


def load_record(rid: str):
    try:
        rec = json.loads(record_path(rid).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("id") == rid else None


def delete_record(rid: str) -> None:
    for path in (record_path(rid), record_path(rid).with_suffix(".json.tmp")):
        try:
            path.unlink()
        except OSError:
            pass


def read_records() -> list:
    records = []
    for path in sorted(pending_dir().glob("*.json")):
        try:
            rec = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("id"):
            rec["_path"] = path
            records.append(rec)
    return records


def new_record(message: str, duration: float, label: str = None) -> dict:
    now = time.time()
    return {
        "id": uuid.uuid4().hex[:6],
        "message": message,
        "label": label or human_duration(duration),
        "duration": duration,
        "created": now,
        "fire_at": now + duration,
        "status": "waiting",
        "pid": 0,
    }


# --------------------------------------------------------------------------- #
# platform helpers
# --------------------------------------------------------------------------- #

def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if IS_WIN:
        # os.kill(pid, 0) would TERMINATE the process on Windows - never use it.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def terminate_pid(pid: int) -> None:
    if not pid or pid == os.getpid():
        return
    try:
        if IS_WIN:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdout=DEVNULL, stderr=DEVNULL, creationflags=0x08000000,
            )
        else:
            os.kill(int(pid), signal.SIGTERM)
    except Exception as exc:
        log(f"terminate {pid} failed: {exc}")


def has_tkinter() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


def sound_path():
    """Locate the bundled ding.wav: next to the script, or in the install share dir."""
    here = Path(__file__).resolve().parent
    for candidate in (
        here / "ding.wav",
        Path(sys.prefix) / "share" / "ding" / "ding.wav",
        Path(sys.base_prefix) / "share" / "ding" / "ding.wav",
    ):
        if candidate.is_file():
            return candidate
    return None


def play_sound() -> None:
    """Ring the ding. Never blocks, never raises."""
    path = sound_path()
    try:
        if IS_WIN:
            import winsound

            # read the bytes so non-ASCII install paths cannot break PlaySound
            data = path.read_bytes() if path else None

            def _ring() -> None:
                try:
                    if data:
                        winsound.PlaySound(data, winsound.SND_MEMORY)
                    else:
                        winsound.MessageBeep(winsound.MB_ICONASTERISK)
                except Exception:
                    pass

            threading.Thread(target=_ring, daemon=True).start()
        elif IS_MAC:
            cmd = ["afplay", str(path)] if path else ["afplay", "/System/Library/Sounds/Glass.aiff"]
            subprocess.Popen(cmd, stdout=DEVNULL, stderr=DEVNULL)
        else:
            cmd = ["paplay", str(path)] if path else ["canberra-gtk-play", "-i", "complete"]
            subprocess.Popen(cmd, stdout=DEVNULL, stderr=DEVNULL)
    except Exception as exc:
        log(f"sound failed: {exc}")


# --------------------------------------------------------------------------- #
# alert window - tkinter when available, native dialog otherwise
# --------------------------------------------------------------------------- #

BG = "#161b26"
SURFACE = "#2b3345"
BORDER = "#3a4459"
TEXT = "#eef2f9"
MUTED = "#8b97ad"
ACCENT = "#3d7dff"
ACCENT_HOVER = "#5a92ff"
GHOST_HOVER = "#39435a"

SNOOZE_CHOICES = (1, 2, 3, 4, 5)


def usable_area(root):
    """Usable screen area (taskbar/dock excluded) as (left, top, right, bottom)."""
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        try:
            user32 = ctypes.windll.user32
            user32.SystemParametersInfoW.argtypes = [
                wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT,
            ]
            user32.SystemParametersInfoW.restype = wintypes.BOOL
            # SPI_GETWORKAREA = 0x0030
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            pass
    bottom = root.winfo_screenheight() - (80 if IS_MAC else 0)
    return 0, 0, root.winfo_screenwidth(), bottom


def stack_index(rid: str) -> int:
    """How many other alerts are already ringing -> stacking slot for this one."""
    ringing = sorted({r.get("id", "") for r in read_records() if r.get("status") == "ringing"})
    if rid in ringing:
        return min(4, ringing.index(rid))
    return 0


def show_alert(message: str, label: str, default_minutes: int, position: int = 0):
    """Block until the user answers. Returns (action, minutes)."""
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except Exception as exc:  # tkinter is the only renderer now
        log(f"worker: tkinter missing ({exc})")
        print(f"ding: this Python has no tkinter ({exc})", file=sys.stderr)
        return "dismiss", default_minutes

    try:
        root = tk.Tk()
    except Exception as exc:
        log(f"worker: Tk() failed ({exc})")
        return "dismiss", default_minutes

    state = {"action": "dismiss", "minutes": default_minutes}
    drag = {"active": False, "dx": 0, "dy": 0}

    root.title("Ding")
    root.configure(bg=BORDER)
    root.resizable(False, False)
    try:
        root.overrideredirect(True)  # borderless card, no title bar
    except tk.TclError:
        pass
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    families = set(tkfont.families(root))
    family = next(
        (f for f in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "DejaVu Sans")
         if f in families),
        "TkDefaultFont",
    )

    # the 1px pad around body paints a hairline border for the borderless window
    body = tk.Frame(root, bg=BG, padx=20, pady=14)
    body.pack(fill="both", expand=True, padx=1, pady=1)

    brand_label = tk.Label(
        body, text=f"REMINDER  ·  {label}", bg=BG, fg=MUTED,
        font=(family, 8, "bold"), anchor="w",
    )
    brand_label.pack(fill="x")

    message_label = tk.Label(
        body, text=message, bg=BG, fg=TEXT, font=(family, 12, "bold"),
        wraplength=336, justify="left", anchor="w",
    )
    message_label.pack(fill="x", pady=(6, 0))

    separator = tk.Frame(body, bg=SURFACE, height=1)
    separator.pack(fill="x", pady=10)

    hint_label = tk.Label(
        body, text="Snooze for", bg=BG, fg=MUTED, font=(family, 9), anchor="w",
    )
    hint_label.pack(fill="x")

    chips_row = tk.Frame(body, bg=BG)
    chips_row.pack(fill="x", pady=(6, 0))
    chips = {}

    def select(minutes: int) -> None:
        state["minutes"] = minutes
        for value, chip in chips.items():
            if value == minutes:
                chip.configure(bg=ACCENT, fg="#ffffff")
            else:
                chip.configure(bg=SURFACE, fg=MUTED)

    for index, minutes in enumerate(SNOOZE_CHOICES):
        chip = tk.Label(
            chips_row, text=str(minutes), bg=SURFACE, fg=MUTED,
            font=(family, 9, "bold"), pady=5, cursor="hand2",
        )
        chip.pack(side="left", expand=True, fill="x", padx=(0 if index == 0 else 5, 0))
        chip.bind("<Button-1>", lambda _e, m=minutes: select(m))
        chip.bind(
            "<Enter>",
            lambda _e, m=minutes: chips[m].configure(
                bg=ACCENT if state["minutes"] == m else GHOST_HOVER),
        )
        chip.bind(
            "<Leave>",
            lambda _e, m=minutes: chips[m].configure(
                bg=ACCENT if state["minutes"] == m else SURFACE),
        )
        chips[minutes] = chip

    actions = tk.Frame(body, bg=BG)
    actions.pack(fill="x", pady=(14, 0))
    actions.columnconfigure(0, weight=1, uniform="btn")
    actions.columnconfigure(1, weight=1, uniform="btn")

    def finish(action: str) -> None:
        state["action"] = action
        # Hide first and destroy a moment later. Destroying a borderless window
        # while the mouse button is still down makes Windows re-route the click
        # to whatever sits underneath - which closed two stacked alerts at once.
        try:
            root.withdraw()
        except tk.TclError:
            pass
        root.after(80, root.destroy)

    snooze_btn = tk.Label(
        actions, text="Snooze", bg=ACCENT, fg="#ffffff",
        font=(family, 10, "bold"), pady=8, cursor="hand2",
    )
    snooze_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
    snooze_btn.bind("<Button-1>", lambda _e: finish("snooze"))
    snooze_btn.bind("<Enter>", lambda _e: snooze_btn.configure(bg=ACCENT_HOVER))
    snooze_btn.bind("<Leave>", lambda _e: snooze_btn.configure(bg=ACCENT))

    dismiss_btn = tk.Label(
        actions, text="Dismiss", bg=SURFACE, fg=TEXT,
        font=(family, 10, "bold"), pady=8, cursor="hand2",
    )
    dismiss_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))
    dismiss_btn.bind("<Button-1>", lambda _e: finish("dismiss"))
    dismiss_btn.bind("<Enter>", lambda _e: dismiss_btn.configure(bg=GHOST_HOVER))
    dismiss_btn.bind("<Leave>", lambda _e: dismiss_btn.configure(bg=SURFACE))

    # the window has no title bar, so let the empty area drag it around
    def start_move(event):
        drag["active"] = True
        drag["dx"] = event.x_root - root.winfo_x()
        drag["dy"] = event.y_root - root.winfo_y()

    def on_move(event):
        if drag["active"]:
            root.geometry(f"+{event.x_root - drag['dx']}+{event.y_root - drag['dy']}")

    def end_move(_event=None):
        drag["active"] = False

    for widget in (body, brand_label, message_label, separator, hint_label, chips_row):
        widget.bind("<ButtonPress-1>", start_move)
        widget.bind("<B1-Motion>", on_move)
        widget.bind("<ButtonRelease-1>", end_move)

    select(default_minutes)

    for minutes in SNOOZE_CHOICES:
        root.bind(str(minutes), lambda _e, m=minutes: select(m))
    root.bind("<Return>", lambda _e: finish("snooze"))
    root.bind("<KP_Enter>", lambda _e: finish("snooze"))
    root.bind("<Escape>", lambda _e: finish("dismiss"))
    root.protocol("WM_DELETE_WINDOW", lambda: finish("dismiss"))

    root.update_idletasks()
    width = max(376, root.winfo_reqwidth())
    height = root.winfo_reqheight()
    left, top, right, bottom = usable_area(root)
    margin = 20
    x = max(left + margin, right - width - margin)
    # stack alerts upwards from the bottom-right corner instead of overlapping
    y = bottom - height - margin - position * (height + 8)
    if y < top + margin:
        y = top + margin + position * 28
    root.geometry(f"{width}x{height}+{x}+{y}")
    try:
        root.lift()
        root.focus_force()
    except tk.TclError:
        pass

    root.mainloop()
    return state["action"], state["minutes"]




# --------------------------------------------------------------------------- #
# worker process
# --------------------------------------------------------------------------- #

def spawn_worker(rec: dict) -> int:
    """Start a detached process that sleeps until fire_at and then rings."""
    script = Path(__file__).resolve()
    exe = sys.executable or "python3"
    if IS_WIN:
        pythonw = Path(exe).with_name("pythonw.exe")
        if pythonw.exists():
            exe = str(pythonw)

    command = [exe, str(script), "--worker", rec["id"]]
    kwargs = dict(
        stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
        cwd=str(Path.home()), close_fds=True,
    )
    if IS_WIN:
        # DETACHED_PROCESS | CREATE_NO_WINDOW - never flash a console window
        kwargs["creationflags"] = 0x08000008
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(command, **kwargs)
    rec["pid"] = proc.pid
    write_record(rec)
    return proc.pid


def worker_main(rid: str) -> int:
    try:
        rec = load_record(rid)
        if not rec:
            log(f"worker {rid}: record missing, exiting")
            return 1
        fire_at = float(rec.get("fire_at", 0))

        while True:
            remaining = fire_at - time.time()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.5))

        rec["status"] = "ringing"
        write_record(rec)

        cfg = load_config()
        if cfg.get("sound", True):
            play_sound()

        action, minutes = show_alert(
            rec.get("message") or "Timer finished",
            rec.get("label") or human_duration(rec.get("duration", 0)),
            cfg["snooze_minutes"],
            stack_index(rid),
        )
        delete_record(rid)

        if action == "snooze":
            minutes = max(1, min(5, int(minutes)))
            cfg["snooze_minutes"] = minutes
            save_config(cfg)
            nxt = new_record(rec.get("message") or "Timer finished",
                             minutes * 60.0, label=f"{minutes}m")
            spawn_worker(nxt)
        return 0
    except Exception:
        log(f"worker {rid} crashed:\n{traceback.format_exc()}")
        delete_record(rid)
        return 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def cmd_list() -> int:
    now = time.time()
    live = []
    for rec in read_records():
        if not pid_alive(rec.get("pid") or 0):
            try:
                rec["_path"].unlink()
            except (OSError, KeyError):
                pass
            continue
        live.append(rec)

    if not live:
        print("No active timers.")
        return 0

    live.sort(key=lambda r: r.get("fire_at") or 0)
    print(f"{'ID':<8}{'DUE':<10}{'IN':<10}MESSAGE")
    for rec in live:
        fire = rec.get("fire_at") or now
        left = max(0.0, fire - now)
        state = "ringing" if rec.get("status") == "ringing" else human_duration(left)
        due = time.strftime("%H:%M:%S", time.localtime(fire))
        print(f"{rec['id']:<8}{due:<10}{state:<10}{rec.get('message', '')}")
    return 0


def cmd_cancel(target: str, quiet: bool = False) -> int:
    records = read_records()
    if not records:
        print("No active timers.")
        return 1

    if target.lower() in ("all", "*", "-a"):
        chosen = records
    else:
        chosen = [r for r in records if r["id"] == target] or \
                 [r for r in records if str(r["id"]).startswith(target)]

    if not chosen:
        print(f"No timer matching '{target}'.")
        return 1

    for rec in chosen:
        terminate_pid(rec.get("pid") or 0)
        delete_record(rec["id"])
        if not quiet:
            print(f"Cancelled {rec['id']}  {rec.get('message', '')}")
    return 0


USAGE_EPILOG = """\
examples:
  ding 2m "Pick up the kids"
  ding 1h30m "Take the cake out of the oven"
  ding 90s "Check the build"
  ding 45 "Stretch"                  bare numbers are minutes
  ding --list
  ding --cancel 3f2a1b
  ding --cancel all
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ding",
        description="Cross-platform countdown reminder that pops up a window with a snooze option.",
        epilog=USAGE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("words", nargs="*", metavar="TIME [MESSAGE]")
    parser.add_argument("-l", "--list", action="store_true", help="list active timers")
    parser.add_argument("-c", "--cancel", metavar="ID|all", help="cancel a timer (id prefix or 'all')")
    parser.add_argument("-f", "--foreground", action="store_true",
                        help="wait in this terminal instead of detaching")
    parser.add_argument("-q", "--quiet", action="store_true", help="do not print a confirmation")
    parser.add_argument("-V", "--version", action="version", version=f"ding {__version__}")
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    args = list(sys.argv[1:] if argv is None else argv)

    # internal: the detached worker re-enters here
    if args and args[0] == "--worker":
        if len(args) < 2:
            return 2
        return worker_main(args[1])

    opts = build_parser().parse_args(args)

    if opts.list:
        return cmd_list()
    if opts.cancel is not None:
        return cmd_cancel(opts.cancel, opts.quiet)

    spec = None
    words = []
    for word in opts.words:
        if spec is None and parse_duration(word) is not None:
            spec = word
        else:
            words.append(word)

    if spec is None:
        build_parser().print_help()
        return 2

    duration = parse_duration(spec)
    if not duration or duration < 1:
        print(f"ding: '{spec}' is not a usable duration (minimum 1 second).", file=sys.stderr)
        return 2

    message = " ".join(words).strip() or "Your timer is up"

    if not has_tkinter():
        print(
            "ding: this Python has no tkinter, so the alert window cannot open.\n"
            "      Run it with the uv-managed interpreter instead:\n"
            "        uv run --no-project ding.py 2m \"...\"      (in the project folder)\n"
            "        uvx --from . ding 2m \"...\"               (installed package)",
            file=sys.stderr,
        )
        return 2

    rec = new_record(message, duration)
    if opts.foreground:
        write_record(dict(rec, pid=os.getpid()))
        return worker_main(rec["id"])

    spawn_worker(rec)

    if not opts.quiet:
        due = time.strftime("%H:%M:%S", time.localtime(rec["fire_at"]))
        print(f"Timer set for {due} (in {rec['label']}) - {message}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
