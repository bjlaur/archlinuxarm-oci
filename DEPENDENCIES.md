# Host dependencies

`build.py` has no third-party Python dependencies. `requirements.txt` is intentionally empty except for comments.

The easiest route is:

```bash
./install-deps.sh
```

The installer checks the package database first and passes only **missing** package names to the package manager. On Arch/CachyOS it uses `pacman -Q` plus `pacman -S --needed`; on Debian/Ubuntu it uses `dpkg-query` and `apt-get install`.

## Arch / CachyOS

Equivalent package set:

```bash
sudo pacman -S --needed python qemu-img qemu-user-static qemu-user-static-binfmt libarchive gptfdisk dosfstools e2fsprogs curl gnupg util-linux systemd
```

## Debian / Ubuntu

Equivalent package set on releases where `qemu-user-static` is a concrete package:

```bash
sudo apt-get update
sudo apt-get install python3 qemu-utils qemu-user-static binfmt-support libarchive-tools gdisk dosfstools e2fsprogs curl gnupg util-linux systemd udev
```

If a newer Debian/Ubuntu release exposes `qemu-user-static` only as a virtual package, install the provider that registers AArch64 with `binfmt_misc` (typically `qemu-user-binfmt`) and verify that `/proc/sys/fs/binfmt_misc/qemu-aarch64` exists after restarting `systemd-binfmt.service`.

## Commands the builder requires

`blkid`, `bsdtar`, `chroot`, `curl`, `gpg`, `losetup`, `mkfs.ext4`, `mkfs.fat`, `mount`, `qemu-img`, `sgdisk`, `sudo`, `systemctl`, `udevadm`, and `umount`.

Run `build.py` as your normal user. It calls `sudo` only for operations requiring host root privileges; do not invoke the entire builder with `sudo`.
