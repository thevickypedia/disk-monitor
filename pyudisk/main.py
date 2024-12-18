import os
import pathlib
import subprocess
from collections.abc import Generator
from datetime import datetime
from typing import Any, Dict, List, NoReturn

import psutil
from psutil._common import sdiskpart
from pydantic import NewPath

from .config import EnvConfig
from .logger import LOGGER
from .models import BlockDevices, Disk, Drives, SystemPartitions
from .notification import notification_service, send_report
from .support import humanize_usage_metrics, load_dump, load_partitions
from .util import standard


def get_disk(env: EnvConfig) -> Generator[sdiskpart]:
    """Gathers disk information using the 'psutil' library.

    Args:
        env: Environment variables configuration.

    Yields:
        sdiskpart:
        Yields the partition datastructure.
    """
    if env.dry_run:
        partitions = load_partitions(filename=env.sample_partitions)
    else:
        partitions = psutil.disk_partitions()
    system_partitions = SystemPartitions()
    for partition in partitions:
        if (
            not any(
                partition.mountpoint.startswith(mnt)
                for mnt in system_partitions.system_mountpoints
            )
            and partition.fstype not in system_partitions.system_fstypes
        ):
            yield partition


def get_smart_metrics(env: EnvConfig) -> str:
    """Gathers disk information using the dump from 'udisksctl' command.

    Args:
        env: Environment variables configuration.

    Returns:
        str:
        Returns the output from disk util dump.
    """
    if env.dry_run:
        text = load_dump(filename=env.sample_dump)
    else:
        try:
            output = subprocess.check_output(f"{env.udisk_lib} dump", shell=True)
        except subprocess.CalledProcessError as error:
            LOGGER.error(error)
            return ""
        text = output.decode(encoding="UTF-8")
    return text


def parse_drives(input_data: str) -> Dict[str, Any]:
    """Parses drivers' information from the dump into a datastructure.

    Args:
        input_data: Smart metrics dump.

    Returns:
        Dict[str, str]:
        Returns a dictionary of drives' metrics as key-value pairs.
    """
    formatted = {}
    head = None
    category = None
    for line in input_data.splitlines():
        if line.startswith(Drives.head):
            head = line.replace(Drives.head, "").rstrip(":").strip()
            formatted[head] = {}
        elif line.strip() in (Drives.category1, Drives.category2):
            category = (
                line.replace(Drives.category1, "Info")
                .replace(Drives.category2, "Attributes")
                .strip()
            )
            formatted[head][category] = {}
        elif head and category:
            try:
                key, val = line.strip().split(":", 1)
                key = key.strip()
                val = val.strip()
            except ValueError as error:
                assert (
                    str(error) == "not enough values to unpack (expected 2, got 1)"
                ), error
                continue
            formatted[head][category][key] = val
    return formatted


def parse_block_devices(
    env: EnvConfig, input_data: str
) -> Dict[sdiskpart, Dict[str, str]]:
    """Parses block_devices' information from the dump into a datastructure.

    Args:
        env: Environment variables configuration.
        input_data: Smart metrics dump.

    Returns:
        Dict[sdiskpart, str]:
        Returns a dictionary of block_devices' metrics as key-value pairs.
    """
    block_devices = {}
    block = None
    category = None
    block_partitions = {
        f"{BlockDevices.head}{block_device.device.split('/')[-1]}:": block_device
        for block_device in get_disk(env)
    }
    for line in input_data.splitlines():
        if matching_block := block_partitions.get(line):
            # Assing a temp value to avoid skipping loop when 'block' has a value
            block = matching_block
            block_devices[block] = {}
        elif block and line.strip() in (
            BlockDevices.category1,
            BlockDevices.category2,
            BlockDevices.category3,
        ):
            category = (
                line.replace(BlockDevices.category1, "Block")
                .replace(BlockDevices.category2, "Filesystem")
                .replace(BlockDevices.category3, "Partition")
                .strip()
            )
        elif block and category:
            try:
                key, val = line.strip().split(":", 1)
                key = key.strip()
                val = val.strip()
                if key == "Drive":
                    val = eval(val).replace(Drives.head, "")
                if key == "Symlinks":
                    block_devices[block][key] = [val]
            except ValueError as error:
                if block_devices[block].get("Symlinks") and line.strip():
                    block_devices[block]["Symlinks"].append(line.strip())
                assert (
                    str(error) == "not enough values to unpack (expected 2, got 1)"
                ), error
                continue
            if (
                # This will ensure that new data is not written to old key
                not block_devices[block].get(key)
                # 'org.freedesktop.UDisks2.Partition' records are skipped
                and category in ("Block", "Filesystem")
                # Only store keys that provide value
                and key
                in (
                    "Device",
                    "DeviceNumber",
                    "Drive",
                    "Id",
                    "IdLabel",
                    "IdType",
                    "IdUUID",
                    "IdUsage",
                    "ReadOnly",
                    "Size",
                    "MountPoints",
                )
            ):
                block_devices[block][key] = val
    return block_devices


def smart_metrics(env: EnvConfig) -> Generator[Disk]:
    """Gathers smart metrics using udisksctl dump, and constructs a Disk object.

    Args:
        env: Environment variables configuration.

    Yields:
        Disk:
        Yields the Disk object from the generated Dataframe.
    """
    smart_dump = get_smart_metrics(env)
    block_devices = dict(
        sorted(
            parse_block_devices(env, smart_dump).items(),
            key=lambda device: device[1]["Drive"],
        )
    )
    drives = {k: v for k, v in sorted(parse_drives(smart_dump).items())}
    if len(block_devices) != len(drives):
        LOGGER.warning(
            f"Number of block devices [{len(block_devices)}] does not match the number of drives [{len(drives)}]"
        )
        device_names = set(v["Drive"] for v in block_devices.values())
        drive_names = set(drives.keys())
        diff = (
            drive_names - device_names
            if len(drive_names) > len(device_names)
            else device_names - drive_names
        )
        LOGGER.warning("UNmounted drive(s) found - '%s'", ", ".join(diff))
    optional_fields = [
        k
        for k, v in Disk.model_json_schema().get("properties").items()
        if v.get("anyOf", [{}])[-1].get("type", "") == "null"
    ]
    # S.M.A.R.T metrics can be null, but the keys are mandatory
    for drive in drives.values():
        for key in optional_fields:
            if key not in drive.keys():
                drive[key] = None
    for (drive, data), (partition, block_data) in zip(
        drives.items(), block_devices.items()
    ):
        if drive == block_data["Drive"]:
            data["Partition"] = block_data
        else:
            raise ValueError(
                f"\n\n{drive} not found in {[bd['Drive'] for bd in block_devices.values()]}"
            )
        data["Usage"] = humanize_usage_metrics(psutil.disk_usage(partition.mountpoint))
        yield Disk(id=drive, model=data.get("Info", {}).get("Model", ""), **data)


def monitor_disk(env: EnvConfig) -> Generator[Disk]:
    """Monitors disk attributes based on the configuration.

    Args:
        env: Environment variables configuration.

    Yields:
        Disk:
        Data structure parsed as a Disk object.
    """
    message = ""
    for disk in smart_metrics(env):
        if disk.Attributes:
            for metric in env.metrics:
                attribute = disk.Attributes.model_dump().get(metric.attribute)
                if metric.max_threshold and attribute >= metric.max_threshold:
                    msg = f"{metric.attribute!r} for {disk.id!r} is >= {metric.max_threshold} at {attribute}"
                    LOGGER.critical(msg)
                    message += msg + "\n"
                if metric.min_threshold and attribute <= metric.min_threshold:
                    msg = f"{metric.attribute!r} for {disk.id!r} is <= {metric.min_threshold} at {attribute}"
                    LOGGER.critical(msg)
                    message += msg + "\n"
                if metric.equal_match and attribute != metric.equal_match:
                    msg = f"{metric.attribute!r} for {disk.id!r} IS NOT {metric.equal_match} at {attribute}"
                    LOGGER.critical(msg)
                    message += msg + "\n"
        else:
            LOGGER.warning("No attributes were loaded for %s", disk.model)
        yield disk
    if message:
        notification_service(
            title="Disk Monitor Alert!!", message=message, env_config=env
        )


def generate_html(
    data: List[Dict[str, str | int | float | bool]], filepath: NewPath = None
) -> str | NoReturn:
    """Generates an HTML report using Jinja2 template.

    Args:
        data: Data to render in the template.
        filepath: Path to save the HTML report locally.

    Returns:
        str:
        Rendered HTML report.
    """
    try:
        import jinja2
    except ModuleNotFoundError:
        standard()

    template_dir = os.path.join(pathlib.Path(__file__).parent, "templates")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(template_dir))
    template = env.get_template("template.html")
    now = datetime.now()
    html_output = template.render(
        data=data, last_updated=f"{now.strftime('%c')} {now.astimezone().tzinfo}"
    )
    if filepath:
        with open(filepath, "w") as file:
            file.write(html_output)
            file.flush()
    return html_output


def generate_report(**kwargs) -> str:
    """Generates the HTML report using UDisk lib.

    Args:
        **kwargs: Arbitrary keyword arguments.

    Returns:
        str:
        Returns the report filepath.
    """
    env = EnvConfig(**kwargs)
    if kwargs.get("raw"):
        return generate_html([disk.model_dump() for disk in monitor_disk(env)])
    if report_file := kwargs.get("filepath"):
        assert report_file.endswith(
            ".html"
        ), "\n\tReport filename should have the suffix '.html'"
        report_dir = str(pathlib.Path(report_file).parent)
        os.makedirs(report_dir, exist_ok=True)
    else:
        if directory := kwargs.get("directory"):
            env.report_dir = directory
        os.makedirs(env.report_dir, exist_ok=True)
        report_file = datetime.now().strftime(
            os.path.join(env.report_dir, env.report_file)
        )
    LOGGER.info("Generating disk report")
    disk_report = [disk.model_dump() for disk in monitor_disk(env)]
    generate_html(disk_report, report_file)
    LOGGER.info("Report has been stored in %s", report_file)
    return report_file


def monitor(**kwargs) -> None:
    """Entrypoint for the disk monitoring service.

    Args:
        **kwargs: Arbitrary keyword arguments.
    """
    env = EnvConfig(**kwargs)
    disk_report = [disk.model_dump() for disk in monitor_disk(env)]
    if disk_report:
        LOGGER.info(
            "Disk monitor report has been generated for %d disks", len(disk_report)
        )
        if env.disk_report:
            os.makedirs(env.report_dir, exist_ok=True)
            report_file = datetime.now().strftime(
                os.path.join(env.report_dir, env.report_file)
            )
            report_data = generate_html(disk_report, report_file)
            if env.gmail_user and env.gmail_pass and env.recipient:
                LOGGER.info("Sending an email disk report to %s", env.recipient)
                send_report(
                    title=f"Disk Report - {datetime.now().strftime('%c')}",
                    user=env.gmail_user,
                    password=env.gmail_pass,
                    recipient=env.recipient,
                    content=report_data,
                )
            else:
                LOGGER.warning(
                    "Reporting feature was enabled but necessary notification vars not found!!"
                )
        else:
            LOGGER.info("Reporting feature has been disabled!")
    else:
        LOGGER.warning("Disk monitor report was not generated!")
