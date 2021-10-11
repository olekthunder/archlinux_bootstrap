# Script to bootstrap my Arch Linux installation

## Running from ArchISO

```
iwctl --passphrase $passphrase station $device connect $SSID
pacman -Sy git --noconfirm
git clone https://github.com/olekthunder/archlinux_bootstrap.git
cd archlinux_bootstrap/
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Configuration and tweaking

This script takes values from [config.toml](config.toml)

You can also add files to [files](files) directory. They are jinja templates. `files` directory structure will be preserved, files will be overwritten. Every file template accepts `AppConfig`. 

To add values to the `AppConfig` just add field type annotations
and add your values to the [config.toml](config.toml).

Partitioning is hardcoded to my preferred setup.
You can edit the `partion_the_disk` function to suit your needs.
