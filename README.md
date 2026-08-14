# benchops

A cross-platform CLI tool that synchronizes local Frappe development
environments with remote servers.

`benchops` automates the repetitive "ship my app to the server" workflow: it
builds your app locally, packages it into a compressed archive, uploads it to
the target server over SSH, extracts it into the remote bench, and runs the
Frappe housekeeping commands to apply the changes — all with output streamed to
your terminal in real time.

## Features

- **Server management** — define remote servers in a local TOML config
  (`~/.benchops/config.toml`) with a friendly interactive CLI.
- **Secure authentication** — store server passwords in your OS credential
  store (`keyring`) or use an SSH private key.
- **Real-time streaming** — every local and remote command streams its stdout
  and stderr to your terminal as it runs.
- **Lean transfers** — source tarballs exclude `.git`, `__pycache__`,
  `node_modules`, and `*.pyc` to keep uploads small.
- **One-command deploy** — build, archive, upload, extract, migrate, and clear
  cache with a single invocation.

## Requirements

- Python `>= 3.14.6`
- [uv](https://docs.astral.sh/uv/) (recommended) or `pip`
- SSH access to the target server (password or key)

## Installation

```bash
git clone <your-repo-url> benchops
cd benchops
uv sync
```

Install the CLI into your environment so the `benchops` command is available:

```bash
uv run pip install -e .
# or, if you built a distribution:
uv build && uv pip install --python .venv/bin/python dist/benchops-0.1.0-py3-none-any.whl
```

Verify it works:

```bash
benchops --help
```

## Quick start

### 1. Initialize the configuration

```bash
benchops init
```

This creates `~/.benchops/config.toml` with an empty `[servers]` table.

### 2. Register a server

```bash
benchops server add
```

You will be prompted for:

| Field       | Description                              |
| ----------- | ---------------------------------------- |
| `alias`     | Short name used to reference the server  |
| `host`      | Hostname or IP address                   |
| `port`      | SSH port (default: `22`)                 |
| `user`      | SSH username                             |
| `bench_path`| Remote path to the bench directory       |

You can also pass everything non-interactively:

```bash
benchops server add --alias dev1 --host 192.168.1.10 --port 22 --user frappe --bench-path /home/frappe/bench
```

List your configured servers:

```bash
benchops server list
```

### 3. Configure authentication

```bash
benchops server set-auth dev1
```

You will be asked to choose between:

- **`password`** — prompted securely (hidden input, double confirmation) and
  stored in your system keyring.
- **`key`** — path to your SSH private key (default `~/.ssh/id_rsa`), stored in
  the server's configuration.

### 4. Deploy

Run `benchops deploy` from the directory that contains your app (either inside
the local bench's `apps/` folder or directly):

```bash
benchops deploy <app_name> <server_alias>
```

For example:

```bash
benchops deploy myapp dev1
```

The deploy pipeline:

1. **Config & auth** — loads the server settings and its credentials.
2. **Local build** — runs `bench build --app <app_name>` in the app's parent
   directory.
3. **Archive** — compresses `<app_name>` into a `.tar.gz` (excluding `.git`,
   `__pycache__`, `node_modules`, and `*.pyc`) in a temporary directory.
4. **Connect** — opens an SSH connection to the server.
5. **Transfer & extract** — uploads the tarball and extracts it into
   `{bench_path}/apps`.
6. **Remote operations** — runs `bench --site all migrate` and
   `bench clear-cache` inside `{bench_path}`.
7. **Cleanup** — closes the connection and reports success.

## Configuration

All server definitions live in `~/.benchops/config.toml`:

```toml
[servers]
dev1 = {host = "192.168.1.10", port = 22, user = "frappe", bench_path = "/home/frappe/bench", private_key_path = "/home/mohammad/.ssh/id_rsa"}
staging = {host = "staging.example.com", port = 22, user = "deploy", bench_path = "/srv/bench"}
```

Passwords are **not** stored in this file — they are kept in the operating
system's credential store via `keyring`.

## Development

```bash
uv sync            # install dependencies
uv build           # build wheel + sdist
uv publish         # upload to PyPI (set UV_PUBLISH_TOKEN first)
```

## License

MIT
