"""
backup_archive.py — nightly offsite backup of the bot's irreplaceable data.

The graded-picks archive, backtest reports, and state files live on a single
Fly volume. Fly's automatic snapshots (retention raised to 60d, 2026-07-09)
cover volume loss; this module covers everything snapshots don't — app/org
deletion, region incidents, fat-fingered `fly volumes destroy`.

Uploads one tar.gz per day to any S3-compatible bucket (Cloudflare R2, AWS
S3, Backblaze B2) using a minimal SigV4 signer — no boto3, no new deps.

Configuration (Fly secrets; nothing set = backup silently skipped).
Two naming conventions are honoured, explicit BACKUP_S3_* first:

    BACKUP_S3_ENDPOINT / _BUCKET / _KEY_ID / _SECRET / _REGION
        — point backups anywhere S3-compatible (R2, B2, AWS).
    AWS_ENDPOINT_URL_S3, BUCKET_NAME, AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY, AWS_REGION
        — exactly what `fly storage create` (Tigris) stamps on the app,
          so Fly-provisioned storage works with zero extra setup.
    BACKUP_RETENTION_DAYS  rolling window, default 60; 0 disables pruning.

Scheduled from telegram_bot at 4:00 AM ET (after the 3 AM daily backtest),
so each upload contains that night's freshly graded results.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import os
import tarfile
import tempfile
import urllib.parse
import urllib.request


def _cfg() -> dict | None:
    """Read backup config from env; None when not configured.
    BACKUP_S3_* wins; the Tigris/AWS names are the fallback."""
    env = os.environ.get
    endpoint = (env("BACKUP_S3_ENDPOINT")
                or env("AWS_ENDPOINT_URL_S3", "")).rstrip("/")
    bucket   = env("BACKUP_S3_BUCKET")  or env("BUCKET_NAME", "")
    key_id   = env("BACKUP_S3_KEY_ID")  or env("AWS_ACCESS_KEY_ID", "")
    secret   = env("BACKUP_S3_SECRET")  or env("AWS_SECRET_ACCESS_KEY", "")
    if not (endpoint and bucket and key_id and secret):
        return None
    return {
        "endpoint": endpoint,
        "bucket":   bucket,
        "key_id":   key_id,
        "secret":   secret,
        "region":   env("BACKUP_S3_REGION") or env("AWS_REGION", "auto"),
    }


def build_backup_tar(paths: list[str], out_path: str) -> int:
    """tar.gz every existing path in `paths` into out_path. Returns the
    number of files added (0 = nothing to back up)."""
    added = 0
    with tarfile.open(out_path, "w:gz") as tf:
        for p in paths:
            if not os.path.exists(p):
                continue
            arcname = os.path.basename(p.rstrip("/"))
            tf.add(p, arcname=arcname)
            if os.path.isdir(p):
                added += sum(len(files) for _, _, files in os.walk(p))
            else:
                added += 1
    return added


# ── Minimal AWS SigV4 for a single PutObject ──────────────────────────
# Reference: docs.aws.amazon.com/general/latest/gr/sigv4-create-signed-request.html
# Only what one PUT needs — path-style URL, payload hash, host+date headers.

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(cfg: dict, method: str, key: str, payload_hash: str,
                  now: _dt.datetime | None = None) -> dict:
    """Build the signed headers for <method> /<bucket>/<key>."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    host = urllib.parse.urlparse(cfg["endpoint"]).netloc
    canonical_uri = f"/{cfg['bucket']}/{urllib.parse.quote(key)}"

    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_headers = (f"host:{host}\n"
                         f"x-amz-content-sha256:{payload_hash}\n"
                         f"x-amz-date:{amz_date}\n")
    canonical_request = "\n".join([
        method, canonical_uri, "", canonical_headers, signed_headers,
        payload_hash,
    ])

    scope = f"{datestamp}/{cfg['region']}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    k = _sign(("AWS4" + cfg["secret"]).encode(), datestamp)
    k = _sign(k, cfg["region"])
    k = _sign(k, "s3")
    k = _sign(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode(),
                         hashlib.sha256).hexdigest()

    auth = (f"AWS4-HMAC-SHA256 Credential={cfg['key_id']}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")
    return {
        "Authorization":        auth,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date":           amz_date,
    }


def sigv4_put_headers(cfg: dict, key: str, payload_hash: str,
                      now: _dt.datetime | None = None) -> dict:
    """Back-compat wrapper — the original PUT-only signer."""
    return sigv4_headers(cfg, "PUT", key, payload_hash, now=now)


_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def upload_file(cfg: dict, local_path: str, key: str) -> None:
    """PUT one file to the bucket. Raises on any non-2xx response."""
    with open(local_path, "rb") as f:
        body = f.read()
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = sigv4_headers(cfg, "PUT", key, payload_hash)
    url = f"{cfg['endpoint']}/{cfg['bucket']}/{urllib.parse.quote(key)}"
    req = urllib.request.Request(url, data=body, method="PUT",
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        if not (200 <= resp.status < 300):
            raise RuntimeError(f"upload HTTP {resp.status}")


def delete_file(cfg: dict, key: str) -> None:
    """DELETE one object. S3 DELETE is idempotent — deleting a key that
    doesn't exist succeeds — so retention pruning never needs a LIST."""
    headers = sigv4_headers(cfg, "DELETE", key, _EMPTY_SHA256)
    url = f"{cfg['endpoint']}/{cfg['bucket']}/{urllib.parse.quote(key)}"
    req = urllib.request.Request(url, method="DELETE", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        if not (200 <= resp.status < 300):
            raise RuntimeError(f"delete HTTP {resp.status}")


def run_backup(output_dir: str | None = None,
               data_dir: str | None = None) -> dict:
    """Tar the archive + reports + state and upload. Never raises.

    Returns {"ok": bool, ...} — "skipped" key set when unconfigured, so
    the caller can distinguish "not set up" from a real failure.
    """
    cfg = _cfg()
    if cfg is None:
        return {"ok": False, "skipped": "BACKUP_S3_* not configured"}

    output_dir = output_dir or os.environ.get("OUTPUT_DIR", "/data/output")
    data_dir   = data_dir or os.environ.get("DATA_DIR", "/data/state")
    paths = [
        os.path.join(output_dir, "archive"),
        os.path.join(output_dir, "Backtest Archives"),
        data_dir,
    ]
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    key = f"arbyx-backup-{today}.tar.gz"

    try:
        with tempfile.TemporaryDirectory() as td:
            tar_path = os.path.join(td, key)
            n = build_backup_tar(paths, tar_path)
            if n == 0:
                return {"ok": False, "error": "nothing to back up"}
            size = os.path.getsize(tar_path)
            upload_file(cfg, tar_path, key)
        result = {"ok": True, "key": key, "files": n, "bytes": size}
    except Exception as e:                      # noqa: BLE001 — never crash the bot
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Retention: after a successful upload, prune the backup from
    # `retention` days ago so the bucket holds a bounded rolling window
    # (~retention × tarball size). Prune failures are reported but never
    # fail the backup — today's upload is the thing that matters.
    try:
        retention = int(os.environ.get("BACKUP_RETENTION_DAYS", "60"))
        if retention > 0:
            old = (_dt.datetime.now(_dt.timezone.utc)
                   - _dt.timedelta(days=retention)).strftime("%Y-%m-%d")
            delete_file(cfg, f"arbyx-backup-{old}.tar.gz")
            result["pruned"] = f"arbyx-backup-{old}.tar.gz"
    except Exception as e:                      # noqa: BLE001
        result["prune_error"] = f"{type(e).__name__}: {e}"
    return result
