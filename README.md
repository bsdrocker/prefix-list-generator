# bgpq4-arista

A tiny Flask service that wraps [`bgpq4`](https://github.com/bgp/bgpq4) and
returns prefix-list entries in the **`seq N permit X/Y [le N]` body format**
that Arista EOS expects when sourcing a prefix-list over HTTP
(`ip prefix-list NAME` → `source http://...`).

Similar in spirit to [bgpq-proxy](https://github.com/peering-manager/bgpq-proxy),
but tailored to Arista's source-http loader instead of returning JSON.

## How it works

When Arista sources a prefix-list over HTTP, the switch already knows the
list name (from the parent `ip prefix-list NAME` command) and expects the
HTTP body to contain only the entries — `seq N permit X/Y [le N]` lines —
not bgpq4's default `ip prefix-list NAME permit ...` form.

So this service runs `bgpq4`, drops the `no ip prefix-list NAME` header,
strips the `ip|ipv6 prefix-list NAME ` prefix from each line, and prepends
sequence numbers starting at 1. The result is returned as `text/plain` and
goes straight into the switch's prefix-list when refreshed.

## Endpoints

```
GET /arista/<prefix-list-name>/<as-set>[?family=ipv4|ipv6]
GET /health
GET /
```

Both `<prefix-list-name>` and `<as-set>` are validated against conservative
regexes before being passed to `bgpq4`.

`family` defaults to `ipv4`. Use `family=ipv6` for IPv6.

## Running

### Docker Compose

```sh
docker compose up -d --build
curl http://localhost:8080/arista/PEER-HE/AS-HURRICANE
curl 'http://localhost:8080/arista/PEER-HE-V6/AS-HURRICANE?family=ipv6'
```

### Bare metal (dev)

```sh
apt-get install -y bgpq4
pip install -r requirements.txt
python app.py
```

## Configuration

All knobs are environment variables (see `docker-compose.yml`):

| Variable             | Default        | Notes                                                                   |
| -------------------- | -------------- | ----------------------------------------------------------------------- |
| `BGPQ4_CACHE_TTL`    | `3600`         | Seconds to cache each prefix-list in memory.                            |
| `BGPQ4_CACHE_MAX`    | `1024`         | Max distinct cached entries.                                            |
| `BGPQ4_SOURCES`      | *(empty)*      | Comma list passed to `bgpq4 -S`, e.g. `RIPE,RADB,APNIC,ARIN,NTTCOM`.    |
| `BGPQ4_HOST`         | *(empty)*      | IRR host (`bgpq4 -h`). Default = bgpq4 default (`rr.ntt.net`).          |
| `BGPQ4_AGGREGATE`    | `1`            | Pass `-A` (aggregate prefixes).                                         |
| `BGPQ4_MAX_LENGTH_V4`| `24`           | Pass `-R <N>` for IPv4. Aggregate but allow more-specifics up to /N. Set to `""` or `0` to omit `-R`. |
| `BGPQ4_MAX_LENGTH_V6`| `48`           | Same as above for IPv6. Default `/48` matches typical peering policy.   |
| `BGPQ4_TIMEOUT`      | `60`           | Subprocess timeout, seconds.                                            |
| `BGPQ4_BIN`          | `bgpq4`        | Path to bgpq4 binary.                                                   |
| `PORT`               | `8080`         | Listen port.                                                            |

With the defaults you get entries like `permit 4.7.0.0/16 le 24` for v4 and
`permit 2001:470::/32 le 48` for v6 — the AS-SET's aggregates plus any
more-specifics down to /24 (v4) and /48 (v6).

## Example response

```
$ curl -s http://localhost:8080/arista/PEER-HE/AS-HURRICANE | head
seq 1 permit 4.7.0.0/16 le 24
seq 2 permit 5.39.96.0/19 le 24
seq 3 permit 8.7.198.0/24
seq 4 permit 12.0.0.0/8 le 24
...
```

Note the absence of `ip prefix-list NAME` on each line — that's intentional.
Arista's source-http loader prepends it from the parent declaration, so the
body returned here must contain only the entries.

## Arista EOS config example

On the switch:

```
! IPv4
ip prefix-list PEER-HE
   source http://bgpq4-arista.example.net:8080/arista/PEER-HE/AS-HURRICANE
!
! IPv6
ipv6 prefix-list PEER-HE-V6
   source http://bgpq4-arista.example.net:8080/arista/PEER-HE-V6/AS-HURRICANE?family=ipv6
```

Then refresh on demand:

```
switch# ip prefix-list PEER-HE refresh
```

…or schedule a periodic refresh via EOS event-handler / scheduler if you want
the switch to pull updates automatically.

> Make sure the `<prefix-list-name>` in the URL exactly matches the
> `ip prefix-list NAME` you defined on the switch — bgpq4 emits the name on
> every line, and Arista will reject lines whose name doesn't match the
> prefix-list being sourced.

## Notes / caveats

- The cache is per-worker, in-memory only. If you run multiple gunicorn workers
  each one warms its own cache. That's fine for this workload.
- IRR queries can be slow; the default 60s subprocess timeout protects the
  service from hanging requests.
- Input is regex-validated before being handed to `bgpq4`. The subprocess is
  invoked with an argument list (no shell), so shell metacharacters in the URL
  cannot cause command injection.
