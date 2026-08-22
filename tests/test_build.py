import argparse
import io
import inspect
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
        rendered = build.render_template("sshd-security-development.conf", IMAGE_USER="tester")
        self.assertIn("AllowUsers tester", rendered)
        self.assertNotIn("{{", rendered)

    def test_factory_cli_and_conflicts(self):
        args = build.parse_args(["--factory-image"])
        self.assertTrue(args.factory_image)
        with self.assertRaises(SystemExit):
            build.parse_args(["--factory-image", "--username", "tester"])
        with self.assertRaises(SystemExit):
            build.parse_args(["--factory-image", "--password", "secret"])
        with self.assertRaises(SystemExit):
            build.parse_args(["--factory-image", "--smoke-test-only", "--work-dir", "/tmp/work"])

    def test_acceleration_cli_defaults_and_choices(self):
        self.assertEqual(build.parse_args([]).accel, "auto")
        for accel in ("auto", "kvm", "tcg"):
            self.assertEqual(build.parse_args(["--accel", accel]).accel, accel)
        with self.assertRaises(SystemExit):
            build.parse_args(["--accel", "invalid"])

    def test_default_image_size_is_four_gibibytes(self):
        self.assertEqual(build.parse_args([]).image_size, "4G")

    def test_acceleration_selection(self):
        cases = (
            ("auto", True, "kvm"),
            ("auto", False, "tcg"),
            ("tcg", True, "tcg"),
            ("tcg", False, "tcg"),
        )
        for requested, usable, expected in cases:
            with self.subTest(requested=requested, usable=usable):
                builder = build.Builder(build.parse_args(["--accel", requested]))
                with mock.patch.object(builder, "probe_kvm", return_value=usable):
                    self.assertEqual(builder.select_acceleration(), expected)
        builder = build.Builder(build.parse_args(["--accel", "kvm"]))
        with mock.patch.object(builder, "probe_kvm", return_value=False):
            with self.assertRaisesRegex(SystemExit, "KVM requested"):
                builder.select_acceleration()

    def test_kvm_probe_requires_native_arch_device_access_and_qemu(self):
        completed = build.subprocess.CompletedProcess(
            ["qemu-system-aarch64"], 0, stdout='{"return": {}}', stderr=""
        )
        with (
            mock.patch.object(build.platform, "machine", return_value="aarch64"),
            mock.patch.object(build.Path, "exists", return_value=True),
            mock.patch.object(build.os, "access", return_value=True),
            mock.patch.object(build.subprocess, "run", return_value=completed) as probe,
        ):
            self.assertTrue(build.Builder.probe_kvm())
            self.assertIn("virt,accel=kvm", probe.call_args.args[0])
        with mock.patch.object(build.platform, "machine", return_value="x86_64"):
            self.assertFalse(build.Builder.probe_kvm())
        with (
            mock.patch.object(build.platform, "machine", return_value="aarch64"),
            mock.patch.object(build.Path, "exists", return_value=True),
            mock.patch.object(build.os, "access", return_value=False),
        ):
            self.assertFalse(build.Builder.probe_kvm())

    def test_acceleration_qemu_arguments_are_shared(self):
        builder = build.Builder(build.parse_args(["--accel", "tcg"]))
        self.assertEqual(builder.qemu_machine_args(), ["-machine", "virt,accel=tcg", "-cpu", "max"])
        builder = build.Builder(build.parse_args(["--accel", "kvm"]))
        with mock.patch.object(builder, "probe_kvm", return_value=True):
            self.assertEqual(builder.qemu_machine_args(), ["-machine", "virt,accel=kvm", "-cpu", "host"])
        source = inspect.getsource(build.Builder)
        self.assertGreaterEqual(source.count("*self.qemu_machine_args()"), 2)

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
    def test_factory_build_skips_credentials(self):
        builder = build.Builder(build.parse_args(["--factory-image"]))
        with (
            mock.patch.object(builder, "check_environment"),
            mock.patch.object(builder, "confirm_output"),
            mock.patch.object(builder, "start_workspace"),
            mock.patch.object(builder, "run_build_stage", return_value=("root-uuid", "esp-uuid")),
            mock.patch.object(builder, "run_uefi_smoke_test"),
            mock.patch.object(builder, "write_state"),
            mock.patch.object(builder, "convert"),
            mock.patch.object(builder, "collect_passwords") as collect,
        ):
            builder.build()
        self.assertEqual(builder.build_mode, "factory")
        self.assertEqual(builder.admin_user, "alarm")
        collect.assert_not_called()

    def test_factory_and_development_ssh_templates(self):
        development = (build.PROJECT / "templates/sshd-security-development.conf").read_text()
        factory = (build.PROJECT / "templates/sshd-security-factory.conf").read_text()
        self.assertIn("PasswordAuthentication yes", development)
        self.assertIn("PubkeyAuthentication no", development)
        self.assertIn("AllowUsers alarm", factory)
        self.assertIn("PasswordAuthentication no", factory)
        self.assertIn("PubkeyAuthentication yes", factory)

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

    def test_state_v2_infers_factory_mode_and_rejects_v1(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            builder = build.Builder(build.parse_args(["--work-dir", str(workspace), "--convert-only"]))
            builder.start_workspace(resume=True)
            builder.admin_user = "alarm"
            builder.build_mode = "factory"
            assert builder.raw is not None and builder.state_file is not None
            builder.raw.write_bytes(b"raw-image")
            builder.write_state(root_uuid="root-uuid", smoke_passed=True)

            resumed = build.Builder(build.parse_args(["--work-dir", str(workspace), "--convert-only"]))
            resumed.start_workspace(resume=True)
            resumed.require_matching_state(require_smoke=True)
            self.assertEqual((resumed.build_mode, resumed.admin_user), ("factory", "alarm"))

            state = resumed.load_state()
            assert state is not None
            state["version"] = 1
            resumed.state_file.write_text(build.json.dumps(state))
            with self.assertRaisesRegex(RuntimeError, "unsupported workspace state"):
                resumed.load_state()

    def test_factory_payload_has_cloud_init_policy_and_no_key(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = build.Builder(build.parse_args(["--factory-image"]))
            builder.work = Path(directory)
            builder.admin_user = "alarm"
            payload = builder.create_build_payload("root-uuid", "esp-uuid")
            with tarfile.open(payload, "r:gz") as archive:
                names = set(archive.getnames())
                cloud_cfg = archive.extractfile(
                    "usr/local/lib/archlinuxarm-oci-builder/final-root/etc/cloud/cloud.cfg.d/90-oci-alarm.cfg"
                )
                assert cloud_cfg is not None
                text = cloud_cfg.read().decode()
            self.assertIn("name: alarm", text)
            self.assertIn(
                "usr/local/lib/archlinuxarm-oci-builder/final-root/etc/sudoers.d/20-alarm-cloud",
                names,
            )
            self.assertFalse(any("authorized_keys" in name for name in names))

    def test_factory_nocloud_seed_contains_public_data_only(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = build.Builder(build.parse_args(["--factory-image"]))
            builder.work = Path(directory)
            with (
                mock.patch.object(build, "run"),
                mock.patch.object(builder, "guestfish"),
            ):
                seed = builder.create_nocloud_seed()
            self.assertEqual(seed, Path(directory) / "nocloud-seed.raw")
            user_data = (Path(directory) / "nocloud-seed/user-data").read_text()
            self.assertIn(build.SMOKE_PUBLIC_KEY, user_data)
            self.assertNotIn("PRIVATE KEY", user_data)
            self.assertFalse(any("private" in path.name.lower() for path in Path(directory).rglob("*")))

    def test_factory_validation_enforces_locked_keyless_cloud_image(self):
        builder = build.Builder(build.parse_args(["--factory-image"]))
        builder.admin_user = "alarm"
        files = {
            "/var/lib/archlinuxarm-oci/build-success": "OCI_IMAGE_BUILD_SUCCESS\n",
            "/etc/passwd": (
                "root:x:0:0::/root:/usr/bin/bash\n"
                "alarm:x:1000:1000::/home/alarm:/bin/bash\n"
            ),
            "/etc/shadow": "root:!:1::::::\nalarm:!:1::::::\n",
            "/etc/ssh/sshd_config.d/10-oci-security.conf": (
                build.PROJECT / "templates/sshd-security-factory.conf"
            ).read_text(),
            "/etc/cloud/cloud.cfg.d/90-oci-alarm.cfg": (
                build.PROJECT / "templates/cloud-init-alarm.cfg"
            ).read_text(),
            "/etc/sudoers.d/20-alarm-cloud": (
                build.PROJECT / "templates/sudoers-alarm"
            ).read_text(),
            "/etc/machine-id": "",
        }
        required = {
            "/usr/bin/cloud-init",
            "/usr/lib/systemd/system-generators/cloud-init-generator",
            "/usr/lib/systemd/system/cloud-init.target",
            "/usr/lib/systemd/system/cloud-init-local.service",
            "/usr/lib/systemd/system/cloud-init-main.service",
            "/usr/lib/systemd/system/cloud-final.service",
        }
        with (
            mock.patch.object(builder, "read_guest_file", side_effect=lambda path, **_kwargs: files[path]),
            mock.patch.object(builder, "guest_path_exists", side_effect=lambda path: path in required),
            mock.patch.object(builder, "guest_glob", return_value=[]),
            mock.patch.object(builder, "guest_file_size", return_value=0),
        ):
            builder.validate_built_image()

        files["/etc/shadow"] = "root:$6$usable:1::::::\nalarm:!:1::::::\n"
        with (
            mock.patch.object(builder, "read_guest_file", side_effect=lambda path, **_kwargs: files[path]),
            mock.patch.object(builder, "guest_path_exists", side_effect=lambda path: path in required),
            mock.patch.object(builder, "guest_glob", return_value=[]),
            mock.patch.object(builder, "guest_file_size", return_value=0),
            self.assertRaisesRegex(RuntimeError, "passwords must be locked"),
        ):
            builder.validate_built_image()

    def test_factory_offline_cleanup_removes_shutdown_generated_identity(self):
        builder = build.Builder(build.parse_args(["--factory-image"]))
        with mock.patch.object(builder, "guestfish") as guestfish:
            builder.sanitize_factory_image()
        commands = guestfish.call_args.args[0]
        self.assertIn("rm-f /var/lib/systemd/random-seed", commands)
        self.assertIn("glob rm-f /etc/ssh/ssh_host_*", commands)
        self.assertIn("truncate-size /etc/machine-id 0", commands)
        self.assertLess(commands.index("mount /dev/sda2 /"), commands.index("rm-f /var/lib/systemd/random-seed"))

    def test_guest_glob_filters_literal_no_match_result(self):
        builder = build.Builder(build.parse_args([]))
        no_matches = build.subprocess.CompletedProcess(
            ["guestfish"], 0, stdout="/etc/ssh/ssh_host_*\n", stderr=""
        )
        with mock.patch.object(builder, "guestfish", return_value=no_matches) as guestfish:
            self.assertEqual(builder.guest_glob("/etc/ssh/ssh_host_*"), [])
        self.assertIn("glob echo /etc/ssh/ssh_host_*", guestfish.call_args.args[0])

class RepositoryTests(unittest.TestCase):
    def test_release_workflow_is_image_focused_without_general_ci(self):
        workflow = (build.PROJECT / ".github/workflows/release.yml").read_text()
        self.assertFalse((build.PROJECT / ".github/workflows/ci.yml").exists())
        self.assertIn("ubuntu-24.04-arm", workflow)
        self.assertIn("--factory-image", workflow)
        self.assertIn("ArchLinuxARM-aarch64", build.DEFAULT_ROOTFS_URL)
        self.assertIn("build-info.json", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertEqual(workflow.count("FORCE_REBUILD: ${{ inputs.force_rebuild }}"), 2)
        self.assertIn("libguestfs-test-tool", workflow)

    def test_arm_ubuntu_installs_kernel_for_supermin(self):
        dependencies = (build.PROJECT / "install-deps.sh").read_text()
        self.assertIn('aarch64|arm64) packages+=(linux-image-arm64)', dependencies)

    def test_factory_smoke_waits_for_cloud_init_with_extended_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = build.Builder(build.parse_args(["--factory-image"]))
            builder.work = Path(directory)
            builder.admin_user = build.FACTORY_USER
            overlay = Path(directory) / "smoke-overlay.qcow2"
            with (
                mock.patch.object(builder, "write_root_owned_tar"),
                mock.patch.object(builder, "guestfish"),
            ):
                builder.install_smoke_payload(overlay, "root-uuid")
            service = (
                Path(directory)
                / "smoke-payload/etc/systemd/system/oci-image-smoke.service"
            ).read_text()
            self.assertIn(
                "After=local-fs.target dbus.service cloud-final.service cloud-init-main.service",
                service,
            )
            self.assertIn(
                "Requires=dbus.service cloud-init-main.service cloud-final.service", service
            )
            self.assertIn("TimeoutStartSec=600", service)
        smoke_script = (build.PROJECT / "guest/uefi-smoke-test.sh").read_text()
        self.assertIn("cloud-init status --long", smoke_script)
        self.assertNotIn("cloud-init status --wait", smoke_script)

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
        self.assertIn("for attempt in 1 2 3", configure)
        self.assertIn("package installation failed after", configure)

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
    def test_factory_console_requires_no_password_prompts_or_tty(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "serial.log"
            with mock.patch.object(sys.stdin, "isatty", return_value=False):
                build.ConsoleRunner.run(
                    [sys.executable, "-c", "print('OCI_IMAGE_BUILD_SUCCESS')"],
                    log=log,
                    success=build.BUILD_SUCCESS,
                    timeout=10,
                )
            self.assertIn(build.BUILD_SUCCESS, log.read_bytes())

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
