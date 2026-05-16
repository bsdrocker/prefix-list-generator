"""
bgpq4 -> Arista prefix-list HTTP front end.

Returns plain-text Arista EOS prefix-list *body* suitable for use with:

    ip   prefix-list NAME source http:<host>:<port>/arista/as_set/<as-set>
    ipv6 prefix-list NAME source http:<host>:<port>/arista/as_set/<as-set>/v6
    ip   prefix-list NAME source http:<host>:<port>/arista/asn/<asn>
    ipv6 prefix-list NAME source http:<host>:<port>/arista/asn/<asn>/v6

(note the Arista CLI quirk — `http:` with a single colon, not `http://`).

When Arista sources a prefix-list over HTTP, the switch already knows the
list name (declared by the parent `ip prefix-list NAME` command), so the
HTTP body must contain only the entries — `seq N permit X/Y [le N]` lines —
not the full `ip prefix-list NAME permit ...` form that bgpq4 emits.

This service runs bgpq4 and reformats its output accordingly: drops the
header line, strips the `ip|ipv6 prefix-list PL ` prefix from each entry,
and prepends a sequence number starting at 1.

Two endpoint shapes are exposed:

* /arista/as_set/<as-set>      — expands an AS-SET / AS object via IRR
* /arista/asn/<asn>            — prefixes originated by a single ASN

Both default to IPv4. Append `/v6` to either path for IPv6. (We bake the
family into the path rather than a query string so the URL is zsh-safe;
`?` is a glob character there and trips users up.)
"""

import os
import re
import shutil
import subprocess
import logging
from flask import Flask, Response, abort
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

# AS-SET / AS object names. Accept things like:
#   AS65000, AS-FOO, AS-FOO:AS-BAR, RIPE::AS-FOO, AS65000:AS-CUSTOMERS
# Conservative pattern, but covers normal IRR object syntax.
AS_RE = re.compile(r"^[A-Za-z0-9_:\-]{1,128}$")

# ASN input for /arista/asn/<asn>. Accept "65000" or "AS65000" (case-insensitive
# AS prefix). Range-checked against the 32-bit AS space after the regex.
ASN_RE = re.compile(r"^(?:[Aa][Ss])?([0-9]{1,10})$")

FAMILY_FLAGS = {"ipv4": "-4", "ipv6": "-6"}

# Placeholder name for `bgpq4 -l`. The switch doesn't see it (we strip the
# `ip[v6] prefix-list PL ` prefix from each entry); it just needs to be a
# stable, valid IRR-style identifier so bgpq4 will emit lines we can parse.
BGPQ4_LIST_NAME = "PL"


def _normalize_asn(raw: str) -> str | None:
    """Return canonical 'AS<digits>' form, or None if input is invalid.

    Accepts '65000', 'AS65000', 'as65000'. Range-checks against 32-bit ASN
    space (1..4294967295); ASN 0 is reserved and rejected.
    """
    m = ASN_RE.match(raw)
    if not m:
        return None
    n = int(m.group(1))
    if n < 1 or n > 4_294_967_295:
        return None
    return f"AS{n}"


# ---- bgpq4 invocation --------------------------------------------------------

def _run_bgpq4(target: str, family: str) -> str:
    cmd = [BGPQ4_BIN, FAMILY_FLAGS[family], "-l", BGPQ4_LIST_NAME]
    if AGGREGATE:
        cmd.append("-A")
    max_len = MAX_LENGTH_V4 if family == "ipv4" else MAX_LENGTH_V6
    if max_len is not None:
        cmd.extend(["-R", str(max_len)])
    if IRR_HOST:
        cmd.extend(["-h", IRR_HOST])
    if IRR_SOURCES:
        cmd.extend(["-S", IRR_SOURCES])
    cmd.append(target)

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
        log.warning("bgpq4 timeout for %s", target)
        abort(504, description="bgpq4 timed out")

    if result.returncode != 0:
        log.warning("bgpq4 rc=%s stderr=%s", result.returncode, result.stderr.strip())
        abort(502, description=f"bgpq4 error: {result.stderr.strip() or 'unknown'}")

    return result.stdout


def _arista_format(raw: str, family: str) -> str:
    """Convert bgpq4 default output into Arista source-http body format.

    bgpq4 emits, for v4:
        no ip prefix-list PL
        ip prefix-list PL permit 1.2.3.0/24
        ip prefix-list PL permit 4.5.0.0/16 le 24

    We want:
        seq 1 permit 1.2.3.0/24
        seq 2 permit 4.5.0.0/16 le 24

    Lines that don't match the expected shape are silently dropped (defensive
    — bgpq4 doesn't emit anything else under default flags, but we don't want
    a stray line to break the switch's parser).
    """
    line_prefix = "ip prefix-list " if family == "ipv4" else "ipv6 prefix-list "
    out = []
    seq = 1
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("no " + line_prefix):
            continue
        if not stripped.startswith(line_prefix):
            continue
        # Everything after "ip[v6] prefix-list NAME " — i.e. "permit X/Y [...]"
        rest = stripped[len(line_prefix):]
        space_idx = rest.find(" ")
        if space_idx == -1:
            continue
        body = rest[space_idx + 1:].strip()
        if not body:
            continue
        out.append(f"seq {seq} {body}")
        seq += 1
    return "\n".join(out) + ("\n" if out else "")


def _get_prefix_list(target: str, family: str) -> str:
    key = (target, family)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        log.debug("cache hit %s", key)
        return cached

    raw = _run_bgpq4(target, family)
    output = _arista_format(raw, family)
    with _cache_lock:
        _cache[key] = output
    return output


# ---- Routes ------------------------------------------------------------------

# Two endpoint families:
#   /arista/as_set/<as-set>[/v6]   — expand an AS-SET via IRR
#   /arista/asn/<asn>[/v6]         — prefixes originated by a single ASN
#
# IPv4 is the default at the base path; the `/v6` suffix flips to IPv6.

@app.route("/arista/as_set/<as_set>", defaults={"family": "ipv4"})
@app.route("/arista/as_set/<as_set>/v6", defaults={"family": "ipv6"})
def arista_as_set(as_set: str, family: str):
    if not AS_RE.match(as_set):
        abort(400, description="invalid AS-SET / AS object")
    body = _get_prefix_list(as_set, family)
    return Response(body, mimetype="text/plain")


@app.route("/arista/asn/<asn>", defaults={"family": "ipv4"})
@app.route("/arista/asn/<asn>/v6", defaults={"family": "ipv6"})
def arista_asn(asn: str, family: str):
    normalized = _normalize_asn(asn)
    if normalized is None:
        abort(400, description="invalid ASN (expected 1..4294967295, optionally AS-prefixed)")
    body = _get_prefix_list(normalized, family)
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
        "  GET /arista/as_set/<as-set>          (IPv4)\n"
        "  GET /arista/as_set/<as-set>/v6       (IPv6)\n"
        "  GET /arista/asn/<asn>                (IPv4, prefixes originated by ASN)\n"
        "  GET /arista/asn/<asn>/v6             (IPv6)\n"
        "  GET /health\n"
        "\n"
        "Examples:\n"
        "  curl http://localhost:8080/arista/as_set/AS-HURRICANE\n"
        "  curl http://localhost:8080/arista/as_set/AS-HURRICANE/v6\n"
        "  curl http://localhost:8080/arista/asn/AS6939\n"
        "  curl http://localhost:8080/arista/asn/6939/v6\n",
        mimetype="text/plain",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
