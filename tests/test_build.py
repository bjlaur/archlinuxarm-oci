import argparse
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import textwrap
import unittest
from unittest import mock

import build


class TerminalBuffer(io.StringIO):
    def isatty(self):
        return True


class ValidationTests(unittest.TestCase):
    def test_password_prompt_confirms_each_password(self):
        responses = iter(["first", "different", "root-secret", "root-secret"])
        with (
            mock.patch.object(build.getpass, "getpass", side_effect=lambda _prompt: next(responses)),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(build.prompt_password("root"), "root-secret")

    def test_builder_collects_distinct_root_and_admin_passwords(self):
        builder = build.Builder(build.parse_args(["--username", "tester"]))
        builder.admin_user = "tester"
        with (
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch.object(build, "prompt_password", side_effect=["root-secret", "admin-secret"]) as prompt,
        ):
            builder.collect_passwords()
        self.assertEqual(builder.passwords, ("root-secret", "admin-secret"))
        self.assertEqual(prompt.call_args_list, [mock.call("root"), mock.call("tester")])

    def test_colorize_uses_ansi_only_for_terminals(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            colored = build.colorize("DONE", build.ANSI_GREEN, stream=TerminalBuffer())
            plain = build.colorize("DONE", build.ANSI_GREEN, stream=io.StringIO())
        self.assertEqual(colored, f"{build.ANSI_GREEN}DONE{build.ANSI_RESET}")
        self.assertEqual(plain, "DONE")

    def test_colorize_respects_no_color(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            rendered = build.colorize("FAILED", build.ANSI_RED, stream=TerminalBuffer())
        self.assertEqual(rendered, "FAILED")

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

    def test_arm_grub_uses_uefi_console_instead_of_x86_com_port(self):
        for name in ("grub.cfg", "grub-smoke.cfg"):
            template = (build.PROJECT / "templates" / name).read_text()
            self.assertNotIn("serial --unit", template)
            self.assertIn("terminal_output console", template)

    def test_test_password_argument(self):
        args = build.parse_args(["--username", "tester", "--password", "only-for-tests"])
        self.assertEqual(args.password, "only-for-tests")

    def test_stage_only_options_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            build.parse_args(["--build-only", "--smoke-test-only"])


class ArchiveTests(unittest.TestCase):
    def test_conversion_uses_zstd_compression(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            args = build.parse_args(["--output", str(base / "output.qcow2")])
            builder = build.Builder(args)
            builder.raw = base / "source.raw"
            builder.raw.write_bytes(b"raw-image")
            with mock.patch.object(build, "run") as mocked_run:
                builder.convert()

            convert = next(
                call.args[0] for call in mocked_run.call_args_list
                if call.args[0][:2] == ["qemu-img", "convert"]
            )
            self.assertIn("compression_type=zstd", convert)

    def test_exact_work_directory_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "exact-workspace"
            args = build.parse_args([
                "--username", "tester", "--work-dir", str(workspace), "--build-only",
            ])
            builder = build.Builder(args)
            builder.start_workspace()
            self.assertEqual(builder.work, workspace.resolve())
            builder.cleanup()
            self.assertTrue(workspace.is_dir())

    def test_conversion_requires_matching_smoke_success_state(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            args = build.parse_args(["--work-dir", str(workspace), "--convert-only"])
            builder = build.Builder(args)
            builder.start_workspace(resume=True)
            builder.admin_user = "tester"
            assert builder.raw is not None
            builder.raw.write_bytes(b"raw-image")
            builder.write_state(root_uuid="root-uuid", smoke_passed=False)

            with self.assertRaisesRegex(RuntimeError, "no successful smoke test"):
                builder.require_matching_state(require_smoke=True)

            builder.write_state(root_uuid="root-uuid", smoke_passed=True)
            builder.require_matching_state(require_smoke=True)
            builder.raw.write_bytes(b"changed-image")
            with self.assertRaisesRegex(RuntimeError, "raw disk changed"):
                builder.require_matching_state(require_smoke=True)

    def test_bootstrap_payload_checks_pci_virtio_disk_serial(self):
        entrypoint = (build.PROJECT / "guest" / "build-entrypoint.sh").read_text()
        self.assertIn("/sys/block/vda/serial", entrypoint)
        self.assertNotIn("/sys/block/vda/device/serial", entrypoint)

    def test_nftables_is_validated_after_reboot_into_installed_kernel(self):
        entrypoint = (build.PROJECT / "guest" / "build-entrypoint.sh").read_text()
        smoke_test = (build.PROJECT / "guest" / "uefi-smoke-test.sh").read_text()
        self.assertNotIn("nft -c -f /etc/nftables.conf", entrypoint)
        self.assertIn("nft -c -f /etc/nftables.conf", smoke_test)

    def test_guest_installs_required_packages_without_full_upgrade(self):
        configure = (build.PROJECT / "guest" / "configure.sh").read_text()
        self.assertNotIn("pacman -Syu", configure)
        self.assertIn("pacman -Sy --needed --noconfirm", configure)

    def test_safe_cleanup_accepts_only_builder_directory_in_system_temp(self):
        workspace = Path(tempfile.mkdtemp(prefix="oci-archarm."))
        (workspace / "example").write_text("value")
        build.safe_rmtree(workspace)
        self.assertFalse(workspace.exists())

        with tempfile.TemporaryDirectory() as directory:
            unsafe = Path(directory) / "oci-archarm.not-direct-child"
            unsafe.mkdir()
            with self.assertRaises(RuntimeError):
                build.safe_rmtree(unsafe)

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

                expected_values = ("root-secret", "root-secret", "admin-secret", "admin-secret")
                for prompt, expected in zip(("New password:", "Retype new password:") * 2, expected_values):
                    attrs = termios.tcgetattr(sys.stdin.fileno())
                    hidden = attrs.copy()
                    hidden[3] &= ~termios.ECHO
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, hidden)
                    print(prompt, end="", flush=True)
                    value = sys.stdin.readline().rstrip("\\r\\n")
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, attrs)
                    print()
                    if value != expected:
                        raise SystemExit(2)
                print("OCI_IMAGE_BUILD_SUCCESS", flush=True)
            """))
            log = base / "serial.log"
            build.ConsoleRunner.run(
                [sys.executable, "-u", fake],
                log=log,
                success=build.BUILD_SUCCESS,
                timeout=10,
                passwords=("root-secret", "admin-secret"),
            )
            self.assertNotIn(b"root-secret", log.read_bytes())
            self.assertNotIn(b"admin-secret", log.read_bytes())


if __name__ == "__main__":
    unittest.main()
