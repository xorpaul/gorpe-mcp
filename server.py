import argparse
import asyncio
import contextvars
import fnmatch
import functools
import http.client
import json
import logging
import os
import socket
import ssl
import sys
import urllib.parse

import uvicorn
import yaml
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("gorpe-mcp")

# Current authenticated user, set per-request by _ClientLogMiddleware.
_current_user: contextvars.ContextVar[str] = contextvars.ContextVar("current_user", default="-")

# access_policy from config: {username: [glob_pattern, ...]}
# Users absent from this dict are unrestricted.
_access_policy: dict[str, list[str]] = {}


def _check_access(user: str, host: str) -> str | None:
    """Return None if access is permitted, or an error string if denied."""
    patterns = _access_policy.get(user)
    if patterns is None:
        return None
    if any(fnmatch.fnmatch(host, p) for p in patterns):
        return None
    return (
        f"Access denied: user '{user}' is restricted to hosts matching "
        + ", ".join(patterns)
        + f" — '{host}' does not match."
    )


# IP → hostname cache; populated on first request from each client.
_dns_cache: dict[str, str] = {}


def _resolve_client(ip: str) -> str:
    if ip not in _dns_cache:
        try:
            _dns_cache[ip] = socket.gethostbyaddr(ip)[0]
        except (socket.herror, socket.gaierror):
            _dns_cache[ip] = ip
    return _dns_cache[ip]


class _ClientLogMiddleware:
    """Pure ASGI middleware: logs each HTTP request with the resolved client hostname.

    Reads X-Forwarded-For (set by a reverse proxy) so the log shows
    the real caller's IP/name, not the proxy's own address.
    """

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k: v for k, v in scope.get("headers", [])}
            xff = headers.get(b"x-forwarded-for", b"").decode()
            ip = xff.split(",")[0].strip() if xff else (scope.get("client") or ("-",))[0]
            name = _resolve_client(ip)
            label = f"{ip} ({name})" if name != ip else ip
            user = headers.get(b"x-remote-user", b"-").decode()
            token = _current_user.set(user)
            logger.info("[%s @ %s] %s %s", user, label, scope["method"], scope["path"])
            try:
                await self._app(scope, receive, send)
            finally:
                _current_user.reset(token)
        else:
            await self._app(scope, receive, send)


DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

_cfg = {}
_cert = ""
_key = ""
_ca = ""


def _load_config(path: str) -> dict:
    if not os.path.exists(path):
        print(f"Config not found: {path}. Copy config.example.yaml to config.yaml.", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _resolve_paths(config: dict) -> tuple[str, str, str]:
    cert = config.get("cert", "/etc/gorpe/ssl/cert.pem")
    key = config.get("key", "/etc/gorpe/ssl/key.pem")
    ca = config.get("ca", "/etc/gorpe/ssl/ca.pem")
    return cert, key, ca


def _gorpe_request(host: str, port: int, command: str, arg: str) -> tuple[int, str]:
    """Make a direct mTLS HTTPS request to a gorpe daemon, requesting JSON output.

    Falls back to parsing the legacy text protocol ("Result Code: N") if the
    daemon has not yet been updated to support Accept: application/json.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    # gorpe daemons may use self-signed certs; skip server cert verification
    # and rely on mTLS client cert for authentication, matching check_gorpe's
    # InsecureSkipVerify = true behaviour.
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(_cert, _key)

    try:
        conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=620)
        req_headers = {"Accept": "application/json"}

        if arg:
            body = urllib.parse.urlencode({"arg1": arg})
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"
            conn.request("POST", f"/{command}", body=body, headers=req_headers)
        else:
            conn.request("GET", f"/{command}", headers=req_headers)

        resp = conn.getresponse()
        content_type = resp.getheader("Content-Type", "")
        body_bytes = resp.read()
        conn.close()

        if "application/json" in content_type:
            data = json.loads(body_bytes.decode())
            return data["exit_code"], data["output"].rstrip("\n")

        # Fallback: old-style text protocol — "...\nResult Code: N\n"
        text = body_bytes.decode()
        lines = text.rstrip("\n").split("\n")
        if len(lines) >= 1 and lines[-1].startswith("Result Code: "):
            rc = int(lines[-1].split(": ", 1)[1].strip())
            output = "\n".join(lines[:-1])
        else:
            rc = 3
            output = text
        return rc, output.strip()

    except OSError as e:
        return 3, f"connection error: {e}"
    except Exception as e:
        return 3, f"error: {e}"


def _run(host: str, command: str, arg: str | None, proxy: str | None) -> tuple[int, str]:
    """Run a gorpe command, optionally via a run_gorpe_proxy hop."""
    if proxy:
        proxy_arg = f"{host} {command}"
        if arg:
            proxy_arg += f" {arg}"
        return _gorpe_request(proxy, 5667, "run_gorpe_proxy", proxy_arg)
    return _gorpe_request(host, 5667, command, arg or "")


_PUPPET_EXIT = {
    0: "no changes (catalog already applied)",
    2: "changes applied successfully",
    4: "resource failures during run",
    6: "changes applied AND some failures",
}

_PUPPET_CMDS = {"run_puppet_agent", "run_puppet_agent_noop", "run_puppet_agent_wild"}


def _make_result(rc: int, output: str, command: str) -> str:
    result: dict = {"exit_code": rc, "output": output if output else ""}
    if command in _PUPPET_CMDS and rc in _PUPPET_EXIT:
        result["exit_meaning"] = _PUPPET_EXIT[rc]
    return json.dumps(result)


mcp = FastMCP("gorpe-mcp")

_COMMAND_CATALOG = """\
# Gorpe command catalog

Call gorpe_list_commands() whenever you are unsure which command to use.
Use gorpe_run() for direct access; gorpe_run_via_proxy() when the host is
behind a firewall (try direct first — only fall back to proxy on connection error).

## Proxy routing (use gorpe_run_via_proxy monitoring_server=...)
  Set monitoring_server to the FQDN of any host that has port 5667 access to
  the target and has run_gorpe_proxy in its gorpe.yaml commands list.

## Intent → command mapping (start here)
  "is the host up / reachable"              → check_w
  "server health / quick overview"          → run_glances  (cpu,mem,swap,load,diskio,fs,network in one shot)
  "full health check"                       → check_linux  (the most comprehensive: covers load, mem, disk,
                                               swap, zombie procs, inode usage, NTP, and more in one call)
  "what services are running"               → run_systemctl_list
  "is <svc> running / status of <svc>"     → check_systemctl_status_unit  arg=<svc>
  "show logs / tail logs for <svc>"        → check_journalctl_unit  arg=<svc>
  "what is using disk"                      → run_df  then run_df_wild  arg=<mount>
  "disk usage check (nagios)"               → check_disk  arg="<warn_pct> <crit_pct>"
  "show network / what ports are open"      → check_listening_ports
  "run puppet"                              → run_puppet_agent
  "dry run puppet / noop"                   → run_puppet_agent_noop
  "show last puppet run result"             → check_journalctl_puppet + check_journalctl_puppet_agent (always both)
  "show kernel messages / dmesg"            → run_dmesg
  "read config file <path>"                 → run_stat_wild (size check first) then run_cat_wild  arg=<path>
  "I/O stats / disk I/O"                    → run_iostat_wild  arg="-xd 1 3"
  "ceph cluster health"                     → check_ceph_health
  "ceph OSD status"                         → check_ceph_osd
  "check TLS/SSL cert expiry"               → check_ssl_cert_wild  arg=<flags>
  "is puppet stale / when did puppet last run" → check_puppet

## Server health (nagios-style OK/WARN/CRIT checks)
  check_linux         — THE comprehensive Linux health check (load, mem, disk, swap, zombies, inodes, NTP, ...)
                        no arg; use this as the primary "is the server healthy" check
  check_w             — logged-in users; fastest liveness probe
  check_puppet        — time since last puppet run; WARN at 3700s, CRIT at 86400s
  check_reboot        — whether a reboot is pending (kernel update not yet applied)
  check_linux_bonding — network bonding state; arg=optional iface filter
  check_listening_ports — all listening sockets (ss -tulpn); no arg
  check_systemctl_status_unit — systemctl status <unit>; arg=unit name
  run_systemctl_list  — all systemd units (list-units --all)
  check_sssd          — SSSD/LDAP authentication daemon health
  check_chrony        — NTP sync status
  check_sockets       — custom socket count check

## Quick resource overview
  run_glances         — one-shot glances snapshot: cpu, mem, memswap, load, diskio, fs, network
                        the fastest way to see overall resource state at a glance; no arg
  run_glances_wild    — same but override plugin list; arg e.g. "cpu,diskio"
  run_uptime          — load average (uptime output)
  run_free            — free -h (RAM and swap)
  run_lscpu           — CPU topology and frequency info
  run_ps              — ps auxf (full process tree)
  run_dmesg           — dmesg -T (kernel ring buffer with timestamps)

## Disk & filesystem
  run_df              — df -h (all mounts)
  run_df_wild         — df with custom path or flags; arg e.g. "/var" or "-i" (inodes)
  run_lsblk           — block device tree
  check_disk          — nagios disk check; arg="<warn_pct> <crit_pct>"
  check_disk_wild     — nagios disk check with custom flags; arg=full check_disk flag string
  check_diskio        — disk I/O utilisation (nagios); arg=hostname

## I/O performance
  run_iostat_wild     — iostat with flags; arg e.g. "-xd 1 3" (3 samples, 1s apart, extended)
  run_vmstat_wild     — vmstat with flags; arg e.g. "1 3" (3 samples, 1s apart)

## Puppet operations
  run_puppet_agent            — full puppet run; exit 0=no-change 2=changes 4=failures 6=changes+failures
  run_puppet_agent_noop       — dry-run (--noop); same exit codes
  run_puppet_agent_wild       — puppet run with custom flags; arg=full flag string e.g. "--tags profile::foo"
  run_facter_wild             — facter with args; arg e.g. "networking.fqdn" or "-j"
  run_puppet_lookup_wild      — puppet lookup with args; arg e.g. "profile::foo::bar --node host.fqdn"

## Puppet log retrieval
  check_journalctl_puppet       — last 300 lines of puppet.service journal (daemon cgroup only)
  check_journalctl_puppet_agent — last 300 lines via -t puppet-agent; catches interactive runs invisible
                                  to check_journalctl_puppet. ALWAYS retrieve both when asked about puppet.
  check_puppet_debian           — EL-compatible puppet check for Debian hosts

## Journal / log access
  check_journalctl_unit         — last 300 lines of journalctl -u <unit>; arg=unit name
  check_journalctl_instance_unit — template/instance units e.g. ceph-mds@foo; arg="<template> <instance>"
  run_journalctl_unit_1h        — last 1 hour of a unit's journal (up to 500 lines); arg=unit name
  run_journalctl_wild           — journalctl with arbitrary flags; arg e.g. "-u postfix -n 200 --since '1 hour ago'"

## File reading & text processing
  Path restrictions for run_cat/tail/grep/awk/sed/z*/bz*: file must be absolute, no ..,
  within /etc/ /var/log/ /proc/ /run/ /sys/ /opt/ /usr/lib/systemd/ /lib/systemd/ /usr/share/
  Denied: /etc/shadow /etc/gshadow.
  CHECK SIZE FIRST with run_stat_wild before run_cat_wild on any log file.

  run_cat_wild   — cat a file; arg=absolute path
  run_tail_wild  — tail; arg e.g. "-n 200 /var/log/syslog"
  run_grep_wild  — grep; exit 1 = no matches (expected for debug use, not an alert)
  run_awk_wild   — awk on a file
  run_sed_wild   — sed (read-only; -i/--in-place blocked)

## Compressed log reading (same path restrictions as run_cat_wild)
  run_zgrep_wild  — zgrep inside .gz log; file path must be last arg
  run_ztail_wild  — zcat | tail on .gz log; use -n to bound output; file path must be last arg
  run_bzgrep_wild — bzgrep inside .bz2 log
  run_bztail_wild — bzcat | tail on .bz2 log

## Directory & file metadata (no path restrictions)
  run_ls_wild    — sudo ls with flags; arg e.g. "-la /etc/nginx/"
  run_stat_wild  — sudo stat; arg=path; use to check file size before run_cat_wild

## Networking
  run_ip_addr    — ip addr show (all interfaces)
  run_ip_route   — ip route show
  run_ip_wild    — any ip subcommand; arg e.g. "neigh show" or "link show eth0"
  run_ss_wild    — sudo ss with flags; arg e.g. "-tulpn" or "-tnp state established"
  run_iptables_list — iptables -L -n -v (empty on nft-only hosts)
  run_nft_list   — nft list ruleset
  check_tcp_wild        — TCP reachability check; arg="-H <host> -p <port>"
  check_tcp_wild_sudo   — same with sudo (run on monitoring server via proxy to test from monitoring VLAN)
  check_http_wild       — HTTP check; arg=full check_http flag string
  check_http_wild_sudo  — same with sudo
  check_bgp_state       — BGP session state (sudo)
  check_ifconfig_wild   — ifconfig-based interface check; arg=flags
  check_ip_wild         — ip-based interface check; arg=flags
  check_network         — network utilisation (nagios); arg=hostname

## Process checks
  check_procs           — process count check; arg="<warn> <crit> <process-pattern>"
  check_procs_wild      — check_procs with arbitrary flags; arg=full flag string
  check_ps              — process presence check; arg=process name

## TLS / certificate checks
  check_ssl_cert        — check_ssl_cert with sudo; arg=flags
  check_ssl_cert_wild   — check_ssl_cert with arbitrary flags; arg=full flag string

## Ceph cluster (run on ceph nodes or monitoring hosts with keyring access)
  check_ceph_health     — overall cluster health (excludes MDS_SLOW_REQUEST)
  check_ceph_mds_slow_request — MDS slow request check
  check_ceph_mon        — monitor health; arg=optional flags
  check_ceph_mgr        — manager health
  check_ceph_df         — cluster disk usage (WARN 70%, CRIT 85%)
  check_ceph_osd_df     — per-OSD disk usage
  check_ceph_osd        — OSD health; arg=optional flags
  check_ceph_mds        — MDS health; arg=optional flags
  check_ceph_read_balance — read balance; arg=optional flags
  run_ceph_wild         — read-only ceph CLI; arg e.g. "osd df" "pg stat" "health detail"
                          "osd tree" "fs status" "log last 50" "tell osd.0 dump_ops_in_flight"
  check_ceph_mounts     — ceph mount health; arg=optional flags
  check_nfs_onode       — NFS onode check (WARN at 90% disk)
  check_mtu_ceph        — all non-loopback, non-subinterface ifaces are MTU 9000

## DRBD
  check_drbd   — DRBD replication state
  check_drbd9  — DRBD9 replication state (newer kernel module)

## Database
  check_psql_tablesize     — PostgreSQL table size check
  check_postgres_wild      — check_postgres.pl with custom flags; arg=full flag string
  check_postgres_connections — PostgreSQL connection count; arg=threshold
  check_db_connection      — generic DB connectivity check (sudo); arg=flags

## Web / application servers
  check_apache        — Apache status check
  check_apache_port   — Apache on a specific port; arg=port number
  check_apache_wild   — Apache with custom flags; arg=full flag string
  check_http_proxy    — HTTP proxy check; arg=flags
  check_logstash      — Logstash queue health; arg=host
  check_elasticsearch — Elasticsearch cluster health; arg="<host> <node>"
  check_elasticsearch_wild — same with arbitrary flags

## Miscellaneous service checks
  check_haproxy         — HAProxy stats (sudo); arg=flags
  check_fail2ban_jail   — fail2ban jail status (sudo); arg=jail name
  check_mount           — mount point check; arg=path
  check_mount_ro        — read-only mount check; arg=path
  check_raid            — software RAID status (sudo)
  check_ipmi            — IPMI sensor status
  check_ipmi_sdr_ok     — IPMI SDR sensor OK check (sudo)
  check_idrac_status    — iDRAC hardware health
  check_kvm             — KVM virtualisation check (sudo)
  check_syncthing       — Syncthing replication status (sudo); arg=flags
  check_service         — systemd service check (sudo); arg=service name
  check_systemd_service — systemd service check; arg=service name
  check_systemd_service_uptime — service uptime check; arg=service name
  check_updates         — pending package updates
  check_yum             — YUM/RPM pending updates (EL)
  check_backup          — backup recency check (sudo)
  check_fileage         — file age check (sudo)
  check_file_age        — file age check; arg=flags
  check_lsof            — open file count per process (sudo); arg=process name
  check_lsmod           — kernel module presence; arg=module name
  check_connections     — TCP connection count check; arg=threshold
  check_lsi_raid        — LSI/MegaRAID RAID status (sudo); arg=flags
  check_prometheus      — Prometheus health
  check_prometheus_targets — Prometheus scrape target health
  check_icinga          — Icinga2 process health
  check_iptables        — iptables rule validation
  run_gorpe_proxy       — proxy a gorpe command to another host (used internally by gorpe_run_via_proxy)
"""


@mcp.tool()
def gorpe_list_commands() -> str:
    """Return the full catalog of available gorpe commands with descriptions and usage guidance.

    Call this when you are unsure which gorpe command to use for a given task,
    or when the user asks about available commands or server health checks.
    Returns plain text with intent→command mappings, arg usage, and proxy routing info.
    """
    return _COMMAND_CATALOG


@mcp.tool()
async def gorpe_run(host: str, command: str, arg: str = "") -> str:
    """Run a gorpe command on a remote host using the server's mTLS client certificate.

    Call gorpe_list_commands() first if you are unsure which command to use.
    host: FQDN or IP of the target host.
    command: gorpe command name — see gorpe_list_commands() for the full catalog.
      Common: run_puppet_agent, run_puppet_agent_noop, check_journalctl_puppet,
      check_journalctl_puppet_agent, check_journalctl_unit, check_systemctl_status_unit,
      check_listening_ports, check_w, run_df, run_free, run_ps, run_journalctl_wild,
      run_cat_wild, run_tail_wild, run_grep_wild, run_ls_wild, run_stat_wild,
      run_ss_wild, run_ip_addr, run_nft_list, run_dmesg.
    arg: argument for commands that take $ARG$ (e.g. unit name for check_journalctl_unit).
      Leave empty for arg-less commands.

    Returns JSON: {"exit_code": N, "output": "...", "exit_meaning": "..."}.
    Exit code notes for run_puppet_agent:
      0 = no changes (catalog already applied)
      2 = changes applied successfully (NOT an error)
      4 = resource failures
      6 = changes applied AND failures
    exit_code 3 for any command = transport/connection error.
    """
    user = _current_user.get()
    denied = _check_access(user, host)
    if denied:
        logger.warning("[%s] gorpe_run DENIED host=%s command=%s: %s", user, host, command, denied)
        return json.dumps({"exit_code": 3, "output": denied})
    logger.info("[%s] gorpe_run host=%s command=%s arg=%r", user, host, command, arg or "")
    loop = asyncio.get_running_loop()
    rc, output = await loop.run_in_executor(None, functools.partial(_run, host, command, arg or None, None))
    logger.info("[%s] gorpe_run host=%s command=%s -> exit %d", user, host, command, rc)
    return _make_result(rc, output, command)


@mcp.tool()
async def gorpe_run_via_proxy(monitoring_server: str, target_host: str, command: str, arg: str = "") -> str:
    """Run a gorpe command via a monitoring-server proxy (run_gorpe_proxy hop).

    Use when direct port 5667 access from this server to the target is blocked by a firewall.
    The request hops: this server → monitoring server gorpe → target gorpe.

    monitoring_server: FQDN of a host that has port 5667 access to the target and has
      run_gorpe_proxy in its gorpe.yaml commands list.
    target_host: FQDN or IP of the actual target.
    command: gorpe command on the target (same options as gorpe_run).
    arg: optional argument for the target command.

    Returns JSON: {"exit_code": N, "output": "...", "exit_meaning": "..."}.
    """
    user = _current_user.get()
    denied = _check_access(user, target_host)
    if denied:
        logger.warning("[%s] gorpe_run_via_proxy DENIED host=%s command=%s: %s", user, target_host, command, denied)
        return json.dumps({"exit_code": 3, "output": denied})
    logger.info("[%s] gorpe_run_via_proxy proxy=%s host=%s command=%s arg=%r",
                user, monitoring_server, target_host, command, arg or "")
    loop = asyncio.get_running_loop()
    rc, output = await loop.run_in_executor(None, functools.partial(_run, target_host, command, arg or None, monitoring_server))
    logger.info("[%s] gorpe_run_via_proxy proxy=%s host=%s command=%s -> exit %d",
                user, monitoring_server, target_host, command, rc)
    return _make_result(rc, output, command)


def main():
    parser = argparse.ArgumentParser(description="Gorpe MCP server")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--transport", default="streamable-http")
    parser.add_argument("--port", type=int, default=8083)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    config = _load_config(args.config)

    global _cert, _key, _ca, _access_policy
    _cert, _key, _ca = _resolve_paths(config)
    _access_policy = {
        user: entry["allowed_hosts"]
        for user, entry in config.get("access_policy", {}).items()
    }
    if _access_policy:
        for user, patterns in _access_policy.items():
            logger.info("access policy: %s restricted to %s", user, ", ".join(patterns))

    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    for path, label in [(_cert, "cert"), (_key, "key"), (_ca, "ca")]:
        if not os.path.exists(path):
            logger.warning("%s path not found: %s", label, path)
        else:
            logger.info("using %s: %s", label, path)

    host = config.get("host", args.host)
    port = config.get("port", args.port)

    app = _ClientLogMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
