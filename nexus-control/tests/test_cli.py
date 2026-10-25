import json
import subprocess

import pytest

from nexus_control import cli


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("hello", True),
        ("my-command_2.0", True),
        ("A" * 64, True),
        ("", False),
        ("-starts-with-hyphen", False),
        ("has space", False),
        ("has/slash", False),
        ("A" * 65, False),
    ],
)
def test_valid_name(name, expected):
    assert cli.valid_name(name) is expected


def test_load_registry_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "registry.json")

    assert cli.load_registry() == {"commands": {}, "profiles": {}}


def test_save_and_load_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "config" / "registry.json")

    data = {
        "commands": {"hello": {"argv": ["echo", "hello"]}},
        "profiles": {"demo": ["hello"]},
    }

    cli.save_registry(data)

    assert cli.load_registry() == data


def test_guide_search_is_case_insensitive(capsys):
    cli.guide(search="PrOfIlE")

    output = capsys.readouterr().out
    assert "NEXUS GUIDE SEARCH: PrOfIlE" in output
    assert "profile" in output
    assert "matching topic(s)" in output


def test_guide_search_no_match_exits_one(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.guide(search="definitely-not-a-topic")

    assert exc.value.code == 1
    assert "No matching topics found." in capsys.readouterr().out


def test_guide_unknown_topic_exits_two(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.guide(topic="not-a-topic")

    assert exc.value.code == 2
    assert "Unknown guide topic" in capsys.readouterr().out


def test_strict_requires_json(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["nexus", "overview", "--strict"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "--strict requires --json" in capsys.readouterr().err


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def mock_healthy_dependencies(monkeypatch, tmp_path, docker_output=""):
    monkeypatch.setattr(
        cli.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeResponse(),
    )

    def fake_run(command, timeout=10):
        if command[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(command, 0, docker_output, "")
        if "compose" in command and "ps" in command:
            output = json.dumps([{
                "Name": "uptime-kuma",
                "State": "running",
                "Health": "healthy",
            }])
            return subprocess.CompletedProcess(command, 0, output, "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "registry.json")


def test_overview_json_healthy_output_exits_zero(
    monkeypatch, tmp_path, capsys
):
    mock_healthy_dependencies(monkeypatch, tmp_path)

    cli.overview_json()

    report = json.loads(capsys.readouterr().out)
    assert report["overall"] == "OPERATIONAL"
    assert report["problems"] == []
    assert report["warnings"] == []
    assert report["uptime_kuma"]["status"] == "HEALTHY"
    assert report["registry"]["status"] == "OK"


def test_overview_json_strict_fails_on_unknown_docker_health(
    monkeypatch, tmp_path, capsys
):
    container = {
        "Names": "example-container",
        "State": "running",
        "Status": "Up 5 minutes",
    }
    mock_healthy_dependencies(
        monkeypatch,
        tmp_path,
        docker_output=json.dumps(container),
    )

    with pytest.raises(SystemExit) as exc:
        cli.overview_json(strict=True)

    assert exc.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["problems"] == []
    assert "docker_health_unknown" in report["warnings"]
    assert report["overall"] == "OPERATIONAL WITH WARNINGS"


def test_command_add_rejects_invalid_name(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "registry.json")

    with pytest.raises(SystemExit, match="Invalid name"):
        cli.command_add("-bad", ["echo", "hello"])


def test_command_add_rejects_missing_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)

    with pytest.raises(SystemExit, match="Executable not found"):
        cli.command_add("missing", ["nexus-test-executable-that-does-not-exist"])


def test_profile_add_rejects_unregistered_command(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "registry.json")

    with pytest.raises(SystemExit, match="Unregistered command"):
        cli.profile_add("demo", ["cmd:not-registered"])


def test_profile_add_rejects_unknown_service(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "registry.json")

    with pytest.raises(SystemExit, match="Unknown service"):
        cli.profile_add("demo", ["service:start:not-a-service"])


def test_command_run_cancelled_does_not_execute(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "registry.json")
    cli.save_registry({
        "commands": {"demo": {"argv": ["echo", "hello"]}},
        "profiles": {},
    })
    monkeypatch.setattr("builtins.input", lambda _: "no")

    def forbidden_run(*args, **kwargs):
        pytest.fail("Command should not execute after cancellation")

    monkeypatch.setattr(cli.subprocess, "run", forbidden_run)

    cli.command_run("demo")

    assert "Cancelled." in capsys.readouterr().out


def test_profile_run_stops_after_command_failure(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "registry.json")
    cli.save_registry({
        "commands": {
            "first": {"argv": ["fake-first"]},
            "second": {"argv": ["fake-second"]},
        },
        "profiles": {"demo": ["first", "second"]},
    })
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    monkeypatch.setattr(cli, "resolve_service", lambda _: None)

    calls = []

    def fake_run(argv, check=False):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 7 if argv == ["fake-first"] else 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    with pytest.raises(SystemExit, match="Profile stopped"):
        cli.profile_run("demo")

    assert calls == [["fake-first"]]
    assert "Profile completed." not in capsys.readouterr().out


def test_load_registry_rejects_malformed_json(
    tmp_path, monkeypatch, capsys
):
    registry = tmp_path / "registry.json"
    registry.write_text("{ invalid json")
    monkeypatch.setattr(cli, "REGISTRY", registry)

    with pytest.raises(SystemExit) as exc:
        cli.load_registry()

    assert exc.value.code == 2
    assert "Cannot read registry" in capsys.readouterr().err


def test_command_add_rejects_duplicate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "registry.json")
    cli.save_registry({
        "commands": {"demo": {"argv": ["echo", "existing"]}},
        "profiles": {},
    })
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/usr/bin/echo")

    with pytest.raises(SystemExit, match="Command already exists"):
        cli.command_add("demo", ["echo", "replacement"])

    assert cli.load_registry()["commands"]["demo"]["argv"] == [
        "echo", "existing"
    ]


def test_profile_add_rejects_duplicate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "registry.json")
    cli.save_registry({
        "commands": {"demo": {"argv": ["echo", "hello"]}},
        "profiles": {"starter": ["demo"]},
    })

    with pytest.raises(SystemExit, match="Profile already exists"):
        cli.profile_add("starter", ["demo"])

    assert cli.load_registry()["profiles"]["starter"] == ["demo"]


def test_command_run_handles_oserror(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "registry.json")
    cli.save_registry({
        "commands": {"demo": {"argv": ["fake-program"]}},
        "profiles": {},
    })
    monkeypatch.setattr("builtins.input", lambda _: "yes")

    def fake_run(*args, **kwargs):
        raise OSError("simulated launch failure")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    with pytest.raises(SystemExit, match="Could not run command") as exc:
        cli.command_run("demo")

    assert "simulated launch failure" in str(exc.value)



def test_profile_run_handles_oserror(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "REGISTRY", tmp_path / "registry.json")
    cli.save_registry({
        "commands": {"demo": {"argv": ["fake-program"]}},
        "profiles": {"starter": ["demo"]},
    })
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    monkeypatch.setattr(cli, "resolve_service", lambda _: None)

    def fake_run(*args, **kwargs):
        raise OSError("simulated launch failure")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    with pytest.raises(SystemExit, match="Failed to launch demo"):
        cli.profile_run("starter")


@pytest.mark.parametrize(
    "registry_data",
    [
        {"commands": {"demo": {}}, "profiles": {}},
        {"commands": {"demo": {"argv": "echo hello"}}, "profiles": {}},
        {"commands": {"demo": {"argv": []}}, "profiles": {}},
        {"commands": {"demo": None}, "profiles": {}},
        {"commands": {}, "profiles": {"demo": "not-a-list"}},
        {"commands": {}, "profiles": {"demo": [123]}},
    ],
)
def test_load_registry_rejects_invalid_entries(
    tmp_path, monkeypatch, capsys, registry_data
):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(registry_data))
    monkeypatch.setattr(cli, "REGISTRY", registry)

    with pytest.raises(SystemExit) as exc:
        cli.load_registry()

    assert exc.value.code == 2
    assert "Cannot read registry" in capsys.readouterr().err


@pytest.mark.parametrize(
    "steps",
    [
        ["cmd:missing-command"],
        ["missing-command"],
        ["service:start:unknown-service"],
        ["service:launch:jellyfin"],
        ["service:start:jellyfin:extra"],
    ],
)
def test_load_registry_rejects_invalid_profile_references(
    tmp_path, monkeypatch, capsys, steps
):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "commands": {"demo": {"argv": ["echo", "hello"]}},
        "profiles": {"demo-profile": steps},
    }))
    monkeypatch.setattr(cli, "REGISTRY", registry)

    with pytest.raises(SystemExit) as exc:
        cli.load_registry()

    assert exc.value.code == 2
    assert "Cannot read registry" in capsys.readouterr().err


def test_load_registry_accepts_valid_profile_references(
    tmp_path, monkeypatch
):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "commands": {"demo": {"argv": ["echo", "hello"]}},
        "profiles": {
            "demo-profile": [
                "demo",
                "cmd:demo",
                "service:start:jellyfin",
            ]
        },
    }))
    monkeypatch.setattr(cli, "REGISTRY", registry)

    data = cli.load_registry()

    assert data["profiles"]["demo-profile"] == [
        "demo",
        "cmd:demo",
        "service:start:jellyfin",
    ]


def test_save_registry_replaces_file_and_removes_temp(
    tmp_path, monkeypatch
):
    registry = tmp_path / "registry.json"
    registry.write_text('{"old": true}\n')
    monkeypatch.setattr(cli, "REGISTRY", registry)
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)

    data = {"commands": {}, "profiles": {}}
    cli.save_registry(data)

    assert json.loads(registry.read_text()) == data
    assert not registry.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("failure_stage", ["write", "replace"])
def test_save_registry_cleans_temp_on_failure(
    tmp_path, monkeypatch, failure_stage
):
    registry = tmp_path / "registry.json"
    original = '{"commands": {"old": {"argv": ["echo"]}}, "profiles": {}}\n'
    registry.write_text(original)
    monkeypatch.setattr(cli, "REGISTRY", registry)
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)

    temporary = registry.with_suffix(".json.tmp")
    path_type = type(registry)

    if failure_stage == "write":
        original_write_text = path_type.write_text

        def failing_write_text(path, *args, **kwargs):
            if path == temporary:
                raise OSError("simulated write failure")
            return original_write_text(path, *args, **kwargs)

        monkeypatch.setattr(path_type, "write_text", failing_write_text)
    else:
        original_replace = path_type.replace

        def failing_replace(path, target):
            if path == temporary:
                raise OSError("simulated replace failure")
            return original_replace(path, target)

        monkeypatch.setattr(path_type, "replace", failing_replace)

    with pytest.raises(OSError):
        cli.save_registry({"commands": {}, "profiles": {}})

    assert registry.read_text() == original
    assert not temporary.exists()


def test_save_registry_preserves_original_if_replace_fails(
    tmp_path, monkeypatch
):
    registry = tmp_path / "registry.json"
    original = '{"commands": {}, "profiles": {"keep": ["demo"]}}\n'
    registry.write_text(original)
    monkeypatch.setattr(cli, "REGISTRY", registry)
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)

    path_type = type(registry)
    original_replace = path_type.replace
    temporary = registry.with_suffix(".json.tmp")

    def failing_replace(path, target):
        if path == temporary:
            raise OSError("simulated replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(path_type, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        cli.save_registry({"commands": {}, "profiles": {}})

    assert registry.read_text() == original
    assert not temporary.exists()


def test_docker_stop_requires_confirmation(tmp_path, monkeypatch, capsys):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}")
    monkeypatch.setattr(cli, "DOCKER_PROJECTS", {"odysseus": compose_file})
    monkeypatch.setattr("builtins.input", lambda _: "no")
    calls = []
    monkeypatch.setattr(cli, "run", lambda *a, **k: calls.append(a) or None)

    cli.docker_action("stop", "odysseus")

    assert calls == []
    assert "Cancelled." in capsys.readouterr().out


def test_docker_stop_calls_compose_stop(tmp_path, monkeypatch, capsys):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}")
    monkeypatch.setattr(cli, "DOCKER_PROJECTS", {"odysseus": compose_file})
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    calls = []

    def fake_run(command, timeout=10):
        calls.append(command)
        if "stop" in command:
            return __import__("subprocess").CompletedProcess(
                command, 0, "stopped\n", ""
            )
        return __import__("subprocess").CompletedProcess(
            command, 0, "[]", ""
        )

    monkeypatch.setattr(cli, "run", fake_run)
    cli.docker_action("stop", "odysseus")

    assert calls[0][-1] == "stop"
    assert "Result: stopped" in capsys.readouterr().out


def test_docker_container_stop_validates_name_and_confirms(monkeypatch, capsys):
    calls = []

    def fake_run(command, timeout=10):
        calls.append(command)
        if command[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(
                command, 0, "localnexus-postgres-1\n", ""
            )
        return subprocess.CompletedProcess(command, 0, "stopped\\n", "")

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda _: "yes")

    cli.docker_container_action("stop", "localnexus-postgres-1")

    assert calls[0][:3] == ["docker", "ps", "-a"]
    assert calls[1] == ["docker", "stop", "localnexus-postgres-1"]
    assert "Result: stop command completed" in capsys.readouterr().out


def test_docker_container_action_rejects_unknown_name(monkeypatch):
    monkeypatch.setattr(
        cli, "run",
        lambda command, timeout=10: subprocess.CompletedProcess(
            command, 0, "uptime-kuma\\n", ""
        ),
    )

    with pytest.raises(SystemExit) as exc:
        cli.docker_container_action("stop", "not-a-real-container")

    assert exc.value.code == 2



@pytest.mark.parametrize(
    ("action", "state"),
    [("enable", "enabled"), ("disable", "disabled")],
)
def test_service_autostart_noop_when_already_in_requested_state(
    monkeypatch, capsys, action, state
):
    monkeypatch.setattr(cli, "resolve_service", lambda name: ("system", "demo.service"))
    monkeypatch.setattr(cli, "autostart_state", lambda scope, unit: state)

    def forbidden_systemctl(*args):
        pytest.fail("systemctl should not run when autostart is already in the requested state")

    monkeypatch.setattr(cli, "systemctl", forbidden_systemctl)

    cli.service_action(action, "demo")

    assert f"already {state}" in capsys.readouterr().out


@pytest.mark.parametrize("state", ["static", "masked", "indirect", "generated", "transient"])
def test_service_autostart_refuses_special_states(
    monkeypatch, state, capsys
):
    monkeypatch.setattr(cli, "resolve_service", lambda name: ("system", "demo.service"))
    monkeypatch.setattr(cli, "autostart_state", lambda scope, unit: state)

    def forbidden_systemctl(*args):
        pytest.fail("systemctl should not run for a special unit state")

    monkeypatch.setattr(cli, "systemctl", forbidden_systemctl)

    with pytest.raises(SystemExit) as exc:
        cli.service_action("enable", "demo")

    assert exc.value.code == 2
    assert state in capsys.readouterr().out


def test_docker_autostart_unit_content(monkeypatch, tmp_path):
    compose_file = tmp_path / "odysseus" / "docker-compose.yml"
    compose_file.parent.mkdir()
    compose_file.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setitem(cli.DOCKER_PROJECTS, "odysseus", compose_file)

    content = cli.docker_autostart_unit_content("odysseus")

    assert "# Managed by Nexus Control Center" in content
    assert "# Project: odysseus" in content
    assert f"User={cli.Path.home().name}" in content
    assert "Requires=docker.service" in content
    assert "After=docker.service" in content
    assert "RemainAfterExit=yes" in content
    assert "up -d" in content
    assert "WantedBy=multi-user.target" in content
    assert str(compose_file) in content


def test_docker_autostart_unit_name():
    assert cli.docker_autostart_unit_name("odysseus") == (
        "nexus-docker-odysseus.service"
    )
