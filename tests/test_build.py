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


def factory_inspection(*, shadow: str = "root:!:1::::::\nalarm:$6$usable:1::::::\n"):
    required = {
        "/usr/bin/cloud-init",
        "/usr/lib/systemd/system-generators/cloud-init-generator",
        "/usr/lib/systemd/system/cloud-init.target",
        "/usr/lib/systemd/system/cloud-init-local.service",
        "/usr/lib/systemd/system/cloud-init-main.service",
        "/usr/lib/systemd/system/cloud-final.service",
    }
    all_paths = required | {
        "/root/.ssh/authorized_keys",
        "/root/.ssh/authorized_keys2",
        "/home/alarm/.ssh/authorized_keys",
        "/home/alarm/.ssh/authorized_keys2",
        "/var/lib/systemd/random-seed",
        "/var/lib/cloud/instance",
    }
    return build.ImageInspection(
        root_uuid="12345678-1234-1234-1234-123456789abc",
        files={
            "/etc/passwd": (
                "root:x:0:0::/root:/usr/bin/bash\n"
                "alarm:x:1000:1000::/home/alarm:/bin/bash\n"
            ),
            "/etc/shadow": shadow,
            "/etc/ssh/sshd_config.d/10-oci-security.conf": (
                build.PROJECT / "templates/sshd-security-factory.conf"
            ).read_text(),
            "/etc/cloud/cloud.cfg.d/90-oci-alarm.cfg": (
                build.PROJECT / "templates/cloud-init-alarm.cfg"
            ).read_text(),
            "/etc/sudoers.d/20-alarm-cloud": (
                build.PROJECT / "templates/sudoers-alarm"
            ).read_text(),
        },
        paths={path: path in required for path in all_paths},
        sizes={"/etc/machine-id": 0},
        globs={"/etc/ssh/ssh_host_*": [], "/var/lib/cloud/instances/*": []},
    )


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

    def test_streamed_stderr_preserves_stdout_capture_and_failure_checking(self):
        completed = build.subprocess.CompletedProcess(["tool"], 0, "machine-data", None)
        with mock.patch.object(build.subprocess, "run", return_value=completed) as invoked:
            result = build.run(["tool"], capture=True, stream_stderr=True)
        self.assertEqual(result.stdout, "machine-data")
        self.assertTrue(invoked.call_args.kwargs["check"])
        self.assertIs(invoked.call_args.kwargs["stdout"], build.subprocess.PIPE)
        self.assertIsNone(invoked.call_args.kwargs["stderr"])

    def test_guestfish_logs_description_before_running(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = build.Builder(build.parse_args(["--accel", "tcg"]))
            builder.work = Path(directory)
            builder.raw = builder.work / "test-image.raw"
            events = mock.Mock()
            events.run.return_value = build.subprocess.CompletedProcess([], 0, "", "")
            output = io.StringIO()
            with (
                mock.patch.object(build, "print_detail", events.detail),
                mock.patch.object(build, "run", events.run),
                mock.patch.object(sys, "stdout", output),
            ):
                builder.guestfish(
                    ["run", "mount-ro /dev/sda2 /", "cat /etc/passwd"],
                    description="inspecting the completed image",
                )

            self.assertEqual(
                events.mock_calls[0],
                mock.call.detail("guestfish: inspecting the completed image"),
            )
            self.assertEqual(events.mock_calls[1][0], "run")
            self.assertEqual(
                events.run.call_args.kwargs["input_text"],
                build.GUESTFISH_PROGRESS_EVENT
                + "\n! echo '     guestfish phase: launching the libguestfs appliance' >&2"
                + "\nrun\nmount-ro /dev/sda2 /\ncat /etc/passwd\n",
            )
            self.assertTrue(events.run.call_args.kwargs["stream_stderr"])
            guestfish_env = events.run.call_args.kwargs["env"]
            self.assertEqual(guestfish_env["SHELL"], "/bin/sh")
            expected_cache = f"{directory}/guestfs-cache/{build.platform.release()}"
            self.assertEqual(guestfish_env["LIBGUESTFS_CACHEDIR"], expected_cache)
            self.assertEqual(guestfish_env["LIBGUESTFS_TMPDIR"], f"{directory}/guestfs-tmp")
            self.assertEqual(guestfish_env["TMPDIR"], f"{directory}/guestfs-tmp")
            self.assertEqual(guestfish_env["XDG_RUNTIME_DIR"], f"{directory}/guestfs-runtime")
            self.assertEqual(guestfish_env["LIBGUESTFS_BACKEND_SETTINGS"], "force_tcg")
            self.assertEqual(
                output.getvalue().splitlines(),
                [
                    "     stdin script:",
                    f"       {build.GUESTFISH_PROGRESS_EVENT}",
                    "       ! echo '     guestfish phase: launching the libguestfs appliance' >&2",
                    "       run",
                    "       mount-ro /dev/sda2 /",
                    "       cat /etc/passwd",
                ],
            )

    def test_guestfish_instruments_long_operations_without_changing_their_order(self):
        builder = build.Builder(build.parse_args([]))
        builder.raw = Path("image.raw")
        completed = build.subprocess.CompletedProcess([], 0, "", None)
        with mock.patch.object(build, "run", return_value=completed) as invoked:
            builder.guestfish(
                ["run", "mkfs ext4 /dev/sda", "tar-in payload.tar /", "sync"],
                description="populating an image",
                capture=True,
            )
        script = invoked.call_args.kwargs["input_text"].splitlines()
        self.assertEqual(script[0], build.GUESTFISH_PROGRESS_EVENT)
        for operation in ("run", "mkfs ext4 /dev/sda", "tar-in payload.tar /", "sync"):
            operation_at = script.index(operation)
            self.assertIn("guestfish phase:", script[operation_at - 1])
        self.assertEqual(
            [line for line in script if not line.startswith("!")][1:],
            ["run", "mkfs ext4 /dev/sda", "tar-in payload.tar /", "sync"],
        )

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
        self.assertEqual(
            builder.qemu_machine_args(),
            ["-machine", "virt,accel=tcg", "-cpu", "neoverse-n1"],
        )
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
        events: list[str] = []
        state = {
            "version": 2, "build_mode": "factory", "image_user": "alarm",
            "root_uuid": "root-uuid",
        }
        with (
            mock.patch.object(builder, "check_environment"),
            mock.patch.object(builder, "confirm_output"),
            mock.patch.object(builder, "start_workspace"),
            mock.patch.object(builder, "run_build_stage", return_value=("root-uuid", "esp-uuid")),
            mock.patch.object(builder, "convert", side_effect=lambda: events.append("convert")),
            mock.patch.object(builder, "load_state", return_value=state),
            mock.patch.object(
                builder, "inspect_built_image",
                return_value=build.ImageInspection("root-uuid", {}, {}, {}, {}),
            ),
            mock.patch.object(builder, "validate_built_image"),
            mock.patch.object(
                builder, "run_uefi_smoke_test", side_effect=lambda *_args, **_kwargs: events.append("smoke")
            ),
            mock.patch.object(builder, "record_smoke_success"),
            mock.patch.object(builder, "collect_passwords") as collect,
        ):
            builder.build()
        self.assertEqual(builder.build_mode, "factory")
        self.assertEqual(builder.admin_user, "alarm")
        self.assertEqual(events, ["convert", "smoke"])
        collect.assert_not_called()

    def test_development_build_converts_before_smoke(self):
        builder = build.Builder(build.parse_args(["--username", "tester", "--password", "test-only"]))
        events: list[str] = []
        state = {
            "version": 2, "build_mode": "development", "image_user": "tester",
            "root_uuid": "root-uuid",
        }
        with (
            mock.patch.object(builder, "check_environment"),
            mock.patch.object(builder, "confirm_output"),
            mock.patch.object(builder, "collect_passwords"),
            mock.patch.object(builder, "start_workspace"),
            mock.patch.object(builder, "run_build_stage", return_value=("root-uuid", "esp-uuid")),
            mock.patch.object(builder, "convert", side_effect=lambda: events.append("convert")),
            mock.patch.object(builder, "load_state", return_value=state),
            mock.patch.object(
                builder, "inspect_built_image",
                return_value=build.ImageInspection("root-uuid", {}, {}, {}, {}),
            ),
            mock.patch.object(builder, "validate_built_image"),
            mock.patch.object(
                builder, "run_uefi_smoke_test", side_effect=lambda *_args, **_kwargs: events.append("smoke")
            ),
            mock.patch.object(builder, "record_smoke_success"),
        ):
            builder.build()
        self.assertEqual(events, ["convert", "smoke"])

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
            args = build.parse_args([
                "--work-dir", str(base), "--convert-only", "--output", str(base / "output.qcow2")
            ])
            builder = build.Builder(args)
            builder.start_workspace(resume=True)
            builder.admin_user = "tester"
            assert builder.raw is not None
            builder.raw.write_bytes(b"raw-image")
            builder.write_state(root_uuid="root-uuid", smoke_passed=False)
            completed = build.subprocess.CompletedProcess(["qemu-img"], 0, stdout="", stderr="")
            with mock.patch.object(build, "run", return_value=completed) as mocked_run:
                builder.convert()

            convert = next(
                call.args[0] for call in mocked_run.call_args_list
                if call.args[0][:2] == ["qemu-img", "convert"]
            )
            self.assertIn("compression_type=zstd", convert)
            state = builder.load_state()
            assert state is not None
            self.assertEqual(state["stage"], "converted")
            self.assertFalse(state["smoke_passed"])
            self.assertEqual(state["converted_image"]["format"], "qcow2")

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

    def test_workspace_state_tracks_raw_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            args = build.parse_args(["--work-dir", str(workspace), "--convert-only"])
            builder = build.Builder(args)
            builder.start_workspace(resume=True)
            builder.admin_user = "tester"
            assert builder.raw is not None
            builder.raw.write_bytes(b"raw-image")
            builder.write_state(root_uuid="root-uuid", smoke_passed=False)

            builder.require_matching_state()
            builder.raw.write_bytes(b"changed-image")
            with self.assertRaisesRegex(RuntimeError, "raw disk changed"):
                builder.require_matching_state()

    def test_converted_image_is_portable_with_workspace_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            image = workspace / "factory.qcow2"
            image.write_bytes(b"converted-image")
            builder = build.Builder(build.parse_args(["--work-dir", str(workspace), "--smoke-test-only"]))
            builder.start_workspace(resume=True)
            builder.admin_user = "alarm"
            assert builder.state_file is not None
            builder.update_state({
                "version": 2,
                "stage": "converted",
                "build_mode": "factory",
                "image_user": "alarm",
                "root_uuid": "root-uuid",
                "raw": {"size": 1, "mtime_ns": 1},
                "smoke_passed": False,
                "build_accel": "tcg",
                "smoke_accel": None,
                "converted_image": {
                    "filename": image.name,
                    "format": "qcow2",
                    "local_path": "/runner/path/that/no-longer-exists.qcow2",
                    "size": image.stat().st_size,
                    "sha256": builder.image_sha256(image),
                },
            })
            info = build.subprocess.CompletedProcess(
                ["qemu-img"], 0, stdout='{"format": "qcow2"}', stderr=""
            )
            with mock.patch.object(build, "run", return_value=info):
                self.assertEqual(builder.resolve_converted_image(builder.load_state()), image)
            image.write_bytes(b"altered")
            with self.assertRaisesRegex(RuntimeError, "size does not match|checksum does not match"):
                builder.resolve_converted_image(builder.load_state())

    def test_smoke_overlay_uses_converted_qcow2_as_backing_image(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            image = workspace / "factory.qcow2"
            image.write_bytes(b"qcow2")
            code = workspace / "code.fd"
            variables = workspace / "vars.fd"
            code.write_bytes(b"code")
            variables.write_bytes(b"vars")
            builder = build.Builder(build.parse_args(["--factory-image"]))
            builder.work = workspace
            builder.admin_user = "alarm"
            completed = build.subprocess.CompletedProcess(["qemu-img"], 0, stdout="", stderr="")
            with (
                mock.patch.object(build, "run", return_value=completed) as invoked,
                mock.patch.object(builder, "install_smoke_payload"),
                mock.patch.object(builder, "create_nocloud_seed", return_value=None),
                mock.patch.object(builder, "find_firmware", return_value=(code, variables)),
                mock.patch.object(builder, "select_acceleration", return_value="tcg"),
                mock.patch.object(build.ConsoleRunner, "run"),
            ):
                builder.run_uefi_smoke_test(
                    "root-uuid", image=image, image_format="qcow2"
                )
            create = invoked.call_args_list[0].args[0]
            self.assertEqual(create[create.index("-F") + 1], "qcow2")
            self.assertEqual(create[create.index("-b") + 1], image)

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
            resumed.require_matching_state()
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

    def test_factory_validation_enforces_console_recovery_account(self):
        builder = build.Builder(build.parse_args(["--factory-image"]))
        builder.admin_user = "alarm"
        builder.validate_built_image(factory_inspection())

        unlocked_root = factory_inspection(
            shadow="root:$6$usable:1::::::\nalarm:!:1::::::\n"
        )
        with self.assertRaisesRegex(RuntimeError, "root password must be locked"):
            builder.validate_built_image(unlocked_root)

        locked_alarm = factory_inspection(
            shadow="root:!:1::::::\nalarm:!:1::::::\n"
        )
        with self.assertRaisesRegex(RuntimeError, "alarm password must remain usable"):
            builder.validate_built_image(locked_alarm)

    def test_completed_image_inspection_uses_one_guestfish_session(self):
        builder = build.Builder(build.parse_args(["--factory-image"]))
        builder.admin_user = "alarm"
        expected = factory_inspection()
        values = [
            expected.root_uuid,
            expected.files["/etc/passwd"],
            expected.files["/etc/ssh/sshd_config.d/10-oci-security.conf"],
            expected.files["/etc/shadow"],
            expected.files["/etc/cloud/cloud.cfg.d/90-oci-alarm.cfg"],
            expected.files["/etc/sudoers.d/20-alarm-cloud"],
            *(["true"] * 6),
            *(["false"] * 6),
            "0",
            "/etc/ssh/ssh_host_*",
            "/var/lib/cloud/instances/*",
        ]
        keys = [
            "root_uuid", *[f"file.{index}" for index in range(5)],
            *[f"path.{index}" for index in range(12)], "size.0", "glob.0", "glob.1",
        ]
        token = "OCIINSPECTdeadbeef"
        stdout = "\n".join(
            line
            for key, value in zip(keys, values, strict=True)
            for line in (f"{token}:BEGIN:{key}", value, f"{token}:END:{key}")
        ) + "\n"
        completed = build.subprocess.CompletedProcess(
            ["guestfish"], 0, stdout=stdout, stderr=""
        )
        with (
            mock.patch.object(build.secrets, "token_hex", return_value="deadbeef"),
            mock.patch.object(builder, "guestfish", return_value=completed) as guestfish,
        ):
            inspection = builder.inspect_built_image(
                image=Path("factory.qcow2"), image_format="qcow2"
            )
        self.assertEqual(guestfish.call_count, 1)
        self.assertTrue(guestfish.call_args.kwargs["read_only"])
        self.assertEqual(inspection.root_uuid, expected.root_uuid)
        self.assertEqual(inspection.sizes["/etc/machine-id"], 0)
        self.assertEqual(inspection.globs["/etc/ssh/ssh_host_*"], [])

    def test_development_inspection_omits_factory_only_queries(self):
        builder = build.Builder(build.parse_args(["--username", "tester"]))
        builder.admin_user = "tester"
        token = "OCIINSPECTdeadbeef"
        values = {
            "root_uuid": "12345678-1234-1234-1234-123456789abc",
            "file.0": "root:x:0:0::/root:/bin/bash\ntester:x:1000:1000::/home/tester:/bin/bash",
            "file.1": (build.PROJECT / "templates/sshd-security-development.conf").read_text().replace(
                "{{IMAGE_USER}}", "tester"
            ),
        }
        stdout = "\n".join(
            line
            for key, value in values.items()
            for line in (f"{token}:BEGIN:{key}", value, f"{token}:END:{key}")
        ) + "\n"
        completed = build.subprocess.CompletedProcess(["guestfish"], 0, stdout, "")
        with (
            mock.patch.object(build.secrets, "token_hex", return_value="deadbeef"),
            mock.patch.object(builder, "guestfish", return_value=completed) as guestfish,
        ):
            inspection = builder.inspect_built_image()
        commands = guestfish.call_args.args[0]
        self.assertNotIn("cat /etc/shadow", commands)
        self.assertFalse(inspection.paths)
        builder.validate_built_image(inspection)

    def test_disk_creation_collects_both_uuids_in_the_import_session(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = build.Builder(build.parse_args([]))
            builder.work = Path(directory)
            builder.raw = builder.work / "disk.raw"
            builder.rootfs = builder.work / "rootfs.tar.gz"
            token = "OCIUUIDdeadbeef"
            stdout = (
                f"{token}:BEGIN:root\n12345678-1234-1234-1234-123456789abc\n"
                f"{token}:END:root\n{token}:BEGIN:esp\nABCD-1234\n{token}:END:esp\n"
            )
            completed = build.subprocess.CompletedProcess(["guestfish"], 0, stdout, "")
            with (
                mock.patch.object(build, "run"),
                mock.patch.object(build.secrets, "token_hex", return_value="deadbeef"),
                mock.patch.object(builder, "guestfish", return_value=completed) as guestfish,
            ):
                uuids = builder.create_and_populate_disk()
        self.assertEqual(
            uuids,
            ("12345678-1234-1234-1234-123456789abc", "ABCD-1234"),
        )
        self.assertEqual(guestfish.call_count, 1)
        commands = guestfish.call_args.args[0]
        self.assertIn("vfs-uuid /dev/sda2", commands)
        self.assertIn("vfs-uuid /dev/sda1", commands)

    def test_guestfish_inspection_rejects_malformed_framing(self):
        with self.assertRaisesRegex(RuntimeError, "invalid framing"):
            build.Builder.parse_guestfish_frames(
                "TOKEN:BEGIN:key\nvalue\n", "TOKEN", ["key"]
            )

    def test_factory_offline_cleanup_removes_shutdown_generated_identity(self):
        builder = build.Builder(build.parse_args(["--factory-image"]))
        with mock.patch.object(builder, "guestfish") as guestfish:
            builder.finalize_built_image()
        commands = guestfish.call_args.args[0]
        self.assertIn("rm-f /var/lib/systemd/random-seed", commands)
        self.assertIn("glob rm-f /etc/ssh/ssh_host_*", commands)
        self.assertIn("truncate-size /etc/machine-id 0", commands)
        self.assertIn("rm-f /var/lib/archlinuxarm-oci/build-success", commands)
        self.assertLess(
            commands.index("mount /dev/sda2 /"),
            commands.index("rm-f /var/lib/systemd/random-seed"),
        )


class RepositoryTests(unittest.TestCase):
    @staticmethod
    def release_workflow() -> str:
        return (build.PROJECT / ".github/workflows/release.yml").read_text()

    @staticmethod
    def upstream_workflow() -> str:
        return (build.PROJECT / ".github/workflows/check-upstream.yml").read_text()

    def test_release_workflow_is_image_focused_without_general_ci(self):
        workflow = self.release_workflow()
        self.assertFalse((build.PROJECT / ".github/workflows/ci.yml").exists())
        self.assertIn("ubuntu-24.04-arm", workflow)
        self.assertIn("--factory-image", workflow)
        self.assertIn("ArchLinuxARM-aarch64", build.DEFAULT_ROOTFS_URL)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("push:", workflow)
        self.assertEqual(workflow.count("FORCE_REBUILD: ${{ inputs.force_rebuild }}"), 2)
        self.assertNotIn("latest_sha", workflow)
        self.assertNotIn('"$GITHUB_SHA" !=', workflow)
        self.assertNotIn("project-or-upstream-changed", workflow)
        self.assertIn('elif [[ "$checksum" != "$latest_md5" ]]; then', workflow)

    def test_scheduled_checker_dispatches_only_for_an_upstream_change(self):
        workflow = self.upstream_workflow()
        self.assertIn("name: Check for Arch Linux ARM rootfs update", workflow)
        self.assertIn("schedule:", workflow)
        self.assertIn("push:", workflow)
        self.assertIn("branches: [main]", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("actions: write", workflow)
        self.assertIn("reason=already-released", workflow)
        self.assertIn("reason=upstream-rootfs-changed", workflow)
        self.assertIn("if: steps.decision.outputs.dispatch == 'true'", workflow)
        self.assertIn("gh workflow run release.yml --ref main", workflow)

    def test_release_workflow_uses_minimal_libguestfs_setup_and_permissions(self):
        workflow = self.release_workflow()
        self.assertNotIn("libguestfs-test-tool", workflow)
        self.assertEqual(workflow.count("run: ./ci/prepare-libguestfs.sh"), 3)
        self.assertEqual(workflow.count("contents: read"), 1)
        self.assertEqual(workflow.count("contents: write"), 1)

    def test_oci_guides_form_an_end_to_end_path(self):
        preparation = (build.PROJECT / "docs/OCI-PREPARATION.md").read_text()
        deployment = (build.PROJECT / "docs/OCI-DEPLOYMENT.md").read_text()
        readme = (build.PROJECT / "README.md").read_text()
        self.assertIn("OCI-DEPLOYMENT.md", preparation)
        self.assertIn("OCI-PREPARATION.md", deployment)
        self.assertIn("--dry-run", deployment)
        self.assertIn("--reuse-download", deployment)
        self.assertIn("--verify-ssh", deployment)
        self.assertIn("--ssh-key PATH", deployment)
        self.assertNotIn("--ssh-public-key", preparation + deployment)
        self.assertNotIn("--ssh-private-key", preparation + deployment)
        self.assertNotIn("BUCKET_NAME", preparation)
        self.assertNotIn("export ", preparation + deployment)
        self.assertIn("searches for accessible subnets", deployment)
        self.assertIn("availability domains", deployment)
        self.assertIn("OCI-PREPARATION.md", readme)
        self.assertIn("OCI-DEPLOYMENT.md", readme)

    def test_release_workflow_smokes_the_uploaded_artifact_before_publish(self):
        workflow = self.release_workflow()
        self.assertIn("build-info.json", workflow)
        self.assertIn("--work-dir /tmp/archlinuxarm-oci-work", workflow)
        self.assertNotIn('$RUNNER_TEMP/archlinuxarm-oci-work', workflow)
        self.assertIn("findmnt -T /tmp", workflow)
        self.assertIn("name: factory-image-${{ github.run_id }}", workflow)
        self.assertIn("name: factory-smoke-results-${{ github.run_id }}", workflow)
        self.assertIn("needs: [decide, build]", workflow)
        self.assertIn("needs: [decide, build, smoke]", workflow)
        self.assertIn("Smoke-test downloaded QCOW2 artifact", workflow)
        self.assertIn("smoke_source_run_id:", workflow)
        self.assertIn("run-id: ${{ inputs.smoke_source_run_id }}", workflow)
        self.assertIn("name: factory-image-${{ inputs.smoke_source_run_id }}", workflow)
        self.assertIn("Smoke-test existing QCOW2 artifact", workflow)
        self.assertLess(
            workflow.index("Upload converted image for smoke testing"),
            workflow.index("Smoke-test downloaded QCOW2 artifact"),
        )
        self.assertLess(
            workflow.index("Smoke-test downloaded QCOW2 artifact"),
            workflow.index("Publish GitHub Release"),
        )

    def test_release_workflow_supports_non_publishing_branch_validation(self):
        workflow = self.release_workflow()
        self.assertIn("publish_release:", workflow)
        self.assertIn("PUBLISH_RELEASE: ${{ inputs.publish_release }}", workflow)
        self.assertIn('"$PUBLISH_RELEASE" == true', workflow)
        self.assertIn("if: inputs.publish_release", workflow)

    def test_ci_libguestfs_setup_only_copies_the_hosted_runner_kernel(self):
        helper = (build.PROJECT / "ci/prepare-libguestfs.sh").read_text()
        self.assertIn("SUPERMIN_KERNEL_VERSION", helper)
        self.assertIn('sudo install -m 0644 "$runner_kernel" "$kernel"', helper)
        self.assertIn('kernel_version="$(uname -r)"', helper)
        self.assertNotIn("libguestfs-test-tool", helper)

    def test_host_dependency_installer_does_not_add_a_second_kernel(self):
        dependencies = (build.PROJECT / "install-deps.sh").read_text()
        self.assertNotIn("linux-image-generic", dependencies)

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
