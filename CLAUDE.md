# gorpe-mcp — Claude Code instructions

## What this is

A FastMCP server (`streamable-http` transport) that talks to [gorpe](https://github.com/xorpaul/gorpe) daemons over mTLS HTTPS, so MCP clients can trigger puppet runs, tail journals and run ad-hoc checks on fleet hosts. Tools: `gorpe_run` (direct), `gorpe_run_via_proxy` (via a `run_gorpe_proxy` hop when a firewall blocks direct access) and `gorpe_list_commands` (command catalog). All gorpe calls use the MCP server's own client cert, not the caller's.

## Repository layout

- `server.py` — the entire server (FastMCP, tools, config loading, main)
- `pyproject.toml` — package config; registers the `gorpe-mcp` console script pointing at `server:main`
- `config.example.yaml` — annotated example; copy to `config.yaml` to run locally
- `requirements.txt` — mirrors `pyproject.toml` dependencies
- `CHANGELOG.md` — Keep a Changelog; add entries under `[Unreleased]`

No sub-packages. `py-modules = ["server"]` in pyproject.toml covers the flat layout.

## Public repository

This repo is public. Use generic examples only (`web01.example.com`, `alice`), never real hostnames, usernames, internal URLs or site-specific command names. Site-specific command sets belong in the deployment's `catalog` / `catalog_file` config, not in the built-in `_COMMAND_CATALOG`.

## Adding or changing tools

All tools live directly in `server.py`. The pattern:

```python
@mcp.tool()
async def my_tool(host: str, command: str, arg: str = "") -> dict:
    """Docstring is what the MCP client sees as the tool description — keep it precise."""
    return await asyncio.get_running_loop().run_in_executor(
        None, functools.partial(_run_and_shape, host, command, arg or None, None, command))
```

`_run()` makes the mTLS request to the gorpe daemon. `_make_result()` returns a plain dict (FastMCP serialises it — don't `json.dumps` it yourself, or callers get JSON inside a string), adds `exit_meaning` for commands in `_PUPPET_CMDS`, and applies the output shaping (`grep`, `exclude`, `head_lines`, `tail_lines`, `max_bytes` default 64 KiB, `keep`) via `_shape_output()`. Add a command to `_PUPPET_CMDS` if it uses puppet's `--detailed-exitcodes` semantics.

## Command catalog

`_COMMAND_CATALOG` is a curated, human-readable description of generic gorpe commands with an intent → command mapping at the top — not a raw dump of a gorpe.yaml. Keep that structure when editing it. `catalog` (inline) or `catalog_file` in the config replaces it at startup (`_load_catalog()`).
