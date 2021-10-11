import os
import subprocess
import sys
import time
import json
from dataclasses import dataclass
from typing import Dict, Iterable, List

import jinja2
import marshmallow_dataclass
import toml


@dataclass
class AppConfig:
    country: str
    kernel_package: str
    time_zone: str
    locales: List[str]
    lc_conf_vars: Dict[str, str]
    hostname: str


def is_efi():
    return os.path.exists("/sys/firmware/efi/efivars")


def load_config(location: str) -> AppConfig:
    return marshmallow_dataclass.class_schema(AppConfig).from_dict(
        toml.load(location)
    )


class CommandNotSuccessful(Exception):
    pass


def run(cmd: str, force: bool = False) -> None:
    """
    Execute cmd from shell.
    If force is True, it won't raise an exeption if cmd exit code isn't
    equal to 0
    """
    rv = subprocess.call(cmd, shell=True)
    if not force and rv != 0:
        raise CommandNotSuccessful(cmd)


def ask(prompt: str) -> str:
    return input(f"{prompt.strip().capitalize()} and press ENTER:\n")


def disk_partitions(device: str) -> List[str]:
    data = json.loads(
        subprocess.run(
            "lsblk --json -o NAME,PATH".split(), capture_output=True, text=True
        ).stdout
    )
    return [
        c["path"]
        for d in data["blockdevices"]
        if d["path"] == device
        for c in d["children"]
    ]


def partion_the_disk(device: str) -> None:
    run("mkdir /key && mount `findfs LABEL=lukskey` /key")
    run(f"yes | parted {device} -- mklabel gpt", force=True)
    run(f"yes | parted {device} -- mkpart ESP fat32 1MiB 512MiB", force=True)
    run(f"yes | parted {device} -- mkpart primary 512MiB 100%", force=True)
    run(f"yes | parted {device} -- set 1 esp on", force=True)
    key = "/key/key"
    root_label = "arch"
    boot, root = sorted(disk_partitions(device))
    run(f"yes YES | cryptsetup luksFormat {root} {key} --label cryptroot")
    run(f"cryptsetup luksOpen {root} cryptroot --key-file {key}")
    run(f"mkfs.ext4 -L {root_label} /dev/mapper/cryptroot")
    time.sleep(1)
    run(f"mount /dev/disk/by-label/{root_label} /mnt")
    run("mkdir /mnt/boot")
    run(f"mount {boot} /mnt/boot")


def sync_mirrors(country: str) -> None:
    country = country.strip().capitalize()
    run(
        f"reflector --save /etc/pacman.d/mirrorlist.back --country {country} "
        "--protocol https --latest 10"
    )
    # partial upgrades are not supported, but I'll take the risk
    run("pacman -Sy pacman-contrib --noconfirm")
    run(
        "rankmirrors -n 5 /etc/pacman.d/mirrorlist.back > /etc/pacman.d/mirrorlist"
    )


def genfstab(outfile: str) -> None:
    run(f"genfstab -U /mnt >> {outfile}")


def set_time_zone(tz: str) -> None:
    run(f"ln -sf /usr/share/zoneinfo/{tz} /etc/localtime")


def write_file(path: str, contents: str):
    # print(path)
    # print(contents)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(contents)


def write_files(cfg: AppConfig, base_dir: str):
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("archlinux_bootstrap", "files"),
        autoescape=jinja2.select_autoescape(),
    )
    for t in env.list_templates():
        write_file("/" + t, env.get_template(t).render(cfg=cfg))


def install_packages(pkgs: Iterable[str]):
    run(f"pacman -S --noconfirm {' '.join(pkgs)}")


def arch_chroot(location: str) -> None:
    """https://wiki.archlinux.org/title/chroot#Using_chroot"""
    os.chdir(location)
    run("mount -t proc /proc proc/")
    run("mount -t sysfs /sys sys/")
    run("mount --rbind /dev dev/")
    run("mount --rbind /run run/")
    run("mount --rbind /sys/firmware/efi/efivars sys/firmware/efi/efivars/")
    run("cp /etc/resolv.conf etc/resolv.conf")
    os.chroot(location)


def bootstrap():
    subprocess.call("cd ~", shell=True)
    if not is_efi():
        sys.exit("Not and EFI setup. Not supported by now. Exitting.")
    cfg = load_config("config.toml")
    run("timedatectl set-ntp true")
    partion_the_disk(ask("Enter a disk to partition"))
    sync_mirrors(cfg.country)
    run(
        f"pacstrap /mnt base base-devel {cfg.kernel_package} linux-firmware intel-ucode"
    )
    genfstab("/mnt/etc/fstab")
    arch_chroot("/mnt")
    set_time_zone(cfg.time_zone)
    run("hwclock --systohc")
    write_files(cfg, "files")
    run("locale-gen")
    run("mkinitcpio -P")
    run("passwd")
    run("bootctl --path=/boot install")
