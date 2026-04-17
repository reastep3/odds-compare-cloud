#!/usr/bin/env python3
"""
Pull the latest report from the Fly volume to this folder.

Usage:
    python pull_latest.py ev          # newest ev_edges_*.xlsx
    python pull_latest.py backtest    # newest backtest_*.xlsx
    python pull_latest.py archives    # full archive/ + Backtest Archives/ as tarball

Double-click pull-latest-ev.bat or pull-latest-backtest.bat for the same effect.
"""
import os
import shutil
import subprocess
import sys


def _find_flyctl() -> str:
    # Prefer whatever's on PATH (Mac/Linux brew install, Windows after PATH setup).
    found = shutil.which("flyctl") or shutil.which("fly")
    if found:
        return found
    # Fallback: well-known Windows install location from the official installer.
    win_default = os.path.expanduser(r"~\.fly\bin\flyctl.exe")
    if os.path.exists(win_default):
        return win_default
    sys.exit("flyctl not found on PATH. Install: https://fly.io/docs/flyctl/install/")


FLYCTL = _find_flyctl()
HERE = os.path.dirname(os.path.abspath(__file__))

PREFIXES = {
    "ev":       "ev_edges_",
    "backtest": "backtest_",
}


def run_remote(shell_cmd: str) -> str:
    """Run a shell command on the Fly machine, return stdout (stripped)."""
    result = subprocess.run(
        [FLYCTL, "ssh", "console", "-C", f"sh -c '{shell_cmd}'"],
        capture_output=True, text=True,
    )
    # Windows flyctl exits 1 on success ("handle is invalid"); ignore returncode.
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def sftp_get(remote: str, local: str) -> None:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    subprocess.run(
        [FLYCTL, "ssh", "sftp", "get", remote, local],
        env=env, check=False,
    )


def pull_latest(kind: str) -> None:
    prefix = PREFIXES[kind]
    remote = run_remote(
        f"ls -t /data/output/{prefix}*.xlsx 2>/dev/null | head -1"
    )
    if not remote.startswith("/data/"):
        print(f"No {kind} reports found in /data/output.")
        sys.exit(1)

    name = os.path.basename(remote)
    local = os.path.join(HERE, name)
    print(f"Pulling {name}...")
    sftp_get(remote, local)
    print(f"Saved: {local}")
    try:
        os.startfile(local)
    except OSError:
        pass


def pull_archives() -> None:
    """Tar up everything under /data/output (incl. archive/ + Backtest Archives/)
    and download the tarball to this folder."""
    print("Bundling archives on remote...")
    run_remote(
        "cd /data/output && tar -czf /tmp/archives.tar.gz "
        "archive 'Backtest Archives'"
    )
    local = os.path.join(HERE, "archives.tar.gz")
    print("Pulling tarball...")
    sftp_get("/tmp/archives.tar.gz", local)
    print(f"Saved: {local}")
    print("Extract with: tar -xzf archives.tar.gz")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in (*PREFIXES, "archives"):
        print(__doc__)
        sys.exit(2)

    kind = sys.argv[1]
    if kind == "archives":
        pull_archives()
    else:
        pull_latest(kind)


if __name__ == "__main__":
    main()
