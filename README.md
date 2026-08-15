# BenchOps

A robust Command Line Interface (CLI) tool designed to streamline and synchronize local Frappe development environments with remote servers. BenchOps automates the deployment pipeline, offering extensible lifecycle command hooks, automated archiving, SFTP transfers, and dynamic multi-site target resolution.

## Features

* **Automated Code Syncing:** Compresses your local Frappe app into a tarball, transfers it securely via SFTP, and extracts it directly into the remote bench, replacing manual SSH copying.
* **Extensible Lifecycle Hooks:** Define custom shell commands to execute at specific stages of the deployment pipeline (`pre-local`, `pre-remote`, `post-remote`).
* **Embedded Multiline Editor:** Write and manage your deployment scripts directly in the terminal using a built-in interactive editor (powered by `prompt_toolkit`).
* **Dynamic Site Target Resolution:** Use the `{site}` placeholder in your hook configurations to dynamically target specific Frappe tenant environments during deployment.
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

## Managing Deployment Hooks

BenchOps allows you to define custom actions that run before, during, and after your deployment. Instead of editing configuration files manually, use the built-in embedded editor to write your scripts.

**Available Lifecycle Phases:**

* `pre-local`: Runs on your local machine before archiving (e.g., compiling assets).
* `pre-remote`: Runs on the remote server before the new code is extracted (e.g., enabling maintenance mode).
* `post-remote`: Runs on the remote server after extraction (e.g., database migrations, clearing cache).

**Editing Hooks:**
Open the interactive terminal editor for a specific phase:

```bash
benchops server edit-hooks staging pre-local

```

*(Press `Esc` then `Enter` to save and exit the editor).*

**Viewing Configurations:**
To see an overview of your configured servers and the number of hooks attached to each phase:

```bash
benchops server list

```

## Deploying an Application

Execute the deployment pipeline from inside your local bench (or `apps` directory).

By passing the optional `--site` flag, BenchOps will automatically resolve the `{site}` placeholder in any of your remote hooks to target a specific tenant environment.

```bash
benchops deploy custom_app staging --site test-16.akwad.qa

```

**Deployment Pipeline Flow:**

1. Executes `pre-local` hooks (e.g., `bench build --app custom_app`).
2. Archives the local app directory into a `.tar.gz` payload.
3. Connects via SSH and executes `pre-remote` hooks.
4. Transfers the tarball via SFTP and extracts it into the remote bench's `apps/` directory.
5. Executes `post-remote` hooks with dynamic site interpolation (e.g., `bench --site test-16.akwad.qa migrate`).
6. Cleans up temporary artifacts and closes connections safely.

## CLI Command Reference

### Global Commands

* `benchops init`: Initializes the BenchOps configuration.
* `benchops deploy <app_name> <server_alias> [--site <site_name>]`: Deploys a local Frappe app to a remote server.

### Server Management (`benchops server`)

* `add`: Interactively add or update a remote server profile.
* `list`: Display a table of all configured servers and hook counts.
* `set-auth <alias>`: Configure SSH key or password authentication.
* `remove <alias>`: Delete a server profile and its credentials.
* `edit-hooks <alias> <phase>`: Open the multiline editor to define lifecycle commands.
* `add-hook <alias> <phase> <cmd>`: Quickly append a single command to a hook phase.
* `clear-hooks <alias> <phase>`: Wipe all commands for a specific lifecycle phase.
