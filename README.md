# ding

A tiny cross-platform countdown reminder for the command line.

![python](https://img.shields.io/badge/python-3.8%2B-blue)
![platform](https://img.shields.io/badge/platform-windows%20%7C%20macos%20%7C%20linux-lightgrey)
![license](https://img.shields.io/badge/license-MIT-green)

```bash
ding 2m "Pick up the kids"
```

Your terminal comes back immediately, and two minutes later a small borderless card appears in the bottom-right corner of the screen. Snooze it for 1–5 minutes or dismiss it. As many timers can be pending as you like.

```
┌────────────────────────────────────┐
│ REMINDER · 2m                      │
│ Pick up the kids                   │
│ ────────────────────────────────── │
│ Snooze for (min)                   │
│ [1]  [2]  [3]  [4]  [5]            │
│ [    Snooze    ]  [   Dismiss   ]  │
└────────────────────────────────────┘
```

## Features

- **Non-blocking** — every timer lives in its own detached process, so the shell prompt returns at once and timers never interfere with each other
- **Compound durations** — `1h30m`, `90s`, or a bare number for minutes
- **Snooze with memory** — pick 1–5 minutes; the last choice is preselected next time (first run defaults to 1 minute)
- **Stacked alerts** — a second reminder opens above the first instead of hiding it
- **Borderless and draggable** — no title bar; drag the card by any empty area
- **Crisp on high-DPI screens** — the window is DPI-aware and scales its own layout, so text stays sharp at 150% / 200% instead of being stretched by Windows
- **A real chime** — a short synthesized bell, not a system beep
- **`--list` / `--cancel`** — see what is pending, kill one by id prefix or all of them
- Pure standard library plus `tkinter`; nothing to `pip install`

## Requirements

- Python 3.8 or newer
- **tkinter** — the only UI toolkit used. The python.org installers and the interpreter that `uv` downloads both include it. Some minimal Linux images do not (`apt install python3-tk`), and on macOS a Homebrew Python needs `brew install python-tk`.
- Windows, macOS or Linux

If tkinter is missing, `ding` exits immediately with a message instead of failing silently when the timer fires.

## Install

Run it once without installing anything — `uv` fetches and builds it on the fly:

```bash
uvx --from git+https://github.com/go66/ding.git ding 2m "Pick up the kids"
```

Install it as a persistent command:

```bash
uv tool install git+https://github.com/go66/ding.git
ding 2m "Pick up the kids"
uv tool upgrade ding-cli      # update to the latest commit
uv tool uninstall ding-cli
```

Pin a tag or branch with `@`:

```bash
uvx --from git+https://github.com/go66/ding.git@v1.1.0 ding 2m "x"
```

From a clone:

```bash
git clone https://github.com/go66/ding.git && cd ding
uv run --no-project ding.py 2m "Pick up the kids"
uv tool install .
```

> When hacking on the source, run it with `uv run --no-project ding.py …`. `uvx --from .` reuses the cached wheel, so if you edit the code without bumping `version` it may keep running the previous build.

## Usage

```bash
ding 2m "Pick up the kids"          # ring in 2 minutes
ding 1h30m "Take the cake out"      # compound durations
ding 90s "Check the build"          # seconds
ding 45 "Stretch"                   # bare number = minutes
ding --list                         # what is pending
ding --cancel 3f2a1b                # cancel by id (prefix is enough)
ding --cancel all                   # cancel everything
```

| Duration | Meaning                              |
| -------- | ------------------------------------ |
| `2m`     | 2 minutes                            |
| `90s`    | 90 seconds                           |
| `1h30m`  | 1 hour 30 minutes                    |
| `1.5h`   | 1 hour 30 minutes                    |
| `45`     | 45 minutes (a bare number is minutes) |

Units: `s`, `m`, `h`, `d` (also `sec`, `min`, `hour`, `day`, …).

### Options

| Flag              | Description                                       |
| ----------------- | ------------------------------------------------- |
| `-l`, `--list`    | list pending timers with due time and remaining   |
| `-c`, `--cancel`  | cancel one timer by id prefix, or `all`           |
| `-f`, `--foreground` | wait in the current terminal instead of detaching |
| `-q`, `--quiet`   | do not print the confirmation line                |
| `-V`, `--version` | print the version                                 |

### In the alert window

| Input               | Action                                    |
| ------------------- | ----------------------------------------- |
| `1` … `5`           | choose a snooze duration                  |
| `Enter`             | snooze with the highlighted duration      |
| `Esc`               | dismiss                                   |
| click `Snooze`      | snooze, then a fresh timer is queued      |
| click `Dismiss`     | close this alert                          |
| drag any empty part | move the card                             |

Snoozing re-queues the same message with the chosen delay and records that delay as the new default.

## Files

Everything lives in a per-user data directory:

| OS      | Directory                                |
| ------- | ---------------------------------------- |
| Windows | `%APPDATA%\ding\`                        |
| macOS   | `~/Library/Application Support/ding/`    |
| Linux   | `${XDG_CONFIG_HOME:-~/.config}/ding/`    |

Inside it:

- `config.json` — the remembered snooze duration and the `sound` switch
- `pending/<id>.json` — one file per timer, so parallel timers never race
- `ding.log` — worker diagnostics, only written when something goes wrong

Set `"sound": false` in `config.json` to silence the chime.

## Development

```bash
uv run --no-project ding.py 3s "layout check"    # ring almost immediately
uv run --no-project ding.py --list
```

The whole tool is one file, `ding.py` (~770 lines):

- `parse_duration()` — `2m` / `1h30m` / `90s` / `45`
- `spawn_worker()` — starts a detached process per timer (`DETACHED_PROCESS` on Windows, `start_new_session` elsewhere)
- `worker_main()` — sleeps until the due time, rings, then re-queues on snooze
- `show_alert()` — the tkinter card: borderless, bottom-right, stacked, draggable

The chime (`ding.wav`, 1.4 s) is synthesized rather than sampled: a 1046.5 Hz fundamental plus four inharmonic partials, each decaying at its own rate, with a 3 ms attack and a short fade-out so it rings without clicking.

## License

MIT
