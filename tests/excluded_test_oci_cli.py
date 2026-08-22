#!/usr/bin/env python3
# This file intentionally does not start with "test_", so default unittest
# discovery does not require the optional OCI CLI. Run it explicitly with:
# python3 tests/excluded_test_oci_cli.py -v

import shutil
import subprocess
import unittest


class OCICommandSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("oci") is None:
            raise unittest.SkipTest("OCI CLI is not installed")

    def help_text(self, *arguments):
        return subprocess.run(
            ["oci", *arguments, "--help"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout

    def assert_options(self, command, *options):
        help_text = self.help_text(*command)
        for option in options:
            with self.subTest(command=command, option=option):
                self.assertIn(option, help_text)

    def test_image_import_options(self):
        self.assert_options(
            ("compute", "image", "import", "from-object"),
            "--bucket-name",
            "--compartment-id",
            "--display-name",
            "--freeform-tags",
            "--launch-mode",
            "--name",
            "--namespace",
            "--operating-system",
            "--operating-system-version",
            "--source-image-type",
        )

    def test_instance_launch_options(self):
        self.assert_options(
            ("compute", "instance", "launch"),
            "--assign-public-ip",
            "--availability-domain",
            "--boot-volume-size-in-gbs",
            "--compartment-id",
            "--freeform-tags",
            "--image-id",
            "--shape-config",
            "--ssh-authorized-keys-file",
            "--subnet-id",
        )

    def test_object_upload_options(self):
        self.assert_options(
            ("os", "object", "put"),
            "--metadata",
            "--no-overwrite",
            "--verify-checksum",
        )

    def test_placement_discovery_options(self):
        self.assert_options(
            ("search", "resource", "structured-search"),
            "--limit",
            "--query-text",
        )
        self.assert_options(
            ("iam", "availability-domain", "list"),
            "--all",
            "--compartment-id",
        )
        self.assert_options(
            ("network", "subnet", "get"),
            "--subnet-id",
        )

    def test_global_request_id_option(self):
        self.assertIn("--opc-client-request-id", self.help_text())


if __name__ == "__main__":
    unittest.main()
