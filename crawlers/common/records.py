"""Shared record helpers for the YouTube, Facebook, TikTok, and News crawlers.

Additive by design: the YouTube and Facebook crawlers keep their own inline
helpers and are deliberately NOT refactored onto this module -- the platform
workers keep their platform-specific capture logic here. TikTok and News
(added when the two crawler sets were merged into one repo) use these
helpers directly.

Conventions encoded here, so every new crawler agrees on them:

- timestamps are Asia/Ho_Chi_Minh with an explicit offset (`now_hcm`)
- batch ids are ``<prefix>_<YYYYMMDD_HHMM>`` (`batch_id`)
- record ids are ``<prefix>_<sha1(parts)[:16]>`` — deterministic, so re-running
  the same source produces the same ids and output stays idempotent
- author identifiers are salted-hashed, never written raw (`salted_hash`)
- output is append-only JSONL, flushed per record, deduplicated in-run
  (`JsonlWriter`)
"""

import hashlib
import json
import os
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

try:
    # Loads .env into os.environ on import, so secrets like FB_HANDLE_SALT,
    # TIKTOK_HANDLE_SALT, and NEWS_HANDLE_SALT only need to be set once in
    # .env rather than re-exported every terminal
    # session. Never overrides a variable already set in the environment.
    #
    # The path is resolved explicitly from this file's location (repo root is
    # two levels up from crawlers/common/) rather than left to load_dotenv()'s
    # default stack-frame search, which finds the wrong location depending on
    # how Python was invoked (e.g. `python -c`, a different cwd) and fails
    # silently rather than raising.
    from dotenv import load_dotenv

    _ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")
    load_dotenv(_ENV_PATH)
except ImportError:  # pragma: no cover - keeps stdlib-only unit tests importable
    pass

HCM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
ID_HASH_LENGTH = 16


def now_hcm():
    """Current time in Asia/Ho_Chi_Minh, timezone-aware."""
    return datetime.now(HCM_TZ)


def batch_id(prefix, moment=None):
    """``<prefix>_<YYYYMMDD_HHMM>``, generated once per run."""
    moment = moment or now_hcm()
    return f"{prefix}_{moment.strftime('%Y%m%d_%H%M')}"


def record_id(prefix, *parts):
    """Deterministic record id: ``<prefix>_<sha1('|'.join(parts))[:16]>``.

    Parts are joined with ``|`` so that ("a|b", "c") and ("a", "b|c") are not
    silently the same key.
    """
    raw = "|".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:ID_HASH_LENGTH]
    return f"{prefix}_{digest}"


def salted_hash(value, salt):
    """``sha1(salt + normalized(value))`` — pseudonymizes author handles.

    The salt lives in an environment variable and is never committed, so the
    hashes cannot be reversed by dictionary attack from the repo alone. Same
    handle always maps to the same hash, which keeps repeat-commenter grouping
    and spam detection working.
    """
    if not salt:
        raise ValueError("salted_hash requires a non-empty salt")
    normalized = unicodedata.normalize("NFC", str(value or "")).strip().casefold()
    return hashlib.sha1((salt + normalized).encode("utf-8")).hexdigest()


def require_env_salt(env_name):
    """Read a required salt from the environment with an actionable error."""
    salt = os.environ.get(env_name, "").strip()
    if not salt:
        raise RuntimeError(
            f"{env_name} is not set. Add it to .env (gitignored) or export it; "
            "it salts the author hashes and must stay out of the repo."
        )
    return salt


def iso_utc_from_epoch(seconds):
    """Unix seconds -> ISO 8601 UTC, e.g. ``2026-07-22T14:03:00+00:00``.

    Returns ``""`` for missing or unparseable values rather than raising: a
    record with an empty timestamp is recoverable, a crashed run is not.
    """
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


class JsonlWriter:
    """Append-only JSONL writer owning the in-run ``written_ids`` set.

    Records are flushed immediately, so an interrupt or a block never loses
    what was already collected.
    """

    def __init__(self, path, id_field="id"):
        self.path = path
        self.id_field = id_field
        self.written_ids = set()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._handle = open(path, "a", encoding="utf-8")

    def write(self, record):
        """Write one record. Returns False if its id was already written."""
        key = record.get(self.id_field)
        if key in self.written_ids:
            return False
        self.written_ids.add(key)
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()
        return True

    @property
    def count(self):
        return len(self.written_ids)

    def close(self):
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False
