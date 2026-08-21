import argparse
from pathlib import Path
import sys
import tarfile
import tempfile
import textwrap
import unittest

import build


class ValidationTests(unittest.TestCase):
    def test_username(self):
        self.assertEqual(build.validate_username("admin_2"), "admin_2")
        for invalid in ("root", "alarm", "Admin", "two words", "-admin", ""):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build.validate_username(invalid)

    def test_hostname(self):
        self.assertEqual(build.validate_hostname("oracle-arm"), "oracle-arm")
        with self.assertRaises(SystemExit):
            build.validate_hostname("Oracle_ARM")

    def test_templates_have_no_unexpanded_values(self):
        rendered = build.render_template("sshd-security.conf", ADMIN_USER="tester")
        self.assertIn("AllowUsers tester", rendered)
        self.assertNotIn("{{", rendered)

    def test_test_password_argument(self):
        args = build.parse_args(["--username", "tester", "--password", "only-for-tests"])
        self.assertEqual(args.password, "only-for-tests")


class ArchiveTests(unittest.TestCase):
    def test_payload_tar_forces_root_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "example").write_text("value")
            archive_path = base / "payload.tar.gz"
            build.Builder.write_root_owned_tar(source, archive_path)
            with tarfile.open(archive_path, "r:gz") as archive:
                member = archive.getmember("example")
                self.assertEqual((member.uid, member.gid), (0, 0))

    def test_package_owned_configuration_is_applied_after_pacman(self):
        with tempfile.TemporaryDirectory() as directory:
            args = build.parse_args(["--username", "tester"])
            builder = build.Builder(args)
            builder.work = Path(directory)
            builder.admin_user = "tester"
            payload = builder.create_build_payload("root-uuid", "esp-uuid")
            with tarfile.open(payload, "r:gz") as archive:
                names = set(archive.getnames())

            prefix = "usr/local/lib/archlinuxarm-oci-builder/final-root/"
            self.assertNotIn("etc/nftables.conf", names)
            self.assertNotIn("etc/sshguard.conf", names)
            self.assertIn(prefix + "etc/nftables.conf", names)
            self.assertIn(prefix + "etc/sshguard.conf", names)
            self.assertIn("etc/fstab", names)
            self.assertIn("etc/systemd/network/20-oci.network", names)
            self.assertIn("etc/systemd/system/oci-image-build.service", names)


class ConsoleTests(unittest.TestCase):
    def test_password_automation_is_not_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fake = base / "fake_guest.py"
            fake.write_text(textwrap.dedent("""
                import sys
                import termios

                for prompt in ("New password:", "Retype new password:") * 2:
                    attrs = termios.tcgetattr(sys.stdin.fileno())
                    hidden = attrs.copy()
                    hidden[3] &= ~termios.ECHO
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, hidden)
                    print(prompt, end="", flush=True)
                    value = sys.stdin.readline().rstrip("\\r\\n")
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, attrs)
                    print()
                    if value != "only-for-tests":
                        raise SystemExit(2)
                print("OCI_IMAGE_BUILD_SUCCESS", flush=True)
            """))
            log = base / "serial.log"
            build.ConsoleRunner.run(
                [sys.executable, "-u", fake],
                log=log,
                success=build.BUILD_SUCCESS,
                timeout=10,
                password="only-for-tests",
            )
            self.assertNotIn(b"only-for-tests", log.read_bytes())


if __name__ == "__main__":
    unittest.main()
