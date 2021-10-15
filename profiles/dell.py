# Profile for work laptop

import archinstall

__packages__ = ["sof-firmware"]

if __name__ == "dell":
    archinstall.storage["installation_session"].add_additional_packages(
        __packages__
    )
