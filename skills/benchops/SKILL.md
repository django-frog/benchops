---
name: benchops
description: Deploy, sync and inspect Frappe apps on shared staging servers with the benchops CLI. Use when the user asks to deploy or ship changes to staging (or a dev bench), sync after approval or a merge, check what is on staging or who has drafts there, run a Frappe method on a server, or restore a site from S3 backups. Do not use for production releases or for hand-editing files on a server.
---

# BenchOps for agents

Written for **benchops 0.23.x**. Run `benchops --version` once per session.
- On **0.22**, the skill works, but `skill install` doesn't add the permission rules yet. On **0.21 or older**, `--plan-only` and `--json` don't exist. Ask the user to upgrade (`uv tool upgrade benchops`). If they can't, preview by running the deploy without `--yes` and with `< /dev/null`: the plan prints, the prompt aborts with exit 1, and nothing is applied.
- On a **newer** version, run `benchops <command> --help` before relying on the flags below.

BenchOps moves a developer's git work from their local Frappe bench to a shared staging server. Several developers and the business team share staging. **Every deploy changes a shared environment, so the safety rules below are mandatory.**

## Mental model (read first)

- **deploy** ships your commits **plus exactly what is staged with `git add`** (the index). Unstaged edits and untracked files never leave the machine.
- What you deploy becomes your **draft** on staging: staged there, tagged with the developer's git email and an optional `--label` (a task ID), until a commit contains it.
- **sync** ships commits only (no staged files). After approval the developer commits and pushes, then runs sync, and their drafts become clean.
- **status** is read-only and takes no lock. It shows staging's branch and commit, the last deploy, pending drafts by label and developer, and hand edits made on staging.
- Edits made directly on staging and other developers' drafts are never removed silently. A deploy that would replace one reports it as an **OVERWRITE** (overlap) and stops unless someone says yes.

## Resolving targets: never guess

A deploy needs three values: **app**, **server alias** and **site**. Resolve them in this order and stop at the first match:

1. Values the user gave in this request.
2. A `BenchOps targets` table in the bench's `CLAUDE.md`, or in `~/.claude/CLAUDE.md`:
   ```markdown
   ## BenchOps targets
   | App   | Server       | Site            | Default |
   |-------|--------------|-----------------|---------|
   | basma | dev-bench-02 | basma.akwad.qa  | yes     |
   ```
3. The app: if exactly one app under `apps/` has staged changes (`git -C apps/<app> status --short`), use that app.
4. The server: `benchops server list` shows the configured aliases. Use it only when exactly one alias is plausible.

If any value is still ambiguous, **ask the user**. Do not pick a server or site by similarity of names. After the user answers, offer to add the row to the targets table.

Never read or print `~/.benchops/config.toml` in full, because it contains key paths and server details. `benchops server list` is the supported view.

## Where to run

Run benchops from the **bench root**: the directory that contains `apps/` and `sites/`. The app must be its own git repository at `apps/<app>`. Git commands about the app run in `apps/<app>`.

## Running commands from an agent

- Deploy and sync run a local `yarn install` and `bench build` and upload a package, so they take minutes. Give the Bash tool a long timeout (10 min) or run them in the background.
- The commands in this skill are bash. On Windows, Claude Code runs them in Git Bash, where `< /dev/null` works as shown.
- **Use `--json`** for `deploy`, `sync` and `status`. Stdout is then exactly one JSON document; the human-readable plan, progress and hook output go to stderr. Read the JSON for decisions. Use stderr only to explain things to the user or to diagnose a failure.
- `--json` never prompts, so `deploy` and `sync` need `--plan-only` (preview) or `--yes` (apply) with it.
- **Start every command with `benchops` itself.** Don't put an environment variable (`VAR=1 benchops ...`), a `cd ... &&`, `bash -c`, or a full path in front of it. Run it from the bench root as your working directory. Claude Code's permission rules match the command text, and read-only commands are only pre-approved when the command starts with `benchops`. The update notice goes to stderr, so it never mixes into the JSON.
- Add `< /dev/null` to every benchops command. If any command does prompt, it then aborts instead of hanging.
- `benchops skill install` adds permission rules to Claude Code's settings:
  - **deny:** `--force`, `--break-lock` and `restore`.
  - **ask:** `deploy`/`sync` with `--yes` or `-y`, `--overwrite`, `execute`, and anything that changes servers or configuration. The developer is asked in addition to your own confirmation. That's expected; don't try to avoid it.
  - **allow** (no prompt): `status`, `--plan-only`, `logs --lines` and the other read-only commands.
  If a command is **denied**, tell the user and give them the command to run themselves. Never retry it in another form (`bash -c`, a full path, other flags) to get past a rule.
- **Always pass `--lines N` to `benchops logs`** (for example `benchops logs <server> --type web.error.log --lines 200 < /dev/null`). That prints the last lines and exits. Without `--lines` it follows the logs until Ctrl+C, which you can't send. Don't wrap it in `timeout` either: on Windows, `timeout` can be a different program that only waits.
- Never run commands that open an editor or ask for secrets: `server edit-hooks`, `local edit-hooks`, `server set-auth`, `git login` without `--token-stdin`, and `server add` without every option. Give the command to the user to run themselves (`! benchops ...`).

## The deploy workflow

**1. Check the local state** (in `apps/<app>`):
```bash
git status --short
git log --oneline @{u}..HEAD 2>/dev/null   # unpushed commits
git fetch --quiet && git status -sb | head -1   # behind upstream?
```
- If **nothing is staged**, do not stage anything yourself. Show the user the modified files and ask which ones to ship. Stage only the files they name. Never use `git add -A` or `git add .`.
- If the branch is **behind its upstream**, tell the user to pull first. Staging refuses a deploy when its commit is not in your history.
- If the index has merge conflicts, stop and tell the user.

**2. Preview** with `--plan-only`. It builds, uploads and computes the plan, then stops. Nothing is applied and nothing is asked:
```bash
benchops deploy <app> <server> --site <site> --label <TASK> --plan-only --json < /dev/null
```
Check `outcome` in the JSON:
- `planned`: continue to step 3.
- `up_to_date`: nothing that staging lacks is staged. Report it and stop.
- `aborted` or `failed`: relay `message` and use the error table below. Do not retry with extra flags.

If `frontend_unshipped` is not empty, tell the user. Those frontend files have changes that won't ship, but the build compiled them anyway. Ask whether that's fine, or whether to apply with `--skip-build` instead, so staging keeps its current build. (`--skip-build` would then ship whatever build is already on disk.)

**3. Summarise the plan for the user** from `plan` in the JSON, in a few lines:
- `local` / `server`: the branches, and `commits_behind` (how many commits ship), plus `staged_files` and the label.
- `writes`: the number of files written on staging.
- **Every entry in `overlaps`, with its `reason` verbatim** (`<Name>'s deployed draft [label]`, `modified on staging`, `new file on staging` or `deleted on staging`).
- `takeover`: drafts that become the user's. `absorbed`: drafts that become committed.
- `warnings`: branch switch, commit not pushed, Frappe versions differ, detached HEAD.
- `suggested_steps` (`bench migrate`, `bench restart`, ...). Compare them with the server's post-remote hooks (`benchops server list` shows the hook counts).

**4. Ask for confirmation**, then apply with the same arguments, using `--yes` in place of `--plan-only`:
```bash
benchops deploy <app> <server> --site <site> --label <TASK> --yes --skip-build --json < /dev/null
```
`--skip-build` is safe here only because the preview just built the app and nothing changed since. Drop it if any file changed after the preview.

The plan is recomputed when you apply. If staging changed in between, the apply can stop with an overlap that wasn't in the preview. When that happens, go back to step 2.

- If the preview showed **no overlaps**, `--yes` is enough.
- If it showed **overlaps**, `--yes` alone stops the deploy, which is intended. Add `--overwrite` **only** when the user explicitly agrees to overwrite those named files in this conversation. If an overlap names another developer, tell the user to talk to that person first. Every overwritten file is backed up on staging under `refs/benchops/overwritten/<timestamp>`.

**5. Report** from the JSON:
- `outcome: applied`: report success, any `result.backups` and `result.warnings`, and that `post_remote_hooks` is `ok`.
- `outcome: failed` with `result.applied: true`: **staging did change**, but a later step failed (usually a post-remote hook such as migrate). Say this clearly, and show the end of stderr.

Suggest `benchops status <app> <server> --json` as the check afterwards.

## After approval: sync

When the user says a task is approved or merged:
1. Do **not** commit or push for them unless they ask. If they ask, commit only the task's files with a message that starts with the task ID, then push.
2. Preview with `benchops sync <app> <server> --site <site> --plan-only --json < /dev/null`. Summarise the plan as in step 3; `plan.absorbed` lists the drafts that become committed. Ask, then run it with `--yes --json`.
3. Confirm with `benchops status <app> <server> --json` that no entry in `drafts` still has the task's label.

## Read-only commands (safe to run without asking)

| Purpose | Command |
|---|---|
| What is on staging, by task and developer | `benchops status <app> <server> --json` (`drafts`, `hand_edits`, `committed_locally`, `lock`) |
| Preview a deploy or sync (builds and uploads, applies nothing) | `benchops deploy\|sync ... --plan-only --json` |
| Configured servers and hook counts | `benchops server list` |
| Diagnose connectivity (SSH, SSM, bench path) | `benchops server check <server>` |
| Version and release notes | `benchops version` |
| Backup sources and recent backups | `benchops backup source list`, `benchops backup list <source> --site <site>` |
| Recent errors | `benchops logs <server> --type web.error.log --lines 200 < /dev/null` |

## Commands that need explicit user approval each time

| Command | Why |
|---|---|
| `deploy ... --yes`, `sync ... --yes` | Change shared staging |
| `--overwrite` | Replaces someone else's draft or a hand edit |
| `--force` | Moves staging off a commit you don't have. Use it only after the user confirms that staging's commit is safe to replace. The old HEAD is backed up. |
| `--break-lock` | Takes over another deploy's lock. Use it only when the user confirms that the holder's deploy crashed. |
| `benchops execute <server> <method> --site <site> [--args JSON] [--kwargs JSON]` | Runs code on a site. Methods that write data, such as `set_value` or `delete_doc`, need approval. |
| `benchops install` / `benchops uninstall` | Run the one-time install or uninstall hooks |
| `benchops restore ...` | Replaces a database. **Never pass `--yes` or `--skip-safety-backup` on your own initiative.** Hand this command to the user. |
| `server add-hook`, `clear-hooks`, `server remove` | Change the team's deploy configuration |

## Never do

- Never run `git pull`, `git checkout`, `git commit` or `bench build` on the staging server. BenchOps owns staging's HEAD and its builds.
- Never edit `.git/benchops/ledger.json` on the server, and never delete `refs/benchops/*`.
- Never retry a failed deploy with `--force`, `--overwrite` or `--break-lock` added just to make it pass. Report the error to the user and quote the fix from the table below.
- Never treat a deploy to a server whose alias or site contains `prod`, `production` or `erp.` as routine. Confirm it with the user explicitly, even if they asked for a deploy.

## Errors and what to tell the user

| Message contains | Meaning | Next step |
|---|---|---|
| `which is not in your history` | Staging has commits you lack | Pull and retry. If someone committed on staging, that needs a human decision before `--force`. |
| `is in progress by ... since ...` | Another deploy holds the lock | Wait and retry. `--break-lock` only if the user confirms the deploy is stale. |
| `would overwrite different changes on staging. Re-run without --yes` | An overlap with `--yes` | Run the preview, show the overlaps and ask |
| `was changed on the server while the plan was shown` | A Desk save happened during the deploy | Re-run the preview |
| `already up to date` | Nothing new to ship | Check that the files are staged (deploy) or committed (sync) |
| `not a git repository` | The app on the server wasn't installed with `bench get-app` | Human fix on the server |
| `Build output ... does not exist` / `Built HTML references assets that do not exist` | Stale or missing build | Re-run without `--skip-build` |
| `unresolved merge conflicts` | A merge on your machine isn't finished | The user resolves it |
| Connection or SSM errors | Access problem | Run `benchops server check <server>` and relay the failing step |

## Limits to know

- Desk-only changes (Custom Fields, Property Setters, Client Scripts) are not files. They ship only after they are exported as fixtures (`bench --site <site> export-fixtures --app <app>`) and staged.
- A rejected draft has no undo command. It must be restored by hand on staging (`git restore --staged --worktree <file>` in `apps/<app>`). Tell the user to do this; do not do it yourself.
- JSON output exists only for `deploy`, `sync` and `status`. For other commands, quote their output instead of paraphrasing it.
