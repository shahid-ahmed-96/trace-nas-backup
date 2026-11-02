#!/usr/bin/env python3
"""
Incremental backup from the cluster to the lab NAS.

    python3 backup.py now                 back up right away
    python3 backup.py scheduled           what cron runs every Friday at 5 pm
    python3 backup.py now --dry-run       show what would be copied; copies nothing, writes no log

Rules:
  - The destination tree mirrors the source tree exactly.
  - Only new or changed files are copied (change = different size or modification time).
  - Nothing is ever deleted from the backup.
  - When a file changed, the old backup copy is kept next to it with the date added,
    e.g. results.csv.2026_10_16 (a second change on the same day adds the time).
  - Symlinks are skipped.
  - A log (CSV) is appended to <log_dir>/backuplog.YYYY_MM_DD.csv on the cluster.
    It lists only new, updated and failed files (plus one line if the run could not happen).

Settings live in backup_config.ini next to this script (or pass --config PATH).
Uses only the Python standard library and rsync.
"""

import argparse
import configparser
import csv
import datetime as dt
import fcntl
import io
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = Path.home() / ".trace_backup"          # lock file + local copy of every log
CSV_HEADER = ["time", "status", "path", "size", "size_bytes", "reason"]
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=30"]

# rsync --out-format line: "<11-char itemize code> <length> <name>"
ITEM_RE = re.compile(r"^(\S{11}) (\d+) (.*)$")


# ---------------------------------------------------------------- helpers

def human_size(n):
    if n is None or n == "":
        return ""
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def load_config(path):
    cp = configparser.ConfigParser()
    if not cp.read(path):
        sys.exit(f"Config file not found: {path}")
    c = cp["backup"]
    cfg = {
        "ssh_host": c.get("ssh_host", "").strip(),
        "source": c.get("source", "").strip().rstrip("/"),
        "mount_point": c.get("mount_point", "").strip().rstrip("/"),
        "destination": c.get("destination", "").strip().rstrip("/"),
        "log_dir": c.get("log_dir", "").strip().rstrip("/"),
        "exclude_dirs": c.get("exclude_dirs", "").split(),
    }
    for key in ("source", "destination", "log_dir"):
        if not cfg[key]:
            sys.exit(f"'{key}' is empty in {path}")
    return cfg


def run_cmd(cmd, stdin_text=None):
    return subprocess.run(cmd, input=stdin_text, capture_output=True, text=True,
                          errors="replace")


# ---------------------------------------------------------------- logging

def rows_to_csv(rows, with_header):
    buf = io.StringIO()
    w = csv.writer(buf)
    if with_header:
        w.writerow(CSV_HEADER)
    w.writerows(rows)
    return buf.getvalue()


def append_local(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="") as f:
        f.write(rows_to_csv(rows, new))


def write_log(cfg, rows, date_tag):
    """Append rows to today's log (kept locally), then copy that file to the cluster.

    The cluster's data node only allows file transfers, so the whole day's log file
    is sent with rsync rather than appended to remotely."""
    name = f"backuplog.{date_tag}.csv"
    local = STATE_DIR / "logs" / name
    append_local(local, rows)                                # full log for the day lives here

    if not cfg["ssh_host"]:                                  # source is local: log is local too
        append_local(Path(cfg["log_dir"]) / name, rows)
        return True

    r = run_cmd(["rsync", "-t", "-s", "-e", "ssh " + " ".join(SSH_OPTS),
                 str(local), f"{cfg['ssh_host']}:{cfg['log_dir']}/"])
    if r.returncode != 0:
        print(f"WARNING: could not copy the log to the cluster ({r.stderr.strip()}). "
              f"Local copy: {local}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------- checks

def nas_is_mounted(cfg):
    mp = cfg["mount_point"]
    if not mp:                       # no mount point configured: skip the check
        return True
    return os.path.ismount(mp)


def cluster_reachable(cfg):
    if not cfg["ssh_host"]:
        return os.path.isdir(cfg["source"]), "source folder not found"
    # The data node only allows file transfers, so check the folder by listing it with rsync.
    r = run_cmd(["rsync", "--list-only", "-s", "-e", "ssh " + " ".join(SSH_OPTS),
                 f"{cfg['ssh_host']}:{cfg['source']}/"])
    if r.returncode == 0:
        return True, ""
    err = r.stderr.strip()
    if "No such file or directory" in err:
        return False, f"source folder not found on cluster: {cfg['source']}"
    return False, f"could not reach the cluster: {err[:300]}"


# ---------------------------------------------------------------- rsync output parsing

def error_path_to_rel(p, cfg):
    """Turn a path from an rsync error message into a path relative to the source."""
    for base in (cfg["source"], cfg["destination"]):
        b = base + "/"
        if p.startswith(b):
            p = p[len(b):]
            break
    d, n = os.path.split(p)
    m = re.match(r"^\.(.+)\.[A-Za-z0-9]{6}$", n)      # rsync temp file: .name.XXXXXX
    if m:
        n = m.group(1)
    return os.path.join(d, n) if d else n


def parse_errors(stderr, cfg):
    """Return (per-file failures, general error lines)."""
    failures, general = [], []
    for line in stderr.splitlines():
        line = line.strip()
        if not line or line.startswith(("rsync error:", "rsync warning:")) \
                or "skipping non-regular file" in line:
            continue
        if line.startswith("file has vanished:"):
            m = re.search(r'"(.*)"', line)
            path = error_path_to_rel(m.group(1), cfg) if m else ""
            failures.append((path, "file disappeared from the cluster during the backup"))
            continue
        m = re.match(r'^rsync:\s*(?:\[\w+\]\s*)?(.*?)\s*"(.*)"\s*(?:failed)?\s*:?\s*(.*)$', line)
        if m:
            action, path, err = m.groups()
            if "[sender]" in line or "send_files" in action:
                reason = f"could not read it on the cluster: {err or action}"
            elif "mkstemp" in action or "rename" in action:
                reason = (f"could not write it to the NAS (the name may contain a character "
                          f"the NAS does not allow, e.g. : * ? \" < > | \\): {err or action}")
            else:
                reason = f"{action} {err}".strip()
            failures.append((error_path_to_rel(path, cfg), reason))
        else:
            general.append(line)
    return failures, general


# ---------------------------------------------------------------- versions

def keep_old_version(dest_file, tmp_suffix, date_tag, start):
    """Rename rsync's backup copy (file + tmp_suffix) to file.YYYY_MM_DD (unique)."""
    tmp = Path(str(dest_file) + tmp_suffix)
    if not tmp.exists():
        return None
    candidates = [f"{dest_file}.{date_tag}",
                  f"{dest_file}.{date_tag}_{start:%H%M}",
                  f"{dest_file}.{date_tag}_{start:%H%M%S}"]
    target = next((c for c in candidates if not os.path.exists(c)), None)
    i = 2
    while target is None:
        c = f"{dest_file}.{date_tag}_{start:%H%M%S}_{i}"
        target = c if not os.path.exists(c) else None
        i += 1
    os.rename(tmp, target)
    return os.path.basename(target)


# ---------------------------------------------------------------- main backup

def do_backup(cfg, start, dry_run):
    date_tag = start.strftime("%Y_%m_%d")
    stamp = start.strftime("%Y-%m-%d %H:%M:%S")
    tmp_suffix = f".__old_{start:%Y_%m_%d_%H%M%S}"   # temporary name for replaced copies
    rows = []

    os.makedirs(cfg["destination"], exist_ok=True)
    src = f"{cfg['ssh_host']}:{cfg['source']}/" if cfg["ssh_host"] else f"{cfg['source']}/"

    cmd = ["rsync",
           "-rt",                       # recursive + keep modification times
           "-s",                        # paths with spaces etc. are passed safely
           "-8",                        # don't escape non-ASCII names in output
           "--no-links",                # skip symlinks
           "--no-perms", "--no-owner", "--no-group",   # the NAS can't keep these
           "--modify-window=2",         # NAS timestamps are less precise than Linux
           "--timeout=900",
           "--backup", f"--suffix={tmp_suffix}",       # keep the old copy of changed files
           "--out-format=%i %l %n",
           "-e", "ssh " + " ".join(SSH_OPTS)]
    for folder in cfg["exclude_dirs"]:
        folder = folder.strip("/")
        if folder:
            cmd.append(f"--exclude=/{folder}/")

    if dry_run:
        cmd.append("--dry-run")
    cmd += [src, cfg["destination"] + "/"]

    proc = run_cmd(cmd)

    for line in proc.stdout.splitlines():
        m = ITEM_RE.match(line)
        if not m:
            continue
        code, length, name = m.groups()
        kind, change = code[1], code[0]
        if kind == "d":
            if "+++" in code:
                rows.append([stamp, "new directory", name.rstrip("/"), "", "", ""])
            continue
        if kind != "f" or change not in "<>c":
            continue                                   # attribute-only changes etc.
        size = int(length)
        if "+++" in code:
            rows.append([stamp, "new", name, human_size(size), size, ""])
        else:
            note = ""
            if not dry_run:
                kept = keep_old_version(Path(cfg["destination"]) / name, tmp_suffix,
                                        date_tag, start)
                note = f"old version kept as {kept}" if kept else ""
            else:
                note = "old version would be kept"
            rows.append([stamp, "updated - multiple versions present", name,
                         human_size(size), size, note])

    failures, general = parse_errors(proc.stderr, cfg)
    for path, reason in failures:
        rows.append([stamp, "failed", path, "", "", reason])

    # 0 = ok, 23 = some files failed, 24 = some files vanished: per-file rows cover these
    if proc.returncode not in (0, 23, 24):
        detail = " | ".join(general[-5:]) or proc.stderr.strip()[-500:]
        rows.append([stamp, "run failed", "", "", "",
                     f"rsync exit code {proc.returncode}: {detail}"])

    if not rows:
        rows.append([stamp, "no changes", "", "", "", "nothing new or changed to back up"])
    return rows


def main():
    ap = argparse.ArgumentParser(description="Back up the cluster folder to the lab NAS.")
    ap.add_argument("mode", choices=["now", "scheduled"])
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be copied; copy nothing and write no log")
    ap.add_argument("--config", default=str(SCRIPT_DIR / "backup_config.ini"))
    args = ap.parse_args()

    cfg = load_config(args.config)
    start = dt.datetime.now()
    date_tag = start.strftime("%Y_%m_%d")
    stamp = start.strftime("%Y-%m-%d %H:%M:%S")
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    def finish(rows):
        if args.dry_run:
            print(rows_to_csv(rows, True), end="")
            return
        write_log(cfg, rows, date_tag)
        counts = {}
        for r in rows:
            counts[r[1]] = counts.get(r[1], 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in counts.items())
        print(f"[{stamp}] {args.mode}: {summary}")

    # Only one backup at a time
    lock = open(STATE_DIR / "backup.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        finish([[stamp, "skipped", "", "", "", "another backup was already running"]])
        return 1

    if not nas_is_mounted(cfg):
        finish([[stamp, "not mounted", "", "", "",
                 f"McGaughey-Lab was not mounted at {cfg['mount_point']}; nothing copied"]])
        return 1

    ok, why = cluster_reachable(cfg)
    if not ok:
        finish([[stamp, "run failed", "", "", "", why]])
        return 1

    rows = do_backup(cfg, start, args.dry_run)
    finish(rows)
    return 0 if not any(r[1] in ("failed", "run failed") for r in rows) else 2


if __name__ == "__main__":
    sys.exit(main())
