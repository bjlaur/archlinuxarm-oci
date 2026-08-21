#!/usr/bin/env python3
"""Build a hardened Arch Linux ARM AArch64 QCOW2 image for OCI Ampere A1."""

from __future__ import annotations

import argparse
import atexit
import hashlib
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parent
DEFAULT_ROOTFS_URL = "https://ca.us.mirror.archlinuxarm.org/os/ArchLinuxARM-aarch64-latest.tar.gz"
ROOTFS_SIGNING_FINGERPRINT = "68B3537F39A313B3E574D06777193F152BDBE6A6"
REQUIRED_COMMANDS = (
    "blkid", "bsdtar", "chroot", "curl", "gpg", "losetup", "mkfs.ext4",
    "mkfs.fat", "mount", "qemu-img", "sgdisk", "sudo", "sync", "systemctl", "truncate", "udevadm", "umount",
)


class Builder:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.admin_user = self.resolve_admin_user(args.username)
        self.hostname = self.validate_hostname(args.hostname)
        self.work = Path(tempfile.mkdtemp(prefix="oci-archarm.", dir="/var/tmp"))
        self.raw = self.work / "archlinuxarm-oci.raw"
        self.rootfs = self.work / "ArchLinuxARM-aarch64-latest.tar.gz"
        self.rootfs_sig = Path(str(self.rootfs) + ".sig")
        self.mountpoint = self.work / "mnt"
        self.loop: str | None = None
        self.mounted: list[Path] = []
        self.output = Path(args.output).resolve()
        self.mountpoint.mkdir(parents=True)
        atexit.register(self.cleanup)

    @staticmethod
    def resolve_admin_user(value: str | None) -> str:
        while True:
            username = value if value is not None else input("Admin username: ").strip()
            if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
                message = (
                    "Username must be 1-32 characters, start with a lowercase letter or underscore, "
                    "and contain only lowercase letters, digits, underscores, or hyphens."
                )
            elif username in {"root", "alarm"}:
                message = f"Username {username!r} is reserved and cannot be used."
            else:
                return username

            if value is not None:
                raise SystemExit(message)
            print(message)

    @staticmethod
    def validate_hostname(value: str) -> str:
        labels = value.split(".")
        valid_label = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
        if (
            not value
            or len(value) > 253
            or any(valid_label.fullmatch(label) is None for label in labels)
        ):
            raise SystemExit(
                "Hostname must be 1-253 characters of lowercase letters, digits, hyphens, "
                "or dots; each label must be 1-63 characters and start and end with a "
                "letter or digit."
            )
        return value

    @staticmethod
    def run(argv, *, capture=False, env=None, cwd=None):
        argv = [str(x) for x in argv]
        print("+", shlex.join(argv), flush=True)
        return subprocess.run(
            argv,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            env=env,
            cwd=cwd,
        )

    @classmethod
    def output_of(cls, argv) -> str:
        return cls.run(argv, capture=True).stdout.strip()

    def run_privileged(self, argv, *, capture=False, env=None, cwd=None):
        """Run one command with sudo; the builder itself stays unprivileged."""
        return self.run(["sudo", "--", *argv], capture=capture, env=env, cwd=cwd)

    def output_of_privileged(self, argv) -> str:
        return self.run_privileged(argv, capture=True).stdout.strip()

    def cleanup(self):
        if getattr(self, "mounted", None):
            for path in reversed(self.mounted):
                subprocess.run(
                    ["sudo", "--", "umount", "-R", str(path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            self.mounted.clear()
        if getattr(self, "loop", None):
            subprocess.run(
                ["sudo", "--", "losetup", "-d", self.loop],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.loop = None
        if getattr(self, "work", None) and self.work.exists() and not self.args.keep_work:
            shutil.rmtree(self.work, ignore_errors=True)

    def require_unprivileged_user(self):
        if os.geteuid() == 0:
            raise SystemExit(
                "Do not run the whole builder as root. Run ./build.py as your normal user; "
                "it will use sudo only for individual privileged operations."
            )

    def authorize_sudo(self):
        print("==> Authorizing privileged build operations with sudo")
        self.run(["sudo", "-v"])

    def check_commands(self):
        missing = [cmd for cmd in REQUIRED_COMMANDS if shutil.which(cmd) is None]
        if missing:
            raise SystemExit(
                "Missing host commands: " + ", ".join(missing) +
                "\nRun ./install-deps.sh first."
            )

    def enable_binfmt(self):
        print("==> Enabling AArch64 binfmt support")
        self.run_privileged(["systemctl", "restart", "systemd-binfmt.service"])
        marker = Path("/proc/sys/fs/binfmt_misc/qemu-aarch64")
        if not marker.exists():
            raise SystemExit(
                "qemu-aarch64 binfmt registration is missing after restarting systemd-binfmt.\n"
                "See CODEX_HANDOFF.md -> 'binfmt / Exec format error'."
            )

    def download_and_verify_rootfs(self):
        print("==> Downloading official Arch Linux ARM AArch64 rootfs")
        self.run(["curl", "-fL", "--retry", "3", "-o", self.rootfs, self.args.rootfs_url])
        self.run(["curl", "-fL", "--retry", "3", "-o", self.rootfs_sig, self.args.rootfs_url + ".sig"])

        print("==> Verifying Arch Linux ARM rootfs signature")
        gnupg = self.work / "gnupg"
        gnupg.mkdir(mode=0o700)
        env = os.environ.copy()
        env["GNUPGHOME"] = str(gnupg)
        self.run(["gpg", "--keyserver", self.args.keyserver, "--recv-keys", ROOTFS_SIGNING_FINGERPRINT], env=env)
        result = self.run(
            ["gpg", "--with-colons", "--fingerprint", ROOTFS_SIGNING_FINGERPRINT],
            capture=True,
            env=env,
        )
        fingerprints = [line.split(":")[9] for line in result.stdout.splitlines() if line.startswith("fpr:")]
        if ROOTFS_SIGNING_FINGERPRINT not in fingerprints:
            raise SystemExit(f"Unexpected signing-key fingerprint(s): {fingerprints}")
        self.run(["gpg", "--verify", self.rootfs_sig, self.rootfs], env=env)

    def create_disk(self):
        print(f"==> Creating {self.args.image_size} GPT disk")
        self.run(["truncate", "-s", self.args.image_size, self.raw])
        self.run(["sgdisk", "--zap-all", self.raw])
        self.run([
            "sgdisk",
            "-n", "1:1MiB:+512MiB", "-t", "1:ef00", "-c", "1:EFI",
            "-n", "2:0:0", "-t", "2:8300", "-c", "2:root",
            self.raw,
        ])
        self.loop = self.output_of_privileged(["losetup", "--find", "--show", "--partscan", self.raw])
        self.run_privileged(["udevadm", "settle"])
        esp = Path(self.loop + "p1")
        root = Path(self.loop + "p2")
        self.run_privileged(["mkfs.fat", "-F", "32", "-n", "EFI", esp])
        self.run_privileged(["mkfs.ext4", "-F", "-L", "root", root])
        self.run_privileged(["mount", root, self.mountpoint])
        self.mounted.append(self.mountpoint)
        self.run_privileged(["mkdir", "-p", self.mountpoint / "boot/efi"])
        self.run_privileged(["mount", esp, self.mountpoint / "boot/efi"])
        self.mounted.append(self.mountpoint / "boot/efi")
        return root, esp

    def extract_rootfs(self):
        print("==> Extracting Arch Linux ARM rootfs")
        self.run_privileged(["bsdtar", "-xpf", self.rootfs, "-C", self.mountpoint])

    def install_text(self, text: str, dst: str, mode: str = "0644"):
        """Write text as the normal user, then install it into the mounted guest with sudo."""
        staging = self.work / "generated" / dst.lstrip("/")
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_text(text)
        self.run_privileged(["install", "-D", "-m", mode, staging, self.mountpoint / dst.lstrip("/")])

    def write_template(self, src: str, dst: str, **values):
        text = (PROJECT / "templates" / src).read_text()
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        self.install_text(text, dst)

    def apply_overlay(self):
        print("==> Applying auditable static overlay")
        self.run_privileged(["cp", "-a", "--no-preserve=ownership", str(PROJECT / "overlay") + "/.", self.mountpoint])

    def prepare_chroot(self):
        # Temporary DNS during the build; final symlink is restored later.
        resolv = self.mountpoint / "etc/resolv.conf"
        self.run_privileged(["rm", "-f", resolv])
        self.run_privileged(["cp", "/etc/resolv.conf", resolv])

        # Mount the pseudo-filesystems package tools expect.
        for source, target, extra in (
            ("/dev", self.mountpoint / "dev", ["--rbind"]),
            ("/proc", self.mountpoint / "proc", ["--types", "proc"]),
            ("/sys", self.mountpoint / "sys", ["--rbind"]),
            ("/run", self.mountpoint / "run", ["--rbind"]),
        ):
            self.run_privileged(["mkdir", "-p", target])
            if source == "/proc":
                self.run_privileged(["mount", *extra, "proc", target])
            else:
                self.run_privileged(["mount", *extra, source, target])
            self.mounted.append(target)
            if source in ("/dev", "/sys", "/run"):
                self.run_privileged(["mount", "--make-rslave", target])

    def copy_guest_scripts(self):
        target = self.mountpoint / "tmp/oci-image-build"
        self.run_privileged(["mkdir", "-p", target])
        for script in (PROJECT / "guest").iterdir():
            self.run_privileged(["install", "-m", "0755", script, target / script.name])

    def chroot(self, *argv, capture=False):
        return self.run_privileged(["chroot", self.mountpoint, *argv], capture=capture)

    def configure_guest(self):
        print("==> Installing and configuring guest packages/users")
        self.chroot("/tmp/oci-image-build/configure.sh", self.admin_user)

        print("\n==> SET A NEW ROOT PASSWORD (console/local only; root SSH is disabled)")
        self.chroot("passwd", "root")
        print(f"\n==> SET THE SSH/SUDO PASSWORD FOR {self.admin_user}")
        self.chroot("passwd", self.admin_user)

        result = self.chroot("/tmp/oci-image-build/verify-login-users.sh", capture=True)
        expected = "\n".join(sorted(("root", self.admin_user)))
        if result.stdout.strip() != expected:
            raise SystemExit("Unexpected interactive accounts remain:\n" + result.stdout)

    def configure_boot(self, root_dev: Path, esp_dev: Path):
        root_uuid = self.output_of_privileged(["blkid", "-s", "UUID", "-o", "value", root_dev])
        esp_uuid = self.output_of_privileged(["blkid", "-s", "UUID", "-o", "value", esp_dev])
        self.write_template("fstab", "/etc/fstab", ROOT_UUID=root_uuid, ESP_UUID=esp_uuid)
        self.write_template("grub.cfg", "/boot/grub/grub.cfg", ROOT_UUID=root_uuid)
        self.write_template(
            "sshd-security.conf",
            "/etc/ssh/sshd_config.d/10-oci-security.conf",
            ADMIN_USER=self.admin_user,
        )

        self.install_text(self.hostname + "\n", "/etc/hostname")
        self.install_text("LANG=en_US.UTF-8\n", "/etc/locale.conf")

        self.chroot("/tmp/oci-image-build/finalize.sh")

    def final_sanity_checks(self):
        print("==> Running offline sanity checks")
        required = (
            "boot/Image",
            "boot/initramfs-linux.img",
            "boot/efi/EFI/BOOT/BOOTAA64.EFI",
            "etc/ssh/sshd_config.d/10-oci-security.conf",
            "usr/local/sbin/oci-grow-root",
        )
        for item in required:
            if not (self.mountpoint / item).exists():
                raise SystemExit(f"Missing expected image file: /{item}")
        self.chroot("ssh-keygen", "-A")
        self.chroot("sshd", "-t")
        self.chroot("/tmp/oci-image-build/verify-login-users.sh")

    def finish_guest(self):
        # Restore systemd-resolved for the real VM.
        resolv = self.mountpoint / "etc/resolv.conf"
        self.run_privileged(["rm", "-f", resolv])
        self.run_privileged(["ln", "-s", "/run/systemd/resolve/stub-resolv.conf", resolv])
        self.run_privileged(["rm", "-rf", self.mountpoint / "tmp/oci-image-build"])

        # Fresh identity/SSH host keys on first boot.
        self.run_privileged(["truncate", "-s", "0", self.mountpoint / "etc/machine-id"])
        self.run_privileged(["rm", "-f", self.mountpoint / "var/lib/systemd/random-seed"])
        self.run_privileged(["find", self.mountpoint / "etc/ssh", "-maxdepth", "1", "-type", "f", "-name", "ssh_host_*", "-delete"])

    def unmount_all(self):
        print("==> Unmounting image")
        self.run(["sync"])
        for path in reversed(self.mounted):
            self.run_privileged(["umount", "-R", path])
        self.mounted.clear()
        if self.loop:
            self.run_privileged(["losetup", "-d", self.loop])
            self.loop = None

    def convert(self):
        print("==> Converting to compressed QCOW2")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        if self.output.exists():
            self.output.unlink()
        self.run(["qemu-img", "convert", "-p", "-f", "raw", "-O", "qcow2", "-c", self.raw, self.output])
        self.run(["qemu-img", "check", self.output])
        self.run(["qemu-img", "info", self.output])
        hasher = hashlib.sha256()
        with self.output.open("rb") as f:
            for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        print(f"\nDONE: {self.output}")
        print(f"SHA256: {digest}")
        print("\nOCI: QCOW2 / Linux / Paravirtualized / UEFI_64 / VM.Standard.A1.Flex")

    def build(self):
        self.require_unprivileged_user()
        self.check_commands()
        self.authorize_sudo()
        self.enable_binfmt()
        self.download_and_verify_rootfs()
        root, esp = self.create_disk()
        self.extract_rootfs()
        self.prepare_chroot()
        self.copy_guest_scripts()
        self.configure_guest()
        self.apply_overlay()
        self.configure_boot(root, esp)
        self.final_sanity_checks()
        self.finish_guest()
        self.unmount_all()
        self.convert()


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image-size", default="10G", help="virtual source disk size (default: 10G)")
    p.add_argument("--hostname", default="oracle-arm", help="guest hostname")
    p.add_argument("--username", help="admin login username (prompted when omitted)")
    p.add_argument("--output", default="archlinuxarm-oci.qcow2", help="output QCOW2 path")
    p.add_argument("--rootfs-url", default=DEFAULT_ROOTFS_URL, help="Arch Linux ARM rootfs URL")
    p.add_argument("--keyserver", default="hkps://keyserver.ubuntu.com", help="OpenPGP keyserver")
    p.add_argument("--keep-work", action="store_true", help="keep /var/tmp build directory for debugging")
    return p.parse_args()


if __name__ == "__main__":
    try:
        Builder(parse_args()).build()
    except subprocess.CalledProcessError as e:
        print(f"\nFAILED: command exited {e.returncode}: {shlex.join(map(str, e.cmd))}", file=sys.stderr)
        raise SystemExit(e.returncode)
