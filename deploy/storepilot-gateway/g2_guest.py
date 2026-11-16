"""Append two G2 Envoy configurations to the retained G1 QEMU initramfs."""

import argparse
import gzip
from pathlib import Path
import shutil

from render_g2 import render


def cpio_file(out, inode, name, mode, data):
    name = name.encode() + b"\0"
    header = b"070701" + b"".join(f"{value:08x}".encode() for value in (
        inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name), 0,
    ))
    out.write(header + name + b"\0" * (-(len(header) + len(name)) % 4))
    out.write(data + b"\0" * (-len(data) % 4))


START = b"""#!/bin/sh
set -ex
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
/bin/busybox --install -s
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
modprobe virtio_net
ip link set lo up
ip link set eth0 up
ip addr add 10.0.2.15/24 dev eth0
mount -o bind /dev /envoy-root/dev
mount -t proc proc /envoy-root/proc
mount -t sysfs sysfs /envoy-root/sys
chroot /envoy-root /usr/local/bin/envoy --version
for config in /g2-1.json /g2-2.json; do
  chroot /envoy-root /usr/local/bin/envoy -c "$config" --mode validate
done
set +x
chroot /envoy-root /usr/local/bin/envoy -c /g2-1.json --concurrency 1 --disable-hot-restart --log-level warning --file-flush-interval-msec 250 >/gateway-1.log 2>&1 &
chroot /envoy-root /usr/local/bin/envoy -c /g2-2.json --concurrency 1 --disable-hot-restart --log-level warning --file-flush-interval-msec 250 >/gateway-2.log 2>&1 &
echo 'G2 Envoy processes launched'
exec tail -n +1 -f /gateway-1.log /gateway-2.log
"""


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("g1_guest", type=Path)
    cli.add_argument("output", type=Path)
    args = cli.parse_args()
    with args.g1_guest.open("rb") as source, args.output.open("xb") as output:
        shutil.copyfileobj(source, output)
        with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as archive:
            cpio_file(archive, 1, "envoy-root/g2-1.json", 0o100644, render().encode())
            cpio_file(archive, 2, "envoy-root/g2-2.json", 0o100644, render(8081, 9902).encode())
            cpio_file(archive, 3, "storepilot-g2-init", 0o100755, START)
            cpio_file(archive, 0, "TRAILER!!!", 0, b"")
