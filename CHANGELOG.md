# Changelog

All notable changes to this project will be documented in this file.

## [v0.2.0] - 2026-11-11

### Added

- **Output shaping** for `gorpe_run` and `gorpe_run_via_proxy`: `grep`, `exclude`, `head_lines`, `tail_lines`, `max_bytes` (default 64 KiB) and `keep`, applied after the command ran. The result reports `truncated`, `total_lines`, `total_bytes` and, for an invalid regex, `filter_error`.
- **Separate `stderr`** in the result when the target gorpe sends stdout and stderr separately (JSON mode).
- **Site-specific command catalog**: `catalog` (inline) or `catalog_file` in `config.yaml` replaces the built-in catalog returned by `gorpe_list_commands()`.

### Changed

- Tools return a plain object instead of a JSON-encoded string, so callers no longer get JSON inside a string.

### Fixed

- `pyproject.toml` uses the `setuptools.build_meta` backend; the previous `setuptools.backends.legacy:build` doesn't exist, so the package couldn't be built or installed.
- Pin `mcp<2.0`: mcp 2.0 removed `FastMCP`.

## [v0.1.0] - 2026-09-18

### Added

- Initial release: `gorpe_run`, `gorpe_run_via_proxy` and `gorpe_list_commands` over mTLS HTTPS, per-user `access_policy`, caller logging behind a reverse proxy.
