# Proposal: accept gzip-compressed responses from gorpe

Status: not implemented. gorpe v2.6.0 and newer can compress responses; gorpe-mcp doesn't ask for it yet.

## Background

From v2.6.0 on, gorpe gzips a response (`gzip.BestSpeed`) when the request has `Accept-Encoding: gzip` and the body is at least `compress_min_bytes` (default 1024, `0` = off). It then sets `Content-Encoding: gzip` and `Vary: Accept-Encoding`. Smaller bodies and clients that don't send the header get plain responses as before. See the [gorpe v2.6.0 CHANGELOG](https://github.com/xorpaul/gorpe/blob/master/CHANGELOG.md).

`check_gorpe` and Icinga use Go's `net/http`, which requests and decodes gzip automatically, so they benefit with no change. gorpe-mcp uses Python's `http.client`, which does neither. So today every response gorpe-mcp receives is uncompressed, including the large ones it exists for: journal tails, `cat`/`grep` of files, and JSON documents.

## Measured benefit (gorpe v2.5.1 vs v2.6.0, 2026-11-11)

| Output | Plain | gzip | Ratio | Time at 100 KB/s, plain → gzip |
|---|---|---|---|---|
| 500 log lines | 32.6 KB | 3.1 KB | 10.4x | 57 ms → 8 ms |
| 5000 log lines | 293.5 KB | 19.4 KB | 15.2x | 2.78 s → 10 ms |
| JSON 181 KB | 180.8 KB | 35.6 KB | 5.1x | 1.62 s → 10 ms |

On localhost, compression adds about 0.5–4 ms per large response. The gain shows over slow or remote links, especially on the proxy hop (`gorpe_run_via_proxy`), where gorpe-mcp → monitoring server is one of the two connections. The numbers come from v2.5.1 and v2.6.0 release binaries side by side on one host, with log text and a JSON document as output; slow links were emulated with `curl --limit-rate`.

Compression doesn't change what callers see. `max_bytes`, `grep`, `head_lines` and `tail_lines` still work on the decoded text, so context size for an LLM caller stays the same. Only the transfer gets shorter.

## Design

In `_gorpe_request` (`server.py`):

```python
import gzip
...
req_headers = {"Accept": "application/json", "Accept-Encoding": "gzip"}
...
body_bytes = resp.read()
if (resp.getheader("Content-Encoding") or "").lower() == "gzip":
    body_bytes = gzip.decompress(body_bytes)
```

- Standard library only, no new dependency.
- Decode only when `Content-Encoding: gzip` is set. Older daemons (< v2.6.0) ignore the request header and answer plain, so both keep working.
- JSON and text-protocol parsing come after decoding and don't change.
- Proxy path: the monitoring server's gorpe compresses `check_gorpe`'s output again for gorpe-mcp, and the monitoring server → target leg is already compressed by `check_gorpe`. Nothing changes there.

### Optional: report wire size

Add `wire_bytes` (`len` of the raw body before decompression) next to `total_bytes` in the result, so callers and the server log show what compression saved. This is useful to confirm the benefit in production, and could be dropped afterwards.

## Safety

- **Decompression bomb:** gorpe is an allow-listed, mTLS-authenticated peer, and its output is bounded by the command, so the risk is low. A cap is still cheap: decompress with `zlib.decompressobj(wbits=31)` and a `max_length` (e.g. 64 MiB), and fail if more data is left.
- **Errors:** a corrupt gzip body raises `gzip.BadGzipFile`/`EOFError`/`zlib.error`. Catch these and return exit code 3 (UNKNOWN) with a clear message, the same way connection errors are handled.

## Tests

- Unit-test the decode path with a fake response: gzip and plain bodies, in both JSON and text protocol.
- Against a v2.6.0 daemon, run a command with large output and check the result matches the plain result from a v2.5.x daemon (or from one with `compress_min_bytes: 0`).

## Not in scope

When gorpe-mcp runs behind a reverse proxy, compressing the MCP client → gorpe-mcp leg is a proxy setting (e.g. Apache `mod_deflate`) and independent of this change.
