import os
import re
import select
import shlex
import subprocess
import sys
import tempfile
import termios
import time
import tty
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# ============================================================
# Configuration
# ============================================================

SERVICE_SOURCES = [
    {
        "name": "~/Services",
        "path": Path.home() / "Services",
    },
    {
        "name": "/etc/systemd/system",
        "path": Path("/etc/systemd/system"),
    },
]

SYSTEMD_DIR = Path("/etc/systemd/system")

STATUS_REFRESH_INTERVAL = 2.0
SERVICE_LIST_REFRESH_INTERVAL = 10.0
ERROR_REFRESH_INTERVAL = 10.0
LOG_REFRESH_INTERVAL = 2.0

MAX_VISIBLE_SERVICES = 15

SYSTEMD_PROPERTIES = [
    "Id",
    "Names",
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "MemoryCurrent",
    "NRestarts",
    "ActiveEnterTimestamp",
    "ControlGroup",
]

SERVICE_NAME_RE = re.compile(
    r"^[A-Za-z0-9_.@:-]+\.service$"
)


# ============================================================
# Dashboard state
# ============================================================

source_index = 0
selected_index = 0

show_logs = False
status_message = ""

services = []

service_cache = {}
service_metadata = {}
pid_memory_cache = {}

last_status_refresh = 0.0
last_service_list_refresh = 0.0
last_error_refresh = 0.0
last_log_refresh = 0.0

selected_error_text = "Loading..."
selected_log_text = "Loading..."


# ============================================================
# Command helpers
# ============================================================

def run_command(command, timeout=5):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return (
            result.returncode,
            result.stdout.strip(),
            result.stderr.strip(),
        )

    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out"

    except Exception as exc:
        return 1, "", str(exc)


def run_interactive_command(command):
    try:
        result = subprocess.run(command)
        return result.returncode == 0

    except Exception as exc:
        print(f"Command failed: {exc}")
        return False


def run_admin_command(command):
    if os.geteuid() == 0:
        return run_interactive_command(command)

    return run_interactive_command(
        ["sudo", *command]
    )


# ============================================================
# Filesystem helpers
# ============================================================

def path_lexists(path):
    """
    Detect normal files and broken symlinks.
    """

    return os.path.lexists(str(path))


def is_systemd_path(path):
    """
    Check whether path belongs under /etc/systemd/system.
    """

    try:
        parent = path.parent.resolve(
            strict=False
        )

        root = SYSTEMD_DIR.resolve(
            strict=False
        )

        return (
            parent == root
            or root in parent.parents
        )

    except OSError:
        return False


def can_write_directory(directory):
    return (
        os.geteuid() == 0
        or os.access(
            directory,
            os.W_OK,
        )
    )


def write_service_file(destination, content):
    """
    Write normally when possible.

    For protected directories such as /etc/systemd/system,
    use sudo install.
    """

    parent = destination.parent

    if not parent.exists():
        try:
            parent.mkdir(
                parents=True,
                exist_ok=True,
            )

        except PermissionError:
            if not run_admin_command(
                [
                    "mkdir",
                    "-p",
                    str(parent),
                ]
            ):
                return False

    # --------------------------------------------------------
    # Writable directory
    # --------------------------------------------------------

    if can_write_directory(parent):
        tmp_path = None

        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=str(parent),
                prefix=f".{destination.name}.",
                suffix=".tmp",
            ) as tmp:

                tmp.write(content)
                tmp.flush()

                os.fsync(
                    tmp.fileno()
                )

                tmp_path = Path(
                    tmp.name
                )

            os.chmod(
                tmp_path,
                0o644,
            )

            os.replace(
                tmp_path,
                destination,
            )

            return True

        except Exception as exc:
            print(
                f"Failed to create service: {exc}"
            )

            if (
                tmp_path
                and tmp_path.exists()
            ):
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

            return False

    # --------------------------------------------------------
    # Protected directory
    # --------------------------------------------------------

    tmp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            prefix="service-dashboard-",
            suffix=".service",
        ) as tmp:

            tmp.write(content)
            tmp.flush()

            os.fsync(
                tmp.fileno()
            )

            tmp_path = Path(
                tmp.name
            )

        return run_admin_command(
            [
                "install",
                "-m",
                "0644",
                str(tmp_path),
                str(destination),
            ]
        )

    finally:
        if (
            tmp_path
            and tmp_path.exists()
        ):
            try:
                tmp_path.unlink()
            except OSError:
                pass


def create_symlink(source, destination):
    if not destination.parent.exists():
        print(
            "\nDestination directory does not exist:"
        )
        print(destination.parent)
        return False

    if path_lexists(destination):
        print(
            "\nDestination already exists:"
        )
        print(destination)
        return False

    if can_write_directory(
        destination.parent
    ):
        try:
            os.symlink(
                str(source),
                str(destination),
            )

            return True

        except OSError as exc:
            print(
                f"Symlink failed: {exc}"
            )

            return False

    return run_admin_command(
        [
            "ln",
            "-s",
            "--",
            str(source),
            str(destination),
        ]
    )


def delete_path(path):
    """
    Delete only the selected filesystem entry.

    Deleting a symlink does not delete its source.
    """

    if can_write_directory(
        path.parent
    ):
        try:
            os.unlink(path)
            return True

        except OSError as exc:
            print(
                f"Delete failed: {exc}"
            )
            return False

    return run_admin_command(
        [
            "rm",
            "--",
            str(path),
        ]
    )


def daemon_reload():
    return run_admin_command(
        [
            "systemctl",
            "daemon-reload",
        ]
    )


# ============================================================
# Service source
# ============================================================

def get_current_source():
    return SERVICE_SOURCES[
        source_index
    ]


def load_services():
    global service_metadata

    directory = (
        get_current_source()["path"]
    )

    found = []
    metadata = {}

    try:
        with os.scandir(
            directory
        ) as entries:

            for entry in entries:
                if not entry.name.endswith(
                    ".service"
                ):
                    continue

                is_link = (
                    entry.is_symlink()
                )

                try:
                    is_file = (
                        entry.is_file(
                            follow_symlinks=True
                        )
                    )

                except OSError:
                    is_file = False

                if not (
                    is_file
                    or is_link
                ):
                    continue

                path = Path(
                    entry.path
                )

                found.append(
                    entry.name
                )

                info = {
                    "path": str(path),
                    "is_symlink": is_link,
                    "link_source": None,
                    "resolved_target": None,
                    "target_name": None,
                }

                if is_link:
                    try:
                        info[
                            "link_source"
                        ] = os.readlink(path)

                    except OSError:
                        pass

                    try:
                        resolved = path.resolve(
                            strict=False
                        )

                        info[
                            "resolved_target"
                        ] = str(resolved)

                        info[
                            "target_name"
                        ] = resolved.name

                    except OSError:
                        pass

                metadata[
                    entry.name
                ] = info

    except (
        FileNotFoundError,
        PermissionError,
        OSError,
    ):
        service_metadata = {}
        return []

    service_metadata = metadata

    return sorted(
        set(found),
        key=str.lower,
    )


def refresh_service_list(force=False):
    global services
    global selected_index
    global last_service_list_refresh

    now = time.monotonic()

    if (
        not force
        and
        now - last_service_list_refresh
        < SERVICE_LIST_REFRESH_INTERVAL
    ):
        return False

    last_service_list_refresh = now

    previous = None

    if (
        services
        and
        0 <= selected_index < len(services)
    ):
        previous = services[
            selected_index
        ]

    new_services = load_services()

    changed = (
        new_services != services
    )

    services = new_services

    if not services:
        selected_index = 0
        return changed

    if previous in services:
        selected_index = (
            services.index(previous)
        )

    else:
        selected_index = min(
            selected_index,
            len(services) - 1,
        )

    return changed


def switch_service_source():
    global source_index
    global selected_index

    global show_logs
    global status_message

    global service_cache
    global pid_memory_cache

    global last_status_refresh
    global last_service_list_refresh

    source_index = (
        source_index + 1
    ) % len(SERVICE_SOURCES)

    selected_index = 0
    show_logs = False

    service_cache = {}
    pid_memory_cache = {}

    last_status_refresh = 0.0
    last_service_list_refresh = 0.0

    reset_selected_journal_cache()

    refresh_service_list(
        force=True
    )

    refresh_service_data(
        force=True
    )

    status_message = (
        "Source changed to "
        f"{get_current_source()['name']}"
    )


# ============================================================
# Fast systemd status
# ============================================================

def refresh_service_data(force=False):
    global service_cache
    global last_status_refresh

    now = time.monotonic()

    if (
        not force
        and
        now - last_status_refresh
        < STATUS_REFRESH_INTERVAL
    ):
        return False

    refresh_service_list()

    last_status_refresh = now

    if not services:
        service_cache = {}
        pid_memory_cache.clear()
        return True

    query_units = list(services)

    # Include actual symlink targets too.
    for metadata in (
        service_metadata.values()
    ):
        target = metadata.get(
            "target_name"
        )

        if (
            target
            and target.endswith(
                ".service"
            )
        ):
            query_units.append(
                target
            )

    # Remove duplicates while preserving order.
    query_units = list(
        dict.fromkeys(
            query_units
        )
    )

    command = [
        "systemctl",
        "show",
        "--no-pager",
    ]

    for prop in SYSTEMD_PROPERTIES:
        command.append(
            f"--property={prop}"
        )

    command.append("--")
    command.extend(query_units)

    _, stdout, _ = run_command(
        command,
        timeout=10,
    )

    cache = {}

    if stdout:
        blocks = re.split(
            r"\n\s*\n",
            stdout.strip(),
        )
    else:
        blocks = []

    for block in blocks:
        if not block.strip():
            continue

        info = {}

        for line in block.splitlines():
            if "=" not in line:
                continue

            key, value = line.split(
                "=",
                1,
            )

            info[key] = value

        canonical = (
            info.get(
                "Id",
                "",
            ).strip()
        )

        if not canonical:
            continue

        cache[
            canonical
        ] = info

        # Also map all aliases to the same information.
        for name in (
            info.get(
                "Names",
                "",
            ).split()
        ):
            if name.endswith(
                ".service"
            ):
                cache[
                    name
                ] = info

    service_cache = cache

    refresh_pid_memory()

    return True


def get_service_info(service):
    direct = service_cache.get(
        service
    )

    if (
        direct is not None
        and direct.get(
            "LoadState"
        ) != "not-found"
    ):
        return direct

    metadata = service_metadata.get(
        service,
        {},
    )

    target = metadata.get(
        "target_name"
    )

    if target:
        target_info = (
            service_cache.get(
                target
            )
        )

        if (
            target_info is not None
            and target_info.get(
                "LoadState"
            ) != "not-found"
        ):
            return target_info

    if direct is not None:
        return direct

    return {
        "Id": service,
        "Names": service,
        "LoadState": "not-found",
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": "0",
        "MemoryCurrent": "",
        "NRestarts": "0",
        "ActiveEnterTimestamp": "",
        "ControlGroup": "",
    }


# ============================================================
# Memory
# ============================================================

def parse_memory(value):
    try:
        value = int(value)

        if 0 < value < 10**15:
            return value

    except (
        TypeError,
        ValueError,
    ):
        pass

    return None


def refresh_pid_memory():
    global pid_memory_cache

    needed = set()
    seen = set()

    for info in (
        service_cache.values()
    ):
        # Avoid processing alias copies repeatedly.
        info_id = id(info)

        if info_id in seen:
            continue

        seen.add(info_id)

        if (
            parse_memory(
                info.get(
                    "MemoryCurrent"
                )
            )
            is not None
        ):
            continue

        try:
            pid = int(
                info.get(
                    "MainPID",
                    "0",
                )
            )

        except (
            TypeError,
            ValueError,
        ):
            continue

        if pid > 0:
            needed.add(pid)

    if not needed:
        pid_memory_cache = {}
        return

    pid_argument = ",".join(
        str(pid)
        for pid in sorted(needed)
    )

    rc, stdout, _ = run_command(
        [
            "ps",
            "-o",
            "pid=,rss=",
            "-p",
            pid_argument,
        ],
        timeout=5,
    )

    if rc != 0:
        return

    cache = {}

    for line in stdout.splitlines():
        parts = line.split()

        if len(parts) != 2:
            continue

        try:
            pid = int(parts[0])
            rss_kib = int(parts[1])

            cache[
                pid
            ] = rss_kib * 1024

        except ValueError:
            continue

    pid_memory_cache = cache


def get_cgroup_memory(info):
    control_group = info.get(
        "ControlGroup",
        "",
    )

    if not control_group:
        return None

    memory_file = (
        Path("/sys/fs/cgroup")
        / control_group.lstrip("/")
        / "memory.current"
    )

    try:
        return parse_memory(
            memory_file
            .read_text()
            .strip()
        )

    except (
        FileNotFoundError,
        PermissionError,
        OSError,
    ):
        return None


def get_service_memory(info):
    # 1. systemd MemoryCurrent
    memory = parse_memory(
        info.get(
            "MemoryCurrent"
        )
    )

    if memory is not None:
        return memory

    # 2. cgroup
    memory = get_cgroup_memory(
        info
    )

    if memory is not None:
        return memory

    # 3. Main PID RSS
    try:
        pid = int(
            info.get(
                "MainPID",
                "0",
            )
        )

    except (
        TypeError,
        ValueError,
    ):
        return None

    if pid <= 0:
        return None

    return pid_memory_cache.get(
        pid
    )


def format_memory(value):
    if (
        value is None
        or value <= 0
    ):
        return "-"

    if value < 1024:
        return f"{value} B"

    if value < 1024**2:
        return (
            f"{value / 1024:.1f} KB"
        )

    if value < 1024**3:
        return (
            f"{value / (1024**2):.1f} MB"
        )

    return (
        f"{value / (1024**3):.2f} GB"
    )


# ============================================================
# Journal
# ============================================================

def reset_selected_journal_cache():
    global last_error_refresh
    global last_log_refresh

    global selected_error_text
    global selected_log_text

    last_error_refresh = 0.0
    last_log_refresh = 0.0

    selected_error_text = "Loading..."
    selected_log_text = "Loading..."


def refresh_selected_error(force=False):
    global last_error_refresh
    global selected_error_text

    if not services:
        selected_error_text = (
            "No service selected"
        )
        return False

    now = time.monotonic()

    if (
        not force
        and
        now - last_error_refresh
        < ERROR_REFRESH_INTERVAL
    ):
        return False

    last_error_refresh = now

    service = services[
        selected_index
    ]

    _, stdout, stderr = run_command(
        [
            "journalctl",
            "-u",
            service,
            "-p",
            "err",
            "-n",
            "3",
            "--no-pager",
            "--output=short",
        ],
        timeout=5,
    )

    selected_error_text = (
        stdout
        or stderr
        or "No recent errors"
    )

    return True


def refresh_selected_logs(force=False):
    global last_log_refresh
    global selected_log_text

    if (
        not services
        or not show_logs
    ):
        return False

    now = time.monotonic()

    if (
        not force
        and
        now - last_log_refresh
        < LOG_REFRESH_INTERVAL
    ):
        return False

    last_log_refresh = now

    service = services[
        selected_index
    ]

    _, stdout, stderr = run_command(
        [
            "journalctl",
            "-u",
            service,
            "-n",
            "20",
            "--no-pager",
            "--output=short",
        ],
        timeout=5,
    )

    selected_log_text = (
        stdout
        or stderr
        or "No logs available"
    )

    return True


# ============================================================
# Create-service helpers
# ============================================================

def normalize_service_name(name):
    name = name.strip()

    if not name.endswith(
        ".service"
    ):
        name += ".service"

    return name


def valid_service_name(name):
    return bool(
        SERVICE_NAME_RE.fullmatch(
            name
        )
    )


def prompt_yes_no(
    message,
    default=False,
):
    suffix = (
        " [Y/n]: "
        if default
        else " [y/N]: "
    )

    while True:
        value = (
            input(
                message + suffix
            )
            .strip()
            .lower()
        )

        if not value:
            return default

        if value in {
            "y",
            "yes",
        }:
            return True

        if value in {
            "n",
            "no",
        }:
            return False

        print(
            "Please enter y or n."
        )


# ============================================================
# High-contrast creation UI
# ============================================================

def creation_header(
    console,
    step,
    title,
    service_name=None,
    file_path=None,
):
    console.clear()

    console.rule(
        "[bold bright_cyan]"
        "Create Service"
        "[/bold bright_cyan]"
    )

    header = Text()

    header.append(
        f"{step}/4",
        style="bold bright_cyan",
    )

    header.append(
        "  "
    )

    header.append(
        title,
        style="bold bright_white",
    )

    console.print()
    console.print(header)

    if service_name:
        line = Text()

        line.append(
            "Service: ",
            style="bold white",
        )

        line.append(
            service_name,
            style="bright_white",
        )

        console.print(line)

    if file_path:
        line = Text()

        line.append(
            "Path: ",
            style="bold white",
        )

        line.append(
            str(file_path),
            style="bright_white",
        )

        console.print(line)

    console.print()


def print_value(
    console,
    label,
    value,
):
    """
    Completed value, for example:

        Description: Loop sensor reader service
    """

    line = Text()

    line.append(
        f"{label}: ",
        style="bold bright_white",
    )

    if value:
        line.append(
            str(value),
            style="bright_white",
        )

    else:
        line.append(
            "-",
            style="white",
        )

    console.print(line)


def print_optional_prompt(
    console,
    title,
    example=None,
    extra=None,
):
    line = Text()

    line.append(
        title,
        style="bold bright_white",
    )

    line.append(
        " (optional)",
        style="yellow",
    )

    console.print(line)

    if extra:
        console.print(
            extra,
            style="white",
        )

    if example:
        console.print(
            f"Example: {example}",
            style="white",
        )


def print_required_prompt(
    console,
    title,
    example=None,
    extra=None,
):
    line = Text()

    line.append(
        title,
        style="bold bright_white",
    )

    line.append(
        " (required)",
        style="bold bright_red",
    )

    console.print(line)

    if extra:
        console.print(
            extra,
            style="white",
        )

    if example:
        console.print(
            f"Example: {example}",
            style="bright_white",
        )


def show_settings_progress(
    console,
    service_name,
    file_path,
    description=None,
    dependency=None,
    working_directory=None,
    include_description=False,
    include_dependency=False,
    include_working_directory=False,
):
    creation_header(
        console,
        3,
        "Service settings",
        service_name=service_name,
        file_path=file_path,
    )

    console.print(
        "Press Enter to skip optional fields.",
        style="white",
    )

    console.print()

    if include_description:
        print_value(
            console,
            "Description",
            description,
        )

    if include_dependency:
        print_value(
            console,
            "Requires / After",
            dependency,
        )

    if include_working_directory:
        print_value(
            console,
            "WorkingDirectory",
            working_directory,
        )

    if (
        include_description
        or include_dependency
        or include_working_directory
    ):
        console.print()


def prompt_working_directory(
    console,
    service_name,
    file_path,
    description,
    dependency,
):
    while True:
        show_settings_progress(
            console,
            service_name,
            file_path,
            description=description,
            dependency=dependency,
            include_description=True,
            include_dependency=True,
        )

        print_optional_prompt(
            console,
            "WorkingDirectory",
            example="/home/pi",
        )

        value = input(
            "> "
        ).strip()

        if not value:
            return ""

        path = Path(value)

        if not path.is_absolute():
            console.print()
            console.print(
                "WorkingDirectory must use "
                "an absolute path.",
                style="bold bright_red",
            )

            console.print(
                "Example: /home/pi",
                style="bright_white",
            )

            input(
                "\nPress Enter to try again..."
            )

            continue

        if not path.exists():
            console.print()
            console.print(
                "Directory does not exist:",
                style="bold bright_red",
            )

            console.print(
                str(path),
                style="bright_white",
            )

            input(
                "\nPress Enter to try again..."
            )

            continue

        if not path.is_dir():
            console.print()
            console.print(
                "This path is not a directory:",
                style="bold bright_red",
            )

            console.print(
                str(path),
                style="bright_white",
            )

            input(
                "\nPress Enter to try again..."
            )

            continue

        return str(
            path.resolve()
        )


def exec_start_error(value):
    if not value.strip():
        return (
            "ExecStart is required."
        )

    try:
        parts = shlex.split(
            value
        )

    except ValueError as exc:
        return (
            f"Invalid command: {exc}"
        )

    if not parts:
        return (
            "ExecStart is required."
        )

    executable = Path(
        parts[0]
    )

    if not executable.is_absolute():
        return (
            "The executable must use an "
            "absolute path."
        )

    if not executable.exists():
        return (
            "Executable does not exist:\n"
            f"{executable}"
        )

    if not executable.is_file():
        return (
            "Executable is not a file:\n"
            f"{executable}"
        )

    if not os.access(
        executable,
        os.X_OK,
    ):
        return (
            "Executable is not executable:\n"
            f"{executable}"
        )

    # Catch obvious relative file/script paths.
    for argument in parts[1:]:
        if argument.startswith("-"):
            continue

        if argument.startswith(
            (
                "~/",
                "./",
                "../",
            )
        ):
            return (
                "Use absolute paths for files "
                "in ExecStart.\n"
                f"Relative path: {argument}"
            )

        suffix = (
            Path(argument)
            .suffix
            .lower()
        )

        if (
            suffix in {
                ".py",
                ".sh",
                ".js",
                ".jar",
            }
            and not Path(
                argument
            ).is_absolute()
        ):
            return (
                "Script paths must be absolute.\n"
                f"Relative path: {argument}"
            )

    return None


def build_service_content(
    description,
    dependency,
    working_directory,
    exec_start,
):
    lines = [
        "[Unit]",
    ]

    if description:
        lines.append(
            f"Description={description}"
        )

    if dependency:
        lines.append(
            f"Requires={dependency}"
        )

        lines.append(
            f"After={dependency}"
        )

    lines.extend(
        [
            "",
            "[Service]",
            "Type=simple",
            "User=pi",
            "Group=pi",
        ]
    )

    if working_directory:
        lines.append(
            f"WorkingDirectory="
            f"{working_directory}"
        )

    lines.extend(
        [
            f"ExecStart={exec_start}",
            "Restart=on-failure",
            "RestartSec=5s",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )

    return "\n".join(lines)


def pause_any_key(
    fd,
    normal_settings,
):
    print(
        "\nPress any key to "
        "return to dashboard...",
        end="",
        flush=True,
    )

    try:
        tty.setcbreak(fd)

        os.read(
            fd,
            1,
        )

    finally:
        termios.tcsetattr(
            fd,
            termios.TCSADRAIN,
            normal_settings,
        )

        try:
            termios.tcflush(
                fd,
                termios.TCIFLUSH,
            )

        except termios.error:
            pass


# ============================================================
# Create service
# ============================================================

def create_service_wizard(
    console,
    fd,
    normal_settings,
):

    # ========================================================
    # 1/4 Service name
    # ========================================================

    creation_header(
        console,
        1,
        "Service name",
    )

    console.print(
        "Enter a name for the service.",
        style="bright_white",
    )

    console.print(
        "Example: sensor-reader.service",
        style="white",
    )

    console.print()

    while True:
        raw_name = input(
            "Service name: "
        ).strip()

        if not raw_name:
            console.print(
                "Service name is required.",
                style="bold bright_red",
            )
            console.print()
            continue

        service_name = (
            normalize_service_name(
                raw_name
            )
        )

        if valid_service_name(
            service_name
        ):
            break

        console.print(
            "Invalid service name.",
            style="bold bright_red",
        )

        console.print(
            "Example: sensor-reader.service",
            style="bright_white",
        )

        console.print()

    # ========================================================
    # 2/4 Location
    # ========================================================

    creation_header(
        console,
        2,
        "Save location",
        service_name=service_name,
    )

    option = Text()

    option.append(
        "1",
        style="bold bright_cyan",
    )

    option.append(
        "  ~/Services",
        style="bright_white",
    )

    console.print(option)

    console.print(
        f"   {SERVICE_SOURCES[0]['path']}",
        style="white",
    )

    console.print()

    option = Text()

    option.append(
        "2",
        style="bold bright_cyan",
    )

    option.append(
        "  /etc/systemd/system",
        style="bright_white",
    )

    console.print(option)

    console.print(
        f"   {SERVICE_SOURCES[1]['path']}",
        style="white",
    )

    console.print()

    while True:
        choice = input(
            "Choose [1/2]: "
        ).strip()

        if choice == "1":
            source = (
                SERVICE_SOURCES[0]
            )
            break

        if choice == "2":
            source = (
                SERVICE_SOURCES[1]
            )
            break

        console.print(
            "Please enter 1 or 2.",
            style="bold bright_red",
        )

    file_path = (
        source["path"]
        / service_name
    )

    if path_lexists(
        file_path
    ):
        console.print()

        console.print(
            "Service already exists:",
            style="bold bright_red",
        )

        console.print(
            str(file_path),
            style="bright_white",
        )

        pause_any_key(
            fd,
            normal_settings,
        )

        return "Create cancelled"

    # ========================================================
    # 3/4 Description
    # ========================================================

    show_settings_progress(
        console,
        service_name,
        file_path,
    )

    print_optional_prompt(
        console,
        "Description",
        example=(
            "Loop sensor reader service"
        ),
    )

    description = input(
        "> "
    ).strip()

    # ========================================================
    # 3/4 Requires / After
    # ========================================================

    show_settings_progress(
        console,
        service_name,
        file_path,
        description=description,
        include_description=True,
    )

    print_optional_prompt(
        console,
        "Requires / After",
        example="network-online.target",
        extra=(
            "Enter once; the same value "
            "is used for both."
        ),
    )

    dependency = input(
        "> "
    ).strip()

    # ========================================================
    # 3/4 WorkingDirectory
    # ========================================================

    working_directory = (
        prompt_working_directory(
            console,
            service_name,
            file_path,
            description,
            dependency,
        )
    )

    # ========================================================
    # 3/4 ExecStart
    # ========================================================

    while True:
        show_settings_progress(
            console,
            service_name,
            file_path,
            description=description,
            dependency=dependency,
            working_directory=working_directory,
            include_description=True,
            include_dependency=True,
            include_working_directory=True,
        )

        print_required_prompt(
            console,
            "ExecStart",
            example=(
                "/usr/bin/python3 "
                "/home/pi/sensor-reader.py"
            ),
        )

        exec_start = input(
            "> "
        ).strip()

        error = exec_start_error(
            exec_start
        )

        if error is None:
            break

        console.print()

        console.print(
            error,
            style="bold bright_red",
        )

        input(
            "\nPress Enter to try again..."
        )

    # ========================================================
    # Build service file
    # ========================================================

    content = (
        build_service_content(
            description,
            dependency,
            working_directory,
            exec_start,
        )
    )

    # ========================================================
    # Preview
    # ========================================================

    creation_header(
        console,
        3,
        "Review",
        service_name=service_name,
        file_path=file_path,
    )

    console.print(
        Panel(
            Text(
                content,
                style="bright_white",
            ),
            title="Service File",
            border_style="bright_cyan",
        )
    )

    if not prompt_yes_no(
        "\nCreate this file?",
        default=True,
    ):
        pause_any_key(
            fd,
            normal_settings,
        )

        return "Create cancelled"

    if not write_service_file(
        file_path,
        content,
    ):
        console.print()

        console.print(
            "Failed to create service.",
            style="bold bright_red",
        )

        pause_any_key(
            fd,
            normal_settings,
        )

        return "Create failed"

    systemd_changed = (
        is_systemd_path(
            file_path
        )
    )

    # ========================================================
    # 4/4 Symlink
    # ========================================================

    creation_header(
        console,
        4,
        "Symlink",
        service_name=service_name,
        file_path=file_path,
    )

    console.print(
        "Create a symlink for this service?",
        style="bright_white",
    )

    console.print(
        "Press Enter for No.",
        style="white",
    )

    console.print()

    symlink_path = None

    if prompt_yes_no(
        "Create symlink?",
        default=False,
    ):
        console.print()

        console.print(
            "Enter the full destination path.",
            style="bold bright_white",
        )

        console.print(
            "Example: "
            "/etc/systemd/system/"
            "sensor-reader.service",
            style="bright_white",
        )

        console.print()

        while True:
            value = input(
                "Destination: "
            ).strip()

            candidate = Path(
                value
            )

            if not candidate.is_absolute():
                console.print(
                    "Use an absolute path.",
                    style="bold bright_red",
                )
                console.print()
                continue

            if not valid_service_name(
                candidate.name
            ):
                console.print(
                    "Destination must end "
                    "with a valid .service name.",
                    style="bold bright_red",
                )
                console.print()
                continue

            if not candidate.parent.exists():
                console.print(
                    "Destination directory "
                    "does not exist:",
                    style="bold bright_red",
                )

                console.print(
                    str(
                        candidate.parent
                    ),
                    style="bright_white",
                )

                console.print()
                continue

            if path_lexists(
                candidate
            ):
                console.print(
                    "Destination already exists:",
                    style="bold bright_red",
                )

                console.print(
                    str(candidate),
                    style="bright_white",
                )

                console.print()
                continue

            try:
                if (
                    candidate.resolve(
                        strict=False
                    )
                    ==
                    file_path.resolve(
                        strict=False
                    )
                ):
                    console.print(
                        "Symlink cannot point "
                        "to itself.",
                        style="bold bright_red",
                    )

                    console.print()
                    continue

            except OSError:
                pass

            symlink_path = candidate
            break

        source_absolute = (
            file_path.resolve(
                strict=False
            )
        )

        if not create_symlink(
            source_absolute,
            symlink_path,
        ):
            console.print()

            console.print(
                "Service was created, but "
                "the symlink could not be created.",
                style="bold yellow",
            )

            symlink_path = None

        elif is_systemd_path(
            symlink_path
        ):
            systemd_changed = True

    # ========================================================
    # systemd reload
    # ========================================================

    if systemd_changed:
        console.print()

        console.print(
            "Reloading systemd...",
            style="bright_white",
        )

        if not daemon_reload():
            console.print(
                "Warning: daemon-reload failed.",
                style="bold yellow",
            )

    # ========================================================
    # Final result
    # ========================================================

    console.clear()

    console.rule(
        f"[bold bright_green]"
        f"{service_name}"
        f"[/bold bright_green]"
    )

    console.print()

    console.print(
        Panel(
            Text(
                content,
                style="bright_white",
            ),
            title="Service File",
            border_style="bright_green",
        )
    )

    console.print()

    console.print(
        "File path:",
        style="bold bright_white",
    )

    console.print(
        str(file_path),
        style="bright_white",
    )

    if symlink_path:
        console.print()

        console.print(
            "Symlink:",
            style="bold bright_white",
        )

        console.print(
            str(symlink_path),
            style="bright_white",
        )

        console.print(
            "    →",
            style="bright_cyan",
        )

        console.print(
            str(
                file_path.resolve(
                    strict=False
                )
            ),
            style="bright_white",
        )

    pause_any_key(
        fd,
        normal_settings,
    )

    return (
        f"Created {service_name}"
    )


# ============================================================
# Delete service
# ============================================================

def delete_service_wizard(
    console,
    fd,
    normal_settings,
):
    if not services:
        console.clear()

        console.print(
            "No service selected.",
            style="bold yellow",
        )

        pause_any_key(
            fd,
            normal_settings,
        )

        return "Delete cancelled"

    service_name = services[
        selected_index
    ]

    metadata = service_metadata.get(
        service_name,
        {},
    )

    file_path = Path(
        metadata.get("path")
        or (
            get_current_source()["path"]
            / service_name
        )
    )

    console.clear()

    console.rule(
        "[bold bright_red]"
        "Delete Service"
        "[/bold bright_red]"
    )

    console.print()

    console.print(
        f"Service: {service_name}",
        style="bright_white",
    )

    console.print(
        f"Path: {file_path}",
        style="bright_white",
    )

    if metadata.get(
        "is_symlink"
    ):
        console.print(
            "Type: Symlink",
            style="bright_white",
        )

        console.print(
            "Link source: "
            f"{metadata.get('link_source') or '-'}",
            style="bright_white",
        )

        console.print()

        console.print(
            "Only this symlink will be deleted. "
            "Its source file will remain.",
            style="bold yellow",
        )

    else:
        console.print(
            "Type: Service file",
            style="bright_white",
        )

    console.print()

    console.print(
        "Type the service name exactly "
        "to confirm:",
        style="bright_white",
    )

    console.print(
        service_name,
        style="bold bright_red",
    )

    confirmation = input(
        "\n> "
    ).strip()

    if confirmation != service_name:
        console.print()

        console.print(
            "Name did not match. "
            "Deletion cancelled.",
            style="bold yellow",
        )

        pause_any_key(
            fd,
            normal_settings,
        )

        return "Delete cancelled"

    if not path_lexists(
        file_path
    ):
        console.print()

        console.print(
            "File no longer exists.",
            style="bold bright_red",
        )

        pause_any_key(
            fd,
            normal_settings,
        )

        return "Delete failed"

    if not delete_path(
        file_path
    ):
        console.print()

        console.print(
            "Deletion failed.",
            style="bold bright_red",
        )

        pause_any_key(
            fd,
            normal_settings,
        )

        return "Delete failed"

    if is_systemd_path(
        file_path
    ):
        console.print(
            "\nReloading systemd...",
            style="bright_white",
        )

        daemon_reload()

    console.print()

    console.print(
        f"Deleted: {service_name}",
        style="bold bright_green",
    )

    pause_any_key(
        fd,
        normal_settings,
    )

    return (
        f"Deleted {service_name}"
    )


# ============================================================
# Formatting
# ============================================================

def format_start_time(timestamp):
    if not timestamp:
        return "-"

    match = re.search(
        r"\b(\d{2}:\d{2}:\d{2})\b",
        timestamp,
    )

    if match:
        return match.group(1)

    return timestamp


def get_state_display(info):
    load_state = info.get(
        "LoadState",
        "unknown",
    )

    state = info.get(
        "ActiveState",
        "unknown",
    )

    if load_state == "not-found":
        return Text(
            "✕ not installed",
            style="bright_magenta",
        )

    if state == "active":
        return Text(
            "● running",
            style="bright_green",
        )

    if state == "failed":
        return Text(
            "✕ failed",
            style="bright_red",
        )

    if state == "inactive":
        return Text(
            "○ stopped",
            style="bright_yellow",
        )

    if state == "activating":
        return Text(
            "◐ starting",
            style="bright_cyan",
        )

    if state == "deactivating":
        return Text(
            "◐ stopping",
            style="bright_yellow",
        )

    return Text(
        f"? {state}",
        style="bright_white",
    )


# ============================================================
# Start / Stop / Restart
# ============================================================

def run_systemctl_action(
    service,
    action,
):
    rc, stdout, stderr = run_command(
        [
            "systemctl",
            "--no-ask-password",
            action,
            service,
        ],
        timeout=12,
    )

    if rc == 0:
        return (
            rc,
            stdout,
            stderr,
        )

    error_text = (
        stderr
        or stdout
        or ""
    ).lower()

    permission_errors = (
        "authentication",
        "access denied",
        "permission denied",
        "not authorized",
    )

    if any(
        text in error_text
        for text in permission_errors
    ):
        return run_command(
            [
                "sudo",
                "-n",
                "systemctl",
                action,
                service,
            ],
            timeout=12,
        )

    return (
        rc,
        stdout,
        stderr,
    )


def control_service(
    service,
    action,
):
    global status_message
    global last_status_refresh

    if action not in {
        "start",
        "stop",
        "restart",
    }:
        return

    rc, stdout, stderr = (
        run_systemctl_action(
            service,
            action,
        )
    )

    if rc == 0:
        status_message = (
            f"{action.capitalize()} "
            f"successful: {service}"
        )

    else:
        error = (
            stderr
            or stdout
            or "Unknown error"
        ).replace(
            "\n",
            " ",
        )

        if len(error) > 120:
            error = (
                error[:120]
                + "..."
            )

        status_message = (
            f"{action.capitalize()} "
            f"failed: {error}"
        )

    last_status_refresh = 0.0

    refresh_service_data(
        force=True
    )

    reset_selected_journal_cache()


# ============================================================
# Symlink display
# ============================================================

def get_visible_range():
    total = len(services)

    if total <= MAX_VISIBLE_SERVICES:
        return 0, total

    half = (
        MAX_VISIBLE_SERVICES
        // 2
    )

    start = max(
        0,
        selected_index - half,
    )

    end = (
        start
        + MAX_VISIBLE_SERVICES
    )

    if end > total:
        end = total

        start = max(
            0,
            end - MAX_VISIBLE_SERVICES,
        )

    return start, end


def get_link_display(service):
    metadata = service_metadata.get(
        service,
        {},
    )

    if not metadata.get(
        "is_symlink"
    ):
        return "-"

    target = metadata.get(
        "target_name"
    )

    if target:
        return f"→ {target}"

    return "→ ?"


def build_symlink_details(service):
    metadata = service_metadata.get(
        service,
        {},
    )

    path = metadata.get(
        "path",
        "-",
    )

    if not metadata.get(
        "is_symlink"
    ):
        return (
            f"[bold bright_white]"
            f"File:"
            f"[/bold bright_white] "
            f"{path}\n"
            f"[bold bright_white]"
            f"Symlink:"
            f"[/bold bright_white] "
            f"No"
        )

    link_source = (
        metadata.get(
            "link_source"
        )
        or "-"
    )

    resolved = (
        metadata.get(
            "resolved_target"
        )
        or "-"
    )

    return (
        f"[bold bright_white]"
        f"File:"
        f"[/bold bright_white] "
        f"{path}\n"
        f"[bold bright_white]"
        f"Symlink:"
        f"[/bold bright_white] "
        f"[bright_cyan]Yes[/bright_cyan]\n"
        f"[bold bright_white]"
        f"Link source:"
        f"[/bold bright_white] "
        f"{link_source}\n"
        f"[bold bright_white]"
        f"Resolved:"
        f"[/bold bright_white] "
        f"{resolved}"
    )


# ============================================================
# Dashboard UI
# ============================================================

def make_source_subtitle():
    source = (
        get_current_source()
    )

    text = Text()

    text.append(
        f"Source: {source['name']}",
        style="bright_white",
    )

    if services:
        text.append(
            f"  "
            f"{selected_index + 1}"
            f"/{len(services)}",
            style="white",
        )

    text.append(
        "    "
    )

    text.append(
        "[d]",
        style="bold bright_cyan",
    )

    text.append(
        " Change source",
        style="white",
    )

    return text


def build_service_table():
    table = Table(
        show_header=True,
        header_style="bold bright_white",
        box=None,
        expand=True,
        padding=(0, 1),
    )

    table.add_column(
        "",
        width=2,
    )

    table.add_column(
        "Service",
        ratio=2,
    )

    table.add_column(
        "Status",
        width=18,
    )

    table.add_column(
        "Memory",
        width=12,
        justify="right",
    )

    table.add_column(
        "Link source",
        ratio=1,
    )

    start, end = (
        get_visible_range()
    )

    for index in range(
        start,
        end,
    ):
        service = services[
            index
        ]

        info = get_service_info(
            service
        )

        selector = (
            ">"
            if index == selected_index
            else ""
        )

        style = (
            "bold bright_white"
            if index == selected_index
            else "white"
        )

        table.add_row(
            selector,
            service,
            get_state_display(
                info
            ),
            format_memory(
                get_service_memory(
                    info
                )
            ),
            get_link_display(
                service
            ),
            style=style,
        )

    return Panel(
        table,
        title="Services",
        subtitle=(
            make_source_subtitle()
        ),
        border_style="white",
    )


def build_empty_panel():
    source = (
        get_current_source()
    )

    text = Text()

    text.append(
        "No .service files found in:\n\n",
        style="bright_white",
    )

    text.append(
        str(
            source["path"]
        ),
        style="bright_white",
    )

    text.append(
        "\n\n"
    )

    text.append(
        "[c]",
        style="bold bright_cyan",
    )

    text.append(
        " Create service    ",
        style="white",
    )

    text.append(
        "[d]",
        style="bold bright_cyan",
    )

    text.append(
        " Change source",
        style="white",
    )

    return Panel(
        text,
        title="Services",
        border_style="white",
    )


def build_detail_panel(service):
    info = get_service_info(
        service
    )

    state = info.get(
        "ActiveState",
        "unknown",
    )

    load_state = info.get(
        "LoadState",
        "unknown",
    )

    canonical = info.get(
        "Id",
        service,
    )

    pid = info.get(
        "MainPID",
        "0",
    )

    if pid in {
        "",
        "0",
    }:
        pid = "-"

    if load_state == "not-found":
        state_text = (
            "[bright_magenta]"
            "Not installed"
            "[/bright_magenta]"
        )

    elif state == "active":
        state_text = (
            "[bright_green]"
            "Running"
            "[/bright_green]"
        )

    elif state == "failed":
        state_text = (
            "[bright_red]"
            "Failed"
            "[/bright_red]"
        )

    elif state == "inactive":
        state_text = (
            "[bright_yellow]"
            "Stopped"
            "[/bright_yellow]"
        )

    else:
        state_text = state

    canonical_text = ""

    if (
        canonical
        and canonical != service
    ):
        canonical_text = (
            f"[bold bright_white]"
            f"Canonical:"
            f"[/bold bright_white] "
            f"{canonical}\n"
        )

    content = (
        f"[bold bright_white]"
        f"Selected:"
        f"[/bold bright_white] "
        f"{service}\n"
        f"{canonical_text}"
        f"{build_symlink_details(service)}"
        f"\n\n"
        f"[bold bright_white]"
        f"Status:"
        f"[/bold bright_white] "
        f"{state_text}\n"
        f"[bold bright_white]"
        f"Load state:"
        f"[/bold bright_white] "
        f"{load_state}\n"
        f"[bold bright_white]"
        f"PID:"
        f"[/bold bright_white] "
        f"{pid}\n"
        f"[bold bright_white]"
        f"Memory:"
        f"[/bold bright_white] "
        f"{format_memory(get_service_memory(info))}\n"
        f"[bold bright_white]"
        f"Restarts:"
        f"[/bold bright_white] "
        f"{info.get('NRestarts', '0')}\n"
        f"[bold bright_white]"
        f"Started:"
        f"[/bold bright_white] "
        f"{format_start_time(info.get('ActiveEnterTimestamp', ''))}"
        f"\n\n"
        f"[bold bright_white]"
        f"Last error:"
        f"[/bold bright_white]\n"
        f"{selected_error_text}"
    )

    return Panel(
        content,
        title="Service Details",
        border_style="white",
    )


def build_logs_panel(service):
    subtitle = Text()

    subtitle.append(
        "[l]",
        style="bold bright_cyan",
    )

    subtitle.append(
        " Back to details",
        style="white",
    )

    return Panel(
        Text(
            selected_log_text,
            style="bright_white",
        ),
        title=f"Logs: {service}",
        subtitle=subtitle,
        border_style="white",
    )


def build_footer():
    footer = Text()

    shortcuts = [
        ("[↑/↓]", " Select   "),
        ("[d]", " Source   "),
        ("[c]", " Create   "),
        ("[D]", " Delete   "),
        ("[f]", " Refresh   "),
        ("[r]", " Restart   "),
        ("[s]", " Start   "),
        ("[x]", " Stop   "),
        ("[l]", " Logs   "),
        ("[q]", " Quit"),
    ]

    for key, label in shortcuts:
        footer.append(
            key,
            style="bold bright_cyan",
        )

        footer.append(
            label,
            style="bright_white",
        )

    return footer


def build_dashboard():
    elements = []

    if services:
        elements.append(
            build_service_table()
        )

        elements.append("")

        service = services[
            selected_index
        ]

        if show_logs:
            elements.append(
                build_logs_panel(
                    service
                )
            )

        else:
            elements.append(
                build_detail_panel(
                    service
                )
            )

    else:
        elements.append(
            build_empty_panel()
        )

    if status_message:
        elements.append("")

        status = Text()

        status.append(
            "Status: ",
            style="bold bright_white",
        )

        status.append(
            status_message,
            style="bright_white",
        )

        elements.append(status)

    elements.append("")
    elements.append(
        build_footer()
    )

    return Group(
        *elements
    )


# ============================================================
# Keyboard
# ============================================================

def read_key(timeout=0.08):
    readable, _, _ = (
        select.select(
            [sys.stdin],
            [],
            [],
            timeout,
        )
    )

    if not readable:
        return None

    fd = sys.stdin.fileno()

    first = os.read(
        fd,
        1,
    )

    if first == b"\x1b":
        sequence = first

        deadline = (
            time.monotonic()
            + 0.03
        )

        while (
            len(sequence) < 3
            and time.monotonic()
            < deadline
        ):
            remaining = max(
                0,
                deadline
                - time.monotonic(),
            )

            ready, _, _ = (
                select.select(
                    [sys.stdin],
                    [],
                    [],
                    remaining,
                )
            )

            if not ready:
                break

            sequence += os.read(
                fd,
                1,
            )

        if sequence == b"\x1b[A":
            return "UP"

        if sequence == b"\x1b[B":
            return "DOWN"

        return "ESC"

    try:
        # Keep uppercase because D = Delete.
        return first.decode(
            "utf-8"
        )

    except UnicodeDecodeError:
        return None


# ============================================================
# Dashboard keyboard actions
# ============================================================

def handle_key(key):
    global selected_index
    global show_logs
    global status_message

    global last_status_refresh
    global last_log_refresh
    global selected_log_text

    if key in {
        "UP",
        "DOWN",
        "ESC",
    }:
        normalized = key

    else:
        normalized = (
            key.lower()
        )

    # Quit
    if normalized == "q":
        return False

    # Change source
    if normalized == "d":
        switch_service_source()
        return True

    # Refresh
    if normalized == "f":
        refresh_service_list(
            force=True
        )

        last_status_refresh = 0.0

        refresh_service_data(
            force=True
        )

        reset_selected_journal_cache()

        status_message = (
            "Refreshed"
        )

        return True

    if not services:
        return True

    # Previous service
    if normalized == "UP":
        selected_index = (
            selected_index - 1
        ) % len(services)

        show_logs = False
        status_message = ""

        reset_selected_journal_cache()

        return True

    # Next service
    if normalized == "DOWN":
        selected_index = (
            selected_index + 1
        ) % len(services)

        show_logs = False
        status_message = ""

        reset_selected_journal_cache()

        return True

    service = services[
        selected_index
    ]

    # Logs
    if normalized == "l":
        show_logs = (
            not show_logs
        )

        if show_logs:
            selected_log_text = (
                "Loading..."
            )

            last_log_refresh = 0.0

        return True

    # Restart
    if normalized == "r":
        control_service(
            service,
            "restart",
        )
        return True

    # Start
    if normalized == "s":
        control_service(
            service,
            "start",
        )
        return True

    # Stop
    if normalized == "x":
        control_service(
            service,
            "stop",
        )
        return True

    return True


# ============================================================
# Modal create/delete
# ============================================================

def run_modal_action(
    live,
    console,
    fd,
    normal_settings,
    action,
):
    global status_message
    global last_status_refresh
    global last_service_list_refresh

    # Leave the dashboard.
    live.stop()

    # Restore normal line-input mode.
    termios.tcsetattr(
        fd,
        termios.TCSADRAIN,
        normal_settings,
    )

    console.clear()

    message = None

    try:
        message = action(
            console,
            fd,
            normal_settings,
        )

    except KeyboardInterrupt:
        console.print()

        console.print(
            "Operation cancelled.",
            style="bold yellow",
        )

        pause_any_key(
            fd,
            normal_settings,
        )

        message = (
            "Operation cancelled"
        )

    except Exception as exc:
        console.print()

        console.print(
            "Unexpected error:",
            style="bold bright_red",
        )

        console.print(
            str(exc),
            style="bright_white",
        )

        pause_any_key(
            fd,
            normal_settings,
        )

        message = (
            "Operation failed"
        )

    if message:
        status_message = message

    # Refresh caches after create/delete.
    last_service_list_refresh = 0.0
    last_status_refresh = 0.0

    refresh_service_list(
        force=True
    )

    refresh_service_data(
        force=True
    )

    reset_selected_journal_cache()

    try:
        termios.tcflush(
            fd,
            termios.TCIFLUSH,
        )

    except termios.error:
        pass

    # Return to one-key mode.
    tty.setcbreak(fd)

    live.update(
        build_dashboard(),
        refresh=False,
    )

    live.start(
        refresh=True
    )


# ============================================================
# Main
# ============================================================

def main():
    # highlight=False is important.
    #
    # Without this, Rich automatically colors things that
    # look like paths, numbers, IP addresses, etc.
    # That was responsible for the dark purple path text.
    console = Console(
        highlight=False
    )

    if not sys.stdin.isatty():
        print(
            "This dashboard must be run "
            "inside an interactive terminal."
        )
        return

    refresh_service_list(
        force=True
    )

    refresh_service_data(
        force=True
    )

    fd = sys.stdin.fileno()

    normal_settings = (
        termios.tcgetattr(fd)
    )

    live = Live(
        build_dashboard(),
        console=console,
        screen=True,
        auto_refresh=False,
    )

    try:
        tty.setcbreak(fd)

        live.start(
            refresh=True
        )

        running = True

        while running:
            key = read_key()

            # =================================================
            # Keyboard gets priority
            # =================================================

            if key is not None:

                # Shift + D = Delete
                if key == "D":
                    run_modal_action(
                        live,
                        console,
                        fd,
                        normal_settings,
                        delete_service_wizard,
                    )

                    continue

                # c / C = Create
                if (
                    key not in {
                        "UP",
                        "DOWN",
                        "ESC",
                    }
                    and
                    key.lower() == "c"
                ):
                    run_modal_action(
                        live,
                        console,
                        fd,
                        normal_settings,
                        create_service_wizard,
                    )

                    continue

                running = handle_key(
                    key
                )

                live.update(
                    build_dashboard(),
                    refresh=True,
                )

                continue

            # =================================================
            # Background refresh
            # =================================================

            changed = False

            if refresh_service_data():
                changed = True

            if show_logs:
                if refresh_selected_logs():
                    changed = True

            else:
                if refresh_selected_error():
                    changed = True

            if changed:
                live.update(
                    build_dashboard(),
                    refresh=True,
                )

    finally:
        try:
            live.stop()

        except Exception:
            pass

        termios.tcsetattr(
            fd,
            termios.TCSADRAIN,
            normal_settings,
        )

        console.clear()


if __name__ == "__main__":
    main()