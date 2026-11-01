# BenchOps

A robust Command Line Interface (CLI) tool designed to streamline and synchronize local Frappe development environments with remote servers. BenchOps automates the deployment pipeline, offering git-verified code syncing, extensible lifecycle command hooks, and dynamic multi-site target resolution.

## Features

* **Ship What You Staged:** A deploy ships your commits plus exactly what you staged with `git add` — nothing else. On the server only those files are written; work people do directly on the server is left on disk and stays visible in its `git status`, where your deploy shows as "Changes to be committed". Each deployed file becomes your *draft* on the server — optionally labelled with a task ID — and stays staged until `benchops sync` brings the commit that contains it. Files changed on both sides are reported up front and are only overwritten after an explicit yes (default: no), with a backup.
* **Conflict-Safe on Shared Servers:** A per-app deploy lock, a history check that stops a deploy when the server is on a commit you don't have, and `benchops status` to see every pending draft by task and developer — without logging in.
* **Build Locally, Ship Assets:** Every deploy carries the app's locally built bundles *and* its entries from the bench's `assets.json`/`assets-rtl.json` manifests, merging them into the remote bench and invalidating Frappe's cached manifest — so the remote never needs to run `bench build`.
* **Extensible Lifecycle Hooks:** Define custom shell commands to execute at specific stages of the deployment pipeline (`pre-local`, `pre-remote`, `post-remote`, `install-remote`, `uninstall-remote`).
* **Embedded Multiline Editor:** Write and manage your deployment scripts directly in the terminal using a built-in interactive editor (powered by `prompt_toolkit`).
* **Dynamic Target Resolution:** Use the `{site}` and `{app}` placeholders in your hook configurations to dynamically target specific Frappe tenant environments and applications during execution.
* **Secure Credential Management:** Store authentication methods locally, supporting both SSH private keys and passwords securely.
* **Zero-Trust Access via AWS SSM:** Connect to EC2 instances with port 22 closed entirely, by tunneling SSH through an AWS Systems Manager Session Manager port-forwarding session (`connection_type = "ssm"`).
* **SSM-Bootstrapped Trust:** `benchops auth setup-keys` generates a BenchOps-managed ed25519 keypair and installs the public half onto the instance via an SSM RunCommand — no pre-existing SSH access required.
* **aws-vault Friendly:** Point a server at an aws-vault profile, or run under `aws-vault exec`; `benchops server check` diagnoses the whole SSM path step by step.
* **Cross-Platform by Design:** Works natively on Linux, macOS, and Windows (PowerShell/cmd.exe) — no WSL required. Local hooks, file permissions, and the SSM tunnel all dispatch to OS-appropriate implementations under the hood.
* **Bulk Cloning with Access Checks:** `benchops clone <project>` clones every repository of a project in parallel — public or on a private GitHub, GitLab, Gitea or plain git host. Your token is checked first and each repository shows your role (read / write / admin); repositories you can't read are skipped with the reason, and existing folders are never overwritten.
* **Restore Sites from S3:** `benchops restore` restores a site from the backups Frappe's S3 Backup Settings upload — on a server or on your local bench, replacing a site (after a safety backup) or creating a new one. A server downloads straight from S3 through short-lived presigned URLs, so it needs no AWS credentials of its own.
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
  --user akwad \
  --bench-path /home/akwad/dev-bench-03 \
  --connection-type ssm \
  --aws-region me-south-1 \
  --aws-profile my-aws-profile

```

Leave out `--instance-id` and BenchOps lists the instances registered with SSM (name, platform, agent status) to pick from. `--aws-profile`/`--aws-region` are optional — omit them to use your default AWS credentials/region. `--port` is sshd's port on the instance (default 22); `--host` is only a label for SSM servers.

**aws-vault** works either way: set `--aws-profile` to the aws-vault profile and BenchOps fetches credentials with `aws-vault export` when the profile has none of its own (MFA/passphrase prompts appear in the terminal), or run any command under `aws-vault exec <profile> -- benchops ...`.

Re-running `server add` for an existing alias updates it and keeps its hooks and key — e.g. to move a server from `ssh` to `ssm`.

Then check the whole path end to end — every step says what to fix if it fails:

```bash
benchops server check staging

```

```
✓ Session Manager plugin installed — session-manager-plugin 1.2.835.0
✓ AWS credentials — arn:aws:iam::123456789012:user/you in me-south-1
✓ Instance registered with SSM — develop.example.com, Red Hat Enterprise Linux 9.7, agent Online
✓ Port-forwarding session (ssm:StartSession) — 127.0.0.1:41873 → i-0123456789abcdef0:22
✓ SSH login — akwad@develop.example.com with key ~/.benchops/keys/benchops_ed25519
✓ Bench directory — /home/akwad/dev-bench-03
```

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
* Your AWS identity needs, on that instance: `ssm:SendCommand` and `ssm:GetCommandInvocation` (for `setup-keys`), and `ssm:StartSession` — on the instance *and* on the `AWS-StartPortForwardingSession` document — plus `ssm:TerminateSession` (for every connection). `ssm:DescribeInstanceInformation` is optional; it powers the instance picker and the registration step of `server check`.
* The [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) must be installed and on `PATH`. The AWS CLI is **not** needed: BenchOps starts the session itself and ends it explicitly when the command finishes, so no session is left open until the idle timeout.

## Managing Hooks and Placeholders

BenchOps allows you to define custom actions that run before, during, and after your deployment, as well as one-time installation actions. Use the built-in embedded editor to write your scripts.

**Dynamic Placeholders:**
You can write generic hooks that apply to any deployment by using these placeholders:

* `{site}`: Automatically replaced by the `--site` flag passed in the CLI.
* `{app}`: Automatically replaced by the local app name passed in the CLI.

**Available Lifecycle Phases:**

* `pre-local`: Runs on your local machine before the build (e.g., `bench --site {site} export-fixtures --app {app}`). Don't add `bench build` here — every deploy builds the app itself.
* `pre-remote`: Runs on the remote server before the new code is extracted (e.g., enabling maintenance mode).
* `post-remote`: Runs on the remote server after extraction (e.g., database migrations, clearing cache).
* `install-remote`: Runs exactly once when using the `install` command (e.g., `bench --site {site} install-app {app}`).
* `uninstall-remote`: Runs exactly once when using the `uninstall` command (e.g., `bench --site {site} uninstall-app {app}`).
* `post-restore`: Runs on the remote server after `benchops restore --server` (e.g., `bench --site {site} migrate`, `bench --site {site} disable-scheduler`). For `restore --local`, use `benchops local add-hook post-restore ...` instead.

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

Run it from your local bench root. The app must be the root of its own git repository, and on the server `apps/<app>` must be a git checkout of it (as `bench get-app` creates) with `git` installed.

**What ships:** your commits, plus what you staged with `git add` — your *index*. Unstaged edits and untracked files stay on your machine; the plan lists them under "Not shipped". Stage exactly what you want on the server (`git add -p` stages single hunks).

**What a deploy does:**

1. `pre-local` hooks run.
2. The app is built locally: `yarn install` in the app (when it has a `package.json`), then `bench build --app <app>`, which also runs the app's own `build` script (how frappe-ui/Vite SPA frontends are built). If frontend files have changes that are *not staged*, BenchOps warns you — the build compiles what is on disk, so it would include source that isn't deployed — and asks whether to build anyway (default yes). Answer no to keep the server's current build. BenchOps then checks every build output exists and built HTML only references `/assets/<app>/…` files that exist.
3. Your commits and index are captured as a *staged commit* (tree = your index, parent = your HEAD) with git plumbing only — your branch, index, working tree and stash are never touched.
4. On the server BenchOps takes the app's deploy lock and checks the server's commit is in your history. A commit made on the server, or commits you haven't pulled, stop the deploy (`--force` overrides and backs the old HEAD up). A server that just ran `git pull` to a commit you also have is fine.
5. One package is uploaded: a git bundle with your staged commit and the commits the server is missing, the build outputs (when they changed), and this app's entries from the local `sites/assets/assets*.json`.
6. A **deploy plan** is shown: your branch and the server's, how many commits and staged files ship, what is not shipped, the files that will be written, and — in red — every **overlap**: a file you're deploying that has *different* uncommitted changes on the server, either **another developer's draft** (named, with its label) or a **hand edit** made on the server. Overlaps need an explicit yes to "Overwrite these files on staging with your version?" (default **no**); with `--yes` they stop the deploy unless you also pass `--overwrite`. Not overlaps: a file the server already has with exactly your content, and **your own earlier draft** that nobody touched since. The plan also lists drafts you take over from someone else (you deploy exactly their content), drafts that become committed, and the other drafts and hand edits it leaves alone.
7. `pre-remote` hooks run, then only the files in your deploy are written (or deleted), the server's HEAD moves to your commit on your branch, and your staged files become your **drafts**: recorded in the server's ledger (`apps/<app>/.git/benchops/ledger.json`) with your git email, the `--label` if given (a redeploy keeps the existing label), and the date, and staged there. Overlapping files are first backed up under `refs/benchops/overwritten/<timestamp>`. Everything else on the server — other developers' drafts, hand edits, new files — stays on disk. Build outputs replace the server's copy wholesale, the asset manifests are merged, and `assets_json` is cleared from `redis_cache`.
8. The result is verified (HEAD is your commit; every deployed file matches your staged version on disk and in the index), the deploy record is written to `apps/<app>/.git/benchops/`, `bench --site all clear-cache` runs if build outputs were shipped, and `post-remote` hooks run (e.g. `bench --site {site} migrate` and `bench restart`). The lock is always released.

**Afterwards, `git status` on the server** reads naturally:

```
On branch develop
Changes to be committed:        ← pending drafts: deployed by any developer, not committed yet
Changes not staged for commit:  ← hand edits made on the server (including drafts edited there since)
Untracked files:                ← files created on the server
```

A draft stays on the server, staged, until a commit contains it — there is no automatic withdrawing. If nothing would change, BenchOps says so and exits. If a file the deploy writes changes on the server while the plan is on screen (e.g. a Desk save), nothing is applied and you re-run. Servers deployed by 0.12/0.13 (HEAD on a "benchops snapshot" commit) are converted automatically on the next deploy, and the last 0.14/0.15 deploy's staged files become that developer's drafts.

**Options:** `--label TASK-142` (tag the drafts, e.g. with a task ID), `--yes`/`-y` (skip confirmation), `--overwrite` (allow overlaps with `--yes`), `--force` (deploy over a server commit that isn't in your history), `--break-lock` (take over a stale lock), `--skip-build` (ship the existing build without rebuilding).

**After approval — `benchops sync`:** once the drafts are approved, commit and push them, then

```bash
benchops sync basma dev-bench-02 --site basma.akwad.qa
```

`sync` runs the same pipeline with **commits only** — your staged files are not shipped. It moves the server to your latest commit; drafts that commit now contains exactly become clean (no longer staged). If you tidied a draft up before committing, your untouched draft is simply replaced by the committed version — no overlap warning; someone else's draft or a hand edit still asks first. Other pending drafts stay as they are. It builds only when the commits touch frontend source (`<app>/public/` or a Vite project), and runs the same hooks as a deploy (e.g. migrate and restart).

**What's on the server — `benchops status`:**

```bash
benchops status basma dev-bench-02          # counts
benchops status basma dev-bench-02 --files  # every file
```

```
  Pending drafts (deployed, not committed yet):
    TASK-142                            Mohammad Hamdan   4 file(s)   2 days ago
    (no label)                          Ali               1 file(s)   9 days ago   ⚠ stale
  Committed on your machine             2 file(s) — run 'benchops sync'
  Hand edits on staging                 1 file(s)
```

It shows the server's branch and commit, the last deploy or sync, every pending draft grouped by label and developer (drafts older than 7 days are flagged stale, drafts edited on the server since are marked), drafts your machine has already committed, and hand edits. It's read-only and doesn't take the lock.

**Build outputs and SPA frontends.** Build outputs are paths a build regenerates. They are shipped straight from the build and never through git, even if an old build was committed. `<app>/public/dist` is always one. For a frappe-ui/Vite frontend, the `outDir` and `indexHtmlPath` in its `vite.config.*` (at the app root or one level below, e.g. `frontend/`) are detected automatically. To declare them explicitly, or to turn detection off with an empty list, use the app's `pyproject.toml`:

```toml
[tool.benchops]
build_outputs = ["basma/public/calendar", "basma/www/calendar.html"]
```

If a build output is tracked in git, the plan warns you. Untrack it (`git rm -r --cached <path>`) and gitignore it, so a rebuild never shows up as a code change.

> Desk changes that only live in the database (Custom Fields, Property Setters, Client Scripts) are not files and are not synced. Export them as fixtures (`bench --site {site} export-fixtures --app {app}`, e.g. as a `pre-local` hook), then stage the exported files so they ship.

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

## Bulk Cloning a Project

A *project* is a named group of repositories you clone together — for example every app of one ERP implementation, so a new bench or a new teammate is set up with one command.

### 1. Save a token for each private git host

```bash
benchops git login gitlab.akwad.qa --provider gitlab     # prompts for the token (hidden)
echo "$TOKEN" | benchops git login github.com --token-stdin
benchops git whoami gitlab.akwad.qa                      # user, scopes, expiry
```

Tokens are verified before they are saved and live in the system keyring (service `benchops-git`, separate from server passwords). On a headless host without a keyring, set `BENCHOPS_GIT_TOKEN_<HOST>` instead, e.g. `BENCHOPS_GIT_TOKEN_GITLAB_AKWAD_QA`. A token is never put in a URL, a command line or `.git/config`: git receives it as an HTTP header for that host only, over HTTPS only. Public repositories need no token; with `--protocol ssh` your SSH keys clone and the token is used only to check access.

Required token permissions: GitHub `repo` (classic) or *Contents: read* (fine-grained); GitLab `read_api` + `read_repository`; Gitea *repository: read*.

### 2. Define the project

From options:

```bash
benchops project add akwad-erp --host gitlab.akwad.qa --branch develop \
    --repo erp-team/akwad_core --repo erp-team/akwad_hr \
    --repo https://github.com/frappe/hrms
benchops project add akwad-all --host gitlab.akwad.qa --provider gitlab --group erp-team --match 'akwad_*'
```

Or from a manifest your team commits (no secrets in it):

```toml
# akwad-erp.toml
host = "gitlab.akwad.qa"
protocol = "https"        # or "ssh"
branch = "develop"        # optional default for every repo
group = "erp-team"        # optional: also discover repos from this group
match = "akwad_*"         # optional: only discovered repos matching this glob

[[repos]]
path = "erp-team/akwad_core"

[[repos]]
url = "https://github.com/frappe/hrms"
branch = "version-15"
dir = "hrms"              # folder name (default: the repo name)
min_access = "write"      # skip it for people who can only read
```

```bash
benchops project add akwad-erp --manifest akwad-erp.toml   # or clone it directly:
benchops clone akwad-erp.toml --into ~/frappe-bench/apps
```

### 3. Clone

```bash
benchops clone akwad-erp --into ~/frappe-bench/apps --dry-run   # plan + your access, nothing written
benchops clone akwad-erp --into ~/frappe-bench/apps -j 6
```

Before anything is written, BenchOps checks every token, asks each host what your role is on each repository, confirms with `git ls-remote` that git itself is allowed in and that the branch exists, and inspects every target folder. The plan lists each repository's branch, your access and the action:

| Action | When |
|---|---|
| `clone` | Folder is missing or empty |
| `skip` | Already cloned from the same repository (any protocol), or your role is below the repo's `min_access` |
| `update` | Already cloned and `--update` given: fetch, then fast-forward only if the work tree is clean and the branch hasn't diverged |
| `no access` | The host says you can't read it (private repos usually look "not found"), or git refused the token — not counted as a failure |
| `conflict` | Folder holds a different repository, is a non-empty non-git folder or a file, or two repositories would land in the same folder (set `dir` on one) |
| `error` | Branch doesn't exist, or the host is unreachable |

Conflicts are never resolved by overwriting. Each repository is cloned into a temporary folder and renamed into place only when complete, so an interrupted or failed clone leaves nothing half-written, and a folder that appears while cloning is left untouched. A lock file stops two `benchops clone` runs from writing into the same directory at once (a lock left by a crashed run on the same machine is taken over automatically). One repository failing never stops the others; transient network errors are retried twice. The exit code is non-zero if anything failed or conflicted, and `benchops clone akwad-erp --retry-failed` redoes only those.

Useful options: `--only core,hr` / `--skip 'legacy_*'` (folder, path or glob), `--branch version-15`, `--depth 1` (shallow), `--partial` (full history, file contents fetched on demand — much faster for large repos; sets up the credential helper so later fetches can authenticate), `--persist-credentials` (later `git pull` uses the stored token through `benchops git credential`, still nothing in plain text), `--protocol ssh`, `--yes`.

## Restoring a Site from S3

`benchops restore` restores a site on a configured server (`--server`) or on the bench on your machine (`--local`) from the backups that Frappe's **S3 Backup Settings** upload (`<bucket>/<YYYYMMDD_HHMMSS>/<YYYYMMDD_HHMMSS>-<site>-database.sql.gz`, `-files.tar`, `-private-files.tar`). Several sites can share a bucket; backups are matched by site name.

### 1. Add the bucket as a backup source

```bash
benchops backup source add prod --bucket akwad-erp-backups --aws-profile prod-readonly
benchops backup list prod --site erp.akwad.qa
```

`source add` checks it can list the bucket before saving. Your AWS identity needs `s3:ListBucket` and `s3:GetObject` (and `s3:GetBucketLocation` when you don't pass `--aws-region`). For MinIO, Wasabi and other S3-compatible stores pass `--endpoint-url`. No AWS credentials are stored by BenchOps.

### 2. Restore

```bash
# Replace staging's copy of the site with its newest backup
benchops restore prod --site erp.akwad.qa --server staging

# Production's backup of 19 Oct as a new site on staging, database only
benchops restore prod --site uat.akwad.qa --from-site erp.akwad.qa --new-site --at 20261019 --no-files --server staging

# The same site on your own machine: run from your bench root, or pass --bench
benchops restore prod --site erp.akwad.qa --new-site --local
benchops restore prod --site erp.akwad.qa --local --bench ~/frappe-bench
```

Pass exactly one of `--server <alias>` or `--local`.

What happens:

1. The backup is picked in S3: the newest one with a database dump, or the newest whose timestamp starts with `--at`.
2. The target is checked: the site must exist (or must not, with `--new-site`) and free disk space is compared with the download. A server needs `curl`; locally, the folder must be a bench (`sites/`, `apps/frappe`) and `bench` must be on your PATH.
3. A plan is shown. Replacing a site asks you to **type its name**; `--yes` skips the question.
4. The files are downloaded into a temporary folder inside the bench. A server downloads them straight from S3 through presigned URLs that expire after an hour; a local restore downloads them with your AWS credentials, with a progress bar. Sizes are checked against S3 and the dump's gzip stream is verified before anything changes.
5. A safety backup of the site being replaced is taken into `sites/<site>/private/backups` (skip with `--skip-safety-backup`), or the site is created with `bench new-site` (`--new-site`). Then `bench --site <site> restore` runs, followed by the `post-restore` hooks: the server's, or the local ones for `--local`.
6. The downloaded files are removed, also when a step fails.

**Local post-restore hooks** are kept apart from every server's:

```bash
benchops local add-hook post-restore "bench --site {site} migrate"
benchops local add-hook post-restore "bench --site {site} disable-scheduler"
benchops local list-hooks          # also: edit-hooks, clear-hooks
```

Local hooks run without a shell, like `pre-local` hooks: one command per line, no `&&` or pipes.

**Database root password:** if `root_password` is in the bench's `common_site_config.json` (or the site's config), nothing is asked. Otherwise you're prompted for it, or it's read from `BENCHOPS_DB_ROOT_PASSWORD` (required with `--yes`). It's never printed, but it's passed to `bench` as an argument, so it's visible in the process list while bench runs.

**Administrator:** the restored site keeps the backup's Administrator password unless you pass `--admin-password`.

The apps installed in the backup must also be installed on the bench, ideally at the same versions; add `bench --site {site} migrate` as a `post-restore` hook when they differ. Encrypted backups (Frappe's `backup_encryption_key`) aren't supported yet.

## CLI Command Reference

### Global Commands

* `benchops init`: Initializes the BenchOps configuration.
* `benchops --version`: Prints the installed version.
* `benchops version [--all]`: Shows the installed version, whether a newer one is on PyPI (with the upgrade command), and the release notes for your version (`--all`: every version). Every other command also prints a one-line notice on stderr when a newer version is available — checked at most once a day; set `BENCHOPS_NO_UPDATE_CHECK=1` to turn it off. See [CHANGELOG.md](CHANGELOG.md).
* `benchops deploy <app_name> <server_alias> [--site <site_name>]`: Deploys your commits and staged changes to a remote server.
* `benchops sync <app_name> <server_alias> [--site <site_name>]`: Brings the server up to your latest commits; drafts those commits contain become committed.
* `benchops status <app_name> <server_alias> [--files]`: Shows what the server runs, pending drafts by label and developer, and hand edits.
* `benchops install <app_name> <server_alias> --site <site_name>`: Executes the install-remote hooks.
* `benchops uninstall <app_name> <server_alias> --site <site_name>`: Executes the uninstall-remote hooks.
* `benchops logs <server_alias> [--type frappe.log|web.error.log|worker.error.log]`: Tails bench logs in real time (Ctrl+C to stop).
* `benchops execute <server_alias> --site <site_name> <method> [--args <json>] [--kwargs <json>]`: Runs a single Python method via `bench execute` — no interactive shell.

* `benchops restore <source> --site <site> (--server <alias> | --local [--bench PATH]) [--from-site ...] [--at latest|STAMP] [--new-site] [--no-files] [--admin-password ...] [--skip-safety-backup] [--yes]`: Restores a site from an S3 backup — see [Restoring a Site from S3](#restoring-a-site-from-s3).

* `benchops clone <project|manifest.toml> [--into DIR] [-j N] [--dry-run] [--update] [--retry-failed] [--only ...] [--skip ...]`: Clones a project's repositories in parallel after checking your access — see [Bulk Cloning a Project](#bulk-cloning-a-project).

### Server Management (`benchops server`)

* `add [--connection-type ssh|ssm] [--instance-id ...] [--aws-profile ...] [--aws-region ...]`: Interactively add or update a remote server profile (for `ssm`, pick the instance from a list). Updating keeps the server's hooks and key.
* `check <alias>`: Test the connection step by step — Session Manager plugin, AWS credentials, SSM registration, port-forwarding session, SSH login, bench directory — and explain what to fix.
* `list`: Display a table of all configured servers, connection type/instance ID, and hook counts.
* `set-auth <alias>`: Configure SSH key or password authentication (for `connection_type=ssh`).
* `remove <alias>`: Delete a server profile and its credentials.
* `edit-hooks <alias> <phase>`: Open the multiline editor to define lifecycle commands.
* `add-hook <alias> <phase> <cmd>`: Quickly append a single command to a hook phase.
* `clear-hooks <alias> <phase>`: Wipe all commands for a specific lifecycle phase.

### Trust Bootstrapping (`benchops auth`)

* `setup-keys <alias>`: Generate/reuse the BenchOps SSH keypair and install it on a `connection_type=ssm` server via an SSM RunCommand — see [Bootstrapping SSH Trust via AWS SSM](#bootstrapping-ssh-trust-via-aws-ssm).

### Git Hosts (`benchops git`)

* `login <host> [--provider github|gitlab|gitea|generic] [--api-url ...] [--username ...] [--token-stdin]`: Verify a token and save it to the keyring.
* `whoami <host>`: Show the token's user, scopes and expiry.
* `hosts`: List git hosts and where each token comes from (keyring / env).
* `logout <host>`: Delete the token and host settings.

### Clone Projects (`benchops project`)

* `add <name> [--manifest file.toml | --host ... --repo ... --group ... --match ... --branch ... --protocol ...] [--replace]`: Define a project.
* `list` / `show <name>` / `remove <name>`: Inspect or delete projects (removing never touches cloned folders).

### Local Bench (`benchops local`)

* `add-hook post-restore <cmd>` / `edit-hooks post-restore` / `clear-hooks post-restore` / `list-hooks`: Commands run after `benchops restore --local`.

### Backups (`benchops backup`)

* `source add <name> --bucket ... [--prefix ...] [--aws-profile ...] [--aws-region ...] [--endpoint-url ...]`: Save an S3 backup source after checking it can be read.
* `source list` / `source remove <name>`: Inspect or delete sources (nothing in S3 is touched).
* `list <source> [--site <site>] [-n N]`: The newest backups, with the stamp to pass as `--at`.

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
* `tests/test_ssm.py` — credentials (aws-vault included), the error explanations, and the tunnel's lifecycle, driven by a fake `session-manager-plugin` run as a real subprocess: readiness, early exit, timeout, and that closing always terminates both the plugin and the SSM session.

To exercise a single file: `uv run pytest tests/test_ssm.py -v`.

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
* AWS credentials (a profile, aws-vault, or the default chain) with the permissions listed under [Bootstrapping SSH Trust via AWS SSM](#bootstrapping-ssh-trust-via-aws-ssm).
* The Session Manager plugin installed (`session-manager-plugin --version`).

Then:

```bash
uv run benchops server add --alias staging-ssm --connection-type ssm \
  --user ec2-user --bench-path /home/ec2-user/frappe-bench --aws-region <region>

uv run benchops auth setup-keys staging-ssm
uv run benchops server check staging-ssm

```

`setup-keys` should print progress and finish with "SSH trust established." Confirm on the instance itself (via the EC2 Instance Connect console, or `aws ssm start-session --target i-...`) that `~/.ssh/authorized_keys` for that user now contains a line ending in `benchops_ed25519`, owned by that user, with `600` permissions.

Then exercise the actual tunnel:

```bash
uv run benchops deploy <your_app> staging-ssm --site <your-site>

```

While that's running, in another terminal you can confirm the tunnel is real:

```bash
ps aux | grep session-manager-plugin                 # Linux/macOS
Get-Process session-manager-plugin                  # Windows PowerShell
aws ssm describe-sessions --state Active            # the session in SSM

```

You should see a `session-manager-plugin` process and an active session for the duration of the deploy; both should be gone once the command finishes (confirming `RemoteRunner.close()` stops the plugin and calls `TerminateSession`).

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
