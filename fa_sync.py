#!/usr/bin/env -S uv run
# /// script
# dependencies = [
#   "google-cloud-storage>=2.19.0",
#   "google-crc32c>=1.6.0",
# ]
# ///

from __future__ import annotations

import argparse
import base64
import os
import posixpath
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import google_crc32c
from google.cloud import storage


DEFAULT_JOBS = 2


@dataclass(frozen=True)
class RemoteObject:
    name: str
    crc32c: str | None
    size: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync selected filing artifacts from a GCS prefix to a local directory."
    )
    parser.add_argument(
        "--type",
        choices=("filing", "index"),
        default="filing",
        help="Content type to sync. Default: filing.",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Source GCS path in the form gs://bucket/prefix",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        type=Path,
        help="Local destination directory.",
    )
    parser.add_argument(
        "filing_list",
        type=Path,
        metavar="FILING_LIST",
        help="Manifest file listing <cik>/<accession> entries to evaluate.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Copy all listed remote files even when the local checksum already matches.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the sync but do not copy any files.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print checksum-match skip lines.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Concurrent download workers. Default: {DEFAULT_JOBS}.",
    )
    return parser.parse_args()


def parse_gs_url(url: str) -> tuple[str, str]:
    if not url.startswith("gs://"):
        raise ValueError(f"Expected gs:// URL, got: {url}")

    remainder = url[5:]
    bucket, _, prefix = remainder.partition("/")
    if not bucket:
        raise ValueError(f"Missing bucket in source URL: {url}")
    return bucket, prefix.rstrip("/")


def iter_manifest_entries(path: Path) -> Iterable[tuple[str, str]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue

            cik, sep, accession = line.partition("/")
            if not sep or not cik or not accession:
                raise ValueError(
                    f"Invalid manifest entry at {path}:{line_number}: {raw_line.rstrip()}"
                )
            yield cik, accession


def expected_relative_paths(content_type: str, cik: str, accession: str) -> list[str]:
    if content_type == "filing":
        return [
            f"{cik}/{accession}_bundle.json.gz",
            f"{cik}/{accession}.md",
        ]
    return [f"{cik}/{accession}_index.json.gz"]


def join_prefix(prefix: str, relative_path: str) -> str:
    return posixpath.join(prefix, relative_path) if prefix else relative_path


def crc32c_for_file(path: Path) -> str:
    checksum = google_crc32c.Checksum()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    return base64.b64encode(checksum.digest()).decode("ascii")


def filing_label(relative_path: str) -> str:
    cik, _, filename = relative_path.partition("/")
    accession = filename
    for suffix in ("_bundle.json.gz", "_index.json.gz", ".md"):
        if accession.endswith(suffix):
            accession = accession[: -len(suffix)]
            break
    return f"{cik.zfill(7)}/{accession}"


def artifact_label(relative_path: str) -> str:
    if relative_path.endswith("_bundle.json.gz"):
        return "bundle"
    if relative_path.endswith("_index.json.gz"):
        return "index "
    if relative_path.endswith(".md"):
        return "md    "
    return "file  "


def download_one(
    client: storage.Client,
    bucket_name: str,
    remote: RemoteObject,
    local_path: Path,
) -> None:
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(remote.name)

    local_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=local_path.parent,
        prefix=f".{local_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_name = handle.name

    try:
        blob.download_to_filename(temp_name)
        if remote.crc32c is not None:
            downloaded_crc32c = crc32c_for_file(Path(temp_name))
            if downloaded_crc32c != remote.crc32c:
                raise ValueError(
                    f"checksum verification failed for {remote.name}: "
                    f"expected {remote.crc32c}, got {downloaded_crc32c}"
                )
        os.replace(temp_name, local_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise

@dataclass
class SyncStats:
    checked: int = 0
    missing_remote: int = 0
    skipped: int = 0
    copied: int = 0
    failed: int = 0


def sync_manifest(
    *,
    client: storage.Client,
    bucket_name: str,
    prefix: str,
    content_type: str,
    manifest_path: Path,
    output_dir: Path,
    force_refresh: bool,
    dry_run: bool,
    verbose: bool,
) -> SyncStats:
    stats = SyncStats()
    seen_local_paths: set[Path] = set()
    bucket = client.bucket(bucket_name)

    for cik, accession in iter_manifest_entries(manifest_path):
        for relative_path in expected_relative_paths(content_type, cik, accession):
            local_path = output_dir / relative_path
            if local_path in seen_local_paths:
                continue
            seen_local_paths.add(local_path)

            remote_name = join_prefix(prefix, relative_path)
            stats.checked += 1
            blob = bucket.get_blob(remote_name)
            if blob is None:
                stats.missing_remote += 1
                print(f"WARNING: missing remote object: {remote_name}", file=sys.stderr)
                continue

            remote = RemoteObject(name=blob.name, crc32c=blob.crc32c, size=blob.size)

            reason: str | None = None
            if force_refresh:
                reason = "forced"
            elif not local_path.is_file():
                reason = "missing local"
            elif remote.crc32c is None:
                reason = "missing remote checksum"
            else:
                local_crc32c = crc32c_for_file(local_path)
                if local_crc32c != remote.crc32c:
                    reason = "checksum mismatch"

            if reason is None:
                stats.skipped += 1
                if verbose:
                    print(
                        f"skipping {filing_label(relative_path)} {artifact_label(relative_path)} "
                        f"[checksum match]"
                    )
                continue

            if dry_run:
                stats.copied += 1
                print(
                    f"copying  {filing_label(relative_path)} {artifact_label(relative_path)} "
                    f"[{reason}]"
                )
                continue

            try:
                download_one(client, bucket_name, remote, local_path)
            except Exception as exc:
                stats.failed += 1
                print(
                    f"ERROR: failed to copy {filing_label(relative_path)}: {exc}",
                    file=sys.stderr,
                )
                continue

            stats.copied += 1
            print(
                f"copying  {filing_label(relative_path)} {artifact_label(relative_path)} "
                f"[{reason}]"
            )

    return stats


def main() -> int:
    args = parse_args()
    bucket_name, prefix = parse_gs_url(args.source)

    output_dir = args.output.expanduser().resolve()
    manifest_path = args.filing_list.expanduser().resolve()

    if not manifest_path.is_file():
        print(f"Manifest file not found: {manifest_path}", file=sys.stderr)
        return 2

    client = storage.Client()

    stats = sync_manifest(
        client=client,
        bucket_name=bucket_name,
        prefix=prefix,
        content_type=args.type,
        manifest_path=manifest_path,
        output_dir=output_dir,
        force_refresh=args.refresh,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    print(
        "Summary: "
        f"checked={stats.checked} "
        f"copied={stats.copied} "
        f"skipped={stats.skipped} "
        f"missing_remote={stats.missing_remote} "
        f"failed={stats.failed}"
    )
    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
