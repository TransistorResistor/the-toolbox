#!/usr/bin/env python3
"""
verify_safetensors.py

Pre-flight verifier for mirrored safetensors models. Works in three modes:

  1. Header/size check  - for each .safetensors URL, download only the header
                          (8-byte prefix + JSON) and confirm the server's file
                          size equals 8 + header_len + max(data_offsets end).
  2. Index cross-check  - for each model.safetensors.index.json found, confirm
                          every shard in weight_map exists, every shard's header
                          contains exactly the tensors the index assigns to it,
                          and the summed data regions equal metadata.total_size.
  3. Recursive crawl    - walk a standard autoindex directory tree (Apache/nginx
                          style HTML with <a href> links, including a parent-dir
                          link), discover every model, and verify each.

Only headers are downloaded, never tensor data. A PASS means structural
completeness (no truncation, no size mismatch, index consistent). It does NOT
prove tensor bytes are bit-for-bit correct; use --manifest for that.

Usage:
    # Recursively crawl a directory tree and verify every model found:
    python verify_safetensors.py --crawl https://mirror.example/models/

    # Check explicit URLs (files or index.json):
    python verify_safetensors.py URL [URL ...]

    # Use the hard-coded FILE_LISTS below:
    python verify_safetensors.py

    # Also compare against a checksum manifest (see --manifest help):
    python verify_safetensors.py --crawl URL --manifest sha256sums.txt

Options:
    --crawl URL       Recursively crawl an autoindex directory tree.
    --manifest FILE   Verify SHA256s from a manifest (requires full download).
    --json            Machine-readable output.
    --max-depth N     Crawl depth limit (default 8).
    --timeout N       Per-request timeout in seconds (default 30).
    --workers N       Concurrent header checks (default 8).
"""

import argparse
import concurrent.futures as cf
import hashlib
import json
import re
import struct
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, unquote

import requests

# ---------------------------------------------------------------------------
# HARD-CODED FILE LISTS (used when no URLs / --crawl are given)
# Each entry: (base_url_ending_in_slash, [filenames]). Absolute URLs override base.
# ---------------------------------------------------------------------------
FILE_LISTS = [
    # ("https://mirror.example/org/model/resolve/main/",
    #  ["model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors",
    #   "model.safetensors.index.json"]),
]

INITIAL_FETCH = 1 << 20          # 1 MiB first grab; covers most headers in one request
MAX_HEADER = 500 << 20           # sanity bound on header length
TIMEOUT = 30
WORKERS = 8
MAX_DEPTH = 8

SAFETENSORS_RE = re.compile(r"\.safetensors$", re.IGNORECASE)
INDEX_RE = re.compile(r"\.safetensors\.index\.json$", re.IGNORECASE)


# ===========================================================================
# Progress reporting (stderr, TTY-aware so logs/pipes stay clean)
# ===========================================================================
class Progress:
    """Lightweight status writer.

    All output goes to stderr so stdout (esp. --json) stays machine-clean.
    On a TTY it draws a single updating line with \\r; when redirected it
    emits plain newline-terminated lines at a throttled cadence. Disable
    entirely with enabled=False (e.g. --quiet or --json without a TTY).
    """
    def __init__(self, enabled=True, min_interval=0.1):
        self.enabled = enabled
        self.is_tty = sys.stderr.isatty()
        self.min_interval = min_interval
        self._last = 0.0
        self._open_line = False

    def _write(self, text, transient):
        if not self.enabled:
            return
        now = time.time()
        # Throttle transient updates so we don't spam; always allow finals.
        if transient and (now - self._last) < self.min_interval:
            return
        self._last = now
        if self.is_tty:
            sys.stderr.write("\r\033[K" + text)
            if not transient:
                sys.stderr.write("\n")
                self._open_line = False
            else:
                self._open_line = True
        else:
            if transient:
                return  # skip noisy intermediate lines when not a TTY
            sys.stderr.write(text + "\n")
        sys.stderr.flush()

    def update(self, text):
        """Transient in-place status (TTY only)."""
        self._write(text, transient=True)

    def line(self, text):
        """A committed status line (shown on TTY and in logs)."""
        if self._open_line and self.is_tty:
            sys.stderr.write("\r\033[K")
            self._open_line = False
        self._write(text, transient=False)

    def clear(self):
        if self.enabled and self.is_tty and self._open_line:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
            self._open_line = False


def _fmt_bytes(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:,.1f}{unit}"
        n /= 1024.0


# ===========================================================================
# HTTP helpers
# ===========================================================================
def make_session(trust_env=True):
    s = requests.Session()
    s.headers.update({"User-Agent": "safetensors-preflight/2.0"})
    # trust_env=False bypasses HTTP(S)_PROXY / NO_PROXY handling; useful when a
    # local or intranet mirror shouldn't go through a configured proxy.
    s.trust_env = trust_env
    return s


def get_full_size(session, url):
    """Return (total_size or None, accepts_ranges_str)."""
    try:
        r = session.head(url, timeout=TIMEOUT, allow_redirects=True)
        cl = r.headers.get("Content-Length")
        if r.ok and cl is not None:
            return int(cl), r.headers.get("Accept-Ranges", "unknown")
    except requests.RequestException:
        pass
    try:
        r = session.get(url, headers={"Range": "bytes=0-0"},
                        timeout=TIMEOUT, allow_redirects=True)
        cr = r.headers.get("Content-Range")
        if cr and "/" in cr:
            total = cr.rsplit("/", 1)[1].strip()
            if total.isdigit():
                return int(total), ("bytes" if r.status_code == 206 else "no")
        cl = r.headers.get("Content-Length")
        if cl is not None and r.status_code == 200:
            return int(cl), "no"
    except requests.RequestException:
        pass
    return None, "unknown"


def fetch_bytes(session, url, start, end):
    r = session.get(url, headers={"Range": f"bytes={start}-{end}"},
                    timeout=TIMEOUT, allow_redirects=True)
    if r.status_code in (200, 206):
        return r.content
    return None


def fetch_text(session, url):
    r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    return r.text


# ===========================================================================
# safetensors header parsing
# ===========================================================================
def read_header(session, url):
    """Return (header_len, header_dict), downloading only the header."""
    first = fetch_bytes(session, url, 0, INITIAL_FETCH - 1)
    if first is None or len(first) < 8:
        raise ValueError("could not read first 8 bytes")
    (hlen,) = struct.unpack("<Q", first[:8])
    if hlen <= 0 or hlen > MAX_HEADER:
        raise ValueError(f"implausible header length {hlen}")
    end = 8 + hlen
    if len(first) >= end:
        raw = first[8:end]
    else:
        rest = fetch_bytes(session, url, len(first), end - 1)
        if rest is None:
            raise ValueError("could not read full header")
        raw = (first + rest)[8:end]
    try:
        header = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"header JSON invalid: {e}")
    return hlen, header


def header_stats(hlen, header):
    """Return (expected_total_size, tensor_names_set, data_region_bytes)."""
    max_end = 0
    names = set()
    for name, meta in header.items():
        if name == "__metadata__":
            continue
        if not isinstance(meta, dict) or "data_offsets" not in meta:
            raise ValueError(f"tensor {name!r} missing data_offsets")
        start, end_ = meta["data_offsets"]
        if end_ < start or start < 0:
            raise ValueError(f"tensor {name!r} bad offsets {start},{end_}")
        max_end = max(max_end, end_)
        names.add(name)
    return 8 + hlen + max_end, names, max_end


# ===========================================================================
# Single-file check
# ===========================================================================
def check_file(session, url):
    res = {"url": url, "kind": "file", "status": "ERROR",
           "expected": None, "actual": None, "tensors": None,
           "accepts_ranges": None, "detail": ""}
    try:
        actual, accepts = get_full_size(session, url)
        res["accepts_ranges"] = accepts
        if actual is None:
            res["detail"] = "server did not report a file size"
            return res
        res["actual"] = actual
        hlen, header = read_header(session, url)
        expected, names, _ = header_stats(hlen, header)
        res["expected"] = expected
        res["tensors"] = len(names)
        if expected == actual:
            res["status"] = "PASS"
            res["detail"] = f"{len(names)} tensors, {expected:,} bytes"
        else:
            res["status"] = "FAIL"
            res["detail"] = (f"size mismatch: header expects {expected:,}, "
                             f"server has {actual:,} (diff {actual - expected:+,})")
    except requests.RequestException as e:
        res["detail"] = f"request failed: {e}"
    except ValueError as e:
        res["detail"] = str(e)
    return res


# ===========================================================================
# Index cross-check
# ===========================================================================
def check_index(session, index_url):
    """Verify a model.safetensors.index.json and all its shards."""
    res = {"url": index_url, "kind": "index", "status": "ERROR",
           "shards": {}, "detail": ""}
    try:
        index = json.loads(fetch_text(session, index_url))
    except (requests.RequestException, json.JSONDecodeError) as e:
        res["detail"] = f"could not load index: {e}"
        return res

    weight_map = index.get("weight_map")
    if not isinstance(weight_map, dict):
        res["detail"] = "index has no weight_map"
        return res

    total_size_declared = index.get("metadata", {}).get("total_size")

    # Group expected tensor names by shard filename.
    shard_expect = {}
    for tname, fname in weight_map.items():
        shard_expect.setdefault(fname, set()).add(tname)

    base = index_url.rsplit("/", 1)[0] + "/"
    all_ok = True
    total_data_bytes = 0

    for fname, expected_names in sorted(shard_expect.items()):
        shard_url = urljoin(base, fname)
        entry = {"status": "ERROR", "detail": ""}
        try:
            actual, _ = get_full_size(session, shard_url)
            if actual is None:
                entry["detail"] = "missing / no size reported"
                all_ok = False
                res["shards"][fname] = entry
                continue
            hlen, header = read_header(session, shard_url)
            expected_size, names, data_bytes = header_stats(hlen, header)
            total_data_bytes += data_bytes

            problems = []
            if expected_size != actual:
                problems.append(f"size {actual:,} != header {expected_size:,}")
            missing = expected_names - names
            extra = names - expected_names
            if missing:
                problems.append(f"{len(missing)} tensors in index but not in shard")
            if extra:
                problems.append(f"{len(extra)} tensors in shard but not in index")
            if problems:
                entry["status"] = "FAIL"
                entry["detail"] = "; ".join(problems)
                all_ok = False
            else:
                entry["status"] = "PASS"
                entry["detail"] = f"{len(names)} tensors, {actual:,} bytes"
            entry["bytes"] = actual
        except requests.RequestException as e:
            entry["detail"] = f"request failed: {e}"
            all_ok = False
        except ValueError as e:
            entry["detail"] = str(e)
            all_ok = False
        res["shards"][fname] = entry

    # Cross-check total_size if declared.
    size_note = ""
    if total_size_declared is not None:
        if total_size_declared == total_data_bytes:
            size_note = f"total_size matches ({total_data_bytes:,})"
        else:
            size_note = (f"total_size mismatch: index says {total_size_declared:,}, "
                         f"shards sum to {total_data_bytes:,}")
            all_ok = False

    n = len(shard_expect)
    n_pass = sum(s["status"] == "PASS" for s in res["shards"].values())
    res["status"] = "PASS" if all_ok else "FAIL"
    res["detail"] = f"{n_pass}/{n} shards ok" + (f"; {size_note}" if size_note else "")
    return res


# ===========================================================================
# Checksum manifest
# ===========================================================================
def parse_manifest(path):
    """Parse a 'sha256  filename' manifest (sha256sum / HF-style). Returns dict."""
    mapping = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                digest = parts[0].lower()
                name = " ".join(parts[1:]).lstrip("*")   # sha256sum marks binary with *
                mapping[name.rsplit("/", 1)[-1]] = digest
    return mapping


def verify_checksum(session, url, expected_digest, on_progress=None):
    """Stream the full file and compare SHA256. Returns (bool, actual_digest).

    on_progress(read_bytes, total_bytes) is called periodically if provided;
    total_bytes is 0 when the server doesn't report Content-Length.
    """
    h = hashlib.sha256()
    r = session.get(url, stream=True, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    total = int(r.headers.get("Content-Length", 0) or 0)
    read = 0
    for chunk in r.iter_content(chunk_size=1 << 20):
        h.update(chunk)
        read += len(chunk)
        if on_progress:
            on_progress(read, total)
    actual = h.hexdigest()
    return actual == expected_digest, actual


# ===========================================================================
# Recursive autoindex crawler
# ===========================================================================
class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.hrefs.append(v)


def is_subpath(child, parent):
    """True if child URL is at or below parent URL's path (prevents escaping up)."""
    p, c = urlparse(parent), urlparse(child)
    if p.netloc != c.netloc:
        return False
    return c.path.startswith(p.path)


def crawl(session, root, max_depth=MAX_DEPTH, verbose=True, progress=None):
    """Walk an autoindex tree from root. Returns (files, indexes) URL lists."""
    if not root.endswith("/"):
        root += "/"
    seen_dirs = set()
    files, indexes = [], []
    stack = [(root, 0)]
    dirs_scanned = 0

    while stack:
        url, depth = stack.pop()
        if url in seen_dirs or depth > max_depth:
            continue
        seen_dirs.add(url)
        try:
            html = fetch_text(session, url)
        except requests.RequestException as e:
            if verbose:
                print(f"  ! skip {url}: {e}", file=sys.stderr)
            continue
        dirs_scanned += 1

        parser = LinkParser()
        parser.feed(html)
        for href in parser.hrefs:
            if href.startswith(("?", "#")) or href.lower().startswith("mailto:"):
                continue
            target = urljoin(url, href)
            target = target.split("#", 1)[0].split("?", 1)[0]
            if not is_subpath(target, root):
                continue  # parent-dir link or external; ignore
            if target.endswith("/"):
                if target != url and target not in seen_dirs:
                    stack.append((target, depth + 1))
            else:
                decoded = unquote(target)
                if INDEX_RE.search(decoded):
                    indexes.append(target)
                elif SAFETENSORS_RE.search(decoded):
                    files.append(target)

        if progress:
            rel = url[len(root):].rstrip("/") or "."
            if len(rel) > 40:
                rel = "…" + rel[-39:]
            progress.update(f"crawling: {dirs_scanned} dirs scanned, "
                            f"{len(files)} files, {len(indexes)} indexes  "
                            f"[{len(stack)} queued]  {rel}")

    if progress:
        progress.clear()
    return sorted(set(files)), sorted(set(indexes))


# ===========================================================================
# Orchestration
# ===========================================================================
def expand_file_lists(file_lists):
    out = []
    for base, names in file_lists:
        for name in names:
            out.append(urljoin(base, name))
    return out


def shard_urls_from_index(session, index_url):
    """Return the set of shard URLs an index references (best-effort)."""
    try:
        index = json.loads(fetch_text(session, index_url))
        wm = index.get("weight_map", {})
        base = index_url.rsplit("/", 1)[0] + "/"
        return {urljoin(base, f) for f in set(wm.values())}
    except Exception:
        return set()


def model_dir(url):
    """The directory URL a file/index belongs to — used to group into models."""
    return url.rsplit("/", 1)[0] + "/"


SHARD_SUFFIX_RE = re.compile(r"-\d{4,5}-of-\d{4,5}\.safetensors$", re.IGNORECASE)


def set_stem(filename):
    """Reduce a safetensors filename to its set identity within a directory.

    'model-00001-of-00002.safetensors' -> 'model'
    'model.fp16.safetensors'           -> 'model.fp16'
    'consolidated.safetensors'         -> 'consolidated'
    So two differently-named sets in the same folder don't get merged, while
    all shards of one set collapse to the same stem.
    """
    if SHARD_SUFFIX_RE.search(filename):
        return SHARD_SUFFIX_RE.sub("", filename)
    if filename.lower().endswith(".safetensors"):
        return filename[: -len(".safetensors")]
    return filename


def index_stem(filename):
    """'model.safetensors.index.json' -> 'model' (matches its shards' stem)."""
    if filename.lower().endswith(".safetensors.index.json"):
        return filename[: -len(".safetensors.index.json")]
    return filename


def human_size(n):
    if n is None:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:,.2f} {unit}"
        n /= 1024.0


def run(urls, indexes, session, manifest=None, as_json=False, workers=WORKERS,
        progress=None):
    results = []
    if progress is None:
        progress = Progress(enabled=False)

    # Index checks first (also tells us which files are shards).
    claimed = set()
    n_idx = len(indexes)
    for i, idx in enumerate(indexes, 1):
        progress.update(f"indexes: [{i}/{n_idx}] {idx.rsplit('/', 1)[-1]}")
        t0 = time.time()
        r = check_index(session, idx)
        r["seconds"] = round(time.time() - t0, 2)
        r["model"] = model_dir(idx)
        results.append(r)
        claimed |= shard_urls_from_index(session, idx)
        if not as_json:
            progress.line(f"[{r['status']:4}] INDEX {idx.rsplit('/', 1)[-1]}  {r['detail']}")
            for fn, s in r.get("shards", {}).items():
                progress.line(f"         - {s['status']:4} {fn}  {s['detail']}")

    # Standalone files (skip ones already covered by an index).
    standalone = [u for u in urls if u not in claimed]
    total = len(standalone)
    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check_file, session, u): u for u in standalone}
        for fut in cf.as_completed(futs):
            r = fut.result()
            r["model"] = model_dir(r["url"])
            results.append(r)
            done += 1
            if not as_json:
                progress.line(f"[{r['status']:4}] {r['url'].rsplit('/', 1)[-1]}  {r['detail']}")
            progress.update(f"header checks: {done}/{total} done "
                            f"({total - done} remaining)")
    progress.clear()

    # Optional checksum verification (full download).
    if manifest:
        man = parse_manifest(manifest)
        if not as_json:
            progress.line(f"\nChecksum manifest: {len(man)} entries. "
                          f"Verifying (full download)...")
        check_targets = standalone + sorted(claimed)
        n_ck = len(check_targets)
        for i, u in enumerate(check_targets, 1):
            name = u.rsplit("/", 1)[-1]
            if name not in man:
                if not as_json:
                    progress.line(f"[SKIP] {name}  no manifest entry")
                continue

            def on_bytes(read, total_bytes, _name=name, _i=i):
                if total_bytes:
                    pct = 100.0 * read / total_bytes
                    progress.update(f"hashing [{_i}/{n_ck}] {_name}  "
                                    f"{_fmt_bytes(read)}/{_fmt_bytes(total_bytes)} "
                                    f"({pct:.0f}%)")
                else:
                    progress.update(f"hashing [{_i}/{n_ck}] {_name}  "
                                    f"{_fmt_bytes(read)}")

            try:
                ok, actual = verify_checksum(session, u, man[name],
                                             on_progress=on_bytes)
                status = "PASS" if ok else "FAIL"
                results.append({"url": u, "kind": "checksum", "status": status,
                                "model": model_dir(u),
                                "detail": "sha256 match" if ok
                                          else f"sha256 mismatch (got {actual[:12]}...)"})
                if not as_json:
                    progress.line(f"[{status:4}] SHA256 {name}")
            except requests.RequestException as e:
                if not as_json:
                    progress.line(f"[ERROR] SHA256 {name}: {e}")
        progress.clear()

    # ------------------------------------------------------------------
    # Group results into models and build the findings summary.
    # A model = one directory. It is "complete" if every check that
    # belongs to it passed (index PASS covers its shards; standalone
    # files must each PASS). Size = server-reported bytes of its files.
    # ------------------------------------------------------------------
    models = build_findings(results)

    if as_json:
        print(json.dumps({"results": results, "models": models}, indent=2))
    else:
        print_findings(models)
        npass = sum(r["status"] == "PASS" for r in results)
        nfail = sum(r["status"] == "FAIL" for r in results)
        nerr = sum(r["status"] == "ERROR" for r in results)
        print(f"\n{npass} passed, {nfail} failed, {nerr} errored, "
              f"{len(results)} checks across {len(models)} model(s).")

    return 0 if all(r["status"] == "PASS" for r in results) else 1


def build_findings(results):
    """Collapse per-file/index results into per-SET records.

    A "set" is one model within a directory, identified by (directory, stem).
    This keeps distinct sets that share a directory separate — e.g. a sharded
    'model-*' set and a single-file 'model.fp16.safetensors' in the same folder
    become two sets, not one.
    """
    sets = {}

    def get(dir_url, stem, sharded):
        key = (dir_url, stem)
        rec = sets.get(key)
        if rec is None:
            rec = {"dir": dir_url, "stem": stem, "sharded": sharded,
                   "complete": True, "bytes": 0, "n_files": 0,
                   "n_tensors": 0, "has_index": False, "issues": []}
            sets[key] = rec
        rec["sharded"] = rec["sharded"] or sharded
        return rec

    for r in results:
        d = model_dir(r["url"])
        fname = r["url"].rsplit("/", 1)[-1]

        if r["kind"] == "index":
            stem = index_stem(fname)
            rec = get(d, stem, sharded=True)
            rec["has_index"] = True
            if r["status"] != "PASS":
                rec["complete"] = False
                rec["issues"].append(f"index: {r['detail']}")
            for shard_fn, s in r.get("shards", {}).items():
                rec["n_files"] += 1
                if s.get("bytes"):
                    rec["bytes"] += s["bytes"]
                if s["status"] != "PASS":
                    rec["complete"] = False
                    rec["issues"].append(f"{shard_fn}: {s['detail']}")

        elif r["kind"] == "file":
            stem = set_stem(fname)
            is_shard = bool(SHARD_SUFFIX_RE.search(fname))
            rec = get(d, stem, sharded=is_shard)
            rec["n_files"] += 1
            if r.get("tensors"):
                rec["n_tensors"] += r["tensors"]
            if r.get("actual"):
                rec["bytes"] += r["actual"]
            if is_shard:
                # Record which shard N-of-M this is, to detect gaps later.
                mm = SHARD_SUFFIX_RE.search(fname)
                nums = re.findall(r"\d{4,5}", mm.group(0))
                if len(nums) == 2:
                    rec.setdefault("shard_seen", set()).add(int(nums[0]))
                    rec["shard_total"] = int(nums[1])
            if r["status"] != "PASS":
                rec["complete"] = False
                rec["issues"].append(f"{fname}: {r['detail']}")

        elif r["kind"] == "checksum":
            stem = set_stem(fname)
            rec = get(d, stem, sharded=bool(SHARD_SUFFIX_RE.search(fname)))
            if r["status"] != "PASS":
                rec["complete"] = False
                rec["issues"].append(f"{fname}: {r['detail']}")

    # Friendly display name + completeness gap check for index-less shard sets.
    out = []
    for rec in sets.values():
        # A sharded set discovered without an index: confirm no shard is missing.
        if rec["sharded"] and not rec["has_index"] and rec.get("shard_total"):
            seen = rec.get("shard_seen", set())
            total = rec["shard_total"]
            missing = [i for i in range(1, total + 1) if i not in seen]
            if missing:
                rec["complete"] = False
                shown = ", ".join(f"{i:05d}-of-{total:05d}" for i in missing[:4])
                more = "" if len(missing) <= 4 else f" (+{len(missing) - 4} more)"
                rec["issues"].append(
                    f"missing shard(s): {shown}{more} — no index present to confirm set")

        base = rec["dir"].rstrip("/").rsplit("/", 1)[-1] or rec["dir"]
        stem = rec["stem"]
        rec["name"] = base if stem in ("model", base) else f"{base} [{stem}]"
        out.append(rec)
    return out


def print_findings(models):
    complete = [m for m in models if m["complete"]]
    incomplete = [m for m in models if not m["complete"]]

    print("\n" + "=" * 68)
    print("FINDINGS")
    print("=" * 68)

    if complete:
        print(f"\nComplete safetensors sets available ({len(complete)}):")
        for m in sorted(complete, key=lambda x: (x["dir"], x["stem"])):
            kind = "sharded" if m["sharded"] else "single-file"
            print(f"  ✓ {m['name']}")
            print(f"      {human_size(m['bytes'])}  |  "
                  f"{m['n_files']} file(s)  |  {kind}")
            print(f"      {m['dir']}")
    else:
        print("\nNo complete safetensors sets found.")

    if incomplete:
        print(f"\nIncomplete / failed sets ({len(incomplete)}):")
        for m in sorted(incomplete, key=lambda x: (x["dir"], x["stem"])):
            print(f"  ✗ {m['name']}  ({human_size(m['bytes'])} present)")
            for issue in m["issues"][:6]:
                print(f"      - {issue}")
            if len(m["issues"]) > 6:
                print(f"      - ... and {len(m['issues']) - 6} more")

    total_ok = sum(m["bytes"] for m in complete)
    print(f"\nTotal downloadable (complete sets only): {human_size(total_ok)}")


def main():
    global TIMEOUT
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*", help="explicit URLs (files or index.json)")
    ap.add_argument("--crawl", metavar="URL", help="recursively crawl an autoindex tree")
    ap.add_argument("--manifest", help="sha256 manifest for full-content verification")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--no-proxy", action="store_true",
                    help="ignore HTTP(S)_PROXY env vars (direct connection)")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress live progress on stderr")
    args = ap.parse_args()

    TIMEOUT = args.timeout

    session = make_session(trust_env=not args.no_proxy)

    # Progress goes to stderr; keep it out of the way for --quiet. It is still
    # useful with --json since json goes to stdout and progress to stderr.
    progress = Progress(enabled=not args.quiet)

    files, indexes = [], []
    if args.crawl:
        progress.line(f"Crawling {args.crawl} ...")
        files, indexes = crawl(session, args.crawl, args.max_depth,
                               verbose=not args.json, progress=progress)
        progress.line(f"Found {len(files)} safetensors files, "
                      f"{len(indexes)} index files.")
    elif args.urls:
        for u in args.urls:
            (indexes if INDEX_RE.search(u) else files).append(u)
    else:
        for u in expand_file_lists(FILE_LISTS):
            (indexes if INDEX_RE.search(u) else files).append(u)

    if not files and not indexes:
        print("Nothing to check. Use --crawl URL, pass URLs, or fill FILE_LISTS.",
              file=sys.stderr)
        return 2

    return run(files, indexes, session, manifest=args.manifest,
               as_json=args.json, workers=args.workers, progress=progress)


if __name__ == "__main__":
    sys.exit(main())
