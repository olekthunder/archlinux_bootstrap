import archinstall
import pathlib
import logging
from archinstall.lib.general import SysCommand
from contextlib import contextmanager, ExitStack
from archlinux_bootstrap.main import load_config, AppConfig


@contextmanager
def mount_key(cfg: AppConfig):
    archinstall.SysCommand(
        f"mkdir {cfg.key_mountpoint} "
        f"&& mount `findfs LABEL={cfg.key_label}` {cfg.key_mountpoint}"
    )
    try:
        yield
    finally:
        archinstall.SysCommand(f"unmount {cfg.key_mountpoint}")
        archinstall.SysCommand(f"rmdir {cfg.key_mountpoint}")


def setup_bootloader(i: archinstall.Installer, cfg: AppConfig):
    SysCommand(f"/usr/bin/arch-chroot {i.target} bootctl --path=/boot install")
    with open(f"{i.target}/boot/loader/loader.conf", "w") as loader:
        loader.write("default arch\n")
        loader.write("timeout 3\n")
        loader.write("editor no\n")
    with open(f"{i.target}/boot/loader/entries/arch.conf", "w") as entry:
        entry.write("title Arch Linux\n")
        entry.write("linux /vmlinuz-linux\n")
        vendor = archinstall.cpu_vendor()
        if vendor == "AuthenticAMD":
            entry.write("initrd /amd-ucode.img\n")
        elif vendor == "GenuineIntel":
            entry.write("initrd /intel-ucode.img\n")
        entry.write("initrd /initramfs-linux.img\n")
        entry.write(
            "options cryptdevice=LABEL=cryptroot:cryptroot "
            f"cryptkey=LABEL={cfg.key_label}:vfat:{cfg.key_file} "
            "root=/dev/mapper/cryptroot rw\n"
        )
    i.helper_flags["bootloader"] = "systemd-bootctl"


def misc_install(cfg: AppConfig):
    with archinstall.Installer("/mnt") as i:
        vendor = archinstall.cpu_vendor()
        if vendor == "AuthenticAMD":
            i.base_packages.append("amd-ucode")
            if (
                ucode := pathlib.Path(f"{i.target}/boot/amd-ucode.img")
            ).exists():
                ucode.unlink()
        elif vendor == "GenuineIntel":
            i.base_packages.append("intel-ucode")
            if (
                ucode := pathlib.Path(f"{i.target}/boot/intel-ucode.img")
            ).exists():
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
            for i, v in cfg.lc_conf_vars.items():
                fh.write(f"{i}={v}\n")
        SysCommand(f"/usr/bin/arch-chroot {i.target} chmod 700 /root")
        i.MODULES.append("vfat")
        i.mkinitcpio("-P")
        i.helper_flags["base"] = True
        # Run registered post-install hooks
        for function in i.post_base_install:
            i.log(
                f"Running post-installation hook: {function}",
                level=logging.INFO,
            )
            function(i)
        setup_bootloader(i)


def partition_the_disk(disk: archinstall.BlockDevice, cfg: AppConfig):
    with archinstall.Filesystem(disk, archinstall.GPT) as fs:
        # 512mb boot, the rest is for root
        fs.use_entire_disk("ext4")
        boot = fs.find_partition("/boot")
        boot.format("vfat")
        root = fs.find_partition("/")
        root.encrypted = True
        # Encrypt root
        key_file = cfg.key_mountpoint / cfg.key_file
        SysCommand(
            f"cryptsetup luksFormat {root.path} {key_file} --label cryptroot"
        )
        with archinstall.luks2(
            root, "cryptroot", key_file=key_file
        ) as unlocked_root:
            # Format root as ext4 and add "arch" label to it
            archinstall.SysCommand("mkfs.ext4 -L arch /dev/mapper/cryptroot")
            unlocked_root.mount("/mnt")
            boot.mount("/mnt/boot")


def main():
    cfg = load_config("config.toml")
    archinstall.arguments["harddrive"] = archinstall.select_disk(
        archinstall.all_disks()
    )
    with mount_key():
        partition_the_disk(archinstall.arguments["harddrive"], cfg)
        misc_install(cfg)


if __name__ == "__main__":
    main()
