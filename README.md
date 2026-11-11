# gorpe-mcp

MCP server that exposes [gorpe](https://github.com/xorpaul/gorpe) remote-command execution over HTTPS. Wraps the gorpe HTTP API so any MCP-capable client — Claude Code, Cursor, or any other — can trigger puppet runs, tail journals, and run ad-hoc host checks on Linux fleet machines.

## How it works

The MCP server holds an mTLS client certificate. When an AI tool calls `gorpe_run`, the server makes a direct HTTPS request to the gorpe daemon on the target host, authenticated by that cert. The daemon runs an allowlisted command and returns `{"exit_code": N, "output": "..."}` as JSON.

To restrict which callers can reach the MCP server, run it behind an authenticating reverse proxy (e.g. Apache with Basic Auth, or any JWT/OAuth proxy). The `access_policy` config key lets you restrict individual users to specific host glob patterns even after they've authenticated.

## Tools

### `gorpe_run(host, command, arg="", grep="", exclude="", head_lines=0, tail_lines=0, max_bytes=65536, keep="end")`

Run any gorpe command directly against a target host.

```python
gorpe_run("web01.example.com", "run_puppet_agent")
gorpe_run("web01.example.com", "check_journalctl_unit", "nginx")
gorpe_run("web01.example.com", "run_journalctl_wild", "-u nginx -n 100 --since '1 hour ago'")
gorpe_run("web01.example.com", "run_df")
```

Common commands:

| Command | Arg | Notes |
|---|---|---|
| `run_puppet_agent` | — | Runs `puppet agent -t`. Exit 2 = changes applied (not an error). |
| `run_puppet_agent_noop` | — | Dry-run puppet. |
| `check_journalctl_puppet` | — | Last 300 lines of puppet.service journal. |
| `check_journalctl_puppet_agent` | — | Last 300 lines via `-t puppet-agent` (catches interactive runs). |
| `check_journalctl_unit` | unit name | Last 300 lines of a systemd unit. |
| `check_systemctl_status_unit` | unit name | `systemctl status <unit>`. |
| `check_listening_ports` | — | `ss -tulpn`. |
| `check_w` | — | Quick liveness probe. |
| `run_df` / `run_df_wild` | path or flags | Disk usage. |
| `run_free` | — | Memory. |
| `run_ps` | — | Full process tree. |
| `run_journalctl_wild` | journalctl flags | Flexible journal query. |
| `run_cat_wild` | absolute path | Read a file (path-restricted). |
| `run_tail_wild` | flags + path | Tail a file. |
| `run_grep_wild` | flags + path | Grep a file. |
| `run_ls_wild` | flags + path | Directory listing. |
| `run_stat_wild` | path | File metadata. |
| `run_ss_wild` | flags | Socket list. |
| `run_ip_addr` / `run_ip_wild` | ip subcommand | IP addresses / routing. |
| `run_nft_list` | — | nftables ruleset. |
| `run_dmesg` | — | Kernel ring buffer. |

Call `gorpe_list_commands()` for the full catalog with intent→command mapping. Sites can replace the built-in catalog with their own (see `catalog` / `catalog_file` below).

**Output shaping** (applied by gorpe-mcp after the command ran, in this order) keeps large outputs out of the caller's context:

| Parameter | Effect |
|---|---|
| `grep` | Regex (Python `re`), keep only matching lines. Use this instead of piping: gorpe never runs a shell. |
| `exclude` | Regex, drop matching lines. |
| `head_lines` / `tail_lines` | Keep only the first / last N lines (`0` = all). |
| `max_bytes` | Cap on the returned output, default 64 KiB (`0` = unlimited). |
| `keep` | `"end"` (default, newest log lines) or `"start"` when `max_bytes` cuts. |

The result is a JSON object: `exit_code`, `output`, `exit_meaning` (puppet commands only), `truncated`, `total_lines` / `total_bytes` (raw output before shaping), `filter_error` (invalid regex only) and `stderr` (only when non-empty and the target gorpe sends it separately; not via proxy). `output` is stdout and stderr interleaved; `stderr` repeats only the stderr part, unaffected by `grep` and line limits.

**Puppet exit code interpretation** (applied automatically to `run_puppet_agent*`):

| Exit | Meaning |
|---|---|
| 0 | No changes — catalog already applied |
| 2 | Changes applied successfully |
| 4 | Resource failures |
| 6 | Changes applied AND failures |
| 3 | Transport error (connection refused, TLS failure) |

### `gorpe_run_via_proxy(monitoring_server, target_host, command, arg="", ...)`

Takes the same output-shaping parameters as `gorpe_run`.

Hop through a monitoring server when direct port 5667 access to the target is blocked by a firewall. The call chain is: MCP server → monitoring server gorpe (`run_gorpe_proxy`) → target gorpe.

```python
gorpe_run_via_proxy(
    "monitoring.example.com",
    "firewalled-host.example.com",
    "run_puppet_agent"
)
```

The monitoring server must have `run_gorpe_proxy` in its `gorpe.yaml` and must accept connections from this machine's IP in its `allowed_ips`. Always try `gorpe_run` first — only fall back to the proxy if the direct attempt fails.

## Installation

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp config.example.yaml config.yaml
# edit config.yaml — set cert/key/ca to your gorpe client certificate paths
.venv/bin/gorpe-mcp --port 8083
```

Test with any MCP client pointed at `http://localhost:8083/mcp`.

## Running as a service

Example systemd unit (`/etc/systemd/system/gorpe-mcp.service`):

```ini
[Unit]
Description=Gorpe MCP server
After=network.target

[Service]
User=gorpe-mcp
ExecStart=/opt/gorpe-mcp/.venv/bin/gorpe-mcp \
  --config /etc/gorpe-mcp/config.yaml \
  --transport streamable-http \
  --port 8083
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Expose it externally via a reverse proxy with authentication. Example Apache snippet:

```apache
<Location /gorpe-mcp/mcp>
    ProxyPass http://127.0.0.1:8083/mcp
    ProxyPassReverse http://127.0.0.1:8083/mcp
    AuthType Basic
    AuthName "gorpe-mcp"
    AuthUserFile /etc/gorpe-mcp/htpasswd
    Require valid-user
    RequestHeader set X-Remote-User "%{REMOTE_USER}s"
</Location>
```

## Connecting Claude Code

Register the server once at user scope so it's available in all projects:

```bash
claude mcp add --transport http -s user gorpe \
  https://your-mcp-server.example.com/gorpe-mcp/mcp \
  --header "Authorization: Basic $(printf 'USER:PASSWORD' | base64)"
```

Replace `USER`, `PASSWORD`, and the URL with your own values. Verify with `claude mcp list`.

## Configuration

All config keys are optional — defaults point to `/etc/gorpe/ssl/{cert,key,ca}.pem`.

The built-in command catalog describes generic gorpe commands. If your gorpe daemons define their own command set, put a curated catalog (intent → command mapping, commands and their args, and proxy routing: which monitoring server reaches which network) into `catalog` or `catalog_file`, so AI callers pick the right command and proxy.

| Key | Default | Description |
|---|---|---|
| `cert` | `/etc/gorpe/ssl/cert.pem` | mTLS client certificate |
| `key` | `/etc/gorpe/ssl/key.pem` | Private key for the client cert |
| `ca` | `/etc/gorpe/ssl/ca.pem` | CA bundle (used when gorpe daemons verify client certs) |
| `host` | `0.0.0.0` | Listen address (overridden by `--host`) |
| `port` | `8083` | Listen port (overridden by `--port`) |
| `access_policy` | — | Per-user host restrictions (fnmatch glob patterns) |
| `catalog` | — | Inline command catalog returned by `gorpe_list_commands()`, replacing the built-in one |
| `catalog_file` | — | Path to a command catalog file (used when `catalog` is not set; falls back to the built-in catalog if unreadable) |

The gorpe server cert is **not** verified — gorpe daemons may use self-signed certs. Authentication is provided by the mTLS client certificate.

## Source and upstream

- gorpe daemon: https://github.com/xorpaul/gorpe
- check_gorpe client: https://github.com/xorpaul/check_gorpe
- gorpe-ai (daemon config, scripts, sudoers, AI skill): https://github.com/xorpaul/gorpe-ai
