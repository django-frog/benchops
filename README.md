# BenchOps

A robust Command Line Interface (CLI) tool designed to streamline and synchronize local Frappe development environments with remote servers. BenchOps automates the deployment pipeline, offering extensible lifecycle command hooks, automated archiving, SFTP transfers, and dynamic multi-site target resolution.

## Features

* **Automated Code Syncing:** Compresses your local Frappe app into a tarball, transfers it securely via SFTP, and extracts it directly into the remote bench, replacing manual SSH copying.
* **Extensible Lifecycle Hooks:** Define custom shell commands to execute at specific stages of the deployment pipeline (`pre-local`, `pre-remote`, `post-remote`, `install-remote`, `uninstall-remote`).
* **Embedded Multiline Editor:** Write and manage your deployment scripts directly in the terminal using a built-in interactive editor (powered by `prompt_toolkit`).
* **Dynamic Target Resolution:** Use the `{site}` and `{app}` placeholders in your hook configurations to dynamically target specific Frappe tenant environments and applications during execution.
* **Secure Credential Management:** Store authentication methods locally, supporting both SSH private keys and passwords securely.

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

Add your target remote server environment (e.g., a staging server):

```bash
benchops server add \
  --alias staging \
  --host 3.7.212.100 \
  --port 22 \
  --user akwad \
  --bench-path /home/akwad/dev-bench-03

```

### 3. Set Authentication

Securely link your local SSH private key (or password) for authentication:

```bash
benchops server set-auth staging

```

## Managing Hooks and Placeholders

BenchOps allows you to define custom actions that run before, during, and after your deployment, as well as one-time installation actions. Use the built-in embedded editor to write your scripts.

**Dynamic Placeholders:**
You can write generic hooks that apply to any deployment by using these placeholders:

* `{site}`: Automatically replaced by the `--site` flag passed in the CLI.
* `{app}`: Automatically replaced by the local app name passed in the CLI.

**Available Lifecycle Phases:**

* `pre-local`: Runs on your local machine before archiving (e.g., compiling assets).
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

## CLI Command Reference

### Global Commands

* `benchops init`: Initializes the BenchOps configuration.
* `benchops deploy <app_name> <server_alias> [--site <site_name>]`: Deploys a local Frappe app to a remote server.
* `benchops install <app_name> <server_alias> --site <site_name>`: Executes the install-remote hooks.
* `benchops uninstall <app_name> <server_alias> --site <site_name>`: Executes the uninstall-remote hooks.

### Server Management (`benchops server`)

* `add`: Interactively add or update a remote server profile.
* `list`: Display a table of all configured servers and hook counts.
* `set-auth <alias>`: Configure SSH key or password authentication.
* `remove <alias>`: Delete a server profile and its credentials.
* `edit-hooks <alias> <phase>`: Open the multiline editor to define lifecycle commands.
* `add-hook <alias> <phase> <cmd>`: Quickly append a single command to a hook phase.
* `clear-hooks <alias> <phase>`: Wipe all commands for a specific lifecycle phase.
