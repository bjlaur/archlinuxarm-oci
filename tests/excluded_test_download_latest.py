# This file intentionally starts with "excluded_", so the project's default
# unittest discovery does not run downloader-specific tests on every build.
# Run it explicitly with: python3 tests/excluded_test_download_latest.py -v

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "download-latest.py"
SPEC = importlib.util.spec_from_file_location("download_latest", SCRIPT)
download_latest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(download_latest)


class DownloadLatestTests(unittest.TestCase):
    def test_metadata_and_checksum_agree_on_image(self):
        metadata = json.dumps(
            {
                "image_filename": "archlinuxarm-oci-aarch64-test.qcow2",
                "image_sha256": "a" * 64,
            }
        ).encode()
        image, checksum = download_latest.parse_metadata(metadata)
        self.assertEqual(image, "archlinuxarm-oci-aarch64-test.qcow2")
        self.assertEqual(checksum, "a" * 64)
        self.assertEqual(
            download_latest.parse_checksum(f"{'a' * 64}  {image}\n".encode(), image),
            checksum,
        )

    def test_metadata_rejects_unsafe_image_filename(self):
        metadata = json.dumps(
            {"image_filename": "../image.qcow2", "image_sha256": "a" * 64}
        ).encode()
        with self.assertRaisesRegex(ValueError, "invalid image filename"):
            download_latest.parse_metadata(metadata)

    def test_checksum_rejects_a_different_image(self):
        with self.assertRaisesRegex(ValueError, "different image"):
            download_latest.parse_checksum(
                f"{'a' * 64}  other.qcow2\n".encode(), "expected.qcow2"
            )

    def test_main_keeps_only_a_verified_image(self):
        image = "test.qcow2"
        contents = b"image contents"
        checksum = download_latest.hashlib.sha256(contents).hexdigest()
        metadata = json.dumps(
            {"image_filename": image, "image_sha256": checksum}
        ).encode()
        checksum_asset = f"{checksum}  {image}\n".encode()

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "downloads"

            def fake_download(_asset, target_directory):
                temporary = target_directory / ".image.part"
                temporary.write_bytes(contents)
                return temporary, checksum

            with (
                mock.patch.object(
                    download_latest,
                    "fetch",
                    side_effect=[metadata, checksum_asset],
                ),
                mock.patch.object(download_latest, "download_image", fake_download),
                mock.patch.object(download_latest.shutil, "which", return_value="/usr/bin/curl"),
                mock.patch("sys.stdout", new=io.StringIO()),
            ):
                self.assertEqual(download_latest.main([str(destination)]), 0)

            self.assertEqual((destination / image).read_bytes(), contents)
            self.assertEqual((destination / "build-info.json").read_bytes(), metadata)
            self.assertEqual(
                (destination / f"{image}.sha256").read_bytes(), checksum_asset
            )


if __name__ == "__main__":
    unittest.main()
