"""Finding Frappe backups in an S3 listing and picking one to restore."""

import io

import boto3.exceptions
import botocore.exceptions
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from benchops.backups import (
    BackupSourceError,
    S3Backups,
    group_backups,
    human_size,
    parse_backup_key,
    select_backup,
    site_slug,
)


def frappe_keys(stamp, slug, prefix="", parts=("database.sql.gz", "files.tar", "private-files.tar",
                                                "site_config_backup.json")):
    folder = f"{prefix}{stamp}/"
    return [(f"{folder}{stamp}-{slug}-{part}", 100 + i) for i, part in enumerate(parts)]


def test_site_slug_matches_frappe_file_names():
    assert site_slug("test-16.akwad.qa") == "test-16_akwad_qa"


@pytest.mark.parametrize("name, part", [
    ("20261019_020000-erp_akwad_qa-database.sql.gz", "database"),
    ("20261019_020000-erp_akwad_qa-files.tar", "public_files"),
    ("20261019_020000-erp_akwad_qa-files.tgz", "public_files"),
    ("20261019_020000-erp_akwad_qa-private-files.tar", "private_files"),
    ("20261019_020000-erp_akwad_qa-site_config_backup.json", "site_config"),
])
def test_parse_backup_key_recognises_every_part(name, part):
    assert parse_backup_key(f"20261019_020000/{name}") == ("20261019_020000", "erp_akwad_qa", part)


def test_private_files_are_not_mistaken_for_a_site_named_private():
    # Non-greedy slug: "-private-files.tar" is the part, not "private" in the slug.
    assert parse_backup_key("20261019_020000-erp_qa-private-files.tar")[1:] == ("erp_qa", "private_files")
    assert parse_backup_key("20261019_020000-my-private_qa-files.tar")[1:] == ("my-private_qa", "public_files")


@pytest.mark.parametrize("key", ["README.md", "20261019_020000/notes.txt", "2026_020000-site-database.sql.gz"])
def test_parse_backup_key_ignores_other_objects(key):
    assert parse_backup_key(key) is None


def test_group_backups_by_run_and_site_newest_first():
    objects = (
        frappe_keys("20261018_020000", "erp_akwad_qa")
        + frappe_keys("20261019_020000", "erp_akwad_qa")
        + frappe_keys("20261019_020000", "hr_akwad_qa")
        + [("unrelated/file.txt", 1)]
    )
    sets = group_backups(objects)
    assert [(b.stamp, b.site_slug) for b in sets] == [
        ("20261019_020000", "hr_akwad_qa"),
        ("20261019_020000", "erp_akwad_qa"),
        ("20261018_020000", "erp_akwad_qa"),
    ]
    assert sets[1].database.key == "20261019_020000/20261019_020000-erp_akwad_qa-database.sql.gz"
    assert sets[1].public_files and sets[1].private_files and sets[1].site_config


def test_group_backups_filters_by_site_name_and_works_under_a_prefix():
    objects = frappe_keys("20261019_020000", "erp_akwad_qa", prefix="backups/prod/") + frappe_keys(
        "20261019_020000", "hr_akwad_qa", prefix="backups/prod/"
    )
    sets = group_backups(objects, site="erp.akwad.qa")
    assert [b.site_slug for b in sets] == ["erp_akwad_qa"]


def test_files_lists_the_database_and_only_archives_that_exist():
    backup = group_backups(frappe_keys("20261019_020000", "erp", parts=("database.sql.gz", "files.tar")))[0]
    assert [f.name.split("-", 2)[2] for f in backup.files()] == ["database.sql.gz", "files.tar"]
    assert [f.name.split("-", 2)[2] for f in backup.files(with_files=False)] == ["database.sql.gz"]


def test_select_backup_skips_sets_without_a_database():
    sets = group_backups(
        frappe_keys("20261019_020000", "erp", parts=("files.tar",))
        + frappe_keys("20261018_020000", "erp")
    )
    assert select_backup(sets).stamp == "20261018_020000"


def test_select_backup_by_timestamp_prefix():
    sets = group_backups(
        frappe_keys("20261019_140000", "erp") + frappe_keys("20261019_020000", "erp")
        + frappe_keys("20261018_020000", "erp")
    )
    assert select_backup(sets, "latest").stamp == "20261019_140000"
    assert select_backup(sets, "20261019").stamp == "20261019_140000"
    assert select_backup(sets, "20261019_02").stamp == "20261019_020000"
    assert select_backup(sets, "20261018_020000").stamp == "20261018_020000"


@pytest.mark.parametrize("at", ["20261017", "yesterday", "2026-10-19"])
def test_select_backup_reports_missing_or_malformed_stamps(at):
    with pytest.raises(BackupSourceError):
        select_backup(group_backups(frappe_keys("20261019_020000", "erp")), at)


# --- S3Backups against botocore's stubbed S3 ------------------------------------

@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def stubbed(source):
    s3 = S3Backups(source)
    return s3, Stubber(s3._client)


def test_list_sets_pages_through_the_prefix(aws_env):
    s3, stub = stubbed({"bucket": "akwad-backups", "prefix": "/prod/", "aws_region": "me-south-1"})
    page = [{"Key": f"prod/{key}", "Size": size} for key, size in frappe_keys("20261019_020000", "erp")]
    expected = {"Bucket": "akwad-backups", "Prefix": "prod/"}
    stub.add_response("list_objects_v2", {"Contents": page[:2], "IsTruncated": True, "NextContinuationToken": "t"}, expected)
    stub.add_response("list_objects_v2", {"Contents": page[2:], "IsTruncated": False}, {**expected, "ContinuationToken": "t"})
    with stub:
        sets = s3.list_sets("erp")
    assert len(sets) == 1 and sets[0].private_files.size == 102


def test_missing_bucket_is_a_readable_error(aws_env):
    s3, stub = stubbed({"bucket": "nope", "aws_region": "me-south-1"})
    stub.add_client_error("list_objects_v2", service_error_code="NoSuchBucket")
    with stub, pytest.raises(BackupSourceError, match="'nope' does not exist"):
        s3.list_sets()


def test_presign_signs_v4_for_the_bucket_region_without_network(aws_env):
    s3 = S3Backups({"bucket": "akwad-backups", "aws_region": "me-south-1"})
    url = s3.presign("20261019_020000/x-database.sql.gz", expires=600)
    assert "me-south-1" in url and "X-Amz-Expires=600" in url and "X-Amz-Signature=" in url


# --- client setup: region lookup and credentials ---------------------------------


class FakeClient:
    def __init__(self, region, location=None, error=None):
        self.region = region
        self.location = location
        self.error = error

    def get_bucket_location(self, Bucket):
        if self.error:
            raise self.error
        return {"LocationConstraint": self.location}

    def get_paginator(self, name):
        error = self.error

        class Paginator:
            def paginate(self, **kwargs):
                if error:
                    raise error
                return [{"Contents": []}]

        return Paginator()


@pytest.fixture
def fake_session(monkeypatch):
    made = []

    class FakeSession:
        location = None
        error = None
        region_name = None

        def __init__(self, profile_name=None, region_name=None):
            self.profile_name = profile_name
            self.region_name = region_name

        def client(self, service, endpoint_url=None, region_name=None, config=None):
            client = FakeClient(region_name or self.region_name, FakeSession.location, FakeSession.error)
            made.append((client, endpoint_url))
            return client

    monkeypatch.setattr("benchops.backups.boto3.Session", FakeSession)
    return FakeSession, made


@pytest.mark.parametrize("location, region", [("eu-west-1", "eu-west-1"), (None, "us-east-1")])
def test_region_is_looked_up_when_the_source_has_none(fake_session, location, region):
    session, made = fake_session
    session.location = location
    s3 = S3Backups({"bucket": "b"})
    assert s3._client.region == region and len(made) == 2


def test_no_lookup_with_a_region_or_an_endpoint(fake_session):
    _, made = fake_session
    S3Backups({"bucket": "b", "aws_region": "me-south-1"})
    S3Backups({"bucket": "b", "endpoint_url": "https://minio.local"})
    assert len(made) == 2 and made[1][1] == "https://minio.local"


def test_denied_region_lookup_suggests_passing_the_region(fake_session):
    session, _ = fake_session
    session.error = botocore.exceptions.ClientError({"Error": {"Code": "AccessDenied"}}, "GetBucketLocation")
    with pytest.raises(BackupSourceError, match="denied.*--aws-region"):
        S3Backups({"bucket": "b"})


def test_missing_credentials_are_named(fake_session):
    session, _ = fake_session
    session.error = botocore.exceptions.NoCredentialsError()
    with pytest.raises(BackupSourceError, match="No AWS credentials"):
        S3Backups({"bucket": "b", "aws_region": "me-south-1"}).list_sets()


def test_unknown_profile_is_a_backup_source_error(monkeypatch):
    def missing(**kwargs):
        raise botocore.exceptions.ProfileNotFound(profile="nope")

    monkeypatch.setattr("benchops.backups.boto3.Session", missing)
    with pytest.raises(BackupSourceError, match="nope"):
        S3Backups({"bucket": "b", "aws_profile": "nope"})


@pytest.mark.parametrize("size, text", [(512, "512 B"), (2048, "2.0 KB"), (5 * 1024**3, "5.0 GB"), (3 * 1024**4, "3.0 TB")])
def test_human_size(size, text):
    assert human_size(size) == text


def test_a_part_uploaded_twice_keeps_the_first_and_records_the_other():
    objects = frappe_keys("20261019_020000", "erp", prefix="a/") + frappe_keys("20261019_020000", "erp", prefix="b/")
    (backup,) = group_backups(objects)
    assert backup.database.key.startswith("a/") and len(backup.extra) == 4


def test_other_s3_errors_keep_their_detail(fake_session):
    session, _ = fake_session
    session.error = botocore.exceptions.ClientError({"Error": {"Code": "SlowDown"}}, "ListObjectsV2")
    with pytest.raises(BackupSourceError, match="S3 rejected the request.*SlowDown"):
        S3Backups({"bucket": "b", "aws_region": "me-south-1"}).list_sets()


def test_network_failures_are_named(fake_session):
    session, _ = fake_session
    session.error = botocore.exceptions.EndpointConnectionError(endpoint_url="https://s3.me-south-1.amazonaws.com")
    with pytest.raises(BackupSourceError, match="Failed to reach S3"):
        S3Backups({"bucket": "b", "aws_region": "me-south-1"}).list_sets()


# --- download -------------------------------------------------------------------


def test_download_writes_the_object_and_reports_progress(aws_env, tmp_path):
    data = b"x" * 4096
    s3, stub = stubbed({"bucket": "akwad-backups", "aws_region": "me-south-1"})
    stub.add_response("head_object", {"ContentLength": len(data)}, {"Bucket": "akwad-backups", "Key": "k"})
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(data), len(data)), "ContentLength": len(data)},
    )
    received = []
    with stub:
        s3.download("k", str(tmp_path / "out"), received.append)
    assert (tmp_path / "out").read_bytes() == data and sum(received) == len(data)


@pytest.mark.parametrize("error, message", [
    (botocore.exceptions.ClientError({"Error": {"Code": "403"}}, "HeadObject"), "denied"),
    (botocore.exceptions.ClientError({"Error": {"Code": "404"}}, "HeadObject"), "S3 rejected the request"),
])
def test_download_errors_are_translated(aws_env, tmp_path, error, message):
    s3 = S3Backups({"bucket": "b", "aws_region": "me-south-1"})

    def fail(*args, **kwargs):
        raise error

    s3._client.download_file = fail
    with pytest.raises(BackupSourceError, match=message):
        s3.download("k", str(tmp_path / "out"))


def test_transfer_failures_are_translated(aws_env, tmp_path):
    s3 = S3Backups({"bucket": "b", "aws_region": "me-south-1"})

    def fail(*args, **kwargs):
        raise boto3.exceptions.S3TransferFailedError("retries exceeded")

    s3._client.download_file = fail
    with pytest.raises(BackupSourceError, match="Downloading k failed: retries exceeded"):
        s3.download("k", str(tmp_path / "out"))
