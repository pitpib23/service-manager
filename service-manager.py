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

SERVICE_SOURCES = [
    {"name": "~/Services", "path": Path.home() / "Services"},
    {"name": "/etc/systemd/system", "path": Path("/etc/systemd/system")},
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
    "UnitFileState",
    "ExecStart",
    "FragmentPath",
]

SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")

source_index = 0
selected_index = 0
show_logs = False
status_message = ""
status_details = []
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


def run_command(command, timeout=5):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out"
    except Exception as exc:
        return 1, "", str(exc)


def run_interactive_command(command):
    try:
        return subprocess.run(command).returncode == 0
    except Exception as exc:
        print(f"Command failed: {exc}")
        return False


def run_admin_command(command):
    if os.geteuid() == 0:
        return run_interactive_command(command)
    return run_interactive_command(["sudo", *command])


def path_lexists(path):
    return os.path.lexists(str(path))


def is_systemd_path(path):
    try:
        parent = path.parent.resolve(strict=False)
        root = SYSTEMD_DIR.resolve(strict=False)
        return parent == root or root in parent.parents
    except OSError:
        return False


def can_write_directory(directory):
    return os.geteuid() == 0 or os.access(directory, os.W_OK)


def write_service_file(destination, content):
    parent = destination.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            if not run_admin_command(["mkdir", "-p", str(parent)]):
                return False

    if can_write_directory(parent):
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=False, dir=str(parent),
                prefix=f".{destination.name}.", suffix=".tmp"
            ) as tmp:
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = Path(tmp.name)
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, destination)
            return True
        except Exception as exc:
            print(f"Failed to create service: {exc}")
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            return False

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False,
            prefix="service-dashboard-", suffix=".service"
        ) as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        return run_admin_command([
            "install", "-m", "0644", str(tmp_path), str(destination)
        ])
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def create_symlink(source, destination):
    if not destination.parent.exists():
        print("\nDestination directory does not exist:")
        print(destination.parent)
        return False
    if path_lexists(destination):
        print("\nDestination already exists:")
        print(destination)
        return False
    if can_write_directory(destination.parent):
        try:
            os.symlink(str(source), str(destination))
            return True
        except OSError as exc:
            print(f"Symlink failed: {exc}")
            return False
    return run_admin_command(["ln", "-s", "--", str(source), str(destination)])


def delete_path(path):
    if can_write_directory(path.parent):
        try:
            os.unlink(path)
            return True
        except OSError as exc:
            print(f"Delete failed: {exc}")
            return False
    return run_admin_command(["rm", "--", str(path)])


def daemon_reload():
    return run_admin_command(["systemctl", "daemon-reload"])


def get_current_source():
    return SERVICE_SOURCES[source_index]


def load_services():
    global service_metadata
    directory = get_current_source()["path"]
    found = []
    metadata = {}
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.endswith(".service"):
                    continue
                is_link = entry.is_symlink()
                try:
                    is_file = entry.is_file(follow_symlinks=True)
                except OSError:
                    is_file = False
                if not (is_file or is_link):
                    continue
                path = Path(entry.path)
                found.append(entry.name)
                info = {
                    "path": str(path),
                    "is_symlink": is_link,
                    "link_source": None,
                    "resolved_target": None,
                    "target_name": None,
                }
                if is_link:
                    try:
                        info["link_source"] = os.readlink(path)
                    except OSError:
                        pass
                    try:
                        resolved = path.resolve(strict=False)
                        info["resolved_target"] = str(resolved)
                        info["target_name"] = resolved.name
                    except OSError:
                        pass
                metadata[entry.name] = info
    except (FileNotFoundError, PermissionError, OSError):
        service_metadata = {}
        return []
    service_metadata = metadata
    return sorted(set(found), key=str.lower)


def refresh_service_list(force=False):
    global services, selected_index, last_service_list_refresh
    now = time.monotonic()
    if not force and now - last_service_list_refresh < SERVICE_LIST_REFRESH_INTERVAL:
        return False
    last_service_list_refresh = now
    previous = None
    if services and 0 <= selected_index < len(services):
        previous = services[selected_index]
    new_services = load_services()
    changed = new_services != services
    services = new_services
    if not services:
        selected_index = 0
        return changed
    if previous in services:
        selected_index = services.index(previous)
    else:
        selected_index = min(selected_index, len(services) - 1)
    return changed


def switch_service_source():
    global source_index, selected_index, show_logs, status_message, status_details
    global service_cache, pid_memory_cache, last_status_refresh, last_service_list_refresh
    source_index = (source_index + 1) % len(SERVICE_SOURCES)
    selected_index = 0
    show_logs = False
    status_details = []
    service_cache = {}
    pid_memory_cache = {}
    last_status_refresh = 0.0
    last_service_list_refresh = 0.0
    reset_selected_journal_cache()
    refresh_service_list(force=True)
    refresh_service_data(force=True)
    status_message = f"Source changed to {get_current_source()['name']}"


def refresh_service_data(force=False):
    global service_cache, last_status_refresh
    now = time.monotonic()
    if not force and now - last_status_refresh < STATUS_REFRESH_INTERVAL:
        return False
    refresh_service_list()
    last_status_refresh = now
    if not services:
        service_cache = {}
        pid_memory_cache.clear()
        return True

    query_units = list(services)
    for metadata in service_metadata.values():
        target = metadata.get("target_name")
        if target and target.endswith(".service"):
            query_units.append(target)
    query_units = list(dict.fromkeys(query_units))

    command = ["systemctl", "show", "--no-pager"]
    for prop in SYSTEMD_PROPERTIES:
        command.append(f"--property={prop}")
    command.append("--")
    command.extend(query_units)

    _, stdout, _ = run_command(command, timeout=10)
    cache = {}
    blocks = re.split(r"\n\s*\n", stdout.strip()) if stdout else []
    for block in blocks:
        if not block.strip():
            continue
        info = {}
        for line in block.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            info[key] = value
        canonical = info.get("Id", "").strip()
        if not canonical:
            continue
        cache[canonical] = info
        for name in info.get("Names", "").split():
            if name.endswith(".service"):
                cache[name] = info
    service_cache = cache
    refresh_pid_memory()
    return True


def get_service_info(service):
    direct = service_cache.get(service)
    if direct is not None and direct.get("LoadState") != "not-found":
        return direct

    metadata = service_metadata.get(service, {})
    target = metadata.get("target_name")
    if target:
        target_info = service_cache.get(target)
        if target_info is not None and target_info.get("LoadState") != "not-found":
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
        "UnitFileState": "unknown",
        "ExecStart": "",
        "FragmentPath": "",
    }


def get_selected_entry_path(service):
    metadata = service_metadata.get(service, {})
    raw = metadata.get("path")
    return Path(raw) if raw else None


def get_service_file_path(service):
    entry = get_selected_entry_path(service)
    if entry is None or not path_lexists(entry):
        return None
    try:
        return entry.resolve(strict=False)
    except OSError:
        try:
            return entry.absolute()
        except OSError:
            return None


def get_systemd_fragment_path(service):
    info = service_cache.get(service)
    if not info or info.get("LoadState") == "not-found":
        return None
    fragment = info.get("FragmentPath", "").strip()
    if not fragment:
        return None
    try:
        return Path(fragment).resolve(strict=False)
    except OSError:
        return Path(fragment)


def get_service_relation(service):
    """
    match         systemd is using the selected file
    not-installed systemd does not know this unit yet
    conflict      same unit name points to a different file
    unknown       cannot safely determine
    """
    selected = get_service_file_path(service)
    fragment = get_systemd_fragment_path(service)
    info = service_cache.get(service)

    if info is None or info.get("LoadState") == "not-found":
        return "not-installed"
    if selected is None or fragment is None:
        return "unknown"
    try:
        if selected.resolve(strict=False) == fragment.resolve(strict=False):
            return "match"
    except OSError:
        if str(selected) == str(fragment):
            return "match"
    return "conflict"


def format_exec_start(value):
    if not value:
        return "-"
    match = re.search(r"argv\[\]=([^;}]+)", value)
    if match:
        command = match.group(1).strip()
        if command:
            return command
    match = re.search(r"path=([^;}]+)", value)
    if match:
        command = match.group(1).strip()
        if command:
            return command
    return value


def parse_memory(value):
    try:
        value = int(value)
        if 0 < value < 10**15:
            return value
    except (TypeError, ValueError):
        pass
    return None


def refresh_pid_memory():
    global pid_memory_cache
    needed = set()
    seen = set()
    for info in service_cache.values():
        info_id = id(info)
        if info_id in seen:
            continue
        seen.add(info_id)
        if parse_memory(info.get("MemoryCurrent")) is not None:
            continue
        try:
            pid = int(info.get("MainPID", "0"))
        except (TypeError, ValueError):
            continue
        if pid > 0:
            needed.add(pid)

    if not needed:
        pid_memory_cache = {}
        return

    pid_argument = ",".join(str(pid) for pid in sorted(needed))
    rc, stdout, _ = run_command([
        "ps", "-o", "pid=,rss=", "-p", pid_argument
    ], timeout=5)
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
            cache[pid] = rss_kib * 1024
        except ValueError:
            continue
    pid_memory_cache = cache


def get_cgroup_memory(info):
    control_group = info.get("ControlGroup", "")
    if not control_group:
        return None
    memory_file = Path("/sys/fs/cgroup") / control_group.lstrip("/") / "memory.current"
    try:
        return parse_memory(memory_file.read_text().strip())
    except (FileNotFoundError, PermissionError, OSError):
        return None


def get_service_memory(info):
    memory = parse_memory(info.get("MemoryCurrent"))
    if memory is not None:
        return memory
    memory = get_cgroup_memory(info)
    if memory is not None:
        return memory
    try:
        pid = int(info.get("MainPID", "0"))
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    return pid_memory_cache.get(pid)


def format_memory(value):
    if value is None or value <= 0:
        return "-"
    if value < 1024:
        return f"{value} B"
    if value < 1024**2:
        return f"{value / 1024:.1f} KB"
    if value < 1024**3:
        return f"{value / (1024**2):.1f} MB"
    return f"{value / (1024**3):.2f} GB"


def reset_selected_journal_cache():
    global last_error_refresh, last_log_refresh
    global selected_error_text, selected_log_text
    last_error_refresh = 0.0
    last_log_refresh = 0.0
    selected_error_text = "Loading..."
    selected_log_text = "Loading..."


def refresh_selected_error(force=False):
    global last_error_refresh, selected_error_text
    if not services:
        selected_error_text = "No service selected"
        return False
    now = time.monotonic()
    if not force and now - last_error_refresh < ERROR_REFRESH_INTERVAL:
        return False
    last_error_refresh = now
    service = services[selected_index]
    relation = get_service_relation(service)
    if relation == "conflict":
        selected_error_text = "Not queried: unit name conflicts with another systemd file."
        return True
    _, stdout, stderr = run_command([
        "journalctl", "-u", service, "-p", "err", "-n", "3",
        "--no-pager", "--output=short"
    ], timeout=5)
    selected_error_text = stdout or stderr or "No recent errors"
    return True


def refresh_selected_logs(force=False):
    global last_log_refresh, selected_log_text
    if not services or not show_logs:
        return False
    now = time.monotonic()
    if not force and now - last_log_refresh < LOG_REFRESH_INTERVAL:
        return False
    last_log_refresh = now
    service = services[selected_index]
    relation = get_service_relation(service)
    if relation == "conflict":
        selected_log_text = "Logs hidden because this service name resolves to a different systemd file."
        return True
    _, stdout, stderr = run_command([
        "journalctl", "-u", service, "-n", "20", "--no-pager", "--output=short"
    ], timeout=5)
    selected_log_text = stdout or stderr or "No logs available"
    return True


def run_noninteractive_systemctl(arguments, timeout=15):
    rc, stdout, stderr = run_command([
        "systemctl", "--no-ask-password", *arguments
    ], timeout=timeout)
    if rc == 0:
        return rc, stdout, stderr

    error_text = (stderr or stdout or "").lower()
    permission_errors = (
        "authentication",
        "permission denied",
        "access denied",
        "not authorized",
        "interactive authentication required",
    )
    if any(text in error_text for text in permission_errors):
        return run_command([
            "sudo", "-n", "systemctl", *arguments
        ], timeout=timeout)
    return rc, stdout, stderr


def extract_systemctl_link_lines(stdout, stderr):
    combined = "\n".join(text for text in (stdout, stderr) if text)
    found = []
    for line in combined.splitlines():
        line = line.strip()
        if "Created symlink" in line or line.startswith("Removed "):
            found.append(line)
    return found


def action_conflict_details(service):
    selected = get_service_file_path(service)
    fragment = get_systemd_fragment_path(service)
    details = ["Action blocked: the same unit name points to a different file."]
    if selected:
        details.append(f"Selected file: {selected}")
    if fragment:
        details.append(f"Systemd file: {fragment}")
    details.append("Rename one of the units or remove the name conflict first.")
    return details


def run_systemctl_action(service, action):
    relation = get_service_relation(service)
    file_path = get_service_file_path(service)

    if relation == "conflict":
        return 2, "", "Unit name conflict", service, False

    target = service
    used_file_path = False

    if action == "enable":
        if relation != "match":
            if file_path is None:
                return 1, "", "Service file path is unavailable", service, False
            target = str(file_path)
            used_file_path = True

    elif action == "disable":
        if relation != "match" and file_path is not None:
            target = str(file_path)
            used_file_path = True

    elif action in {"start", "stop", "restart"}:
        if relation != "match":
            return 1, "", "Service is not installed in systemd. Enable it first.", service, False

    rc, stdout, stderr = run_noninteractive_systemctl([action, target], timeout=15)
    return rc, stdout, stderr, target, used_file_path


def dashboard_daemon_reload():
    global status_message, status_details
    global last_status_refresh, last_service_list_refresh
    status_details = []
    rc, stdout, stderr = run_noninteractive_systemctl(["daemon-reload"], timeout=15)
    if rc == 0:
        status_message = "Daemon reload successful"
        status_details.append("systemd unit files reloaded.")
    else:
        error = (stderr or stdout or "Unknown error").replace("\n", " ")
        status_message = f"Daemon reload failed: {error}"
    last_status_refresh = 0.0
    last_service_list_refresh = 0.0
    refresh_service_list(force=True)
    refresh_service_data(force=True)


def control_service(service, action):
    global status_message, status_details
    global last_status_refresh, last_service_list_refresh

    if action not in {"start", "stop", "restart", "enable", "disable"}:
        return

    status_details = []
    relation = get_service_relation(service)
    if relation == "conflict":
        status_message = f"{action.capitalize()} blocked: {service}"
        status_details = action_conflict_details(service)
        return

    rc, stdout, stderr, actual_target, used_file_path = run_systemctl_action(service, action)

    if rc == 0:
        status_message = f"{action.capitalize()} successful: {service}"

        if action == "enable":
            if used_file_path:
                status_details.append(f"Service file: {actual_target}")
            link_lines = extract_systemctl_link_lines(stdout, stderr)
            if link_lines:
                status_details.append("Created symlinks:")
                status_details.extend(link_lines)
            reload_rc, reload_stdout, reload_stderr = run_noninteractive_systemctl(["daemon-reload"], timeout=15)
            if reload_rc == 0:
                status_details.append("systemd daemon reloaded.")
            else:
                reload_error = (reload_stderr or reload_stdout or "Unknown error").replace("\n", " ")
                status_details.append(f"Warning: daemon-reload failed: {reload_error}")

        elif action == "disable":
            link_lines = extract_systemctl_link_lines(stdout, stderr)
            if link_lines:
                status_details.append("Removed symlinks:")
                status_details.extend(link_lines)
            reload_rc, _, _ = run_noninteractive_systemctl(["daemon-reload"], timeout=15)
            if reload_rc == 0:
                status_details.append("systemd daemon reloaded.")

    else:
        error = (stderr or stdout or "Unknown error").replace("\n", " ")
        if len(error) > 180:
            error = error[:180] + "..."
        status_message = f"{action.capitalize()} failed: {error}"

    last_status_refresh = 0.0
    last_service_list_refresh = 0.0
    refresh_service_list(force=True)
    refresh_service_data(force=True)
    reset_selected_journal_cache()


def normalize_service_name(name):
    name = name.strip()
    if not name.endswith(".service"):
        name += ".service"
    return name


def valid_service_name(name):
    return bool(SERVICE_NAME_RE.fullmatch(name))


def prompt_yes_no(message, default=False):
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        value = input(message + suffix).strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please enter y or n.")


def creation_header(console, step, title, service_name=None, file_path=None):
    console.clear()
    console.rule("[bold bright_cyan]Create Service[/bold bright_cyan]")
    header = Text()
    header.append(f"{step}/4", style="bold bright_cyan")
    header.append("  ")
    header.append(title, style="bold bright_white")
    console.print()
    console.print(header)
    if service_name:
        line = Text()
        line.append("Service: ", style="bold white")
        line.append(service_name, style="bright_white")
        console.print(line)
    if file_path:
        line = Text()
        line.append("Path: ", style="bold white")
        line.append(str(file_path), style="bright_white")
        console.print(line)
    console.print()


def print_value(console, label, value):
    line = Text()
    line.append(f"{label}: ", style="bold bright_white")
    if value:
        line.append(str(value), style="bright_white")
    else:
        line.append("-", style="white")
    console.print(line)


def print_optional_prompt(console, title, example=None, extra=None):
    line = Text()
    line.append(title, style="bold bright_white")
    line.append(" (optional)", style="yellow")
    console.print(line)
    if extra:
        console.print(extra, style="white")
    if example:
        console.print(f"Example: {example}", style="bright_white")


def print_required_prompt(console, title, example=None):
    line = Text()
    line.append(title, style="bold bright_white")
    line.append(" (required)", style="bold bright_red")
    console.print(line)
    if example:
        console.print(f"Example: {example}", style="bright_white")


def show_settings_progress(
    console, service_name, file_path,
    description=None, dependency=None, working_directory=None,
    include_description=False, include_dependency=False,
    include_working_directory=False,
):
    creation_header(console, 3, "Service settings", service_name, file_path)
    console.print("Press Enter to skip optional fields.", style="white")
    console.print()
    if include_description:
        print_value(console, "Description", description)
    if include_dependency:
        print_value(console, "Requires / After", dependency)
    if include_working_directory:
        print_value(console, "WorkingDirectory", working_directory)
    if include_description or include_dependency or include_working_directory:
        console.print()


def prompt_working_directory(console, service_name, file_path, description, dependency):
    while True:
        show_settings_progress(
            console, service_name, file_path,
            description=description, dependency=dependency,
            include_description=True, include_dependency=True,
        )
        print_optional_prompt(console, "WorkingDirectory", example="/home/pi")
        value = input("> ").strip()
        if not value:
            return ""
        path = Path(value)
        if not path.is_absolute():
            console.print("\nWorkingDirectory must use an absolute path.", style="bold bright_red")
            input("\nPress Enter to try again...")
            continue
        if not path.exists():
            console.print(f"\nDirectory does not exist: {path}", style="bold bright_red")
            input("\nPress Enter to try again...")
            continue
        if not path.is_dir():
            console.print(f"\nNot a directory: {path}", style="bold bright_red")
            input("\nPress Enter to try again...")
            continue
        return str(path.resolve())


def exec_start_error(value):
    if not value.strip():
        return "ExecStart is required."
    try:
        parts = shlex.split(value)
    except ValueError as exc:
        return f"Invalid command: {exc}"
    if not parts:
        return "ExecStart is required."
    executable = Path(parts[0])
    if not executable.is_absolute():
        return "The executable must use an absolute path."
    if not executable.exists():
        return f"Executable does not exist:\n{executable}"
    if not executable.is_file():
        return f"Executable is not a file:\n{executable}"
    if not os.access(executable, os.X_OK):
        return f"Executable is not executable:\n{executable}"
    for argument in parts[1:]:
        if argument.startswith("-"):
            continue
        if argument.startswith(("~/", "./", "../")):
            return f"Use absolute paths for files in ExecStart.\nRelative path: {argument}"
        suffix = Path(argument).suffix.lower()
        if suffix in {".py", ".sh", ".js", ".jar"} and not Path(argument).is_absolute():
            return f"Script paths must be absolute.\nRelative path: {argument}"
    return None


def build_service_content(description, dependency, working_directory, exec_start):
    lines = ["[Unit]"]
    if description:
        lines.append(f"Description={description}")
    if dependency:
        lines.append(f"Requires={dependency}")
        lines.append(f"After={dependency}")
    lines.extend(["", "[Service]", "Type=simple", "User=pi", "Group=pi"])
    if working_directory:
        lines.append(f"WorkingDirectory={working_directory}")
    lines.extend([
        f"ExecStart={exec_start}",
        "Restart=on-failure",
        "RestartSec=5s",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])
    return "\n".join(lines)


def pause_any_key(fd, normal_settings):
    print("\nPress any key to return to dashboard...", end="", flush=True)
    try:
        tty.setcbreak(fd)
        os.read(fd, 1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, normal_settings)
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error:
            pass


def create_service_wizard(console, fd, normal_settings):
    creation_header(console, 1, "Service name")
    console.print("Enter a name for the service.", style="bright_white")
    console.print("Example: sensor-reader.service", style="white")
    console.print()

    while True:
        raw_name = input("Service name: ").strip()
        if not raw_name:
            console.print("Service name is required.", style="bold bright_red")
            continue
        service_name = normalize_service_name(raw_name)
        if valid_service_name(service_name):
            break
        console.print("Invalid service name.", style="bold bright_red")
        console.print("Example: sensor-reader.service", style="bright_white")

    creation_header(console, 2, "Save location", service_name=service_name)
    console.print("[bold bright_cyan]1[/bold bright_cyan]  [bright_white]~/Services[/bright_white]")
    console.print(str(SERVICE_SOURCES[0]["path"]), style="white")
    console.print()
    console.print("[bold bright_cyan]2[/bold bright_cyan]  [bright_white]/etc/systemd/system[/bright_white]")
    console.print(str(SERVICE_SOURCES[1]["path"]), style="white")
    console.print()

    while True:
        choice = input("Choose [1/2]: ").strip()
        if choice == "1":
            source = SERVICE_SOURCES[0]
            break
        if choice == "2":
            source = SERVICE_SOURCES[1]
            break
        console.print("Please enter 1 or 2.", style="bold bright_red")

    file_path = source["path"] / service_name
    if path_lexists(file_path):
        console.print("\nService already exists:", style="bold bright_red")
        console.print(str(file_path), style="bright_white")
        pause_any_key(fd, normal_settings)
        return "Create cancelled"

    show_settings_progress(console, service_name, file_path)
    print_optional_prompt(console, "Description", example="Loop sensor reader service")
    description = input("> ").strip()

    show_settings_progress(
        console, service_name, file_path,
        description=description, include_description=True,
    )
    print_optional_prompt(
        console, "Requires / After",
        example="network-online.target",
        extra="Enter once; the same value is used for both.",
    )
    dependency = input("> ").strip()

    working_directory = prompt_working_directory(
        console, service_name, file_path, description, dependency
    )

    while True:
        show_settings_progress(
            console, service_name, file_path,
            description=description,
            dependency=dependency,
            working_directory=working_directory,
            include_description=True,
            include_dependency=True,
            include_working_directory=True,
        )
        print_required_prompt(
            console, "ExecStart",
            example="/usr/bin/python3 /home/pi/sensor-reader.py",
        )
        exec_start = input("> ").strip()
        error = exec_start_error(exec_start)
        if error is None:
            break
        console.print(f"\n{error}", style="bold bright_red")
        input("\nPress Enter to try again...")

    content = build_service_content(
        description, dependency, working_directory, exec_start
    )

    creation_header(console, 3, "Review", service_name, file_path)
    console.print(Panel(
        Text(content, style="bright_white"),
        title="Service File",
        border_style="bright_cyan",
    ))

    if not prompt_yes_no("\nCreate this file?", default=True):
        pause_any_key(fd, normal_settings)
        return "Create cancelled"

    if not write_service_file(file_path, content):
        console.print("\nFailed to create service.", style="bold bright_red")
        pause_any_key(fd, normal_settings)
        return "Create failed"

    systemd_changed = is_systemd_path(file_path)

    creation_header(console, 4, "Symlink", service_name, file_path)
    console.print("Create an additional symlink?", style="bright_white")
    console.print("Optional. Press Enter for No.", style="white")
    console.print()

    symlink_path = None
    if prompt_yes_no("Create symlink?", default=False):
        console.print()
        console.print("Enter the full destination path.", style="bright_white")
        console.print(
            "Example: /etc/systemd/system/sensor-reader.service",
            style="bright_white",
        )
        console.print()

        while True:
            value = input("Destination: ").strip()
            candidate = Path(value)
            if not candidate.is_absolute():
                console.print("Use an absolute path.", style="bold bright_red")
                continue
            if not valid_service_name(candidate.name):
                console.print(
                    "Destination must end with a valid .service name.",
                    style="bold bright_red",
                )
                continue
            if not candidate.parent.exists():
                console.print("Destination directory does not exist.", style="bold bright_red")
                continue
            if path_lexists(candidate):
                console.print("Destination already exists.", style="bold bright_red")
                continue
            try:
                if candidate.resolve(strict=False) == file_path.resolve(strict=False):
                    console.print("Symlink cannot point to itself.", style="bold bright_red")
                    continue
            except OSError:
                pass
            symlink_path = candidate
            break

        source_absolute = file_path.resolve(strict=False)
        if not create_symlink(source_absolute, symlink_path):
            console.print(
                "\nService created, but symlink creation failed.",
                style="bold yellow",
            )
            symlink_path = None
        elif is_systemd_path(symlink_path):
            systemd_changed = True

    if systemd_changed:
        console.print("\nReloading systemd...", style="bright_white")
        if not daemon_reload():
            console.print("Warning: daemon-reload failed.", style="bold yellow")

    console.clear()
    console.rule(f"[bold bright_green]{service_name}[/bold bright_green]")
    console.print()
    console.print(Panel(
        Text(content, style="bright_white"),
        title="Service File",
        border_style="bright_green",
    ))
    console.print()
    console.print("File path:", style="bold bright_white")
    console.print(str(file_path), style="bright_white")

    if symlink_path:
        console.print()
        console.print("Symlink:", style="bold bright_white")
        console.print(str(symlink_path), style="bright_white")
        console.print("    →", style="bright_cyan")
        console.print(str(file_path.resolve(strict=False)), style="bright_white")
    elif not systemd_changed:
        console.print()
        console.print(
            "Use [e] Enable in the dashboard to install this service into systemd.",
            style="bright_cyan",
        )

    pause_any_key(fd, normal_settings)
    return f"Created {service_name}"


def find_systemd_symlinks_to_file(real_path):
    """
    Find every symlink below /etc/systemd/system that resolves
    to the selected service file. This includes:
      /etc/systemd/system/name.service
      *.wants/name.service
      *.requires/name.service
      aliases created by systemctl enable
    """
    links = []
    try:
        target = real_path.resolve(strict=False)
    except OSError:
        target = real_path

    try:
        for root, dirs, files in os.walk(SYSTEMD_DIR):
            for name in files + dirs:
                candidate = Path(root) / name
                try:
                    if not candidate.is_symlink():
                        continue
                    resolved = candidate.resolve(strict=False)
                    if resolved == target:
                        links.append(candidate)
                except OSError:
                    continue
    except OSError:
        pass

    return sorted(
        set(links),
        key=lambda p: (len(p.parts), str(p)),
        reverse=True,
    )


def remove_systemd_symlinks_to_file(real_path):
    removed = []
    failed = []
    for link in find_systemd_symlinks_to_file(real_path):
        if not path_lexists(link):
            continue
        if delete_path(link):
            removed.append(link)
        else:
            failed.append(link)
    return removed, failed


def delete_service_wizard(console, fd, normal_settings):
    if not services:
        console.clear()
        console.print("No service selected.", style="bold yellow")
        pause_any_key(fd, normal_settings)
        return "Delete cancelled"

    service_name = services[selected_index]
    metadata = service_metadata.get(service_name, {})
    entry_path = get_selected_entry_path(service_name)

    if entry_path is None:
        console.clear()
        console.print("Service file path is unavailable.", style="bold bright_red")
        pause_any_key(fd, normal_settings)
        return "Delete failed"

    is_link = metadata.get("is_symlink", False)
    real_path = get_service_file_path(service_name)
    relation = get_service_relation(service_name)
    info = get_service_info(service_name)

    console.clear()
    console.rule("[bold bright_red]Delete Service[/bold bright_red]")
    console.print()
    console.print(f"Service: {service_name}", style="bright_white")
    console.print(f"Selected path: {entry_path}", style="bright_white")

    if is_link:
        console.print("Type: Symlink", style="bright_white")
        console.print(f"Source file: {real_path or '-'}", style="bright_white")
        console.print()
        console.print(
            "The source file will NOT be deleted. The service will be disabled and systemd links to that source will be removed.",
            style="bold yellow",
        )
    else:
        console.print("Type: Service file", style="bright_white")
        console.print()
        console.print(
            "Delete will stop the matching running unit, disable it, remove systemd symlinks pointing to this file, then delete the file.",
            style="bold yellow",
        )

    if relation == "conflict":
        console.print()
        console.print(
            "WARNING: systemd currently resolves this unit name to a different file.",
            style="bold bright_red",
        )
        selected_real = get_service_file_path(service_name)
        fragment = get_systemd_fragment_path(service_name)
        if selected_real:
            console.print(f"Selected file: {selected_real}", style="bright_white")
        if fragment:
            console.print(f"Systemd file: {fragment}", style="bright_white")
        console.print(
            "The conflicting systemd unit will not be stopped or disabled. Only links that actually point to the selected file will be cleaned up.",
            style="yellow",
        )

    console.print()
    console.print("Type the service name exactly to confirm:", style="bright_white")
    console.print(service_name, style="bold bright_red")
    confirmation = input("\n> ").strip()

    if confirmation != service_name:
        console.print("\nName did not match. Deletion cancelled.", style="bold yellow")
        pause_any_key(fd, normal_settings)
        return "Delete cancelled"

    if not path_lexists(entry_path):
        console.print("\nSelected file no longer exists.", style="bold bright_red")
        pause_any_key(fd, normal_settings)
        return "Delete failed"

    cleanup_messages = []

    # Stop only if systemd is definitely using this same file.
    if relation == "match":
        active_state = info.get("ActiveState", "")
        if active_state in {"active", "activating", "reloading", "deactivating"}:
            rc, stdout, stderr = run_noninteractive_systemctl(["stop", service_name], timeout=15)
            if rc != 0:
                error = (stderr or stdout or "Unknown error").replace("\n", " ")
                console.print(
                    f"\nCould not stop the running service: {error}",
                    style="bold bright_red",
                )
                console.print(
                    "Nothing was deleted. Stop the service first, then try again.",
                    style="yellow",
                )
                pause_any_key(fd, normal_settings)
                return "Delete cancelled: stop failed"
            cleanup_messages.append("Stopped running service.")

    # Decide whether this is a custom service source that this dashboard
    # is responsible for cleaning up broadly.
    #
    # Regular files selected from ~/Services or /etc/systemd/system are
    # safe to treat as owned service definitions. A top-level /etc symlink
    # pointing into /usr/lib or /lib may be a distro/package alias, so we do
    # NOT remove every other link to that package unit.
    custom_source = not is_link
    if is_link and real_path is not None:
        try:
            services_root = (Path.home() / "Services").resolve(strict=False)
            resolved_real = real_path.resolve(strict=False)
            custom_source = (
                resolved_real == services_root
                or services_root in resolved_real.parents
            )
        except OSError:
            custom_source = False

    # Disable before deleting a custom service file so systemctl can remove
    # the symlinks it created (including multi-user.target.wants).
    #
    # For a package/system alias symlink, deleting the alias should not
    # disable the canonical distro unit or wipe unrelated aliases.
    if real_path is not None and relation != "conflict" and custom_source:
        disable_target = service_name if relation == "match" else str(real_path)
        rc, stdout, stderr = run_noninteractive_systemctl(["disable", disable_target], timeout=15)
        if rc == 0:
            lines = extract_systemctl_link_lines(stdout, stderr)
            cleanup_messages.extend(lines)
        else:
            error_text = (stderr or stdout or "").lower()
            harmless = (
                "not loaded" in error_text
                or "does not exist" in error_text
                or "not enabled" in error_text
            )
            if not harmless:
                cleanup_messages.append(
                    "Warning: systemctl disable failed; manual symlink cleanup will continue."
                )

    # Remove every remaining systemd symlink that points to a custom service
    # file. This catches links left in multi-user.target.wants, *.requires,
    # aliases, and the top-level /etc/systemd/system/name.service link.
    removed_links = []
    failed_links = []
    if real_path is not None and custom_source:
        removed_links, failed_links = remove_systemd_symlinks_to_file(real_path)

    if failed_links:
        console.print("\nCould not remove these systemd symlinks:", style="bold bright_red")
        for link in failed_links:
            console.print(str(link), style="bright_white")
        console.print(
            "The service file was NOT deleted, so no broken systemd links are left intentionally.",
            style="yellow",
        )
        pause_any_key(fd, normal_settings)
        return "Delete cancelled: symlink cleanup failed"

    # The selected symlink may already have been removed by disable/manual cleanup.
    if path_lexists(entry_path):
        if not delete_path(entry_path):
            console.print("\nFailed to delete the selected path.", style="bold bright_red")
            pause_any_key(fd, normal_settings)
            return "Delete failed"

    # Reload after all filesystem changes.
    reload_rc, reload_stdout, reload_stderr = run_noninteractive_systemctl(["daemon-reload"], timeout=15)
    if reload_rc != 0:
        reload_error = (reload_stderr or reload_stdout or "Unknown error").replace("\n", " ")
        cleanup_messages.append(f"Warning: daemon-reload failed: {reload_error}")

    # Best effort: clear stale failed state only if there was no name conflict.
    if relation != "conflict":
        run_noninteractive_systemctl(["reset-failed", service_name], timeout=10)

    console.print()
    console.print(f"Deleted: {service_name}", style="bold bright_green")

    if is_link:
        console.print("Source file kept:", style="bold bright_white")
        console.print(str(real_path or "-"), style="bright_white")
    else:
        console.print("Deleted file:", style="bold bright_white")
        console.print(str(entry_path), style="bright_white")

    all_removed = []
    for line in cleanup_messages:
        if line.startswith("Removed "):
            all_removed.append(line)
    all_removed.extend(f"Removed symlink {link}" for link in removed_links)

    if all_removed:
        console.print()
        console.print("Systemd links removed:", style="bold bright_cyan")
        for line in all_removed:
            console.print(line, style="bright_white")

    warnings = [line for line in cleanup_messages if line.startswith("Warning:")]
    if warnings:
        console.print()
        for warning in warnings:
            console.print(warning, style="yellow")

    pause_any_key(fd, normal_settings)
    return f"Deleted {service_name}"


def format_start_time(timestamp):
    if not timestamp:
        return "-"
    match = re.search(r"\b(\d{2}:\d{2}:\d{2})\b", timestamp)
    if match:
        return match.group(1)
    return timestamp


def get_state_display(service, info):
    relation = get_service_relation(service)
    if relation == "conflict":
        return Text("⚠ conflict", style="bold bright_red")

    load_state = info.get("LoadState", "unknown")
    state = info.get("ActiveState", "unknown")

    if load_state == "not-found":
        return Text("✕ not installed", style="bright_magenta")
    if state == "active":
        return Text("● running", style="bright_green")
    if state == "failed":
        return Text("✕ failed", style="bright_red")
    if state == "inactive":
        return Text("○ stopped", style="bright_yellow")
    if state == "activating":
        return Text("◐ starting", style="bright_cyan")
    if state == "deactivating":
        return Text("◐ stopping", style="bright_yellow")
    return Text(f"? {state}", style="bright_white")


def format_startup_state(state):
    if not state:
        return "-"
    if state == "enabled":
        return "[bright_green]Enabled[/bright_green]"
    if state == "enabled-runtime":
        return "[bright_green]Enabled (runtime)[/bright_green]"
    if state == "disabled":
        return "[bright_yellow]Disabled[/bright_yellow]"
    if state == "masked":
        return "[bright_red]Masked[/bright_red]"
    if state == "static":
        return "[bright_white]Static[/bright_white]"
    if state == "indirect":
        return "[bright_white]Indirect[/bright_white]"
    if state == "generated":
        return "[bright_white]Generated[/bright_white]"
    return f"[bright_white]{state}[/bright_white]"


def get_visible_range():
    total = len(services)
    if total <= MAX_VISIBLE_SERVICES:
        return 0, total
    half = MAX_VISIBLE_SERVICES // 2
    start = max(0, selected_index - half)
    end = start + MAX_VISIBLE_SERVICES
    if end > total:
        end = total
        start = max(0, end - MAX_VISIBLE_SERVICES)
    return start, end


def get_link_display(service):
    metadata = service_metadata.get(service, {})
    if not metadata.get("is_symlink"):
        return "-"
    target = metadata.get("target_name")
    return f"→ {target}" if target else "→ ?"


def build_symlink_details(service):
    metadata = service_metadata.get(service, {})
    path = metadata.get("path", "-")
    if not metadata.get("is_symlink"):
        return (
            f"[bold bright_white]File:[/bold bright_white] {path}\n"
            f"[bold bright_white]Symlink:[/bold bright_white] No"
        )
    link_source = metadata.get("link_source") or "-"
    resolved = metadata.get("resolved_target") or "-"
    return (
        f"[bold bright_white]File:[/bold bright_white] {path}\n"
        f"[bold bright_white]Symlink:[/bold bright_white] [bright_cyan]Yes[/bright_cyan]\n"
        f"[bold bright_white]Link source:[/bold bright_white] {link_source}\n"
        f"[bold bright_white]Resolved:[/bold bright_white] {resolved}"
    )


def make_source_subtitle():
    source = get_current_source()
    text = Text()
    text.append(f"Source: {source['name']}", style="bright_white")
    if services:
        text.append(f"  {selected_index + 1}/{len(services)}", style="white")
    text.append("    ")
    text.append("[d]", style="bold bright_cyan")
    text.append(" Change source", style="white")
    return text


def build_service_table():
    table = Table(
        show_header=True,
        header_style="bold bright_white",
        box=None,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("", width=2)
    table.add_column("Service", ratio=2)
    table.add_column("Status", width=18)
    table.add_column("Memory", width=12, justify="right")
    table.add_column("Link source", ratio=1)

    start, end = get_visible_range()
    for index in range(start, end):
        service = services[index]
        info = get_service_info(service)
        selector = ">" if index == selected_index else ""
        style = "bold bright_white" if index == selected_index else "white"
        table.add_row(
            selector,
            service,
            get_state_display(service, info),
            format_memory(get_service_memory(info)),
            get_link_display(service),
            style=style,
        )

    return Panel(
        table,
        title="Services",
        subtitle=make_source_subtitle(),
        border_style="white",
    )


def build_empty_panel():
    source = get_current_source()
    text = Text()
    text.append("No .service files found in:\n\n", style="bright_white")
    text.append(str(source["path"]), style="bright_white")
    text.append("\n\n")
    text.append("[c]", style="bold bright_cyan")
    text.append(" Create service    ", style="white")
    text.append("[d]", style="bold bright_cyan")
    text.append(" Change source", style="white")
    return Panel(text, title="Services", border_style="white")


def build_detail_panel(service):
    info = get_service_info(service)
    relation = get_service_relation(service)
    state = info.get("ActiveState", "unknown")
    load_state = info.get("LoadState", "unknown")
    startup_state = info.get("UnitFileState", "")
    exec_start = format_exec_start(info.get("ExecStart", ""))
    canonical = info.get("Id", service)
    pid = info.get("MainPID", "0")
    if pid in {"", "0"}:
        pid = "-"

    conflict_text = ""
    if relation == "conflict":
        selected_real = get_service_file_path(service)
        fragment = get_systemd_fragment_path(service)
        state_text = "[bold bright_red]Name conflict[/bold bright_red]"
        startup_text = "-"
        exec_start = "-"
        pid = "-"
        conflict_text = (
            "\n[bold bright_red]Conflict:[/bold bright_red] systemd is using a different file\n"
            f"[bold bright_white]Selected file:[/bold bright_white] {selected_real or '-'}\n"
            f"[bold bright_white]Systemd file:[/bold bright_white] {fragment or '-'}"
        )
    else:
        if load_state == "not-found":
            state_text = "[bright_magenta]Not installed[/bright_magenta]"
        elif state == "active":
            state_text = "[bright_green]Running[/bright_green]"
        elif state == "failed":
            state_text = "[bright_red]Failed[/bright_red]"
        elif state == "inactive":
            state_text = "[bright_yellow]Stopped[/bright_yellow]"
        else:
            state_text = state
        startup_text = format_startup_state(startup_state)

    canonical_text = ""
    if canonical and canonical != service and relation != "conflict":
        canonical_text = f"[bold bright_white]Canonical:[/bold bright_white] {canonical}\n"

    content = (
        f"[bold bright_white]Selected:[/bold bright_white] {service}\n"
        f"{canonical_text}"
        f"{build_symlink_details(service)}"
        f"{conflict_text}"
        f"\n\n"
        f"[bold bright_white]Status:[/bold bright_white] {state_text}\n"
        f"[bold bright_white]Startup:[/bold bright_white] {startup_text}\n"
        f"[bold bright_white]Load state:[/bold bright_white] {load_state}\n"
        f"[bold bright_white]PID:[/bold bright_white] {pid}\n"
        f"[bold bright_white]Memory:[/bold bright_white] {format_memory(get_service_memory(info)) if relation != 'conflict' else '-'}\n"
        f"[bold bright_white]Restarts:[/bold bright_white] {info.get('NRestarts', '0') if relation != 'conflict' else '-'}\n"
        f"[bold bright_white]Started:[/bold bright_white] {format_start_time(info.get('ActiveEnterTimestamp', '')) if relation != 'conflict' else '-'}\n"
        f"[bold bright_white]ExecStart:[/bold bright_white] {exec_start}"
        f"\n\n"
        f"[bold bright_white]Last error:[/bold bright_white]\n"
        f"{selected_error_text}"
    )

    return Panel(content, title="Service Details", border_style="white")


def build_logs_panel(service):
    subtitle = Text()
    subtitle.append("[l]", style="bold bright_cyan")
    subtitle.append(" Back to details", style="white")
    return Panel(
        Text(selected_log_text, style="bright_white"),
        title=f"Logs: {service}",
        subtitle=subtitle,
        border_style="white",
    )


def build_status_panel():
    if not status_message:
        return None
    text = Text()
    text.append(status_message, style="bold bright_white")
    for detail in status_details:
        text.append("\n")
        if detail in {"Created symlinks:", "Removed symlinks:"}:
            text.append(detail, style="bold bright_cyan")
        elif detail.startswith("Warning:") or detail.startswith("Action blocked:"):
            text.append(detail, style="yellow")
        else:
            text.append(detail, style="bright_white")
    return Panel(text, title="Last Action", border_style="bright_cyan")


def build_footer():
    footer = Text()
    shortcuts = [
        ("[↑/↓]", " Select   "),
        ("[d]", " Source   "),
        ("[c]", " Create   "),
        ("[D]", " Delete   "),
        ("[e]", " Enable   "),
        ("[E]", " Disable   "),
        ("[r]", " Restart   "),
        ("[s]", " Start   "),
        ("[x]", " Stop   "),
        ("[g]", " Daemon Reload   "),
        ("[f]", " Refresh   "),
        ("[l]", " Logs   "),
        ("[q]", " Quit"),
    ]
    for key, label in shortcuts:
        footer.append(key, style="bold bright_cyan")
        footer.append(label, style="bright_white")
    return footer


def build_dashboard():
    elements = []
    if services:
        elements.append(build_service_table())
        elements.append("")
        service = services[selected_index]
        elements.append(build_logs_panel(service) if show_logs else build_detail_panel(service))
    else:
        elements.append(build_empty_panel())

    status_panel = build_status_panel()
    if status_panel:
        elements.append("")
        elements.append(status_panel)

    elements.append("")
    elements.append(build_footer())
    return Group(*elements)


def read_key(timeout=0.08):
    readable, _, _ = select.select([sys.stdin], [], [], timeout)
    if not readable:
        return None
    fd = sys.stdin.fileno()
    first = os.read(fd, 1)
    if first == b"\x1b":
        sequence = first
        deadline = time.monotonic() + 0.03
        while len(sequence) < 3 and time.monotonic() < deadline:
            remaining = max(0, deadline - time.monotonic())
            ready, _, _ = select.select([sys.stdin], [], [], remaining)
            if not ready:
                break
            sequence += os.read(fd, 1)
        if sequence == b"\x1b[A":
            return "UP"
        if sequence == b"\x1b[B":
            return "DOWN"
        return "ESC"
    try:
        return first.decode("utf-8")
    except UnicodeDecodeError:
        return None


def handle_key(key):
    global selected_index, show_logs, status_message, status_details
    global last_status_refresh, last_log_refresh, selected_log_text

    if key == "E":
        if services:
            control_service(services[selected_index], "disable")
        return True

    normalized = key if key in {"UP", "DOWN", "ESC"} else key.lower()

    if normalized == "q":
        return False
    if normalized == "d":
        switch_service_source()
        return True
    if normalized == "g":
        dashboard_daemon_reload()
        return True
    if normalized == "f":
        status_details = []
        refresh_service_list(force=True)
        last_status_refresh = 0.0
        refresh_service_data(force=True)
        reset_selected_journal_cache()
        status_message = "Refreshed"
        return True

    if not services:
        return True

    if normalized == "UP":
        selected_index = (selected_index - 1) % len(services)
        show_logs = False
        reset_selected_journal_cache()
        return True

    if normalized == "DOWN":
        selected_index = (selected_index + 1) % len(services)
        show_logs = False
        reset_selected_journal_cache()
        return True

    service = services[selected_index]

    if normalized == "e":
        control_service(service, "enable")
        return True
    if normalized == "l":
        show_logs = not show_logs
        if show_logs:
            selected_log_text = "Loading..."
            last_log_refresh = 0.0
        return True
    if normalized == "r":
        control_service(service, "restart")
        return True
    if normalized == "s":
        control_service(service, "start")
        return True
    if normalized == "x":
        control_service(service, "stop")
        return True

    return True


def run_modal_action(live, console, fd, normal_settings, action):
    global status_message, status_details
    global last_status_refresh, last_service_list_refresh

    live.stop()
    termios.tcsetattr(fd, termios.TCSADRAIN, normal_settings)
    console.clear()
    message = None

    try:
        message = action(console, fd, normal_settings)
    except KeyboardInterrupt:
        console.print("\nOperation cancelled.", style="bold yellow")
        pause_any_key(fd, normal_settings)
        message = "Operation cancelled"
    except Exception as exc:
        console.print("\nUnexpected error:", style="bold bright_red")
        console.print(str(exc), style="bright_white")
        pause_any_key(fd, normal_settings)
        message = "Operation failed"

    status_details = []
    if message:
        status_message = message

    last_service_list_refresh = 0.0
    last_status_refresh = 0.0
    refresh_service_list(force=True)
    refresh_service_data(force=True)
    reset_selected_journal_cache()

    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except termios.error:
        pass

    tty.setcbreak(fd)
    live.update(build_dashboard(), refresh=False)
    live.start(refresh=True)


def main():
    console = Console(highlight=False)

    if not sys.stdin.isatty():
        print("This dashboard must be run inside an interactive terminal.")
        return

    refresh_service_list(force=True)
    refresh_service_data(force=True)

    fd = sys.stdin.fileno()
    normal_settings = termios.tcgetattr(fd)

    live = Live(
        build_dashboard(),
        console=console,
        screen=True,
        auto_refresh=False,
    )

    try:
        tty.setcbreak(fd)
        live.start(refresh=True)
        running = True

        while running:
            key = read_key()

            if key is not None:
                if key == "D":
                    run_modal_action(
                        live, console, fd, normal_settings,
                        delete_service_wizard,
                    )
                    continue

                if (
                    key not in {"UP", "DOWN", "ESC"}
                    and key.lower() == "c"
                ):
                    run_modal_action(
                        live, console, fd, normal_settings,
                        create_service_wizard,
                    )
                    continue

                running = handle_key(key)
                live.update(build_dashboard(), refresh=True)
                continue

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
                live.update(build_dashboard(), refresh=True)

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
