#!/usr/bin/env python3
"""Build a hardened Arch Linux ARM OCI image without host root privileges."""

from __future__ import annotations

import argparse
import atexit
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import pty
import re
import selectors
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time


PROJECT = Path(__file__).resolve().parent
DEFAULT_ROOTFS_URL = "https://ca.us.mirror.archlinuxarm.org/os/ArchLinuxARM-aarch64-latest.tar.gz"
ROOTFS_SIGNING_FINGERPRINT = "68B3537F39A313B3E574D06777193F152BDBE6A6"
EFI_SYSTEM_GUID = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LINUX_FILESYSTEM_GUID = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
BUILD_SUCCESS = b"OCI_IMAGE_BUILD_SUCCESS"
SMOKE_SUCCESS = b"OCI_IMAGE_UEFI_SMOKE_SUCCESS"
REQUIRED_COMMANDS = ("curl", "gpg", "guestfish", "qemu-img", "qemu-system-aarch64")
FACTORY_USER = "alarm"
BUILD_MODES = ("development", "factory")
SMOKE_PUBLIC_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEYdU6aY7SBVn3fnVPoknHLghaHffieYYPuJ0a1PUKiT "
    "archlinuxarm-oci-smoke"
)
ANSI_RESET = "\033[0m"
ANSI_BOLD_BLUE = "\033[1;34m"
ANSI_DIM_CYAN = "\033[2;36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"


def command(argv: list[object]) -> str:
    return shlex.join([str(item) for item in argv])


def colorize(text: str, style: str, *, stream: object | None = None) -> str:
    """Apply terminal color without polluting redirected output or logs."""
    output = sys.stdout if stream is None else stream
    is_tty = getattr(output, "isatty", lambda: False)()
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb" or not is_tty:
        return text
    return f"{style}{text}{ANSI_RESET}"


def print_command(argv: list[object]) -> None:
    print(colorize("+ " + command(argv), ANSI_DIM_CYAN), flush=True)


def print_stage(message: str) -> None:
    print(colorize(f"==> {message}", ANSI_BOLD_BLUE))


def run(argv: list[object], *, capture: bool = False, input_text: str | None = None,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    args = [str(item) for item in argv]
    print_command(args)
    return subprocess.run(
        args,
        check=True,
        text=True,
        input=input_text,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env=env,
    )


def validate_username(value: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value):
        raise ValueError(
            "username must be 1-32 characters, begin with a lowercase letter or "
            "underscore, and contain only lowercase letters, digits, underscores, or hyphens"
        )
    if value in {"root", "alarm"}:
        raise ValueError(f"username {value!r} is reserved")
    return value


def prompt_username(value: str | None) -> str:
    if value is not None:
        try:
            return validate_username(value)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    while True:
        try:
            return validate_username(input("Admin username: ").strip())
        except ValueError as exc:
            print(exc)


def prompt_password(account: str) -> str:
    while True:
        password = getpass.getpass(f"New {account} password: ")
        if not password or any(char in password for char in "\r\n\0"):
            print("password must be nonempty and cannot contain newline or NUL characters")
            continue
        confirmation = getpass.getpass(f"Retype {account} password: ")
        if password == confirmation:
            return password
        print("passwords do not match; try again")


def validate_hostname(value: str) -> str:
    label = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
    if not value or len(value) > 253 or any(label.fullmatch(part) is None for part in value.split(".")):
        raise SystemExit("invalid hostname: use lowercase letters, digits, hyphens, and dots")
    return value


def render_template(name: str, **values: str) -> str:
    text = (PROJECT / "templates" / name).read_text()
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    leftovers = re.findall(r"{{[A-Z0-9_]+}}", text)
    if leftovers:
        raise RuntimeError(f"unrendered placeholders in {name}: {leftovers}")
    return text


def guestfish_quote(value: Path | str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def safe_rmtree(path: Path) -> None:
    resolved = path.resolve()
    parent = resolved.parent
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if parent != temporary_root or not resolved.name.startswith("oci-archarm."):
        raise RuntimeError(f"refusing unsafe cleanup path: {resolved}")
    shutil.rmtree(resolved)


class ConsoleRunner:
    """Run QEMU on a PTY, mirror serial I/O, and watch for explicit sentinels."""

    PASSWORD_PROMPTS = (b"New password:", b"Retype new password:") * 2
    FATAL_MARKERS = (
        b"OCI_IMAGE_BUILD_FAILED",
        b"OCI_IMAGE_UEFI_SMOKE_FAILED",
        b"Kernel panic - not syncing",
        b"You are in emergency mode",
    )

    @staticmethod
    def run(argv: list[object], *, log: Path, success: bytes, timeout: int,
            passwords: tuple[str, str] | None = None) -> None:
        args = [str(item) for item in argv]
        print_command(args)
        master, slave = pty.openpty()
        process = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        os.set_blocking(master, False)
        selector = selectors.DefaultSelector()
        selector.register(master, selectors.EVENT_READ, "qemu")
        deadline = time.monotonic() + timeout
        seen_success = False
        seen_fatal: bytes | None = None
        scan = b""
        prompt_index = 0
        try:
            with log.open("wb") as serial_log:
                while True:
                    if time.monotonic() >= deadline:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        raise TimeoutError(f"QEMU timed out after {timeout} seconds; serial log: {log}")

                    for key, _ in selector.select(timeout=0.25):
                        try:
                            data = os.read(master, 65536)
                        except BlockingIOError:
                            continue
                        except OSError:
                            data = b""
                        if not data:
                            try:
                                selector.unregister(master)
                            except Exception:
                                pass
                            continue
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                        serial_log.write(data)
                        serial_log.flush()
                        scan = (scan + data)[-131072:]
                        if success in scan:
                            seen_success = True
                        for marker in ConsoleRunner.FATAL_MARKERS:
                            if marker in scan:
                                seen_fatal = marker

                        if passwords is not None and prompt_index < len(ConsoleRunner.PASSWORD_PROMPTS):
                            expected = ConsoleRunner.PASSWORD_PROMPTS[prompt_index]
                            location = scan.find(expected)
                            if location >= 0:
                                password = passwords[prompt_index // 2]
                                os.write(master, password.encode() + b"\r")
                                prompt_index += 1
                                scan = scan[location + len(expected):]

                    status = process.poll()
                    if status is not None:
                        # Drain the PTY once after QEMU exits.
                        try:
                            tail = os.read(master, 65536)
                        except OSError:
                            tail = b""
                        if tail:
                            sys.stdout.buffer.write(tail)
                            sys.stdout.buffer.flush()
                            serial_log.write(tail)
                            scan += tail
                            seen_success = seen_success or success in scan
                        break
        finally:
            selector.close()
            os.close(master)
            if process.poll() is None:
                process.kill()
                process.wait()

        if passwords is not None and prompt_index != len(ConsoleRunner.PASSWORD_PROMPTS):
            raise RuntimeError(f"saw only {prompt_index}/4 expected passwd prompts; serial log: {log}")
        if process.returncode != 0:
            raise RuntimeError(f"QEMU exited with status {process.returncode}; serial log: {log}")
        if seen_fatal is not None:
            raise RuntimeError(f"guest emitted fatal marker {seen_fatal.decode(errors='replace')}; serial log: {log}")
        if not seen_success:
            raise RuntimeError(f"guest shut down without success marker; serial log: {log}")


class Builder:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.work: Path | None = None
        self.raw: Path | None = None
        self.rootfs: Path | None = None
        self.rootfs_sig: Path | None = None
        self.kernel: Path | None = None
        self.initramfs: Path | None = None
        self.state_file: Path | None = None
        self.explicit_work = False
        self.output = Path(args.output).resolve()
        self.admin_user: str | None = None
        self.passwords: tuple[str, str] | None = None
        self.build_mode = "factory" if args.factory_image else "development"
        self.selected_accel: str | None = None
        self.build_accel: str | None = None
        self.smoke_accel: str | None = None
        self.hostname = validate_hostname(args.hostname)

    def require_unprivileged(self) -> None:
        if os.geteuid() == 0:
            raise SystemExit("refusing to run as root; invoke ./build.py as your normal user")

    def check_environment(self, *, required: tuple[str, ...] = REQUIRED_COMMANDS,
                          require_firmware: bool = True, require_accel: bool = True) -> None:
        self.require_unprivileged()
        missing = [name for name in required if shutil.which(name) is None]
        if missing:
            raise SystemExit("missing host commands: " + ", ".join(missing) + "\nRun ./install-deps.sh first.")
        if require_firmware:
            self.find_firmware()
        if require_accel:
            self.select_acceleration()
        print(colorize("Required rootless build dependencies are available.", ANSI_GREEN))

    @staticmethod
    def probe_kvm() -> bool:
        if platform.machine().lower() not in {"aarch64", "arm64"}:
            return False
        device = Path("/dev/kvm")
        if not device.exists() or not os.access(device, os.R_OK | os.W_OK):
            return False
        try:
            result = subprocess.run(
                [
                    "qemu-system-aarch64", "-machine", "virt,accel=kvm", "-cpu", "host",
                    "-nodefaults", "-display", "none", "-S", "-qmp", "stdio",
                ],
                check=False,
                text=True,
                input='{"execute":"qmp_capabilities"}\n{"execute":"quit"}\n',
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and '"return"' in result.stdout

    def select_acceleration(self) -> str:
        if self.selected_accel is not None:
            return self.selected_accel
        requested = self.args.accel
        if requested == "tcg":
            selected = "tcg"
        else:
            usable = self.probe_kvm()
            if requested == "kvm" and not usable:
                raise SystemExit(
                    "KVM requested but /dev/kvm is unavailable, inaccessible, or unusable."
                )
            selected = "kvm" if usable else "tcg"
        self.selected_accel = selected
        print(f"QEMU acceleration: {selected}")
        return selected

    def qemu_machine_args(self) -> list[object]:
        accel = self.select_acceleration()
        cpu = "host" if accel == "kvm" else "max"
        return ["-machine", f"virt,accel={accel}", "-cpu", cpu]

    def bind_workspace(self, work: Path) -> None:
        self.work = work
        self.raw = self.work / "archlinuxarm-oci.raw"
        self.rootfs = self.work / "ArchLinuxARM-aarch64-latest.tar.gz"
        self.rootfs_sig = self.work / "ArchLinuxARM-aarch64-latest.tar.gz.sig"
        self.kernel = self.work / "Image"
        self.initramfs = self.work / "initramfs-linux.img"
        self.state_file = self.work / "build-state.json"

    def start_workspace(self, *, resume: bool = False) -> None:
        if self.args.work_dir:
            work = Path(self.args.work_dir).expanduser().resolve()
            self.explicit_work = True
            if resume:
                if not work.is_dir():
                    raise SystemExit(f"workspace does not exist: {work}")
            else:
                work.mkdir(parents=True, exist_ok=True)
                if any(work.iterdir()):
                    raise SystemExit(
                        f"new build workspace is not empty: {work}; use a stage-only mode to resume it"
                    )
        else:
            if resume:
                raise SystemExit("--work-dir is required with --smoke-test-only or --convert-only")
            work = Path(tempfile.mkdtemp(prefix="oci-archarm."))
        self.bind_workspace(work)
        atexit.register(self.cleanup)

    def cleanup(self) -> None:
        if (
            self.work is not None
            and self.work.exists()
            and not self.explicit_work
            and not self.args.keep_work
            and not self.args.build_only
        ):
            safe_rmtree(self.work)

    def raw_identity(self) -> dict[str, int]:
        assert self.raw
        stat = self.raw.stat()
        return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    def load_state(self) -> dict[str, object] | None:
        assert self.state_file
        if not self.state_file.is_file():
            return None
        try:
            state = json.loads(self.state_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"invalid workspace state file: {self.state_file}") from exc
        if not isinstance(state, dict) or state.get("version") != 2:
            raise RuntimeError(f"unsupported workspace state file: {self.state_file}")
        mode = state.get("build_mode")
        image_user = state.get("image_user")
        if mode not in BUILD_MODES or not isinstance(image_user, str):
            raise RuntimeError(f"invalid workspace state file: {self.state_file}")
        return state

    def write_state(self, *, root_uuid: str, smoke_passed: bool) -> None:
        assert self.state_file and self.admin_user
        state = {
            "version": 2,
            "build_mode": self.build_mode,
            "image_user": self.admin_user,
            "root_uuid": root_uuid,
            "raw": self.raw_identity(),
            "smoke_passed": smoke_passed,
            "build_accel": self.build_accel,
            "smoke_accel": self.smoke_accel,
        }
        temporary = self.state_file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.replace(temporary, self.state_file)

    def adopt_state(self, state: dict[str, object]) -> None:
        mode = state["build_mode"]
        image_user = state["image_user"]
        assert isinstance(mode, str) and isinstance(image_user, str)
        self.build_mode = mode
        self.admin_user = image_user
        build_accel = state.get("build_accel")
        smoke_accel = state.get("smoke_accel")
        self.build_accel = build_accel if isinstance(build_accel, str) else None
        self.smoke_accel = smoke_accel if isinstance(smoke_accel, str) else None

    def require_matching_state(self, *, require_smoke: bool) -> dict[str, object]:
        state = self.load_state()
        if state is None:
            raise RuntimeError(
                f"workspace has no build-state.json; run --smoke-test-only first: {self.work}"
            )
        if state.get("raw") != self.raw_identity():
            raise RuntimeError("raw disk changed since the workspace state was recorded")
        self.adopt_state(state)
        if require_smoke and state.get("smoke_passed") is not True:
            raise RuntimeError("workspace has no successful smoke test for the current raw disk")
        return state

    def confirm_output(self) -> None:
        if not self.output.exists():
            return
        if self.args.force:
            return
        if not sys.stdin.isatty():
            raise SystemExit(f"output exists: {self.output}; pass --force to replace it")
        answer = input(f"Replace existing {self.output}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            raise SystemExit("cancelled")

    def download_and_verify(self) -> None:
        assert self.work and self.rootfs and self.rootfs_sig
        print_stage("Downloading official Arch Linux ARM rootfs")
        run(["curl", "-fL", "--retry", "3", "-o", self.rootfs, self.args.rootfs_url])
        run(["curl", "-fL", "--retry", "3", "-o", self.rootfs_sig, self.args.rootfs_url + ".sig"])
        gnupg = self.work / "gnupg"
        gnupg.mkdir(mode=0o700)
        env = os.environ.copy()
        env["GNUPGHOME"] = str(gnupg)
        print_stage("Verifying rootfs signature with the pinned full fingerprint")
        run(["gpg", "--keyserver", self.args.keyserver, "--recv-keys", ROOTFS_SIGNING_FINGERPRINT], env=env)
        result = run(["gpg", "--with-colons", "--fingerprint", ROOTFS_SIGNING_FINGERPRINT], capture=True, env=env)
        fingerprints = [line.split(":")[9] for line in result.stdout.splitlines() if line.startswith("fpr:")]
        if ROOTFS_SIGNING_FINGERPRINT not in fingerprints:
            raise RuntimeError(f"unexpected signing key fingerprints: {fingerprints}")
        run(["gpg", "--verify", self.rootfs_sig, self.rootfs], env=env)

    def extract_verified_kernel(self) -> None:
        assert self.rootfs and self.kernel and self.initramfs
        print_stage("Extracting the verified AArch64 kernel/initramfs for direct QEMU boot")
        with tarfile.open(self.rootfs, "r:gz") as archive:
            wanted = {
                "boot/Image": self.kernel,
                "boot/initramfs-linux.img": self.initramfs,
            }
            found: set[str] = set()
            for member in archive:
                name = member.name.lstrip("./")
                if name not in wanted:
                    continue
                if not member.isfile():
                    raise RuntimeError(f"verified rootfs member is not a regular file: /{name}")
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"could not read /{name} from verified rootfs")
                with wanted[name].open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                found.add(name)
            missing = sorted(set(wanted) - found)
            if missing:
                raise RuntimeError("verified rootfs is missing direct-boot files: " + ", ".join(missing))

    def guestfish(self, commands: list[str], *, image: Path | None = None,
                  image_format: str = "raw", read_only: bool = False,
                  capture: bool = False) -> subprocess.CompletedProcess[str]:
        target = image or self.raw
        assert target is not None
        mode = "--ro" if read_only else "--rw"
        script = "\n".join(commands) + "\n"
        env = os.environ.copy()
        env["LIBGUESTFS_BACKEND"] = "direct"
        if self.work is not None:
            cache = self.work / "guestfs-cache"
            cache.mkdir(exist_ok=True)
            env["LIBGUESTFS_CACHEDIR"] = str(cache)
        return run(
            ["guestfish", mode, f"--format={image_format}", "-a", target],
            capture=capture,
            input_text=script,
            env=env,
        )

    def create_and_populate_disk(self) -> tuple[str, str]:
        assert self.raw and self.rootfs
        print_stage(f"Creating rootless {self.args.image_size} GPT disk with libguestfs")
        run(["qemu-img", "create", "-f", "raw", self.raw, self.args.image_size])
        self.guestfish([
            "run",
            "modprobe vfat",
            "part-init /dev/sda gpt",
            "part-add /dev/sda p 2048 1050623",
            "part-add /dev/sda p 1050624 -34",
            f"part-set-gpt-type /dev/sda 1 {EFI_SYSTEM_GUID}",
            f"part-set-gpt-type /dev/sda 2 {LINUX_FILESYSTEM_GUID}",
            "part-set-name /dev/sda 1 EFI",
            "part-set-name /dev/sda 2 root",
            "mkfs vfat /dev/sda1 label:EFI",
            "mkfs ext4 /dev/sda2 label:root",
            "mount /dev/sda2 /",
            "mkdir-p /boot/efi",
            "mount /dev/sda1 /boot/efi",
            f"tar-in {guestfish_quote(self.rootfs)} / compress:gzip xattrs:true acls:true",
            "sync",
            "umount-all",
        ])
        root_uuid = self.read_uuid("/dev/sda2")
        esp_uuid = self.read_uuid("/dev/sda1")
        return root_uuid, esp_uuid

    def read_uuid(self, device: str) -> str:
        result = self.guestfish(["run", f"vfs-uuid {device}"], read_only=True, capture=True)
        values = [line.strip() for line in result.stdout.splitlines() if re.fullmatch(r"[0-9A-Fa-f-]{4,}", line.strip())]
        if not values:
            raise RuntimeError(f"could not read filesystem UUID for {device}: {result.stdout!r}")
        return values[-1]

    @staticmethod
    def write_root_owned_tar(source: Path, destination: Path) -> None:
        def root_owner(info: tarfile.TarInfo) -> tarfile.TarInfo:
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            return info
        with tarfile.open(destination, "w:gz") as archive:
            for child in sorted(source.iterdir()):
                archive.add(child, arcname=child.name, recursive=True, filter=root_owner)

    def create_build_payload(self, root_uuid: str, esp_uuid: str) -> Path:
        assert self.work and self.admin_user
        staging = self.work / "payload"
        builder_dir = staging / "usr/local/lib/archlinuxarm-oci-builder"
        final_root = builder_dir / "final-root"
        shutil.copytree(PROJECT / "overlay", final_root, symlinks=True)
        for script in (PROJECT / "guest").glob("*.sh"):
            shutil.copy2(script, builder_dir / script.name)
        ssh_template = (
            "sshd-security-factory.conf"
            if self.build_mode == "factory"
            else "sshd-security-development.conf"
        )
        final_generated = {
            "etc/fstab": render_template("fstab", ROOT_UUID=root_uuid, ESP_UUID=esp_uuid),
            "boot/grub/grub.cfg": render_template("grub.cfg", ROOT_UUID=root_uuid),
            "etc/ssh/sshd_config.d/10-oci-security.conf": render_template(
                ssh_template, IMAGE_USER=self.admin_user
            ),
            "etc/hostname": self.hostname + "\n",
            "etc/locale.conf": "LANG=en_US.UTF-8\n",
        }
        if self.build_mode == "factory":
            final_generated.update({
                "etc/cloud/cloud.cfg.d/90-oci-alarm.cfg": (
                    PROJECT / "templates/cloud-init-alarm.cfg"
                ).read_text(),
                "etc/sudoers.d/20-alarm-cloud": (
                    PROJECT / "templates/sudoers-alarm"
                ).read_text(),
            })
        for relative, text in final_generated.items():
            path = final_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            if relative.startswith("etc/sudoers.d/"):
                path.chmod(0o440)
        resolv = final_root / "etc/resolv.conf"
        if resolv.exists() or resolv.is_symlink():
            resolv.unlink()
        os.symlink("/run/systemd/resolve/stub-resolv.conf", resolv)

        # Install only the files needed to boot and network the build VM at their
        # final paths. Package-owned configuration stays under final-root until
        # pacman has installed the corresponding packages inside the guest.
        early_files = {
            "etc/fstab": final_generated["etc/fstab"],
            "etc/systemd/system/oci-image-build.service": render_template(
                "oci-image-build.service", BUILD_MODE=self.build_mode, IMAGE_USER=self.admin_user
            ),
        }
        for relative, text in early_files.items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        early_network = staging / "etc/systemd/network/20-oci.network"
        early_network.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT / "overlay/etc/systemd/network/20-oci.network", early_network)
        early_resolv = staging / "etc/resolv.conf"
        early_resolv.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("/run/systemd/resolve/stub-resolv.conf", early_resolv)

        payload = self.work / "payload.tar.gz"
        self.write_root_owned_tar(staging, payload)
        return payload

    def install_build_payload(self, payload: Path) -> None:
        self.guestfish([
            "run",
            "modprobe vfat",
            "mount /dev/sda2 /",
            "mount /dev/sda1 /boot/efi",
            "rm-f /etc/resolv.conf",
            f"tar-in {guestfish_quote(payload)} / compress:gzip xattrs:true acls:true",
            "sync",
            "umount-all",
        ])

    def direct_boot_args(self) -> list[object]:
        assert self.raw and self.kernel and self.initramfs
        return [
            "qemu-system-aarch64", "-name", "archlinuxarm-oci-build", *self.qemu_machine_args(),
            "-smp", str(self.args.cpus), "-m", str(self.args.memory),
            "-display", "none", "-monitor", "none", "-serial", "stdio", "-no-reboot",
            "-kernel", self.kernel,
            "-initrd", self.initramfs,
            "-append", (
                "root=/dev/vda2 rw rootwait console=ttyAMA0,115200n8 "
                "systemd.unit=oci-image-build.service systemd.show_status=yes"
            ),
            "-drive", f"file={self.raw},format=raw,if=none,id=rootdisk,cache=writeback",
            "-device", "virtio-blk-pci,drive=rootdisk,serial=OCIARCHBUILDER",
            "-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0",
            "-object", "rng-random,filename=/dev/urandom,id=rng0", "-device", "virtio-rng-pci,rng=rng0",
            "-rtc", "base=utc",
        ]

    def run_build_vm(self) -> None:
        assert self.work
        print_stage("Configuring the image inside a disposable AArch64 QEMU VM")
        self.build_accel = self.select_acceleration()
        try:
            ConsoleRunner.run(
                self.direct_boot_args(),
                log=self.work / "build-serial.log",
                success=BUILD_SUCCESS,
                timeout=self.args.build_timeout,
                passwords=self.passwords,
            )
        finally:
            self.passwords = None

    def read_guest_file(self, path: str, *, image: Path | None = None,
                        image_format: str = "raw") -> str:
        result = self.guestfish(
            ["run", "mount-ro /dev/sda2 /", f"cat {path}"],
            image=image, image_format=image_format, read_only=True, capture=True,
        )
        return result.stdout

    def guest_path_exists(self, path: str) -> bool:
        result = self.guestfish(
            ["run", "mount-ro /dev/sda2 /", f"exists {path}"],
            read_only=True, capture=True,
        )
        values = [line.strip() for line in result.stdout.splitlines() if line.strip() in {"true", "false"}]
        if not values:
            raise RuntimeError(f"could not inspect guest path: {path}")
        return values[-1] == "true"

    def guest_glob(self, pattern: str) -> list[str]:
        result = self.guestfish(
            ["run", "mount-ro /dev/sda2 /", f"glob echo {pattern}"],
            read_only=True, capture=True,
        )
        return [
            value for line in result.stdout.splitlines()
            if (value := line.strip()).startswith("/") and value != pattern
        ]

    def guest_file_size(self, path: str) -> int:
        result = self.guestfish(
            ["run", "mount-ro /dev/sda2 /", f"filesize {guestfish_quote(path)}"],
            read_only=True, capture=True,
        )
        values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not values or not values[-1].isdigit():
            raise RuntimeError(f"could not inspect guest file size: {path}")
        return int(values[-1])

    def validate_built_image(self, *, require_marker: bool = True) -> None:
        assert self.admin_user
        if require_marker:
            marker = self.read_guest_file("/var/lib/archlinuxarm-oci/build-success").strip()
            if marker != BUILD_SUCCESS.decode():
                raise RuntimeError(f"missing durable build marker: {marker!r}")
        passwd = self.read_guest_file("/etc/passwd")
        interactive: list[str] = []
        for line in passwd.splitlines():
            fields = line.split(":")
            if len(fields) != 7:
                continue
            uid = int(fields[2])
            if (uid == 0 or 1000 <= uid < 65534) and not re.search(r"(?:nologin|false)$", fields[6]):
                interactive.append(fields[0])
        if sorted(interactive) != sorted(["root", self.admin_user]):
            raise RuntimeError(f"unexpected interactive users in completed image: {interactive}")
        ssh = self.read_guest_file("/etc/ssh/sshd_config.d/10-oci-security.conf")
        if self.build_mode == "factory":
            if self.admin_user != FACTORY_USER or re.search(r"^arch:", passwd, re.MULTILINE):
                raise RuntimeError("factory image must preserve alarm and must not contain an arch account")
            required = (
                "AllowUsers alarm", "PasswordAuthentication no", "PubkeyAuthentication yes",
                "KbdInteractiveAuthentication no", "PermitRootLogin no", "PermitEmptyPasswords no",
            )
        else:
            if re.search(r"^alarm:", passwd, re.MULTILINE):
                raise RuntimeError("development image still contains the upstream alarm account")
            required = (
                f"AllowUsers {self.admin_user}", "PasswordAuthentication yes", "PubkeyAuthentication no",
                "KbdInteractiveAuthentication no", "PermitRootLogin no", "PermitEmptyPasswords no",
            )
        for line in required:
            if line not in ssh:
                raise RuntimeError(f"completed image is missing SSH policy: {line}")
        if self.build_mode == "factory":
            shadow = self.read_guest_file("/etc/shadow")
            hashes = {
                fields[0]: fields[1]
                for line in shadow.splitlines()
                if len(fields := line.split(":")) >= 2 and fields[0] in {"root", FACTORY_USER}
            }
            if set(hashes) != {"root", FACTORY_USER} or any(
                not value.startswith(("!", "*")) for value in hashes.values()
            ):
                raise RuntimeError("factory root and alarm passwords must be locked")
            cloud_cfg = self.read_guest_file("/etc/cloud/cloud.cfg.d/90-oci-alarm.cfg")
            if "name: alarm" not in cloud_cfg or "lock_passwd: true" not in cloud_cfg:
                raise RuntimeError("factory image is missing the alarm cloud-init override")
            sudoers = self.read_guest_file("/etc/sudoers.d/20-alarm-cloud")
            if "alarm ALL=(ALL:ALL) NOPASSWD: ALL" not in sudoers:
                raise RuntimeError("factory image is missing passwordless alarm sudo")
            required_paths = (
                "/usr/bin/cloud-init",
                "/usr/lib/systemd/system-generators/cloud-init-generator",
                "/usr/lib/systemd/system/cloud-init.target",
                "/usr/lib/systemd/system/cloud-init-local.service",
                "/usr/lib/systemd/system/cloud-init-main.service",
                "/usr/lib/systemd/system/cloud-final.service",
            )
            for path in required_paths:
                if not self.guest_path_exists(path):
                    raise RuntimeError(f"factory image is missing cloud-init component: {path}")
            forbidden_paths = (
                "/root/.ssh/authorized_keys", "/root/.ssh/authorized_keys2",
                "/home/alarm/.ssh/authorized_keys", "/home/alarm/.ssh/authorized_keys2",
                "/var/lib/systemd/random-seed", "/var/lib/cloud/instance",
            )
            for path in forbidden_paths:
                if self.guest_path_exists(path):
                    raise RuntimeError(f"factory image contains forbidden first-boot state: {path}")
            if self.guest_file_size("/etc/machine-id") != 0:
                raise RuntimeError("factory machine-id is not empty")
            if self.guest_glob("/etc/ssh/ssh_host_*") or self.guest_glob("/var/lib/cloud/instances/*"):
                raise RuntimeError("factory image contains generated SSH or cloud-init state")

    def remove_build_marker(self) -> None:
        self.guestfish([
            "run", "mount /dev/sda2 /", "rm-f /var/lib/archlinuxarm-oci/build-success", "sync", "umount-all"
        ])

    def sanitize_factory_image(self) -> None:
        """Remove identity and first-boot state after systemd can no longer recreate it."""
        self.guestfish([
            "run",
            "mount /dev/sda2 /",
            "truncate-size /etc/machine-id 0",
            "rm-f /var/lib/systemd/random-seed",
            "glob rm-f /etc/ssh/ssh_host_*",
            "rm-rf /root/.ssh",
            f"rm-rf /home/{FACTORY_USER}/.ssh",
            "rm-rf /var/lib/cloud",
            "mkdir-p /var/lib/cloud",
            "sync",
            "umount-all",
        ])

    def root_uuid(self) -> str:
        result = self.guestfish(["run", "vfs-uuid /dev/sda2"], read_only=True, capture=True)
        root_uuid = result.stdout.strip()
        if not root_uuid:
            raise RuntimeError("could not read the root filesystem UUID")
        return root_uuid

    def find_firmware(self) -> tuple[Path, Path]:
        if self.args.firmware_code or self.args.firmware_vars:
            if not (self.args.firmware_code and self.args.firmware_vars):
                raise SystemExit("--firmware-code and --firmware-vars must be supplied together")
            code = Path(self.args.firmware_code)
            variables = Path(self.args.firmware_vars)
            if not code.is_file() or not variables.is_file():
                raise SystemExit("specified AArch64 firmware files do not exist")
            return code, variables
        candidates = (
            (Path("/usr/share/AAVMF/AAVMF_CODE.fd"), Path("/usr/share/AAVMF/AAVMF_VARS.fd")),
            (Path("/usr/share/edk2/aarch64/QEMU_EFI.fd"), Path("/usr/share/edk2/aarch64/QEMU_VARS.fd")),
        )
        for code, variables in candidates:
            if code.is_file() and variables.is_file():
                return code, variables
        raise SystemExit(
            "AArch64 UEFI firmware not found. Install edk2-aarch64 (Arch) or "
            "qemu-efi-aarch64 (Debian/Ubuntu), or pass --firmware-code/--firmware-vars."
        )

    def install_smoke_payload(self, overlay: Path, root_uuid: str) -> None:
        assert self.work and self.admin_user
        staging = self.work / "smoke-payload"
        if staging.is_symlink():
            raise RuntimeError(f"refusing symlinked smoke staging directory: {staging}")
        if staging.exists():
            shutil.rmtree(staging)
        script_dir = staging / "usr/local/lib/archlinuxarm-oci-smoke"
        script_dir.mkdir(parents=True)
        shutil.copy2(PROJECT / "guest/uefi-smoke-test.sh", script_dir / "uefi-smoke-test.sh")
        factory = self.build_mode == "factory"
        smoke_key_b64 = base64.b64encode(SMOKE_PUBLIC_KEY.encode()).decode() if factory else "-"
        generated = {
            "etc/systemd/system/oci-image-smoke.service": render_template(
                "oci-image-smoke.service",
                BUILD_MODE=self.build_mode,
                IMAGE_USER=self.admin_user,
                SMOKE_KEY_B64=smoke_key_b64,
                AFTER_UNITS=(
                    "dbus.service cloud-final.service cloud-init-main.service" if factory else ""
                ),
                REQUIRES_UNITS=(
                    "Requires=dbus.service cloud-init-main.service cloud-final.service"
                    if factory else ""
                ),
                TIMEOUT_SECONDS="600" if factory else "300",
            ),
            "boot/grub/grub.cfg": render_template("grub-smoke.cfg", ROOT_UUID=root_uuid),
        }
        for relative, text in generated.items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        payload = self.work / "smoke-payload.tar.gz"
        self.write_root_owned_tar(staging, payload)
        self.guestfish([
            "run", "modprobe vfat", "mount /dev/sda2 /", "mount /dev/sda1 /boot/efi",
            f"tar-in {guestfish_quote(payload)} / compress:gzip xattrs:true acls:true",
            "sync", "umount-all",
        ], image=overlay, image_format="qcow2")

    def create_nocloud_seed(self) -> Path:
        assert self.work
        seed_dir = self.work / "nocloud-seed"
        if seed_dir.is_symlink():
            raise RuntimeError(f"refusing symlinked NoCloud seed directory: {seed_dir}")
        if seed_dir.exists():
            shutil.rmtree(seed_dir)
        seed_dir.mkdir()
        (seed_dir / "meta-data").write_text(
            "instance-id: archlinuxarm-oci-smoke\nlocal-hostname: oci-factory-smoke\n"
        )
        (seed_dir / "user-data").write_text(
            "#cloud-config\n"
            "users:\n"
            "  - default\n"
            "ssh_authorized_keys:\n"
            f"  - {SMOKE_PUBLIC_KEY}\n"
            "write_files:\n"
            "  - path: /var/lib/archlinuxarm-oci/cloud-init-smoke\n"
            "    permissions: '0600'\n"
            "    content: cloud-init NoCloud smoke completed\n"
        )
        seed_tar = self.work / "nocloud-seed.tar"
        if seed_tar.exists():
            seed_tar.unlink()
        with tarfile.open(seed_tar, "w") as archive:
            for child in sorted(seed_dir.iterdir()):
                archive.add(child, arcname=child.name)
        seed = self.work / "nocloud-seed.raw"
        if seed.exists():
            seed.unlink()
        run(["qemu-img", "create", "-f", "raw", seed, "4M"])
        self.guestfish([
            "run", "modprobe vfat", "mkfs vfat /dev/sda label:CIDATA", "mount /dev/sda /",
            f"tar-in {guestfish_quote(seed_tar)} /", "sync", "umount-all",
        ], image=seed, image_format="raw")
        return seed

    def run_uefi_smoke_test(self, root_uuid: str) -> None:
        assert self.work and self.raw
        print_stage("Boot-testing the completed image through AArch64 UEFI")
        overlay = self.work / "uefi-smoke.qcow2"
        for disposable in (
            overlay,
            self.work / "smoke-payload.tar.gz",
            self.work / "AAVMF_VARS.fd",
            self.work / "uefi-smoke-serial.log",
        ):
            if disposable.exists():
                disposable.unlink()
        run(["qemu-img", "create", "-f", "qcow2", "-F", "raw", "-b", self.raw, overlay])
        self.install_smoke_payload(overlay, root_uuid)
        seed = self.create_nocloud_seed() if self.build_mode == "factory" else None
        code, variables = self.find_firmware()
        vars_copy = self.work / "AAVMF_VARS.fd"
        shutil.copyfile(variables, vars_copy)
        args: list[object] = [
            "qemu-system-aarch64", "-name", "archlinuxarm-oci-uefi-smoke", *self.qemu_machine_args(),
            "-smp", "2", "-m", "2048", "-display", "none", "-monitor", "none",
            "-serial", "stdio", "-no-reboot",
            "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={code}",
            "-drive", f"if=pflash,format=raw,unit=1,file={vars_copy}",
            "-drive", f"file={overlay},format=qcow2,if=none,id=rootdisk,cache=writeback",
            "-device", "virtio-blk-pci,drive=rootdisk",
            "-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0",
            "-object", "rng-random,filename=/dev/urandom,id=rng0", "-device", "virtio-rng-pci,rng=rng0",
            "-rtc", "base=utc",
        ]
        if seed is not None:
            args.extend([
                "-drive", f"file={seed},format=raw,if=none,id=cidata,readonly=on",
                "-device", "virtio-blk-pci,drive=cidata",
            ])
        self.smoke_accel = self.select_acceleration()
        ConsoleRunner.run(
            args, log=self.work / "uefi-smoke-serial.log", success=SMOKE_SUCCESS,
            timeout=self.args.smoke_timeout,
        )

    def convert(self) -> None:
        assert self.raw
        print_stage("Converting verified raw disk to compressed QCOW2")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=self.output.name + ".", suffix=".partial", dir=self.output.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        run([
            "qemu-img", "convert", "-p", "-f", "raw", "-O", "qcow2", "-c",
            "-o", "compression_type=zstd", self.raw, temporary,
        ])
        run(["qemu-img", "check", "-f", "qcow2", temporary])
        os.replace(temporary, self.output)
        run(["qemu-img", "info", "-f", "qcow2", self.output])
        digest = hashlib.sha256()
        with self.output.open("rb") as source:
            for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        done = colorize("DONE:", ANSI_GREEN)
        sha256 = colorize("SHA256:", ANSI_GREEN)
        print(f"\n{done} {self.output}\n{sha256} {digest.hexdigest()}")

    def collect_passwords(self) -> None:
        if self.args.password is not None:
            if not self.args.password or any(char in self.args.password for char in "\r\n\0"):
                raise SystemExit("--password must be nonempty and cannot contain newline or NUL characters")
            warning = (
                "WARNING: --password is TEST-ONLY. Its value may be exposed by shell history and "
                "the host process list. The same test password will be used for root and the admin account."
            )
            print(colorize(warning, ANSI_YELLOW, stream=sys.stderr), file=sys.stderr)
            self.passwords = (self.args.password, self.args.password)
            return
        if not sys.stdin.isatty():
            raise SystemExit("interactive passwords require a terminal; use --password only for testing")
        assert self.admin_user
        root_password = prompt_password("root")
        admin_password = prompt_password(self.admin_user)
        self.passwords = (root_password, admin_password)

    def run_build_stage(self) -> tuple[str, str]:
        self.download_and_verify()
        self.extract_verified_kernel()
        root_uuid, esp_uuid = self.create_and_populate_disk()
        payload = self.create_build_payload(root_uuid, esp_uuid)
        self.install_build_payload(payload)
        self.run_build_vm()
        if self.build_mode == "factory":
            self.sanitize_factory_image()
        self.validate_built_image()
        self.remove_build_marker()
        self.write_state(root_uuid=root_uuid, smoke_passed=False)
        complete = colorize("BUILD STAGE COMPLETE:", ANSI_GREEN)
        print(f"\n{complete} {self.raw}\nWorkspace: {self.work}")
        return root_uuid, esp_uuid

    def build(self) -> None:
        if self.args.smoke_test_only:
            self.check_environment(
                required=("guestfish", "qemu-img", "qemu-system-aarch64"),
                require_firmware=True,
            )
            if self.args.password is not None:
                raise SystemExit("--password is only valid when running the build stage")
            self.start_workspace(resume=True)
            assert self.raw
            if not self.raw.is_file():
                raise RuntimeError(f"workspace raw disk is missing: {self.raw}")
            state = self.require_matching_state(require_smoke=False)
            root_uuid = self.root_uuid()
            if state.get("root_uuid") != root_uuid:
                raise RuntimeError("root filesystem UUID does not match workspace state")
            self.validate_built_image(require_marker=False)
            self.run_uefi_smoke_test(root_uuid)
            self.write_state(root_uuid=root_uuid, smoke_passed=True)
            complete = colorize("SMOKE STAGE COMPLETE:", ANSI_GREEN)
            print(f"\n{complete} {self.raw}\nWorkspace: {self.work}")
            return

        if self.args.convert_only:
            self.check_environment(required=("qemu-img",), require_firmware=False, require_accel=False)
            if self.args.password is not None or self.args.username is not None:
                raise SystemExit("--username and --password are not used with --convert-only")
            self.start_workspace(resume=True)
            assert self.raw
            if not self.raw.is_file():
                raise RuntimeError(f"workspace raw disk is missing: {self.raw}")
            self.require_matching_state(require_smoke=True)
            self.confirm_output()
            self.convert()
            return

        self.check_environment()
        if not self.args.build_only:
            self.confirm_output()
        if self.build_mode == "factory":
            self.admin_user = FACTORY_USER
        else:
            self.admin_user = prompt_username(self.args.username)
            self.collect_passwords()
        self.start_workspace()
        root_uuid, _ = self.run_build_stage()
        if self.args.build_only:
            return
        self.run_uefi_smoke_test(root_uuid)
        self.write_state(root_uuid=root_uuid, smoke_passed=True)
        self.convert()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-size", default="4G", help="virtual source disk size (default: 4G)")
    parser.add_argument("--hostname", default="oracle-arm", help="guest hostname")
    parser.add_argument("--username", help="admin username (prompted when omitted)")
    parser.add_argument("--password", help="UNSAFE TEST-ONLY password for both interactive accounts")
    parser.add_argument("--factory-image", action="store_true", help="build a credential-free cloud-init factory image")
    parser.add_argument("--output", default="archlinuxarm-oci.qcow2", help="output QCOW2 path")
    parser.add_argument("--rootfs-url", default=DEFAULT_ROOTFS_URL, help="official ALARM rootfs URL")
    parser.add_argument("--keyserver", default="hkps://keyserver.ubuntu.com", help="OpenPGP keyserver")
    parser.add_argument("--memory", type=int, default=4096, help="build VM RAM in MiB (default: 4096)")
    parser.add_argument("--cpus", type=int, default=4, help="build VM vCPU count (default: 4)")
    parser.add_argument("--accel", choices=("auto", "kvm", "tcg"), default="auto",
                        help="QEMU acceleration (default: auto)")
    parser.add_argument("--build-timeout", type=int, default=10800, help="build VM timeout in seconds")
    parser.add_argument("--smoke-timeout", type=int, default=600, help="UEFI smoke-test timeout in seconds")
    parser.add_argument("--firmware-code", help="AArch64 UEFI CODE firmware path")
    parser.add_argument("--firmware-vars", help="AArch64 UEFI VARS template path")
    parser.add_argument("--work-dir", help="exact retained workspace path for staged or resumed builds")
    parser.add_argument("--keep-work", action="store_true", help="keep the unprivileged temporary workspace")
    parser.add_argument("--force", action="store_true", help="replace an existing output without prompting")
    parser.add_argument("--check", action="store_true", help="check rootless dependencies and exit")
    stages = parser.add_mutually_exclusive_group()
    stages.add_argument("--build-only", action="store_true", help="build and validate the raw disk, then stop")
    stages.add_argument("--smoke-test-only", action="store_true", help="UEFI smoke-test an existing workspace")
    stages.add_argument("--convert-only", action="store_true", help="convert a smoke-tested workspace raw disk to QCOW2")
    args = parser.parse_args(argv)
    if args.factory_image and (args.username is not None or args.password is not None):
        parser.error("--factory-image cannot be combined with --username or --password")
    if (args.smoke_test_only or args.convert_only) and args.factory_image:
        parser.error("stage-only modes infer the build mode from --work-dir state; omit --factory-image")
    if (args.smoke_test_only or args.convert_only) and (
        args.username is not None or args.password is not None
    ):
        parser.error("--username and --password are only valid when running the build stage")
    return args


def main() -> int:
    args = parse_args()
    try:
        builder = Builder(args)
        if args.check:
            builder.check_environment()
        else:
            builder.build()
        return 0
    except (RuntimeError, TimeoutError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.rstrip(), file=sys.stderr)
        failed = colorize("FAILED:", ANSI_RED, stream=sys.stderr)
        print(f"\n{failed} {exc}", file=sys.stderr)
        return exc.returncode if isinstance(exc, subprocess.CalledProcessError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
