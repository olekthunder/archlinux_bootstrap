import logging
import pathlib
import re
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, Iterable, List, Mapping

import archinstall
import marshmallow_dataclass
import toml
from marshmallow import fields


class PathField(fields.Field):
    def _serialize(self, value, *args, **kwargs):
        if value is None:
            return ""
        return str(value)

    def _deserialize(self, value, *args, **kwargs):
        return pathlib.Path(value)


@dataclass
class Config:
    user: str
    kernel_package: str
    time_zone: str
    locales: List[str]
    lc_conf_vars: Dict[str, str]
    hostname: str
    key_label: pathlib.Path = field(metadata={"marshmallow_field": PathField()})
    key_mountpoint: pathlib.Path = field(
        metadata={"marshmallow_field": PathField()}
    )
    key_file: pathlib.Path = field(metadata={"marshmallow_field": PathField()})


def load_config(location: str) -> Config:
    return marshmallow_dataclass.class_schema(Config)().load(
        toml.load(location)
    )


# Remove password arg and use keyfile only
class Luks2(archinstall.luks2):
    def __init__(
        self,
        partition,
        mountpoint,
        key_file=None,
        auto_unmount=False,
        *args,
        **kwargs,
    ):
        super().__init__(
            partition,
            mountpoint,
            None,
            key_file=key_file,
            auto_unmount=auto_unmount,
            *args,
            **kwargs,
        )

    def __enter__(self):
        return self.unlock(self.partition, self.mountpoint, self.key_file)


@contextmanager
def partition_mount(partition: archinstall.Partition, dst: str):
    partition.mount(dst)
    try:
        yield
    finally:
        archinstall.log(f"unmounting {partition.path}")
        partition.umount()


@contextmanager
def mount_key(cfg: Config):
    archinstall.SysCommand(f"mkdir {cfg.key_mountpoint}")
    archinstall.SysCommand(
        f"mount /dev/disk/by-label/{cfg.key_label} {cfg.key_mountpoint}"
    )
    try:
        yield
    finally:
        archinstall.SysCommand(f"umount {cfg.key_mountpoint}")
        archinstall.SysCommand(f"rmdir {cfg.key_mountpoint}")


def use_mirrors(
    regions: Mapping[str, Iterable[str]],
    destination: str = "/etc/pacman.d/mirrorlist",
) -> bool:
    with open(destination, "w") as mirrorlist:
        for region, mirrors in regions.items():
            for mirror in mirrors:
                mirrorlist.write(f"## {region}\n")
                mirrorlist.write(f"Server = {mirror}\n")
    return True


def re_rank_mirrors(
    top: int = 10,
    src: str = "/etc/pacman.d/mirrorlist",
    dst: str = "/etc/pacman.d/mirrorlist",
) -> bool:
    cmd = archinstall.SysCommand(f"/usr/bin/rankmirrors -n {top} {src}")
    if cmd.exit_code != 0:
        return False
    with open(dst, "w") as f:
        f.write(str(cmd))
    return True


def rank_mirrors():
    archinstall.SysCommand("pacman -Sy pacman-contrib --noconfirm")
    archinstall.log("Ranking mirrors. It can take much time")
    use_mirrors(archinstall.list_mirrors())
    re_rank_mirrors(5)


def setup_bootloader(
    i: archinstall.Installer,
    cfg: Config,
):
    i.arch_chroot("bootctl --path=/boot install")
    with open(f"{i.target}/boot/loader/loader.conf", "w") as loader:
        loader.write("default arch\n")
        loader.write("timeout 3\n")
        loader.write("editor no\n")
    with open(f"{i.target}/boot/loader/entries/arch.conf", "w") as entry:
        entry.write("title Arch Linux\n")
        entry.write(f"linux /vmlinuz-{cfg.kernel_package}\n")
        vendor = archinstall.cpu_vendor()
        if vendor == "AuthenticAMD":
            entry.write("initrd /amd-ucode.img\n")
        elif vendor == "GenuineIntel":
            entry.write("initrd /intel-ucode.img\n")
        entry.write(f"initrd /initramfs-{cfg.kernel_package}.img\n")
        entry.write(
            "options cryptdevice=LABEL=cryptroot:cryptroot "
            f"cryptkey=LABEL={cfg.key_label}:vfat:{cfg.key_file} "
            "root=/dev/mapper/cryptroot rw\n"
        )
    i.helper_flags["bootloader"] = "systemd-bootctl"


def get_user_pass() -> str:
    return archinstall.storage["USER_PASSWORD"]


def add_user(i: archinstall.Installer, cfg: Config):
    i.user_create(cfg.user)
    i.user_set_pw(
        cfg.user,
        get_user_pass(),
    )
    i.enable_sudo(cfg.user)


def setup_network(i: archinstall.Installer):
    i.pacstrap("networkmanager", "systemd")
    i.enable_service("NetworkManager", "systemd-resolved")
    i.arch_chroot(
        "ln -sf /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf"
    )
    with open(f"{i.target}/etc/NetworkManager/conf.d/dns.conf", "w") as f:
        f.write("[main]\n")
        f.write("dns=systemd-resolved\n")


def setup_aur_helper(i: archinstall.Installer, cfg: Config):
    i.pacstrap("base-devel", "git")
    try:
        i.arch_chroot(
            "git clone https://aur.archlinux.org/paru.git "
            f"&& cd paru/ && echo {get_user_pass()} | makepkg -si --noconfirm",
            runas=cfg.user,
        )
    finally:
        i.arch_chroot("rm paru -rf")


def install_aur_package(i: archinstall.Installer, *packages: str):
    i.arch_chroot(f"paru -S {' '.join(packages)}")


def add_groups(i: archinstall.Installer, user: str, *groups: str):
    if len(groups) > 0:
        i.arch_chroot(f"usermod -aG {','.join(groups)} {user}")


class GPUManufacturer(Enum):
    INTEL = auto()


def get_gpu_manufacturer() -> GPUManufacturer:
    lspci = str(archinstall.SysCommand("lspci"))
    if "Intel" in lspci:
        return GPUManufacturer.INTEL
    raise NotImplementedError()


def setup_xorg(i: archinstall.Installer, cfg: Config):
    i.pacstrap("lightdm", "xorg-server", "lightdm-gtk-greeter")
    i.enable_service("lightdm")
    i.arch_chroot("groupadd -r autologin")
    add_groups(i, cfg.user, "autologin")
    with open(f"{i.target}/etc/lightdm/lightdm.conf") as f:
        txt = f.read()
        match = re.search(r"^\[Seat:\*\]", txt, flags=re.MULTILINE)
        if match is None:
            raise NotImplementedError()
        txt = (
            txt[: match.end()]
            + f"\nautologin-user={cfg.user}\n"
            + txt[match.end() :]
        )
    with open(f"{i.target}/etc/lightdm/lightdm.conf") as f:
        f.write(txt)


def misc_install(stack: ExitStack, cfg: Config):
    i = stack.enter_context(
        archinstall.Installer("/mnt", kernels=[cfg.kernel_package])
    )
    rank_mirrors()
    vendor = archinstall.cpu_vendor()
    if vendor == "AuthenticAMD":
        i.base_packages.append("amd-ucode")
        if (ucode := pathlib.Path(f"{i.target}/boot/amd-ucode.img")).exists():
            ucode.unlink()
    elif vendor == "GenuineIntel":
        i.base_packages.append("intel-ucode")
        if (ucode := pathlib.Path(f"{i.target}/boot/intel-ucode.img")).exists():
            ucode.unlink()
    i.pacstrap(i.base_packages)
    i.helper_flags["base-strapped"] = True
    i.set_hostname(cfg.hostname)
    # Set locale
    # i.set_locale does not support LC_* vars
    with open(f"{i.target}/etc/locale.gen", "a") as fh:
        for locale in cfg.locales:
            fh.write(f"{locale}\n")
    with open(f"{i.target}/etc/locale.conf", "w") as fh:
        for k, v in cfg.lc_conf_vars.items():
            fh.write(f"{k}={v}\n")
    i.arch_chroot("locale-gen")
    i.set_timezone(cfg.time_zone)
    i.activate_ntp()
    i.arch_chroot("hwclock --systohc")
    i.arch_chroot("chmod 700 /root")
    i.MODULES.append("vfat")
    if "encrypt" not in i.HOOKS:
        i.HOOKS.insert(i.HOOKS.index("filesystems"), "encrypt")
    i.mkinitcpio("-P")
    i.helper_flags["base"] = True
    # Run registered post-install hooks
    for function in i.post_base_install:
        i.log(
            f"Running post-installation hook: {function}",
            level=logging.INFO,
        )
        function(i)
    setup_bootloader(i, cfg)
    add_user(i, cfg)
    setup_aur_helper(i, cfg)
    setup_network(i)
    setup_xorg(i, cfg)
    i.install_profile(archinstall.storage["PROFILE"])


def partition_the_disk(
    stack: ExitStack, disk: archinstall.BlockDevice, cfg: Config
):
    fs = stack.enter_context(archinstall.Filesystem(disk, archinstall.GPT))
    # 512mb boot, the rest is for root
    fs.use_entire_disk("ext4")
    boot = fs.find_partition("/boot")
    boot.format("vfat")
    root = fs.find_partition("/")
    root.encrypted = True
    # Encrypt root
    key_file = cfg.key_mountpoint / cfg.key_file
    archinstall.log("Encrypting root...")
    archinstall.SysCommand(
        f"cryptsetup -q luksFormat {root.path} {key_file} --label cryptroot"
    )
    unlocked_root = stack.enter_context(
        Luks2(root, "cryptroot", key_file=key_file, auto_unmount=True)
    )
    # Format root as ext4 and add "arch" label to it
    archinstall.log("Formatting root as ext4")
    archinstall.SysCommand("mkfs.ext4 -L arch /dev/mapper/cryptroot")
    unlocked_root.filesystem = "ext4"
    stack.enter_context(partition_mount(unlocked_root, "/mnt"))
    stack.enter_context(partition_mount(boot, "/mnt/boot"))


def main():
    cfg = load_config("config.toml")
    archinstall.arguments["harddrive"] = archinstall.select_disk(
        archinstall.all_disks()
    )
    archinstall.storage["USER_PASSWORD"] = archinstall.get_password(
        prompt=f"Enter password for {cfg.user}: "
    )
    archinstall.storage["PROFILE"] = archinstall.select_profile()
    with ExitStack() as stack:
        stack.enter_context(mount_key(cfg))
        partition_the_disk(stack, archinstall.arguments["harddrive"], cfg)
        misc_install(stack, cfg)


if __name__ == "__main__":
    main()
