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

    def test_default_name_requires_and_accepts_confirmation(self):
        args = no_bucket_args()
        prompt = mock.Mock(return_value="")
        deploy.resolve_bucket_selection(
            args, environ={"USER": "test-user"}, input_fn=prompt
        )
        self.assertEqual(args.create_bucket, "archlinuxarm-oci-import-test-user")
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
        state.data = {"inputs": {"shape": "A1"}, "release": {"sha256": "a" * 64}}
        deploy.validate_resume(state, {"shape": "A1"}, "a" * 64)
        with self.assertRaisesRegex(deploy.DeploymentError, "arguments"):
            deploy.validate_resume(state, {"shape": "different"}, "a" * 64)
        with self.assertRaisesRegex(deploy.DeploymentError, "release"):
            deploy.validate_resume(state, {"shape": "A1"}, "b" * 64)


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
            key: {"default-value": value}
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

    def test_global_capabilities_are_accepted(self):
        oci = SequenceOCI(self.global_responses())
        result = deploy.validate_capabilities(oci, "image")
        self.assertEqual(result, deploy.REQUIRED_CAPABILITIES)

    def test_incompatible_image_override_is_rejected(self):
        oci = SequenceOCI(
            self.global_responses({"Compute.Firmware": {"default-value": "BIOS"}})
        )
        with self.assertRaisesRegex(deploy.DeploymentError, "Compute.Firmware"):
            deploy.validate_capabilities(oci, "image")

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


class MutationCommandTests(unittest.TestCase):
    def state(self):
        return FakeState(
            {"deployment_id": str(deploy.uuid.uuid4()), "resources": {}}
        )

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
        image_id, _ = deploy.create_image(
            args, oci, state, "namespace", "bucket", "image.qcow2", {"tag": "value"}
        )
        command = oci.calls[0][0]
        self.assertEqual(image_id, "image-id")
        self.assertEqual(command[:4], ["compute", "image", "import", "from-object"])
        self.assertEqual(command[command.index("--source-image-type") + 1], "QCOW2")
        self.assertEqual(command[command.index("--launch-mode") + 1], "PARAVIRTUALIZED")
        self.assertIn("--opc-client-request-id", command)

    def test_instance_launch_command_contains_only_public_ssh_key_path(self):
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
            ssh_public_key=Path("/keys/public.pub"),
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
            "/keys/public.pub",
        )
        self.assertEqual(command[command.index("--assign-public-ip") + 1], "false")
        self.assertNotIn("private", " ".join(str(part) for part in command))
        self.assertIn("--opc-client-request-id", command)


class DryRunTests(unittest.TestCase):
    def test_dry_run_stops_before_state_or_mutations(self):
        args = valid_args("--dry-run", "--reuse-download")
        metadata = {"image_filename": "image.qcow2", "image_sha256": "a" * 64}
        with (
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
