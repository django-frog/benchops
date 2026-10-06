"""Remote half of the git-based sync, executed on the server.

This file is never imported by BenchOps locally. Its source is sent over the
existing SSH/SSM channel and run with the bench's own interpreter
(`./env/bin/python -c <source> <command> <json-args>`, cwd = bench root), so
it must stay self-contained: standard library only (plus `redis`, which
Frappe always installs), and compatible with every Python a Frappe bench may
run (3.8+).

Each command prints exactly one line starting with RESULT_MARKER followed by
a JSON object. Expected failures (lock held, not a repo, ...) are reported
as {"ok": false, "error": ...} rather than a non-zero exit, so the caller can
tell them apart from a crash.

Deploy model ("staged overlay"): a deploy ships the developer's commits plus
what they staged with `git add` — the *staged commit* S, whose tree is their
index and whose parent is their HEAD. On the server:

  * the deploy set is every path that differs between the server's HEAD and
    S; only those files are written (or deleted), and their index entries
    are set to S, so they show as "Changes to be committed";
  * HEAD moves to the developer's commit, on their branch;
  * every other file is left exactly as it is, so work done directly on the
    server stays on disk and stays visible in `git status`;
  * a path in the deploy set that has different uncommitted content on the
    server is an *overlap*: it is reported up front, backed up under
    refs/benchops/overwritten/, and only then overwritten;
  * build outputs (public/dist, SPA builds) are not part of the overlay:
    when shipped, they replace the server's copy wholesale.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

RESULT_MARKER = "BENCHOPS_RESULT:"
IGNORE_BEGIN = "# >>> benchops: staging-only files (managed, do not edit) >>>"
IGNORE_END = "# <<< benchops <<<"
INCOMING_REF = "refs/benchops/incoming"
DEPLOYED_REF = "refs/benchops/deployed"
MANIFEST_NAMES = ("assets.json", "assets-rtl.json")
CHUNK = 200

# What a porcelain status code means for someone reading the plan.
STAGING_STATE = {
    "??": "new file on staging",
    "D": "deleted on staging",
    "A": "added on staging",
    "M": "modified on staging",
}


class AgentError(Exception):
    def __init__(self, error, **data):
        super().__init__(error)
        self.error = error
        self.data = data


def git(app_dir, *args, check=True, env=None):
    proc = subprocess.run(
        ["git", "--literal-pathspecs", "-c", "core.quotepath=off", *args],
        cwd=app_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=env,
    )
    if check and proc.returncode != 0:
        raise AgentError("git %s failed: %s" % (args[0], proc.stderr.strip()))
    return proc.stdout if check else (proc.returncode, proc.stdout)


def git_z(app_dir, *args):
    return [item for item in git(app_dir, *args).split("\0") if item]


def chunks(items):
    items = list(items)
    for start in range(0, len(items), CHUNK):
        yield items[start:start + CHUNK]


def state_dir(app_dir):
    path = os.path.join(app_dir, ".git", "benchops")
    os.makedirs(path, exist_ok=True)
    return path


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def rev(app_dir, name):
    code, out = git(app_dir, "rev-parse", "-q", "--verify", name, check=False)
    return out.strip() if code == 0 else None


def is_ancestor(app_dir, ancestor, descendant):
    code, _ = git(app_dir, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    return code == 0


def current_branch(app_dir):
    code, out = git(app_dir, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    return out.strip() if code == 0 else None


def tree_entries(app_dir, treeish):
    """{path: blob sha} for every file in a commit or tree."""
    entries = {}
    for item in git_z(app_dir, "ls-tree", "-r", "-z", "--full-tree", treeish):
        meta, path = item.split("\t", 1)
        entries[path] = meta.split()[2]
    return entries


def name_status(app_dir, *args):
    parts = git_z(app_dir, "diff", "--name-status", "-z", "--no-renames", *args)
    return [[status, path] for status, path in zip(parts[0::2], parts[1::2])]


def dirty_paths(app_dir):
    """{path: porcelain code} for every uncommitted change on the server,
    including untracked files (ignored files are not changes)."""
    dirty = {}
    for entry in git_z(app_dir, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"):
        dirty[entry[3:]] = entry[:2]
    return dirty


def describe(code):
    if code == "??":
        return STAGING_STATE["??"]
    for letter in (code[1], code[0]):
        if letter in STAGING_STATE:
            return STAGING_STATE[letter]
    return "changed on staging"


def worktree_hashes(app_dir, paths):
    """{path: blob sha of the file on disk, or None if it doesn't exist}."""
    hashes = {}
    existing = [p for p in paths if os.path.isfile(os.path.join(app_dir, p))]
    for group in chunks(existing):
        out = git(app_dir, "hash-object", "--", *group).split()
        hashes.update(zip(group, out))
    for path in paths:
        hashes.setdefault(path, None)
    return hashes


def under_outputs(path, build_outputs):
    return any(path == o["path"] or path.startswith(o["path"] + "/") for o in build_outputs)


def write_ignore_block(app_dir, build_outputs):
    """Keep caches and build outputs out of `git status` via a managed block
    in .git/info/exclude (local to the server, never committed). Blocks
    written by 0.12/0.13 also hid staging-only files; those entries are
    dropped, so that work becomes visible again."""
    path = os.path.join(app_dir, ".git", "info", "exclude")
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    kept, inside = [], False
    for line in lines:
        if line == IGNORE_BEGIN:
            inside = True
        elif line == IGNORE_END:
            inside = False
        elif not inside:
            kept.append(line)

    block = [IGNORE_BEGIN, "__pycache__/", "*.pyc", "node_modules/"]
    for output in build_outputs:
        pattern = re.sub(r"([\\*?\[\]#! ])", r"\\\1", output["path"])
        block.append("/" + pattern + ("/" if output["dir"] else ""))
    block.append(IGNORE_END)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(kept + block) + "\n")


def frappe_version():
    try:
        with open(os.path.join("apps", "frappe", "frappe", "__init__.py")) as f:
            match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", f.read(), re.M)
            return match.group(1) if match else None
    except OSError:
        return None


def lock_path(app_dir):
    return os.path.join(state_dir(app_dir), "lock.json")


def check_lock(app_dir, token):
    lock = read_json(lock_path(app_dir))
    if not lock or lock.get("token") != token:
        raise AgentError("lock_lost", lock=lock)


def safe_join(app_dir, rel):
    root = os.path.realpath(app_dir)
    target = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, target]) != root or target == root:
        raise AgentError("refusing to touch path outside the app: %s" % rel)
    return target


def summarize_status(app_dir):
    """The server's `git status`, split the way a developer reads it."""
    staged, unstaged, untracked = [], [], []
    for path, code in sorted(dirty_paths(app_dir).items()):
        if code == "??":
            untracked.append(path)
            continue
        if code[0] != " ":
            staged.append([code[0], path])
        if code[1] != " ":
            unstaged.append([code[1], path])
    return {"staged": staged, "unstaged": unstaged, "untracked": untracked}


# --------------------------------------------------------------------------- deploy planning


def deploy_set(app_dir, head, staged_commit, build_outputs):
    """Paths the deploy writes, split into code paths (written to disk and
    index) and build-output paths (index only; their files ship separately)."""
    code, outputs = [], []
    for status, path in name_status(app_dir, head, staged_commit):
        (outputs if under_outputs(path, build_outputs) else code).append([status, path])
    return code, outputs


def fingerprint(app_dir, paths):
    """Hash of HEAD plus the on-disk and index state of `paths` — the files a
    deploy will touch. A Desk save on one of them between preview and apply
    changes it; edits to unrelated files don't."""
    digest = hashlib.sha256((rev(app_dir, "HEAD") or "").encode())
    dirty = dirty_paths(app_dir)
    for path, sha in sorted(worktree_hashes(app_dir, paths).items()):
        digest.update(("%s\0%s\0%s\0" % (path, sha, dirty.get(path, ""))).encode())
    return digest.hexdigest()


# --------------------------------------------------------------------------- commands


def cmd_inspect(args):
    app = args["app"]
    app_dir = os.path.join("apps", app)
    if shutil.which("git") is None:
        raise AgentError("git is not installed on the server")
    if not os.path.isdir(os.path.join(app_dir, ".git")):
        raise AgentError("not_repo")
    if not rev(app_dir, "HEAD"):
        raise AgentError("apps/%s on the server has no commits" % app)

    lock = dict(args["lock"], acquired_at=int(time.time()))
    path = lock_path(app_dir)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            json.dump(lock, f)
    except FileExistsError:
        if not args.get("break_lock"):
            raise AgentError("locked", lock=read_json(path))
        write_json(path, lock)

    write_ignore_block(app_dir, args.get("build_outputs", []))

    # 0.12/0.13 left HEAD detached on a snapshot commit that already contained
    # the deployer's uncommitted work. Point HEAD back at their real commit —
    # files untouched — so that work shows up in `git status` like any other.
    record = read_json(os.path.join(state_dir(app_dir), "deploy.json"))
    converted = False
    head = rev(app_dir, "HEAD")
    if record and record.get("snapshot") == head and record.get("base") and rev(app_dir, record["base"]):
        git(app_dir, "reset", "-q", "--mixed", record["base"])
        head, converted = record["base"], True

    return {
        "head": head,
        "branch": current_branch(app_dir),
        "record": record,
        "converted_snapshot": converted,
        "frappe_version": frappe_version(),
    }


def cmd_preview(args):
    app = args["app"]
    app_dir = os.path.join("apps", app)
    check_lock(app_dir, args["token"])
    build_outputs = args.get("build_outputs", [])

    incoming = os.path.join(state_dir(app_dir), "incoming")
    shutil.rmtree(incoming, ignore_errors=True)
    os.makedirs(incoming)
    with tarfile.open(args["package"]) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(incoming, filter="data")
        else:
            tar.extractall(incoming)
    meta = read_json(os.path.join(incoming, "meta.json"))

    bundle = os.path.abspath(os.path.join(incoming, "staged.bundle"))
    git(app_dir, "bundle", "verify", bundle)
    git(app_dir, "fetch", "-q", "--no-tags", bundle, "+%s:%s" % (meta["bundle_ref"], INCOMING_REF))
    staged_commit = rev(app_dir, INCOMING_REF)
    new_head = rev(app_dir, INCOMING_REF + "^")
    head = rev(app_dir, "HEAD")

    code_paths, output_paths = deploy_set(app_dir, head, staged_commit, build_outputs)
    incoming_blobs = tree_entries(app_dir, staged_commit)
    dirty = {p: c for p, c in dirty_paths(app_dir).items() if not under_outputs(p, build_outputs)}
    on_disk = worktree_hashes(app_dir, [p for _, p in code_paths])
    index_blobs = {}
    for item in git_z(app_dir, "ls-files", "-s", "-z"):
        meta_part, path = item.split("\t", 1)
        index_blobs[path] = meta_part.split()[1]

    previous = (read_json(os.path.join(state_dir(app_dir), "deploy.json")) or {})
    previous_staged = set(previous.get("staged", []))

    overlaps, pending = [], []
    for _, path in code_paths:
        want = incoming_blobs.get(path)
        if on_disk[path] != want or index_blobs.get(path) != want:
            pending.append(path)
        if path in dirty and on_disk[path] != want:
            reason = describe(dirty[path])
            if path in previous_staged:
                reason += " (from %s's deploy)" % previous.get("deployer", "an earlier")
            overlaps.append([path, reason])

    outputs_pending = [p for _, p in output_paths if index_blobs.get(p) != incoming_blobs.get(p)]
    code_set = set(p for _, p in code_paths)
    # Staged on the server but not part of this deploy: the previous deploy's
    # staged files. They stay on disk and become unstaged, so "Changes to be
    # committed" shows exactly this deploy.
    restage = [
        p for p in git_z(app_dir, "diff", "--cached", "--name-only", "-z", head)
        if p not in code_set and not under_outputs(p, build_outputs)
    ]
    untouched = sorted(p for p in dirty if p not in code_set)

    branch_conflict = None
    if args.get("branch"):
        existing = rev(app_dir, "refs/heads/" + args["branch"])
        if existing and existing != new_head and not is_ancestor(app_dir, existing, new_head):
            branch_conflict = existing

    return {
        "new_head": new_head,
        "staged_commit": staged_commit,
        "head_is_ancestor": is_ancestor(app_dir, head, new_head),
        "writes": [[s, p] for s, p in code_paths if p in pending],
        "changes": [p for _, p in code_paths] + [p for _, p in output_paths],
        "overlaps": overlaps,
        "restage": restage,
        "untouched": untouched,
        "branch_conflict": branch_conflict,
        "up_to_date": head == new_head and not pending and not outputs_pending and not restage,
        "fingerprint": fingerprint(app_dir, sorted(code_set)),
    }


def backup_paths(app_dir, base, paths, label):
    """Commit the server's current version of `paths` on top of `base`, using
    a throwaway index, and keep it under refs/benchops/<label>/."""
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, "index"))
        git(app_dir, "read-tree", base, env=env)
        for group in chunks(paths):
            git(app_dir, "add", "-A", "--", *group, env=env)
        tree = git(app_dir, "write-tree", env=env).strip()
    commit = git(
        app_dir, "-c", "user.name=benchops", "-c", "user.email=benchops@localhost",
        "commit-tree", tree, "-p", base, "-m", "benchops: staging versions before %s" % label,
    ).strip()
    ref = "refs/benchops/%s/%d" % (label, int(time.time()))
    git(app_dir, "update-ref", ref, commit)
    return ref


def merge_manifests(app, manifests):
    prefix = "/assets/%s/" % app
    for name in MANIFEST_NAMES:
        if name not in manifests:
            continue
        path = os.path.join("sites", "assets", name)
        current = read_json(path, {})
        merged = {k: v for k, v in current.items() if not str(v).startswith(prefix)}
        merged.update(manifests[name])
        tmp_path = path + ".benchops.tmp"
        with open(tmp_path, "w") as f:
            json.dump(merged, f, indent=4)
        os.replace(tmp_path, path)

    # Same invalidation Frappe's own build performs.
    try:
        redis_url = read_json(os.path.join("sites", "common_site_config.json"), {})["redis_cache"]
        import redis

        redis.Redis.from_url(redis_url).delete("assets_json")
        return None
    except Exception as exc:
        return "could not clear assets_json from redis_cache (%s); run 'bench clear-cache'" % exc


def replace_build_outputs(app_dir, incoming, shipped):
    for rel in shipped:
        source = os.path.join(incoming, "build", rel)
        if not os.path.lexists(source):
            raise AgentError("build output %s is missing from the deploy package" % rel)
        target = safe_join(app_dir, rel)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
        elif os.path.lexists(target):
            os.remove(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.rename(source, target)


def remove_file(app_dir, rel):
    target = safe_join(app_dir, rel)
    if os.path.isfile(target) or os.path.islink(target):
        os.remove(target)
    root = os.path.realpath(app_dir)
    parent = os.path.dirname(target)
    while parent != root and os.path.isdir(parent) and not os.listdir(parent):
        os.rmdir(parent)
        parent = os.path.dirname(parent)


def cmd_apply(args):
    app = args["app"]
    app_dir = os.path.join("apps", app)
    check_lock(app_dir, args["token"])
    build_outputs = args.get("build_outputs", [])
    staged_commit = rev(app_dir, INCOMING_REF)
    new_head = rev(app_dir, INCOMING_REF + "^")
    head = rev(app_dir, "HEAD")

    code_paths, output_paths = deploy_set(app_dir, head, staged_commit, build_outputs)
    if fingerprint(app_dir, sorted(p for _, p in code_paths)) != args["fingerprint"]:
        raise AgentError("staging_changed")

    result = {"backups": [], "warnings": []}
    overlaps = args.get("overlaps", [])
    if overlaps:
        result["backups"].append(backup_paths(app_dir, head, overlaps, "overwritten"))
    if not is_ancestor(app_dir, head, new_head):
        ref = "refs/benchops/previous-head/%d" % int(time.time())
        git(app_dir, "update-ref", ref, head)
        result["backups"].append(ref)

    incoming_blobs = tree_entries(app_dir, staged_commit)
    writes = [p for _, p in code_paths if p in incoming_blobs]
    deletes = [p for _, p in code_paths if p not in incoming_blobs]
    for group in chunks(writes):
        git(app_dir, "checkout", staged_commit, "--", *group)
    for rel in deletes:
        remove_file(app_dir, rel)
    for group in chunks(deletes):
        git(app_dir, "rm", "-q", "--cached", "--ignore-unmatch", "--", *group)
    # Build outputs: only the index follows the developer; the files on disk
    # come from the shipped build below.
    for group in chunks(p for _, p in output_paths):
        git(app_dir, "reset", "-q", staged_commit, "--", *group)
    for group in chunks(args.get("restage", [])):
        git(app_dir, "reset", "-q", new_head, "--", *group)

    branch = args.get("branch")
    if branch:
        ref = "refs/heads/" + branch
        existing = rev(app_dir, ref)
        if existing and existing != new_head and not is_ancestor(app_dir, existing, new_head):
            backup = "refs/benchops/branch-backup/%s/%d" % (branch, int(time.time()))
            git(app_dir, "update-ref", backup, existing)
            result["backups"].append(backup)
        git(app_dir, "update-ref", ref, new_head)
        git(app_dir, "symbolic-ref", "HEAD", ref)
    else:
        git(app_dir, "update-ref", "--no-deref", "HEAD", new_head)

    incoming = os.path.join(state_dir(app_dir), "incoming")
    meta = read_json(os.path.join(incoming, "meta.json"), {})
    shipped = meta.get("build_outputs", [])
    replace_build_outputs(app_dir, incoming, shipped)
    if shipped:
        warning = merge_manifests(app, read_json(os.path.join(incoming, "manifests.json"), {}))
        if warning:
            result["warnings"].append(warning)
    result["build_outputs"] = shipped

    # Verify: HEAD is the developer's commit, and every deployed path matches
    # their staged version on disk and in the index.
    if rev(app_dir, "HEAD") != new_head:
        raise AgentError("verification failed: HEAD is not %s" % new_head)
    paths = [p for _, p in code_paths]
    for group in chunks(paths):
        if git_z(app_dir, "diff", "--name-only", "-z", staged_commit, "--", *group) or git_z(
            app_dir, "diff", "--cached", "--name-only", "-z", staged_commit, "--", *group
        ):
            raise AgentError("verification failed: deployed files do not match the staged versions")

    git(app_dir, "update-ref", DEPLOYED_REF, staged_commit)
    git(app_dir, "update-ref", "-d", INCOMING_REF, check=False)
    record = dict(args["record"], base=new_head, staged_commit=staged_commit, previous_head=head)
    write_json(os.path.join(state_dir(app_dir), "deploy.json"), record)
    with open(os.path.join(state_dir(app_dir), "history.jsonl"), "a") as f:
        f.write(json.dumps(record) + "\n")
    return result


def cmd_status(args):
    """Read-only: what the server runs, who deployed it, what's changed since."""
    app_dir = os.path.join("apps", args["app"])
    if not os.path.isdir(os.path.join(app_dir, ".git")):
        raise AgentError("not_repo")
    head = rev(app_dir, "HEAD")
    record = read_json(os.path.join(app_dir, ".git", "benchops", "deploy.json"))
    result = dict(summarize_status(app_dir), head=head, branch=current_branch(app_dir), record=record)
    result["lock"] = read_json(os.path.join(app_dir, ".git", "benchops", "lock.json"))
    result["subject"] = git(app_dir, "log", "-1", "--format=%s", "HEAD").strip() if head else None

    # Deployed files that someone changed on the server since the deploy.
    drifted = []
    if record and record.get("staged_commit") and rev(app_dir, record["staged_commit"]):
        deployed = record.get("staged", [])
        for group in chunks(deployed):
            drifted += git_z(app_dir, "diff", "--name-only", "-z", record["staged_commit"], "--", *group)
    result["drifted"] = sorted(set(drifted))
    return result


def cmd_release(args):
    app_dir = os.path.join("apps", args["app"])
    if not os.path.isdir(os.path.join(app_dir, ".git")):
        return {}
    shutil.rmtree(os.path.join(state_dir(app_dir), "incoming"), ignore_errors=True)
    git(app_dir, "update-ref", "-d", INCOMING_REF, check=False)
    if args.get("package"):
        try:
            os.remove(args["package"])
        except FileNotFoundError:
            pass
    lock = read_json(lock_path(app_dir))
    if lock and lock.get("token") == args["token"]:
        os.remove(lock_path(app_dir))
    return {}


COMMANDS = {
    "inspect": cmd_inspect,
    "preview": cmd_preview,
    "apply": cmd_apply,
    "status": cmd_status,
    "release": cmd_release,
}


def main():
    command, args = sys.argv[1], json.loads(sys.argv[2])
    try:
        result = dict(COMMANDS[command](args), ok=True)
    except AgentError as exc:
        result = dict(exc.data, ok=False, error=exc.error)
    print(RESULT_MARKER + json.dumps(result))


if __name__ == "__main__":
    main()
