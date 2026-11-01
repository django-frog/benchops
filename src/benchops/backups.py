"""Frappe site backups stored in S3, as written by Frappe's S3 Backup Settings.

Frappe uploads every backup run into a folder named after its timestamp:

    <bucket>/[<prefix>/]<YYYYMMDD_HHMMSS>/<YYYYMMDD_HHMMSS>-<site_slug>-<part>

where <site_slug> is the site name with dots replaced by underscores and
<part> is one of database.sql.gz, files.tar, private-files.tar (.tgz when
compressed) or site_config_backup.json. Several sites can share a bucket,
so backups are matched by file name, not by folder — which also makes any
extra prefix in front of the timestamp folder work.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

import boto3
import boto3.exceptions
import botocore.config
import botocore.exceptions

from benchops.awscreds import AwsCredentialsError, aws_session

DEFAULT_URL_EXPIRY_SECONDS = 3600

_KEY_RE = re.compile(
    r"^(?P<stamp>\d{8}_\d{6})-(?P<slug>.+?)-"
    r"(?P<part>database\.sql(?:\.gz)?|files\.t(?:ar|gz)|private-files\.t(?:ar|gz)|site_config_backup\.json)$"
)
_PARTS = {
    "database": "database",
    "files": "public_files",
    "private-files": "private_files",
    "site_config_backup": "site_config",
}
_STAMP_FILTER_RE = re.compile(r"^\d{4}(\d{2}(\d{2}(_\d{2}(\d{2}(\d{2})?)?)?)?)?$")


class BackupSourceError(Exception):
    """Raised when S3 can't be reached or doesn't hold the requested backup."""


@dataclass(frozen=True)
class BackupFile:
    key: str
    size: int

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]


@dataclass
class BackupSet:
    """The files of one backup run of one site."""
    stamp: str
    site_slug: str
    database: BackupFile | None = None
    public_files: BackupFile | None = None
    private_files: BackupFile | None = None
    site_config: BackupFile | None = None
    extra: list[BackupFile] = field(default_factory=list)

    @property
    def taken_at(self) -> datetime:
        return datetime.strptime(self.stamp, "%Y%m%d_%H%M%S")

    @property
    def restorable(self) -> bool:
        return self.database is not None

    def files(self, with_files: bool = True) -> list[BackupFile]:
        """The files a restore downloads: the database, plus the file archives."""
        parts = [self.database]
        if with_files:
            parts += [self.public_files, self.private_files]
        return [part for part in parts if part is not None]


def site_slug(site: str) -> str:
    """The site name as Frappe writes it into backup file names."""
    return site.strip().replace(".", "_")


def parse_backup_key(key: str) -> tuple[str, str, str] | None:
    """(stamp, site_slug, part attribute) for a Frappe backup key, else None."""
    match = _KEY_RE.match(key.rsplit("/", 1)[-1])
    if not match:
        return None
    part = match["part"].split(".", 1)[0]
    return match["stamp"], match["slug"], _PARTS[part]


def group_backups(objects, site: str | None = None) -> list[BackupSet]:
    """Group (key, size) pairs into backup sets, newest first.

    When one stamp holds the same part twice (an upload under two prefixes),
    the first one listed wins; S3 lists keys in lexicographic order.
    """
    wanted = site_slug(site) if site else None
    sets: dict[tuple[str, str], BackupSet] = {}
    for key, size in objects:
        parsed = parse_backup_key(key)
        if parsed is None:
            continue
        stamp, slug, part = parsed
        if wanted and slug != wanted:
            continue
        backup = sets.setdefault((stamp, slug), BackupSet(stamp=stamp, site_slug=slug))
        if getattr(backup, part) is None:
            setattr(backup, part, BackupFile(key=key, size=size))
        else:
            backup.extra.append(BackupFile(key=key, size=size))
    return sorted(sets.values(), key=lambda b: (b.stamp, b.site_slug), reverse=True)


def select_backup(sets: list[BackupSet], at: str | None = None) -> BackupSet:
    """The newest restorable set, or the newest one whose stamp starts with
    `at` (20261019, 20261019_02, 20261019_020000, ...)."""
    at = (at or "latest").strip()
    if at != "latest" and not _STAMP_FILTER_RE.match(at):
        raise BackupSourceError(f"Invalid --at '{at}'. Use 'latest' or a timestamp like 20261019 or 20261019_020000.")
    candidates = [b for b in sets if b.restorable and (at == "latest" or b.stamp.startswith(at))]
    if not candidates:
        when = "" if at == "latest" else f" taken at '{at}'"
        raise BackupSourceError(f"No restorable backup (one with a database dump){when} was found.")
    return candidates[0]


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


class S3Backups:
    """Reads one backup source: a bucket (and optional prefix) in S3 or an
    S3-compatible store."""

    def __init__(self, source: dict) -> None:
        self.bucket = source["bucket"]
        self.prefix = (source.get("prefix") or "").strip("/")
        self._client = self._make_client(source)

    def _make_client(self, source: dict):
        endpoint_url = source.get("endpoint_url")
        region = source.get("aws_region")
        try:
            session = aws_session(source.get("aws_profile"), region)
            config = botocore.config.Config(signature_version="s3v4")
            client = session.client("s3", endpoint_url=endpoint_url, config=config)
            # Presigned URLs only work when signed for the bucket's own region,
            # so look it up when the source doesn't name one.
            if not endpoint_url and not (region or session.region_name):
                try:
                    location = self._call(client.get_bucket_location, Bucket=self.bucket)
                except BackupSourceError as exc:
                    raise BackupSourceError(f"{exc} Pass --aws-region to skip looking up the bucket's region.") from exc
                bucket_region = location.get("LocationConstraint") or "us-east-1"
                client = session.client("s3", region_name=bucket_region, config=config)
        except AwsCredentialsError as exc:
            raise BackupSourceError(str(exc)) from exc
        except botocore.exceptions.BotoCoreError as exc:
            raise BackupSourceError(f"Could not create an S3 client: {exc}") from exc
        return client

    def _error(self, exc: Exception) -> BackupSourceError:
        if isinstance(exc, botocore.exceptions.ClientError):
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "NoSuchBucket":
                return BackupSourceError(f"Bucket '{self.bucket}' does not exist.")
            if code in ("AccessDenied", "403"):
                return BackupSourceError(
                    f"Access to bucket '{self.bucket}' was denied (needs s3:ListBucket and s3:GetObject): {exc}"
                )
            return BackupSourceError(f"S3 rejected the request: {exc}")
        if isinstance(exc, botocore.exceptions.NoCredentialsError):
            return BackupSourceError("No AWS credentials found. Pass --aws-profile, or configure the AWS CLI.")
        return BackupSourceError(f"Failed to reach S3: {exc}")

    def _call(self, method, **kwargs):
        try:
            return method(**kwargs)
        except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as exc:
            raise self._error(exc) from exc

    def _objects(self) -> list[tuple[str, int]]:
        paginator = self._client.get_paginator("list_objects_v2")
        prefix = f"{self.prefix}/" if self.prefix else ""
        try:
            return [
                (obj["Key"], obj["Size"])
                for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix)
                for obj in page.get("Contents", [])
            ]
        except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as exc:
            raise self._error(exc) from exc

    def list_sets(self, site: str | None = None) -> list[BackupSet]:
        return group_backups(self._objects(), site)

    def download(self, key: str, path: str, progress=None) -> None:
        """Download one object to `path` (multipart and parallel for large
        files); `progress` is called with the bytes received since the last call."""
        try:
            self._client.download_file(self.bucket, key, path, Callback=progress)
        except (botocore.exceptions.ClientError, botocore.exceptions.BotoCoreError) as exc:
            raise self._error(exc) from exc
        except boto3.exceptions.Boto3Error as exc:
            raise BackupSourceError(f"Downloading {key} failed: {exc}") from exc

    def presign(self, key: str, expires: int = DEFAULT_URL_EXPIRY_SECONDS) -> str:
        return self._call(
            self._client.generate_presigned_url,
            ClientMethod="get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )

