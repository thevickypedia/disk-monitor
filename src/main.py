import logging

import psutil
from pySMART import SMARTCTL, Device


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

SMARTCTL.sudo = True


def get_smart_data(disk: Device):
    if disk.smart_capable:
        print(f"\tModel:  {disk.model}")
        print(f"\tAssessment:  {disk.assessment}")
    else:
        LOGGER.info("SMART not supported on this device.")


def get_all_disks():
    partitions = psutil.disk_partitions()
    for partition in partitions:
        if "loop" in partition.device:
            continue
        usage = psutil.disk_usage(partition.mountpoint)
        print(f"Device: {partition.device}")
        print(f"  Total: {usage.total / (1024 ** 3):.2f} GB")
        print(f"  Used: {usage.used / (1024 ** 3):.2f} GB")
        print(f"  Free: {usage.free / (1024 ** 3):.2f} GB")
        print(f"  Percentage: {usage.percent}%")
        yield Device(partition.device)


def monitor():
    for disk in get_all_disks():
        get_smart_data(disk)


if __name__ == '__main__':
    monitor()
