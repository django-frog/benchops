# Changelog

All notable changes to BenchOps. Run `benchops version` to see your installed
version, whether a newer one is available, and what changed.

## [Unreleased]

## [0.21.1] - 2026-11-01

### Fixed
- `execute` rejects anything that isn't a dotted Python method path before connecting, and points bench commands like `list-apps` to their method (`frappe.get_installed_apps`) instead of failing remotely with a confusing `NameError`.

## [0.21.0] - 2026-11-01

### Added
- `connection_type = "ssm"` now runs everything — commands, captured output, file uploads, `logs -f` with Ctrl+C — inside an AWS Session Manager shell session. No sshd, SSH keys or port forwarding: it needs only the IAM permission behind `aws ssm start-session --target <instance>`, and commands run as the user your Session Manager preferences set (or via `sudo -n -u <user>` from the session user, which `server check` verifies).
- `server add` for SSM servers asks which AWS profile to use (or takes the aws-vault one), shows the AWS identity it resolved to, then lists the instances to pick from.

### Changed
- SSH tunneled through Session Manager (the 0.20.0 behaviour of `ssm`) is now `connection_type = "ssm-ssh"`; `auth setup-keys` applies to it only. Servers added as `ssm` with 0.20.0 switch to the shell session and no longer use their key.
- A failed remote command raises the same error (`BenchOpsCommandError`) on every transport.
- Access-denied hints name the SSM document actually used, and `ssm:DescribeInstanceInformation` is shown as needing resource `*`.

### Fixed
- The connection-type prompt showed its default as `ConnectionType.ssh` (Python 3.14).

## [0.20.0] - 2026-11-01

### Added
- `benchops server check <alias>`: tests the connection step by step — Session Manager plugin, AWS credentials (shows the identity), SSM registration and agent status, the port-forwarding session, SSH login and the bench directory — and says what to fix at the step that fails, including the exact IAM permission AWS refused.
- `benchops server add --connection-type ssm` without `--instance-id` lists the instances registered with SSM (name, platform, agent status) to pick from, and no longer asks for a host.
- aws-vault support for SSM and S3 backup sources: under `aws-vault exec` the injected credentials are used even when the server names an `aws_profile`; outside it, a profile without credentials of its own gets them from `aws-vault export`.

### Changed
- SSM connections no longer need the AWS CLI, only the Session Manager plugin: BenchOps starts the session itself and terminates it when the command finishes. Previously the plugin could be left running and the session open until its idle timeout.
- SSH connections send keepalives, so long silent remote commands (e.g. `bench migrate`) are not cut off by idle timeouts.
- AWS errors (access denied, agent offline, unknown instance, expired credentials) are explained with the fix instead of being shown raw.

### Fixed
- A deploy refused because another deploy held the lock deleted that deploy's in-flight state on the server, making it fail (or half-apply) when confirmed.
- Re-running `server add` for an existing server dropped its hooks and private key.
- `auth setup-keys` failed for users whose primary group is not named after them, and on Debian/Ubuntu, where `/bin/sh` has no `pipefail`.

## [0.19.0] - 2026-10-20

### Added
- `benchops restore <source> --site <site> --local`: restore into the bench on this machine (the current directory, or `--bench PATH`). Downloads with your AWS credentials, with a progress bar, into a temporary folder inside the bench; sizes and the dump's gzip stream are verified before anything changes. Every option of a server restore works the same way.
- `benchops local add-hook|edit-hooks|clear-hooks|list-hooks post-restore`: commands run after a local restore, kept apart from every server's hooks.

### Changed
- `benchops restore` takes the server as an option: `benchops restore <source> --site <site> --server <alias>` instead of `benchops restore <server> <source> ...`. The 0.18.0 form prints the new one.

## [0.18.0] - 2026-10-20

### Added
- `benchops restore <server> <source> --site <site>`: restore a site from a backup that Frappe's S3 Backup Settings uploaded. The server downloads it straight from S3 through short-lived presigned URLs signed with your AWS credentials, so it needs no AWS access and the data never passes through your machine.
- Replace an existing site (type its name to confirm; a safety backup is taken first unless `--skip-safety-backup`) or create one with `--new-site`. `--from-site` restores another site's backup (e.g. production's onto staging), `--at` picks a backup by timestamp, `--no-files` restores only the database, `--admin-password` resets Administrator.
- Downloads are checked against the sizes in S3 and with `gzip -t` before anything changes; the downloads are removed afterwards, also on failure. Signed URLs and passwords never appear on the terminal.
- `benchops backup source add|list|remove` (S3 or S3-compatible buckets, AWS profile, region looked up automatically) and `benchops backup list <source> [--site]`.
- `post-restore` hook phase, e.g. `bench --site {site} migrate` or disabling the scheduler on a restored production copy.

## [0.17.0] - 2026-10-20

### Added
- `benchops clone <project>`: clone every repository of a project in parallel (`-j`, default 4), from public hosts and private GitHub / GitHub Enterprise, GitLab, Gitea / Forgejo or any git host.
- Projects: `benchops project add|list|show|remove`, from options or a shareable TOML manifest; repositories listed by path or URL, discovered from an org / group (`group`, `match`), or both. `benchops clone` also accepts a manifest file directly.
- `benchops git login|whoami|hosts|logout`: per-host tokens, verified before saving and kept in the system keyring (or `BENCHOPS_GIT_TOKEN_<HOST>`). Tokens reach git as an HTTPS header scoped to their host — never in URLs, argv or `.git/config` — and are masked in all output.
- Role-aware plan before cloning: your access (read / write / admin) per repository from the host, `min_access` per repository, token scope and expiry warnings, and a `git ls-remote` check that the branch exists and git accepts the token.
- Conflict safety: folders holding another repository, non-empty non-git folders and two repositories mapped to one folder are reported and never overwritten; clones land via a temporary folder and an atomic rename; a lock file prevents concurrent runs into the same directory.
- Repository URLs with embedded credentials are rejected in projects and manifests (use `benchops git login`); an unwritable target directory is a clear error; a failure during the clone phase always sets a non-zero exit code.
- `--dry-run`, `--update` (fetch + fast-forward only when clean), `--retry-failed`, `--only` / `--skip`, `--branch`, `--depth`, `--partial` (blob-less clone), `--protocol ssh|https`, `--persist-credentials` (credential helper backed by the keyring).

## [0.16.0] - 2026-10-07

### Added
- Deploy ledger on the server: every deployed uncommitted file is a *draft* with its owner (git email), label and date. Drafts from every developer stay staged on the server until committed, so "Changes to be committed" there lists all pending drafts.
- `deploy --label TASK-142`: tag drafts with a task; a redeploy keeps the existing label.
- `benchops sync`: bring the server up to your latest commits without shipping staged files. Drafts those commits contain become clean. Builds only when the commits touch frontend source.
- `benchops status` groups pending drafts by label and developer, flags drafts older than 7 days, marks drafts edited on the server, shows drafts already committed on your machine, and lists hand edits.

### Changed
- Replacing your own untouched draft is no longer an overlap; replacing another developer's draft is, and the plan names them and the label.
- Deploying exactly another developer's draft takes it over (the plan says so); its label carries over.
- Earlier deploys' drafts stay staged on the server instead of moving to "not staged".


## [0.15.0] - 2026-10-06

### Added
- `benchops --version` and `benchops version`: the installed version, the latest version on PyPI, and the release notes. Every command also prints a one-line notice when a newer version is available (checked at most once a day; set `BENCHOPS_NO_UPDATE_CHECK=1` to turn it off).

## [0.14.0] - 2026-10-06

### Changed
- Deploys ship your commits plus what you staged with `git add` — nothing else. Unstaged edits and untracked files stay on your machine.
- On the server only the deployed files are written. Work done directly on the server stays on disk and stays visible in its `git status`, where your deploy shows as "Changes to be committed". HEAD moves to your commit, on your branch.
- The "HEAD moved outside BenchOps" check is replaced by a history check against the server's actual HEAD: a `git pull` on the server to a commit you also have no longer blocks a deploy.
- Servers left on a 0.12/0.13 snapshot commit are converted automatically on the next deploy.

### Added
- Overlap warning: files you're deploying that have different uncommitted changes on the server are listed in red and only overwritten after an explicit yes (default no), or `--overwrite` with `--yes`. Staging's versions are backed up first.
- Warning and a "build anyway?" prompt when frontend files have unstaged changes.
- `benchops status <app> <server> [--files]`: branch, last deploy, staged / unstaged / untracked changes on the server, and deployed files changed there since.

### Removed
- `deploy --adopt` (no longer needed).

## [0.13.0] - 2026-10-06

### Fixed
- Blank SPA pages after deploy: a frappe-ui/Vite build committed before it was gitignored left the rebuilt hashed assets out of the deploy.
- `{app}` / `{site}` placeholders were not filled in for pre-local hooks.

### Added
- Every deploy builds locally (`yarn install` + `bench build --app <app>`); `--skip-build` ships the existing build.
- Build outputs (`public/dist`, plus SPA outputs detected from `vite.config.*` or set in `[tool.benchops] build_outputs`) are shipped from the build and replace the server's copy wholesale.
- Deploys stop before uploading when a build output is missing or built HTML references assets that don't exist.
- `bench --site all clear-cache` runs on the server after build outputs ship.

## [0.12.0] - 2026-10-05

### Changed
- Deploys are git-based: only missing commits travel (as a git bundle), files deleted locally are removed on the server, and the result is verified against your tree.

### Added
- Per-app deploy lock (`--break-lock`), history checks (`--force`), a deploy plan with confirmation (`--yes`), and backups of overwritten staging edits.

## [0.11.0] - 2026-10-05

### Fixed
- Locally built assets were not served after deploy: the app's entries in `sites/assets/assets.json` / `assets-rtl.json` are now merged into the server's manifests and Frappe's `assets_json` cache is cleared.

## [0.10.0] - 2026-09-05

### Added
- `benchops logs`: tail `frappe.log`, `web.error.log` and `worker.error.log` live.
- `benchops execute`: run one whitelisted method with `bench execute`, never an interactive shell.

## [0.9.0] - 2026-09-04

### Added
- Native Windows support: local hooks, file permissions and the SSM tunnel use OS-appropriate implementations.
- Test suite.

## [0.8.0] - 2026-09-04

### Added
- AWS SSM transport (`connection_type = "ssm"`): SSH over a Session Manager tunnel, with port 22 closed.
- `benchops auth setup-keys`: install a BenchOps-managed SSH key on an instance through SSM.

## [0.7.0] - 2026-09-04

### Changed
- Runner, auth and config refactored to support multiple transports.

## [0.6.0] - 2026-08-15

### Added
- `benchops install` and `benchops uninstall` (one-time hooks).
- Lifecycle hooks with an interactive editor, and `{site}` / `{app}` placeholders.
