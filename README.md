# BenchOps

A robust Command Line Interface (CLI) tool designed to streamline and synchronize local Frappe development environments with remote servers. BenchOps automates the deployment pipeline, offering extensible lifecycle command hooks, automated archiving, SFTP transfers, and dynamic multi-site target resolution.

## Features

* **Automated Code Syncing:** Compresses your local Frappe app into a tarball, transfers it securely via SFTP, and extracts it directly into the remote bench, replacing manual SSH copying.
* **Build Locally, Ship Assets:** Every deploy carries the app's locally built bundles *and* its entries from the bench's `assets.json`/`assets-rtl.json` manifests, merging them into the remote bench and invalidating Frappe's cached manifest — so the remote never needs to run `bench build`.
* **Extensible Lifecycle Hooks:** Define custom shell commands to execute at specific stages of the deployment pipeline (`pre-local`, `pre-remote`, `post-remote`, `install-remote`, `uninstall-remote`).
* **Embedded Multiline Editor:** Write and manage your deployment scripts directly in the terminal using a built-in interactive editor (powered by `prompt_toolkit`).
* **Dynamic Target Resolution:** Use the `{site}` and `{app}` placeholders in your hook configurations to dynamically target specific Frappe tenant environments and applications during execution.
* **Secure Credential Management:** Store authentication methods locally, supporting both SSH private keys and passwords securely.
* **Zero-Trust Access via AWS SSM:** Connect to EC2 instances with port 22 closed entirely, by tunneling SSH through an AWS Systems Manager Session Manager port-forwarding session (`connection_type = "ssm"`).
* **SSM-Bootstrapped Trust:** `benchops auth setup-keys` generates a BenchOps-managed ed25519 keypair and installs the public half onto the instance via an SSM RunCommand — no pre-existing SSH access required.
* **Cross-Platform by Design:** Works natively on Linux, macOS, and Windows (PowerShell/cmd.exe) — no WSL required. Local hooks, file permissions, and the SSM tunnel all dispatch to OS-appropriate implementations under the hood.
* **Live Log Tailing:** `benchops logs` streams `frappe.log`, `web.error.log`, and/or `worker.error.log` from the bench in real time, over either transport.
* **Safe Ad-Hoc Execution:** `benchops execute` runs a single whitelisted Python method via `bench execute` — no interactive shell is ever opened on the target instance.

## Installation

BenchOps is built with Python and utilizes `uv` for fast package management. You can install it globally using `uv tool`:

```bash
uv tool install benchops

```

## Getting Started

### 1. Initialize the Configuration

Bootstrap the local configuration structure (`~/.benchops/config.toml`):

```bash
benchops init

```

### 2. Register a Remote Server

Add your target remote server environment (e.g., a staging server). BenchOps supports two ways of reaching it:

**Direct SSH** (`connection_type=ssh`, the default — requires port 22 open):

```bash
benchops server add \
  --alias staging \
  --host 3.7.212.100 \
  --port 22 \
  --user akwad \
  --bench-path /home/akwad/dev-bench-03

```

**AWS SSM** (`connection_type=ssm` — no open port 22 needed; traffic tunnels through Session Manager):

```bash
benchops server add \
  --alias staging \
  --host 3.7.212.100 \
  --port 22 \
  --user akwad \
  --bench-path /home/akwad/dev-bench-03 \
  --connection-type ssm \
  --instance-id i-0123456789abcdef0 \
  --aws-region me-south-1 \
  --aws-profile my-aws-profile

```

`--aws-profile`/`--aws-region` are optional — omit them to use your default AWS CLI credentials/region. `--host`/`--port` are still recorded for reference; the SSM path only actually uses `--instance-id` and `--port` (as the sshd port on the instance to forward to).

### 3. Set Authentication

For `connection_type=ssh`, securely link your local SSH private key (or password) for authentication:

```bash
benchops server set-auth staging

```

For `connection_type=ssm`, see [Bootstrapping SSH Trust via AWS SSM](#bootstrapping-ssh-trust-via-aws-ssm) below — it establishes an SSH key automatically, without needing SSH access to already exist.

## Bootstrapping SSH Trust via AWS SSM

For `connection_type=ssm` servers, there's a chicken-and-egg problem: SSH-based deployment needs a key on the box, but you have no SSH access to put one there (that's the point of closing port 22). `benchops auth setup-keys` solves this by authenticating entirely through the SSM control plane (IAM), never over SSH:

```bash
benchops auth setup-keys staging

```

This:

1. Generates (or reuses) a BenchOps-managed ed25519 keypair at `~/.benchops/keys/benchops_ed25519`.
2. Runs an SSM `AWS-RunShellScript` command on the target instance that appends the public key to the configured `--user`'s `~/.ssh/authorized_keys`, with correct ownership, `700`/`600` permissions, and an SELinux `restorecon` pass (RHEL).
3. Wires the private key into the server's config automatically (equivalent to `server set-auth`), so `deploy`/`install`/`uninstall` work immediately afterward.

**Prerequisites for this to work:**

* The EC2 instance's IAM role must have the `AmazonSSMManagedInstanceCore` policy (or equivalent) and the SSM Agent must be running and registered — check under **AWS Console → Systems Manager → Fleet Manager** that the instance shows as "Online".
* Your local AWS credentials/profile need `ssm:SendCommand` and `ssm:GetCommandInvocation` on that instance.
* The [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) and the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) must both be installed and on `PATH` — required for the `ssm setup-keys` prerequisite check and for every subsequent `connection_type=ssm` connection.

## Managing Hooks and Placeholders

BenchOps allows you to define custom actions that run before, during, and after your deployment, as well as one-time installation actions. Use the built-in embedded editor to write your scripts.

**Dynamic Placeholders:**
You can write generic hooks that apply to any deployment by using these placeholders:

* `{site}`: Automatically replaced by the `--site` flag passed in the CLI.
* `{app}`: Automatically replaced by the local app name passed in the CLI.

**Available Lifecycle Phases:**

* `pre-local`: Runs on your local machine before archiving (e.g., `bench build --app {app}`).
* `pre-remote`: Runs on the remote server before the new code is extracted (e.g., enabling maintenance mode).
* `post-remote`: Runs on the remote server after extraction (e.g., database migrations, clearing cache).
* `install-remote`: Runs exactly once when using the `install` command (e.g., `bench --site {site} install-app {app}`).
* `uninstall-remote`: Runs exactly once when using the `uninstall` command (e.g., `bench --site {site} uninstall-app {app}`).

**Editing Hooks:**
Open the interactive terminal editor for a specific phase:

```bash
benchops server edit-hooks staging install-remote

```

*(Press `Esc` then `Enter` to save and exit the editor).*

## Executing Commands

By passing the optional `--site` flag to the core commands, BenchOps will automatically resolve the placeholders in your hooks.

### Deploying an Application

Synchronize your local code and run the deployment hooks (`pre-local`, `pre-remote`, `post-remote`):

```bash
benchops deploy custom_app staging --site test-16.akwad.qa

```

Run it from your local bench root. Assets are always built locally and shipped — never built on the remote:

1. `pre-local` hooks run — this is where `bench build --app {app}` belongs.
2. BenchOps reads this app's entries from the local `sites/assets/assets.json` and `assets-rtl.json`, failing the deploy if the bench has never been built or the manifest references a bundle missing from `dist/`.
3. The app (including `public/dist/`) is archived, transferred, and extracted into the remote `apps/` directory.
4. The app's manifest entries are merged into the remote manifests — other apps' entries are left untouched, and this app's stale entries are replaced — and the `assets_json` key is cleared from `redis_cache`, exactly as `bench build` does.
5. `post-remote` hooks run (e.g., `bench --site {site} migrate` and `bench restart`).

If the local manifest has no entries for the app (a backend-only app), step 4 is skipped and the remote manifest is left as-is.

### Installing an Application (One-Time)

Run the isolated `install-remote` hooks for a brand new application:

```bash
benchops install custom_app staging --site test-16.akwad.qa

```

### Uninstalling an Application (One-Time)

Run the isolated `uninstall-remote` hooks to remove an application from a site:

```bash
benchops uninstall custom_app staging --site test-16.akwad.qa

```

## Operating a Deployed Site

Once a server is registered and authenticated (`server add` + `server set-auth`/`auth setup-keys`), two commands cover the day-to-day operational work that doesn't need a full deploy — checking what's happening in the logs, and running one-off Frappe methods. Both work identically whether the server is `connection_type=ssh` or `connection_type=ssm`; the tunnel, if any, is set up and torn down automatically around each command.

### Tailing Logs (`benchops logs`)

Stream bench logs from the server in real time, exactly like `tail -f` run locally.

```bash
# Tail frappe.log, web.error.log, and worker.error.log together (the default)
benchops logs staging

# Tail just one
benchops logs staging --type web.error.log

```

`--type` accepts `frappe.log`, `web.error.log`, or `worker.error.log`. Press **Ctrl+C** to stop — this closes the remote log stream (and, for `connection_type=ssm`, tears down the SSM tunnel) rather than leaving anything running on the server.

Log files are read from `<bench_path>/logs/<name>` on the server — the standard location for a Frappe bench — so no extra configuration is needed beyond the server's existing `bench_path`.

### Running a One-Off Command (`benchops execute`)

Run a single Python method against a site via `bench execute`, without ever getting shell access to the box:

```bash
# No arguments
benchops execute staging --site demo.local frappe.clear_cache

# With positional arguments (--args, a JSON array)
benchops execute staging --site demo.local frappe.client.delete_doc \
  --args '["Error Log", "abc123"]'

# With keyword arguments (--kwargs, a JSON object)
benchops execute staging --site demo.local frappe.client.set_value \
  --kwargs '{"doctype": "User", "name": "admin@example.com", "fieldname": "enabled", "value": 1}'

```

* `--args`/`--kwargs` are validated as JSON *before* connecting — a malformed value fails immediately with a local error, not a confusing remote one.
* If the method returns a value, `execute` tries to parse it as JSON and pretty-prints it; otherwise it prints the raw text as-is.
* If the remote command fails, the remote Python traceback is printed and `benchops` exits with the same non-zero status code `bench execute` did — safe to use in scripts that check the exit code.
* There is deliberately no `benchops shell`/`benchops ssh` command. `execute` is the sanctioned way to run something on a site; it runs exactly one bounded, non-interactive command per invocation and nothing more.

## CLI Command Reference

### Global Commands

* `benchops init`: Initializes the BenchOps configuration.
* `benchops deploy <app_name> <server_alias> [--site <site_name>]`: Deploys a local Frappe app to a remote server.
* `benchops install <app_name> <server_alias> --site <site_name>`: Executes the install-remote hooks.
* `benchops uninstall <app_name> <server_alias> --site <site_name>`: Executes the uninstall-remote hooks.
* `benchops logs <server_alias> [--type frappe.log|web.error.log|worker.error.log]`: Tails bench logs in real time (Ctrl+C to stop).
* `benchops execute <server_alias> --site <site_name> <method> [--args <json>] [--kwargs <json>]`: Runs a single Python method via `bench execute` — no interactive shell.

### Server Management (`benchops server`)

* `add [--connection-type ssh|ssm] [--instance-id ...] [--aws-profile ...] [--aws-region ...]`: Interactively add or update a remote server profile.
* `list`: Display a table of all configured servers, connection type/instance ID, and hook counts.
* `set-auth <alias>`: Configure SSH key or password authentication (for `connection_type=ssh`).
* `remove <alias>`: Delete a server profile and its credentials.
* `edit-hooks <alias> <phase>`: Open the multiline editor to define lifecycle commands.
* `add-hook <alias> <phase> <cmd>`: Quickly append a single command to a hook phase.
* `clear-hooks <alias> <phase>`: Wipe all commands for a specific lifecycle phase.

### Trust Bootstrapping (`benchops auth`)

* `setup-keys <alias>`: Generate/reuse the BenchOps SSH keypair and install it on a `connection_type=ssm` server via an SSM RunCommand — see [Bootstrapping SSH Trust via AWS SSM](#bootstrapping-ssh-trust-via-aws-ssm).

## Testing Your Changes

A quick guide to verifying the SSM integration and cross-platform fixes, roughly in order of "fastest to run" → "needs real AWS infrastructure."

### 1. Run the automated test suite

No AWS account or real server needed — everything is mocked.

```bash
uv sync --dev
uv run pytest -v

```

You should see all tests pass, including:

* `tests/test_local_runner_windows.py` — confirms `LocalRunner` uses `shell=True` with an unmodified command string on Windows (preserving `C:\path\like\this`), and the original `shlex.split` + `shell=False` behavior on Linux/macOS.
* `tests/test_secure_file_permissions.py` — confirms `secure_file()` calls `chmod` on POSIX and `icacls` on Windows, including its failure paths (missing `icacls`, missing `USERNAME`, non-zero exit).
* `tests/test_ssm_proxy_windows.py` — confirms `RemoteRunner.via_ssm()` refuses to proceed (and never spawns a process) if `aws` or `session-manager-plugin` isn't on `PATH`.

To exercise a single file: `uv run pytest tests/test_ssm_proxy_windows.py -v`.

### 2. Smoke-test the SSH path (no AWS required)

If you have any Linux box reachable over SSH (a spare VM, a Docker container running `sshd`, or an existing dev server), this exercises the full pipeline without touching AWS at all:

```bash
uv run benchops init
uv run benchops server add --alias test-ssh --host <ip> --port 22 --user <user> --bench-path /home/<user>/bench
uv run benchops server set-auth test-ssh
uv run benchops server list

```

Confirm `~/.benchops/config.toml` was created with `600` permissions (`ls -l ~/.benchops/config.toml` on Linux/macOS; on Windows, `icacls %USERPROFILE%\.benchops\config.toml` should show only your user with `(F)`).

### 3. Smoke-test the SSM path (needs a real, SSM-managed EC2 instance)

This is the part that actually needs AWS. Prerequisites:

* An EC2 instance with the SSM Agent registered (**Systems Manager → Fleet Manager** shows it "Online") and an IAM role with `AmazonSSMManagedInstanceCore`.
* Your local AWS credentials configured (`aws configure` or `AWS_PROFILE`) with `ssm:SendCommand`, `ssm:GetCommandInvocation`, and `ssm:StartSession` on that instance.
* AWS CLI v2 and the Session Manager plugin installed — verify with:

  ```bash
  aws --version
  session-manager-plugin

  ```

  If either is missing, `benchops` should now fail immediately with a clear `Missing required tool(s) for SSM connections: ...` error rather than a cryptic traceback — worth deliberately testing by temporarily renaming/hiding one of the binaries.

Then:

```bash
uv run benchops server add --alias staging-ssm --host <any-placeholder> --port 22 \
  --user ec2-user --bench-path /home/ec2-user/frappe-bench \
  --connection-type ssm --instance-id i-0123456789abcdef0 --aws-region <region>

uv run benchops auth setup-keys staging-ssm

```

`setup-keys` should print progress and finish with "SSH trust established." Confirm on the instance itself (via the EC2 Instance Connect console, or `aws ssm start-session --target i-...`) that `~/.ssh/authorized_keys` for that user now contains a line ending in `benchops_ed25519`, owned by that user, with `600` permissions.

Then exercise the actual tunnel:

```bash
uv run benchops deploy <your_app> staging-ssm --site <your-site>

```

While that's running, in another terminal you can confirm the tunnel is real:

```bash
ps aux | grep "ssm start-session"        # Linux/macOS
Get-Process aws                          # Windows PowerShell

```

You should see an `aws ssm start-session ... --document-name AWS-StartPortForwardingSession ...` process for the duration of the deploy, and it should disappear once the command finishes (confirming `RemoteRunner.close()` tears it down).

### 4. Windows-specific manual checks

This sandbox can't run real Windows, so the automated tests mock the OS-specific branches — worth confirming for real on an actual Windows machine before rolling this out to Windows teammates:

* Add a `pre_local_commands` hook containing a raw Windows path and confirm it survives, e.g. `benchops server add-hook staging pre-local "echo C:\Users\%USERNAME%\Desktop"` — the path should print intact, not with backslashes stripped.
* After `benchops init` and `benchops auth setup-keys`, run `icacls %USERPROFILE%\.benchops\config.toml` and `icacls %USERPROFILE%\.benchops\keys\benchops_ed25519` — both should list only your user with Full Control, no inherited entries.
* Confirm `connection_type=ssm` actually connects on Windows now (this was the critical bug the last round of fixes targeted) — the earlier `paramiko.ProxyCommand`-based approach could never work on Windows at all; the rewritten port-forwarding approach should.

### 5. Smoke-test `logs` and `execute`

Against either a `connection_type=ssh` or `connection_type=ssm` server that's already authenticated:

```bash
benchops logs staging

```

Confirm output starts streaming immediately, then **press Ctrl+C** — this is the one behavior in this release verified only by reading Fabric/Invoke's source (no live SSH server was available to test against directly), so it's worth confirming for real: `benchops` should stop and print `Stopped tailing logs.` within a second or two, not hang. If it does hang, that specific mechanism (a remote pty translating the forwarded Ctrl+C byte into SIGINT) isn't behaving as expected on that server/OpenSSH version and is worth reporting.

```bash
benchops execute staging --site <your-site> frappe.utils.get_installed_apps

```

Should print a pretty-printed JSON array. Then try a deliberately bad method to confirm error handling:

```bash
benchops execute staging --site <your-site> frappe.this_method_does_not_exist

```

Should print a remote Python traceback and exit non-zero (check with `echo $?` / `$LASTEXITCODE`) — not a raw stack trace from `benchops` itself.
