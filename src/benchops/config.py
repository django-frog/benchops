"""Configuration management for benchops."""

from pathlib import Path

import tomlkit
from tomlkit.toml_file import TOMLFile

CONFIG_PATH = Path.home() / ".benchops" / "config.toml"


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

    def _read(self) -> tomlkit.TOMLDocument:
        return TOMLFile(self.config_path).read()

    def add_server(
        self,
        alias: str,
        host: str,
        port: int,
        user: str,
        bench_path: str,
        private_key_path: str | None = None,
        post_commands: list[str] | None = None,
    ) -> None:
        """Add or update a server, preserving existing comments and formatting."""
        self.init_config()
        doc = self._read()
        if "servers" not in doc:
            doc["servers"] = tomlkit.table()
        entry = tomlkit.inline_table()
        entry["host"] = host
        entry["port"] = port
        entry["user"] = user
        entry["bench_path"] = bench_path
        if private_key_path is not None:
            entry["private_key_path"] = private_key_path
        if post_commands is not None:
            array = tomlkit.array()
            for cmd in post_commands:
                array.append(cmd)
            entry["post_commands"] = array

        doc["servers"][alias] = entry
        TOMLFile(self.config_path).write(doc)

    def update_server_key(self, alias: str, private_key_path: str) -> None:
        """Add or update the private key path for an existing server."""
        self.init_config()
        doc = self._read()
        servers = doc.get("servers")
        if servers is None or alias not in servers:
            raise ValueError(f"Server '{alias}' not found in configuration.")
        servers[alias]["private_key_path"] = private_key_path
        TOMLFile(self.config_path).write(doc)

    def get_server(self, alias: str) -> dict | None:
        """Return the configuration for a specific server alias."""
        return self.list_servers().get(alias)

    def list_servers(self) -> dict:
        """Return all configured servers keyed by alias."""
        try:
            doc = self._read()
        except FileNotFoundError:
            return {}
        servers = doc.get("servers")
        if servers is None:
            return {}
        return {str(alias): dict(config) for alias, config in servers.items()}

    def remove_server(self, alias: str) -> None:
        """Remove a server from the configuration."""
        try:
            doc = self._read()
        except FileNotFoundError:
            raise ValueError("Configuration file not found.")

        if "servers" in doc and alias in doc["servers"]:
            del doc["servers"][alias]
            TOMLFile(self.config_path).write(doc)
        else:
            raise ValueError(f"Server '{alias}' not found in configuration.")
