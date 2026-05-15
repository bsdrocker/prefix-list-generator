"""
bgpq4 -> Arista prefix-list HTTP front end.

Returns plain-text Arista EOS prefix-list config suitable for use with:

    ip prefix-list NAME
       source http://<this-server>:<port>/arista/NAME/AS-SET

bgpq4's default output is Cisco IOS-style `ip prefix-list ...` lines, which
Arista EOS accepts verbatim. For IPv6 we pass -6 and bgpq4 emits
`ipv6 prefix-list ...` lines, which Arista also accepts via
`ipv6 prefix-list NAME ; source http://...`.
"""

import os
import re
import shutil
import subprocess
import logging
from flask import Flask, Response, abort, request
from cachetools import TTLCache
from threading import Lock

app = Flask(__name__)

# ---- Config (env-overridable) ------------------------------------------------

# Cache TTL in seconds (default 1 hour).
CACHE_TTL = int(os.environ.get("BGPQ4_CACHE_TTL", "3600"))
# Max distinct cache entries.
CACHE_MAX = int(os.environ.get("BGPQ4_CACHE_MAX", "1024"))
# Comma-separated IRR sources, passed to bgpq4 -S. Empty = bgpq4 default.
IRR_SOURCES = os.environ.get("BGPQ4_SOURCES", "").strip()
# IRR host, passed to bgpq4 -h. Empty = bgpq4 default (rr.ntt.net).
IRR_HOST = os.environ.get("BGPQ4_HOST", "").strip()
# Aggregate prefixes (bgpq4 -A). Defaults on.
AGGREGATE = os.environ.get("BGPQ4_AGGREGATE", "1") not in ("0", "false", "False")
# Path to bgpq4 binary.
BGPQ4_BIN = os.environ.get("BGPQ4_BIN", "bgpq4")
# Subprocess timeout in seconds.
BGPQ4_TIMEOUT = int(os.environ.get("BGPQ4_TIMEOUT", "60"))


def _parse_max_len(raw: str, family_label: str, valid_max: int):
    """Validate BGPQ4_MAX_LENGTH_* env vars. Empty/'0' -> None (omit -R)."""
    raw = (raw or "").strip()
    if not raw or raw == "0":
        return None
    try:
        n = int(raw)
    except ValueError:
        raise SystemExit(
            f"BGPQ4_MAX_LENGTH_{family_label}: expected integer, got {raw!r}"
        )
    if n < 1 or n > valid_max:
        raise SystemExit(
            f"BGPQ4_MAX_LENGTH_{family_label}: must be 1..{valid_max}, got {n}"
        )
    return n


# Max prefix length for bgpq4 -R, per family. bgpq4 expands longer specifics
# into `le N` form, so on an aggregated v4 list `-R 24` permits anything from
# the aggregate up to /24 — the usual policy for peer prefix-lists. Set the
# env var to empty or "0" to omit -R and use bgpq4's default (no le clause).
MAX_LENGTH_V4 = _parse_max_len(os.environ.get("BGPQ4_MAX_LENGTH_V4", "24"), "V4", 32)
MAX_LENGTH_V6 = _parse_max_len(os.environ.get("BGPQ4_MAX_LENGTH_V6", "48"), "V6", 128)

# Logging
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("bgpq4-arista")

# ---- Cache -------------------------------------------------------------------

_cache = TTLCache(maxsize=CACHE_MAX, ttl=CACHE_TTL)
_cache_lock = Lock()

# ---- Validation --------------------------------------------------------------

# Prefix-list names: letters, digits, dash, underscore. Keep it conservative.
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

# AS-SET / AS object names. Accept things like:
#   AS65000, AS-FOO, AS-FOO:AS-BAR, RIPE::AS-FOO, AS65000:AS-CUSTOMERS
# Conservative pattern, but covers normal IRR object syntax.
AS_RE = re.compile(r"^[A-Za-z0-9_:\-]{1,128}$")

FAMILY_FLAGS = {"ipv4": "-4", "ipv6": "-6"}


def _validate(name: str, as_set: str, family: str) -> None:
    if not NAME_RE.match(name):
        abort(400, description="invalid prefix-list name")
    if not AS_RE.match(as_set):
        abort(400, description="invalid AS-SET / AS object")
    if family not in FAMILY_FLAGS:
        abort(400, description="family must be ipv4 or ipv6")


# ---- bgpq4 invocation --------------------------------------------------------

def _run_bgpq4(name: str, as_set: str, family: str) -> str:
    cmd = [BGPQ4_BIN, FAMILY_FLAGS[family], "-l", name]
    if AGGREGATE:
        cmd.append("-A")
    max_len = MAX_LENGTH_V4 if family == "ipv4" else MAX_LENGTH_V6
    if max_len is not None:
        cmd.extend(["-R", str(max_len)])
    if IRR_HOST:
        cmd.extend(["-h", IRR_HOST])
    if IRR_SOURCES:
        cmd.extend(["-S", IRR_SOURCES])
    cmd.append(as_set)

    log.info("running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=BGPQ4_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        log.error("bgpq4 binary not found at %r", BGPQ4_BIN)
        abort(500, description="bgpq4 not installed on server")
    except subprocess.TimeoutExpired:
        log.warning("bgpq4 timeout for %s/%s", name, as_set)
        abort(504, description="bgpq4 timed out")

    if result.returncode != 0:
        log.warning("bgpq4 rc=%s stderr=%s", result.returncode, result.stderr.strip())
        abort(502, description=f"bgpq4 error: {result.stderr.strip() or 'unknown'}")

    return result.stdout


def _get_prefix_list(name: str, as_set: str, family: str) -> str:
    key = (name, as_set, family)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        log.debug("cache hit %s", key)
        return cached

    output = _run_bgpq4(name, as_set, family)
    with _cache_lock:
        _cache[key] = output
    return output


# ---- Routes ------------------------------------------------------------------

@app.route("/arista/<name>/<as_set>")
def arista(name: str, as_set: str):
    family = request.args.get("family", "ipv4").lower()
    _validate(name, as_set, family)
    body = _get_prefix_list(name, as_set, family)
    # text/plain so Arista's HTTP-sourced prefix-list loader is happy.
    return Response(body, mimetype="text/plain")


@app.route("/health")
def health():
    ok = shutil.which(BGPQ4_BIN) is not None or os.path.isfile(BGPQ4_BIN)
    status = 200 if ok else 503
    body = "ok\n" if ok else "bgpq4 not found\n"
    return Response(body, status=status, mimetype="text/plain")


@app.route("/")
def index():
    return Response(
        "bgpq4 -> Arista prefix-list proxy\n"
        "\n"
        "Usage:\n"
        "  GET /arista/<prefix-list-name>/<as-set>[?family=ipv4|ipv6]\n"
        "  GET /health\n"
        "\n"
        "Example:\n"
        "  curl http://localhost:8080/arista/PEER-HE/AS-HURRICANE\n"
        "  curl http://localhost:8080/arista/PEER-HE-V6/AS-HURRICANE?family=ipv6\n",
        mimetype="text/plain",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
