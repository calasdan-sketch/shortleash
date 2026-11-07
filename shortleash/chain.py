"""A tamper-evident receipt log, and the command that checks it.

``--receipt`` writes one JSON file per run: nothing says it is the file the
run wrote. ``--chain PATH`` appends each receipt to a JSON-lines log in
which every record carries the SHA-256 of the record before it, so an
auditor can run ``shortleash verify PATH`` and know whether a receipt was
edited, removed from the middle, inserted or reordered since it was written.

    {"seq": 3, "prev": "<hash of record 2>", "receipt": {...},
     "hash": "<sha256 of seq, prev and receipt>", "mac": "<optional>"}

What this proves, and what it does not:

- An edit, deletion, insertion or reordering anywhere but at the very end
  breaks the chain: ``verify`` names the first record that does not match.
- Dropping the LAST records leaves a valid, shorter chain. Write down the
  head hash that ``verify`` prints (or ``--chain`` prints at the end of a
  run) somewhere the log's writer cannot reach, and pass it back with
  ``--expect-head``: a shorter or different chain then fails.
- Hashes alone do not stop someone who can write the file from rewriting the
  WHOLE chain consistently. Set ``SHORTLEASH_CHAIN_KEY`` (or the variable you
  name with ``--chain-key-env``) to a secret kept away from the agent and
  each record also carries an HMAC-SHA256 over its hash; a chain rewritten
  without the key then fails ``verify --key-env``. Anyone who has the key can
  forge, and the receipt's contents are only as true as the run's telemetry
  (see the trust boundary in the README).
- It says nothing about a run that wrote no record, or about when a record
  was written (the receipt's own timestamps are the agent host's clock).

Appends take an exclusive lock on ``PATH.lock`` so concurrent runs extend one
chain instead of forking it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

GENESIS = "0" * 64
#: A record larger than this is refused, so a log cannot be made to eat
#: memory (a receipt is a few KB).
MAX_RECORD = 1024 * 1024
LOCK_WAIT = 30.0


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _hash(seq: int, prev: str, receipt) -> str:
    return hashlib.sha256(_canon({"seq": seq, "prev": prev,
                                  "receipt": receipt})).hexdigest()


def _mac(key: bytes, digest: str) -> str:
    return hmac.new(key, digest.encode("ascii"), hashlib.sha256).hexdigest()


class _Lock:
    """An exclusive lock on a side file: flock on POSIX, msvcrt on Windows."""

    def __init__(self, path: str):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                self._try()
                return self
            except OSError:
                if time.monotonic() > deadline:
                    os.close(self.fd)
                    raise OSError(f"could not lock {self.path} within "
                                  f"{LOCK_WAIT:g}s") from None
                time.sleep(0.02)

    def _try(self):
        if os.name == "nt":
            import msvcrt
            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def _tail(path: str) -> tuple[int, str]:
    """(next seq, hash of the last record) of an existing chain, reading
    only its end. A log whose last line cannot be read is an error: it must
    be looked at, not silently extended."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return 0, GENESIS
            f.seek(max(0, size - MAX_RECORD - 1))
            data = f.read()
    except FileNotFoundError:
        return 0, GENESIS
    lines = [ln for ln in data.split(b"\n") if ln.strip()]
    if not lines or not data.endswith(b"\n"):
        raise OSError(f"{path}: the last record is incomplete; run "
                      f"`shortleash verify` before extending it")
    try:
        rec = json.loads(lines[-1].decode("utf-8"))
        return int(rec["seq"]) + 1, str(rec["hash"])
    except (ValueError, KeyError, TypeError):
        raise OSError(f"{path}: the last record cannot be read; run "
                      f"`shortleash verify` before extending it") from None


def prepare(path: str) -> None:
    """Fail early (before a run) when the chain cannot be extended."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with _Lock(path + ".lock"):
        _tail(path)


def append(path: str, receipt: dict, key: bytes | None = None) -> str:
    """Add ``receipt`` to the chain at ``path``; returns the new head hash.
    Raises OSError when it cannot be written (or the log's end is damaged)
    and ValueError when the receipt cannot be serialised."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with _Lock(path + ".lock"):
        seq, prev = _tail(path)
        digest = _hash(seq, prev, receipt)
        rec = {"seq": seq, "prev": prev, "receipt": receipt, "hash": digest}
        if key:
            rec["mac"] = _mac(key, digest)
        line = json.dumps(rec, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False) + "\n"
        if len(line) > MAX_RECORD:
            raise ValueError("the receipt is too large for the chain")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
    return digest


def verify(path: str, key: bytes | None = None,
           expect_head: str | None = None) -> dict:
    """Check a chain. Returns {"ok", "records", "head", "problems",
    "mac_checked"}; ``problems`` names the first break (and each later one
    that is independent of it), never the receipt contents."""
    problems: list[str] = []
    prev = GENESIS
    n = 0
    head = GENESIS
    macs = 0
    try:
        f = open(path, "rb")
    except OSError as e:
        return {"ok": False, "records": 0, "head": GENESIS, "mac_checked": 0,
                "problems": [f"cannot read {path}: {e.strerror or e}"]}
    with f:
        lineno = 0
        while True:
            raw = f.readline(MAX_RECORD + 2)  # never hold an endless line
            if not raw:
                break
            lineno += 1
            if len(raw) > MAX_RECORD:
                problems.append(f"line {lineno}: record too large")
                break
            if not raw.strip():
                continue
            if not raw.endswith(b"\n"):
                problems.append(f"line {lineno}: incomplete last record "
                                f"(cut short or still being written)")
                break
            try:
                rec = json.loads(raw.decode("utf-8"))
                seq, p, h = rec["seq"], rec["prev"], rec["hash"]
                receipt = rec["receipt"]
                if (not isinstance(seq, int) or isinstance(seq, bool)
                        or not isinstance(p, str) or not isinstance(h, str)):
                    raise TypeError
            except (ValueError, KeyError, TypeError, RecursionError):
                problems.append(f"line {lineno}: not a chain record")
                break
            if seq != n:
                problems.append(f"line {lineno}: record number is {seq}, "
                                f"expected {n} (a record was removed, "
                                f"inserted or moved)")
            if p != prev:
                problems.append(f"line {lineno}: record {seq} does not "
                                f"follow the record before it (the chain "
                                f"is broken here)")
            try:
                good = _hash(seq, p, receipt)
            except (ValueError, TypeError, RecursionError):
                problems.append(f"line {lineno}: record {seq} cannot be "
                                f"hashed")
                break
            if good != h:
                problems.append(f"line {lineno}: record {seq} was changed "
                                f"after it was written (hash mismatch)")
            if key is not None:
                m = rec.get("mac")
                if not isinstance(m, str) or not hmac.compare_digest(
                        m, _mac(key, h)):
                    problems.append(f"line {lineno}: record {seq} has no "
                                    f"valid MAC for the key given")
                else:
                    macs += 1
            prev = h
            head = h
            n += 1
    if expect_head is not None and head != expect_head.strip().lower():
        problems.append(f"the chain ends at {head}, not at the expected "
                        f"head (records may have been cut off the end, or "
                        f"added)")
    return {"ok": not problems, "records": n, "head": head,
            "problems": problems, "mac_checked": macs}
