#!/usr/bin/env python3
"""
Interactive range puller — bundles a date range of EV or backtest reports
into a single .zip on your PC.

Usage:
    python pull_range.py                              # prompts for everything
    python pull_range.py ev last-7d                   # preset shortcut
    python pull_range.py ev all                       # everything
    python pull_range.py ev 2026-03-01 2026-04-15     # explicit range

Presets: last-1d, last-7d, last-30d, all
"""
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from datetime import date, timedelta


def _find_flyctl() -> str:
    found = shutil.which("flyctl") or shutil.which("fly")
    if found:
        return found
    win_default = os.path.expanduser(r"~\.fly\bin\flyctl.exe")
    if os.path.exists(win_default):
        return win_default
    sys.exit("flyctl not found on PATH. Install: https://fly.io/docs/flyctl/install/")


FLYCTL = _find_flyctl()
HERE = os.path.dirname(os.path.abspath(__file__))

SOURCES = {
    "ev":       "/data/output/archive",
    "backtest": "/data/output/Backtest Archives",
}

PRESETS = {
    "last-1d":  1,
    "last-7d":  7,
    "last-30d": 30,
    "all":      None,  # sentinel — start at epoch
}


def run_remote(shell_cmd: str) -> str:
    """Run a shell command on the Fly machine, return stdout."""
    result = subprocess.run(
        [FLYCTL, "ssh", "console", "-C", f"sh -c '{shell_cmd}'"],
        capture_output=True, text=True,
    )
    return result.stdout


def sftp_get(remote: str, local: str) -> None:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    subprocess.run(
        [FLYCTL, "ssh", "sftp", "get", remote, local],
        env=env, check=False,
    )


def prompt_kind() -> str:
    while True:
        s = input("Type (ev/backtest): ").strip().lower()
        if s in SOURCES:
            return s
        print("  Please enter 'ev' or 'backtest'.")


def prompt_date(label: str) -> str:
    while True:
        s = input(f"{label} (YYYY-MM-DD): ").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return s
        print("  Invalid format. Use YYYY-MM-DD (e.g. 2026-03-15).")


def preset_to_dates(preset: str) -> tuple[str, str]:
    today = date.today()
    days = PRESETS[preset]
    start = date(1970, 1, 1) if days is None else today - timedelta(days=days)
    return start.isoformat(), today.isoformat()


def prompt_range() -> tuple[str, str]:
    print("\nDate range:")
    print("  1) Last day")
    print("  2) Last 7 days")
    print("  3) Last 30 days")
    print("  4) All time")
    print("  5) Custom range")
    options = {"1": "last-1d", "2": "last-7d", "3": "last-30d", "4": "all"}
    while True:
        c = input("Choice [1-5]: ").strip()
        if c in options:
            return preset_to_dates(options[c])
        if c == "5":
            return prompt_date("Start date"), prompt_date("End date")
        print("  Please pick 1-5.")


def file_date(filename: str) -> str | None:
    """Pull the first YYYY-MM-DD or YYYYMMDD date from the filename."""
    m = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", filename)
    return m.group(1) + m.group(2) + m.group(3) if m else None


def main() -> None:
    args = sys.argv[1:]

    if len(args) == 3:                       # ev 2026-03-01 2026-04-15
        kind, start, end = args
        if kind not in SOURCES:
            sys.exit(f"First arg must be 'ev' or 'backtest', got {kind!r}.")
        for d in (start, end):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                sys.exit(f"Date must be YYYY-MM-DD, got {d!r}.")
    elif len(args) == 2:                     # ev last-7d
        kind, preset = args
        if kind not in SOURCES:
            sys.exit(f"First arg must be 'ev' or 'backtest', got {kind!r}.")
        if preset not in PRESETS:
            sys.exit(f"Unknown preset {preset!r}. Options: {', '.join(PRESETS)}.")
        start, end = preset_to_dates(preset)
    elif not args:                           # interactive
        print("Range puller — bundles a date range of reports as a .zip.\n")
        kind = prompt_kind()
        start, end = prompt_range()
    else:
        sys.exit(__doc__)

    src_dir = SOURCES[kind]
    start_c, end_c = start.replace("-", ""), end.replace("-", "")
    if start_c > end_c:
        start_c, end_c = end_c, start_c
        start, end = end, start

    print(f"\nListing {src_dir} ...")
    listing = run_remote(f'ls "{src_dir}"')
    files = [f.strip() for f in listing.splitlines() if f.strip().endswith(".xlsx")]

    matching = []
    for f in files:
        d = file_date(f)
        if d and start_c <= d <= end_c:
            matching.append(f)

    if not matching:
        print(f"No {kind} files found between {start} and {end}.")
        sys.exit(1)

    print(f"Found {len(matching)} file(s). Bundling on remote ...")
    quoted = " ".join(f'"{f}"' for f in matching)
    run_remote(f'cd "{src_dir}" && tar -czf /tmp/range.tar.gz {quoted}')

    local_tar = os.path.join(HERE, "_range_tmp.tar.gz")
    print("Downloading ...")
    sftp_get("/tmp/range.tar.gz", local_tar)

    zip_name = f"{kind}_{start}_to_{end}.zip"
    zip_path = os.path.join(HERE, zip_name)
    print(f"Converting to {zip_name} ...")
    with tarfile.open(local_tar, "r:gz") as tf, \
         zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member in tf.getmembers():
            if member.isfile():
                data = tf.extractfile(member).read()
                zf.writestr(os.path.basename(member.name), data)

    os.remove(local_tar)
    run_remote("rm /tmp/range.tar.gz")

    size_kb = os.path.getsize(zip_path) // 1024
    print(f"\nSaved: {zip_path}")
    print(f"Contains {len(matching)} file(s), {size_kb} KB.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
