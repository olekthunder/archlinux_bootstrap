import operator
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, List

import jinja2
import marshmallow_dataclass
import psutil
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
    rv = subprocess.call(cmd, shell=True)
    if not force and rv != 0:
        raise CommandNotSuccessful(cmd)


def ask(prompt: str) -> str:
    return input(f"{prompt.strip().capitalize()} and press ENTER:\n")


def partion_the_disk(device: str) -> None:
    run("mkdir /key && mount `findfs LABEL=lukskey` /key")
    run(f"yes | parted {device} -- mklabel gpt", force=True)
    run(f"yes | parted {device} -- mkpart ESP fat32 1MiB 512MiB", force=True)
    run(f"yes | parted {device} -- mkpart primary 512MiB 100%", force=True)
    run(f"yes | parted {device} -- set 1 esp on", force=True)
    key = "/key/key"
    root_label = "arch"
    disk_partitions = (
        p for p in psutil.disk_partitions() if device in p.device
    )
    boot, root = (
        p.device
        for p in sorted(disk_partitions, key=operator.attrgetter("device"))
    )
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
        f"reflector --save /etc/pacman.d/mirrorlist --country {country} "
        "--protocol https --latest 10"
    )


def genfstab(outfile: str) -> None:
    run(f"genfstab -U /mnt >> {outfile}")


def set_time_zone(tz: str) -> None:
    run(f"ln -sf /usr/share/zoneinfo/{tz} /etc/localtime")


def write_file(path: str, contents: str):
    # print(path)
    # print(contents)
    with open(path, "w") as f:
        f.write(contents)


def write_files(cfg: AppConfig, base_dir: str):
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("archlinux_bootstrap", "files"),
        autoescape=jinja2.select_autoescape(),
    )
    for t in env.list_templates():
        write_file("/" + t, env.get_template(t).render(cfg=cfg))


def bootstrap():
    subprocess.call("cd ~", shell=True)
    if not is_efi():
        sys.exit("Not and EFI setup. Not supported by now. Exitting.")
    cfg = load_config("config.toml")
    run("timedatectl set-ntp true")
    partion_the_disk(ask("Enter a disk to partition"))
    sync_mirrors(cfg.country)
    run(f"pacstrap /mnt base {cfg.kernel_package} linux-firmware")
    genfstab("/mnt/etc/fstab")
    run("arch-chroot /mnt")
    set_time_zone(cfg.time_zone)
    run("hwclock --systohc")
    write_files(cfg, "files")
    run("locale-gen")
    run("mkinitcpio -P")
    run("passwd")
    run("bootctl install")
