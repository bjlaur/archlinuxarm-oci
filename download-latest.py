#!/usr/bin/env python3
"""Download and verify the latest published Arch Linux ARM OCI image."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse


DEFAULT_RELEASE_BASE = (
    "https://github.com/bjlaur/archlinuxarm-oci/releases/latest/download"
)
BUFFER_SIZE = 1024 * 1024


def asset_url(asset: str) -> str:
    return f"{DEFAULT_RELEASE_BASE}/{urllib.parse.quote(asset)}"


def curl_command(asset: str, *options: str) -> list:
    return [
        "curl",
        "--fail",
        "--location",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--retry",
        "3",
        *options,
        asset_url(asset),
    ]


def fetch(asset: str) -> bytes:
    completed = subprocess.run(
        curl_command(asset, "--silent", "--show-error"),
        check=True,
        stdout=subprocess.PIPE,
    )
    return completed.stdout


def parse_metadata(contents: bytes):
    try:
        info = json.loads(contents)
        image = info["image_filename"]
        checksum = info["image_sha256"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("build-info.json is missing valid image metadata") from error

    if (
        not isinstance(image, str)
        or Path(image).name != image
        or not re.fullmatch(r"[A-Za-z0-9._-]+\.qcow2", image)
    ):
        raise ValueError("build-info.json contains an invalid image filename")
    if not isinstance(checksum, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", checksum
    ):
        raise ValueError("build-info.json contains an invalid image checksum")
    return image, checksum.lower()


def parse_checksum(contents: bytes, image: str) -> str:
    try:
        fields = contents.decode("utf-8").strip().split(maxsplit=1)
    except UnicodeDecodeError as error:
        raise ValueError("checksum asset is not valid UTF-8") from error
    if len(fields) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
        raise ValueError("checksum asset has an invalid format")
    if fields[1].lstrip("*") != image:
        raise ValueError("checksum asset names a different image")
    return fields[0].lower()


def download_image(asset: str, directory: Path):
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{asset}.", suffix=".part", dir=directory
    )
    temporary = Path(temporary_name)

    try:
        os.close(descriptor)
        subprocess.run(
            curl_command(
                asset,
                "--show-error",
                "--progress-bar",
                "--output",
                temporary,
            ),
            check=True,
        )
        digest = hashlib.sha256()
        with temporary.open("rb") as downloaded:
            while chunk := downloaded.read(BUFFER_SIZE):
                digest.update(chunk)
        return temporary, digest.hexdigest()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_new(path: Path, contents: bytes) -> None:
    with path.open("xb") as output:
        output.write(contents)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="download directory (default: current directory)",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    destination = Path(args.directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if shutil.which("curl") is None:
        raise OSError("curl is required but was not found")

    print("Downloading build-info.json", flush=True)
    metadata_contents = fetch("build-info.json")
    image, expected_checksum = parse_metadata(metadata_contents)
    checksum_name = f"{image}.sha256"
    targets = [
        destination / "build-info.json",
        destination / image,
        destination / checksum_name,
    ]
    existing = [path for path in targets if path.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite: {existing[0]}")

    print(f"Downloading {checksum_name}", flush=True)
    checksum_contents = fetch(checksum_name)
    published_checksum = parse_checksum(checksum_contents, image)
    if published_checksum != expected_checksum:
        raise ValueError("checksum asset does not match build-info.json")

    print(f"Downloading {image}", flush=True)
    temporary, actual_checksum = download_image(image, destination)
    created = []
    try:
        if actual_checksum != expected_checksum:
            raise ValueError(
                "downloaded image does not match its published SHA-256 checksum"
            )

        write_new(destination / "build-info.json", metadata_contents)
        created.append(destination / "build-info.json")
        write_new(destination / checksum_name, checksum_contents)
        created.append(destination / checksum_name)
        temporary.rename(destination / image)
    except BaseException:
        temporary.unlink(missing_ok=True)
        for path in created:
            path.unlink(missing_ok=True)
        raise

    print(f"Verified SHA-256: {actual_checksum}")
    print(f"Downloaded: {destination / image}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
