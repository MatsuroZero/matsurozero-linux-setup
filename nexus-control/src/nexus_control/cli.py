from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import sys
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.2.0"
CONFIG_DIR = Path.home() / ".config" / "nexus-control"
REGISTRY = CONFIG_DIR / "registry.json"

KUMA_COMPOSE = Path.home() / "Projects" / "uptime-kuma" / "compose.yaml"

SERVICES = {
    "jellyfin": ("system", "jellyfin.service"),
    "docker": ("system", "docker.service"),
    "ollama": ("system", "ollama.service"),
    "localnexus": ("user", "localnexus.service"),
    "telegram-bot": ("user", "telegram-control-bot.service"),
    "dashboard": ("system", "system-dashboard.service"),
    "dashboard-ydotool": ("system", "system-dashboard-ydotool.service"),
}

DOCKER_PROJECTS = {
    "odysseus": Path.home() / "Projects" / "odysseus" / "docker-compose.yml",
}

SUNSHINE_UNITS = (
    "sunshine.service",
    "app-dev.lizardbyte.app.Sunshine.service",
)


def run(command: list[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, text=True, capture_output=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, 1, "", str(exc))


def load_registry() -> dict:
    if not REGISTRY.exists():
        return {"commands": {}, "profiles": {}}
    try:
        data = json.loads(REGISTRY.read_text())
        if not isinstance(data, dict):
            raise ValueError("Registry root must be an object")

        data.setdefault("commands", {})
        data.setdefault("profiles", {})

        commands = data["commands"]
        profiles = data["profiles"]
        if not isinstance(commands, dict) or not isinstance(profiles, dict):
            raise ValueError("Invalid commands or profiles section")

        for name, entry in commands.items():
            if not isinstance(name, str) or not valid_name(name):
                raise ValueError(f"Invalid command name: {name!r}")
            if not isinstance(entry, dict):
                raise ValueError(f"Invalid command entry: {name}")
            argv = entry.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(arg, str) for arg in argv)
                or not argv[0].strip()
            ):
                raise ValueError(f"Invalid argv list for command: {name}")

        allowed_actions = {"start", "stop", "restart"}

        for name, steps in profiles.items():
            if not isinstance(name, str) or not valid_name(name):
                raise ValueError(f"Invalid profile name: {name!r}")
            if (
                not isinstance(steps, list)
                or not steps
                or any(not isinstance(step, str) or not step for step in steps)
            ):
                raise ValueError(f"Invalid steps for profile: {name}")

            for step in steps:
                if step.startswith("service:"):
                    parts = step.split(":")
                    if (
                        len(parts) != 3
                        or parts[1] not in allowed_actions
                        or resolve_service(parts[2]) is None
                    ):
                        raise ValueError(
                            f"Invalid service step in profile {name}: {step!r}"
                        )
                else:
                    command_name = step[4:] if step.startswith("cmd:") else step
                    if not command_name or command_name not in commands:
                        raise ValueError(
                            f"Unknown command in profile {name}: {step!r}"
                        )

        return data
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Cannot read registry {REGISTRY}: {exc}", file=sys.stderr)
        raise SystemExit(2)

def save_registry(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = REGISTRY.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        temporary.replace(REGISTRY)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def valid_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name))


def unit_state(scope: str, unit: str) -> str:
    command = ["systemctl"]
    if scope == "user":
        command.append("--user")
    result = run(command + ["is-active", unit])
    return result.stdout.strip() or "unknown"


def resolve_service(name: str) -> tuple[str, str] | None:
    if name in SERVICES:
        return SERVICES[name]
    if name == "sunshine":
        states = [(unit, unit_state("user", unit)) for unit in SUNSHINE_UNITS]
        active = [unit for unit, state in states if state == "active"]
        if len(active) == 1:
            return "user", active[0]
        if len(active) > 1:
            print("Both Sunshine units are active. Refusing to guess.")
            return None
        enabled = [
            unit for unit in SUNSHINE_UNITS
            if run(["systemctl", "--user", "is-enabled", unit]).stdout.strip() == "enabled"
        ]
        if len(enabled) == 1:
            return "user", enabled[0]
    return None


def systemctl(scope: str, *args: str) -> subprocess.CompletedProcess[str]:
    command = ["systemctl"]
    if scope == "user":
        command.append("--user")
    elif not args or args[0] not in ("is-active", "is-enabled"):
        command.insert(0, "sudo")
    return run(command + list(args), timeout=30)


def autostart_state(scope: str, unit: str) -> str:
    result = run(
        ["systemctl"] + (["--user"] if scope == "user" else [])
        + ["is-enabled", unit],
        timeout=30,
    )
    state = result.stdout.strip()
    if state:
        return state
    return "unknown"


def list_services(show_autostart: bool = True) -> None:
    if show_autostart:
        print(f"{'SERVICE':<20} {'RUNTIME':<12} {'AUTOSTART':<12} UNIT")
    else:
        print(f"{'SERVICE':<20} {'AUTOSTART':<12} UNIT")

    for name in (*SERVICES, "sunshine"):
        resolved = resolve_service(name)
        if resolved is None:
            print(f"{name:<20} unresolved")
            continue

        scope, unit = resolved
        startup = autostart_state(scope, unit)
        if show_autostart:
            print(
                f"{name:<20} {unit_state(scope, unit):<12} "
                f"{startup:<12} {unit}"
            )
        else:
            print(f"{name:<20} {startup:<12} {unit}")


def service_action(action: str, name: str) -> None:
    resolved = resolve_service(name)
    if resolved is None:
        print(f"Unknown or ambiguous service: {name}")
        raise SystemExit(2)
    scope, unit = resolved

    if action in ("stop", "restart"):
        answer = input(f"{action.title()} {name} ({unit})? Type yes to confirm: ")
        if answer.strip().lower() != "yes":
            print("Cancelled.")
            return

    if action in ("enable", "disable"):
        current = autostart_state(scope, unit)
        desired = "enabled" if action == "enable" else "disabled"
        if current == desired:
            print(f"Autostart for {name} is already {desired}. No change needed.")
            return
        if current in ("static", "masked", "indirect", "generated", "transient"):
            print(
                f"Cannot set autostart for {name}: systemd reports "
                f"'{current}'. No change made."
            )
            raise SystemExit(2)

    verb = {
        "start": "Starting",
        "stop": "Stopping",
        "restart": "Restarting",
        "enable": "Enabling autostart for",
        "disable": "Disabling autostart for",
    }[action]
    print(f"{verb} {name}...")
    result = systemctl(scope, action, unit)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        print(f"Command failed with exit code {result.returncode}.")
        raise SystemExit(result.returncode)

    if action in ("enable", "disable"):
        print(f"Autostart: {autostart_state(scope, unit)}")
    else:
        print(f"Runtime: {unit_state(scope, unit)}")


def show_logs(name: str, lines: int) -> None:
    resolved = resolve_service(name)
    if resolved is None:
        print(f"Unknown or ambiguous service: {name}")
        raise SystemExit(2)
    scope, unit = resolved
    command = ["journalctl"]
    if scope == "user":
        command.append("--user")
    else:
        command.insert(0, "sudo")
    result = run(command + ["-u", unit, "-n", str(lines), "--no-pager"], timeout=30)
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        raise SystemExit(result.returncode)



def docker_compose(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    compose_file = DOCKER_PROJECTS.get(name)
    if compose_file is None:
        print(f"Unknown Docker project: {name}")
        raise SystemExit(2)
    if not compose_file.is_file():
        print(f"Compose file not found for {name}: {compose_file}",
              file=sys.stderr)
        raise SystemExit(2)
    return run(
        ["docker", "compose", "-f", str(compose_file), *args],
        timeout=60,
    )


def docker_project_state(name: str) -> str:
    result = docker_compose(name, "ps", "--all", "--format", "json")
    if result.returncode:
        return "error"
    if not result.stdout.strip():
        return "stopped"
    try:
        output = result.stdout.strip()
        if not output:
            return "stopped"

        try:
            data = json.loads(output)
            if isinstance(data, dict):
                data = [data]
        except json.JSONDecodeError:
            # Docker Compose may emit one JSON object per line.
            data = [json.loads(line) for line in output.splitlines() if line.strip()]

        if not isinstance(data, list):
            return "unknown"
        if not data:
            return "stopped"

        states = [
            str(item.get("State", "")).lower()
            for item in data
            if isinstance(item, dict)
        ]
        if not states:
            return "unknown"

        running = sum(state == "running" for state in states)
        if running == len(states):
            return "running"
        if running:
            return "partial"
        return "stopped"
    except (json.JSONDecodeError, TypeError, AttributeError):
        return "unknown"


def docker_list() -> None:
    command = [
        "docker", "ps", "-a",
        "--format",
        '{{.Names}}\\t{{.Status}}\\t{{.Label "com.docker.compose.project"}}\\t{{.Image}}',
    ]
    result = run(command, timeout=30)

    if result.returncode:
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        print(f"Docker container listing failed (exit code {result.returncode}).")
        raise SystemExit(result.returncode)

    print(f"{'CONTAINER':<28} {'STATUS':<24} {'PROJECT':<18} IMAGE")
    if not result.stdout.strip():
        print("No Docker containers found.")
        return

    for line in result.stdout.splitlines():
        columns = line.split("\\t", 3)
        if len(columns) != 4:
            print(line)
            continue
        name, status, project, image = columns
        print(f"{name:<28} {status:<24} {project or '-':<18} {image}")



def docker_action(action: str, name: str) -> None:
    if name not in DOCKER_PROJECTS:
        print(f"Unknown Docker project: {name}")
        raise SystemExit(2)

    if action in ("stop", "restart"):
        answer = input(
            f"{action.title()} Docker project {name}? "
            "Type yes to confirm: "
        )
        if answer.strip().lower() != "yes":
            print("Cancelled.")
            return

    if action == "start":
        compose_args = ("up", "-d")
    elif action == "stop":
        compose_args = ("stop",)
    else:
        compose_args = ("restart",)

    print(f"{action.title()} Docker project {name}...")
    result = docker_compose(name, *compose_args)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        print(f"Command failed with exit code {result.returncode}.")
        raise SystemExit(result.returncode)

    print(f"Result: {docker_project_state(name)}")



def docker_container_action(action: str, name: str) -> None:
    # Only act on containers that currently exist in Docker.
    listing = run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        timeout=30,
    )
    if listing.returncode:
        if listing.stderr:
            print(listing.stderr, end="", file=sys.stderr)
        raise SystemExit(listing.returncode)

    containers = {
        line.strip() for line in listing.stdout.splitlines() if line.strip()
    }
    if name not in containers:
        print(f"Unknown Docker container: {name}")
        raise SystemExit(2)

    if action in ("stop", "restart"):
        answer = input(
            f"{action.title()} container {name}? Type yes to confirm: "
        )
        if answer.strip().lower() != "yes":
            print("Cancelled.")
            return

    print(f"{action.title()} container {name}...")
    result = run(["docker", action, name], timeout=60)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        print(f"Command failed with exit code {result.returncode}.")
        raise SystemExit(result.returncode)
    print(f"Result: {action} command completed for {name}")



def docker_autostart_unit_name(name: str) -> str:
    return f"nexus-docker-{name}.service"


def docker_autostart_unit_content(name: str) -> str:
    compose_file = DOCKER_PROJECTS[name]
    user = Path.home().name

    def quote(value: str) -> str:
        value = str(value).replace("\\", "\\\\")
        value = value.replace('"', '\\"').replace("%", "%%")
        return f'"{value}"'

    return (
        "# Managed by Nexus Control Center\n"
        f"# Project: {name}\n"
        "[Unit]\n"
        f"Description=Nexus Docker autostart for {name}\n"
        "Requires=docker.service\n"
        "After=docker.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"User={user}\n"
        f"WorkingDirectory={quote(str(compose_file.parent))}\n"
        "RemainAfterExit=yes\n"
        f"ExecStart=/usr/bin/docker compose -f {quote(str(compose_file))} up -d\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def docker_autostart(action: str | None = None, name: str | None = None) -> None:
    unit_dir = Path("/etc/systemd/system")

    if action is None:
        print("Docker project autostart:")
        for project in DOCKER_PROJECTS:
            unit = docker_autostart_unit_name(project)
            result = run(["systemctl", "is-enabled", unit])
            state = (result.stdout or "").strip()
            if result.returncode == 0:
                state = "enabled"
            elif not state or state == "not-found":
                state = "disabled"
            print(f"  {project}: {state}")
        return

    if name not in DOCKER_PROJECTS:
        print(f"Unknown Docker project: {name}")
        return

    compose_file = DOCKER_PROJECTS[name]
    if not compose_file.is_file():
        print(f"Compose file not found: {compose_file}")
        return

    unit = docker_autostart_unit_name(name)
    unit_path = unit_dir / unit

    if action == "enable":
        exists = run(["sudo", "test", "-e", str(unit_path)])
        if exists.returncode == 0:
            existing = run(["sudo", "cat", str(unit_path)])
            if (
                existing.returncode != 0
                or "# Managed by Nexus Control Center" not in (existing.stdout or "")
                or f"# Project: {name}" not in (existing.stdout or "")
            ):
                print(f"Refusing to overwrite an unmanaged unit: {unit_path}")
                return

        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="nexus-docker-",
                suffix=".service", delete=False,
            ) as temporary:
                temporary.write(docker_autostart_unit_content(name))
                temporary_path = Path(temporary.name)

            result = run([
                "sudo", "install", "-m", "644",
                str(temporary_path), str(unit_path),
            ])
            if result.returncode != 0:
                print((result.stderr or result.stdout or "Could not install systemd unit").strip())
                return

            result = run(["sudo", "systemctl", "daemon-reload"])
            if result.returncode != 0:
                print((result.stderr or result.stdout or "systemd daemon-reload failed").strip())
                return

            result = run(["sudo", "systemctl", "enable", unit])
            if result.returncode != 0:
                print((result.stderr or result.stdout or "Could not enable autostart").strip())
                return

            print(f"Autostart enabled for {name}. It will start on boot.")
            print("The project has not been started by this command.")
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return

    if action == "disable":
        exists = run(["sudo", "test", "-e", str(unit_path)])
        if exists.returncode != 0:
            print(f"Autostart is already disabled for {name}; no Nexus unit was found.")
            return

        existing = run(["sudo", "cat", str(unit_path)])
        if (
            existing.returncode != 0
            or "# Managed by Nexus Control Center" not in (existing.stdout or "")
            or f"# Project: {name}" not in (existing.stdout or "")
        ):
            print(f"Refusing to disable an unmanaged unit: {unit_path}")
            return

        result = run(["sudo", "systemctl", "disable", unit])
        if result.returncode != 0:
            print((result.stderr or result.stdout or "Could not disable autostart").strip())
            return

        print(f"Autostart disabled for {name}. Running containers were not stopped.")
        return

    print(f"Unknown autostart action: {action}")


def docker_logs(name: str, lines: int) -> None:
    if name not in DOCKER_PROJECTS:
        print(f"Unknown Docker project: {name}")
        raise SystemExit(2)
    result = docker_compose(name, "logs", "--tail", str(lines))
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        raise SystemExit(result.returncode)


def command_list() -> None:
    commands = load_registry()["commands"]
    if not commands:
        print("No custom commands registered.")
        return
    for name, entry in sorted(commands.items()):
        print(f"{name:<24} {' '.join(entry['argv'])}")


def command_add(name: str, argv: list[str]) -> None:
    if not valid_name(name):
        raise SystemExit("Invalid name. Use letters, numbers, dots, underscores, or hyphens.")
    if not argv or not argv[0].strip():
        raise SystemExit("Provide an executable and optional arguments.")
    data = load_registry()
    if name in data["commands"]:
        raise SystemExit(f"Command already exists: {name}")
    if not shutil.which(argv[0]) and not Path(argv[0]).is_file():
        raise SystemExit(f"Executable not found: {argv[0]}")
    data["commands"][name] = {"argv": argv}
    save_registry(data)
    print(f"Registered command: {name}")


def command_run(name: str) -> None:
    commands = load_registry()["commands"]
    if name not in commands:
        raise SystemExit(f"Unknown command: {name}")
    argv = commands[name]["argv"]
    print(f"Command: {' '.join(argv)}")
    if input("Run it? Type yes to confirm: ").strip().lower() != "yes":
        print("Cancelled.")
        return
    try:
        result = subprocess.run(argv, check=False)
    except OSError as exc:
        raise SystemExit(f"Could not run command: {exc}")
    if result.returncode:
        raise SystemExit(result.returncode)


def command_remove(name: str) -> None:
    data = load_registry()
    if name not in data["commands"]:
        raise SystemExit(f"Unknown command: {name}")
    affected = [p for p, items in data["profiles"].items() if name in items or f"cmd:{name}" in items]
    if affected:
        raise SystemExit(
            f"Command is used by profile(s): {', '.join(affected)}. "
            "Remove it from those profiles first."
        )
    if input(f"Remove registration '{name}'? Type yes: ").strip().lower() != "yes":
        print("Cancelled.")
        return
    del data["commands"][name]
    save_registry(data)
    print("Registration removed. The underlying executable was not deleted.")


def profile_list() -> None:
    profiles = load_registry()["profiles"]
    if not profiles:
        print("No profiles registered.")
        return
    for name, items in sorted(profiles.items()):
        print(f"{name:<24} {' -> '.join(items)}")


def profile_add(name: str, steps: list[str]) -> None:
    if not valid_name(name):
        raise SystemExit("Invalid profile name.")
    if not steps:
        raise SystemExit("Provide at least one profile step.")

    data = load_registry()
    if name in data["profiles"]:
        raise SystemExit(f"Profile already exists: {name}")

    allowed_services = {*SERVICES, "sunshine"}
    for step in steps:
        if step.startswith("cmd:"):
            command_name = step[4:]
            if command_name not in data["commands"]:
                raise SystemExit(f"Unregistered command: {command_name}")
        elif step.startswith("service:"):
            parts = step.split(":")
            if len(parts) != 3 or parts[1] not in ("start", "stop", "restart"):
                raise SystemExit(
                    f"Invalid service step '{step}'. Use service:start:jellyfin."
                )
            if parts[2] not in allowed_services:
                raise SystemExit(f"Unknown service in step: {parts[2]}")
        elif step not in data["commands"]:
            raise SystemExit(
                f"Unknown step '{step}'. Use a command name, cmd:name, "
                "or service:start:service-name."
            )

    data["profiles"][name] = steps
    save_registry(data)
    print(f"Created profile: {name}")


def describe_step(step: str, commands: dict) -> str:
    if step.startswith("service:"):
        _, action, name = step.split(":", 2)
        resolved = resolve_service(name)
        unit = resolved[1] if resolved else "unresolved unit"
        return f"{action} service {name} ({unit})"
    command_name = step[4:] if step.startswith("cmd:") else step
    entry = commands.get(command_name)
    if entry is None:
        return f"{command_name} (missing command)"
    return f"command {command_name}: {' '.join(entry['argv'])}"




def profile_show(name: str) -> None:
    data = load_registry()
    if name not in data["profiles"]:
        raise SystemExit(f"Unknown profile: {name}")
    print(f"Profile: {name}")
    for index, step in enumerate(data["profiles"][name], 1):
        print(f"{index}. {describe_step(step, data['commands'])}")


def profile_run(name: str) -> None:
    data = load_registry()
    if name not in data["profiles"]:
        raise SystemExit(f"Unknown profile: {name}")

    profile_show(name)
    if input("Execute this profile in order? Type yes to confirm: ").strip().lower() != "yes":
        print("Cancelled.")
        return

    for step in data["profiles"][name]:
        if step.startswith("service:"):
            _, action, service_name = step.split(":", 2)
            print(f"\n>>> {action} service {service_name}")
            service_action(action, service_name)
            continue

        command_name = step[4:] if step.startswith("cmd:") else step
        entry = data["commands"].get(command_name)
        if entry is None:
            raise SystemExit(f"Command registration missing: {command_name}")
        argv = entry["argv"]
        print(f"\n>>> {command_name}: {' '.join(argv)}")
        try:
            result = subprocess.run(argv, check=False)
        except OSError as exc:
            raise SystemExit(f"Failed to launch {command_name}: {exc}")
        if result.returncode:
            raise SystemExit(
                f"{command_name} failed with exit code {result.returncode}. "
                "Profile stopped."
            )

    print("\nProfile completed.")

def profile_remove(name: str) -> None:
    data = load_registry()
    if name not in data["profiles"]:
        raise SystemExit(f"Unknown profile: {name}")
    if input(f"Remove profile '{name}'? Type yes: ").strip().lower() != "yes":
        print("Cancelled.")
        return
    del data["profiles"][name]
    save_registry(data)
    print("Profile removed. Registered commands were not deleted.")


def status() -> None:
    print("NEXUS CONTROL CENTER")
    print("====================\n")
    list_services()
    print("\nTools")
    for name in ("docker", "ollama", "claude", "codex", "flatpak"):
        print(f"{name:<20} {shutil.which(name) or 'not found'}")
    data = load_registry()
    print(f"\nCustom commands: {len(data['commands'])}")
    print(f"Profiles:         {len(data['profiles'])}")



def health() -> None:
    print("NEXUS HEALTH")
    print("============")

    checks = (
    ("Dashboard", "http://127.0.0.1:7171/"),
    ("Jellyfin", "http://127.0.0.1:8096/"),
    ("Ollama API", "http://127.0.0.1:11434/api/tags"),
    ("Uptime Kuma", "http://127.0.0.1:3001/"),
)

    failures = 0
    warnings = 0
    unknown = 0

    print("\nHTTP endpoints")
    for name, url in checks:
        request = urllib.request.Request(
            url, headers={"User-Agent": f"Nexus-Control/{VERSION}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                status = response.status
                redirected = response.geturl() != url
        except urllib.error.HTTPError as exc:
            status = exc.code
            redirected = False
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            print(f"[FAILED] {name:<14} {reason}")
            failures += 1
            continue

        if 200 <= status < 400:
            note = " (redirected)" if redirected else ""
            print(f"[OK]     {name:<14} HTTP {status}{note}")
        elif status < 500:
            print(f"[WARNING] {name:<12} HTTP {status} (server responded)")
            warnings += 1
        else:
            print(f"[FAILED] {name:<14} HTTP {status}")
            failures += 1

    print("\nDocker containers")
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[WARNING] Docker query failed: {exc}")
        warnings += 1
    else:
        if result.returncode != 0:
            detail = result.stderr.strip() or "Docker command failed"
            print(f"[WARNING] {detail}")
            warnings += 1
        else:
            containers = []
            for line in result.stdout.splitlines():
                try:
                    containers.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

            if not containers:
                print("[INFO]   No containers found")
            else:
                for container in containers:
                    name = container.get("Names", "unknown")
                    state = container.get("State", "unknown").lower()
                    status_text = container.get("Status", "")
                    status_lower = status_text.lower()

                    if state != "running":
                        print(f"[WARNING] {name:<24} {status_text or state}")
                        warnings += 1
                    elif "(unhealthy)" in status_lower:
                        print(f"[FAILED] {name:<24} unhealthy")
                        failures += 1
                    elif "(healthy)" in status_lower:
                        print(f"[OK]     {name:<24} running, healthy")
                    elif "(health: starting)" in status_lower:
                        print(f"[WARNING] {name:<24} health check starting")
                        warnings += 1
                    else:
                        print(f"[INFO]   {name:<24} running, health unknown")
                        unknown += 1

    print()
    if failures:
        print(f"Result: {failures} failed, {warnings} warning(s)")
    elif warnings:
        print(f"Result: no failures, {warnings} warning(s)")
    elif unknown:
        print(f"Result: checks passed; health unknown for {unknown} container(s)")
    else:
        print("Result: all checks passed")

def doctor() -> None:
    print("NEXUS DOCTOR\n============")
    print(f"[{'OK' if sys.version_info >= (3, 11) else 'CHECK'}] Python {sys.version.split()[0]}")
    print(f"[{'OK' if shutil.which('docker') else 'CHECK'}] Docker CLI")
    result = run(["docker", "ps", "-q"])
    print(f"[{'OK' if result.returncode == 0 else 'CHECK'}] Docker API access")
    if result.returncode:
        print(result.stderr.strip())
    for name in ("dashboard-start", "dashboard-stop"):
        print(f"[{'OK' if shutil.which(name) else 'CHECK'}] {name}")
    states = [unit_state("user", unit) for unit in SUNSHINE_UNITS]
    if states.count("active") > 1:
        print("[CHECK] Both Sunshine units are active; selection is ambiguous")
    else:
        print("[OK] Sunshine unit selection is unambiguous")
    try:
        data = load_registry()
        print(f"[OK] Registry readable ({len(data['commands'])} commands, {len(data['profiles'])} profiles)")
    except SystemExit:
        print("[CHECK] Registry needs attention")



def kuma_status() -> None:
    import json

    print("UPTIME KUMA")
    print("===========")

    if not KUMA_COMPOSE.is_file():
        print(f"[FAIL] Compose file not found: {KUMA_COMPOSE}")
        raise SystemExit(1)

    result = run([
        "docker", "compose", "-f", str(KUMA_COMPOSE),
        "ps", "--format", "json"
    ], timeout=15)

    if result.returncode != 0:
        print("[FAIL] Could not read the Kuma Compose project")
        print((result.stderr or result.stdout).strip())
        raise SystemExit(result.returncode or 1)

    try:
        output = result.stdout.strip()
        if output.startswith("["):
            containers = json.loads(output)
        else:
            containers = [
                json.loads(line)
                for line in output.splitlines()
                if line.strip()
            ]
    except json.JSONDecodeError:
        print("[FAIL] Could not parse Docker Compose status")
        raise SystemExit(1)

    if not containers:
        print("[FAIL] No Kuma containers found")
        raise SystemExit(1)

    container_ok = True

    for container in containers:
        name = container.get("Name") or container.get("Service") or "Kuma"
        state = container.get("State", "unknown")
        health = container.get("Health", "")
        ok = state.lower() == "running" and (
            not health or health.lower() == "healthy"
        )

        details = state
        if health:
            details += f", {health}"

        print(f"[{'OK' if ok else 'FAIL'}] Docker: {name} ({details})")
        container_ok = container_ok and ok

    http_result = run([
        "curl", "--silent", "--show-error",
        "--output", "/dev/null",
        "--write-out", "%{http_code}",
        "--max-time", "5",
        "http://127.0.0.1:3001/"
    ], timeout=7)

    code = (http_result.stdout or "").strip()
    http_ok = (
        http_result.returncode == 0
        and code.isdigit()
        and 200 <= int(code) < 400
    )

    if http_ok:
        print(f"[OK] HTTP: http://127.0.0.1:3001/ (HTTP {code})")
    else:
        detail = (
            http_result.stderr or f"HTTP status {code or 'unavailable'}"
        ).strip()
        print(f"[FAIL] HTTP: http://127.0.0.1:3001/ ({detail})")

    all_ok = container_ok and http_ok
    print(f"\nResult: {'all checks passed' if all_ok else 'one or more checks failed'}")

    if not all_ok:
        raise SystemExit(1)


def kuma_logs(lines: int) -> None:
    if not 1 <= lines <= 1000:
        print("Error: log lines must be between 1 and 1000", file=sys.stderr)
        raise SystemExit(2)

    if not KUMA_COMPOSE.is_file():
        print(f"Error: Compose file not found: {KUMA_COMPOSE}", file=sys.stderr)
        raise SystemExit(1)

    result = run([
        "docker", "compose", "-f", str(KUMA_COMPOSE),
        "logs", "--tail", str(lines), "--timestamps"
    ], timeout=30)

    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")

    if result.returncode != 0:
        print((result.stderr or "Could not retrieve Kuma logs").strip(),
              file=sys.stderr)
        raise SystemExit(result.returncode)


def overview() -> None:
    print("NEXUS OVERVIEW")
    print("==============")

    problems = 0
    warnings = 0

    endpoints = (
        ("Dashboard", "http://127.0.0.1:7171/"),
        ("Jellyfin", "http://127.0.0.1:8096/"),
        ("Ollama API", "http://127.0.0.1:11434/api/tags"),
        ("Uptime Kuma", "http://127.0.0.1:3001/"),
    )

    http_ok = 0
    for name, url in endpoints:
        request = urllib.request.Request(
            url, headers={"User-Agent": f"Nexus-Control/{VERSION}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                code = response.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        except (urllib.error.URLError, TimeoutError, OSError):
            continue

        if 200 <= code < 400:
            http_ok += 1

    print(f"HTTP endpoints  {http_ok}/{len(endpoints)} responding")
    if http_ok < len(endpoints):
        problems += 1

    result = run(
        ["docker", "ps", "-a", "--format", "{{json .}}"],
        timeout=8,
    )

    running = healthy = unhealthy = stopped = unknown = 0
    unknown_names = []
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            state = item.get("State", "unknown").lower()
            status_text = item.get("Status", "").lower()

            if state != "running":
                stopped += 1
            elif "(unhealthy)" in status_text:
                running += 1
                unhealthy += 1
            elif "(healthy)" in status_text:
                running += 1
                healthy += 1
            else:
                running += 1
                unknown += 1
                unknown_names.append(
                    item.get("Names") or item.get("Name") or item.get("ID") or "unnamed"
                )

        print(
            f"Docker          {running} running "
            f"({healthy} healthy, {unhealthy} unhealthy, "
            f"{unknown} health unknown), {stopped} stopped"
        )
        if unhealthy or stopped:
            problems += 1
        if unknown:
            warnings += 1
            print("                Health unknown: " + ", ".join(unknown_names))
    else:
        print("Docker          unavailable")
        problems += 1

    kuma = run([
        "docker", "compose", "-f", str(KUMA_COMPOSE),
        "ps", "--format", "json"
    ], timeout=10)

    kuma_state = "UNKNOWN"
    if kuma.returncode == 0:
        try:
            output = kuma.stdout.strip()
            if output.startswith("["):
                entries = json.loads(output)
            else:
                entries = [
                    json.loads(line)
                    for line in output.splitlines()
                    if line.strip()
                ]

            if entries:
                item = entries[0]
                state = item.get("State", "unknown").lower()
                health = item.get("Health", "").lower()

                if state == "running" and health == "healthy":
                    kuma_state = "HEALTHY"
                elif state == "running" and not health:
                    kuma_state = "RUNNING"
                elif state == "running":
                    kuma_state = "UNHEALTHY"
                else:
                    kuma_state = state.upper()
        except (json.JSONDecodeError, TypeError):
            pass

    print(f"Uptime Kuma     {kuma_state}")
    if kuma_state not in ("HEALTHY", "RUNNING"):
        problems += 1

    try:
        registry = load_registry()
        print(
            f"Registry        {len(registry['commands'])} commands, "
            f"{len(registry['profiles'])} profiles"
        )
    except SystemExit:
        print("Registry        needs attention")
        problems += 1

    if problems:
        print(f"\nOverall         NEEDS ATTENTION ({problems} problem area(s))")
    elif warnings:
        print(f"\nOverall         OPERATIONAL ({warnings} warning area(s))")
    else:
        print("\nOverall         OPERATIONAL")


def overview_json(strict: bool = False) -> None:
    import json

    report = {
        "version": VERSION,
        "http_endpoints": [],
        "docker": {
            "available": False,
            "running": 0,
            "healthy": 0,
            "unhealthy": 0,
            "health_unknown": [],
            "stopped": [],
            "containers": [],
        },
        "uptime_kuma": {"status": "UNKNOWN"},
        "registry": {"status": "UNKNOWN"},
        "problems": [],
        "warnings": [],
    }

    endpoints = (
        ("Dashboard", "http://127.0.0.1:7171/"),
        ("Jellyfin", "http://127.0.0.1:8096/"),
        ("Ollama API", "http://127.0.0.1:11434/api/tags"),
        ("Uptime Kuma", "http://127.0.0.1:3001/"),
    )

    for name, url in endpoints:
        entry = {"name": name, "url": url, "ok": False, "http_status": None}
        request = urllib.request.Request(
            url, headers={"User-Agent": f"Nexus-Control/{VERSION}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                code = response.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            entry["error"] = str(exc)
        else:
            entry["http_status"] = code

        if entry["http_status"] is not None:
            entry["ok"] = 200 <= entry["http_status"] < 400

        report["http_endpoints"].append(entry)

    if not all(e["ok"] for e in report["http_endpoints"]):
        report["problems"].append("http_endpoints")

    docker = run(
        ["docker", "ps", "-a", "--format", "{{json .}}"],
        timeout=8,
    )

    if docker.returncode == 0:
        report["docker"]["available"] = True

        for line in docker.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            name = item.get("Names") or item.get("Name") or item.get("ID") or "unnamed"
            state = item.get("State", "unknown").lower()
            status_text = item.get("Status", "").lower()

            if state != "running":
                health = "stopped"
                report["docker"]["stopped"].append(name)
            elif "(unhealthy)" in status_text:
                health = "unhealthy"
                report["docker"]["running"] += 1
                report["docker"]["unhealthy"] += 1
            elif "(healthy)" in status_text:
                health = "healthy"
                report["docker"]["running"] += 1
                report["docker"]["healthy"] += 1
            else:
                health = "unknown"
                report["docker"]["running"] += 1
                report["docker"]["health_unknown"].append(name)

            report["docker"]["containers"].append({
                "name": name,
                "state": state,
                "status": item.get("Status", ""),
                "health": health,
            })

        if report["docker"]["unhealthy"] or report["docker"]["stopped"]:
            report["problems"].append("docker")
        if report["docker"]["health_unknown"]:
            report["warnings"].append("docker_health_unknown")
    else:
        report["problems"].append("docker")
        report["docker"]["error"] = (docker.stderr or docker.stdout).strip()

    kuma = run([
        "docker", "compose", "-f", str(KUMA_COMPOSE),
        "ps", "--format", "json"
    ], timeout=10)

    if kuma.returncode == 0:
        try:
            output = kuma.stdout.strip()
            if output.startswith("["):
                entries = json.loads(output)
            else:
                entries = [
                    json.loads(line)
                    for line in output.splitlines()
                    if line.strip()
                ]

            if entries:
                item = entries[0]
                state = item.get("State", "unknown").lower()
                health = item.get("Health", "").lower()

                if state == "running" and health == "healthy":
                    status = "HEALTHY"
                elif state == "running" and not health:
                    status = "RUNNING"
                elif state == "running":
                    status = "UNHEALTHY"
                else:
                    status = state.upper()

                report["uptime_kuma"] = {
                    "status": status,
                    "container": item.get("Name") or item.get("Service") or "uptime-kuma",
                }
            else:
                report["uptime_kuma"]["status"] = "NOT_FOUND"
        except (json.JSONDecodeError, TypeError):
            report["uptime_kuma"]["status"] = "UNKNOWN"
    else:
        report["uptime_kuma"]["error"] = (
            kuma.stderr or kuma.stdout
        ).strip()

    if report["uptime_kuma"]["status"] not in ("HEALTHY", "RUNNING"):
        report["problems"].append("uptime_kuma")

    try:
        registry = load_registry()
        report["registry"] = {
            "status": "OK",
            "commands": len(registry["commands"]),
            "profiles": len(registry["profiles"]),
        }
    except SystemExit:
        report["registry"]["status"] = "ERROR"
        report["problems"].append("registry")

    if report["problems"]:
        report["overall"] = "NEEDS ATTENTION"
    elif report["warnings"]:
        report["overall"] = "OPERATIONAL WITH WARNINGS"
    else:
        report["overall"] = "OPERATIONAL"

    print(json.dumps(report, indent=2))

    if report["problems"] or (strict and report["warnings"]):
        raise SystemExit(1)


def guide(topic: str | None = None, search: str | None = None) -> None:
    topics = {
        "overview": """OVERVIEW
  START HERE
  nexus
      Show the command-line entry point and available commands.

  nexus guide
      List guide topics. Use 'nexus guide TOPIC' for command help, or
      'nexus guide --search WORD' to search the guide.

  SYSTEM SUMMARIES
  nexus overview
      Compact system overview for HTTP endpoints, Docker, Uptime Kuma,
      and the command/profile registry.

  nexus status
      Show the general Nexus/system status summary.

  nexus health
      Check configured HTTP endpoints and report their responses.

  nexus doctor
      Run the project's diagnostic checks.

  MACHINE-READABLE OUTPUT
  nexus overview --json
      Print machine-readable JSON. Exits 1 when actual problems are found.

  nexus overview --json --strict
      Also exits 1 when warnings exist, such as unknown Docker health status.
      Successful checks exit 0. JSON is printed before the exit code is set.

  TROUBLESHOOTING PATH
  1. Run 'nexus overview' for a compact summary.
  2. Run 'nexus health' to inspect configured HTTP endpoints.
  3. Run 'nexus doctor' for project diagnostic checks.
  4. Use 'nexus service list' or 'nexus docker list' to inspect services
     and configured Docker projects.
  5. Open the relevant guide topic for available controls and logs.""",

        "status": """STATUS
  nexus status
      Show the general Nexus/system status summary.

  NEXT STEPS
  Use 'nexus overview' for a broader summary, 'nexus health' for
  endpoint checks, or 'nexus doctor' for diagnostic checks.""",

        "doctor": """DOCTOR
  nexus doctor
      Run the project's diagnostic checks.

  NEXT STEPS
  Compare reported problems with 'nexus overview' and 'nexus health'.
  Consult the relevant guide topic before changing services.""",

        "health": """HEALTH
  nexus health
      Check configured HTTP endpoints and report their responses.

  NEXT STEPS
  If an endpoint check fails, run 'nexus overview' for context and
  'nexus doctor' for diagnostics. Check the service before restarting it.""",

        "version": """VERSION
  nexus version
      Print the installed Nexus Control Center version.

  NEXT STEPS
  Run 'nexus guide' to list topics or 'nexus guide --search WORD'
  to search for a command or feature.""",

        "kuma": """UPTIME KUMA
  nexus kuma status
      Show the Uptime Kuma container and endpoint status.

  nexus kuma logs [-n LINES]
      Show recent Uptime Kuma logs. The line count must be 1-1000;
      the default is 50.

  TROUBLESHOOTING
  Check 'nexus kuma status', then inspect 'nexus kuma logs'.
  Use 'nexus service list' to inspect service state.""",

        "service": """SERVICES
  nexus service list
      List supported services and their current state.

  nexus service start NAME
  nexus service stop NAME
  nexus service restart NAME
      Control a service now. Stop and restart may require confirmation.

  nexus service autostart
      Show service autostart settings.

  nexus service autostart enable NAME
  nexus service autostart disable NAME
      Enable or disable starting a service automatically at boot.
      This changes boot configuration, not whether it is running now.

  nexus service enable NAME
  nexus service disable NAME
      Compatibility aliases for enabling or disabling service autostart.

  nexus service logs NAME [-n LINES]
      Show service logs. The line count must be 1-1000; default is 50.
      Use 'nexus service list' to see supported names.

  IMPORTANT
  'start', 'stop', and 'restart' control runtime. Autostart enable
  and disable control startup at boot. Use service logs to investigate.""",

        "docker": """DOCKER
  nexus docker list
      List configured Docker Compose projects.

  nexus docker start PROJECT
  nexus docker stop PROJECT
  nexus docker restart PROJECT
      Start, stop, or restart a configured Compose project.

  nexus docker start-container NAME
  nexus docker stop-container NAME
  nexus docker restart-container NAME
      Control one container by its exact Docker container name.
      Stop and restart may require confirmation.

  nexus docker logs PROJECT [-n LINES]
      Show logs for a configured Compose project. The line count must
      be 1-1000; default is 50.

  nexus docker autostart
      Show whether configured projects are enabled for startup at boot.

  nexus docker autostart enable PROJECT
  nexus docker autostart disable PROJECT
      Enable or disable boot startup using a Nexus-managed systemd unit.
      Disabling autostart does not stop currently running containers.

  Use 'nexus docker list' to see configured projects. Container commands
  require the exact container name reported by Docker.

  TROUBLESHOOTING
  Start with 'nexus docker list'. Use 'nexus docker logs PROJECT'
  to inspect logs. Project controls and container controls are different.""",

        "cmd": """CUSTOM COMMANDS
  nexus cmd list
      List saved custom commands.

  nexus cmd add NAME COMMAND [ARG ...]
      Save a command and its arguments for later use.

  nexus cmd run NAME
      Run a saved command.

  nexus cmd remove NAME
      Remove a saved command. Check the name with 'nexus cmd list' first.

  FIND MORE HELP
  Use 'nexus guide --search command' to find related documentation.
  Check the command syntax before executing saved commands.""",

        "profile": """COMMAND PROFILES
  nexus profile list
      List saved command profiles.

  nexus profile add NAME STEP [STEP ...]
      Create a profile from saved command names.

  nexus profile show NAME
      Show a profile's configured steps.

  nexus profile run NAME
      Run a saved profile.

  nexus profile remove NAME
      Remove a profile.

  FIND MORE HELP
  Use 'nexus guide --search profile' to find related documentation.
  Review the available profile commands before changing configuration.""",

        "guide": """GUIDE
  nexus guide
      Show the command catalogue.

  nexus guide TOPIC
      Explain one topic, for example 'nexus guide overview' or
      'nexus guide profile'.

  Topics: overview, status, doctor, health, version, kuma, service,
  docker, cmd, profile, guide.

  SEARCH TIPS
  Use 'nexus guide TOPIC' for a topic, or 'nexus guide --search WORD'
  to find matching topics. Start with 'nexus guide overview'."""
    }

    if search is not None:
        query = search.strip().casefold()
        if not query:
            raise SystemExit("Search term cannot be empty.")

        matches = [
            (name, content)
            for name, content in topics.items()
            if query in name.casefold() or query in content.casefold()
        ]

        print(f"NEXUS GUIDE SEARCH: {search.strip()}")
        print("=" * (21 + len(search.strip())))
        print(f"{len(matches)} matching topic(s)")
        if not matches:
            print("No matching topics found.")
            raise SystemExit(1)

        for name, content in matches:
            print()
            print(name)
            print("-" * len(name))
            print(content)
        return

    if topic is None:
        descriptions = {
            "overview": "System overview and JSON/strict modes",
            "status": "General status summary",
            "doctor": "Diagnostics",
            "health": "HTTP endpoint checks",
            "version": "Installed version",
            "kuma": "Uptime Kuma status and logs",
            "service": "Service control, autostart, and logs",
            "docker": "Docker Compose projects, containers, logs, and autostart",
            "cmd": "Saved custom commands",
            "profile": "Reusable command sequences",
            "guide": "Help and topic search",
        }

        print("NEXUS GUIDE")
        print("===========")
        print("Use 'nexus guide TOPIC' for details.")
        print("Use 'nexus guide --search WORD' to search help text.")
        print()

        for name in topics:
            description = descriptions.get(name, "Detailed command help")
            print(f"  {name:<10} {description}")

        print()
        print("Examples:")
        print("  nexus guide overview")
        print("  nexus guide profile")
        print("  nexus guide --search docker")
        return

    if topic not in topics:
        print(f"Unknown guide topic: {topic}")
        print("Run 'nexus guide' to see available topics.")
        raise SystemExit(2)

    print(topics[topic])


def main() -> None:
    parser = argparse.ArgumentParser(prog="nexus")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("health", help="Check application HTTP endpoints")
    overview_parser = sub.add_parser(
        "overview", help="Show a combined system overview"
    )
    overview_parser.add_argument(
        "--json", action="store_true",
        help="Output the overview as JSON"
    )
    overview_parser.add_argument(
        "--strict", action="store_true",
        help="Exit nonzero for warnings as well as problems (requires --json)"
    )
    guide_parser = sub.add_parser("guide", help="Explain Nexus commands and features")
    guide_parser.add_argument("topic", nargs="?", help="Optional topic to explain")
    guide_parser.add_argument(
        "--search", metavar="WORD",
        help="Search topic names and help text (case-insensitive)"
    )
    sub.add_parser("version")

    kuma = sub.add_parser("kuma", help="Inspect the Uptime Kuma project")
    kuma_actions = kuma.add_subparsers(dest="kuma_action", required=True)
    kuma_actions.add_parser("status")
    kuma_log_parser = kuma_actions.add_parser("logs")
    kuma_log_parser.add_argument("-n", "--lines", type=int, default=50)

    service = sub.add_parser("service", help="Manage system services")
    actions = service.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    autostart = actions.add_parser(
        "autostart", help="Show or manage service autostart settings"
    )
    autostart_actions = autostart.add_subparsers(dest="autostart_action")
    for action in ("enable", "disable"):
        item = autostart_actions.add_parser(action)
        item.add_argument("name", choices=(*SERVICES, "sunshine"))

    for action in ("start", "stop", "restart", "enable", "disable"):
        item = actions.add_parser(action)
        item.add_argument("name", choices=(*SERVICES, "sunshine"))
    logs = actions.add_parser("logs")
    logs.add_argument("name", choices=(*SERVICES, "sunshine"))
    logs.add_argument("-n", "--lines", type=int, default=50)

    docker = sub.add_parser("docker", help="Manage Docker Compose projects")
    docker_actions = docker.add_subparsers(dest="docker_action", required=True)
    autostart_parser = docker_actions.add_parser(
        "autostart", help="View or configure Docker project startup at boot"
    )
    autostart_actions = autostart_parser.add_subparsers(
        dest="docker_autostart_action"
    )
    for action in ("enable", "disable"):
        item = autostart_actions.add_parser(action)
        item.add_argument("name", choices=tuple(DOCKER_PROJECTS))
    docker_actions.add_parser("list")
    for action in ("start", "stop", "restart"):
        item = docker_actions.add_parser(action)
        item.add_argument("name", choices=tuple(DOCKER_PROJECTS))
    for action in ("start", "stop", "restart"):
        item = docker_actions.add_parser(f"{action}-container")
        item.add_argument("name", help="Exact Docker container name")

    docker_log_parser = docker_actions.add_parser("logs")
    docker_log_parser.add_argument("name", choices=tuple(DOCKER_PROJECTS))
    docker_log_parser.add_argument("-n", "--lines", type=int, default=50)

    cmd = sub.add_parser("cmd", help="Manage custom commands")
    cmd_actions = cmd.add_subparsers(dest="cmd_action", required=True)
    cmd_actions.add_parser("list")
    add = cmd_actions.add_parser("add")
    add.add_argument("name")
    add.add_argument("argv", nargs=argparse.REMAINDER)
    run_cmd = cmd_actions.add_parser("run")
    run_cmd.add_argument("name")
    remove_cmd = cmd_actions.add_parser("remove")
    remove_cmd.add_argument("name")

    profile = sub.add_parser("profile", help="Manage command profiles")
    profile_actions = profile.add_subparsers(dest="profile_action", required=True)
    profile_actions.add_parser("list")
    add_profile = profile_actions.add_parser("add")
    add_profile.add_argument("name")
    add_profile.add_argument("commands", nargs="+", metavar="STEP")
    show_profile = profile_actions.add_parser("show")
    show_profile.add_argument("name")
    run_profile = profile_actions.add_parser("run")
    run_profile.add_argument("name")
    remove_profile = profile_actions.add_parser("remove")
    remove_profile.add_argument("name")

    args = parser.parse_args()

    if args.command == "version":
        print(f"Nexus Control Center {VERSION}")
    elif args.command == "status":
        status()
    elif args.command == "doctor":
        doctor()
    elif args.command == "health":
        health()
    elif args.command == "overview":
        if args.strict and not args.json:
            parser.error("--strict requires --json")
        overview_json(strict=args.strict) if args.json else overview()
    elif args.command == "guide":
        guide(args.topic, args.search)
    elif args.command == "kuma":
        if args.kuma_action == "status":
            kuma_status()
        else:
            if not 1 <= args.lines <= 1000:
                parser.error("log lines must be between 1 and 1000")
            kuma_logs(args.lines)
    elif args.command == "service":
        if args.action == "list":
            list_services()
        elif args.action == "autostart":
            if args.autostart_action is None:
                list_services(show_autostart=False)
            else:
                service_action(args.autostart_action, args.name)
        elif args.action == "logs":
            if not 1 <= args.lines <= 1000:
                parser.error("log lines must be between 1 and 1000")
            show_logs(args.name, args.lines)
        else:
            service_action(args.action, args.name)
    elif args.command == "docker":
        if args.docker_action == "autostart":
            docker_autostart(
                args.docker_autostart_action,
                getattr(args, "name", None),
            )
        elif args.docker_action == "list":
            docker_list()
        elif args.docker_action == "logs":
            if not 1 <= args.lines <= 1000:
                parser.error("log lines must be between 1 and 1000")
            docker_logs(args.name, args.lines)
        elif args.docker_action.endswith("-container"):
            action = args.docker_action.removesuffix("-container")
            docker_container_action(action, args.name)
        else:
            docker_action(args.docker_action, args.name)
    elif args.command == "cmd":
        if args.cmd_action == "list":
            command_list()
        elif args.cmd_action == "add":
            command_add(args.name, args.argv)
        elif args.cmd_action == "run":
            command_run(args.name)
        else:
            command_remove(args.name)
    elif args.command == "profile":
        if args.profile_action == "list":
            profile_list()
        elif args.profile_action == "add":
            profile_add(args.name, args.commands)
        elif args.profile_action == "show":
            profile_show(args.name)
        elif args.profile_action == "run":
            profile_run(args.name)
        else:
            profile_remove(args.name)


if __name__ == "__main__":
    main()
