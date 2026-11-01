"""Configuration management for benchops."""

from pathlib import Path

import tomlkit
from tomlkit.toml_file import TOMLFile

from benchops.security import secure_file

CONFIG_PATH = Path.home() / ".benchops" / "config.toml"

DEFAULT_CONNECTION_TYPE = "ssh"
VALID_CONNECTION_TYPES = {"ssh", "ssm"}


class ConfigManager:
    """Reads and writes the local benchops TOML configuration file."""

    def __init__(self, config_path: Path = CONFIG_PATH) -> None:
        self.config_path = config_path
        self.config_dir = self.config_path.parent

    def init_config(self) -> None:
        """Create the config directory and file with a [servers] table."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if self.config_path.exists():
            return
        doc = tomlkit.document()
        doc["servers"] = tomlkit.table()
        TOMLFile(self.config_path).write(doc)
        secure_file(self.config_path, 0o600)

    def _read(self) -> tomlkit.TOMLDocument:
        return TOMLFile(self.config_path).read()

    def _get_server_entry(self, alias: str) -> tuple[tomlkit.TOMLDocument, dict]:
        """Helper to load config, validate server exists, and return the doc and server table."""
        alias = alias.strip()
        try:
            doc = self._read()
        except FileNotFoundError:
            raise ValueError("Configuration file not found.")

        servers = doc.get("servers")
        if servers is None or alias not in servers:
            raise ValueError(f"Server '{alias}' not found in configuration.")

        return doc, servers[alias]

    def _get_hook_key(self, phase: str) -> str:
        """Convert CLI phase string (pre-local) to TOML key (pre_local_commands)."""
        return phase.replace("-", "_") + "_commands"

    def add_server(
        self,
        alias: str,
        host: str,
        port: int,
        user: str,
        bench_path: str,
        connection_type: str = DEFAULT_CONNECTION_TYPE,
        private_key_path: str | None = None,
        instance_id: str | None = None,
        aws_profile: str | None = None,
        aws_region: str | None = None,
        pre_local_commands: list[str] | None = None,
        pre_remote_commands: list[str] | None = None,
        post_remote_commands: list[str] | None = None,
        install_remote_commands: list[str] | None = None,
        uninstall_remote_commands: list[str] | None = None,
    ) -> None:
        """Add or update a server, preserving existing comments and formatting.

        Updating an existing server keeps its hooks and private key unless
        new ones are passed — re-running `server add` to change the port or
        switch to SSM must not silently drop them.
        """
        if connection_type not in VALID_CONNECTION_TYPES:
            raise ValueError(
                f"Invalid connection_type '{connection_type}'. "
                f"Must be one of: {', '.join(sorted(VALID_CONNECTION_TYPES))}."
            )

        alias = alias.strip()
        self.init_config()
        doc = self._read()
        if "servers" not in doc:
            doc["servers"] = tomlkit.table()

        entry = tomlkit.inline_table()
        entry["host"] = host
        entry["port"] = port
        entry["user"] = user
        entry["bench_path"] = bench_path
        entry["connection_type"] = connection_type

        for key, value in (
            ("private_key_path", private_key_path),
            ("instance_id", instance_id),
            ("aws_profile", aws_profile),
            ("aws_region", aws_region),
        ):
            if value is not None:
                entry[key] = value

        for hook, commands in (
            ("pre_local_commands", pre_local_commands),
            ("pre_remote_commands", pre_remote_commands),
            ("post_remote_commands", post_remote_commands),
            ("install_remote_commands", install_remote_commands),
            ("uninstall_remote_commands", uninstall_remote_commands),
        ):
            if commands is not None:
                array = tomlkit.array()
                for cmd in commands:
                    array.append(cmd)
                entry[hook] = array

        existing = doc["servers"].get(alias)
        if existing is not None:
            for key, value in existing.items():
                if key not in entry and (key == "private_key_path" or key.endswith("_commands")):
                    entry[key] = value

        doc["servers"][alias] = entry
        TOMLFile(self.config_path).write(doc)

    def update_server_key(self, alias: str, private_key_path: str) -> None:
        """Add or update the private key path for an existing server."""
        doc, server = self._get_server_entry(alias)
        server["private_key_path"] = private_key_path
        TOMLFile(self.config_path).write(doc)

    def clear_private_key(self, alias: str) -> None:
        """Remove the private key path from a server's configuration, if present."""
        doc, server = self._get_server_entry(alias)
        if "private_key_path" in server:
            del server["private_key_path"]
            TOMLFile(self.config_path).write(doc)

    def get_server(self, alias: str) -> dict | None:
        """Return the configuration for a specific server alias."""
        return self.list_servers().get(alias)

    def list_servers(self) -> dict:
        """Return all configured servers keyed by alias.

        Older config entries predate the `connection_type` field; they are
        defaulted to "ssh" here so every caller sees a consistent schema.
        """
        try:
            doc = self._read()
        except FileNotFoundError:
            return {}

        servers = doc.get("servers")
        if servers is None:
            return {}

        result = {}
        for alias, config in servers.items():
            server = dict(config)
            server.setdefault("connection_type", DEFAULT_CONNECTION_TYPE)
            result[str(alias).strip()] = server
        return result

    def remove_server(self, alias: str) -> None:
        """Remove a server from the configuration."""
        doc, _ = self._get_server_entry(alias)
        del doc["servers"][alias]
        TOMLFile(self.config_path).write(doc)

    def get_hooks(self, alias: str, phase: str) -> list[str]:
        """Retrieve the hooks for a specific phase without modifying the config."""
        server = self.get_server(alias)
        if server is None:
            raise ValueError(f"Server '{alias}' not found in configuration.")

        config_key = self._get_hook_key(phase)
        return server.get(config_key, [])

    def add_hook(self, alias: str, phase: str, command: str) -> None:
        """Append a command to a specific lifecycle hook array for a server."""
        doc, server = self._get_server_entry(alias)
        config_key = self._get_hook_key(phase)

        if config_key not in server:
            server[config_key] = tomlkit.array()

        server[config_key].append(command)
        TOMLFile(self.config_path).write(doc)

    def clear_hooks(self, alias: str, phase: str) -> None:
        """Clear all commands for a specific lifecycle hook array."""
        doc, server = self._get_server_entry(alias)
        config_key = self._get_hook_key(phase)

        if config_key in server:
            del server[config_key]
            TOMLFile(self.config_path).write(doc)

    def set_hooks(self, alias: str, phase: str, commands: list[str]) -> None:
        """Overwrite the hook array for a specific phase."""
        doc, server = self._get_server_entry(alias)
        config_key = self._get_hook_key(phase)

        array = tomlkit.array()
        for cmd in commands:
            array.append(cmd)

        server[config_key] = array
        TOMLFile(self.config_path).write(doc)

    # --- Git hosts and clone projects -------------------------------------
    # Kept in their own tables, apart from [servers], so a project or git host
    # can share a name with a server alias without either overwriting the other.

    def _write_table_entry(self, table: str, key: str, entry) -> None:
        self.init_config()
        doc = self._read()
        if table not in doc:
            doc[table] = tomlkit.table()
        doc[table][key] = entry
        TOMLFile(self.config_path).write(doc)

    def _read_table(self, table: str) -> dict:
        try:
            doc = self._read()
        except FileNotFoundError:
            return {}
        return {str(k).strip(): v.unwrap() if hasattr(v, "unwrap") else v for k, v in (doc.get(table) or {}).items()}

    def _remove_table_entry(self, table: str, key: str, what: str) -> None:
        try:
            doc = self._read()
        except FileNotFoundError:
            raise ValueError("Configuration file not found.")
        if key not in (doc.get(table) or {}):
            raise ValueError(f"{what} '{key}' not found in configuration.")
        del doc[table][key]
        TOMLFile(self.config_path).write(doc)

    def set_git_host(self, host: str, provider: str, api_url: str | None = None, username: str | None = None) -> None:
        """Record which provider a git host runs; its token lives in the keyring."""
        entry = tomlkit.inline_table()
        entry["provider"] = provider
        if api_url:
            entry["api_url"] = api_url
        if username:
            entry["username"] = username
        self._write_table_entry("git_hosts", host.strip().lower(), entry)

    def get_git_host(self, host: str) -> dict | None:
        return self.list_git_hosts().get(host.strip().lower())

    def list_git_hosts(self) -> dict:
        return self._read_table("git_hosts")

    def remove_git_host(self, host: str) -> None:
        self._remove_table_entry("git_hosts", host.strip().lower(), "Git host")

    def set_project(self, name: str, project: dict) -> None:
        """Add or replace a clone project (see benchops.projects for the schema)."""
        entry = tomlkit.table()
        for key, value in project.items():
            if key == "repos":
                repos = tomlkit.array()
                for repo in value:
                    item = tomlkit.inline_table()
                    item.update(repo)
                    repos.append(item)
                repos.multiline(True)
                entry["repos"] = repos
            elif value is not None:
                entry[key] = value
        self._write_table_entry("projects", name.strip(), entry)

    def get_project(self, name: str) -> dict | None:
        return self.list_projects().get(name.strip())

    def list_projects(self) -> dict:
        return self._read_table("projects")

    def remove_project(self, name: str) -> None:
        self._remove_table_entry("projects", name.strip(), "Project")

    # --- S3 backup sources ------------------------------------------------
    # Where Frappe's S3 Backup Settings upload site backups. AWS credentials
    # are never stored here: a source names an AWS profile, or falls back to
    # the default AWS credential chain.

    def set_backup_source(
        self,
        name: str,
        bucket: str,
        prefix: str | None = None,
        aws_profile: str | None = None,
        aws_region: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        entry = tomlkit.inline_table()
        entry["bucket"] = bucket.strip()
        for key, value in (
            ("prefix", prefix.strip("/") if prefix else None),
            ("aws_profile", aws_profile),
            ("aws_region", aws_region),
            ("endpoint_url", endpoint_url),
        ):
            if value:
                entry[key] = value
        self._write_table_entry("backup_sources", name.strip(), entry)

    def get_backup_source(self, name: str) -> dict | None:
        return self.list_backup_sources().get(name.strip())

    def list_backup_sources(self) -> dict:
        return self._read_table("backup_sources")

    def remove_backup_source(self, name: str) -> None:
        self._remove_table_entry("backup_sources", name.strip(), "Backup source")

    # --- Local bench --------------------------------------------------------
    # Hooks for commands that act on the bench on this machine
    # (`benchops restore --local`), kept apart from every server's.

    def get_local_hooks(self, phase: str) -> list[str]:
        return list(self._read_table("local").get(self._get_hook_key(phase)) or [])

    def set_local_hooks(self, phase: str, commands: list[str]) -> None:
        self.init_config()
        doc = self._read()
        if "local" not in doc:
            doc["local"] = tomlkit.table()
        key = self._get_hook_key(phase)
        if commands:
            array = tomlkit.array()
            for cmd in commands:
                array.append(cmd)
            doc["local"][key] = array
        elif key in doc["local"]:
            del doc["local"][key]
        TOMLFile(self.config_path).write(doc)

    def add_local_hook(self, phase: str, command: str) -> None:
        self.set_local_hooks(phase, self.get_local_hooks(phase) + [command])
