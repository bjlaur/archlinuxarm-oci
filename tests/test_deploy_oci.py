import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "deploy_oci", Path(__file__).resolve().parents[1] / "deploy-oci.py"
)
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


COMPARTMENT = "ocid1.compartment.oc1..example"
SUBNET = "ocid1.subnet.oc1.us-test-1.example"


def required_cli_args():
    return [
        "--compartment-id",
        COMPARTMENT,
        "--subnet-id",
        SUBNET,
        "--availability-domain",
        "test:US-TEST-AD-1",
    ]


def valid_args(*extra):
    return deploy.parse_args([*required_cli_args(), "--bucket", "test-bucket", *extra])


def no_bucket_args(*extra):
    return deploy.parse_args([*required_cli_args(), *extra])


class FakeState:
    def __init__(self, data=None):
        self.data = data or {"resources": {}}
        self.resources = []

    def resource(self, resource_name, **values):
        current = self.data.setdefault("resources", {}).setdefault(resource_name, {})
        current.update(values)
        self.resources.append((resource_name, values))


class SequenceOCI:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def run(self, arguments, **kwargs):
        self.calls.append((arguments, kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response


class ArgumentTests(unittest.TestCase):
    def test_defaults_are_conservative(self):
        args = valid_args()
        self.assertEqual(args.shape, "VM.Standard.A1.Flex")
        self.assertEqual(args.boot_volume_gbs, 50)
        self.assertFalse(args.assign_public_ip)
        self.assertFalse(args.cleanup_object)
        self.assertEqual(args.ssh_key, deploy.DEFAULT_SSH_KEY)

    def test_rejects_small_boot_volume(self):
        with self.assertRaises(SystemExit):
            valid_args("--boot-volume-gbs", "49")

    def test_rejects_nonfinite_shape_values(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            deploy.positive_float("nan")
        with self.assertRaises(argparse.ArgumentTypeError):
            deploy.positive_float("inf")

    def test_custom_ssh_key_path_is_resolved(self):
        args = valid_args("--ssh-key", "~/keys/oci")
        self.assertEqual(args.ssh_key, Path("~/keys/oci").expanduser().resolve())

    def test_bucket_selection_is_optional_at_parse_time(self):
        args = no_bucket_args()
        self.assertIsNone(args.bucket)
        self.assertIsNone(args.create_bucket)

    def test_deployment_location_is_optional_at_parse_time(self):
        args = deploy.parse_args([])
        self.assertIsNone(args.compartment_id)
        self.assertIsNone(args.subnet_id)
        self.assertIsNone(args.availability_domain)

    def test_resume_and_dry_run_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            valid_args("--resume", "--dry-run")

    def test_rejects_invalid_ocid(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            deploy.validate_ocid("not-an-ocid", "subnet")


class SSHKeyTests(unittest.TestCase):
    def test_missing_pair_is_generated_without_a_passphrase(self):
        with tempfile.TemporaryDirectory() as temporary:
            private_key = Path(temporary) / "keys" / "oci"
            public_key = Path(f"{private_key}.pub")
            args = mock.Mock(ssh_key=private_key)

            def run(command, **_kwargs):
                private_key.write_text("private")
                public_key.write_text("public")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch.object(deploy.subprocess, "run", side_effect=run) as invoked,
                mock.patch.object(
                    deploy, "key_fingerprint", side_effect=["SHA256:key", "SHA256:key"]
                ),
            ):
                fingerprint = deploy.prepare_ssh_key(args)
            parent_mode = private_key.parent.stat().st_mode & 0o777

        command = invoked.call_args.args[0]
        self.assertEqual(fingerprint, "SHA256:key")
        self.assertEqual(args.ssh_private_key, private_key)
        self.assertEqual(args.ssh_public_key, public_key)
        self.assertEqual(command[command.index("-t") + 1], "ed25519")
        self.assertEqual(command[command.index("-N") + 1], "")
        self.assertEqual(parent_mode, 0o700)

    def test_complete_existing_pair_is_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            private_key = Path(temporary) / "oci"
            public_key = Path(f"{private_key}.pub")
            private_key.write_text("private")
            public_key.write_text("public")
            args = mock.Mock(ssh_key=private_key)
            with (
                mock.patch.object(
                    deploy, "key_fingerprint", side_effect=["SHA256:key", "SHA256:key"]
                ),
                mock.patch.object(deploy.subprocess, "run") as invoked,
            ):
                self.assertEqual(deploy.prepare_ssh_key(args), "SHA256:key")
        invoked.assert_not_called()

    def test_partial_pair_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            private_key = Path(temporary) / "oci"
            private_key.write_text("private")
            with self.assertRaisesRegex(deploy.DeploymentError, "incomplete SSH key pair"):
                deploy.prepare_ssh_key(mock.Mock(ssh_key=private_key))

    def test_mismatched_pair_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            private_key = Path(temporary) / "oci"
            Path(private_key).write_text("private")
            Path(f"{private_key}.pub").write_text("public")
            with (
                mock.patch.object(
                    deploy,
                    "key_fingerprint",
                    side_effect=["SHA256:private", "SHA256:public"],
                ),
                self.assertRaisesRegex(deploy.DeploymentError, "do not match"),
            ):
                deploy.prepare_ssh_key(mock.Mock(ssh_key=private_key))


class BucketSelectionTests(unittest.TestCase):
    def test_explicit_bucket_is_unchanged_without_prompting(self):
        args = valid_args()
        deploy.resolve_bucket_selection(
            args,
            environ={},
            input_fn=mock.Mock(side_effect=AssertionError("unexpected prompt")),
        )
        self.assertEqual(args.bucket, "test-bucket")
        self.assertIsNone(args.create_bucket)

    def test_environment_name_selects_bucket_creation_without_prompting(self):
        args = no_bucket_args()
        deploy.resolve_bucket_selection(
            args,
            environ={"BUCKET_NAME": "my-import-bucket"},
            input_fn=mock.Mock(side_effect=AssertionError("unexpected prompt")),
        )
        self.assertEqual(args.create_bucket, "my-import-bucket")
        self.assertTrue(args.allow_existing_create_bucket)

    def test_default_name_requires_and_accepts_confirmation(self):
        args = no_bucket_args()
        prompt = mock.Mock(return_value="")
        deploy.resolve_bucket_selection(
            args, environ={"USER": "test-user"}, input_fn=prompt
        )
        self.assertEqual(args.create_bucket, "archlinuxarm-oci-import-test-user")
        self.assertTrue(args.allow_existing_create_bucket)
        self.assertIn("[Y/n]", prompt.call_args.args[0])

    def test_default_name_rejection_cancels(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "cancelled"):
            deploy.resolve_bucket_selection(
                no_bucket_args(), environ={"USER": "test-user"}, input_fn=lambda _: "n"
            )

    def test_noninteractive_selection_explains_explicit_options(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "BUCKET_NAME"):
            deploy.resolve_bucket_selection(
                no_bucket_args(),
                environ={"USER": "test-user"},
                input_fn=mock.Mock(side_effect=EOFError),
            )

    def test_default_bucket_cleanup_prompt_defaults_to_keep(self):
        args = no_bucket_args()
        deploy.resolve_bucket_selection(
            args, environ={"USER": "test-user"}, input_fn=lambda _: ""
        )
        prompt = mock.Mock(return_value="")
        deploy.resolve_bucket_cleanup(args, input_fn=prompt)
        self.assertFalse(args.cleanup_bucket)
        self.assertIn("[N/y]", prompt.call_args.args[0])

    def test_default_bucket_cleanup_prompt_accepts_yes(self):
        args = no_bucket_args()
        deploy.resolve_bucket_selection(
            args, environ={"USER": "test-user"}, input_fn=lambda _: ""
        )
        deploy.resolve_bucket_cleanup(args, input_fn=mock.Mock(return_value="y"))
        self.assertTrue(args.cleanup_bucket)

    def test_explicit_bucket_cleanup_is_not_prompted(self):
        args = valid_args()
        deploy.resolve_bucket_cleanup(
            args, input_fn=mock.Mock(side_effect=AssertionError("unexpected prompt"))
        )
        self.assertFalse(args.cleanup_bucket)


class DeploymentInputTests(unittest.TestCase):
    def shape(self):
        return {
            "shape": deploy.DEFAULT_SHAPE,
            "is-flexible": True,
            "ocpu-options": {"min": 1, "max": 80},
            "memory-options": {"min-in-g-bs": 1, "max-in-g-bs": 512},
        }

    def subnet(self, name, subnet_id, compartment=COMPARTMENT, public=True):
        return {
            "id": subnet_id,
            "display-name": name,
            "compartment-id": compartment,
            "cidr-block": "10.0.0.0/24",
            "availability-domain": None,
            "lifecycle-state": "AVAILABLE",
            "prohibit-public-ip-on-vnic": not public,
        }

    def test_explicit_values_do_not_call_discovery(self):
        args = valid_args()
        oci = mock.Mock()
        self.assertEqual(
            deploy.resolve_deployment_inputs(
                args,
                oci,
                environ={},
                input_fn=mock.Mock(side_effect=AssertionError("unexpected prompt")),
            ),
            {},
        )
        oci.run.assert_not_called()

    def test_environment_supplies_unattended_values(self):
        args = deploy.parse_args([])
        oci = mock.Mock()
        deploy.resolve_deployment_inputs(
            args,
            oci,
            environ={
                "COMPARTMENT_ID": COMPARTMENT,
                "SUBNET_ID": SUBNET,
                "AVAILABILITY_DOMAIN": "test:US-TEST-AD-1",
            },
            input_fn=mock.Mock(side_effect=AssertionError("unexpected prompt")),
        )
        self.assertEqual(args.compartment_id, COMPARTMENT)
        self.assertEqual(args.subnet_id, SUBNET)
        self.assertEqual(args.availability_domain, "test:US-TEST-AD-1")
        oci.run.assert_not_called()

    def test_discovers_subnet_compartment_and_a1_domain(self):
        args = deploy.parse_args([])
        first = "ocid1.subnet.oc1.us-test-1.first"
        second = "ocid1.subnet.oc1.us-test-1.second"
        oci = SequenceOCI(
            [
                {"data": {"items": [{"identifier": first}, {"identifier": second}]}},
                {"data": self.subnet("alpha", first)},
                {"data": self.subnet("beta", second)},
                {"data": [{"name": "test:AD-1"}, {"name": "test:AD-2"}]},
                {"data": []},
                {"data": [self.shape()]},
            ]
        )
        selected = deploy.resolve_deployment_inputs(
            args,
            oci,
            environ={},
            input_fn=mock.Mock(return_value="2"),
        )
        self.assertEqual(args.compartment_id, COMPARTMENT)
        self.assertEqual(args.subnet_id, second)
        self.assertEqual(args.availability_domain, "test:AD-2")
        self.assertEqual(selected["subnet"]["display-name"], "beta")
        self.assertEqual(selected["shape"]["shape"], deploy.DEFAULT_SHAPE)
        self.assertEqual(oci.calls[0][0][:3], ["search", "resource", "structured-search"])

    def test_single_public_subnet_is_selected_automatically(self):
        args = deploy.parse_args([])
        private = "ocid1.subnet.oc1.us-test-1.private"
        public = "ocid1.subnet.oc1.us-test-1.public"
        args.assign_public_ip = True
        oci = SequenceOCI(
            [
                {"data": {"items": [{"identifier": private}, {"identifier": public}]}},
                {"data": self.subnet("private", private, public=False)},
                {"data": self.subnet("public", public)},
                {"data": [{"name": "test:AD-1"}]},
                {"data": [self.shape()]},
            ]
        )
        deploy.resolve_deployment_inputs(
            args,
            oci,
            environ={},
            input_fn=mock.Mock(side_effect=AssertionError("unexpected prompt")),
        )
        self.assertEqual(args.subnet_id, public)

    def test_no_discoverable_subnet_explains_override(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "no suitable subnets"):
            deploy.resolve_deployment_inputs(
                deploy.parse_args([]),
                SequenceOCI([{"data": {"items": []}}]),
                environ={},
                input_fn=mock.Mock(side_effect=EOFError),
            )

    def test_environment_ocid_is_validated(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "invalid compartment"):
            deploy.resolve_deployment_inputs(
                deploy.parse_args([]), mock.Mock(), environ={"COMPARTMENT_ID": "invalid"}
            )


class ResumeDefaultTests(unittest.TestCase):
    def test_resume_recovers_interactive_values_from_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "inputs": {
                            "compartment_id": COMPARTMENT,
                            "subnet_id": SUBNET,
                            "availability_domain": "test:US-TEST-AD-1",
                            "bucket": None,
                            "create_bucket": "saved-bucket",
                            "cleanup_bucket": True,
                        }
                    }
                )
            )
            args = deploy.parse_args(
                ["--resume", "--state-file", str(state_path)]
            )
            deploy.apply_resume_defaults(args)
        self.assertEqual(args.compartment_id, COMPARTMENT)
        self.assertEqual(args.subnet_id, SUBNET)
        self.assertEqual(args.availability_domain, "test:US-TEST-AD-1")
        self.assertEqual(args.create_bucket, "saved-bucket")
        self.assertTrue(args.cleanup_bucket)

    def test_explicit_resume_value_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "inputs": {
                            "compartment_id": "ocid1.compartment.oc1..saved",
                            "subnet_id": SUBNET,
                            "availability_domain": "test:US-TEST-AD-1",
                            "bucket": "saved-bucket",
                            "create_bucket": None,
                        }
                    }
                )
            )
            args = deploy.parse_args(
                [
                    "--resume",
                    "--state-file",
                    str(state_path),
                    "--compartment-id",
                    COMPARTMENT,
                ]
            )
            deploy.apply_resume_defaults(args)
        self.assertEqual(args.compartment_id, COMPARTMENT)


class StateTests(unittest.TestCase):
    def test_state_is_atomic_private_and_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = deploy.StateFile(path)
            state.update(phase="tested", value=7)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["value"], 7)
            resumed = deploy.StateFile(path, resume=True)
            self.assertTrue(resumed.loaded)
            self.assertEqual(resumed.data["phase"], "tested")

    def test_state_refuses_implicit_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{}")
            with self.assertRaises(deploy.DeploymentError):
                deploy.StateFile(path)

    def test_resume_requires_identical_inputs_and_release(self):
        state = mock.Mock(loaded=True)
        state.data = {
            "inputs": {"shape": "A1"},
            "release": {"sha256": "a" * 64},
        }
        deploy.validate_resume(
            state, {"shape": "A1", "cleanup_bucket": False}, "a" * 64
        )
        deploy.validate_resume(
            state, {"shape": "A1", "cleanup_bucket": True}, "a" * 64
        )
        with self.assertRaisesRegex(deploy.DeploymentError, "arguments"):
            deploy.validate_resume(
                state, {"shape": "different", "cleanup_bucket": False}, "a" * 64
            )
        with self.assertRaisesRegex(deploy.DeploymentError, "release"):
            deploy.validate_resume(
                state, {"shape": "A1", "cleanup_bucket": False}, "b" * 64
            )


class ReleaseTests(unittest.TestCase):
    def write_release(self, directory, contents=b"image"):
        digest = hashlib.sha256(contents).hexdigest()
        name = "image.qcow2"
        (directory / name).write_bytes(contents)
        (directory / "build-info.json").write_text(
            json.dumps({"image_filename": name, "image_sha256": digest})
        )
        (directory / f"{name}.sha256").write_text(f"{digest}  {name}\n")
        return digest

    def test_parse_release_verifies_both_metadata_sources_and_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            digest = self.write_release(directory)
            metadata, image, actual = deploy.parse_release(directory)
            self.assertEqual(metadata["image_sha256"], digest)
            self.assertEqual(image.name, "image.qcow2")
            self.assertEqual(actual, digest)

    def test_parse_release_rejects_tampered_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_release(directory)
            (directory / "image.qcow2").write_bytes(b"tampered")
            with self.assertRaisesRegex(deploy.DeploymentError, "does not match"):
                deploy.parse_release(directory)

    def test_parse_release_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "build-info.json").write_text(
                json.dumps({"image_filename": "../image.qcow2", "image_sha256": "a" * 64})
            )
            with self.assertRaisesRegex(deploy.DeploymentError, "unsafe"):
                deploy.parse_release(directory)


class BucketMutationTests(unittest.TestCase):
    def test_auto_selected_existing_bucket_is_reused(self):
        args = no_bucket_args()
        args.create_bucket = "test-bucket"
        args.allow_existing_create_bucket = True
        args.resume = False
        state = FakeState(
            {"deployment_id": str(deploy.uuid.uuid4()), "resources": {}}
        )
        oci = SequenceOCI(
            [
                {
                    "data": {
                        "name": "test-bucket",
                        "public-access-type": "NoPublicAccess",
                        "storage-tier": "Standard",
                        "compartment-id": COMPARTMENT,
                    }
                }
            ]
        )
        self.assertEqual(
            deploy.ensure_bucket(args, oci, state, "namespace", {"tag": "value"}),
            "test-bucket",
        )
        self.assertEqual(state.data["resources"]["bucket"]["created"], False)

    def test_resume_reuses_existing_bucket_without_record(self):
        args = no_bucket_args()
        args.create_bucket = "test-bucket"
        args.resume = True
        state = FakeState(
            {"deployment_id": str(deploy.uuid.uuid4()), "resources": {}}
        )
        oci = SequenceOCI(
            [
                {
                    "data": {
                        "name": "test-bucket",
                        "public-access-type": "NoPublicAccess",
                        "storage-tier": "Standard",
                        "compartment-id": COMPARTMENT,
                    }
                }
            ]
        )
        self.assertEqual(
            deploy.ensure_bucket(args, oci, state, "namespace", {"tag": "value"}),
            "test-bucket",
        )
        self.assertEqual(state.data["resources"]["bucket"]["created"], False)


class RunnerTests(unittest.TestCase):
    def test_runner_uses_argument_array_and_parses_json(self):
        completed = subprocess.CompletedProcess([], 0, '{"data":"namespace"}', "")
        with mock.patch.object(deploy.subprocess, "run", return_value=completed) as invoked:
            runner = deploy.OCIRunner(
                profile="P", config_file=Path("/config"), region="r", verbose=False
            )
            self.assertEqual(runner.run(["os", "ns", "get"]), {"data": "namespace"})
        command = invoked.call_args.args[0]
        self.assertEqual(command[:4], ["oci", "os", "ns", "get"])
        self.assertIn("--profile", command)
        self.assertNotIn("shell", invoked.call_args.kwargs)

    def test_runner_rejects_invalid_json(self):
        completed = subprocess.CompletedProcess([], 0, "not-json", "")
        with mock.patch.object(deploy.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(deploy.DeploymentError, "invalid JSON"):
                deploy.OCIRunner().run(["os", "ns", "get"])

    def test_runner_can_treat_empty_stdout_as_empty_data(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(deploy.subprocess, "run", return_value=completed):
            self.assertEqual(
                deploy.OCIRunner().run(
                    ["compute", "image-capability-schema", "list"],
                    empty_data=[],
                ),
                {"data": []},
            )

    def test_runner_can_treat_empty_stdout_as_empty_object_list(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(deploy.subprocess, "run", return_value=completed):
            self.assertEqual(
                deploy.OCIRunner().run(
                    ["os", "object", "list"],
                    empty_data={"objects": []},
                ),
                {"data": {"objects": []}},
            )

    def test_oci_error_recognizes_only_not_found_responses(self):
        missing = deploy.OCIError([], 1, "ServiceError: 404 NotAuthorizedOrNotFound")
        forbidden = deploy.OCIError([], 1, "ServiceError: 403 NotAllowed")
        self.assertTrue(missing.not_found)
        self.assertFalse(forbidden.not_found)


class ShapeAndLifecycleTests(unittest.TestCase):
    def test_shape_config_checks_memory_per_ocpu(self):
        shape = {
            "shape": deploy.DEFAULT_SHAPE,
            "is-flexible": True,
            "ocpu-options": {"min": 1, "max": 80},
            "memory-options": {
                "min-in-g-bs": 1,
                "max-in-g-bs": 512,
                "min-per-ocpu-in-gbs": 1,
                "max-per-ocpu-in-gbs": 64,
            },
        }
        deploy.validate_shape_config(shape, 1, 6)
        with self.assertRaisesRegex(deploy.DeploymentError, "per OCPU"):
            deploy.validate_shape_config(shape, 1, 100)

    def test_waiter_reports_transitions_and_returns_success(self):
        resources = iter(
            [
                {"lifecycle-state": "IMPORTING"},
                {"lifecycle-state": "AVAILABLE", "id": "image"},
            ]
        )
        clock = iter([0, 0, 1, 1])
        with (
            mock.patch.object(deploy.time, "monotonic", side_effect=lambda: next(clock)),
            mock.patch.object(deploy.time, "sleep"),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = deploy.wait_for_resource(
                lambda: next(resources), {"IMPORTING"}, "AVAILABLE", 30, "image"
        )
        self.assertEqual(result["id"], "image")

    def test_waiter_reports_unchanged_state_periodically(self):
        resources = iter(
            [
                {"lifecycle-state": "IMPORTING"},
                {"lifecycle-state": "IMPORTING"},
                {"lifecycle-state": "IMPORTING"},
                {"lifecycle-state": "AVAILABLE", "id": "image"},
            ]
        )
        times = iter([0, 0, 30, 65, 65, 90])
        with (
            mock.patch.object(deploy.time, "monotonic", side_effect=lambda: next(times)),
            mock.patch.object(deploy.time, "sleep"),
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            result = deploy.wait_for_resource(
                lambda: next(resources), {"IMPORTING"}, "AVAILABLE", 120, "image"
            )
        self.assertEqual(result["id"], "image")
        self.assertIn("IMAGE  IMPORTING (0s)", output.getvalue())
        self.assertIn("IMAGE  IMPORTING (65s)", output.getvalue())
        self.assertIn("IMAGE  AVAILABLE (65s)", output.getvalue())

    def test_waiter_fails_closed_on_unknown_state(self):
        with self.assertRaisesRegex(deploy.DeploymentError, "FAILED"):
            deploy.wait_for_resource(
                lambda: {"lifecycle-state": "FAILED"},
                {"IMPORTING"},
                "AVAILABLE",
                30,
                "image",
            )


class CapabilityTests(unittest.TestCase):
    def global_responses(self, image_schema=None):
        global_data = {
            key: {
                "default-value": value,
                "descriptor-type": "enumstring",
                "source": "GLOBAL",
                "values": [value],
            }
            for key, value in deploy.REQUIRED_CAPABILITIES.items()
        }
        responses = [
            {"data": [{"id": "global", "current-version-name": "v1"}]},
            {"data": {"schema-data": global_data}},
            {"data": [] if image_schema is None else [{"id": "custom"}]},
        ]
        if image_schema is not None:
            responses.append({"data": {"schema-data": image_schema}})
        return responses

    def test_missing_image_capability_schema_is_created(self):
        oci = SequenceOCI(
            self.global_responses()
            + [{"data": {"schema-data": self.global_responses()[1]["data"]["schema-data"]}}]
        )
        result = deploy.validate_capabilities(
            valid_args(), oci, "image", {"tag": "value"}
        )
        self.assertEqual(result, deploy.REQUIRED_CAPABILITIES)
        create = oci.calls[3][0]
        self.assertEqual(create[:3], ["compute", "image-capability-schema", "create"])
        schema = json.loads(create[create.index("--schema-data") + 1])
        self.assertEqual(schema["Compute.Firmware"]["defaultValue"], "UEFI_64")

    def test_incompatible_image_override_is_updated(self):
        updated = {
            key: {"defaultValue": value}
            for key, value in deploy.REQUIRED_CAPABILITIES.items()
        }
        oci = SequenceOCI(
            self.global_responses(
                {"Compute.Firmware": {"default-value": "BIOS"}}
            )
            + [{"data": {"schema-data": updated}}]
        )
        result = deploy.validate_capabilities(
            valid_args(), oci, "image", {"tag": "value"}
        )
        self.assertEqual(result, deploy.REQUIRED_CAPABILITIES)
        update = oci.calls[4][0]
        self.assertEqual(update[:3], ["compute", "image-capability-schema", "update"])

    def test_shape_compatibility_adds_only_when_missing(self):
        oci = SequenceOCI(
            [
                {"data": []},
                {"data": {}},
                {"data": [{"shape": deploy.DEFAULT_SHAPE}]},
            ]
        )
        deploy.ensure_shape_compatibility(oci, "image", deploy.DEFAULT_SHAPE)
        self.assertEqual(len(oci.calls), 3)
        self.assertIn("add", oci.calls[1][0])
        oci = SequenceOCI([{"data": [{"shape": deploy.DEFAULT_SHAPE}]}])
        deploy.ensure_shape_compatibility(oci, "image", deploy.DEFAULT_SHAPE)
        self.assertEqual(len(oci.calls), 1)

    def test_ambiguous_tag_recovery_fails_closed(self):
        tags = {"deployment": "one"}
        resources = [
            {"id": "first", "freeform-tags": tags},
            {"id": "second", "freeform-tags": tags},
        ]
        with self.assertRaisesRegex(deploy.DeploymentError, "multiple"):
            deploy.exactly_one_tagged(resources, tags, "image")


class ObjectSafetyTests(unittest.TestCase):
    def test_reused_object_requires_size_and_release_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "image.qcow2"
            image.write_bytes(b"image")
            digest = hashlib.sha256(b"image").hexdigest()
            args = mock.Mock(reuse_object=True, resume=False)
            state = FakeState()
            oci = SequenceOCI(
                [
                    {
                        "content-length": str(image.stat().st_size),
                        "opc-meta-archlinuxarm-oci-sha256": digest,
                    }
                ]
            )
            self.assertEqual(
                deploy.ensure_object(args, oci, state, "ns", "bucket", image, digest),
                image.name,
            )

    def test_cleanup_refuses_object_not_owned_by_deployment(self):
        args = mock.Mock(cleanup_object=True)
        state = FakeState(
            {
                "resources": {
                    "object": {"uploaded": False},
                    "image": {"lifecycle_state": "AVAILABLE"},
                    "instance": {"lifecycle_state": "RUNNING"},
                }
            }
        )
        with self.assertRaisesRegex(deploy.DeploymentError, "not uploaded"):
            deploy.cleanup_object(args, mock.Mock(), state, "ns", "bucket", "image")

    def test_cleanup_skips_an_already_deleted_object(self):
        args = mock.Mock(cleanup_object=True)
        state = FakeState({"resources": {"object": {"uploaded": True, "deleted": True}}})
        oci = mock.Mock()
        deploy.cleanup_object(args, oci, state, "ns", "bucket", "image")
        oci.run.assert_not_called()

    def test_upload_records_release_hash_as_object_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "image.qcow2"
            image.write_bytes(b"image")
            digest = hashlib.sha256(b"image").hexdigest()
            args = mock.Mock(reuse_object=False, resume=False)
            state = FakeState(
                {"deployment_id": str(deploy.uuid.uuid4()), "resources": {}}
            )
            oci = SequenceOCI(
                [
                    None,
                    None,
                    {
                        "content-length": str(image.stat().st_size),
                        "opc-meta-archlinuxarm-oci-sha256": digest,
                    },
                ]
            )
            deploy.ensure_object(args, oci, state, "ns", "bucket", image, digest)
        upload = oci.calls[1][0]
        metadata = json.loads(upload[upload.index("--metadata") + 1])
        self.assertEqual(metadata["archlinuxarm-oci-sha256"], digest)
        self.assertIn("--opc-client-request-id", upload)

    def test_resume_skips_missing_object_after_image_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "image.qcow2"
            image.write_bytes(b"image")
            digest = hashlib.sha256(b"image").hexdigest()
            args = mock.Mock(reuse_object=False, resume=True)
            state = FakeState(
                {
                    "resources": {
                        "object": {
                            "name": image.name,
                            "uploaded": True,
                            "sha256": digest,
                        },
                        "image": {"id": "ocid1.image.oc1..example"},
                    }
                }
            )
            oci = SequenceOCI([])
            self.assertEqual(
                deploy.ensure_object(args, oci, state, "ns", "bucket", image, digest),
                image.name,
            )
        self.assertEqual(oci.calls, [])

    def test_bucket_cleanup_implies_uploaded_object_cleanup(self):
        args = mock.Mock(cleanup_object=False, cleanup_bucket=True)
        state = FakeState(
            {
                "deployment_id": str(deploy.uuid.uuid4()),
                "resources": {
                    "object": {"uploaded": True},
                    "image": {"lifecycle_state": "AVAILABLE"},
                    "instance": {"lifecycle_state": "RUNNING"},
                },
            }
        )
        oci = SequenceOCI([{}, None, None])
        deploy.cleanup_object(args, oci, state, "ns", "bucket", "image")
        self.assertEqual(oci.calls[1][0][:3], ["os", "object", "delete"])
        self.assertEqual(oci.calls[1][1], {"empty_data": {}})
        self.assertTrue(state.data["resources"]["object"]["deleted"])

    def test_object_cleanup_marks_missing_object_deleted(self):
        args = mock.Mock(cleanup_object=True)
        state = FakeState(
            {
                "deployment_id": str(deploy.uuid.uuid4()),
                "resources": {
                    "object": {"uploaded": True},
                    "image": {"lifecycle_state": "AVAILABLE"},
                    "instance": {"lifecycle_state": "RUNNING"},
                },
            }
        )
        oci = SequenceOCI(
            [deploy.OCIError([], 1, "ServiceError: 404 NotAuthorizedOrNotFound")]
        )
        deploy.cleanup_object(args, oci, state, "ns", "bucket", "image")
        self.assertEqual(oci.calls[0][0][:3], ["os", "object", "head"])
        self.assertTrue(state.data["resources"]["object"]["deleted"])

    def test_bucket_cleanup_deletes_empty_bucket(self):
        args = mock.Mock(cleanup_bucket=True)
        state = FakeState(
            {
                "deployment_id": str(deploy.uuid.uuid4()),
                "resources": {
                    "image": {"lifecycle_state": "AVAILABLE"},
                    "instance": {"lifecycle_state": "RUNNING"},
                },
            }
        )
        oci = SequenceOCI(
            [
                {"data": []},
                {"data": {"objects": []}},
                {},
                deploy.OCIError([], 1, "ServiceError: 404 NotAuthorizedOrNotFound"),
            ]
        )
        deploy.cleanup_bucket(args, oci, state, "ns", "bucket")
        self.assertEqual(oci.calls[2][0][:3], ["os", "bucket", "delete"])
        self.assertEqual(oci.calls[2][1], {"empty_data": {}})
        self.assertTrue(state.data["resources"]["bucket"]["deleted"])

    def test_bucket_cleanup_refuses_non_empty_bucket(self):
        args = mock.Mock(cleanup_bucket=True)
        state = FakeState(
            {
                "deployment_id": str(deploy.uuid.uuid4()),
                "resources": {
                    "image": {"lifecycle_state": "AVAILABLE"},
                    "instance": {"lifecycle_state": "RUNNING"},
                },
            }
        )
        oci = SequenceOCI(
            [{"data": []}, {"data": {"objects": [{"name": "leftover"}]}}]
        )
        with self.assertRaisesRegex(deploy.DeploymentError, "non-empty bucket"):
            deploy.cleanup_bucket(args, oci, state, "ns", "bucket")

    def test_bucket_object_names_accepts_bare_empty_list_response(self):
        oci = SequenceOCI([{"prefixes": []}])
        self.assertEqual(deploy.bucket_object_names(oci, "ns", "bucket"), [])


class CleanDeploymentTests(unittest.TestCase):
    def write_state(self, path):
        path.write_text(
            json.dumps(
                {
                    "schema_version": deploy.STATE_SCHEMA_VERSION,
                    "deployment_id": str(deploy.uuid.uuid4()),
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "phase": "failed",
                    "resources": {
                        "bucket": {"namespace": "ns", "name": "bucket"},
                        "object": {
                            "namespace": "ns",
                            "bucket": "bucket",
                            "name": "image.qcow2",
                            "uploaded": True,
                        },
                        "image": {
                            "id": "ocid1.image.oc1.us-test-1.image",
                            "created": True,
                        },
                        "instance": {
                            "id": "ocid1.instance.oc1.us-test-1.instance",
                            "created": True,
                        },
                    },
                }
            )
        )

    def test_clean_deployment_deletes_recorded_resources_and_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            self.write_state(state_path)
            args = mock.Mock(
                state_file=state_path,
                profile="DEFAULT",
                config_file=None,
                region=None,
                verbose=False,
                instance_timeout=30,
                image_timeout=30,
            )
            oci = SequenceOCI(
                [
                    {"data": {"lifecycle-state": "RUNNING"}},
                    {},
                    {"data": {"lifecycle-state": "TERMINATED"}},
                    {"data": {"lifecycle-state": "AVAILABLE"}},
                    {},
                    {"data": {"lifecycle-state": "DELETED"}},
                    {"content-length": "1"},
                    {},
                    deploy.OCIError([], 1, "ServiceError: 404 NotAuthorizedOrNotFound"),
                    {
                        "data": {
                            "public-access-type": "NoPublicAccess",
                            "storage-tier": "Standard",
                        }
                    },
                    {
                        "data": [
                            {
                                "object": "image.qcow2",
                                "upload-id": "upload",
                            }
                        ]
                    },
                    {},
                    {"data": {"objects": []}},
                    {},
                    deploy.OCIError([], 1, "ServiceError: 404 NotAuthorizedOrNotFound"),
                ]
            )
            with (
                mock.patch.object(deploy.shutil, "which", return_value="/usr/bin/oci"),
                mock.patch.object(deploy, "OCIRunner", return_value=oci),
            ):
                self.assertEqual(deploy.clean_deployment(args), 0)
            self.assertFalse(state_path.exists())
        commands = [call[0] for call in oci.calls]
        self.assertEqual(commands[1][:3], ["compute", "instance", "terminate"])
        self.assertEqual(commands[4][:3], ["compute", "image", "delete"])
        self.assertEqual(commands[7][:3], ["os", "object", "delete"])
        self.assertEqual(commands[10][:3], ["os", "multipart", "list"])
        self.assertEqual(commands[11][:3], ["os", "multipart", "abort"])
        self.assertEqual(commands[13][:3], ["os", "bucket", "delete"])


class MutationCommandTests(unittest.TestCase):
    def state(self):
        return FakeState(
            {"deployment_id": str(deploy.uuid.uuid4()), "resources": {}}
        )

    def ssh_public_key(self, directory):
        key = Path(directory) / "key.pub"
        key.write_text(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEYdU6aY7SBVn3fnVPoknHLghaHffieYYPuJ0a1PUKiT test\n"
        )
        return key

    def test_image_import_command_is_explicit_and_records_ocid(self):
        args = mock.Mock(
            resume=False,
            image_name="Arch",
            compartment_id=COMPARTMENT,
            image_timeout=30,
        )
        state = self.state()
        oci = SequenceOCI(
            [
                {"data": {"id": "image-id", "lifecycle-state": "IMPORTING"}},
                {"data": {"id": "image-id", "lifecycle-state": "AVAILABLE"}},
            ]
        )
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            image_id, _ = deploy.create_image(
                args, oci, state, "namespace", "bucket", "image.qcow2", {"tag": "value"}
            )
        command = oci.calls[0][0]
        self.assertEqual(image_id, "image-id")
        self.assertIn("may take several minutes", output.getvalue())
        self.assertEqual(command[:4], ["compute", "image", "import", "from-object"])
        self.assertEqual(command[command.index("--source-image-type") + 1], "QCOW2")
        self.assertEqual(command[command.index("--launch-mode") + 1], "PARAVIRTUALIZED")
        self.assertIn("--opc-client-request-id", command)

    def test_cloud_init_user_data_installs_alarm_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            user_data = deploy.cloud_init_user_data(self.ssh_public_key(temporary))
        self.assertIn("name: alarm", user_data)
        self.assertIn("ssh_authorized_keys:", user_data)
        self.assertIn(
            "AAAAC3NzaC1lZDI1NTE5AAAAIEYdU6aY7SBVn3fnVPoknHLghaHffieYYPuJ0a1PUKiT",
            user_data,
        )

    def test_instance_launch_command_contains_only_public_ssh_key_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = mock.Mock(
                resume=False,
                instance_name="instance",
                compartment_id=COMPARTMENT,
                subnet_id=SUBNET,
                availability_domain="test:AD-1",
                shape=deploy.DEFAULT_SHAPE,
                ocpus=1.0,
                memory_gbs=6.0,
                boot_volume_gbs=50,
                ssh_public_key=self.ssh_public_key(temporary),
                assign_public_ip=False,
                instance_timeout=30,
            )
            state = self.state()
            oci = SequenceOCI(
                [
                    {"data": {"id": "instance-id", "lifecycle-state": "PROVISIONING"}},
                    {"data": {"id": "instance-id", "lifecycle-state": "RUNNING"}},
                ]
            )
            instance_id, _ = deploy.launch_instance(
                args, oci, state, "image-id", {"tag": "value"}
            )
        command = oci.calls[0][0]
        self.assertEqual(instance_id, "instance-id")
        self.assertEqual(
            command[command.index("--ssh-authorized-keys-file") + 1],
            str(args.ssh_public_key),
        )
        self.assertIn("--user-data-file", command)
        self.assertNotIn("--launch-options", command)
        self.assertEqual(command[command.index("--assign-public-ip") + 1], "false")
        self.assertNotIn("private", " ".join(str(part) for part in command))
        self.assertIn("--opc-client-request-id", command)

    def test_resume_launches_when_empty_instance_list_has_empty_stdout(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = mock.Mock(
                resume=True,
                instance_name="instance",
                compartment_id=COMPARTMENT,
                subnet_id=SUBNET,
                availability_domain="test:AD-1",
                shape=deploy.DEFAULT_SHAPE,
                ocpus=1.0,
                memory_gbs=6.0,
                boot_volume_gbs=50,
                ssh_public_key=self.ssh_public_key(temporary),
                assign_public_ip=False,
                instance_timeout=30,
            )
            state = self.state()
            oci = SequenceOCI(
                [
                    {"data": []},
                    {"data": {"id": "instance-id", "lifecycle-state": "PROVISIONING"}},
                    {"data": {"id": "instance-id", "lifecycle-state": "RUNNING"}},
                ]
            )
            instance_id, _ = deploy.launch_instance(
                args, oci, state, "image-id", {"tag": "value"}
            )
        self.assertEqual(instance_id, "instance-id")
        self.assertEqual(oci.calls[0][0][:3], ["compute", "instance", "list"])
        self.assertEqual(oci.calls[0][1]["empty_data"], [])
        self.assertEqual(oci.calls[1][0][:3], ["compute", "instance", "launch"])

    def test_resume_ignores_terminated_tagged_instances(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = mock.Mock(
                resume=True,
                instance_name="instance",
                compartment_id=COMPARTMENT,
                subnet_id=SUBNET,
                availability_domain="test:AD-1",
                shape=deploy.DEFAULT_SHAPE,
                ocpus=1.0,
                memory_gbs=6.0,
                boot_volume_gbs=50,
                ssh_public_key=self.ssh_public_key(temporary),
                assign_public_ip=False,
                instance_timeout=30,
            )
            tags = {"deployment": "one"}
            state = self.state()
            oci = SequenceOCI(
                [
                    {
                        "data": [
                            {
                                "id": "old-instance",
                                "lifecycle-state": "TERMINATED",
                                "freeform-tags": tags,
                            }
                        ]
                    },
                    {"data": {"id": "instance-id", "lifecycle-state": "PROVISIONING"}},
                    {"data": {"id": "instance-id", "lifecycle-state": "RUNNING"}},
                ]
            )
            instance_id, _ = deploy.launch_instance(args, oci, state, "image-id", tags)
        self.assertEqual(instance_id, "instance-id")
        self.assertEqual(oci.calls[1][0][:3], ["compute", "instance", "launch"])


class DryRunTests(unittest.TestCase):
    def test_existing_state_file_fails_before_discovery_or_prompts(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "state.json"
            state_path.write_text("{}")
            args = valid_args("--state-file", str(state_path))
            with (
                mock.patch.object(deploy, "validate_local_tools") as tools,
                mock.patch.object(deploy, "resolve_deployment_inputs") as discovery,
                self.assertRaisesRegex(deploy.DeploymentError, "--resume"),
            ):
                deploy.deploy(args)
        tools.assert_not_called()
        discovery.assert_not_called()

    def test_dry_run_stops_before_state_or_mutations(self):
        args = valid_args("--dry-run", "--reuse-download")
        metadata = {"image_filename": "image.qcow2", "image_sha256": "a" * 64}
        with (
            mock.patch.object(deploy, "validate_local_tools"),
            mock.patch.object(
                deploy,
                "validate_prerequisites",
                return_value=("namespace", {}, {}, "fingerprint"),
            ),
            mock.patch.object(
                deploy,
                "obtain_release",
                return_value=(metadata, Path("image.qcow2"), "a" * 64),
            ),
            mock.patch.object(deploy, "StateFile") as state,
            mock.patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(deploy.deploy(args), 0)
        state.assert_not_called()
        self.assertIn("Planned mutations", output.getvalue())


if __name__ == "__main__":
    unittest.main()
