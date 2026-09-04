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
        """Add or update a server, preserving existing comments and formatting."""
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
