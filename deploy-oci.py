#!/usr/bin/env python3
"""Deploy the latest published Arch Linux ARM image to OCI Ampere A1."""

import argparse
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid


PROJECT = Path(__file__).resolve().parent
DOWNLOADER = PROJECT / "download-latest.py"
STATE_SCHEMA_VERSION = 1
DEFAULT_SHAPE = "VM.Standard.A1.Flex"
DEFAULT_SSH_KEY = Path.home() / ".ssh" / "archlinuxarm-oci"
REQUIRED_CAPABILITIES = {
    "Compute.Firmware": "UEFI_64",
    "Compute.LaunchMode": "PARAVIRTUALIZED",
    "Network.AttachmentType": "PARAVIRTUALIZED",
    "Storage.BootVolumeType": "PARAVIRTUALIZED",
}
ACTIVE_IMAGE_STATES = {"IMPORTING", "PROVISIONING"}
ACTIVE_INSTANCE_STATES = {"PROVISIONING", "STARTING"}
OCID_RE = re.compile(r"^ocid1\.[a-z0-9-]+\.oc[0-9]*\.[a-z0-9-]*\.[A-Za-z0-9._-]+$")


class DeploymentError(RuntimeError):
    """A deployment failure safe to show without a traceback."""


class OCIError(DeploymentError):
    def __init__(self, command, returncode, stderr):
        self.command = command
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(self.stderr or f"OCI CLI exited with status {returncode}")

    @property
    def not_found(self):
        return bool(
            re.search(r"\b404\b|NotAuthorizedOrNotFound", self.stderr, re.IGNORECASE)
        )


def compact_command(command):
    return shlex.join(str(part) for part in command)


def require_object(value, description="OCI response"):
    if not isinstance(value, dict):
        raise DeploymentError(f"{description} is not a JSON object")
    return value


def response_data(response, expected_type=dict, description="OCI response"):
    response = require_object(response, description)
    data = response.get("data")
    if not isinstance(data, expected_type):
        raise DeploymentError(f"{description} has invalid or missing data")
    return data


def validate_ocid(value, resource):
    if not OCID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"invalid {resource} OCID: {value}")
    return value


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


class StateFile:
    def __init__(self, path, resume=False):
        self.path = Path(path).expanduser().resolve()
        if self.path.exists():
            if not resume:
                raise DeploymentError(
                    f"state file already exists: {self.path}; pass --resume to use it"
                )
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise DeploymentError(f"could not read state file: {error}") from error
            if self.data.get("schema_version") != STATE_SCHEMA_VERSION:
                raise DeploymentError("state file uses an unsupported schema version")
            self.loaded = True
        else:
            if resume:
                raise DeploymentError(f"resume state file does not exist: {self.path}")
            now = timestamp()
            self.data = {
                "schema_version": STATE_SCHEMA_VERSION,
                "deployment_id": str(uuid.uuid4()),
                "created_at": now,
                "updated_at": now,
                "phase": "initialized",
                "resources": {},
            }
            self.loaded = False

    def update(self, phase=None, **values):
        if phase is not None:
            self.data["phase"] = phase
        self.data.update(values)
        self.data["updated_at"] = timestamp()
        self.write()

    def resource(self, resource_name, **values):
        resources = self.data.setdefault("resources", {})
        current = resources.setdefault(resource_name, {})
        current.update(values)
        self.data["updated_at"] = timestamp()
        self.write()

    def record_failure(self, error):
        self.data["failure"] = {
            "at": timestamp(),
            "type": type(error).__name__,
            "message": str(error),
        }
        self.data["updated_at"] = timestamp()
        self.write()

    def write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(self.data, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise


class OCIRunner:
    def __init__(self, profile="DEFAULT", config_file=None, region=None, verbose=False):
        self.profile = profile
        self.config_file = config_file
        self.region = region
        self.verbose = verbose

    def command(self, arguments):
        command = ["oci"] + list(arguments)
        if self.config_file:
            command.extend(["--config-file", str(self.config_file)])
        if self.profile:
            command.extend(["--profile", self.profile])
        if self.region:
            command.extend(["--region", self.region])
        command.extend(["--output", "json"])
        return command

    def run(self, arguments, *, passthrough=False, empty_data=None):
        command = self.command(arguments)
        if self.verbose:
            print(f"+ {compact_command(command)}", file=sys.stderr, flush=True)
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=None if passthrough else subprocess.PIPE,
            stderr=None if passthrough else subprocess.PIPE,
        )
        if completed.returncode:
            raise OCIError(command, completed.returncode, completed.stderr or "")
        if passthrough:
            return None
        if completed.stdout.strip() == "" and empty_data is not None:
            return {"data": empty_data}
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise DeploymentError(
                f"OCI CLI returned invalid JSON for: {compact_command(command)}"
            ) from error


def timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def parse_release(directory):
    directory = Path(directory)
    metadata_path = directory / "build-info.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        filename = metadata["image_filename"]
        expected = metadata["image_sha256"].lower()
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise DeploymentError("downloaded build-info.json is invalid") from error
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise DeploymentError("build-info.json contains an unsafe image filename")
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.qcow2", filename):
        raise DeploymentError("build-info.json contains an invalid image filename")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise DeploymentError("build-info.json contains an invalid image SHA-256")
    image = directory / filename
    checksum_path = directory / f"{filename}.sha256"
    try:
        fields = checksum_path.read_text(encoding="utf-8").strip().split(maxsplit=1)
    except OSError as error:
        raise DeploymentError("published checksum file is missing") from error
    if len(fields) != 2 or fields[1].lstrip("*") != filename:
        raise DeploymentError("published checksum file names a different image")
    if fields[0].lower() != expected:
        raise DeploymentError("published checksum disagrees with build-info.json")
    if not image.is_file():
        raise DeploymentError(f"downloaded image is missing: {image}")
    actual = sha256_file(image)
    if actual != expected:
        raise DeploymentError("downloaded image SHA-256 does not match its metadata")
    return metadata, image, actual


def obtain_release(args):
    directory = args.download_dir
    if args.reuse_download or args.resume:
        return parse_release(directory)
    directory.mkdir(parents=True, exist_ok=True)
    print(f"VERIFY  Downloading and verifying the latest release in {directory}")
    subprocess.run([sys.executable, str(DOWNLOADER), str(directory)], check=True)
    return parse_release(directory)


def key_fingerprint(path):
    completed = subprocess.run(
        ["ssh-keygen", "-l", "-f", str(path)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        raise DeploymentError(completed.stderr.strip() or f"invalid SSH key: {path}")
    fields = completed.stdout.split()
    if len(fields) < 2:
        raise DeploymentError("ssh-keygen returned an invalid fingerprint")
    return fields[1]


def prepare_ssh_key(args):
    private_key = args.ssh_key
    public_key = Path(f"{private_key}.pub")
    private_exists = private_key.exists()
    public_exists = public_key.exists()
    if private_exists != public_exists:
        missing = public_key if private_exists else private_key
        raise DeploymentError(
            f"incomplete SSH key pair at {private_key}; missing {missing}"
        )
    if not private_exists:
        private_key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        print(f"SSH-KEY  Generating dedicated Ed25519 key pair at {private_key}")
        completed = subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-a",
                "64",
                "-N",
                "",
                "-C",
                "archlinuxarm-oci",
                "-f",
                str(private_key),
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode:
            raise DeploymentError(
                completed.stderr.strip() or "ssh-keygen could not create the key pair"
            )
    if not private_key.is_file() or not public_key.is_file():
        raise DeploymentError(f"SSH key pair was not created at {private_key}")
    private_fingerprint = key_fingerprint(private_key)
    public_fingerprint = key_fingerprint(public_key)
    if private_fingerprint != public_fingerprint:
        raise DeploymentError(
            f"SSH private and public keys do not match: {private_key}, {public_key}"
        )
    args.ssh_private_key = private_key
    args.ssh_public_key = public_key
    return public_fingerprint


def default_bucket_name(environ=None):
    environ = os.environ if environ is None else environ
    username = environ.get("USER") or getpass.getuser() or "user"
    safe_username = re.sub(r"[^A-Za-z0-9._-]+", "-", username).strip(".-")
    return f"archlinuxarm-oci-import-{safe_username or 'user'}"


def apply_deployment_environment(args, environ=None):
    environ = os.environ if environ is None else environ
    fields = (
        ("compartment_id", "COMPARTMENT_ID", "compartment"),
        ("subnet_id", "SUBNET_ID", "subnet"),
        ("availability_domain", "AVAILABILITY_DOMAIN", None),
    )
    for attribute, variable, ocid_type in fields:
        if getattr(args, attribute):
            continue
        value = environ.get(variable, "").strip()
        if not value:
            continue
        if ocid_type:
            try:
                value = validate_ocid(value, ocid_type)
            except argparse.ArgumentTypeError as error:
                raise DeploymentError(str(error)) from error
        setattr(args, attribute, value)


def choose_candidate(label, candidates, describe, input_fn=None, default=None):
    if not candidates:
        raise DeploymentError(f"no suitable {label}s were discovered")
    if len(candidates) == 1:
        selected = candidates[0]
        print(f"DISCOVER  Automatically selected {label}: {describe(selected)}")
        return selected
    print(f"DISCOVER  Available {label}s:")
    for number, candidate in enumerate(candidates, 1):
        suffix = " (default)" if default == number - 1 else ""
        print(f"  {number}. {describe(candidate)}{suffix}")
    prompt = f"Select {label} [1-{len(candidates)}]"
    if default is not None:
        prompt += f" (default {default + 1})"
    prompt += ": "
    ask = input_fn or input
    while True:
        try:
            answer = ask(prompt).strip()
        except EOFError as error:
            raise DeploymentError(
                f"{label} selection requires input; pass an explicit command-line option"
            ) from error
        if not answer and default is not None:
            return candidates[default]
        try:
            selection = int(answer)
        except ValueError:
            selection = 0
        if 1 <= selection <= len(candidates):
            return candidates[selection - 1]
        print(f"Enter a number from 1 to {len(candidates)}.", file=sys.stderr)


def discover_subnets(args, oci):
    response = response_data(
        oci.run(
            [
                "search",
                "resource",
                "structured-search",
                "--query-text",
                "query subnet resources",
                "--limit",
                "1000",
            ]
        ),
        dict,
        "subnet search response",
    )
    summaries = response.get("items")
    if not isinstance(summaries, list):
        raise DeploymentError("subnet search response has invalid or missing items")
    subnets = []
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        subnet_id = summary.get("identifier") or summary.get("id")
        if not isinstance(subnet_id, str):
            continue
        try:
            subnet = response_data(
                oci.run(["network", "subnet", "get", "--subnet-id", subnet_id]),
                dict,
                "subnet response",
            )
        except OCIError as error:
            if error.not_found:
                continue
            raise
        if subnet.get("lifecycle-state") != "AVAILABLE":
            continue
        if args.assign_public_ip and subnet.get("prohibit-public-ip-on-vnic") is True:
            continue
        subnets.append(subnet)
    return sorted(
        subnets,
        key=lambda subnet: (
            str(subnet.get("display-name") or "").lower(),
            str(subnet.get("id") or ""),
        ),
    )


def describe_subnet(subnet):
    name = subnet.get("display-name") or "unnamed subnet"
    cidr = subnet.get("cidr-block") or "unknown CIDR"
    scope = subnet.get("availability-domain") or "regional"
    return f"{name} — {cidr} — {scope}"


def shapes_in_domain(args, oci, availability_domain):
    shapes = response_data(
        oci.run(
            [
                "compute",
                "shape",
                "list",
                "--compartment-id",
                args.compartment_id,
                "--availability-domain",
                availability_domain,
                "--all",
            ]
        ),
        list,
        "shape response",
    )
    return next(
        (
            shape
            for shape in shapes
            if isinstance(shape, dict) and shape.get("shape") == args.shape
        ),
        None,
    )


def resolve_deployment_inputs(args, oci, environ=None, input_fn=None):
    apply_deployment_environment(args, environ)
    if args.compartment_id and args.subnet_id and args.availability_domain:
        return {}
    discovered = {}
    if args.subnet_id:
        subnet = response_data(
            oci.run(["network", "subnet", "get", "--subnet-id", args.subnet_id]),
            dict,
            "subnet response",
        )
    else:
        print("DISCOVER  Searching OCI for accessible subnets")
        candidates = discover_subnets(args, oci)
        subnet = choose_candidate(
            "subnet", candidates, describe_subnet, input_fn=input_fn
        )
        args.subnet_id = subnet.get("id")
        if not isinstance(args.subnet_id, str):
            raise DeploymentError("selected subnet has no OCID")
    discovered["subnet"] = subnet
    if args.compartment_id is None:
        args.compartment_id = subnet.get("compartment-id")
        if not isinstance(args.compartment_id, str):
            raise DeploymentError("selected subnet has no compartment OCID")
        print("DISCOVER  Using the selected subnet's compartment for the deployment")

    subnet_ad = subnet.get("availability-domain")
    if args.availability_domain is None and subnet_ad:
        args.availability_domain = subnet_ad
    if args.availability_domain is None:
        domains = response_data(
            oci.run(
                [
                    "iam",
                    "availability-domain",
                    "list",
                    "--compartment-id",
                    args.compartment_id,
                    "--all",
                ]
            ),
            list,
            "availability-domain response",
        )
        offered = []
        for domain in domains:
            if not isinstance(domain, dict) or not isinstance(domain.get("name"), str):
                continue
            shape = shapes_in_domain(args, oci, domain["name"])
            if shape is not None:
                offered.append((domain["name"], shape))
        offered.sort(key=lambda item: item[0])
        selected_domain, selected_shape = choose_candidate(
            "A1 availability domain",
            offered,
            lambda item: item[0],
            input_fn=input_fn,
            default=0 if offered else None,
        )
        args.availability_domain = selected_domain
        discovered["shape"] = selected_shape
    return discovered


def apply_resume_defaults(args):
    if not args.resume:
        return
    try:
        saved = json.loads(args.state_file.read_text(encoding="utf-8"))
        inputs = saved["inputs"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise DeploymentError(
            f"could not read resume inputs from {args.state_file}"
        ) from error
    if not isinstance(inputs, dict):
        raise DeploymentError(f"resume inputs are invalid in {args.state_file}")
    for attribute in ("compartment_id", "subnet_id", "availability_domain"):
        if getattr(args, attribute) is None and inputs.get(attribute):
            setattr(args, attribute, inputs[attribute])
    if args.bucket is None and args.create_bucket is None:
        if inputs.get("bucket"):
            args.bucket = inputs["bucket"]
        elif inputs.get("create_bucket"):
            args.create_bucket = inputs["create_bucket"]
    if args.cleanup_bucket is None and "cleanup_bucket" in inputs:
        args.cleanup_bucket = inputs["cleanup_bucket"]


def validate_state_file_selection(args):
    if args.dry_run:
        return
    if args.resume:
        if not args.state_file.exists():
            raise DeploymentError(f"resume state file does not exist: {args.state_file}")
    elif args.state_file.exists():
        raise DeploymentError(
            f"state file already exists: {args.state_file}; pass --resume to use it"
        )


def resolve_bucket_selection(args, environ=None, input_fn=None):
    if args.bucket or args.create_bucket:
        return
    args.allow_existing_create_bucket = True
    environ = os.environ if environ is None else environ
    configured = environ.get("BUCKET_NAME", "").strip()
    if configured:
        args.create_bucket = configured
        print(
            "BUCKET  Selected private Standard bucket from BUCKET_NAME: "
            f"{configured}"
        )
        return
    name = default_bucket_name(environ)
    prompt = (
        f"Use default private bucket {name!r} and create it if needed? "
        "[Y/n] "
    )
    try:
        answer = (input_fn or input)(prompt).strip().lower()
    except EOFError as error:
        raise DeploymentError(
            "bucket selection requires confirmation; set BUCKET_NAME or pass "
            "--bucket/--create-bucket"
        ) from error
    if answer not in ("", "y", "yes"):
        raise DeploymentError("bucket creation cancelled")
    args.create_bucket = name
    print(f"BUCKET  Selected private Standard bucket: {name}")


def resolve_bucket_cleanup(args, input_fn=None):
    if args.cleanup_bucket is not None:
        return
    args.cleanup_bucket = False
    if args.bucket or not getattr(args, "allow_existing_create_bucket", False):
        return
    name = args.create_bucket
    prompt = (
        f"Delete bucket {name!r} after successful deployment if it is empty? "
        "[N/y] "
    )
    try:
        answer = (input_fn or input)(prompt).strip().lower()
    except EOFError:
        return
    if answer in ("y", "yes"):
        args.cleanup_bucket = True


def find_shape(shapes, name):
    for shape in shapes:
        if isinstance(shape, dict) and shape.get("shape") == name:
            return shape
    raise DeploymentError(f"shape {name} is not offered in the selected availability domain")


def validate_shape_config(shape, ocpus, memory_gbs):
    if not shape.get("is-flexible"):
        raise DeploymentError(f"shape {shape.get('shape')} is not flexible")
    ocpu_options = shape.get("ocpu-options") or {}
    memory_options = shape.get("memory-options") or {}
    minimum_ocpus = ocpu_options.get("min", 1)
    maximum_ocpus = ocpu_options.get("max", shape.get("ocpus", ocpus))
    minimum_memory = memory_options.get("min-in-g-bs", 1)
    maximum_memory = memory_options.get(
        "max-in-g-bs", shape.get("memory-in-gbs", memory_gbs)
    )
    minimum_per_ocpu = memory_options.get("min-per-ocpu-in-gbs", 0)
    maximum_per_ocpu = memory_options.get("max-per-ocpu-in-gbs", float("inf"))
    if not minimum_ocpus <= ocpus <= maximum_ocpus:
        raise DeploymentError(
            f"requested OCPUs {ocpus} are outside shape range {minimum_ocpus}..{maximum_ocpus}"
        )
    if not minimum_memory <= memory_gbs <= maximum_memory:
        raise DeploymentError(
            f"requested memory {memory_gbs} GB is outside shape range "
            f"{minimum_memory}..{maximum_memory}"
        )
    per_ocpu = memory_gbs / ocpus
    if not minimum_per_ocpu <= per_ocpu <= maximum_per_ocpu:
        raise DeploymentError("requested memory per OCPU is outside the shape constraints")


def validate_local_tools():
    for program in ("curl", "oci", "ssh-keygen"):
        if shutil.which(program) is None:
            raise DeploymentError(f"required program not found: {program}")
    if not DOWNLOADER.is_file():
        raise DeploymentError(f"release downloader not found: {DOWNLOADER}")


def validate_prerequisites(args, oci, discovered=None):
    discovered = discovered or {}
    fingerprint = prepare_ssh_key(args)
    namespace = response_data(oci.run(["os", "ns", "get"]), str, "namespace response")
    subnet = discovered.get("subnet")
    if subnet is None:
        subnet = response_data(
            oci.run(["network", "subnet", "get", "--subnet-id", args.subnet_id]),
            dict,
            "subnet response",
        )
    if subnet.get("lifecycle-state") not in (None, "AVAILABLE"):
        raise DeploymentError("selected subnet is not available")
    subnet_ad = subnet.get("availability-domain")
    if subnet_ad and subnet_ad != args.availability_domain:
        raise DeploymentError(
            f"subnet availability domain {subnet_ad} does not match {args.availability_domain}"
        )
    if args.assign_public_ip and subnet.get("prohibit-public-ip-on-vnic") is True:
        raise DeploymentError("selected subnet prohibits public IP addresses")
    shape = discovered.get("shape")
    if shape is None:
        shape = shapes_in_domain(args, oci, args.availability_domain)
    if shape is None:
        raise DeploymentError(
            f"shape {args.shape} is not offered in the selected availability domain"
        )
    validate_shape_config(shape, args.ocpus, args.memory_gbs)
    return namespace, subnet, shape, fingerprint


def deployment_tags(state, image_sha256):
    return {
        "archlinuxarm-oci-deployment": state.data["deployment_id"],
        "archlinuxarm-oci-sha256": image_sha256,
    }


def client_request_id(state, operation):
    return str(uuid.uuid5(uuid.UUID(state.data["deployment_id"]), operation))


def tags_match(resource, tags):
    actual = resource.get("freeform-tags") if isinstance(resource, dict) else None
    return isinstance(actual, dict) and all(
        actual.get(key) == value for key, value in tags.items()
    )


def exactly_one_tagged(resources, tags, description):
    matches = [resource for resource in resources if tags_match(resource, tags)]
    if len(matches) > 1:
        raise DeploymentError(f"multiple {description} resources match this deployment")
    return matches[0] if matches else None


def get_bucket(oci, namespace, name):
    try:
        return response_data(
            oci.run(["os", "bucket", "get", "--namespace", namespace, "--name", name]),
            dict,
            "bucket response",
        )
    except OCIError as error:
        if error.not_found:
            return None
        raise


def ensure_bucket(args, oci, state, namespace, tags):
    name = args.bucket or args.create_bucket
    existing = get_bucket(oci, namespace, name)
    allow_existing = bool(getattr(args, "allow_existing_create_bucket", False))
    if args.bucket:
        if existing is None:
            raise DeploymentError(f"Object Storage bucket does not exist: {name}")
        if existing.get("public-access-type") != "NoPublicAccess":
            raise DeploymentError("Object Storage bucket must be private")
        if existing.get("storage-tier") not in (None, "Standard"):
            raise DeploymentError("Object Storage bucket must use the Standard tier")
        created = False
    else:
        if existing is not None:
            recorded = state.data.get("resources", {}).get("bucket", {})
            resumed_preexisting = (
                args.resume
                and recorded.get("name") == name
                and recorded.get("namespace") == namespace
                and recorded.get("created") is False
            )
            resume_without_bucket_record = args.resume and not recorded
            if allow_existing or resumed_preexisting or resume_without_bucket_record:
                print(f"UPLOAD  Using existing private Object Storage bucket {name}")
                created = False
            elif not args.resume:
                raise DeploymentError(
                    f"bucket already exists: {name}; use --bucket to select it"
                )
            elif not tags_match(existing, tags):
                raise DeploymentError(
                    "existing bucket is not tagged as part of this resumed deployment"
                )
            else:
                created = True
        else:
            print(f"UPLOAD  Creating private Object Storage bucket {name}")
            bucket = response_data(
                oci.run(
                    [
                        "os",
                        "bucket",
                        "create",
                        "--namespace",
                        namespace,
                        "--compartment-id",
                        args.object_compartment_id or args.compartment_id,
                        "--name",
                        name,
                        "--storage-tier",
                        "Standard",
                        "--public-access-type",
                        "NoPublicAccess",
                        "--freeform-tags",
                        json.dumps(tags, separators=(",", ":")),
                        "--opc-client-request-id",
                        client_request_id(state, "create-bucket"),
                    ]
                ),
                dict,
                "bucket create response",
            )
            existing = bucket
            created = True
    if existing.get("public-access-type") != "NoPublicAccess":
        raise DeploymentError("Object Storage bucket must be private")
    if existing.get("storage-tier") not in (None, "Standard"):
        raise DeploymentError("Object Storage bucket must use the Standard tier")
    state.resource(
        "bucket",
        namespace=namespace,
        name=name,
        created=created,
        compartment_id=existing.get("compartment-id") if existing else None,
    )
    return name


def head_object(oci, namespace, bucket, name):
    try:
        return oci.run(
            [
                "os",
                "object",
                "head",
                "--namespace",
                namespace,
                "--bucket-name",
                bucket,
                "--name",
                name,
            ]
        )
    except OCIError as error:
        if error.not_found:
            return None
        raise


def object_size(head):
    if not isinstance(head, dict):
        return None
    headers = head.get("headers") if isinstance(head.get("headers"), dict) else {}
    candidates = [
        head.get("content-length"),
        head.get("Content-Length"),
        headers.get("content-length"),
    ]
    for candidate in candidates:
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def object_sha256(head):
    if not isinstance(head, dict):
        return None
    headers = head.get("headers") if isinstance(head.get("headers"), dict) else {}
    metadata = head.get("metadata") if isinstance(head.get("metadata"), dict) else {}
    candidates = [
        head.get("opc-meta-archlinuxarm-oci-sha256"),
        head.get("archlinuxarm-oci-sha256"),
        headers.get("opc-meta-archlinuxarm-oci-sha256"),
        metadata.get("archlinuxarm-oci-sha256"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and re.fullmatch(
            r"[0-9a-fA-F]{64}", candidate
        ):
            return candidate.lower()
    return None


def ensure_object(args, oci, state, namespace, bucket, image, image_sha256):
    name = image.name
    recorded = state.data.get("resources", {}).get("object", {})
    recorded_image = state.data.get("resources", {}).get("image", {})
    if args.resume and recorded.get("deleted") and recorded_image.get("id"):
        return recorded.get("name", name)
    existing = head_object(oci, namespace, bucket, name)
    uploaded = False
    if existing is not None:
        if not (args.reuse_object or args.resume):
            raise DeploymentError(
                f"Object Storage object already exists: {bucket}/{name}; pass --reuse-object"
            )
        remote_size = object_size(existing)
        if remote_size is None or remote_size != image.stat().st_size:
            raise DeploymentError("existing Object Storage object has a different size")
        if object_sha256(existing) != image_sha256:
            raise DeploymentError(
                "existing Object Storage object lacks matching release SHA-256 metadata"
            )
    else:
        print(f"UPLOAD  Uploading {image} to {bucket}/{name}")
        oci.run(
            [
                "os",
                "object",
                "put",
                "--namespace",
                namespace,
                "--bucket-name",
                bucket,
                "--name",
                name,
                "--file",
                str(image),
                "--content-type",
                "application/octet-stream",
                "--verify-checksum",
                "--no-overwrite",
                "--metadata",
                json.dumps(
                    {"archlinuxarm-oci-sha256": image_sha256}, separators=(",", ":")
                ),
                "--opc-client-request-id",
                client_request_id(state, "upload-object"),
            ],
            passthrough=True,
        )
        existing = head_object(oci, namespace, bucket, name)
        if existing is None:
            raise DeploymentError("uploaded object could not be read back")
        uploaded = True
    remote_size = object_size(existing)
    if remote_size is None or remote_size != image.stat().st_size:
        raise DeploymentError("Object Storage object has a different size after upload")
    if object_sha256(existing) != image_sha256:
        raise DeploymentError(
            "Object Storage object lacks matching release SHA-256 metadata after upload"
        )
    state.resource(
        "object",
        namespace=namespace,
        bucket=bucket,
        name=name,
        uploaded=uploaded
        or bool(state.data.get("resources", {}).get("object", {}).get("uploaded")),
        size=image.stat().st_size,
        sha256=image_sha256,
        etag=existing.get("etag") if isinstance(existing, dict) else None,
    )
    return name


def lifecycle_value(resource, description):
    value = resource.get("lifecycle-state")
    if not isinstance(value, str):
        raise DeploymentError(f"{description} lacks a lifecycle state")
    return value


def wait_for_resource(getter, active_states, success_state, timeout, description):
    started = time.monotonic()
    delay = 10
    progress_interval = 60
    last_state = None
    last_reported = started
    while True:
        now = time.monotonic()
        resource = getter()
        state = lifecycle_value(resource, description)
        elapsed = int(now - started)
        if state != last_state or now - last_reported >= progress_interval:
            print(f"{description.upper()}  {state} ({elapsed}s)")
            last_state = state
            last_reported = now
        if state == success_state:
            return resource
        if state not in active_states:
            raise DeploymentError(f"{description} entered unexpected state {state}")
        if now - started >= timeout:
            raise DeploymentError(f"timed out waiting for {description} to reach {success_state}")
        time.sleep(delay)
        delay = min(30, delay + 5)


def get_image(oci, image_id):
    return response_data(
        oci.run(["compute", "image", "get", "--image-id", image_id]),
        dict,
        "image response",
    )


def create_image(args, oci, state, namespace, bucket, object_name, tags):
    recorded = state.data.get("resources", {}).get("image", {})
    image_id = recorded.get("id") if args.resume else None
    if args.resume and not image_id:
        images = response_data(
            oci.run(
                [
                    "compute",
                    "image",
                    "list",
                    "--compartment-id",
                    args.compartment_id,
                    "--all",
                ]
            ),
            list,
            "image list response",
        )
        recovered = exactly_one_tagged(images, tags, "custom image")
        if recovered is not None:
            image_id = recovered.get("id")
            if not isinstance(image_id, str):
                raise DeploymentError("recovered custom image lacks an OCID")
            state.resource("image", id=image_id, created=True, recovered=True)
    if image_id:
        image = get_image(oci, image_id)
    else:
        print(f"IMPORT  Importing {bucket}/{object_name} as {args.image_name}")
        image = response_data(
            oci.run(
                [
                    "compute",
                    "image",
                    "import",
                    "from-object",
                    "--namespace",
                    namespace,
                    "--bucket-name",
                    bucket,
                    "--name",
                    object_name,
                    "--compartment-id",
                    args.compartment_id,
                    "--display-name",
                    args.image_name,
                    "--source-image-type",
                    "QCOW2",
                    "--operating-system",
                    "Linux",
                    "--operating-system-version",
                    "Arch Linux ARM",
                    "--launch-mode",
                    "PARAVIRTUALIZED",
                    "--freeform-tags",
                    json.dumps(tags, separators=(",", ":")),
                    "--opc-client-request-id",
                    client_request_id(state, "import-image"),
                ]
            ),
            dict,
            "image import response",
        )
        image_id = image.get("id")
        if not isinstance(image_id, str):
            raise DeploymentError("image import response lacks an OCID")
        state.resource(
            "image",
            id=image_id,
            created=True,
            lifecycle_state=image.get("lifecycle-state"),
        )
    image = wait_for_resource(
        lambda: get_image(oci, image_id),
        ACTIVE_IMAGE_STATES,
        "AVAILABLE",
        args.image_timeout,
        "image",
    )
    state.resource("image", id=image_id, created=True, lifecycle_state="AVAILABLE")
    return image_id, image


def schema_default(entry):
    if not isinstance(entry, dict):
        return None
    return entry.get("default-value", entry.get("defaultValue"))


def schema_data(resource):
    data = resource.get("schema-data", resource.get("schemaData", {}))
    if not isinstance(data, dict):
        raise DeploymentError("image capability schema data is invalid")
    return data


def validate_capabilities(oci, image_id):
    globals_list = response_data(
        oci.run(["compute", "global-image-capability-schema", "list"]),
        list,
        "global capability schema response",
    )
    if len(globals_list) != 1:
        raise DeploymentError("OCI did not return exactly one global image capability schema")
    global_schema = globals_list[0]
    schema_id = global_schema.get("id")
    version = global_schema.get("current-version-name")
    if not isinstance(schema_id, str) or not isinstance(version, str):
        raise DeploymentError("global capability schema lacks an ID or current version")
    global_version = response_data(
        oci.run(
            [
                "compute",
                "global-image-capability-schema-version",
                "get",
                "--global-image-capability-schema-id",
                schema_id,
                "--global-image-capability-schema-version-name",
                version,
            ]
        ),
        dict,
        "global capability schema version response",
    )
    effective = {
        name: schema_default(entry) for name, entry in schema_data(global_version).items()
    }
    image_schemas = response_data(
        oci.run(
            [
                "compute",
                "image-capability-schema",
                "list",
                "--image-id",
                image_id,
                "--all",
            ],
            empty_data=[],
        ),
        list,
        "image capability schema response",
    )
    if len(image_schemas) > 1:
        raise DeploymentError("image has multiple capability schemas")
    if image_schemas:
        image_schema_id = image_schemas[0].get("id")
        if not isinstance(image_schema_id, str):
            raise DeploymentError("image capability schema lacks an OCID")
        image_schema = response_data(
            oci.run(
                [
                    "compute",
                    "image-capability-schema",
                    "get",
                    "--image-capability-schema-id",
                    image_schema_id,
                ]
            ),
            dict,
            "image capability schema response",
        )
        for name, entry in schema_data(image_schema).items():
            value = schema_default(entry)
            if value is not None:
                effective[name] = value
    mismatches = {
        name: effective.get(name)
        for name, expected in REQUIRED_CAPABILITIES.items()
        if effective.get(name) != expected
    }
    if mismatches:
        rendered = ", ".join(f"{name}={value!r}" for name, value in mismatches.items())
        raise DeploymentError(f"image has incompatible effective capabilities: {rendered}")
    return {name: effective[name] for name in REQUIRED_CAPABILITIES}


def ensure_shape_compatibility(oci, image_id, shape, state=None):
    entries = response_data(
        oci.run(
            [
                "compute",
                "image-shape-compatibility-entry",
                "list",
                "--image-id",
                image_id,
                "--all",
            ]
        ),
        list,
        "image shape compatibility response",
    )
    compatible = any(
        entry.get("shape") == shape for entry in entries if isinstance(entry, dict)
    )
    if not compatible:
        print(f"CAPABILITIES  Adding {shape} compatibility")
        command = [
            "compute",
            "image-shape-compatibility-entry",
            "add",
            "--image-id",
            image_id,
            "--shape-name",
            shape,
            "--force",
        ]
        if state is not None:
            command.extend(
                [
                    "--opc-client-request-id",
                    client_request_id(state, "add-shape-compatibility"),
                ]
            )
        oci.run(command)
        verified = response_data(
            oci.run(
                [
                    "compute",
                    "image-shape-compatibility-entry",
                    "list",
                    "--image-id",
                    image_id,
                    "--all",
                ]
            ),
            list,
            "image shape compatibility response",
        )
        if not any(
            entry.get("shape") == shape
            for entry in verified
            if isinstance(entry, dict)
        ):
            raise DeploymentError(f"OCI did not retain {shape} image compatibility")


def get_instance(oci, instance_id):
    return response_data(
        oci.run(["compute", "instance", "get", "--instance-id", instance_id]),
        dict,
        "instance response",
    )


def launch_instance(args, oci, state, image_id, tags):
    recorded = state.data.get("resources", {}).get("instance", {})
    instance_id = recorded.get("id") if args.resume else None
    if args.resume and not instance_id:
        instances = response_data(
            oci.run(
                [
                    "compute",
                    "instance",
                    "list",
                    "--compartment-id",
                    args.compartment_id,
                    "--all",
                ]
            ),
            list,
            "instance list response",
        )
        recovered = exactly_one_tagged(instances, tags, "instance")
        if recovered is not None:
            instance_id = recovered.get("id")
            if not isinstance(instance_id, str):
                raise DeploymentError("recovered instance lacks an OCID")
            state.resource("instance", id=instance_id, created=True, recovered=True)
    if instance_id:
        instance = get_instance(oci, instance_id)
    else:
        print(f"LAUNCH  Launching {args.instance_name} on {args.shape}")
        instance = response_data(
            oci.run(
                [
                    "compute",
                    "instance",
                    "launch",
                    "--availability-domain",
                    args.availability_domain,
                    "--compartment-id",
                    args.compartment_id,
                    "--subnet-id",
                    args.subnet_id,
                    "--image-id",
                    image_id,
                    "--shape",
                    args.shape,
                    "--shape-config",
                    json.dumps(
                        {"ocpus": args.ocpus, "memoryInGBs": args.memory_gbs},
                        separators=(",", ":"),
                    ),
                    "--boot-volume-size-in-gbs",
                    str(args.boot_volume_gbs),
                    "--ssh-authorized-keys-file",
                    str(args.ssh_public_key),
                    "--assign-public-ip",
                    str(args.assign_public_ip).lower(),
                    "--display-name",
                    args.instance_name,
                    "--freeform-tags",
                    json.dumps(tags, separators=(",", ":")),
                    "--opc-client-request-id",
                    client_request_id(state, "launch-instance"),
                ]
            ),
            dict,
            "instance launch response",
        )
        instance_id = instance.get("id")
        if not isinstance(instance_id, str):
            raise DeploymentError("instance launch response lacks an OCID")
        state.resource(
            "instance",
            id=instance_id,
            created=True,
            lifecycle_state=instance.get("lifecycle-state"),
        )
    instance = wait_for_resource(
        lambda: get_instance(oci, instance_id),
        ACTIVE_INSTANCE_STATES,
        "RUNNING",
        args.instance_timeout,
        "instance",
    )
    state.resource("instance", id=instance_id, created=True, lifecycle_state="RUNNING")
    return instance_id, instance


def instance_addresses(oci, instance_id):
    vnics = response_data(
        oci.run(
            ["compute", "instance", "list-vnics", "--instance-id", instance_id, "--all"]
        ),
        list,
        "instance VNIC response",
    )
    if not vnics or not isinstance(vnics[0], dict):
        raise DeploymentError("instance has no readable primary VNIC")
    vnic = vnics[0]
    return {
        "id": vnic.get("id"),
        "private_ip": vnic.get("private-ip"),
        "public_ip": vnic.get("public-ip"),
    }


def verify_ssh(args, addresses, state):
    if not args.verify_ssh:
        return
    address = addresses.get("public_ip") or addresses.get("private_ip")
    if not address:
        raise DeploymentError("instance has no IP address for SSH verification")
    if not args.ssh_private_key.is_file():
        raise DeploymentError(
            f"managed SSH private key is not readable: {args.ssh_private_key}"
        )
    known_hosts = state.path.with_suffix(state.path.suffix + ".known_hosts")
    remote = " && ".join(
        [
            "cloud-init status --wait --long",
            "sudo -n true",
            "test $(sudo passwd -S root | awk '{print $2}') = L",
            "test $(sudo passwd -S alarm | awk '{print $2}') = L",
            "systemctl is-active systemd-networkd.service systemd-resolved.service "
            "sshd.service nftables.service sshguard.service",
            "test -e /var/lib/oci-root-grown",
            "findmnt -no SOURCE,FSTYPE,SIZE /",
        ]
    )
    command = [
        "ssh",
        "-i",
        str(args.ssh_private_key),
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={args.ssh_connect_timeout}",
        f"alarm@{address}",
        remote,
    ]
    deadline = time.monotonic() + args.ssh_timeout
    print(f"SSH-CHECK  Waiting for alarm@{address}")
    while True:
        completed = subprocess.run(command, check=False)
        if completed.returncode == 0:
            state.resource("ssh_verification", passed=True, address=address)
            return
        if completed.returncode != 255:
            raise DeploymentError(
                f"SSH connected but guest verification failed with status {completed.returncode}"
            )
        if time.monotonic() >= deadline:
            raise DeploymentError("timed out waiting for successful SSH verification")
        time.sleep(15)


def cleanup_object(args, oci, state, namespace, bucket, object_name):
    if not (args.cleanup_object or getattr(args, "cleanup_bucket", False)):
        return
    record = state.data.get("resources", {}).get("object", {})
    if record.get("deleted"):
        return
    if not record.get("uploaded"):
        raise DeploymentError("refusing to delete an object not uploaded by this deployment")
    image = state.data.get("resources", {}).get("image", {})
    instance = state.data.get("resources", {}).get("instance", {})
    if image.get("lifecycle_state") != "AVAILABLE" or instance.get("lifecycle_state") != "RUNNING":
        raise DeploymentError("refusing object cleanup before image and instance are ready")
    print(f"CLEANUP  Deleting temporary object {bucket}/{object_name}")
    oci.run(
        [
            "os",
            "object",
            "delete",
            "--namespace",
            namespace,
            "--bucket-name",
            bucket,
            "--name",
            object_name,
            "--force",
            "--opc-client-request-id",
            client_request_id(state, "delete-object"),
        ]
    )
    if head_object(oci, namespace, bucket, object_name) is not None:
        raise DeploymentError("temporary object still exists after deletion")
    state.resource("object", deleted=True)


def bucket_object_names(oci, namespace, bucket):
    response = response_data(
        oci.run(
            [
                "os",
                "object",
                "list",
                "--namespace",
                namespace,
                "--bucket-name",
                bucket,
                "--all",
            ]
        ),
        dict,
        "object list response",
    )
    objects = response.get("objects", [])
    if not isinstance(objects, list):
        raise DeploymentError("object list response has invalid objects")
    names = []
    for item in objects:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise DeploymentError("object list response has an invalid object")
        names.append(item["name"])
    return names


def cleanup_bucket(args, oci, state, namespace, bucket):
    if not args.cleanup_bucket:
        return
    image = state.data.get("resources", {}).get("image", {})
    instance = state.data.get("resources", {}).get("instance", {})
    if (
        image.get("lifecycle_state") != "AVAILABLE"
        or instance.get("lifecycle_state") != "RUNNING"
    ):
        raise DeploymentError(
            "refusing bucket cleanup before image and instance are ready"
        )
    objects = bucket_object_names(oci, namespace, bucket)
    if objects:
        raise DeploymentError(
            f"refusing to delete non-empty bucket {bucket}: {', '.join(objects)}"
        )
    print(f"CLEANUP  Deleting Object Storage bucket {bucket}")
    oci.run(
        [
            "os",
            "bucket",
            "delete",
            "--namespace",
            namespace,
            "--name",
            bucket,
            "--force",
            "--opc-client-request-id",
            client_request_id(state, "delete-bucket"),
        ]
    )
    if get_bucket(oci, namespace, bucket) is not None:
        raise DeploymentError("bucket still exists after deletion")
    state.resource("bucket", deleted=True)


def print_dry_run(args, image, image_sha256, namespace):
    bucket = args.bucket or args.create_bucket
    operations = []
    if args.create_bucket:
        if getattr(args, "allow_existing_create_bucket", False):
            operations.append(f"use or create private Standard bucket {bucket}")
        else:
            operations.append(f"create private Standard bucket {bucket}")
    operations.extend(
        [
            f"upload {image.name} ({image_sha256}) to {namespace}/{bucket}",
            f"import custom image {args.image_name} as QCOW2/PARAVIRTUALIZED",
            f"verify UEFI/paravirtualized capabilities and {args.shape} compatibility",
            f"launch {args.instance_name}: {args.ocpus} OCPU, "
            f"{args.memory_gbs:g} GB RAM, {args.boot_volume_gbs} GB boot volume",
            "assign a public IP" if args.assign_public_ip else "launch without a public IP",
        ]
    )
    if args.cleanup_object or args.cleanup_bucket:
        operations.append("delete the uploaded object after successful launch")
    if args.cleanup_bucket:
        operations.append("delete the bucket after successful launch if it is empty")
    print("DRY-RUN  Read-only validation passed. Planned mutations:")
    for number, operation in enumerate(operations, 1):
        print(f"  {number}. {operation}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Without placement overrides, the tool discovers accessible subnets, "
            "uses the selected subnet's compartment, and finds an A1 availability "
            "domain. "
            "Without a bucket option, BUCKET_NAME selects a bucket to use or create; "
            "if unset, the tool asks before using its default name."
        ),
    )
    parser.add_argument(
        "--compartment-id",
        type=lambda value: validate_ocid(value, "compartment"),
        help="override the selected subnet's compartment (or COMPARTMENT_ID)",
    )
    parser.add_argument(
        "--subnet-id",
        type=lambda value: validate_ocid(value, "subnet"),
        help="use this subnet instead of API discovery (or SUBNET_ID)",
    )
    parser.add_argument(
        "--availability-domain",
        help=(
            "use this domain instead of A1-capable API discovery "
            "(or AVAILABILITY_DOMAIN)"
        ),
    )
    parser.add_argument(
        "--ssh-key",
        type=lambda value: Path(value).expanduser().resolve(),
        default=DEFAULT_SSH_KEY,
        help=(
            "private-key path for the dedicated instance SSH key pair; "
            "the pair is generated when absent (default: %(default)s)"
        ),
    )
    bucket = parser.add_mutually_exclusive_group()
    bucket.add_argument("--bucket", help="use an existing private Standard bucket")
    bucket.add_argument(
        "--create-bucket", help="create a private Standard bucket with this name"
    )
    parser.add_argument(
        "--object-compartment-id",
        type=lambda value: validate_ocid(value, "object compartment"),
    )
    parser.add_argument("--profile", default="DEFAULT")
    parser.add_argument("--config-file", type=lambda v: Path(v).expanduser().resolve())
    parser.add_argument("--region")
    parser.add_argument("--shape", default=DEFAULT_SHAPE, choices=[DEFAULT_SHAPE])
    parser.add_argument("--ocpus", type=positive_float, default=1.0)
    parser.add_argument("--memory-gbs", type=positive_float, default=6.0)
    parser.add_argument("--boot-volume-gbs", type=positive_int, default=50)
    public_ip = parser.add_mutually_exclusive_group()
    public_ip.add_argument("--assign-public-ip", dest="assign_public_ip", action="store_true")
    public_ip.add_argument("--no-public-ip", dest="assign_public_ip", action="store_false")
    parser.set_defaults(assign_public_ip=False)
    parser.add_argument("--image-name", default="Arch-Linux-ARM-OCI")
    parser.add_argument("--instance-name", default="archlinuxarm-a1")
    parser.add_argument(
        "--download-dir",
        type=lambda value: Path(value).expanduser().resolve(),
        default=Path("archlinuxarm-oci-download").resolve(),
    )
    parser.add_argument(
        "--state-file",
        type=lambda value: Path(value).expanduser().resolve(),
        default=Path(".deploy-oci-state.json").resolve(),
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reuse-download", action="store_true")
    parser.add_argument("--reuse-object", action="store_true")
    parser.add_argument("--cleanup-object", action="store_true")
    bucket_cleanup = parser.add_mutually_exclusive_group()
    bucket_cleanup.add_argument(
        "--cleanup-bucket",
        dest="cleanup_bucket",
        action="store_true",
        help="delete the selected bucket after a successful deployment if it is empty",
    )
    bucket_cleanup.add_argument(
        "--keep-bucket",
        dest="cleanup_bucket",
        action="store_false",
        help="keep the selected bucket after deployment",
    )
    parser.set_defaults(cleanup_bucket=None)
    parser.add_argument("--verify-ssh", action="store_true")
    parser.add_argument("--image-timeout", type=positive_int, default=7200)
    parser.add_argument("--instance-timeout", type=positive_int, default=1800)
    parser.add_argument("--ssh-timeout", type=positive_int, default=600)
    parser.add_argument("--ssh-connect-timeout", type=positive_int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.boot_volume_gbs < 50:
        parser.error("--boot-volume-gbs must be at least 50")
    if args.resume and args.dry_run:
        parser.error("--resume cannot be combined with --dry-run")
    return args


def immutable_inputs(args, fingerprint):
    return {
        "profile": args.profile,
        "config_file": str(args.config_file) if args.config_file else None,
        "region": args.region,
        "compartment_id": args.compartment_id,
        "object_compartment_id": args.object_compartment_id,
        "subnet_id": args.subnet_id,
        "availability_domain": args.availability_domain,
        "bucket": args.bucket,
        "create_bucket": args.create_bucket,
        "image_name": args.image_name,
        "instance_name": args.instance_name,
        "shape": args.shape,
        "ocpus": args.ocpus,
        "memory_gbs": args.memory_gbs,
        "boot_volume_gbs": args.boot_volume_gbs,
        "assign_public_ip": args.assign_public_ip,
        "cleanup_bucket": args.cleanup_bucket,
        "ssh_public_key_fingerprint": fingerprint,
    }


def validate_resume(state, inputs, image_sha256):
    if not state.loaded:
        return
    saved_inputs = state.data.get("inputs")
    if isinstance(saved_inputs, dict):
        saved_inputs = {**saved_inputs}
        if "cleanup_bucket" not in saved_inputs:
            saved_inputs["cleanup_bucket"] = inputs.get("cleanup_bucket", False)
    if saved_inputs != inputs:
        raise DeploymentError("resume arguments do not match the recorded deployment inputs")
    release = state.data.get("release")
    if not isinstance(release, dict) or release.get("sha256") != image_sha256:
        raise DeploymentError("verified release does not match the recorded deployment")


def install_signal_handlers(state):
    def stop(signum, _frame):
        error = DeploymentError(f"interrupted by signal {signum}")
        state.record_failure(error)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)


def deploy(args):
    validate_state_file_selection(args)
    apply_resume_defaults(args)
    validate_local_tools()
    oci = OCIRunner(args.profile, args.config_file, args.region, args.verbose)
    discovered = resolve_deployment_inputs(args, oci)
    resolve_bucket_selection(args)
    resolve_bucket_cleanup(args)
    print("VALIDATE  Checking local tools, OCI access, network, and A1 shape")
    namespace, subnet, shape, fingerprint = validate_prerequisites(
        args, oci, discovered
    )
    metadata, image, image_sha256 = obtain_release(args)
    if args.dry_run:
        print_dry_run(args, image, image_sha256, namespace)
        return 0

    state = StateFile(args.state_file, args.resume)
    install_signal_handlers(state)
    inputs = immutable_inputs(args, fingerprint)
    validate_resume(state, inputs, image_sha256)
    state.update(
        phase="validated",
        profile=args.profile,
        region=args.region,
        inputs=inputs,
        release={
            "metadata": metadata,
            "image": str(image),
            "sha256": image_sha256,
        },
        validation={"subnet": subnet, "shape": shape},
    )
    tags = deployment_tags(state, image_sha256)
    try:
        bucket = ensure_bucket(args, oci, state, namespace, tags)
        state.update(phase="bucket-ready")
        object_name = ensure_object(
            args, oci, state, namespace, bucket, image, image_sha256
        )
        state.update(phase="object-ready")
        image_id, _image = create_image(
            args, oci, state, namespace, bucket, object_name, tags
        )
        capabilities = validate_capabilities(oci, image_id)
        ensure_shape_compatibility(oci, image_id, args.shape, state)
        state.resource("image", capabilities=capabilities, compatible_shape=args.shape)
        state.update(phase="image-ready")
        instance_id, _instance = launch_instance(args, oci, state, image_id, tags)
        addresses = instance_addresses(oci, instance_id)
        state.resource("instance", addresses=addresses)
        state.update(phase="instance-running")
        verify_ssh(args, addresses, state)
        cleanup_object(args, oci, state, namespace, bucket, object_name)
        cleanup_bucket(args, oci, state, namespace, bucket)
        state.data.pop("failure", None)
        state.update(phase="complete", completed_at=timestamp())
    except BaseException as error:
        state.record_failure(error)
        raise

    print("DONE  OCI instance is running")
    print(f"Instance OCID: {instance_id}")
    print(f"Private IP: {addresses.get('private_ip') or 'unavailable'}")
    print(f"Public IP: {addresses.get('public_ip') or 'unavailable'}")
    address = addresses.get("public_ip") or addresses.get("private_ip")
    if address:
        key_option = (
            f" -i {shlex.quote(str(args.ssh_private_key))}"
            if args.ssh_private_key
            else ""
        )
        print(f"Connect: ssh{key_option} {shlex.quote('alarm@' + address)}")
    print(f"State: {state.path}")
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        return deploy(args)
    except KeyboardInterrupt:
        print("ERROR: interrupted; deployment state was preserved", file=sys.stderr)
        return 130
    except (DeploymentError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
