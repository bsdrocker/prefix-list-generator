# bgpq4-arista

A tiny Flask service that wraps [`bgpq4`](https://github.com/bgp/bgpq4) and returns
prefix-lists in **Arista EOS plain-text syntax** so Arista switches can pull them
dynamically with `ip prefix-list NAME source http://...`.

Similar in spirit to [bgpq-proxy](https://github.com/peering-manager/bgpq-proxy),
but instead of JSON the response body is exactly what an Arista switch wants to
see when it sources a prefix-list over HTTP.

## How it works

bgpq4's default output is Cisco IOS-style `ip prefix-list NAME [seq N] permit X/Y`
lines (and `ipv6 prefix-list ...` lines with `-6`). Arista EOS accepts that
syntax verbatim, both when pasted into the CLI and when fetched via the
`source http://...` feature, so this service just runs `bgpq4` and returns
the output as `text/plain`.

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

With the defaults you get output like:

```
ip prefix-list PEER-HE permit 4.7.0.0/16 le 24
ipv6 prefix-list PEER-HE-V6 permit 2001:470::/32 le 48
```

i.e. the AS-SET's aggregates plus any more-specifics down to /24 (v4) and /48 (v6).

## Example response

```
$ curl -s http://localhost:8080/arista/PEER-HE/AS-HURRICANE | head
no ip prefix-list PEER-HE
ip prefix-list PEER-HE permit 4.7.6.0/24
ip prefix-list PEER-HE permit 5.39.96.0/19
ip prefix-list PEER-HE permit 8.7.198.0/24
...
```

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
