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

Ownership model on the remote app directory:
  * files in the deployed snapshot belong to local and are reset to it;
  * files git has never tracked there were created on staging (e.g. via
    Desk in developer_mode); they are listed in a BenchOps-managed block of
    .git/info/exclude, so they stay out of git and are never touched;
  * build outputs (public/dist, SPA builds) are never in git: each deploy
    that ships them replaces the remote copy wholesale.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time

RESULT_MARKER = "BENCHOPS_RESULT:"
EXCLUDE_BEGIN = "# >>> benchops: staging-only files (managed, do not edit) >>>"
EXCLUDE_END = "# <<< benchops <<<"
EXCLUDE_OWNED = "# staging-only:"
INCOMING_REF = "refs/benchops/incoming"
DEPLOYED_REF = "refs/benchops/deployed"
MANIFEST_NAMES = ("assets.json", "assets-rtl.json")


class AgentError(Exception):
    def __init__(self, error, **data):
        super().__init__(error)
        self.error = error
        self.data = data


def git(app_dir, *args, check=True):
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=app_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if check and proc.returncode != 0:
        raise AgentError("git %s failed: %s" % (args[0], proc.stderr.strip()))
    return proc.stdout if check else (proc.returncode, proc.stdout)


def git_z(app_dir, *args):
    return [item for item in git(app_dir, *args).split("\0") if item]


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


def tree_files(app_dir, commit):
    if not commit:
        return set()
    return set(git_z(app_dir, "ls-tree", "-r", "-z", "--name-only", commit))


def ancestor_dirs(paths):
    dirs = set()
    for path in paths:
        parent = os.path.dirname(path)
        while parent:
            dirs.add(parent)
            parent = os.path.dirname(parent)
    return dirs


def tracked_changes(app_dir):
    """Uncommitted changes to tracked files, as [[code, path], ...]."""
    entries = git_z(app_dir, "status", "--porcelain=v1", "-z", "--untracked-files=no")
    changes = []
    skip_next = False
    for entry in entries:
        if skip_next:  # the source path of a rename/copy entry
            skip_next = False
            continue
        code, path = entry[:2], entry[3:]
        changes.append([code.strip(), path])
        skip_next = code[0] in "RC"
    return changes


def status_fingerprint(app_dir):
    """Hash of the full working-tree status, to detect edits made on staging
    (e.g. a Desk save) between preview and apply."""
    out = git(app_dir, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    return hashlib.sha256(out.encode()).hexdigest()


def exclude_path(app_dir):
    return os.path.join(app_dir, ".git", "info", "exclude")


def escape_pattern(path):
    return re.sub(r"([\\*?\[\]#! ])", r"\\\1", path)


def read_staging_owned(app_dir):
    try:
        with open(exclude_path(app_dir)) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    block, inside = [], False
    for line in lines:
        if line == EXCLUDE_BEGIN:
            inside = True
        elif line == EXCLUDE_END:
            inside = False
        elif inside:
            block.append(line)
    if EXCLUDE_OWNED in block:
        entries = block[block.index(EXCLUDE_OWNED) + 1:]
    else:  # blocks written by 0.12.0 had no section marker
        entries = [line for line in block if line.startswith("/") and not line.endswith("/")]
    return [re.sub(r"\\(.)", r"\1", line[1:]) for line in entries if line.startswith("/")]


def write_staging_owned(app_dir, owned, build_outputs):
    path = exclude_path(app_dir)
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    kept, inside = [], False
    for line in lines:
        if line == EXCLUDE_BEGIN:
            inside = True
        elif line == EXCLUDE_END:
            inside = False
        elif not inside:
            kept.append(line)

    # Build outputs and caches are never staging-owned, whatever the app's
    # .gitignore says; listing them keeps them out of the untracked set.
    block = [EXCLUDE_BEGIN, "__pycache__/", "*.pyc", "node_modules/"]
    block += ["/" + escape_pattern(o["path"]) + ("/" if o["dir"] else "") for o in build_outputs]
    block.append(EXCLUDE_OWNED)
    block += ["/" + escape_pattern(p) for p in sorted(owned)]
    block.append(EXCLUDE_END)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(kept + block) + "\n")


def under_outputs(path, build_outputs):
    return any(path == o["path"] or path.startswith(o["path"] + "/") for o in build_outputs)


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


# --------------------------------------------------------------------------- commands


def cmd_inspect(args):
    app = args["app"]
    app_dir = os.path.join("apps", app)
    if shutil.which("git") is None:
        raise AgentError("git is not installed on the server")

    if not os.path.isdir(os.path.join(app_dir, ".git")):
        if not args.get("adopt"):
            raise AgentError("not_repo")
        os.makedirs(app_dir, exist_ok=True)
        git(app_dir, "init", "-q")

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

    # Make sure caches and build outputs are ignored before anything is listed.
    write_staging_owned(app_dir, read_staging_owned(app_dir), args.get("build_outputs", []))

    head = rev(app_dir, "HEAD")
    return {
        "head": head,
        "tree": rev(app_dir, "HEAD^{tree}") if head else None,
        "record": read_json(os.path.join(state_dir(app_dir), "deploy.json")),
        "staging_edits": tracked_changes(app_dir) if head else [],
        "frappe_version": frappe_version(),
    }


def cmd_preview(args):
    app = args["app"]
    app_dir = os.path.join("apps", app)
    check_lock(app_dir, args["token"])

    incoming = os.path.join(state_dir(app_dir), "incoming")
    shutil.rmtree(incoming, ignore_errors=True)
    os.makedirs(incoming)
    with tarfile.open(args["package"]) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(incoming, filter="data")
        else:
            tar.extractall(incoming)
    meta = read_json(os.path.join(incoming, "meta.json"))

    head = rev(app_dir, "HEAD")
    if meta.get("bundle_ref"):
        bundle = os.path.abspath(os.path.join(incoming, "snapshot.bundle"))
        git(app_dir, "bundle", "verify", bundle)
        git(app_dir, "fetch", "-q", "--no-tags", bundle, "+%s:%s" % (meta["bundle_ref"], INCOMING_REF))
        new = rev(app_dir, INCOMING_REF)
    else:
        new = head
    if not new:
        raise AgentError("nothing to deploy: the remote has no commit and none was shipped")

    owned = set(read_staging_owned(app_dir))
    untracked = set(git_z(app_dir, "ls-files", "-z", "--others", "--exclude-standard"))
    head_files, new_files = tree_files(app_dir, head), tree_files(app_dir, new)

    staging_files = owned | untracked
    collisions = staging_files & new_files
    removed_dirs = ancestor_dirs(head_files) - ancestor_dirs(new_files)
    orphans = {p for p in staging_files - collisions if ancestor_dirs([p]) & removed_dirs}

    if head:
        changes = git_z(app_dir, "diff", "-z", "--name-only", "--no-renames", head, new)
    else:
        changes = sorted(new_files)
    # Build outputs committed by older deploys leave the tree but are
    # replaced by the shipped build, so they are not real deletions.
    outputs = args.get("build_outputs", [])
    changes = [p for p in changes if not under_outputs(p, outputs)]

    return {
        "new": new,
        "changes": changes,
        "deletions": sorted(p for p in head_files - new_files if not under_outputs(p, outputs)),
        "staging_edits": tracked_changes(app_dir) if head else [],
        "staging_owned": sorted(owned - collisions),
        "staging_new": sorted(untracked - collisions),
        "collisions": sorted(collisions),
        "orphans": sorted(orphans),
        "fingerprint": status_fingerprint(app_dir),
    }


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


def cmd_apply(args):
    app = args["app"]
    app_dir = os.path.join("apps", app)
    check_lock(app_dir, args["token"])
    if status_fingerprint(app_dir) != args["fingerprint"]:
        raise AgentError("staging_changed")

    incoming = os.path.join(state_dir(app_dir), "incoming")
    new = args["new"]
    previous = rev(app_dir, "HEAD")
    result = {"backup_ref": None, "warnings": []}

    # Keep staging edits to deployed files recoverable before overwriting them.
    if previous and tracked_changes(app_dir):
        stash = git(
            app_dir, "-c", "user.name=benchops", "-c", "user.email=benchops@localhost", "stash", "create"
        ).strip()
        if stash:
            ref = "refs/benchops/overwritten/%d" % int(time.time())
            git(app_dir, "update-ref", ref, stash)
            result["backup_ref"] = ref

    for rel in args.get("delete", []):
        target = safe_join(app_dir, rel)
        if os.path.isfile(target) or os.path.islink(target):
            os.remove(target)
        parent = os.path.dirname(target)
        while parent != os.path.realpath(app_dir) and os.path.isdir(parent) and not os.listdir(parent):
            os.rmdir(parent)
            parent = os.path.dirname(parent)

    git(app_dir, "checkout", "-q", "-f", "--detach", new)
    write_staging_owned(app_dir, args.get("keep", []), args.get("build_outputs", []))

    meta = read_json(os.path.join(incoming, "meta.json"), {})
    shipped = meta.get("build_outputs", [])
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
    if shipped:
        warning = merge_manifests(app, read_json(os.path.join(incoming, "manifests.json"), {}))
        if warning:
            result["warnings"].append(warning)
    result["build_outputs"] = shipped

    tree = rev(app_dir, "HEAD^{tree}")
    if tree != args["record"]["tree"] or tracked_changes(app_dir):
        raise AgentError("verification failed: remote tree %s does not match %s" % (tree, args["record"]["tree"]))

    git(app_dir, "update-ref", DEPLOYED_REF, new)
    git(app_dir, "update-ref", "-d", INCOMING_REF, check=False)

    record = dict(args["record"], snapshot=new, previous_snapshot=previous, staging_only=sorted(args.get("keep", [])))
    write_json(os.path.join(state_dir(app_dir), "deploy.json"), record)
    with open(os.path.join(state_dir(app_dir), "history.jsonl"), "a") as f:
        f.write(json.dumps(record) + "\n")

    result["tree"] = tree
    return result


def cmd_release(args):
    app_dir = os.path.join("apps", args["app"])
    if not os.path.isdir(os.path.join(app_dir, ".git")):
        return {}
    shutil.rmtree(os.path.join(state_dir(app_dir), "incoming"), ignore_errors=True)
    if args.get("package"):
        try:
            os.remove(args["package"])
        except FileNotFoundError:
            pass
    lock = read_json(lock_path(app_dir))
    if lock and lock.get("token") == args["token"]:
        os.remove(lock_path(app_dir))
    return {}


COMMANDS = {"inspect": cmd_inspect, "preview": cmd_preview, "apply": cmd_apply, "release": cmd_release}


def main():
    command, args = sys.argv[1], json.loads(sys.argv[2])
    try:
        result = dict(COMMANDS[command](args), ok=True)
    except AgentError as exc:
        result = dict(exc.data, ok=False, error=exc.error)
    print(RESULT_MARKER + json.dumps(result))


if __name__ == "__main__":
    main()
