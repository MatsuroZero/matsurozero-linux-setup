from flask import Flask, request, jsonify, render_template, session, redirect, url_for, send_file, Response
import getpass
import os
import pwd
import psutil
import re
import shutil
import subprocess
import time
import urllib.request
import urllib.parse
import json
import io
import platform
import socket
import hashlib
import statistics
import ctypes
import ctypes.util
import secrets
import threading
import signal
import logging
import mimetypes
import sqlite3
import ipaddress
from logging.handlers import RotatingFileHandler
from functools import wraps
from contextlib import contextmanager
from pathlib import Path

app = Flask(__name__)
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "7171"))

# Quiet, bounded application logs: routine polling endpoints are not logged by
# Werkzeug, while meaningful warnings/errors are retained in a rotating file.
try:
    _log_user = os.environ.get("DASHBOARD_USER") or getpass.getuser()
    _log_home = Path(pwd.getpwnam(_log_user).pw_dir)
    _log_dir = Path(os.environ.get("XDG_STATE_HOME", str(_log_home / ".local/state"))) / "system-dashboard"
    _log_dir.mkdir(parents=True, exist_ok=True)
    try: os.chmod(_log_dir, 0o700)
    except OSError: pass
    _handler = RotatingFileHandler(_log_dir / "dashboard.log", maxBytes=1_500_000, backupCount=3, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    app.logger.handlers.clear()
    app.logger.addHandler(_handler)
    for _log_file in [_log_dir / "dashboard.log", *[_log_dir / f"dashboard.log.{i}" for i in range(1, 4)]]:
        try: os.chmod(_log_file, 0o600)
        except OSError: pass
    app.logger.setLevel(logging.INFO)
    app.logger.propagate = False
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
# The reserved dashboard administrator is named marwan and authenticates with
# the Linux password via PAM. All other users are dashboard-only SQLite accounts.
# DASHBOARD_USER only locates the dashboard data directory and service home.
DASHBOARD_USER = os.environ.get("DASHBOARD_USER") or getpass.getuser()
def _is_dashboard_admin(username, role=None):
    """Only the dashboard-only account named marwan is an administrator.

    Linux users and privileges are unrelated to dashboard roles. The role is
    stored in the dashboard SQLite database and checked again on every request.
    """
    return str(username or "").casefold() == "marwan" and role == "admin"

SECRET_PATH = Path.home() / ".config/system-dashboard/secret_key"

def _load_secret_key():
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        if SECRET_PATH.is_file():
            value = SECRET_PATH.read_text(encoding="utf-8").strip()
            if value:
                os.chmod(SECRET_PATH, 0o600)
                return value
        value = secrets.token_hex(32)
        SECRET_PATH.write_text(value + "\n", encoding="utf-8")
        os.chmod(SECRET_PATH, 0o600)
        return value
    except OSError:
        # Fallback is only for unusual read-only home directories.
        # A process-local random key is still safer than a hard-coded key.
        return secrets.token_hex(32)

app.secret_key = _load_secret_key()
app.config.update(
    MAX_CONTENT_LENGTH=None,
    MAX_FORM_MEMORY_SIZE=None,
    MAX_FORM_PARTS=None,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,  # Set True when served behind HTTPS.
    PERMANENT_SESSION_LIFETIME=1800,
)

_login_failures = {}
_LOGIN_WINDOW = 60
_LOGIN_MAX_FAILURES = 5

def _client_key():
    return request.remote_addr or "unknown"

def _rate_limited():
    now = time.monotonic()
    key = _client_key()
    attempts = [t for t in _login_failures.get(key, []) if now - t < _LOGIN_WINDOW]
    _login_failures[key] = attempts
    return len(attempts) >= _LOGIN_MAX_FAILURES

def _record_login_failure():
    key = _client_key()
    _login_failures.setdefault(key, []).append(time.monotonic())

def _clear_login_failures():
    _login_failures.pop(_client_key(), None)

@app.errorhandler(500)
def dashboard_internal_error(error):
    # Keep production responses useful without exposing stack traces or secrets.
    original = getattr(error, "original_exception", None)
    if original is not None:
        app.logger.error("Unhandled dashboard request exception", exc_info=(type(original), original, original.__traceback__))
    else:
        app.logger.error("Dashboard request returned HTTP 500: %s", str(error)[:240])
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "The request failed on the dashboard server. Check system-dashboard.service logs for details."}), 500
    return ("""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dashboard hiccup</title><body style="font:16px system-ui;max-width:680px;margin:12vh auto;padding:24px;background:#171820;color:#f2f2f7;border-radius:18px"><h1>Dashboard hiccup</h1><p>This request failed, but the dashboard service is still running. Your files and settings were not intentionally changed by this error.</p><p>Check <code>journalctl -u system-dashboard.service</code> for the detailed cause, then return to the dashboard.</p><p><a style="color:#b2c5ff" href="/login">Return to login</a></p></body></html>""", 500)


@app.before_request
def require_login():
    if request.endpoint in {"login", "logout"} or request.path.startswith("/static/"):
        return None
    if request.method == "GET" and request.endpoint in {"dashboard_login_users", "serpantinum_theme"}:
        return None
    if session.get("authenticated"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    return redirect(url_for("login"))

def _verify_linux_pam(username, password):
    """Authenticate the reserved dashboard administrator through Linux PAM.

    Only the fixed dashboard admin username uses PAM. Every other account is
    checked against the dashboard's own SQLite password hashes.
    """
    if username != "marwan" or not password:
        return False
    try:
        libname = ctypes.util.find_library("pam")
        if not libname:
            return False
        libpam = ctypes.CDLL(libname)
        libc = ctypes.CDLL(None)
        libc.malloc.argtypes = [ctypes.c_size_t]
        libc.malloc.restype = ctypes.c_void_p
        libc.memset.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
        libc.memmove.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        class PamMessage(ctypes.Structure):
            _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]
        class PamResponse(ctypes.Structure):
            _fields_ = [("resp", ctypes.c_char_p), ("resp_retcode", ctypes.c_int)]
        Conversation = ctypes.CFUNCTYPE(ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.POINTER(PamMessage)),
            ctypes.POINTER(ctypes.POINTER(PamResponse)),
            ctypes.c_void_p)
        class PamConv(ctypes.Structure):
            _fields_ = [("conv", Conversation), ("appdata_ptr", ctypes.c_void_p)]
        pam_start = libpam.pam_start
        pam_start.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(PamConv), ctypes.POINTER(ctypes.c_void_p)]
        pam_start.restype = ctypes.c_int
        pam_authenticate = libpam.pam_authenticate
        pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
        pam_authenticate.restype = ctypes.c_int
        pam_acct_mgmt = libpam.pam_acct_mgmt
        pam_acct_mgmt.argtypes = [ctypes.c_void_p, ctypes.c_int]
        pam_acct_mgmt.restype = ctypes.c_int
        pam_end = libpam.pam_end
        pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
        pam_end.restype = ctypes.c_int
        password_bytes = password.encode("utf-8")
        username_bytes = username.encode("utf-8")
        def converse(num_msg, messages, response_ptr, appdata):
            response_mem = None
            try:
                response_mem = libc.malloc(ctypes.sizeof(PamResponse) * num_msg)
                if not response_mem:
                    return 5
                libc.memset(response_mem, 0, ctypes.sizeof(PamResponse) * num_msg)
                answers = ctypes.cast(response_mem, ctypes.POINTER(PamResponse))
                for i in range(num_msg):
                    style = messages[i].contents.msg_style
                    if style == 1:  # PAM_PROMPT_ECHO_OFF: password
                        data = password_bytes
                    elif style == 2:  # PAM_PROMPT_ECHO_ON: username
                        data = username_bytes
                    else:
                        answers[i].resp = None
                        answers[i].resp_retcode = 0
                        continue
                    buf = libc.malloc(len(data) + 1)
                    if not buf:
                        return 5
                    source = ctypes.create_string_buffer(data, len(data))
                    libc.memmove(buf, source, len(data))
                    ctypes.memset(ctypes.c_void_p(buf + len(data)), 0, 1)
                    answers[i].resp = ctypes.cast(buf, ctypes.c_char_p)
                    answers[i].resp_retcode = 0
                # PAM owns this malloc-allocated response array and its strings.
                response_ptr[0] = answers
                return 0
            except Exception:
                return 5
        callback = Conversation(converse)
        conversation = PamConv(callback, None)
        handle = ctypes.c_void_p()
        # system-auth is the standard Arch/CachyOS PAM stack; login is fallback.
        status = pam_start(b"system-auth", username_bytes, ctypes.byref(conversation), ctypes.byref(handle))
        if status != 0:
            status = pam_start(b"login", username_bytes, ctypes.byref(conversation), ctypes.byref(handle))
        if status != 0 or not handle.value:
            return False
        try:
            status = pam_authenticate(handle, 0)
            if status == 0:
                status = pam_acct_mgmt(handle, 0)
            return status == 0
        finally:
            pam_end(handle, status)
    except Exception as exc:
        app.logger.warning("PAM administrator authentication unavailable: %s", str(exc)[:180])
        return False


@app.route("/login", methods=["GET", "POST"])
def login():
    # A POST must be allowed to authenticate the submitted account even if an
    # older session cookie is present. Clear it first to prevent account
    # switching from silently retaining the previous user's identity.
    if request.method == "POST":
        session.clear()
    elif session.get("authenticated"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if _rate_limited():
            error = "Too many failed attempts. Wait a minute and try again."
        else:
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            # marwan is the one exception: reuse the Linux account password via
            # PAM. All other usernames/passwords exist only in dashboard SQLite.
            try:
                if username.casefold() == "marwan":
                    username = "marwan"
                    ok = _verify_linux_pam(username, password)
                    role = "admin" if ok else "user"
                else:
                    ok = _dashboard_verify_user(username, password)
                    role = _dashboard_user_role(username) if ok else "user"
            except Exception as exc:
                app.logger.warning("Login verification failed: %s", str(exc)[:250])
                ok = False
                role = "user"
                error = "Login service is temporarily unavailable. Please retry; if it continues, ask the dashboard administrator to check the service logs."
            if ok:
                _clear_login_failures()
                session.clear()
                session.permanent = True
                session["authenticated"] = True
                session["username"] = username
                session["role"] = role
                session["is_admin"] = _is_dashboard_admin(username, role)
                _audit_record(username, "auth.login", "Signed in", request.path, request.method, 200)
                return redirect(url_for("index"))
            if not error:
                _record_login_failure()
                error = "Invalid username or password."
    return render_template("login.html", username=(request.form.get("username") or "").strip()[:32], error=error)

@app.route("/logout", methods=["POST", "GET"])
def logout():
    if session.get("authenticated"):
        _audit_record(session.get("username"), "auth.logout", "Signed out", request.path, request.method, 302)
    session.clear()
    return redirect(url_for("login"))

# The dashboard HTML lives here:
# ~/system-dashboard/templates/dashboard.html
@app.route("/")
def index():
    username = session.get("username", DASHBOARD_USER)
    # Recompute admin visibility from the authenticated identity on every page
    # render instead of trusting a possibly stale session flag.
    current_role = _dashboard_user_role(username) if session.get("authenticated") else "user"
    is_admin = bool(session.get("authenticated") and _is_dashboard_admin(username, current_role))
    session["role"] = current_role if session.get("authenticated") else "user"
    session["is_admin"] = is_admin
    return render_template("dashboard.html", username=username, is_admin=is_admin)


def run_command(command, timeout=10, env=None):
    """Run a command and return (success, stdout, stderr)."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return False, "", str(exc)



# Dashboard terminal: run commands through the user's real Zsh environment.
# This is intentionally the same user account as the dashboard service (marwan), not root.
# ---------------------------------------------------------------------------
# Dashboard PTY terminal
# ---------------------------------------------------------------------------
# This is deliberately a PTY rather than subprocess.run(). TUIs such as cava,
# btop, yazi, vim, sl, etc. need a real terminal device for cursor movement,
# ANSI escape sequences, keyboard input, resizing, and continuous output.
TERMINAL_LOCK = threading.RLock()
TERMINAL_PROC = None
TERMINAL_MASTER_FD = None
TERMINAL_CWD = str(Path.home().resolve())
TERMINAL_SHELL = None
TERMINAL_BUFFER = bytearray()
TERMINAL_BUFFER_LIMIT = 2 * 1024 * 1024


def _terminal_login_shell():
    username = os.environ.get("DASHBOARD_USER") or getpass.getuser()
    try:
        shell = pwd.getpwnam(username).pw_shell
    except Exception:
        shell = os.environ.get("SHELL") or "/bin/sh"
    if not shell or not Path(shell).is_file() or not os.access(shell, os.X_OK):
        shell = "/bin/sh"
    return shell


def _terminal_env():
    env = os.environ.copy()
    username = os.environ.get("DASHBOARD_USER") or getpass.getuser()
    try:
        pw = pwd.getpwnam(username)
        env["HOME"] = pw.pw_dir
        env["USER"] = username
        env["LOGNAME"] = username
        env["SHELL"] = pw.pw_shell or env.get("SHELL", "/bin/sh")
        uid = pw.pw_uid
    except Exception:
        uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    env.setdefault("TERM_PROGRAM", "system-dashboard")
    # A systemd service may not inherit the graphical session variables.
    runtime = Path(env["XDG_RUNTIME_DIR"])
    if "WAYLAND_DISPLAY" not in env:
        sockets = sorted(runtime.glob("wayland-*")) if runtime.is_dir() else []
        if sockets:
            env["WAYLAND_DISPLAY"] = sockets[0].name
    if "DBUS_SESSION_BUS_ADDRESS" not in env and (runtime / "bus").exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime / 'bus'}"
    return env


def _terminal_read_available():
    global TERMINAL_BUFFER
    if TERMINAL_MASTER_FD is None:
        return
    import select
    while True:
        try:
            ready, _, _ = select.select([TERMINAL_MASTER_FD], [], [], 0)
        except (OSError, ValueError):
            return
        if not ready:
            return
        try:
            chunk = os.read(TERMINAL_MASTER_FD, 65536)
        except BlockingIOError:
            return
        except OSError:
            return
        if not chunk:
            return
        TERMINAL_BUFFER.extend(chunk)
        if len(TERMINAL_BUFFER) > TERMINAL_BUFFER_LIMIT:
            del TERMINAL_BUFFER[:-TERMINAL_BUFFER_LIMIT]


def _terminal_start(cwd=None):
    global TERMINAL_PROC, TERMINAL_MASTER_FD, TERMINAL_CWD, TERMINAL_SHELL
    import fcntl
    import pty
    import struct
    import termios

    with TERMINAL_LOCK:
        if TERMINAL_PROC is not None and TERMINAL_PROC.poll() is None and TERMINAL_MASTER_FD is not None:
            return
        if TERMINAL_MASTER_FD is not None:
            try: os.close(TERMINAL_MASTER_FD)
            except OSError: pass
            TERMINAL_MASTER_FD = None
        TERMINAL_BUFFER.clear()
        target = Path(cwd or TERMINAL_CWD).expanduser().resolve()
        if not target.is_dir():
            target = Path.home().resolve()
        shell = _terminal_login_shell()
        master, slave = pty.openpty()
        winsize = struct.pack("HHHH", 32, 120, 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, winsize)

        def child_setup():
            os.setsid()
            try:
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        env = _terminal_env()
        TERMINAL_PROC = subprocess.Popen(
            [shell, "-i", "-l"],
            stdin=slave, stdout=slave, stderr=slave,
            cwd=str(target), env=env,
            preexec_fn=child_setup,
            start_new_session=False,
            close_fds=True,
        )
        os.close(slave)
        os.set_blocking(master, False)
        TERMINAL_MASTER_FD = master
        TERMINAL_CWD = str(target)
        TERMINAL_SHELL = shell
        _terminal_read_available()


def _terminal_stop():
    global TERMINAL_PROC, TERMINAL_MASTER_FD
    with TERMINAL_LOCK:
        proc = TERMINAL_PROC
        fd = TERMINAL_MASTER_FD
        TERMINAL_PROC = None
        TERMINAL_MASTER_FD = None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGHUP)
            except Exception:
                try: proc.terminate()
                except Exception: pass
        if fd is not None:
            try: os.close(fd)
            except OSError: pass


def _terminal_resize(rows, cols):
    import fcntl, struct, termios
    if TERMINAL_MASTER_FD is None:
        return
    rows = max(5, min(int(rows or 32), 200))
    cols = max(20, min(int(cols or 120), 300))
    fcntl.ioctl(TERMINAL_MASTER_FD, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


@app.route("/api/terminal", methods=["GET", "POST"])
def terminal_api():
    global TERMINAL_CWD, TERMINAL_BUFFER
    if request.method == "GET":
        with TERMINAL_LOCK:
            _terminal_start()
            _terminal_read_available()
            data = bytes(TERMINAL_BUFFER)
            TERMINAL_BUFFER.clear()
            proc = TERMINAL_PROC
            return jsonify({"ok": True, "cwd": TERMINAL_CWD,
                            "shell": TERMINAL_SHELL or _terminal_login_shell(),
                            "user": getpass.getuser(), "running": bool(proc and proc.poll() is None),
                            "data": data.decode("utf-8", "replace")})

    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "input"))
    try:
        with TERMINAL_LOCK:
            if action == "start":
                _terminal_start(data.get("cwd") or TERMINAL_CWD)
            elif action == "reset":
                _terminal_stop()
                TERMINAL_CWD = str(Path.home().resolve())
                _terminal_start(TERMINAL_CWD)
            elif action == "input":
                _terminal_start()
                raw = data.get("data", "")
                if not isinstance(raw, str):
                    raise ValueError("Terminal input must be text")
                if len(raw) > 65536:
                    raise ValueError("Terminal input is too large")
                os.write(TERMINAL_MASTER_FD, raw.encode("utf-8", "replace"))
            elif action == "resize":
                _terminal_start()
                _terminal_resize(data.get("rows", 32), data.get("cols", 120))
            elif action == "stop":
                _terminal_stop()
            else:
                raise ValueError("Unknown terminal action")
            _terminal_read_available()
            output = bytes(TERMINAL_BUFFER)
            TERMINAL_BUFFER.clear()
            proc = TERMINAL_PROC
            return jsonify({"ok": True, "cwd": TERMINAL_CWD,
                            "shell": TERMINAL_SHELL or _terminal_login_shell(),
                            "user": getpass.getuser(), "running": bool(proc and proc.poll() is None),
                            "data": output.decode("utf-8", "replace")})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

@app.route("/api/stats", methods=["GET"])
def stats():
    try:
        cpu = psutil.cpu_percent(interval=0.05)
    except Exception:
        cpu = 0.0

    try:
        mem = psutil.virtual_memory()
        memory = {
            "percent": round(float(mem.percent), 1),
            "used_gb": round(mem.used / (1024 ** 3), 2),
            "total_gb": round(mem.total / (1024 ** 3), 2),
            "available_gb": round(mem.available / (1024 ** 3), 2),
        }
    except Exception:
        memory = {"percent": 0.0, "used_gb": 0.0, "total_gb": 0.0, "available_gb": 0.0}

    try:
        disk = psutil.disk_usage("/")
        disk_data = {
            "percent": round(float(disk.percent), 1),
            "used_gb": round(disk.used / (1024 ** 3), 2),
            "total_gb": round(disk.total / (1024 ** 3), 2),
            "free_gb": round(disk.free / (1024 ** 3), 2),
        }
    except Exception:
        disk_data = {"percent": 0.0, "used_gb": 0.0, "total_gb": 0.0, "free_gb": 0.0}

    cpu_temp = None
    try:
        temperatures = psutil.sensors_temperatures(fahrenheit=False) or {}
        preferred = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz")
        for sensor_name in preferred:
            readings = temperatures.get(sensor_name, [])
            values = [r.current for r in readings if r.current is not None and -20 < r.current < 150]
            if values:
                cpu_temp = round(max(values), 1)
                break
        if cpu_temp is None:
            values = [r.current for readings in temperatures.values() for r in readings if r.current is not None and -20 < r.current < 150]
            if values:
                cpu_temp = round(max(values), 1)
    except Exception:
        pass
    if cpu_temp is None:
        try:
            for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
                for raw in hwmon.glob("temp*_input"):
                    try:
                        value = int(raw.read_text().strip()) / 1000.0
                        if -20 < value < 150:
                            cpu_temp = round(value, 1)
                            break
                    except (OSError, ValueError):
                        continue
                if cpu_temp is not None:
                    break
        except Exception:
            pass

    def command_output(cmd, timeout=2):
        ok, out, _ = run_command(cmd, timeout=timeout)
        return out if ok else ""

    cpu_name = platform.processor() or ""
    try:
        cpu_name = (Path("/proc/cpuinfo").read_text(errors="ignore").split("model name\t: ", 1)[1].splitlines()[0].strip()) if "model name\t:" in Path("/proc/cpuinfo").read_text(errors="ignore") else cpu_name
    except Exception:
        pass
    gpu_lines = command_output(["sh", "-c", "lspci -mm 2>/dev/null | grep -Ei 'VGA|3D|Display' | head -5"]).splitlines()
    gpus = []
    for line in gpu_lines:
        parts = line.split('"')
        if len(parts) >= 6:
            gpus.append(parts[5])
        else:
            gpus.append(line)
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        load = [0, 0, 0]
    swap = psutil.swap_memory()
    disks = []
    try:
        for p in psutil.disk_partitions(all=False):
            if p.mountpoint.startswith('/run'):
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
                disks.append({"device": p.device, "mount": p.mountpoint, "fstype": p.fstype, "total_gb": round(u.total/(1024**3),2), "free_gb": round(u.free/(1024**3),2), "percent": round(u.percent,1)})
            except Exception:
                continue
    except Exception:
        pass
    net = []
    try:
        for name, st in (psutil.net_if_stats() or {}).items():
            addrs = psutil.net_if_addrs().get(name, [])
            ips = [x.address for x in addrs if getattr(x, 'family', None) == socket.AF_INET]
            net.append({"name": name, "up": bool(st.isup), "speed_mbps": int(st.speed or 0), "ip": ips[0] if ips else ""})
    except Exception:
        pass

    # GPU details: prefer DRM sysfs for VRAM, then lspci/OpenGL metadata.
    gpu_info = []
    try:
        drm_root = Path("/sys/class/drm")
        seen = set()
        for card in sorted(drm_root.glob("card[0-9]")):
            dev = card / "device"
            if not dev.exists():
                continue
            vendor = (dev / "vendor").read_text().strip() if (dev / "vendor").exists() else ""
            device_id = (dev / "device").read_text().strip() if (dev / "device").exists() else ""
            vram_total = None
            vram_used = None
            for name, key in (("mem_info_vram_total", "vram_total"), ("mem_info_vram_used", "vram_used")):
                f = dev / name
                if f.exists():
                    try:
                        val = int(f.read_text().strip())
                        if key == "vram_total": vram_total = round(val / (1024**3), 2)
                        else: vram_used = round(val / (1024**3), 2)
                    except Exception:
                        pass
            if vram_total is None:
                f = dev / "resource2"
                # Intel integrated GPUs generally expose shared memory rather than dedicated VRAM.
            info = {"card": card.name, "vendor_id": vendor, "device_id": device_id, "vram_total_gb": vram_total, "vram_used_gb": vram_used}
            if str(info) not in seen:
                seen.add(str(info)); gpu_info.append(info)
    except Exception:
        pass
    try:
        lspci_gpu = command_output(["sh", "-c", "lspci -nnk 2>/dev/null | grep -EA4 'VGA|3D|Display'"]).strip()
    except Exception:
        lspci_gpu = ""
    try:
        glx = command_output(["sh", "-c", "glxinfo -B 2>/dev/null | grep -E 'OpenGL vendor|OpenGL renderer|OpenGL version|Video memory'"]).splitlines()
    except Exception:
        glx = []
    # Intel/AMD integrated graphics usually borrow system RAM dynamically rather
    # than having a fixed dedicated VRAM chip. Report this explicitly.
    gpu_renderer = ""
    for line in glx:
        if "OpenGL renderer" in line:
            gpu_renderer = line.split(":", 1)[-1].strip()
            break
    if not gpu_renderer:
        gpu_renderer = next((g for g in gpus if g), "")
    integrated_gpu = bool(re.search(r"Intel.*(HD Graphics|UHD Graphics|Iris)|AMD.*(Radeon Graphics|Vega)", gpu_renderer, re.I))
    shared_graphics = {"available": integrated_gpu, "type": "Shared system RAM (dynamically allocated)" if integrated_gpu else "Dedicated VRAM / driver-reported memory", "system_ram_gb": round(psutil.virtual_memory().total/(1024**3), 2) if integrated_gpu else None, "renderer": gpu_renderer}

    # Connected physical-ish devices available without extra Python libraries.
    connected = {"usb": [], "bluetooth": [], "input": [], "storage": []}
    try:
        lsusb = command_output(["lsusb"], timeout=4)
        connected["usb"] = [x.strip() for x in lsusb.splitlines() if x.strip()]
    except Exception:
        pass
    try:
        bt = command_output(["bluetoothctl", "devices"], timeout=4)
        connected["bluetooth"] = [x.strip() for x in bt.splitlines() if x.strip()]
    except Exception:
        pass
    try:
        input_root = Path("/dev/input/by-id")
        if input_root.exists():
            connected["input"] = sorted(str(x.name) for x in input_root.iterdir() if x.is_symlink())
    except Exception:
        pass
    try:
        lsblk = command_output(["lsblk", "-dn", "-o", "NAME,MODEL,SIZE,TYPE,MOUNTPOINT"], timeout=4)
        connected["storage"] = [x.strip() for x in lsblk.splitlines() if x.strip()]
    except Exception:
        pass

    return jsonify({
        "ok": True, "cpu_percent": cpu, "memory": memory, "disk": disk_data, "cpu_temp": cpu_temp,
        "gpu_details": gpu_info, "gpu_lspci": lspci_gpu, "gpu_glx": glx, "shared_graphics": shared_graphics,
        "connected_devices": connected,
        "device": {
            "hostname": socket.gethostname(), "os": platform.platform(), "kernel": platform.release(),
            "arch": platform.machine(), "python": platform.python_version(), "cpu": cpu_name,
            "logical_cpus": psutil.cpu_count(logical=True) or 0, "physical_cpus": psutil.cpu_count(logical=False) or 0,
            "cpu_freq_mhz": round((psutil.cpu_freq().current if psutil.cpu_freq() else 0), 0), "gpus": gpus,
            "boot_time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(psutil.boot_time())), "load": load,
            "swap_percent": round(float(swap.percent),1), "swap_used_gb": round(swap.used/(1024**3),2), "swap_total_gb": round(swap.total/(1024**3),2),
            "disks": disks, "network": net,
        }
    })


@app.route("/api/benchmark", methods=["POST"])
def benchmark():
    """Short, repeatable local benchmark. It does not write to the user's disk."""
    try:
        duration = max(0.5, min(4.0, float((request.get_json(silent=True) or {}).get("seconds", 1.5))))
    except (TypeError, ValueError):
        duration = 1.5
    # Integer mixing + SHA-256 gives a stable CPU workload without disk writes.
    deadline = time.perf_counter() + duration
    value = 0x123456789ABCDEF
    blocks = 0
    started = time.perf_counter()
    while time.perf_counter() < deadline:
        value = (value * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        hashlib.sha256(value.to_bytes(8, "little")).digest()
        blocks += 1
    elapsed = max(time.perf_counter() - started, 1e-9)
    return jsonify({
        "ok": True,
        "seconds": round(elapsed, 3),
        "iterations": blocks,
        "iterations_per_second": round(blocks / elapsed, 0),
        "cpu": psutil.cpu_percent(interval=0.05),
    })


@app.route("/api/uptime", methods=["GET"])
def uptime():
    seconds = max(0, time.time() - psutil.boot_time())
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)

    if days:
        value = f"{days}d {hours}h {minutes}m"
    else:
        value = f"{hours}h {minutes}m"

    return jsonify({"ok": True, "uptime": value})


@app.route("/api/volume", methods=["POST"])
def volume():
    data = request.get_json(silent=True) or {}

    try:
        level = max(0, min(100, int(data.get("level", 50))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid volume level"}), 400

    # PipeWire/PulseAudio compatibility.
    if shutil.which("wpctl"):
        ok, _, err = run_command(
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level}%"],
            env=graphical_environment(),
        )
    elif shutil.which("pactl"):
        ok, _, err = run_command(
            ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"],
            env=graphical_environment(),
        )
    else:
        return jsonify({"ok": False, "error": "wpctl/pactl is not installed"}), 500

    if not ok:
        return jsonify({"ok": False, "error": err or "Could not change volume"}), 500

    return jsonify({"ok": True, "level": level})


def _nmcli_parse_terse_line(line):
    """Parse nmcli -t output while respecting backslash-escaped separators."""
    fields = []
    current = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


def _wifi_device():
    """Return the first NetworkManager Wi-Fi interface, if available."""
    if not shutil.which("nmcli"):
        return None
    ok, stdout, _ = run_command(
        ["nmcli", "-t", "-e", "yes", "-f", "DEVICE,TYPE", "device", "status"],
        timeout=4,
    )
    if not ok:
        return None
    for line in stdout.splitlines():
        parts = _nmcli_parse_terse_line(line)
        if len(parts) >= 2 and parts[1] == "wifi" and parts[0]:
            return parts[0]
    return None


def _wifi_networks(rescan=False):
    if not shutil.which("nmcli"):
        raise RuntimeError("NetworkManager/nmcli is not installed")

    device = _wifi_device()
    if not device:
        return {"device": None, "networks": [], "wifi_enabled": None}

    if rescan:
        # NetworkManager handles the scan asynchronously. Give it a moment so
        # the list returned below reflects the fresh scan where possible.
        run_command(["nmcli", "device", "wifi", "rescan", "ifname", device], timeout=12)
        time.sleep(0.7)

    ok, stdout, err = run_command(
        [
            "nmcli", "-t", "-e", "yes",
            "-f", "IN-USE,SSID,SIGNAL,SECURITY,BSSID,CHAN",
            "device", "wifi", "list", "ifname", device,
        ],
        timeout=8,
    )
    if not ok:
        raise RuntimeError(err or "Could not scan Wi-Fi networks")

    networks = []
    seen = set()
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = _nmcli_parse_terse_line(line)
        if len(parts) < 6:
            continue
        in_use, ssid, signal, security, bssid, channel = parts[:6]
        # Hidden APs have no useful SSID to connect to from the normal picker.
        if not ssid:
            continue
        key = (ssid, bssid)
        if key in seen:
            continue
        seen.add(key)
        try:
            signal_value = int(signal)
        except (TypeError, ValueError):
            signal_value = 0
        networks.append({
            "ssid": ssid,
            "signal": signal_value,
            "security": security or "Open",
            "bssid": bssid,
            "channel": channel,
            "connected": in_use == "*",
        })

    # Collapse duplicate BSSIDs/entries for a cleaner phone UI. Keep the
    # strongest AP entry for each SSID and preserve connected state.
    by_ssid = {}
    for network in networks:
        current = by_ssid.get(network["ssid"])
        if current is None or network["signal"] > current["signal"] or network["connected"]:
            by_ssid[network["ssid"]] = network
        elif network["connected"]:
            current["connected"] = True

    result = sorted(
        by_ssid.values(),
        key=lambda item: (not item["connected"], -item["signal"], item["ssid"].lower()),
    )
    return {"device": device, "networks": result, "wifi_enabled": True}


@app.route("/api/wifi/networks", methods=["GET"])
def wifi_networks():
    if not shutil.which("nmcli"):
        return jsonify({"ok": False, "error": "NetworkManager/nmcli is not installed"}), 500
    try:
        rescan = request.args.get("scan", "0") in {"1", "true", "yes"}
        data = _wifi_networks(rescan=rescan)
        ok, radio, _ = run_command(["nmcli", "radio", "wifi"], timeout=4)
        data["wifi_enabled"] = radio.strip().lower() == "enabled" if ok else None
        return jsonify({"ok": True, **data})
    except (RuntimeError, OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/wifi/connect", methods=["POST"])
def wifi_connect():
    data = request.get_json(silent=True) or {}
    ssid = str(data.get("ssid", "")).strip()
    password = data.get("password")
    if not ssid:
        return jsonify({"ok": False, "error": "Wi-Fi network name is required"}), 400
    if password is not None:
        password = str(password)

    if not shutil.which("nmcli"):
        return jsonify({"ok": False, "error": "NetworkManager/nmcli is not installed"}), 500

    device = str(data.get("device") or _wifi_device() or "")
    command = ["nmcli", "device", "wifi", "connect", ssid]
    if device:
        command += ["ifname", device]
    if password:
        command += ["password", password]

    ok, stdout, err = run_command(command, timeout=30)
    if not ok:
        # Avoid echoing a password or other command arguments back to the UI.
        message = err or stdout or "Wi-Fi connection failed"
        return jsonify({"ok": False, "error": message}), 400

    return jsonify({"ok": True, "message": stdout or f"Connected to {ssid}", "ssid": ssid})


@app.route("/api/wifi/disconnect", methods=["POST"])
def wifi_disconnect():
    data = request.get_json(silent=True) or {}
    device = str(data.get("device") or _wifi_device() or "").strip()
    if not device:
        return jsonify({"ok": False, "error": "No Wi-Fi device found"}), 400
    if not shutil.which("nmcli"):
        return jsonify({"ok": False, "error": "NetworkManager/nmcli is not installed"}), 500

    ok, stdout, err = run_command(["nmcli", "device", "disconnect", device], timeout=15)
    if not ok:
        return jsonify({"ok": False, "error": err or stdout or "Wi-Fi disconnect failed"}), 400
    return jsonify({"ok": True, "message": stdout or f"Disconnected {device}", "device": device})


@app.route("/api/wifi", methods=["POST"])
def wifi():
    data = request.get_json(silent=True) or {}
    action = data.get("action")

    if action not in ("on", "off"):
        return jsonify({"ok": False, "error": "Invalid Wi-Fi action"}), 400

    if not shutil.which("nmcli"):
        return jsonify({"ok": False, "error": "NetworkManager/nmcli is not installed"}), 500

    ok, _, err = run_command(["nmcli", "radio", "wifi", action])

    if not ok:
        return jsonify({"ok": False, "error": err or "Wi-Fi command failed"}), 500

    return jsonify({"ok": True, "message": f"Wi-Fi {action}"})


@app.route("/api/power", methods=["POST"])
def power():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    user = os.environ.get("DASHBOARD_USER") or getpass.getuser()

    if action == "lock":
        # Serpantinum provides its own Wayland/Quickshell lockscreen.
        # Use that instead of loginctl: this avoids the unsupported
        # `loginctl lock-user` verb on the user's current system.
        lock_script = Path.home() / ".local/share/serpantinum/src/scripts/lock.sh"
        if lock_script.exists() and os.access(lock_script, os.X_OK):
            ok, out, err = run_command([str(lock_script)], timeout=15, env=graphical_environment())
            if ok:
                return jsonify({"ok": True, "message": "Serpantinum lock requested"})
            return jsonify({
                "ok": False,
                "error": err or out or "Serpantinum lock script failed",
            }), 500

        # Generic fallback for systems where the Serpantinum script is absent.
        ok, _, err = run_command(["loginctl", "lock-session"], timeout=8)
        if ok:
            return jsonify({"ok": True, "message": "Lock requested"})
        return jsonify({"ok": False, "error": err or "No supported lock method found"}), 500

    commands = {
        "suspend": ["systemctl", "suspend"],
        "reboot": ["systemctl", "reboot"],
        "shutdown": ["systemctl", "poweroff"],
    }

    command = commands.get(action)
    if not command:
        return jsonify({"ok": False, "error": "Unknown power action"}), 400

    ok, _, err = run_command(command, timeout=10, env=graphical_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or "Power action failed"}), 500

    return jsonify({"ok": True})



# ---------------------------------------------------------------------------
# File Explorer
# ---------------------------------------------------------------------------
FILE_ROOT = Path.home().resolve()

# The file explorer is a single-user dashboard, so keep its clipboard on the
# server instead of inside the Flask session cookie. This survives page reloads
# and avoids the intermittent "Clipboard is empty" problem.
FILE_CLIPBOARD = None
FILE_CLIPBOARD_LOCK = threading.RLock()


def _clipboard_snapshot():
    with FILE_CLIPBOARD_LOCK:
        return dict(FILE_CLIPBOARD) if FILE_CLIPBOARD else None


def _clipboard_paths(clip):
    if not clip:
        return []
    paths = clip.get("paths")
    if isinstance(paths, list):
        return [str(x) for x in paths if x]
    # Compatibility with the old single-item clipboard shape.
    return [str(clip.get("path"))] if clip.get("path") else []


def _path_is_inside(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_file_path(raw_path=""):
    """Resolve a dashboard file path while keeping it inside the user's home."""
    raw = str(raw_path or ".").strip()
    if not raw:
        raw = "."
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = FILE_ROOT / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(FILE_ROOT)
    except (OSError, ValueError):
        raise ValueError("Path is outside the dashboard file root")
    return resolved


def _file_rel(path):
    try:
        rel = path.relative_to(FILE_ROOT)
    except ValueError:
        return ""
    return "." if str(rel) == "." else str(rel)


def _file_entry(path):
    stat = path.stat()
    is_dir = path.is_dir()
    return {
        "name": path.name or FILE_ROOT.name,
        "path": _file_rel(path),
        "type": "directory" if is_dir else "file",
        "size": None if is_dir else stat.st_size,
        "modified": stat.st_mtime,
        "hidden": path.name.startswith("."),
    }


@app.route("/api/files", methods=["GET"])
def files_list():
    try:
        directory = _safe_file_path(request.args.get("path", "."))
        if not directory.is_dir():
            return jsonify({"ok": False, "error": "Not a directory"}), 400
        entries = []
        for item in directory.iterdir():
            try:
                # Skip symlinks that resolve outside the allowed tree.
                _safe_file_path(item.relative_to(FILE_ROOT))
                entries.append(_file_entry(item))
            except (OSError, ValueError):
                continue
        entries.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))
        parent = None if directory == FILE_ROOT else _file_rel(directory.parent)
        with FILE_CLIPBOARD_LOCK:
            clipboard = dict(FILE_CLIPBOARD) if FILE_CLIPBOARD else None
        return jsonify({"ok": True, "root": str(FILE_ROOT), "path": _file_rel(directory),
                        "parent": parent, "entries": entries, "clipboard": clipboard})
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/files/download", methods=["GET"])
def files_download():
    try:
        path = _safe_file_path(request.args.get("path", ""))
        if not path.is_file():
            return jsonify({"ok": False, "error": "File not found"}), 404
        return send_file(path, as_attachment=True, download_name=path.name)
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/files/upload", methods=["POST"])
def files_upload():
    try:
        directory = _safe_file_path(request.form.get("path", "."))
        if not directory.is_dir():
            return jsonify({"ok": False, "error": "Destination is not a directory"}), 400
        uploaded = request.files.getlist("files")
        if not uploaded:
            return jsonify({"ok": False, "error": "No files uploaded"}), 400
        results = []
        for item in uploaded:
            name = Path(item.filename or "").name
            if not name or name in {".", ".."}:
                continue
            target = _safe_file_path(_file_rel(directory / name))
            if target.exists():
                return jsonify({"ok": False, "error": f"Already exists: {name}"}), 409
            item.save(target)
            results.append(_file_rel(target))
        return jsonify({"ok": True, "files": results})
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/files/folder", methods=["POST"])
def files_folder():
    try:
        parent = _safe_file_path(request.json.get("path", "."))
        name = Path(str(request.json.get("name", "")).strip()).name
        if not name or name in {".", ".."}:
            return jsonify({"ok": False, "error": "Invalid folder name"}), 400
        target = _safe_file_path(_file_rel(parent / name))
        target.mkdir()
        return jsonify({"ok": True, "path": _file_rel(target)})
    except FileExistsError:
        return jsonify({"ok": False, "error": "Folder already exists"}), 409
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/files/rename", methods=["POST"])
def files_rename():
    try:
        source = _safe_file_path(request.json.get("path", ""))
        name = Path(str(request.json.get("name", "")).strip()).name
        if not source.exists() or not name or name in {".", ".."}:
            return jsonify({"ok": False, "error": "Invalid source or name"}), 400
        target = _safe_file_path(_file_rel(source.parent / name))
        if target.exists():
            return jsonify({"ok": False, "error": "A file or folder with that name already exists"}), 409
        source.rename(target)
        with FILE_CLIPBOARD_LOCK:
            clip = FILE_CLIPBOARD
            if clip:
                updated = []
                for raw in _clipboard_paths(clip):
                    try:
                        cp = _safe_file_path(raw)
                    except ValueError:
                        continue
                    if cp == source:
                        updated.append(_file_rel(target))
                    else:
                        updated.append(_file_rel(cp))
                if updated:
                    FILE_CLIPBOARD = {"paths": updated, "mode": clip.get("mode"), "count": len(updated)}
                else:
                    FILE_CLIPBOARD = None
        return jsonify({"ok": True, "path": _file_rel(target), "clipboard": _clipboard_snapshot()})
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/files/delete", methods=["POST"])
def files_delete():
    global FILE_CLIPBOARD
    try:
        payload = request.get_json(silent=True) or {}
        raw_paths = payload.get("paths")
        if raw_paths is None:
            raw_paths = [payload.get("path", "")]
        if not isinstance(raw_paths, list) or not raw_paths:
            return jsonify({"ok": False, "error": "No files selected"}), 400

        paths = []
        for raw in raw_paths:
            path = _safe_file_path(raw)
            if path == FILE_ROOT:
                return jsonify({"ok": False, "error": "The home directory cannot be deleted"}), 400
            if not path.exists():
                return jsonify({"ok": False, "error": "File or folder not found"}), 404
            paths.append(path)

        # Delete deepest paths first so a selected child does not cause a
        # second deletion after its selected parent has already been removed.
        unique_paths = sorted(set(paths), key=lambda x: len(x.parts), reverse=True)
        for path in unique_paths:
            if not path.exists():
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

        # If a cut item was deleted, remove it from the clipboard immediately.
        with FILE_CLIPBOARD_LOCK:
            clip = FILE_CLIPBOARD
            if clip:
                remaining = []
                for raw in _clipboard_paths(clip):
                    try:
                        cp = _safe_file_path(raw)
                    except ValueError:
                        continue
                    if any(_path_is_inside(cp, deleted) for deleted in unique_paths):
                        continue
                    remaining.append(_file_rel(cp))
                if remaining:
                    FILE_CLIPBOARD = {"paths": remaining, "mode": clip.get("mode"), "count": len(remaining)}
                else:
                    FILE_CLIPBOARD = None
            clipboard = dict(FILE_CLIPBOARD) if FILE_CLIPBOARD else None
        return jsonify({"ok": True, "clipboard": clipboard})
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/files/clipboard", methods=["POST"])
def files_clipboard():
    global FILE_CLIPBOARD
    try:
        payload = request.get_json(silent=True) or {}
        raw_paths = payload.get("paths")
        if raw_paths is None:
            raw_paths = [payload.get("path", "")]
        if not isinstance(raw_paths, list) or not raw_paths:
            return jsonify({"ok": False, "error": "No files selected"}), 400
        mode = payload.get("mode")
        if mode not in {"copy", "cut"}:
            return jsonify({"ok": False, "error": "Clipboard mode must be copy or cut"}), 400

        paths = []
        for raw in raw_paths:
            path = _safe_file_path(raw)
            if not path.exists():
                return jsonify({"ok": False, "error": f"File or folder not found: {_file_rel(path)}"}), 404
            rel = _file_rel(path)
            if rel not in paths:
                paths.append(rel)

        with FILE_CLIPBOARD_LOCK:
            FILE_CLIPBOARD = {"paths": paths, "mode": mode, "count": len(paths)}
            clipboard = dict(FILE_CLIPBOARD)
        return jsonify({"ok": True, "clipboard": clipboard, **clipboard})
    except (OSError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


def _unique_file_target(directory, name):
    """Return a collision-free path such as file (1).ext."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    source_name = Path(name)
    stem = source_name.stem
    suffix = source_name.suffix
    number = 1
    while True:
        candidate = directory / f"{stem} ({number}){suffix}"
        if not candidate.exists():
            return candidate
        number += 1


@app.route("/api/files/paste", methods=["POST"])
def files_paste():
    global FILE_CLIPBOARD
    try:
        payload = request.get_json(silent=True) or {}
        destination = _safe_file_path(payload.get("path", "."))
        conflict_action = str(payload.get("conflict", "ask")).lower()
        if not destination.is_dir():
            return jsonify({"ok": False, "error": "Destination is not a directory"}), 400
        if conflict_action not in {"ask", "replace", "rename"}:
            return jsonify({"ok": False, "error": "Invalid conflict action"}), 400

        with FILE_CLIPBOARD_LOCK:
            clip = dict(FILE_CLIPBOARD) if FILE_CLIPBOARD else None
        if not clip:
            return jsonify({"ok": False, "error": "Clipboard is empty"}), 400

        raw_sources = _clipboard_paths(clip)
        sources = []
        missing = []
        for raw in raw_sources:
            source = _safe_file_path(raw)
            if not source.exists():
                missing.append(raw)
            else:
                sources.append(source)
        if missing:
            with FILE_CLIPBOARD_LOCK:
                FILE_CLIPBOARD = None
            return jsonify({"ok": False, "error": "Clipboard source no longer exists", "missing": missing}), 404
        if not sources:
            with FILE_CLIPBOARD_LOCK:
                FILE_CLIPBOARD = None
            return jsonify({"ok": False, "error": "Clipboard is empty"}), 400

        plans = []
        conflicts = []
        for source in sources:
            target = _safe_file_path(_file_rel(destination / source.name))
            if target == source:
                return jsonify({"ok": False, "error": f"The source and destination are the same: {source.name}"}), 400
            conflict = target.exists()
            if conflict:
                conflicts.append({"name": source.name, "path": _file_rel(target), "mode": clip.get("mode")})
            plans.append({"source": source, "target": target})

        # Ask before touching anything. This makes multi-file paste safe: if
        # three files collide, none of the other files are pasted prematurely.
        if conflicts and conflict_action == "ask":
            return jsonify({
                "ok": False,
                "conflict": True,
                "conflicts": conflicts,
                "count": len(conflicts),
                "mode": clip.get("mode"),
                "message": "One or more selected items already exist here."
            }), 409

        results = []
        mode = clip.get("mode")
        for plan in plans:
            source, target = plan["source"], plan["target"]
            if target.exists() and conflict_action == "replace":
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            elif target.exists() and conflict_action == "rename":
                target = _unique_file_target(destination, source.name)

            if mode == "copy":
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    shutil.copy2(source, target)
            elif mode == "cut":
                shutil.move(str(source), str(target))
            else:
                return jsonify({"ok": False, "error": "Invalid clipboard mode"}), 400
            results.append(_file_rel(target))

        # Cut is consumed after the complete batch succeeds. Copy remains so
        # the same selection can be pasted repeatedly.
        if mode == "cut":
            with FILE_CLIPBOARD_LOCK:
                FILE_CLIPBOARD = None

        with FILE_CLIPBOARD_LOCK:
            clipboard = dict(FILE_CLIPBOARD) if FILE_CLIPBOARD else None
        return jsonify({"ok": True, "paths": results, "count": len(results), "mode": mode, "clipboard": clipboard})
    except (OSError, ValueError, shutil.Error) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# ---------------------------------------------------------------------------
# Package manager / application discovery
# ---------------------------------------------------------------------------
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9@._+:-]+$")

def package_command_prefix():
    """Use yay when available so both repo and AUR packages are handled."""
    yay = shutil.which("yay")
    if yay:
        return [yay]
    pacman = shutil.which("pacman")
    return [pacman] if pacman else []

def run_package_manager(args, timeout=60):
    """Run yay as the dashboard user when the dashboard itself is root."""
    command = package_command_prefix()
    if not command:
        return False, "", "No package manager found"
    full = command + args
    if Path(command[0]).name == "yay" and os.geteuid() == 0:
        username = os.environ.get("DASHBOARD_USER") or getpass.getuser()
        full = ["runuser", "-u", username, "--"] + full
    return run_command(full, timeout=timeout, env=graphical_environment())

def parse_pacman_info(text):
    packages = []
    current = {}
    for line in text.splitlines():
        if not line.strip():
            if current.get("name"):
                packages.append(current)
            current = {}
            continue
        if " : " not in line:
            continue
        key, value = line.split(" : ", 1)
        key = key.strip().lower().replace(" ", "_")
        value = value.strip()
        current[key] = value
    if current.get("name"):
        packages.append(current)
    return packages

def installed_packages():
    if not shutil.which("pacman"):
        return [], "pacman is not installed"
    ok, out, err = run_command(["pacman", "-Qi"], timeout=25)
    if not ok:
        return [], err or out or "pacman -Qi failed"
    foreign = set()
    okf, outf, _ = run_command(["pacman", "-Qm"], timeout=15)
    if okf:
        for line in outf.splitlines():
            parts = line.split()
            if parts:
                foreign.add(parts[0])
    rows = []
    for item in parse_pacman_info(out):
        name = item.get("name", "")
        if not name:
            continue
        repo = item.get("repository", "")
        if name in foreign:
            source = "AUR / foreign"
        elif repo and repo.lower() not in {"unknown", "none"}:
            source = repo
        else:
            source = "local / foreign"
        rows.append({
            "name": name,
            "version": item.get("version", ""),
            "repo": repo,
            "source": source,
            "description": item.get("description", ""),
            "size": item.get("installed_size", ""),
        })
    rows.sort(key=lambda x: x["name"].lower())
    return rows, ""

def available_package_updates():
    updates = {}
    command = package_command_prefix()
    if not command:
        return updates, "No package manager found"
    # yay -Qu covers repository + AUR updates. pacman -Qu covers repo updates.
    ok, out, err = run_package_manager(["-Qu"], timeout=60)
    if not ok:
        return updates, err or out or "Could not query package updates"
    for line in out.splitlines():
        m = re.match(r"^([^ ]+)\s+([^ ]+)\s+->\s+(.+)$", line.strip())
        if m:
            updates[m.group(1)] = {"old": m.group(2), "new": m.group(3).strip()}
        else:
            parts = line.split()
            if len(parts) >= 4 and parts[2] == "->":
                updates[parts[0]] = {"old": parts[1], "new": parts[3]}
    return updates, ""

@app.route("/api/packages", methods=["GET"])
def packages():
    try:
        rows, error = installed_packages()
        updates, update_error = available_package_updates()
        for row in rows:
            change = updates.get(row["name"])
            if change:
                row["status"] = "update"
                row["available_version"] = change["new"]
            else:
                row["status"] = "uptodate"
                row["available_version"] = row["version"]
        return jsonify({
            "ok": True,
            "packages": rows,
            "count": len(rows),
            "updates": len(updates),
            "error": error or update_error or None,
            "manager": "yay" if shutil.which("yay") else "pacman",
        })
    except Exception as exc:
        app.logger.exception("Package list refresh failed")
        return jsonify({"ok": False, "error": f"Package refresh failed: {str(exc)[:400]}"}), 500

def _open_local_auth_terminal(kind, action, name="", pid=None):
    """Open a visible terminal on the host and run one narrowly-scoped privileged action."""
    env = graphical_environment()
    username = os.environ.get("DASHBOARD_USER") or getpass.getuser()
    try:
        account = pwd.getpwnam(username)
    except KeyError:
        return False, "Dashboard user account was not found"

    restart_start_cmd = None
    if kind == "dashboard" and action == "restart":
        import shlex
        home = Path(account.pw_dir)
        stop_candidates = [shutil.which("dashboard-stop"), str(home / ".local/bin/dashboard-stop"), "/usr/local/bin/dashboard-stop"]
        start_candidates = [shutil.which("dashboard-start"), str(home / ".local/bin/dashboard-start"), "/usr/local/bin/dashboard-start"]
        stop_cmd = next((p for p in stop_candidates if p and Path(p).is_file()), None)
        start_cmd = next((p for p in start_candidates if p and Path(p).is_file()), None)
        if not stop_cmd or not start_cmd:
            return False, "Could not find both dashboard-stop and dashboard-start on the PC."
        # dashboard-stop can terminate the shell/process tree that launched it.
        # Start the starter in a separate terminal process first, with a short
        # delay, so it still runs after the stop script finishes.
        restart_start_cmd = start_cmd
        command = [stop_cmd]
        title = "Stop System Dashboard (restart queued)"
    elif kind == "ollama" and action in {"start", "stop"}:
        command = ["sudo", "systemctl", action, "ollama.service"]
        title = f"Ollama service: {action}"
    elif kind == "package" and action in {"update", "reinstall", "uninstall"} and PACKAGE_NAME_RE.fullmatch(name or ""):
        manager = shutil.which("pacman")
        if not manager:
            return False, "pacman is not installed"
        if action == "uninstall":
            command = ["sudo", manager, "-Rns", name]
        else:
            command = ["sudo", manager, "-S", name]
        title = f"Package {action}: {name}"
    elif kind == "process" and action == "terminate":
        try:
            pid = int(pid)
            proc = psutil.Process(pid)
            # Confirm it still exists before presenting the privileged command.
            proc.name()
        except (TypeError, ValueError, psutil.NoSuchProcess):
            return False, "That process no longer exists"
        if pid <= 1 or pid == os.getpid():
            return False, "Refusing to terminate a protected process"
        command = ["sudo", "kill", "-TERM", str(pid)]
        title = f"Terminate process {pid}"
    else:
        return False, "Unsupported authorization action"

    # Keep the command fixed and argument-based. Only the selected terminal's
    # shell wrapper is used to keep the window open after sudo completes.
    import shlex
    script = "printf '\\nAuthorization is required for this dashboard action.\\n'; " + shlex.join(command) + "; rc=$?; printf '\\nCommand finished with exit code %s. You may close this window.\\n' \"$rc\"; read -r -p 'Press Enter to close...' _"
    terminals = ["kitty", "foot", "alacritty", "wezterm", "konsole", "xterm"]
    terminal = next((shutil.which(x) for x in terminals if shutil.which(x)), None)
    if not terminal:
        return False, "No supported terminal emulator found (kitty, foot, alacritty, wezterm, konsole, xterm)"
    # For a restart, open a second visible terminal for dashboard-start before
    # invoking dashboard-stop. The separate terminal survives aggressive stop
    # scripts and can still handle any local sudo prompt.
    if restart_start_cmd:
        delayed_script = f"sleep 3; {shlex.quote(restart_start_cmd)}; rc=$?; printf '\nDashboard start exit code: %s\n' \"$rc\"; read -r -p 'Press Enter to close...' _"
        start_argv = [terminal]
        if Path(terminal).name == "kitty":
            start_argv += ["--title", "Start System Dashboard", "sh", "-lc", delayed_script]
        elif Path(terminal).name == "foot":
            start_argv += ["--title", "Start System Dashboard", "sh", "-lc", delayed_script]
        elif Path(terminal).name == "alacritty":
            start_argv += ["--title", "Start System Dashboard", "-e", "sh", "-lc", delayed_script]
        elif Path(terminal).name == "wezterm":
            start_argv += ["start", "--always-new-process", "--", "sh", "-lc", delayed_script]
        elif Path(terminal).name == "konsole":
            start_argv += ["--new-tab", "-p", "tabtitle=Start System Dashboard", "-e", "sh", "-lc", delayed_script]
        else:
            start_argv += ["-T", "Start System Dashboard", "-e", "sh", "-lc", delayed_script]
        try:
            if os.geteuid() == 0 and account.pw_uid != 0:
                runuser = shutil.which("runuser") or "/usr/bin/runuser"
                start_argv = [runuser, "-u", username, "--", "/usr/bin/env"] + [f"{k}={v}" for k, v in env.items() if k in {"HOME", "USER", "LOGNAME", "PATH", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY", "HYPRLAND_INSTANCE_SIGNATURE"}] + start_argv
                subprocess.Popen(start_argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
            else:
                subprocess.Popen(start_argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        except OSError as exc:
            return False, f"Could not open the dashboard-start terminal: {exc}"

    argv = [terminal]
    if Path(terminal).name == "kitty":
        argv += ["--title", title, "sh", "-lc", script]
    elif Path(terminal).name == "foot":
        argv += ["--title", title, "sh", "-lc", script]
    elif Path(terminal).name == "alacritty":
        argv += ["--title", title, "-e", "sh", "-lc", script]
    elif Path(terminal).name == "wezterm":
        argv += ["start", "--always-new-process", "--", "sh", "-lc", script]
    elif Path(terminal).name == "konsole":
        argv += ["--new-tab", "-p", f"tabtitle={title}", "-e", "sh", "-lc", script]
    else:
        argv += ["-T", title, "-e", "sh", "-lc", script]

    try:
        # The dashboard may be a root-owned system service. Drop to the desktop
        # user's account so the terminal appears in that user's graphical session.
        if os.geteuid() == 0 and account.pw_uid != 0:
            runuser = shutil.which("runuser") or "/usr/bin/runuser"
            argv = [runuser, "-u", username, "--"] + ["/usr/bin/env"] + [f"{k}={v}" for k, v in env.items() if k in {"HOME", "USER", "LOGNAME", "PATH", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY", "HYPRLAND_INSTANCE_SIGNATURE"}] + argv
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        else:
            subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        app.logger.info("Opened local authorization terminal: kind=%s action=%s", kind, action)
        return True, "A terminal has been opened on the PC. Complete the authorization there."
    except OSError as exc:
        app.logger.warning("Could not open authorization terminal: %s", str(exc)[:250])
        return False, f"Could not open a terminal: {exc}"


@app.route("/api/auth/terminal", methods=["POST"])
def open_auth_terminal():
    data = request.get_json(silent=True) or {}
    if str(data.get("kind", "")) == "dashboard":
        denied = _require_admin()
        if denied: return denied
    ok, message = _open_local_auth_terminal(
        str(data.get("kind", "")), str(data.get("action", "")),
        str(data.get("name", "")), data.get("pid")
    )
    return jsonify({"ok": ok, "message": message, "error": None if ok else message}), (200 if ok else 400)


@app.route("/api/package/action", methods=["POST"])
def package_action():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    action = str(data.get("action", "")).strip()
    if not name or not PACKAGE_NAME_RE.fullmatch(name) or action not in {"update", "reinstall", "uninstall"}:
        return jsonify({"ok": False, "error": "Invalid package action"}), 400

    # Only operate on packages that are actually installed, preventing this
    # endpoint from becoming a general arbitrary-command launcher.
    installed, error = installed_packages()
    known = {x["name"] for x in installed}
    if name not in known:
        return jsonify({"ok": False, "error": "Package is not installed"}), 404

    manager = shutil.which("yay") or shutil.which("pacman")
    if not manager:
        return jsonify({"ok": False, "error": "Neither yay nor pacman is installed"}), 500
    manager_name = Path(manager).name

    if action == "uninstall":
        args = [manager, "-Rns", "--noconfirm", name]
    else:
        args = [manager, "-S", "--noconfirm", name]

    # yay refuses to run as root. If the dashboard is a system service, execute
    # yay as the dashboard user while retaining its normal home/cache/config.
    target_user = os.environ.get("DASHBOARD_USER") or getpass.getuser()
    if manager_name == "yay" and os.geteuid() == 0:
        args = ["runuser", "-u", target_user, "--"] + args

    # The dashboard has no safe interactive TTY for sudo. Never collect or
    # store the user's sudo password in the web page; validate an existing sudo
    # authorization ticket and ask the user to authorize locally if needed.
    if manager_name == "pacman":
        sudo = shutil.which("sudo")
        if not sudo:
            return jsonify({"ok": False, "error": "sudo is required for pacman actions. Install/configure sudo, then retry."}), 403
        args = [sudo, "-n"] + args
    ok, out, err = run_command(args, timeout=900, env=graphical_environment())
    if not ok:
        detail = (err or out or f"Package {action} failed").strip()
        if any(marker in detail.lower() for marker in ("password is required", "a password is required", "no tty present", "terminal is required to read the password", "not allowed to execute", "sudo: a password", "authentication is required")):
            app.logger.info("Package action needs local sudo authorization: action=%s package=%s", action, name)
            return jsonify({"ok": False, "needs_sudo": True, "error": "Linux requires sudo authorization. Open a terminal on the PC, run `sudo -v`, enter your password there, then press Update again. The dashboard intentionally never collects your sudo password."}), 403
        app.logger.warning("Package action failed: action=%s package=%s error=%s", action, name, detail[:500])
        return jsonify({"ok": False, "error": detail}), 500
    app.logger.info("Package action completed: action=%s package=%s", action, name)
    return jsonify({"ok": True, "action": action, "package": name, "output": out[-4000:]})


def discover_desktop_apps():
    """Return launchable .desktop entries visible to the dashboard user."""
    roots = [Path("/usr/share/applications"), Path("/usr/local/share/applications")]
    try:
        username = os.environ.get("DASHBOARD_USER") or getpass.getuser()
        user_home = Path(pwd.getpwnam(username).pw_dir)
    except Exception:
        user_home = Path(os.environ.get("HOME", str(Path.home())))
    roots.append(user_home / ".local/share/applications")
    seen = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.desktop")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"^NoDisplay=true\s*$", text, re.M | re.I) or re.search(r"^Hidden=true\s*$", text, re.M | re.I):
                continue
            name_m = re.search(r"^Name(?:\[[^]]+\])?=(.+)$", text, re.M)
            exec_m = re.search(r"^Exec=(.+)$", text, re.M)
            if not name_m or not exec_m:
                continue
            name = name_m.group(1).strip()
            exec_line = exec_m.group(1).strip()
            icon_m = re.search(r"^Icon=(.+)$", text, re.M)
            icon_value = icon_m.group(1).strip() if icon_m else ""
            # Remove desktop field codes and shell metacharacters. Launch via
            # gtk-launch using the desktop ID instead of executing Exec directly.
            if not name:
                continue
            desktop_id = path.name
            key = desktop_id.lower()
            if key in seen:
                continue
            seen[key] = {
                "id": desktop_id,
                "name": name,
                "desktop": str(path),
                "exec": exec_line,
                "icon": icon_value,
                "terminal": bool(re.search(r"^Terminal=true\\s*$", text, re.M | re.I)),
            }
    # Flatpak applications may not have desktop files in the usual roots, so
    # merge their exported app list explicitly. Missing Flatpak is harmless.
    flatpak = shutil.which("flatpak")
    if flatpak:
        try:
            proc = subprocess.run([flatpak, "list", "--app", "--columns=application,name"], capture_output=True, text=True, timeout=8, check=False, env=graphical_environment())
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    parts = line.split("\t", 1)
                    if len(parts) != 2: parts = line.split(None, 1)
                    if len(parts) != 2: continue
                    app_id, app_name = parts[0].strip(), parts[1].strip()
                    if not app_id or not app_name: continue
                    desktop_id = "flatpak:" + app_id
                    seen.setdefault(desktop_id.lower(), {"id": desktop_id, "name": app_name, "desktop": "", "exec": "flatpak run " + app_id, "icon": "", "terminal": False, "flatpak": True, "app_id": app_id})
        except (OSError, subprocess.TimeoutExpired):
            pass
    return sorted(seen.values(), key=lambda x: x["name"].lower())

@app.route("/api/apps", methods=["GET"])
def apps_list():
    return jsonify({"ok": True, "apps": discover_desktop_apps()})

def _resolve_desktop_icon(icon_value):
    if not icon_value:
        return None
    value = Path(icon_value).expanduser()
    if value.is_absolute() and value.is_file():
        return value
    roots = [Path("/usr/share/icons"), Path("/usr/share/pixmaps"), Path("/usr/local/share/pixmaps")]
    try:
        home = Path(pwd.getpwnam(DASHBOARD_USER).pw_dir)
        roots += [home / ".local/share/icons", home / ".icons"]
    except Exception:
        pass
    extensions = (".png", ".svg", ".xpm", ".jpg", ".jpeg", ".webp")
    for root in roots:
        if not root.exists():
            continue
        direct = root / icon_value
        if direct.is_file():
            return direct
        for ext in extensions:
            candidate = root / (icon_value if icon_value.endswith(ext) else icon_value + ext)
            if candidate.is_file():
                return candidate
        try:
            for candidate in root.glob(f"**/{icon_value}.*"):
                if candidate.suffix.lower() in extensions and candidate.is_file():
                    return candidate
        except OSError:
            continue
    return None

@app.route("/api/app-icon/<path:desktop_id>", methods=["GET"])
def app_icon(desktop_id):
    entry = next((x for x in discover_desktop_apps() if x["id"] == desktop_id), None)
    if not entry:
        return "", 404
    icon = _resolve_desktop_icon(entry.get("icon", ""))
    if not icon:
        return "", 404
    try:
        return send_file(icon, conditional=True, max_age=3600)
    except (OSError, ValueError):
        return "", 404

@app.route("/api/desktop-app", methods=["POST"])
def launch_desktop_app():
    data = request.get_json(silent=True) or {}
    desktop_id = str(data.get("id", "")).strip()
    apps = {x["id"]: x for x in discover_desktop_apps()}
    entry = apps.get(desktop_id)
    if not entry:
        return jsonify({"ok": False, "error": "Unknown desktop application"}), 404
    try:
        if entry.get("flatpak"):
            flatpak = shutil.which("flatpak")
            if not flatpak:
                return jsonify({"ok": False, "error": "Flatpak is not installed"}), 404
            command = [flatpak, "run", entry["app_id"]]
        else:
            gio = shutil.which("gio")
            if not gio:
                return jsonify({"ok": False, "error": "gio is not installed"}), 404
            command = [gio, "launch", entry["desktop"]]
        env = graphical_environment()
        if os.geteuid() == 0:
            username = os.environ.get("DASHBOARD_USER") or getpass.getuser()
            command = ["runuser", "-u", username, "--"] + command
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        return jsonify({"ok": True, "message": f"Started {entry['name']}"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/media/status", methods=["GET"])
def media_status():
    if not shutil.which("playerctl"):
        return jsonify({"ok": True, "available": False, "playing": False, "volume": None})
    env = graphical_environment()
    ok, status, _ = run_command(["playerctl", "status"], timeout=3, env=env)
    okm, meta, _ = run_command(["playerctl", "metadata", "--format", "{{artist}}|{{title}}|{{album}}"], timeout=3, env=env)
    okv, vol, _ = run_command(["playerctl", "volume"], timeout=3, env=env)
    volume = None
    try:
        volume = round(float(vol.strip()) * 100, 0) if okv else None
    except ValueError:
        pass
    artist = title = album = ""
    if okm:
        bits = meta.split("|", 2)
        artist = bits[0] if bits else ""
        title = bits[1] if len(bits) > 1 else ""
        album = bits[2] if len(bits) > 2 else ""
    return jsonify({"ok": True, "available": True, "playing": status.strip().lower() == "playing" if ok else False,
                    "status": status.strip() if ok else "No player", "artist": artist, "title": title, "album": album, "volume": volume})

APP_COMMANDS = {
    "brave": {
        "package": "brave-bin",
        "commands": ["brave-browser", "brave"],
    },
    "discord": {
        "package": "discord",
        "commands": ["discord"],
    },
    "kitty": {
        "package": "kitty",
        "commands": ["kitty"],
    },
    "vscode": {
        "package": "visual-studio-code-bin",
        "commands": ["code"],
    },
    "spotify": {
        "package": "spotify-launcher",
        "commands": ["spotify"],
    },
    "lutris": {
        "package": "lutris",
        "commands": ["lutris"],
    },
    "prismlauncher": {
        "package": "prismlauncher",
        "commands": ["prismlauncher"],
    },
    "yazi": {
        "package": "yazi",
        "commands": ["yazi"],
    },
    # Terminal utilities are launched inside Kitty so their TUI/ANSI output
    # is visible immediately instead of being swallowed by the Flask service.
    "cava": {"package": "cava", "commands": ["cava"]},
    "asciiquarium": {"package": "asciiquarium", "commands": ["asciiquarium"]},
    "sl": {"package": "sl", "commands": ["sl"]},
    "btop": {"package": "btop", "commands": ["btop"]},
    "bottom": {"package": "bottom", "commands": ["btm", "bottom"]},
    "glow": {"package": "glow", "commands": ["glow"]},
    "chafa": {"package": "chafa", "commands": ["chafa"]},
    "zenith": {"package": "zenith", "commands": ["zenith"]},
    "paclear": {"package": "paclear", "commands": ["paclear"]},
    "terminaltexteffects": {"package": "terminaltexteffects", "commands": ["tte", "terminaltexteffects"]},
}


def graphical_environment():
    """
    Supply the usual Wayland/DBus environment when the dashboard is run as
    a system service. Existing values are preserved.
    """
    env = os.environ.copy()
    username = os.environ.get("DASHBOARD_USER") or getpass.getuser()
    try:
        pw = pwd.getpwnam(username)
        uid = pw.pw_uid
        env.setdefault("HOME", pw.pw_dir)
        env.setdefault("USER", username)
        env.setdefault("LOGNAME", username)
    except KeyError:
        uid = os.getuid()

    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    # If the service does not inherit these, try the common Wayland socket.
    runtime_dir = Path(env["XDG_RUNTIME_DIR"])
    if "WAYLAND_DISPLAY" not in env:
        candidates = sorted(runtime_dir.glob("wayland-*"))
        if candidates:
            env["WAYLAND_DISPLAY"] = candidates[0].name

    if "DBUS_SESSION_BUS_ADDRESS" not in env:
        bus = runtime_dir / "bus"
        if bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"

    return env


def hyprctl_command(args, timeout=5):
    """Run hyprctl against the active Hyprland instance even when Flask is a system service."""
    # Use the real Hyprland CLI, not any user wrapper/alias that may be
    # installed in ~/.local/bin (Serpantinum can expose its own dispatch helper).
    exe = "/usr/bin/hyprctl" if Path("/usr/bin/hyprctl").exists() else shutil.which("hyprctl")
    if not exe:
        return False, "", "hyprctl is not installed"
    env = graphical_environment()
    try:
        # A systemd service usually does not inherit HYPRLAND_INSTANCE_SIGNATURE.
        # Discover the running instance first, then use its signature for the real command.
        if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
            probe = subprocess.run([exe, "instances", "-j"], capture_output=True, text=True, timeout=timeout, env=env)
            if probe.returncode != 0:
                return False, probe.stdout.strip(), probe.stderr.strip() or "Could not query Hyprland instances"
            instances = json.loads(probe.stdout or "[]")
            if not instances:
                return False, "", "No running Hyprland instance found"
            # Prefer the instance whose Wayland socket matches the environment.
            chosen = None
            wayland = env.get("WAYLAND_DISPLAY", "")
            for item in instances:
                if item.get("wl_socket") == wayland:
                    chosen = item
                    break
            chosen = chosen or instances[0]
            signature = chosen.get("instance") or chosen.get("signature")
            if signature:
                env["HYPRLAND_INSTANCE_SIGNATURE"] = signature
        result = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, env=env)
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as exc:
        return False, "", str(exc)


@app.route("/api/hypr/workspaces", methods=["GET"])
def hypr_workspaces():
    ok, out, err = hyprctl_command(["-j", "workspaces"])
    if not ok:
        return jsonify({"ok": False, "error": err or out or "Could not query Hyprland workspaces"}), 500
    try:
        workspaces = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    ok2, active_out, active_err = hyprctl_command(["-j", "activeworkspace"])
    active = None
    if ok2:
        try:
            active = json.loads(active_out or "{}")
        except json.JSONDecodeError:
            active = None
    return jsonify({"ok": True, "workspaces": workspaces, "active": active, "slots": list(range(1, 11))})


@app.route("/api/hypr/workspaces/switch", methods=["POST"])
def hypr_switch_workspace():
    data = request.get_json(silent=True) or {}
    try:
        workspace = int(data.get("workspace"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid workspace"}), 400
    if workspace < 1 or workspace > 10:
        return jsonify({"ok": False, "error": "Workspace must be between 1 and 10"}), 400
    # Hyprland 0.55+ uses Lua dispatchers. `hyprctl dispatch workspace 2`
    # is parsed as Lua and fails; invoke the actual dispatcher table instead.
    expression = f'hl.dispatch(hl.dsp.focus({{ workspace = "{workspace}" }}))'
    ok, out, err = hyprctl_command(["eval", expression])
    if not ok:
        return jsonify({"ok": False, "error": err or out or "Could not switch workspace"}), 502
    if out and re.search(r"\b(error|failed)\b", out, re.I):
        return jsonify({"ok": False, "error": out}), 502
    return jsonify({"ok": True, "workspace": workspace})


@app.route("/api/app", methods=["POST"])
def launch_app():
    data = request.get_json(silent=True) or {}
    app_name = data.get("app")
    entry = APP_COMMANDS.get(app_name)

    if not entry:
        return jsonify({"ok": False, "error": "Unknown application"}), 400

    executable = next(
        (candidate for candidate in entry["commands"] if shutil.which(candidate)),
        None,
    )

    if not executable:
        return jsonify({
            "ok": False,
            "error": f'{entry["package"]} executable not found in PATH',
        }), 404

    try:
        command = [executable]
        # Terminal applications need a real terminal. Launch them in Kitty.
        terminal_apps = {
            "yazi", "cava", "asciiquarium", "sl", "btop", "bottom", "glow",
            "chafa", "zenith", "paclear", "terminaltexteffects",
        }
        if app_name in terminal_apps:
            kitty = shutil.which("kitty")
            if not kitty:
                return jsonify({"ok": False, "error": "kitty executable not found in PATH"}), 404
            command = [kitty, "-e", executable]

        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=graphical_environment(),
        )
        return jsonify({
            "ok": True,
            "message": f'Started {entry["package"]}',
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500



@app.route("/api/scrcpy", methods=["POST"])
def scrcpy_launch():
    data = request.get_json(silent=True) or {}
    if not shutil.which("scrcpy"):
        return jsonify({"ok": False, "error": "scrcpy is not installed"}), 404
    args = ["scrcpy", "--select-usb"]
    flags = {
        "no_audio": "--no-audio",
        "turn_screen_off": "--turn-screen-off",
        "stay_awake": "--stay-awake",
        "fullscreen": "--fullscreen",
        "always_on_top": "--always-on-top",
        "show_touches": "--show-touches",
        "no_control": "--no-control",
        "no_video": "--no-video",
        "no_audio_playback": "--no-audio-playback",
        "keep_active": "--keep-active",
    }
    for key, flag in flags.items():
        if data.get(key):
            args.append(flag)

    max_size = str(data.get("max_size", "")).strip()
    if max_size:
        if not max_size.isdigit() or not 100 <= int(max_size) <= 10000:
            return jsonify({"ok": False, "error": "Invalid max-size"}), 400
        args.append(f"--max-size={max_size}")

    max_fps = str(data.get("max_fps", "")).strip()
    if max_fps:
        try:
            fps = float(max_fps)
            if not 1 <= fps <= 240:
                raise ValueError
        except ValueError:
            return jsonify({"ok": False, "error": "Invalid max-FPS"}), 400
        args.append(f"--max-fps={max_fps}")

    bit_rate = str(data.get("bit_rate", "")).strip()
    if bit_rate:
        if not re.fullmatch(r"[0-9]+[KMGkmg]?", bit_rate):
            return jsonify({"ok": False, "error": "Invalid video bit-rate"}), 400
        args.append(f"--video-bit-rate={bit_rate}")

    serial = str(data.get("serial", "")).strip()
    if serial:
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", serial):
            return jsonify({"ok": False, "error": "Invalid device serial"}), 400
        args.extend(["--serial", serial])

    # TCP/IP is opt-in. Default remains wired/USB via --select-usb.
    tcpip = str(data.get("tcpip", "")).strip()
    if tcpip:
        if not re.fullmatch(r"\+?[0-9A-Fa-f:.]+(?::[0-9]{1,5})?", tcpip):
            return jsonify({"ok": False, "error": "Invalid TCP/IP address"}), 400
        # Replace the USB selector when explicitly choosing TCP/IP.
        args = ["scrcpy", f"--tcpip={tcpip}"] + args[2:]

    kitty = shutil.which("kitty")
    if not kitty:
        return jsonify({"ok": False, "error": "kitty executable not found in PATH"}), 404

    try:
        subprocess.Popen([kitty, "-e", *args], env=graphical_environment(), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        return jsonify({"ok": True, "command": " ".join(args)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def ydotool_environment():
    """Find the dashboard user's ydotool socket even though Flask runs as a system service."""
    env = graphical_environment()
    username = os.environ.get("DASHBOARD_USER") or getpass.getuser()
    try:
        pw = pwd.getpwnam(username)
        uid = pw.pw_uid
    except KeyError:
        uid = 1000

    candidates = [
        Path("/tmp/system-dashboard-ydotool.sock"),
        Path(f"/run/user/{uid}/.ydotool_socket"),
        Path("/tmp/.ydotool_socket"),
    ]
    for socket_path in candidates:
        if socket_path.exists():
            env["YDOTOOL_SOCKET"] = str(socket_path)
            return env
    # Prefer the per-user runtime socket so the error points at the intended daemon.
    env["YDOTOOL_SOCKET"] = str(candidates[0])
    return env


@app.route("/api/mouse/move", methods=["POST"])
def mouse_move():
    data = request.get_json(silent=True) or {}
    try:
        dx = max(-500, min(500, int(data.get("dx", 0))))
        dy = max(-500, min(500, int(data.get("dy", 0))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid mouse movement"}), 400
    if not shutil.which("ydotool"):
        return jsonify({"ok": False, "error": "ydotool is not installed. Install it with: sudo pacman -S ydotool"}), 500
    ok, out, err = run_command(["ydotool", "mousemove", "-x", str(dx), "-y", str(dy)], timeout=3, env=ydotool_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or out or "ydotool mouse move failed. Is ydotoold running?"}), 500
    return jsonify({"ok": True})


@app.route("/api/mouse/button", methods=["POST"])
def mouse_button():
    data = request.get_json(silent=True) or {}
    button = str(data.get("button", "left"))
    state = str(data.get("state", "down"))
    buttons = {"left": 0, "right": 1, "middle": 2, "side": 3, "extra": 4}
    if button not in buttons or state not in {"down", "up"}:
        return jsonify({"ok": False, "error": "Invalid mouse button state"}), 400
    if not shutil.which("ydotool"):
        return jsonify({"ok": False, "error": "ydotool is not installed"}), 500
    mask = 0x40 if state == "down" else 0x80
    code = mask | buttons[button]
    ok, out, err = run_command(["ydotool", "click", hex(code)], timeout=3, env=ydotool_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or out or "ydotool mouse button failed"}), 500
    return jsonify({"ok": True})


@app.route("/api/mouse/scroll", methods=["POST"])
def mouse_scroll():
    data = request.get_json(silent=True) or {}
    try:
        x = max(-1000, min(1000, int(data.get("x", 0))))
        y = max(-1000, min(1000, int(data.get("y", 0))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid scroll amount"}), 400
    if not shutil.which("ydotool"):
        return jsonify({"ok": False, "error": "ydotool is not installed"}), 500
    # ydotool wheel uses signed click values for vertical/horizontal wheel movement.
    args = ["ydotool", "mousemove", "--wheel", "-x", str(x), "-y", str(y)]
    ok, out, err = run_command(args, timeout=3, env=ydotool_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or out or "ydotool scroll failed. Is ydotoold running?"}), 500
    return jsonify({"ok": True})


@app.route("/api/mouse/click", methods=["POST"])
def mouse_click():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    buttons = {"left": "0xC0", "right": "0xC1", "middle": "0xC2"}
    if action not in {"left", "right", "double_left", "double_right", "middle"}:
        return jsonify({"ok": False, "error": "Invalid mouse action"}), 400
    if not shutil.which("ydotool"):
        return jsonify({"ok": False, "error": "ydotool is not installed. Install it with: sudo pacman -S ydotool"}), 500
    base = "left" if "left" in action else ("right" if "right" in action else "middle")
    count = 2 if action.startswith("double_") else 1
    ok, out, err = run_command(["ydotool", "click", "-r", str(count), buttons[base]], timeout=3, env=ydotool_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or out or "ydotool click failed. Is ydotoold running?"}), 500
    return jsonify({"ok": True})


@app.route("/api/keyboard/type", methods=["POST"])
def keyboard_type():
    data = request.get_json(silent=True) or {}
    text_value = str(data.get("text", ""))
    if not text_value or len(text_value) > 256:
        return jsonify({"ok": False, "error": "Invalid text"}), 400
    if not shutil.which("wtype"):
        return jsonify({"ok": False, "error": "wtype is not installed. Install it with: sudo pacman -S wtype"}), 500
    ok, out, err = run_command(["wtype", "--", text_value], timeout=5, env=graphical_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or out or "wtype failed"}), 500
    return jsonify({"ok": True})


@app.route("/api/keyboard/event", methods=["POST"])
def keyboard_event():
    data = request.get_json(silent=True) or {}
    key = str(data.get("key", ""))
    state = str(data.get("state", "down"))
    keycodes = {
        "Escape":1,"grave":41,"1":2,"2":3,"3":4,"4":5,"5":6,"6":7,"7":8,"8":9,"9":10,"0":11,"minus":12,"equal":13,"BackSpace":14,
        "Tab":15,"q":16,"w":17,"e":18,"r":19,"t":20,"y":21,"u":22,"i":23,"o":24,"p":25,"bracketleft":26,"bracketright":27,"backslash":43,"Return":28,
        "Caps_Lock":58,"semicolon":39,"apostrophe":40,"a":30,"s":31,"d":32,"f":33,"g":34,"h":35,"j":36,"k":37,"l":38,
        "shift_left":42,"shift_right":54,"z":44,"x":45,"c":46,"v":47,"b":48,"n":49,"m":50,"comma":51,"period":52,"slash":53,
        "ctrl_left":29,"ctrl_right":97,"alt_left":56,"alt_right":100,"logo_left":125,"logo_right":126,"space":57,"Menu":127,
        "F1":59,"F2":60,"F3":61,"F4":62,"F5":63,"F6":64,"F7":65,"F8":66,"F9":67,"F10":68,"F11":87,"F12":88,
        "Print":99,"Scroll_Lock":70,"Pause":119,"Insert":110,"Delete":111,"Home":102,"End":107,"Page_Up":104,"Page_Down":109,
        "Up":103,"Left":105,"Down":108,"Right":106,"Num_Lock":69,"KP_Divide":98,"KP_Multiply":55,"KP_Subtract":74,"KP_Add":78,
        "KP_7":71,"KP_8":72,"KP_9":73,"KP_4":75,"KP_5":76,"KP_6":77,"KP_1":79,"KP_2":80,"KP_3":81,"KP_0":82,"KP_Decimal":83,"KP_Enter":96,
    }
    code = keycodes.get(key)
    if code is None or state not in {"down", "up"}:
        return jsonify({"ok": False, "error": "Invalid keyboard event"}), 400
    if not shutil.which("ydotool"):
        return jsonify({"ok": False, "error": "ydotool is not installed"}), 500
    ok, out, err = run_command(["ydotool", "key", f"{code}:{1 if state == 'down' else 0}"], timeout=3, env=ydotool_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or out or "ydotool keyboard event failed"}), 500
    return jsonify({"ok": True})


@app.route("/api/keyboard/key", methods=["POST"])
def keyboard_key():
    data = request.get_json(silent=True) or {}
    key = str(data.get("key", ""))
    allowed = {"Return","BackSpace","Tab","Escape","Delete","Home","End","Left","Right","Up","Down","Page_Up","Page_Down","Insert","Print","Pause","Menu","Caps_Lock","Num_Lock","Scroll_Lock","space","KP_0","KP_1","KP_2","KP_3","KP_4","KP_5","KP_6","KP_7","KP_8","KP_9","KP_Decimal","KP_Divide","KP_Multiply","KP_Subtract","KP_Add","KP_Enter"} | {f"F{i}" for i in range(1, 13)}
    if key not in allowed:
        return jsonify({"ok": False, "error": "Invalid key"}), 400
    if not shutil.which("wtype"):
        return jsonify({"ok": False, "error": "wtype is not installed. Install it with: sudo pacman -S wtype"}), 500
    ok, out, err = run_command(["wtype", "-k", key], timeout=5, env=graphical_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or out or "wtype failed"}), 500
    return jsonify({"ok": True})


@app.route("/api/keyboard/combo", methods=["POST"])
def keyboard_combo():
    data = request.get_json(silent=True) or {}
    combo = str(data.get("combo", "")).strip()
    allowed_mods = {"ctrl", "alt", "shift", "logo"}
    allowed_keys = {"a","b","c","d","e","f","g","h","i","j","k","l","m","n","o","p","q","r","s","t","u","v","w","x","y","z",
        "0","1","2","3","4","5","6","7","8","9","minus","equal","bracketleft","bracketright","backslash","semicolon","apostrophe","grave","comma","period","slash",
        "return","backspace","tab","escape","delete","home","end","left","right","up","down","page_up","page_down","insert","print","pause","menu","space"} | {f"f{i}" for i in range(1,13)}
    parts = [part.strip().lower() for part in combo.split("+") if part.strip()]
    if len(parts) < 2 or any(part not in allowed_mods and part not in allowed_keys for part in parts):
        # Allow standalone Escape as a convenience shortcut.
        if parts != ["escape"]:
            return jsonify({"ok": False, "error": "Invalid keyboard combination"}), 400
    if not shutil.which("wtype"):
        return jsonify({"ok": False, "error": "wtype is not installed"}), 500
    key_map = {"backspace":"BackSpace", "tab":"Tab", "escape":"Escape", "delete":"Delete", "home":"Home", "end":"End", "left":"Left", "right":"Right", "up":"Up", "down":"Down", "page_up":"Page_Up", "page_down":"Page_Down", "insert":"Insert", "print":"Print", "pause":"Pause", "menu":"Menu", "return":"Return", "space":"space"}
    key = key_map.get(parts[-1], parts[-1].upper() if parts[-1].startswith("f") else parts[-1])
    mods = parts[:-1]
    args = ["wtype"]
    for mod in mods: args += ["-M", "win" if mod == "logo" else mod]
    args += ["-k", key]
    for mod in reversed(mods): args += ["-m", "win" if mod == "logo" else mod]
    ok, out, err = run_command(args, timeout=5, env=graphical_environment())
    if not ok: return jsonify({"ok": False, "error": err or out or "wtype combo failed"}), 500
    return jsonify({"ok": True})


@app.route("/api/processes", methods=["GET"])
def processes():
    current_user = os.environ.get("DASHBOARD_USER") or getpass.getuser()
    # Prime per-process CPU counters briefly so the first response is meaningful.
    found = []
    for proc in psutil.process_iter(["pid", "name", "username", "memory_percent", "memory_info"]):
        try:
            if proc.info.get("pid") == os.getpid() or proc.info.get("username") != current_user:
                continue
            proc.cpu_percent(None)
            found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    time.sleep(0.12)
    results = []
    mem_total = max(psutil.virtual_memory().total, 1)
    for proc in found:
        try:
            info = proc.as_dict(attrs=["pid", "name", "username", "memory_percent", "memory_info"])
            cpu = max(0.0, float(proc.cpu_percent(None) or 0.0))
            # psutil's process CPU can exceed 100 on multi-core systems; normalize for UI bars.
            cpu = min(100.0, cpu / max(psutil.cpu_count() or 1, 1))
            rss = int((info.get("memory_info") or 0).rss) if info.get("memory_info") else 0
            mem_pct = min(100.0, max(0.0, rss * 100.0 / mem_total))
            results.append({"pid": info["pid"], "name": info.get("name") or "unknown",
                            "cpu_percent": round(cpu, 2), "memory_percent": round(mem_pct, 2),
                            "memory_mb": round(rss / (1024 * 1024), 1)})
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            continue
    results.sort(key=lambda item: (item["cpu_percent"], item["memory_percent"]), reverse=True)
    vm = psutil.virtual_memory()
    return jsonify({"ok": True, "cpu_percent": psutil.cpu_percent(None),
                    "memory": {"percent": vm.percent, "used_gb": round(vm.used/1024**3, 2),
                               "total_gb": round(vm.total/1024**3, 2)},
                    "processes": results})


@app.route("/api/process/kill", methods=["POST"])
def kill_process():
    data = request.get_json(silent=True) or {}

    try:
        pid = int(data.get("pid", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid PID"}), 400

    if pid <= 1 or pid == os.getpid():
        return jsonify({"ok": False, "error": "Refusing to kill this process"}), 400

    current_user = os.environ.get("DASHBOARD_USER") or getpass.getuser()

    try:
        proc = psutil.Process(pid)

        if proc.username() != current_user:
            return jsonify({
                "ok": False,
                "error": "You can only terminate your own processes",
            }), 403

        name = proc.name()
        proc.terminate()

        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)

        return jsonify({
            "ok": True,
            "message": f"Terminated {name} (PID {pid})",
        })

    except psutil.NoSuchProcess:
        return jsonify({"ok": False, "error": "Process no longer exists"}), 404
    except psutil.AccessDenied:
        return jsonify({"ok": False, "error": "Permission denied"}), 403
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500



@app.route("/api/mute", methods=["POST"])
def mute():
    data = request.get_json(silent=True) or {}
    action = data.get("action", "toggle")
    if action not in ("toggle", "on", "off"):
        return jsonify({"ok": False, "error": "Invalid mute action"}), 400

    if shutil.which("wpctl"):
        if action == "toggle":
            command = ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"]
        else:
            command = ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", action]
    elif shutil.which("pactl"):
        if action == "toggle":
            command = ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"]
        else:
            command = ["pactl", "set-sink-mute", "@DEFAULT_SINK@", action]
    else:
        return jsonify({"ok": False, "error": "wpctl/pactl is not installed"}), 500

    ok, out, err = run_command(command, env=graphical_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or out or "Mute command failed"}), 500
    return jsonify({"ok": True})


MEDIA_ACTIONS = {
    "play": "play",
    "pause": "pause",
    "stop": "stop",
    "next": "next",
    "previous": "previous",
}


@app.route("/api/media", methods=["POST"])
def media_control():
    data = request.get_json(silent=True) or {}
    action = MEDIA_ACTIONS.get(data.get("action"))
    if not action:
        return jsonify({"ok": False, "error": "Invalid media action"}), 400

    if not shutil.which("playerctl"):
        return jsonify({"ok": False, "error": "playerctl is not installed"}), 500

    ok, out, err = run_command(["playerctl", action], timeout=8, env=graphical_environment())
    if not ok:
        return jsonify({
            "ok": False,
            "error": err or out or f"No player accepted the {action} command",
        }), 500
    return jsonify({"ok": True, "action": action})


KEY_ACTIONS = {
    "capslock": "Caps_Lock",
    "numlock": "Num_Lock",
    "scrolllock": "Scroll_Lock",
    "pause": "Pause",
    "stop": "Cancel",
}


@app.route("/api/key", methods=["POST"])
def key_control():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    key = KEY_ACTIONS.get(action)
    if not key:
        return jsonify({"ok": False, "error": "Invalid key action"}), 400

    # Prefer wtype on Wayland. Unlike ydotool, it uses the compositor's
    # virtual-keyboard protocol and needs no ydotoold/uinput daemon.
    if shutil.which("wtype"):
        ok, out, err = run_command(["wtype", "-k", key], timeout=8, env=graphical_environment())
        if ok:
            return jsonify({"ok": True, "action": action, "backend": "wtype"})
        wtype_error = err or out or "wtype failed"
    else:
        wtype_error = "wtype is not installed"

    # Keep ydotool as a compatibility fallback for systems where wtype is
    # unavailable, but it is no longer the primary backend.
    legacy_codes = {"capslock": 58, "numlock": 69, "scrolllock": 70, "pause": 119, "stop": 128}
    if shutil.which("ydotool"):
        code = legacy_codes[action]
        ok, out, err = run_command(["ydotool", "key", f"{code}:1", f"{code}:0"], timeout=8, env=graphical_environment())
        if ok:
            return jsonify({"ok": True, "action": action, "backend": "ydotool-fallback"})
        return jsonify({"ok": False, "error": f"wtype failed: {wtype_error}; ydotool fallback failed: {err or out}"}), 500

    return jsonify({"ok": False, "error": f"{wtype_error}. Install wtype with: sudo pacman -S wtype"}), 500


SERPANTINUM_SETTINGS = Path.home() / ".config/serpantinum/settings.json"
SERPANTINUM_RELOAD = Path.home() / ".local/share/serpantinum/src/scripts/reload.sh"


def read_serpantinum_settings():
    if not SERPANTINUM_SETTINGS.exists():
        return None, "Serpantinum settings.json was not found"
    try:
        return json.loads(SERPANTINUM_SETTINGS.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, f"Could not parse Serpantinum settings: {exc}"


@app.route("/api/serpantinum/settings", methods=["GET"])
def serpantinum_settings():
    settings, error = read_serpantinum_settings()
    if error:
        return jsonify({"ok": False, "error": error}), 500

    # Return the settings as-is except for location, which is unnecessary for
    # the dashboard's settings view.
    safe = dict(settings)
    general = dict(safe.get("general", {}))
    general.pop("location", None)
    safe["general"] = general
    return jsonify({"ok": True, "settings": safe})


@app.route("/api/serpantinum/reload", methods=["POST"])
def serpantinum_reload():
    if not SERPANTINUM_RELOAD.exists():
        return jsonify({"ok": False, "error": "Serpantinum reload script was not found"}), 404
    ok, out, err = run_command(["bash", str(SERPANTINUM_RELOAD)], timeout=20, env=graphical_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or out or "Serpantinum reload failed"}), 500
    return jsonify({"ok": True, "message": "Serpantinum reload requested"})


@app.route("/api/serpantinum/action", methods=["POST"])
def serpantinum_action():
    data = request.get_json(silent=True) or {}
    command = data.get("command")
    target = data.get("target")
    sub = data.get("sub")

    if command not in ("toggle", "open", "close"):
        return jsonify({"ok": False, "error": "Invalid Serpantinum action"}), 400
    if command != "close" and not target:
        return jsonify({"ok": False, "error": "Missing Serpantinum target"}), 400

    executable = shutil.which("serpantinum") or str(Path.home() / ".local/bin/serpantinum")
    if not Path(executable).exists() and not shutil.which("serpantinum"):
        return jsonify({"ok": False, "error": "serpantinum command not found"}), 404

    args = [executable, "msg", command]
    if target:
        args.append(target)
    if sub:
        args.append(sub)

    ok, out, err = run_command(args, timeout=10, env=graphical_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or out or "Serpantinum action failed"}), 500
    return jsonify({"ok": True, "message": out or "Action sent"})


# ---------------------------------------------------------------------------
# Wallpapers
# Uses Serpantinum's own wallpaper index and IPC. The dashboard never calls
# awww/swww directly and never starts a second wallpaper implementation.
# ---------------------------------------------------------------------------

WALLPAPER_DIR = Path.home() / "Pictures/Wallpapers"
SERPANTINUM_STATE = Path.home() / ".local/state/serpantinum"
SERPANTINUM_CACHE = Path.home() / ".cache/serpantinum"
SERPANTINUM_QS = Path.home() / ".local/share/serpantinum/src/quickshell"
WALLPAPER_INDEX_CANDIDATES = [
    SERPANTINUM_CACHE / "wallpaper/current_index.json",
    SERPANTINUM_STATE / "wallpaper/current_index.json",
    Path.home() / ".local/share/serpantinum/current_index.json",
]

def _wallpaper_index():
    for path in WALLPAPER_INDEX_CANDIDATES:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("items"), list):
                    return data, path
            except Exception:
                pass
    return {"srcDir": str(WALLPAPER_DIR), "items": []}, None

def _safe_wallpaper_path(raw_path):
    if not raw_path:
        return None
    try:
        candidate = Path(raw_path).expanduser().resolve()
        allowed_roots = [
            WALLPAPER_DIR.resolve(),
            (SERPANTINUM_CACHE / "wallpaper").resolve(),
            (SERPANTINUM_STATE / "wallpaper").resolve(),
        ]
        if any(candidate == root or root in candidate.parents for root in allowed_roots):
            return candidate
    except Exception:
        pass
    return None

def _wallpaper_url_for_path(path):
    safe = _safe_wallpaper_path(path)
    if not safe:
        return ""
    try:
        rel = safe.relative_to(WALLPAPER_DIR.resolve())
        return "/api/wallpaper/file/" + urllib.parse.quote(str(rel).replace(os.sep, "/"), safe="/")
    except ValueError:
        pass
    for root in ((SERPANTINUM_CACHE / "wallpaper").resolve(),
                 (SERPANTINUM_STATE / "wallpaper").resolve()):
        try:
            rel = safe.relative_to(root)
            return "/api/wallpaper/cache/" + urllib.parse.quote(str(rel).replace(os.sep, "/"), safe="/")
        except ValueError:
            continue
    return ""

@app.route("/api/wallpapers", methods=["GET"])
def wallpapers():
    data, index_path = _wallpaper_index()
    items = []
    for item in data.get("items", []):
        if not isinstance(item, dict):
            continue
        obj = dict(item)
        fp = obj.get("filePath") or ""
        obj["thumbUrl"] = _wallpaper_url_for_path(obj.get("posterPath") or fp)
        obj["fileUrl"] = _wallpaper_url_for_path(fp)
        obj["posterUrl"] = _wallpaper_url_for_path(obj.get("posterPath") or "")
        items.append(obj)
    return jsonify({
        "ok": True,
        "items": items,
        "source": str(index_path) if index_path else None,
        "srcDir": data.get("srcDir") or str(WALLPAPER_DIR),
        "updatedAt": data.get("updatedAt"),
    })


def _video_wallpaper_preview(candidate):
    """Extract a small JPEG frame from a video wallpaper using ffmpeg, if available."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg: return None
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "00:00:02",
            "-i", str(candidate), "-frames:v", "1", "-vf", "scale=220:124:force_original_aspect_ratio=decrease,pad=220:124:(ow-iw)/2:(oh-ih)/2",
            "-q:v", "8", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=8)
        if result.returncode != 0 or not result.stdout: return None
        return app.response_class(result.stdout, mimetype="image/jpeg", headers={"Cache-Control":"public, max-age=86400"})
    except Exception:
        return None

def _wallpaper_preview_response(candidate):
    """Return a tiny JPEG preview for fast wallpaper browsing."""
    try:
        from PIL import Image, ImageOps
        with Image.open(candidate) as image:
            if getattr(image, "is_animated", False):
                image.seek(0)
            image = image.convert("RGB")
            tiny = request.args.get("tiny") == "1"
            size = (220, 124) if tiny else (420, 236)
            quality = 22 if tiny else 42
            image.thumbnail(size, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", size, (18, 19, 24))
            x = (size[0] - image.width) // 2
            y = (size[1] - image.height) // 2
            canvas.paste(image, (x, y))
            out = io.BytesIO()
            canvas.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
            out.seek(0)
        from flask import send_file
        response = send_file(out, mimetype="image/jpeg", max_age=86400)
        response.headers["Cache-Control"] = "public, max-age=86400, immutable"
        return response
    except Exception:
        return None

@app.route("/api/wallpaper/file/<path:filename>", methods=["GET"])
def wallpaper_file(filename):
    from flask import send_from_directory
    root = WALLPAPER_DIR.resolve()
    candidate = _safe_wallpaper_path(root / filename)
    if not candidate or not candidate.is_file():
        return jsonify({"ok": False, "error": "Wallpaper file not found"}), 404
    if request.args.get("preview") == "1":
        preview = _wallpaper_preview_response(candidate)
        if preview is not None:
            return preview
        if candidate.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".gif"}:
            generated = _video_wallpaper_preview(candidate)
            if generated is not None: return generated
    rel = candidate.relative_to(root)
    return send_from_directory(root, str(rel))

@app.route("/api/wallpaper/cache/<path:filename>", methods=["GET"])
def wallpaper_cache_file(filename):
    from flask import send_from_directory
    for root in ((SERPANTINUM_CACHE / "wallpaper").resolve(),
                 (SERPANTINUM_STATE / "wallpaper").resolve()):
        candidate = _safe_wallpaper_path(root / filename)
        if candidate and candidate.is_file():
            if request.args.get("preview") == "1":
                preview = _wallpaper_preview_response(candidate)
                if preview is not None:
                    return preview
                if candidate.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".gif"}:
                    generated = _video_wallpaper_preview(candidate)
                    if generated is not None: return generated
            rel = candidate.relative_to(root)
            return send_from_directory(root, str(rel))
    return jsonify({"ok": False, "error": "Wallpaper preview not found"}), 404

def wallpaper_apply_internal(safe):
    executable = shutil.which("serpantinum") or str(Path.home() / ".local/bin/serpantinum")
    if not shutil.which("serpantinum") and not Path(executable).exists():
        raise RuntimeError("serpantinum command not found")
    wallpaper_args = [executable, "ipc", "call", "wallpaper", "setWallpaper", "all", str(safe), "fade"]
    ok, out, err = run_command(wallpaper_args, timeout=15, env=graphical_environment())
    if not ok:
        raise RuntimeError(err or out or "Serpantinum wallpaper IPC failed")
    matugen_args = [executable, "ipc", "call", "matugen", "generateImage", str(safe)]
    matugen_ok, matugen_out, matugen_err = run_command(matugen_args, timeout=30, env=graphical_environment())
    if not matugen_ok:
        raise RuntimeError(matugen_err or matugen_out or "Serpantinum Matugen IPC failed")
    return "Wallpaper and Matugen applied"

@app.route("/api/wallpaper/apply", methods=["POST"])
def wallpaper_apply():
    payload = request.get_json(silent=True) or {}
    raw = payload.get("path", "")
    safe = _safe_wallpaper_path(raw)
    if not safe or not safe.is_file():
        return jsonify({"ok": False, "error": "Invalid wallpaper path"}), 400

    executable = shutil.which("serpantinum") or str(Path.home() / ".local/bin/serpantinum")
    if not shutil.which("serpantinum") and not Path(executable).exists():
        return jsonify({"ok": False, "error": "serpantinum command not found"}), 404

    wallpaper_args = [executable, "ipc", "call", "wallpaper", "setWallpaper", "all", str(safe), "fade"]
    ok, out, err = run_command(wallpaper_args, timeout=15, env=graphical_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or out or "Serpantinum wallpaper IPC failed"}), 500

    matugen_args = [executable, "ipc", "call", "matugen", "generateImage", str(safe)]
    matugen_ok, matugen_out, matugen_err = run_command(matugen_args, timeout=30, env=graphical_environment())
    if not matugen_ok:
        return jsonify({"ok": False, "path": str(safe),
                         "error": matugen_err or matugen_out or "Serpantinum Matugen IPC failed",
                         "wallpaperApplied": True}), 500
    return jsonify({"ok": True, "path": str(safe), "message": "Wallpaper and Matugen applied",
                    "wallpaperApplied": True, "matugenApplied": True})

@app.route("/api/wallpaper/current", methods=["GET"])
def wallpaper_current():
    executable = shutil.which("serpantinum") or str(Path.home() / ".local/bin/serpantinum")
    if shutil.which("serpantinum") or Path(executable).exists():
        # Use Serpantinum's public IPC rather than trying to inspect aww/swww.
        ok, out, err = run_command(
            [executable, "ipc", "call", "wallpaper", "getWallpaperPath", ""],
            timeout=8,
            env=graphical_environment(),
        )
        if ok:
            current = out.strip()
            return jsonify({"ok": True, "path": current})
    return jsonify({"ok": True, "path": ""})

_PUBLIC_IP_CACHE = {"value": "Unavailable", "time": 0.0}
_PUBLIC_IP_LOCK = threading.Lock()

def public_ip():
    """Return the public IP, but never block the dashboard on every network refresh."""
    now = time.monotonic()
    with _PUBLIC_IP_LOCK:
        if now - _PUBLIC_IP_CACHE["time"] < 60:
            return _PUBLIC_IP_CACHE["value"]

    urls = [
        "https://api.ipify.org?format=json",
        "https://ifconfig.me/ip",
    ]
    value = "Unavailable"
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CozySystemDashboard/1.0"})
            with urllib.request.urlopen(req, timeout=2.5) as response:
                candidate = response.read(128).decode("utf-8", "replace").strip()
            if candidate.startswith("{"):
                candidate = json.loads(candidate).get("ip", "")
            if re.fullmatch(r"[0-9a-fA-F:.]+", candidate):
                value = candidate
                break
        except Exception:
            continue

    with _PUBLIC_IP_LOCK:
        _PUBLIC_IP_CACHE["value"] = value
        _PUBLIC_IP_CACHE["time"] = time.monotonic()
    return value


def detect_network():
    net_io = psutil.net_io_counters()
    addresses = psutil.net_if_addrs()
    interface_stats = psutil.net_if_stats()

    interfaces = {}

    for name, addr_list in addresses.items():
        ips = []

        for addr in addr_list:
            family = getattr(addr.family, "name", str(addr.family))
            if family == "AF_INET" and not addr.address.startswith("127."):
                ips.append(addr.address)
            elif family == "AF_INET6" and not addr.address.startswith("::1"):
                ips.append(addr.address)

        stat = interface_stats.get(name)
        is_wireless = Path(f"/sys/class/net/{name}/wireless").exists()

        if name == "lo":
            kind = "Loopback"
        elif is_wireless:
            kind = "Wi-Fi"
        else:
            kind = "Ethernet"

        interfaces[name] = {
            "isup": bool(stat and stat.isup),
            "type": kind,
            "addresses": ips,
            "speed": (
                int(stat.speed)
                if stat and stat.speed and stat.speed > 0
                else None
            ),
        }

    # Find NetworkManager's active connection if available.
    connection = {
        "type": "Unknown",
        "device": "Unknown",
        "interface": "Unknown",
        "ssid": "N/A",
        "ipv4": "N/A",
        "ipv6": "N/A",
        "gateway": "N/A",
        "dns": "N/A",
        "speed": None,
    }

    active_device = None

    if shutil.which("nmcli"):
        ok, stdout, _ = run_command(
            [
                "nmcli",
                "-t",
                "-f",
                "DEVICE,TYPE,STATE,CONNECTION",
                "device",
            ],
            timeout=4,
        )

        if ok:
            for line in stdout.splitlines():
                parts = line.split(":", 3)
                if len(parts) != 4:
                    continue

                device, kind, state, connection_name = parts

                if state == "connected" and device in interfaces:
                    active_device = device
                    connection["device"] = device
                    connection["interface"] = device
                    connection["type"] = "Wi-Fi" if kind == "wifi" else "Ethernet"
                    connection["ssid"] = (
                        connection_name if kind == "wifi" else "N/A"
                    )
                    connection["speed"] = interfaces[device]["speed"]
                    break

    # Fallback if NetworkManager is unavailable.
    if active_device is None:
        for name, info in interfaces.items():
            if info["isup"] and info["type"] != "Loopback":
                active_device = name
                connection["device"] = name
                connection["interface"] = name
                connection["type"] = info["type"]
                connection["speed"] = info["speed"]
                break

    ipv4 = []
    ipv6 = []

    for name, addr_list in addresses.items():
        for addr in addr_list:
            family = getattr(addr.family, "name", str(addr.family))
            if family == "AF_INET" and not addr.address.startswith("127."):
                ipv4.append(addr.address)
            elif family == "AF_INET6" and not addr.address.startswith("::1"):
                ipv6.append(addr.address)

    connection["ipv4"] = ipv4[0] if ipv4 else "N/A"
    connection["ipv6"] = ipv6[0] if ipv6 else "N/A"

    # Default gateway.
    if shutil.which("ip"):
        ok, stdout, _ = run_command(
            ["ip", "route", "show", "default"], timeout=3
        )
        if ok:
            match = re.search(r"default via ([^ ]+)", stdout)
            if match:
                connection["gateway"] = match.group(1)

    # DNS servers from resolvectl, falling back to /etc/resolv.conf.
    dns_servers = []

    if shutil.which("resolvectl"):
        ok, stdout, _ = run_command(
            ["resolvectl", "dns"], timeout=3
        )
        if ok:
            for line in stdout.splitlines():
                if ":" in line:
                    dns_servers.extend(
                        token for token in line.split()[2:] if token
                    )

    if not dns_servers:
        try:
            for line in Path("/etc/resolv.conf").read_text().splitlines():
                if line.startswith("nameserver "):
                    dns_servers.append(line.split()[1])
        except OSError:
            pass

    if dns_servers:
        connection["dns"] = ", ".join(dict.fromkeys(dns_servers))

    return {
        "bytes_sent": net_io.bytes_sent,
        "bytes_recv": net_io.bytes_recv,
        "connection": connection,
        "interfaces": interfaces,
        "public_ip": public_ip(),
    }




def _atomic_json_write(path, data):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _set_nested(data, dotted, value):
    parts = [p for p in dotted.split(".") if p]
    if not parts or len(parts) > 6:
        raise ValueError("Invalid setting path")
    cur = data
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def _theme_file():
    # Matugen's generated Quickshell palette is the source of truth for the
    # dashboard. Prefer the exact file used by the user's Hypr/Quickshell
    # setup, with a Serpantinum-local fallback.
    # Serpantinum/Matugen writes the live palette here.
    # This is the source of truth for the dashboard theme.
    candidates = [
        Path.home() / ".local/state/serpantinum/qs_colors.json",
        Path.home() / ".local/state/serpantinum/qs_matugen_colors.json",
        Path.home() / ".config/hypr/scripts/quickshell/qs_colors.json",
        Path.home() / ".config/serpantinum/qs_colors.json",
        Path.home() / ".config/qs_colors.json",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


@app.route("/api/serpantinum/theme", methods=["GET"])
def serpantinum_theme():
    path = _theme_file()
    colors = {}
    if path:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            colors = raw if isinstance(raw, dict) else {}
        except Exception:
            colors = {}
    modified = None
    if path:
        try:
            modified = path.stat().st_mtime
        except OSError:
            pass
    # Some Matugen templates wrap the palette in a `colors` object. Normalize
    # neither shape here so the frontend can consume either directly.
    return jsonify({"colors": colors, "source": str(path) if path else None, "modified": modified})


@app.route("/api/serpantinum/settings", methods=["PUT"])
def serpantinum_settings_update():
    payload = request.get_json(silent=True) or {}
    path = str(payload.get("path", "")).strip()
    if not path or any(x in path for x in ("__", "[", "]", "(" , ")")):
        return jsonify({"ok": False, "error": "Invalid setting path"}), 400
    if "value" not in payload:
        return jsonify({"ok": False, "error": "Missing value"}), 400
    try:
        settings = json.loads(SERPANTINUM_SETTINGS.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("settings.json is not an object")
        _set_nested(settings, path, payload["value"])
        _atomic_json_write(SERPANTINUM_SETTINGS, settings)
        if SERPANTINUM_RELOAD.exists():
            run_command(["bash", str(SERPANTINUM_RELOAD)], timeout=15, env=graphical_environment())
        return jsonify({"ok": True, "settings": settings})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/power-profile", methods=["GET", "POST"])
def power_profile():
    if request.method == "GET":
        ok, out, err = run_command(["powerprofilesctl", "get"], timeout=5)
        if ok:
            return jsonify({"ok": True, "profile": out.strip()})
        return jsonify({"ok": False, "error": err or "powerprofilesctl unavailable"}), 200
    payload = request.get_json(silent=True) or {}
    profile = payload.get("profile")
    allowed = {"performance", "balanced", "power-saver"}
    if profile not in allowed:
        return jsonify({"ok": False, "error": "Profile must be performance, balanced, or power-saver"}), 400
    ok, out, err = run_command(["powerprofilesctl", "set", profile], timeout=8)
    if not ok:
        return jsonify({"ok": False, "error": err or "Unable to change power profile"}), 500
    return jsonify({"ok": True, "profile": profile})


KEEP_AWAKE_PID = Path("/tmp/system-dashboard-keep-awake.pid")

def _keep_awake_pid():
    try:
        pid = int(KEEP_AWAKE_PID.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        try:
            KEEP_AWAKE_PID.unlink(missing_ok=True)
        except Exception:
            pass
        return None

def _set_keep_awake(enabled):
    current = _keep_awake_pid()
    if not enabled:
        if current:
            try:
                os.kill(current, 15)
            except ProcessLookupError:
                pass
            except Exception:
                try: os.kill(current, 9)
                except Exception: pass
        try: KEEP_AWAKE_PID.unlink(missing_ok=True)
        except Exception: pass
        return False
    if current:
        return True
    if not shutil.which("systemd-inhibit"):
        raise RuntimeError("systemd-inhibit is unavailable")
    env = graphical_environment()
    target_user = os.environ.get("DASHBOARD_USER") or getpass.getuser()
    inhibit = [
        "systemd-inhibit", "--what=idle:sleep", "--mode=block",
        "--who=System Dashboard", "--why=Do Not Disturb (keep awake)",
        "sleep", "infinity"
    ]
    # If Flask is running as root under a system service, run the inhibitor in
    # the actual desktop user's session so logind sees the same user/session.
    if os.geteuid() == 0 and target_user != "root":
        inhibit = ["runuser", "-u", target_user, "--"] + inhibit
    proc = subprocess.Popen(
        inhibit, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        start_new_session=True, env=env, text=True
    )
    time.sleep(0.12)
    if proc.poll() is not None:
        err = (proc.stderr.read() if proc.stderr else "").strip()
        raise RuntimeError(err or "systemd-inhibit exited immediately")
    KEEP_AWAKE_PID.write_text(str(proc.pid) + "\n", encoding="utf-8")
    return True

@app.route("/api/keep-awake", methods=["GET", "POST"])
def keep_awake():
    if request.method == "GET":
        return jsonify({"ok": True, "enabled": _keep_awake_pid() is not None})
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    try:
        state = _set_keep_awake(enabled)
        return jsonify({"ok": True, "enabled": state})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/battery", methods=["GET"])
def battery():
    # Prefer UPower, matching Serpantinum's UPower-backed battery widgets.
    ok, devices, _ = run_command(["upower", "-e"], timeout=5)
    if ok:
        battery_paths = [x.strip() for x in devices.splitlines() if "battery" in x.lower()]
        if battery_paths:
            ok2, info, _ = run_command(["upower", "-i", battery_paths[0]], timeout=5)
            if ok2:
                vals = {}
                for line in info.splitlines():
                    if ":" in line:
                        k, v = line.strip().split(":", 1)
                        vals[k.strip()] = v.strip()
                return jsonify({"ok": True, "source": "upower", "percentage": vals.get("percentage"), "state": vals.get("state"), "time_to_empty": vals.get("time to empty"), "time_to_full": vals.get("time to full"), "energy_rate": vals.get("energy-rate"), "health": vals.get("capacity")})
    ps = Path("/sys/class/power_supply")
    for bat in sorted(ps.glob("BAT*")):
        try:
            pct = int((bat / "capacity").read_text().strip())
            state = (bat / "status").read_text().strip()
            return jsonify({"ok": True, "source": "sysfs", "percentage": f"{pct}%", "state": state})
        except Exception:
            continue
    return jsonify({"ok": True, "available": False, "message": "No battery detected (desktop mode)."})


@app.route("/api/bluetooth", methods=["GET", "POST"])
def bluetooth():
    if request.method == "GET":
        ok, out, _ = run_command(["rfkill", "list", "bluetooth"], timeout=4)
        if not ok:
            return jsonify({"ok": False, "powered": None, "error": "Bluetooth/rfkill unavailable"})
        blocked = any("soft blocked: yes" in line.lower() for line in out.splitlines())
        return jsonify({"ok": True, "powered": not blocked})
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    rf_action = "unblock" if enabled else "block"
    ok, _, err = run_command(["rfkill", rf_action, "bluetooth"], timeout=5)
    if not ok:
        return jsonify({"ok": False, "error": err or "Could not change Bluetooth rfkill state"}), 500
    if enabled and shutil.which("bluetoothctl"):
        run_command(["bluetoothctl", "power", "on"], timeout=5)
    elif not enabled and shutil.which("bluetoothctl"):
        run_command(["bluetoothctl", "power", "off"], timeout=5)
    return jsonify({"ok": True, "powered": enabled})


@app.route("/api/airplane", methods=["GET", "POST"])
def airplane():
    if request.method == "GET":
        wifi_ok, wifi_out, _ = run_command(["nmcli", "radio", "wifi"], timeout=4)
        bt_ok, bt_out, _ = run_command(["rfkill", "list", "bluetooth"], timeout=4)
        wifi = wifi_out.strip().lower() == "enabled" if wifi_ok else None
        bt = None if not bt_ok else not any("soft blocked: yes" in line.lower() for line in bt_out.splitlines())
        return jsonify({"ok": True, "wifi": wifi, "bluetooth": bt, "airplane": wifi is False and bt is False})
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    wifi_cmd = ["nmcli", "radio", "wifi", "off" if enabled else "on"]
    run_command(wifi_cmd, timeout=5)
    if enabled:
        run_command(["rfkill", "block", "bluetooth"], timeout=5)
    else:
        run_command(["rfkill", "unblock", "bluetooth"], timeout=5)
        run_command(["bluetoothctl", "power", "on"], timeout=5)
    return jsonify({"ok": True, "airplane": enabled})


@app.route("/api/eye-protection", methods=["GET", "POST"])
def eye_protection():
    script = Path.home() / ".local/share/serpantinum/src/scripts/blue_light_filter.sh"
    if not script.exists():
        return jsonify({"ok": False, "error": "Serpantinum blue_light_filter.sh not found"}), 404
    if request.method == "GET":
        ok, out, err = run_command(["bash", str(script), "status"], timeout=6)
        status = out.strip()
        low = status.lower()
        enabled = any(token in low for token in ("enabled", "active", "on", "4000k")) and "off" not in low and "disabled" not in low
        return jsonify({"ok": ok, "status": status, "enabled": enabled, "error": err.strip() if err else None})
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    if enabled:
        temp = int(payload.get("temperature", 4000))
        temp = max(1000, min(10000, temp))
        cmd = ["bash", str(script), "set", str(temp)]
    else:
        cmd = ["bash", str(script), "reset"]
    env = graphical_environment()
    user_home = env.get("HOME", str(Path.home()))
    env["PATH"] = f"{user_home}/.local/bin:" + env.get("PATH", "")
    ok, out, err = run_command(cmd, timeout=8, env=env)
    if not ok:
        return jsonify({"ok": False, "error": err or out or "Eye protection failed"}), 500
    return jsonify({"ok": True, "enabled": enabled, "output": out.strip()})


@app.route("/api/equalizer", methods=["GET", "POST"])
def equalizer():
    script = Path.home() / ".local/share/serpantinum/src/quickshell/media/equalizer.sh"
    if not script.exists():
        return jsonify({"ok": False, "error": "Serpantinum equalizer.sh not found"}), 404
    if request.method == "GET":
        ok, out, err = run_command(["bash", str(script), "get"], timeout=6)
        return jsonify({"ok": ok, "data": out.strip(), "error": err.strip() if err else None})
    payload = request.get_json(silent=True) or {}
    preset = payload.get("preset")
    allowed = {"Flat", "Bass", "Treble", "Vocal", "Pop", "Rock", "Jazz", "Classic"}
    if preset not in allowed:
        return jsonify({"ok": False, "error": "Unknown equalizer preset"}), 400
    ok, out, err = run_command(["bash", str(script), "preset", preset], timeout=12, env=graphical_environment())
    if not ok:
        return jsonify({"ok": False, "error": err or out or "Equalizer failed"}), 500
    return jsonify({"ok": True, "preset": preset})

@app.route("/api/network", methods=["GET"])
def network():
    try:
        return jsonify({"ok": True, **detect_network()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/speedtest", methods=["POST"])
def speedtest():
    """Short, bounded Cloudflare test; each phase can fail independently."""
    user_agent = "CozySystemDashboard/2.0"
    result = {"ok": True, "ping_ms": None, "download_mbps": None, "upload_mbps": None}
    errors = []
    def fetch(url, data=None, method=None, timeout=12):
        headers = {"User-Agent": user_agent, "Cache-Control": "no-cache"}
        if data is not None: headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        return urllib.request.urlopen(req, timeout=timeout)
    try:
        started = time.perf_counter()
        with fetch("https://speed.cloudflare.com/__down?bytes=1", timeout=7) as response:
            response.read(1)
        result["ping_ms"] = round((time.perf_counter()-started)*1000, 1)
    except Exception as exc: errors.append("Ping: " + str(exc))
    try:
        # 3 MiB is sufficient for a quick estimate and avoids long mobile/LAN timeouts.
        size = 3 * 1024 * 1024
        started = time.perf_counter(); total = 0
        with fetch(f"https://speed.cloudflare.com/__down?bytes={size}", timeout=14) as response:
            while total < size:
                chunk = response.read(min(128*1024, size-total))
                if not chunk: break
                total += len(chunk)
        elapsed = max(time.perf_counter()-started, 0.001)
        if total: result["download_mbps"] = round(total*8/elapsed/1_000_000, 1)
        else: errors.append("Download returned no data")
    except Exception as exc: errors.append("Download: " + str(exc))
    try:
        payload = os.urandom(512 * 1024)
        started = time.perf_counter()
        with fetch("https://speed.cloudflare.com/__up", data=payload, method="POST", timeout=14) as response:
            response.read(1024)
        elapsed = max(time.perf_counter()-started, 0.001)
        result["upload_mbps"] = round(len(payload)*8/elapsed/1_000_000, 1)
    except Exception as exc: errors.append("Upload: " + str(exc))
    result["message"] = "Test complete." if not errors else "Partial results. " + " • ".join(errors)
    if all(result[k] is None for k in ("ping_ms", "download_mbps", "upload_mbps")):
        result["ok"] = False
        return jsonify({**result, "error": result["message"]}), 502
    return jsonify(result)


# ---------------------------------------------------------------------------
# Local Ollama chat
# ---------------------------------------------------------------------------
OLLAMA_BASE = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

@app.route("/api/ollama/server", methods=["GET", "POST"])
def ollama_server():
    """Inspect/start/stop the local Ollama systemd service."""
    action = str((request.get_json(silent=True) or {}).get("action", "status")).lower() if request.method == "POST" else "status"
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return jsonify({"ok": False, "error": "systemctl is unavailable"}), 500
    env = graphical_environment()
    try:
        if action == "status":
            ok, out, err = run_command([systemctl, "is-active", "ollama.service"], timeout=4, env=env)
            active = ok and out.strip() == "active"
            # A user-installed Ollama process may run without the system service.
            if not active:
                try:
                    with urllib.request.urlopen(OLLAMA_BASE + "/api/tags", timeout=1): active = True
                except Exception: pass
            return jsonify({"ok": True, "active": active, "managed": True})
        if action not in {"start", "stop"}:
            return jsonify({"ok": False, "error": "Unknown action"}), 400
        # Service management requires an existing local sudo ticket; never accept passwords over HTTP.
        sudo = shutil.which("sudo")
        if not sudo:
            return jsonify({"ok": False, "needs_sudo": True, "error": "Run `sudo -v` in a local terminal, then retry."}), 403
        ok, out, err = run_command([sudo, "-n", systemctl, action, "ollama.service"], timeout=20, env=env)
        if not ok:
            app.logger.info("Ollama service %s needs authorization or failed: %s", action, (err or out)[:300])
            return jsonify({"ok": False, "needs_sudo": True, "error": "Ollama service control needs sudo authorization. Run `sudo -v` in a terminal on the PC, then try again."}), 403
        app.logger.info("Ollama system service %s requested", action)
        time.sleep(0.6)
        status_ok, status_out, _ = run_command([systemctl, "is-active", "ollama.service"], timeout=4, env=env)
        return jsonify({"ok": True, "active": status_ok and status_out.strip() == "active"})
    except Exception as exc:
        app.logger.warning("Ollama service action failed: %s", str(exc)[:300])
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.route("/api/ollama/models", methods=["GET"])
def ollama_models():
    try:
        req = urllib.request.Request(OLLAMA_BASE + "/api/tags", headers={"Accept":"application/json"})
        with urllib.request.urlopen(req, timeout=4) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        return jsonify({"ok": True, "models": data.get("models", []), "host": OLLAMA_BASE})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not reach Ollama at {OLLAMA_BASE}: {exc}", "models": []}), 503

@app.route("/api/ollama/chat", methods=["POST"])
def ollama_chat():
    payload = request.get_json(silent=True) or {}
    model = str(payload.get("model", "")).strip()
    messages = payload.get("messages")
    if not model or not isinstance(messages, list) or not messages:
        return jsonify({"ok": False, "error": "Choose a model and send at least one message."}), 400
    if len(messages) > 80:
        messages = messages[-80:]
    clean = []
    for item in messages:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant", "system"):
            continue
        content = str(item.get("content", ""))[:20000]
        clean.append({"role": item["role"], "content": content})
    body = json.dumps({"model": model, "messages": clean, "stream": True}).encode()
    def generate():
        try:
            req = urllib.request.Request(OLLAMA_BASE + "/api/chat", data=body,
                headers={"Content-Type":"application/json", "Accept":"application/x-ndjson"}, method="POST")
            with urllib.request.urlopen(req, timeout=180) as response:
                for raw in response:
                    if not raw.strip(): continue
                    try:
                        item = json.loads(raw.decode("utf-8", "replace"))
                        if item.get("error"):
                            yield "data: " + json.dumps({"error": item["error"]}) + "\n\n"
                            break
                        msg = item.get("message", {}).get("content", "")
                        if msg: yield "data: " + json.dumps({"token": msg}) + "\n\n"
                        if item.get("done"):
                            yield "data: " + json.dumps({"done": True, "total_duration": item.get("total_duration")}) + "\n\n"
                            break
                    except Exception:
                        continue
        except Exception as exc:
            yield "data: " + json.dumps({"error": str(exc)}) + "\n\n"
    from flask import Response
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no", "Connection":"keep-alive"})


# ---------------------------------------------------------------------------
# Sharing chat: persistent text + arbitrary-count file attachments.
# Messages are stored in SQLite; file bytes are stored separately on disk.
# The app does not impose a message/file-size/count cap. Physical disk, browser,
# and network limits still apply. All endpoints use the dashboard login.
# ---------------------------------------------------------------------------
try:
    _SHARING_HOME = Path(pwd.getpwnam(DASHBOARD_USER).pw_dir)
except Exception:
    _SHARING_HOME = Path.home()
SHARING_DIR = _SHARING_HOME / ".local/share/system-dashboard/sharing"
SHARING_FILES_DIR = SHARING_DIR / "files"
SHARING_DB = SHARING_DIR / "messages.sqlite3"
DASHBOARD_AUTH_DB = _SHARING_HOME / ".local/share/system-dashboard/dashboard-users.sqlite3"
DASHBOARD_AUDIT_DB = _SHARING_HOME / ".local/share/system-dashboard/activity.sqlite3"
DASHBOARD_PROFILE_FILE = _SHARING_HOME / ".local/share/system-dashboard/profiles.json"


@app.route("/api/login/users", methods=["GET"])
def dashboard_login_users():
    """Expose only the minimal account labels and profile pictures needed by login."""
    users = [{"username": "marwan", "display_name": "marwan", "avatar": ""}]
    try:
        _dashboard_init_db()
        with _dashboard_db(DASHBOARD_AUTH_DB) as db:
            rows = db.execute("SELECT username FROM users WHERE lower(username) != 'marwan' ORDER BY username COLLATE NOCASE").fetchall()
        users.extend({"username": str(row["username"]), "display_name": str(row["username"]), "avatar": ""} for row in rows)
    except Exception as exc:
        app.logger.warning("Could not load dashboard login roster: %s", str(exc)[:160])
    try:
        profiles = json.loads(DASHBOARD_PROFILE_FILE.read_text(encoding="utf-8")) if DASHBOARD_PROFILE_FILE.exists() else {}
        if not isinstance(profiles, dict): profiles = {}
    except (OSError, ValueError):
        profiles = {}
    for user in users:
        profile = profiles.get(user["username"], {})
        if isinstance(profile, dict):
            user["display_name"] = str(profile.get("display_name") or user["username"])[:32]
            avatar = profile.get("avatar", "")
            # Only serve the same constrained image data URLs accepted by profile updates.
            if isinstance(avatar, str) and len(avatar) <= 1_800_000 and re.fullmatch(r"data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+", avatar):
                user["avatar"] = avatar
    return jsonify({"ok": True, "users": users})
_DASHBOARD_PROFILE_LOCK = threading.RLock()
_DASHBOARD_DB_LOCK = threading.RLock()


def _dashboard_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try: os.chmod(path.parent, 0o700)
    except OSError: pass
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def _dashboard_init_db():
    with _DASHBOARD_DB_LOCK:
        with _dashboard_db(DASHBOARD_AUTH_DB) as db:
            db.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, salt TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL, created_by TEXT NOT NULL)")
        with _dashboard_db(DASHBOARD_AUDIT_DB) as db:
            db.execute("CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL, path TEXT NOT NULL, method TEXT NOT NULL, status INTEGER NOT NULL, source_ip TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_activity_created ON activity(id DESC)")
        for path in (DASHBOARD_AUTH_DB, DASHBOARD_AUDIT_DB):
            try: os.chmod(path, 0o600)
            except OSError: pass


def _dashboard_hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 310000).hex()
    return digest, salt


def _dashboard_user_role(username):
    # The dashboard administrator is named marwan. Their password is checked
    # against Linux PAM at login, but all sessions and roles remain dashboard-side.
    if str(username or "").casefold() == "marwan":
        return "admin"
    _dashboard_init_db()
    with _dashboard_db(DASHBOARD_AUTH_DB) as db:
        row = db.execute("SELECT role FROM users WHERE username=?", (username,)).fetchone()
    return str(row["role"]) if row else "user"


def _dashboard_verify_user(username, password):
    if (not re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", username)
            or not password):
        return False
    _dashboard_init_db()
    with _dashboard_db(DASHBOARD_AUTH_DB) as db:
        row = db.execute("SELECT password_hash,salt FROM users WHERE username=?", (username,)).fetchone()
    if not row: return False
    actual, _ = _dashboard_hash_password(password, row["salt"])
    return secrets.compare_digest(actual, str(row["password_hash"]))


def _audit_record(username, action, detail, path="", method="", status=200):
    try:
        _dashboard_init_db()
        detail = re.sub(r"(?i)(password|token|secret|authorization)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", str(detail or ""))[:500]
        with _dashboard_db(DASHBOARD_AUDIT_DB) as db:
            db.execute("INSERT INTO activity(created_at,username,action,detail,path,method,status,source_ip) VALUES(?,?,?,?,?,?,?,?)",
                       (time.strftime("%Y-%m-%dT%H:%M:%S%z"), str(username or "unknown")[:32], str(action)[:100], detail,
                        str(path)[:250], str(method)[:10], int(status or 0), str(request.remote_addr or "")[:64]))
    except Exception as exc:
        app.logger.warning("Audit log write failed: %s", str(exc)[:200])


def _require_admin():
    username = session.get("username")
    # Re-read the dashboard database on every privileged request, so a stale
    # session role can never preserve admin access after the DB role changes.
    current_role = _dashboard_user_role(username) if session.get("authenticated") else "user"
    if not session.get("authenticated") or not _is_dashboard_admin(username, current_role):
        session["role"] = current_role
        session["is_admin"] = False
        return jsonify({"ok": False, "error": "Administrator access required."}), 403
    session["role"] = current_role
    session["is_admin"] = True
    return None


@app.after_request
def audit_meaningful_actions(response):
    try:
        if session.get("authenticated"):
            path = request.path
            method = request.method.upper()
            excluded = ("/api/stats", "/api/remote/input", "/api/remote/mouse", "/api/remote/keyboard",
                        "/api/ollama/models", "/api/sharing/thumbnail/", "/api/sharing/preview/", "/api/sharing/files/")
            if not path.startswith(excluded) and not path.startswith("/static/"):
                meaningful = method in {"POST", "PUT", "PATCH", "DELETE"} or path in {"/", "/api/audit/log", "/api/packages"}
                if path == "/api/agent/run" and method == "POST": meaningful = True
                if meaningful and not path.startswith("/api/audit/log"):
                    detail = "Dashboard action" if method in {"POST", "PUT", "PATCH", "DELETE"} else "Viewed dashboard section"
                    _audit_record(session.get("username"), f"{method} {path}", detail, path, method, response.status_code)
    except Exception:
        pass
    return response


@app.route("/api/profile", methods=["GET", "POST"])
def dashboard_profile():
    """Read or update the current dashboard user's display name and avatar."""
    if not session.get("authenticated"):
        return jsonify({"ok": False, "error": "Sign in required."}), 401
    username = str(session.get("username", "user"))
    with _DASHBOARD_PROFILE_LOCK:
        try:
            all_profiles = json.loads(DASHBOARD_PROFILE_FILE.read_text(encoding="utf-8")) if DASHBOARD_PROFILE_FILE.exists() else {}
            if not isinstance(all_profiles, dict): all_profiles = {}
        except (OSError, ValueError):
            all_profiles = {}
        profile = all_profiles.get(username, {}) if isinstance(all_profiles.get(username, {}), dict) else {}
        if request.method == "GET":
            return jsonify({"ok": True, "username": username,
                            "display_name": str(profile.get("display_name") or username)[:32],
                            "avatar": profile.get("avatar") if isinstance(profile.get("avatar"), str) else ""})
        payload = request.get_json(silent=True) or {}
        display_name = str(payload.get("display_name", username)).strip()
        avatar = payload.get("avatar", "")
        if not display_name or len(display_name) > 32 or any(ord(ch) < 32 for ch in display_name):
            return jsonify({"ok": False, "error": "Display name must be 1–32 printable characters."}), 400
        if not isinstance(avatar, str):
            return jsonify({"ok": False, "error": "Invalid avatar data."}), 400
        if avatar and (len(avatar) > 1_800_000 or not re.fullmatch(r"data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+", avatar)):
            return jsonify({"ok": False, "error": "The optimized profile picture was unexpectedly large. Try another image."}), 400
        all_profiles[username] = {"display_name": display_name, "avatar": avatar}
        DASHBOARD_PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DASHBOARD_PROFILE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(all_profiles, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(DASHBOARD_PROFILE_FILE)
    _audit_record(username, "profile.update", "Updated profile name/avatar", request.path, request.method, 200)
    return jsonify({"ok": True, "username": username, "display_name": display_name, "avatar": avatar})


@app.route("/api/admin/users", methods=["GET", "POST"])
def dashboard_admin_users():
    denied = _require_admin()
    if denied: return denied
    _dashboard_init_db()
    if request.method == "GET":
        with _dashboard_db(DASHBOARD_AUTH_DB) as db:
            rows = db.execute("SELECT username,role,created_at,created_by FROM users WHERE lower(username) != 'marwan' ORDER BY username COLLATE NOCASE").fetchall()
        return jsonify({"ok": True, "admin": "marwan", "users": [dict(r) for r in rows]})
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", username):
        return jsonify({"ok": False, "error": "Username must be 2–32 characters using letters, numbers, dot, underscore, or hyphen."}), 400
    if username.casefold() == "marwan":
        return jsonify({"ok": False, "error": "The username marwan is reserved for the dashboard administrator."}), 400
    if len(password) < 8 or len(password) > 256:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters."}), 400
    password_hash, salt = _dashboard_hash_password(password)
    try:
        with _dashboard_db(DASHBOARD_AUTH_DB) as db:
            db.execute("INSERT INTO users(username,password_hash,salt,role,created_at,created_by) VALUES(?,?,?,?,?,?)",
                       (username, password_hash, salt, "user", time.strftime("%Y-%m-%dT%H:%M:%S%z"), session.get("username", DASHBOARD_USER)))
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "That dashboard username already exists."}), 409
    _audit_record(session.get("username"), "user.create", f"Created dashboard user {username}", request.path, request.method, 201)
    return jsonify({"ok": True, "username": username}), 201


@app.route("/api/admin/users/<username>", methods=["DELETE"])
def dashboard_admin_delete_user(username):
    denied = _require_admin()
    if denied: return denied
    if username.casefold() == "marwan":
        return jsonify({"ok": False, "error": "The dashboard administrator account cannot be deleted."}), 400
    with _dashboard_db(DASHBOARD_AUTH_DB) as db:
        cur = db.execute("DELETE FROM users WHERE username=?", (username,))
    if not cur.rowcount: return jsonify({"ok": False, "error": "User not found."}), 404
    _audit_record(session.get("username"), "user.delete", f"Deleted dashboard user {username}", request.path, request.method, 200)
    return jsonify({"ok": True})


@app.route("/api/admin/restart", methods=["POST"])
def dashboard_restart():
    denied = _require_admin()
    if denied: return denied
    # Never collect or transmit the Linux password through a LAN web page.
    ok, message = _open_local_auth_terminal("dashboard", "restart")
    if not ok:
        app.logger.error("Dashboard restart terminal failed: %s", message)
        return jsonify({"ok": False, "error": message}), 500
    _audit_record(session.get("username"), "dashboard.restart", "Opened host terminal to run dashboard-stop and dashboard-start", request.path, request.method, 202)
    return jsonify({"ok": True, "message": "Two terminals will open on the PC: one runs dashboard-stop and the other runs dashboard-start after a short delay. Enter any authorization prompts in those local terminals."}), 202


@app.route("/api/audit/log", methods=["GET"])
def dashboard_audit_log():
    denied = _require_admin()
    if denied: return denied
    _dashboard_init_db()
    try: limit = max(1, min(300, int(request.args.get("limit", "100"))))
    except ValueError: limit = 100
    with _dashboard_db(DASHBOARD_AUDIT_DB) as db:
        rows = db.execute("SELECT id,created_at,username,action,detail,path,method,status,source_ip FROM activity ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify({"ok": True, "entries": [dict(r) for r in rows]})

_SHARING_INIT_LOCK = threading.Lock()
_SHARING_INITIALIZED = False


def _sharing_connect():
    SHARING_DIR.mkdir(parents=True, exist_ok=True)
    SHARING_FILES_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(SHARING_DB), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


@contextmanager
def _sharing_db():
    db = _sharing_connect()
    try:
        with db:
            yield db
    finally:
        db.close()


def _sharing_init():
    global _SHARING_INITIALIZED
    if _SHARING_INITIALIZED:
        return
    with _SHARING_INIT_LOCK:
        if _SHARING_INITIALIZED:
            return
        with _sharing_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS sharing_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                attachments_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                source_ip TEXT NOT NULL DEFAULT ''
            )""")
            message_columns = {row["name"] for row in db.execute("PRAGMA table_info(sharing_messages)").fetchall()}
            if "channel_id" not in message_columns:
                db.execute("ALTER TABLE sharing_messages ADD COLUMN channel_id TEXT NOT NULL DEFAULT 'general'")
            db.execute("UPDATE sharing_messages SET channel_id='general' WHERE channel_id IS NULL OR channel_id=''")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sharing_messages_channel_created ON sharing_messages(channel_id, id DESC)")
            db.execute("""CREATE TABLE IF NOT EXISTS sharing_channels (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL, created_by TEXT NOT NULL
            )""")
            db.execute("INSERT OR IGNORE INTO sharing_channels(id,name,created_at,created_by) VALUES('general','general',?,?)",
                       (time.strftime("%Y-%m-%dT%H:%M:%S%z"), "marwan"))
            db.execute("""CREATE TABLE IF NOT EXISTS sharing_attachments (
                id TEXT PRIMARY KEY, message_id INTEGER NOT NULL, name TEXT NOT NULL,
                disk_name TEXT NOT NULL, size INTEGER NOT NULL, mimetype TEXT NOT NULL,
                FOREIGN KEY(message_id) REFERENCES sharing_messages(id) ON DELETE CASCADE
            )""")
        try:
            os.chmod(SHARING_DIR, 0o700)
            os.chmod(SHARING_FILES_DIR, 0o700)
            if SHARING_DB.exists(): os.chmod(SHARING_DB, 0o600)
        except OSError:
            pass
        _SHARING_INITIALIZED = True


def _sharing_is_local_request():
    raw = (request.remote_addr or "").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(raw)
        if ip.is_loopback:
            return True
    except ValueError:
        return False
    local_ips = {"127.0.0.1", "::1"}
    try:
        for entries in psutil.net_if_addrs().values():
            for entry in entries:
                address = (entry.address or "").split("%", 1)[0]
                try: ipaddress.ip_address(address); local_ips.add(address)
                except ValueError: pass
    except Exception:
        pass
    return raw in local_ips


def _sharing_public_message(row):
    try: attachments = json.loads(row["attachments_json"] or "[]")
    except Exception: attachments = []
    return {"id": row["id"], "sender": row["sender"], "text": row["text"],
            "channel_id": row["channel_id"], "attachments": attachments, "created_at": row["created_at"]}


def _sharing_message_page(channel_id="general", before=None, after=None, limit=10):
    _sharing_init()
    limit = max(1, min(10, int(limit or 10)))
    with _sharing_db() as db:
        exists = db.execute("SELECT 1 FROM sharing_channels WHERE id=?", (channel_id,)).fetchone()
        if not exists:
            return {"messages": [], "has_older": False, "has_newer": False, "first_id": None, "last_id": None}
        if before is not None:
            rows = db.execute("SELECT * FROM sharing_messages WHERE channel_id=? AND id < ? ORDER BY id DESC LIMIT ?", (channel_id, before, limit)).fetchall()
            rows = list(reversed(rows))
        elif after is not None:
            rows = db.execute("SELECT * FROM sharing_messages WHERE channel_id=? AND id > ? ORDER BY id ASC LIMIT ?", (channel_id, after, limit)).fetchall()
        else:
            rows = list(reversed(db.execute("SELECT * FROM sharing_messages WHERE channel_id=? ORDER BY id DESC LIMIT ?", (channel_id, limit)).fetchall()))
        if rows:
            first_id, last_id = rows[0]["id"], rows[-1]["id"]
            has_older = db.execute("SELECT 1 FROM sharing_messages WHERE channel_id=? AND id < ? LIMIT 1", (channel_id, first_id)).fetchone() is not None
            has_newer = db.execute("SELECT 1 FROM sharing_messages WHERE channel_id=? AND id > ? LIMIT 1", (channel_id, last_id)).fetchone() is not None
        else:
            has_older = has_newer = False
    return {"messages": [_sharing_public_message(row) for row in rows],
            "has_older": has_older, "has_newer": has_newer,
            "first_id": rows[0]["id"] if rows else None,
            "last_id": rows[-1]["id"] if rows else None}


@app.route("/api/sharing/channels", methods=["GET", "POST"])
def sharing_channels():
    _sharing_init()
    if request.method == "GET":
        with _sharing_db() as db:
            rows = db.execute("SELECT id,name,created_at,created_by FROM sharing_channels ORDER BY CASE WHEN id='general' THEN 0 ELSE 1 END, name COLLATE NOCASE").fetchall()
        return jsonify({"ok": True, "channels": [dict(row) for row in rows]})
    denied = _require_admin()
    if denied: return denied
    payload = request.get_json(silent=True) or {}
    name = re.sub(r"\s+", "-", str(payload.get("name", "")).strip()).strip("-_")
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", name):
        return jsonify({"ok": False, "error": "Channel names must be 2–40 characters using letters, numbers, hyphens, or underscores."}), 400
    channel_id = "ch_" + secrets.token_hex(8)
    try:
        with _sharing_db() as db:
            db.execute("INSERT INTO sharing_channels(id,name,created_at,created_by) VALUES(?,?,?,?)",
                       (channel_id, name, time.strftime("%Y-%m-%dT%H:%M:%S%z"), session.get("username", "marwan")))
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "A channel with that name already exists."}), 409
    _audit_record(session.get("username"), "sharing.channel.create", f"Created channel #{name}", request.path, request.method, 201)
    return jsonify({"ok": True, "channel": {"id": channel_id, "name": name}}), 201


@app.route("/api/sharing/channels/<channel_id>", methods=["DELETE", "PATCH"])
def sharing_delete_channel(channel_id):
    denied = _require_admin()
    if denied: return denied
    _sharing_init()
    if request.method == "PATCH":
        if channel_id == "general":
            return jsonify({"ok": False, "error": "The #general channel cannot be renamed."}), 400
        payload = request.get_json(silent=True) or {}
        name = re.sub(r"\s+", "-", str(payload.get("name", "")).strip()).strip("-_")
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", name):
            return jsonify({"ok": False, "error": "Channel names must be 2–40 characters using letters, numbers, hyphens, or underscores."}), 400
        try:
            with _sharing_db() as db:
                current = db.execute("SELECT name FROM sharing_channels WHERE id=?", (channel_id,)).fetchone()
                if not current:
                    return jsonify({"ok": False, "error": "Channel not found."}), 404
                db.execute("UPDATE sharing_channels SET name=? WHERE id=?", (name, channel_id))
            _audit_record(session.get("username"), "sharing.channel.rename", f"Renamed channel #{current['name']} to #{name}", request.path, request.method, 200)
            return jsonify({"ok": True, "channel": {"id": channel_id, "name": name}})
        except sqlite3.IntegrityError:
            return jsonify({"ok": False, "error": "A channel with that name already exists."}), 409
        except Exception as exc:
            app.logger.warning("Channel rename failed: %s", str(exc)[:200])
            return jsonify({"ok": False, "error": "Could not rename channel."}), 500
    if channel_id == "general":
        return jsonify({"ok": False, "error": "The #general channel cannot be deleted."}), 400
    try:
        with _sharing_db() as db:
            channel = db.execute("SELECT name FROM sharing_channels WHERE id=?", (channel_id,)).fetchone()
            if not channel:
                return jsonify({"ok": False, "error": "Channel not found."}), 404
            message_rows = db.execute("SELECT id FROM sharing_messages WHERE channel_id=?", (channel_id,)).fetchall()
            message_ids = [int(row["id"]) for row in message_rows]
            file_rows = []
            if message_ids:
                marks = ",".join("?" for _ in message_ids)
                file_rows = db.execute(f"SELECT disk_name FROM sharing_attachments WHERE message_id IN ({marks})", message_ids).fetchall()
                db.execute(f"DELETE FROM sharing_attachments WHERE message_id IN ({marks})", message_ids)
                db.execute(f"DELETE FROM sharing_messages WHERE id IN ({marks})", message_ids)
            db.execute("DELETE FROM sharing_channels WHERE id=?", (channel_id,))
        for row in file_rows:
            try: (SHARING_FILES_DIR / row["disk_name"]).unlink(missing_ok=True)
            except OSError: pass
        _audit_record(session.get("username"), "sharing.channel.delete", f"Deleted channel #{channel['name']} and its messages", request.path, request.method, 200)
        return jsonify({"ok": True})
    except Exception as exc:
        app.logger.warning("Channel deletion failed: %s", str(exc)[:200])
        return jsonify({"ok": False, "error": "Could not delete channel."}), 500


# Dashboard presence is a short-lived heartbeat. A member is online only while
# at least one authenticated browser session has refreshed its heartbeat recently.
_SHARING_PRESENCE = {}
_SHARING_PRESENCE_LOCK = threading.RLock()
_SHARING_PRESENCE_TTL = 18.0

@app.route("/api/sharing/presence", methods=["POST"])
def sharing_presence_heartbeat():
    username = str(session.get("username") or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Authentication required"}), 401
    now = time.monotonic()
    with _SHARING_PRESENCE_LOCK:
        _SHARING_PRESENCE[username.casefold()] = {"username": username, "last_seen": now}
        stale = [key for key, value in _SHARING_PRESENCE.items() if now - value.get("last_seen", 0) > _SHARING_PRESENCE_TTL]
        for key in stale:
            _SHARING_PRESENCE.pop(key, None)
    return jsonify({"ok": True, "online": True})

@app.route("/api/sharing/members", methods=["GET"])
def sharing_members():
    _dashboard_init_db()
    with _dashboard_db(DASHBOARD_AUTH_DB) as db:
        rows = db.execute("SELECT username,role FROM users ORDER BY username COLLATE NOCASE").fetchall()
    try:
        profile_data = json.loads(DASHBOARD_PROFILE_FILE.read_text(encoding="utf-8")) if DASHBOARD_PROFILE_FILE.exists() else {}
        if not isinstance(profile_data, dict): profile_data = {}
    except (OSError, ValueError):
        profile_data = {}
    def public_member(username, role):
        profile = profile_data.get(username, {}) if isinstance(profile_data.get(username, {}), dict) else {}
        now = time.monotonic()
        with _SHARING_PRESENCE_LOCK:
            presence = _SHARING_PRESENCE.get(username.casefold())
            online = bool(presence and now - presence.get("last_seen", 0) <= _SHARING_PRESENCE_TTL)
        return {"username": username, "role": role,
                "display_name": str(profile.get("display_name") or username)[:32],
                "avatar": profile.get("avatar") if isinstance(profile.get("avatar"), str) else "",
                "online": online}
    members = [public_member("marwan", "admin")]
    members.extend(public_member(row["username"], row["role"] or "user") for row in rows if row["username"].casefold() != "marwan")
    return jsonify({"ok": True, "members": members})


@app.route("/api/sharing/messages", methods=["GET", "POST"])
def sharing_messages():
    _sharing_init()
    if request.method == "GET":
        try:
            before_raw, after_raw = request.args.get("before"), request.args.get("after")
            channel_id = str(request.args.get("channel", "general"))[:64]
            before = int(before_raw) if before_raw else None
            after = int(after_raw) if after_raw else None
            limit = int(request.args.get("limit", "10"))
            if limit < 1 or limit > 10: raise ValueError
            if before is not None and before < 1: raise ValueError
            if after is not None and after < 0: raise ValueError
            if before is not None and after is not None: raise ValueError
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid message cursor."}), 400
        page = _sharing_message_page(channel_id=channel_id, before=before, after=after, limit=limit)
        if request.args.get("sync") == "1":
            # Lightweight ID index lets clients notice deletions even in older loaded pages.
            with _sharing_db() as db:
                page["message_ids"] = [int(row["id"]) for row in db.execute(
                    "SELECT id FROM sharing_messages WHERE channel_id=? ORDER BY id", (channel_id,)
                ).fetchall()]
        return jsonify({"ok": True, **page})

    channel_id = str(request.form.get("channel_id", "general"))[:64]
    with _sharing_db() as db:
        channel_exists = db.execute("SELECT 1 FROM sharing_channels WHERE id=?", (channel_id,)).fetchone()
    if not channel_exists:
        return jsonify({"ok": False, "error": "That channel no longer exists."}), 404
    text_value = request.form.get("text", "")
    uploads = [item for item in request.files.getlist("files") if item and item.filename]
    if not text_value and not uploads:
        return jsonify({"ok": False, "error": "Write a message or attach at least one file."}), 400

    # Authentication middleware protects this endpoint; the message author is
    # the signed-in dashboard identity, never the Linux service process user.
    sender = str(session.get("username") or "Unknown user")[:32]
    saved = []
    try:
        for upload in uploads:
            original = str(upload.filename or "file").replace("\\", "/").split("/")[-1].strip() or "file"
            file_id = secrets.token_urlsafe(24)
            disk_name = secrets.token_hex(20) + ".upload"
            disk_path = SHARING_FILES_DIR / disk_name
            upload.save(str(disk_path))
            try: size = disk_path.stat().st_size
            except OSError: size = 0
            saved.append({"id": file_id, "name": original, "size": size,
                          "mimetype": upload.mimetype or "application/octet-stream", "disk_name": disk_name})
        public_attachments = [{k: v for k, v in item.items() if k != "disk_name"} for item in saved]
        with _sharing_db() as db:
            cursor = db.execute("INSERT INTO sharing_messages(sender,text,attachments_json,created_at,source_ip,channel_id) VALUES(?,?,?,?,?,?)",
                                (sender, text_value, json.dumps(public_attachments, ensure_ascii=False),
                                 time.strftime("%Y-%m-%dT%H:%M:%S%z"), request.remote_addr or "", channel_id))
            message_id = cursor.lastrowid
            # The download route uses the private disk_name mapping stored in a side table.
            for item in saved:
                db.execute("INSERT INTO sharing_attachments(id,message_id,name,disk_name,size,mimetype) VALUES(?,?,?,?,?,?)",
                           (item["id"], message_id, item["name"], item["disk_name"], item["size"], item["mimetype"]))
            row = db.execute("SELECT * FROM sharing_messages WHERE id=?", (message_id,)).fetchone()
        try: os.chmod(SHARING_DB, 0o600)
        except OSError: pass
        return jsonify({"ok": True, "message": _sharing_public_message(row)})
    except Exception as exc:
        for item in saved:
            try: (SHARING_FILES_DIR / item["disk_name"]).unlink(missing_ok=True)
            except OSError: pass
        app.logger.warning("Sharing message save failed: %s", str(exc)[:250])
        return jsonify({"ok": False, "error": f"Could not save message: {exc}"}), 500


@app.route("/api/sharing/files", methods=["GET"])
def sharing_file_index():
    """Return every file shared in the room, newest first."""
    _sharing_init()
    with _sharing_db() as db:
        rows = db.execute(
            "SELECT a.id, a.message_id, a.name, a.size, a.mimetype, m.sender, m.created_at "
            "FROM sharing_attachments a JOIN sharing_messages m ON m.id=a.message_id "
            "ORDER BY m.id DESC, a.rowid DESC"
        ).fetchall()
    return jsonify({"ok": True, "files": [dict(row) for row in rows], "count": len(rows)})


@app.route("/api/sharing/messages/<int:message_id>", methods=["DELETE"])
def sharing_delete_message(message_id):
    """Delete one shared message and all of its stored attachments."""
    _sharing_init()
    try:
        with _sharing_db() as db:
            row = db.execute("SELECT id,sender FROM sharing_messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                return jsonify({"ok": False, "error": "Message not found; it may already have been deleted."}), 404
            current_username = str(session.get("username") or "")
            current_role = _dashboard_user_role(current_username) if session.get("authenticated") else "user"
            is_admin = _is_dashboard_admin(current_username, current_role)
            if not is_admin and str(row["sender"] or "").casefold() != current_username.casefold():
                return jsonify({"ok": False, "error": "You can only delete your own messages."}), 403
            files = db.execute("SELECT disk_name FROM sharing_attachments WHERE message_id=?", (message_id,)).fetchall()
            db.execute("DELETE FROM sharing_attachments WHERE message_id=?", (message_id,))
            db.execute("DELETE FROM sharing_messages WHERE id=?", (message_id,))
        for item in files:
            try:
                (SHARING_FILES_DIR / item["disk_name"]).unlink(missing_ok=True)
            except OSError as exc:
                app.logger.warning("Could not remove shared attachment bytes for message %s: %s", message_id, exc)
        return jsonify({"ok": True, "deleted_id": message_id, "deleted_files": len(files)})
    except Exception as exc:
        app.logger.exception("Sharing message deletion failed")
        return jsonify({"ok": False, "error": f"Could not delete message: {str(exc)[:300]}"}), 500


@app.route("/api/sharing/files/<file_id>", methods=["GET"])
def sharing_download(file_id):
    _sharing_init()
    with _sharing_db() as db:
        row = db.execute("SELECT name,disk_name,mimetype FROM sharing_attachments WHERE id=?", (file_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Attachment not found."}), 404
    path = SHARING_FILES_DIR / row["disk_name"]
    if not path.is_file():
        return jsonify({"ok": False, "error": "Attachment file is missing from disk."}), 404
    return send_file(path, as_attachment=True, download_name=row["name"], mimetype=row["mimetype"] or "application/octet-stream")


@app.route("/api/sharing/preview/<file_id>", methods=["GET"])
def sharing_preview(file_id):
    """Inline media preview endpoint; browsers can request video byte ranges."""
    _sharing_init()
    with _sharing_db() as db:
        row = db.execute("SELECT name,disk_name,mimetype FROM sharing_attachments WHERE id=?", (file_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Attachment not found."}), 404
    path = SHARING_FILES_DIR / row["disk_name"]
    if not path.is_file():
        return jsonify({"ok": False, "error": "Attachment file is missing from disk."}), 404
    mimetype = row["mimetype"] or mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
    if not (mimetype.startswith("image/") or mimetype.startswith("video/")):
        return jsonify({"ok": False, "error": "Only images and videos can be previewed."}), 415
    return send_file(path, as_attachment=False, download_name=row["name"], mimetype=mimetype, conditional=True, etag=True)


@app.route("/api/sharing/thumbnail/<file_id>", methods=["GET"])
def sharing_thumbnail(file_id):
    """Generate/cache small JPEG thumbnails for image and video attachments."""
    _sharing_init()
    with _sharing_db() as db:
        row = db.execute("SELECT name,disk_name,mimetype FROM sharing_attachments WHERE id=?", (file_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Attachment not found."}), 404
    path = SHARING_FILES_DIR / row["disk_name"]
    if not path.is_file():
        return jsonify({"ok": False, "error": "Attachment file is missing from disk."}), 404
    mimetype = row["mimetype"] or mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
    if not (mimetype.startswith("image/") or mimetype.startswith("video/")):
        return jsonify({"ok": False, "error": "Not an image or video."}), 415
    thumb_dir = SHARING_DIR / "thumbnails"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb = thumb_dir / (hashlib.sha256(file_id.encode()).hexdigest() + ".jpg")
    if not thumb.is_file():
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            if mimetype.startswith("video/"):
                cmd = [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(path), "-frames:v", "1", "-vf", "scale=240:180:force_original_aspect_ratio=decrease", "-q:v", "9", str(thumb)]
            else:
                cmd = [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(path), "-frames:v", "1", "-vf", "scale=240:180:force_original_aspect_ratio=decrease", "-q:v", "9", str(thumb)]
            try:
                subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            except Exception:
                pass
        if not thumb.is_file():
            # Graceful fallback if ffmpeg is not installed or the file format is unsupported.
            return send_file(path, as_attachment=False, mimetype=mimetype, conditional=True)
        try: os.chmod(thumb, 0o600)
        except OSError: pass
    response = send_file(thumb, mimetype="image/jpeg", conditional=True, max_age=86400)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------------------
# Local PC agent for Ollama. The model can only call this explicit tool list;
# it never receives an arbitrary shell-execution tool.
# ---------------------------------------------------------------------------
_AGENT_PENDING = {}
_AGENT_PENDING_LOCK = threading.Lock()
_AGENT_PENDING_TTL = 300
_AGENT_SYSTEM_PROMPT = (
    "/no_think\nYou are a capable local CachyOS PC agent, not merely a chatbot. You have dedicated desktop tools AND run_shell_command for general local-PC tasks. "
    "If a task is not covered by a dedicated tool, use run_shell_command to investigate or perform it when possible. "
    "Never say 'none of the tools can do that' without first considering the general shell tool. If unsure which command or file is relevant, inspect first using read-only commands (pwd, ls, find, grep, cat, ps, free, df, systemctl status, etc.), then act. "
    "Use tools whenever the user asks you to do something or asks for current system information. Report actual tool output directly, not a generic 'result is shown below'. "
    "Never claim an action succeeded unless a tool result confirms it. Be concise. Never request or print passwords, tokens, or other secrets. Do not invent app IDs, window addresses, or wallpaper paths. "
    "Actions that may change files, packages, services, processes, or system state pause for explicit user approval. Commands run as the dashboard user, not as root; do not attempt privilege escalation. "
    "You cannot see the screen visually; use list_windows for window titles and do not pretend to have visual awareness. If a task genuinely requires visual inspection, privileged access, unavailable hardware, or an external service not configured on this PC, explain the specific limitation after checking what is available. "
    "For multi-step requests, break work into small verified steps and inspect outputs between steps. Never hide errors or invent results."
)

_AGENT_TOOLS = [
    {"type":"function","function":{"name":"get_system_status","description":"Get current CPU, RAM, disk, OS, and uptime information.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"list_apps","description":"List installed launchable desktop apps and their exact IDs.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"open_app","description":"Launch an installed desktop app by its exact ID from list_apps.","parameters":{"type":"object","properties":{"app_id":{"type":"string"}},"required":["app_id"]}}},
    {"type":"function","function":{"name":"list_windows","description":"List open Hyprland windows with title, app class, workspace, and address.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"list_wallpapers","description":"List wallpaper names and exact file paths from the existing Serpantinum wallpaper index.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"list_processes","description":"List current user processes with PID, name, CPU and RAM use.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"focus_window","description":"Focus an existing window by its address returned by list_windows.","parameters":{"type":"object","properties":{"address":{"type":"string"}},"required":["address"]}}},
    {"type":"function","function":{"name":"switch_workspace","description":"Switch to a numbered Hyprland workspace from 1 to 10.","parameters":{"type":"object","properties":{"workspace":{"type":"integer","minimum":1,"maximum":10}},"required":["workspace"]}}},
    {"type":"function","function":{"name":"media_control","description":"Control the active media player.","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["play-pause","play","pause","next","previous"]}},"required":["action"]}}},
    {"type":"function","function":{"name":"press_shortcut","description":"Send a safe, predefined keyboard shortcut to the currently focused app.","parameters":{"type":"object","properties":{"combo":{"type":"string","enum":["logo+d","alt+tab","logo+e","ctrl+l","ctrl+t","ctrl+w","ctrl+shift+esc","ctrl+c","ctrl+v","ctrl+x","ctrl+a","ctrl+s","ctrl+f","ctrl+z","ctrl+y","escape"]}},"required":["combo"]}}},
    {"type":"function","function":{"name":"set_wallpaper","description":"Set a wallpaper using a path returned by the dashboard wallpaper index. Requires confirmation.","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
    {"type":"function","function":{"name":"type_text","description":"Type literal text into the currently focused application. Always requires confirmation; never use to run shell commands.","parameters":{"type":"object","properties":{"text":{"type":"string","maxLength":160}},"required":["text"]}}},
    {"type":"function","function":{"name":"close_window","description":"Close an existing window by its address. Requires confirmation.","parameters":{"type":"object","properties":{"address":{"type":"string"}},"required":["address"]}}},
    {"type":"function","function":{"name":"click_at","description":"Click a screen coordinate with the mouse. Requires confirmation because the model cannot see the screen; only use coordinates explicitly supplied by the user.","parameters":{"type":"object","properties":{"x":{"type":"integer","minimum":0,"maximum":10000},"y":{"type":"integer","minimum":0,"maximum":10000},"button":{"type":"string","enum":["left","right","double_left"]}},"required":["x","y","button"]}}},
    {"type":"function","function":{"name":"terminate_process","description":"Terminate a process by PID. Requires confirmation; never terminate protected system or dashboard processes.","parameters":{"type":"object","properties":{"pid":{"type":"integer","minimum":2}},"required":["pid"]}}},
    {"type":"function","function":{"name":"package_action","description":"Update, reinstall, or uninstall an already-installed official pacman package. Requires confirmation. Does not install new packages.","parameters":{"type":"object","properties":{"package":{"type":"string","maxLength":120},"action":{"type":"string","enum":["update","reinstall","uninstall"]}},"required":["package","action"]}}},
    {"type":"function","function":{"name":"control_ollama_service","description":"Start or stop the Ollama system service. Requires confirmation and may open a local sudo terminal.","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["start","stop"]}},"required":["action"]}}},
    {"type":"function","function":{"name":"reload_serpantinum","description":"Reload Serpantinum's shell. Requires confirmation because it can briefly interrupt the desktop shell.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"run_shell_command","description":"Run a general shell command on the local PC as the dashboard user and return stdout/stderr. Use for tasks not covered by a dedicated tool. Read-only inspection commands can run automatically; commands that may modify files, packages, services, processes, or system state require user approval. Do not use for passwords or claim success without checking output.","parameters":{"type":"object","properties":{"command":{"type":"string","minLength":1,"maxLength":2000}},"required":["command"]}}},
]

_AGENT_SENSITIVE_TOOLS = {"set_wallpaper", "type_text", "close_window", "click_at", "terminate_process", "package_action", "control_ollama_service", "reload_serpantinum"}
_AGENT_SAFE_SHORTCUTS = {
    "logo+d": ["logo", "d"], "alt+tab": ["alt", "tab"], "logo+e": ["logo", "e"],
    "ctrl+l": ["ctrl", "l"], "ctrl+t": ["ctrl", "t"], "ctrl+w": ["ctrl", "w"],
    "ctrl+shift+esc": ["ctrl", "shift", "escape"], "ctrl+c": ["ctrl", "c"],
    "ctrl+v": ["ctrl", "v"], "ctrl+x": ["ctrl", "x"], "ctrl+a": ["ctrl", "a"],
    "ctrl+s": ["ctrl", "s"], "ctrl+f": ["ctrl", "f"], "ctrl+z": ["ctrl", "z"],
    "ctrl+y": ["ctrl", "y"], "escape": ["escape"],
}

def _agent_result(ok, message, **extra):
    return {"ok": bool(ok), "message": str(message)[:1200], **extra}


def _agent_parse_text_tool_call(content):
    """Some small models print a tool name plus JSON instead of returning tool_calls."""
    if not content or len(content) > 5000:
        return None
    allowed = {item["function"]["name"] for item in _AGENT_TOOLS}
    match = re.match(r"^\s*(?:<\|tool_call\|>\s*)?([a-zA-Z_][a-zA-Z0-9_]*)\s*(\{.*\})\s*(?:<\|/tool_call\|>)?\s*$", content, re.S)
    if not match or match.group(1) not in allowed:
        return None
    try:
        args = json.loads(match.group(2))
    except json.JSONDecodeError:
        return None
    if not isinstance(args, dict):
        return None
    return [{"function": {"name": match.group(1), "arguments": args}}]

_AGENT_SHELL_MUTATION_RE = re.compile(
    r"(?:\bsudo\b|\bpacman\b|\bsystemctl\s+(?:start|stop|restart|enable|disable|mask|unmask)\b|"
    r"\b(?:rm|mv|cp|install|mkdir|rmdir|touch|chmod|chown|chgrp|truncate|tee|kill|pkill|killall|"
    r"mount|umount|reboot|shutdown|poweroff|mkfs(?:\.\w+)?|wipefs|fdisk|parted|dd)\b|"
    r"\bsed\s+-i\b|\b(perl|python|python3)\b.*(?:write_text|open\(|unlink|remove\()|"
    r"(?:>>?|\|\s*(?:sh|bash)\b))", re.I
)

def _agent_shell_requires_approval(command):
    """Conservative fast-path classifier; unknown commands still execute only as the user."""
    return bool(_AGENT_SHELL_MUTATION_RE.search(str(command or "")))

def _agent_list_windows():
    ok, out, err = hyprctl_command(["-j", "clients"], timeout=5)
    if not ok:
        raise RuntimeError(err or out or "Could not query Hyprland windows")
    try:
        raw = json.loads(out or "[]")
    except json.JSONDecodeError:
        raise RuntimeError("Hyprland returned invalid window data")
    windows = []
    for win in raw[:60]:
        windows.append({
            "address": str(win.get("address", "")),
            "title": str(win.get("title", ""))[:180],
            "app": str(win.get("class", "") or win.get("initialClass", ""))[:100],
            "workspace": (win.get("workspace") or {}).get("name", "?"),
            "focused": bool(win.get("focusHistoryID") == 0),
        })
    return windows

def _agent_execute_tool(name, args):
    """Execute a desktop/system tool with validation and bounded output."""
    args = args if isinstance(args, dict) else {}
    if name == "run_shell_command":
        command = str(args.get("command", "")).strip()
        if not command or len(command) > 2000 or "\x00" in command:
            raise ValueError("Enter a shell command between 1 and 2000 characters.")
        try:
            home = Path(pwd.getpwnam(DASHBOARD_USER).pw_dir)
        except Exception:
            home = Path.home()
        try:
            result = subprocess.run(
                ["bash", "-lc", command], cwd=str(home), env=graphical_environment(),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
                errors="replace", start_new_session=True,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            err = exc.stderr or ""
            if isinstance(out, bytes): out = out.decode("utf-8", "replace")
            if isinstance(err, bytes): err = err.decode("utf-8", "replace")
            return _agent_result(False, "Command timed out after 30 seconds.", command=command,
                                 exit_code=124, stdout=out[-5000:], stderr=err[-2500:])
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Could not run command: {exc}")
        out, err = result.stdout[-5000:], result.stderr[-2500:]
        ok = result.returncode == 0
        summary = "Command completed successfully." if ok else f"Command exited with status {result.returncode}."
        if err.strip(): summary += " Stderr is included in the result."
        return _agent_result(ok, summary, command=command, exit_code=result.returncode,
                             stdout=out, stderr=err)
    if name == "get_system_status":
        vm = psutil.virtual_memory()
        try: user_home = Path(pwd.getpwnam(DASHBOARD_USER).pw_dir)
        except Exception: user_home = Path.home()
        disk = psutil.disk_usage(str(user_home))
        return _agent_result(True, "System status collected.", data={
            "host": socket.gethostname(), "os": platform.platform(),
            "cpu_percent": round(psutil.cpu_percent(interval=0.15), 1),
            "ram_percent": round(vm.percent, 1), "ram_used_gb": round(vm.used/1024**3, 2),
            "ram_total_gb": round(vm.total/1024**3, 2), "disk_percent": round(disk.percent, 1),
            "disk_free_gb": round(disk.free/1024**3, 2),
            "uptime_minutes": round(max(0, time.time()-psutil.boot_time())/60),
        })
    if name == "list_apps":
        apps = discover_desktop_apps()
        return _agent_result(True, f"Found {len(apps)} launchable desktop apps.", apps=[{"id": x["id"], "name": x["name"]} for x in apps[:100]])
    if name == "open_app":
        app_id = str(args.get("app_id", ""))[:255]
        entry = next((x for x in discover_desktop_apps() if x["id"] == app_id), None)
        if not entry:
            raise ValueError("That app ID was not found. Call list_apps and choose an exact ID.")
        if not shutil.which("gio"):
            raise RuntimeError("gio is not installed")
        env = graphical_environment()
        cmd = ["gio", "launch", entry["desktop"]]
        if os.geteuid() == 0:
            account = pwd.getpwnam(DASHBOARD_USER)
            env["HOME"] = account.pw_dir
            env["USER"] = DASHBOARD_USER
            env["LOGNAME"] = DASHBOARD_USER
            env["PATH"] = str(Path(account.pw_dir) / ".local/bin") + ":" + env.get("PATH", "/usr/bin:/bin")
            cmd = ["runuser", "-u", DASHBOARD_USER, "--"] + cmd
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        return _agent_result(True, f"Launched {entry['name']}.", app={"id": entry["id"], "name": entry["name"]})
    if name == "list_windows":
        windows = _agent_list_windows()
        return _agent_result(True, f"Found {len(windows)} open windows.", windows=windows)
    if name == "list_wallpapers":
        data, _index_path = _wallpaper_index()
        items = []
        for item in data.get("items", []):
            if not isinstance(item, dict): continue
            raw = item.get("filePath") or ""
            safe = _safe_wallpaper_path(raw)
            if safe and safe.is_file(): items.append({"name":str(item.get("name") or safe.name)[:160], "path":str(safe)})
        return _agent_result(True, f"Found {len(items)} indexed wallpapers.", wallpapers=items[:80])
    if name == "list_processes":
        found = []
        for proc in psutil.process_iter(["pid", "name", "username"]):
            try:
                if proc.info.get("username") != DASHBOARD_USER: continue
                proc.cpu_percent(None)
                found.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied): continue
        time.sleep(0.1)
        rows = []
        cpu_count = max(psutil.cpu_count() or 1, 1)
        for proc in found:
            try:
                info = proc.as_dict(attrs=["pid", "name", "username", "memory_percent"])
                rows.append({"pid":int(info["pid"]), "name":str(info.get("name") or "unknown")[:100],
                             "cpu_percent":round(min(100.0, max(0.0, float(proc.cpu_percent(None) or 0.0))/cpu_count), 1),
                             "memory_percent":round(float(info.get("memory_percent") or 0),2)})
            except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError): continue
        rows.sort(key=lambda x:(x["cpu_percent"],x["memory_percent"]), reverse=True)
        return _agent_result(True, f"Listed {min(len(rows),30)} processes for the dashboard user.", processes=rows[:30])
    if name == "focus_window":
        address = str(args.get("address", ""))[:80]
        windows = _agent_list_windows()
        if not address or not any(w["address"] == address for w in windows):
            raise ValueError("Window address is not currently open. Call list_windows first.")
        ok, out, err = hyprctl_command(["dispatch", "focuswindow", "address:" + address], timeout=5)
        if not ok: raise RuntimeError(err or out or "Could not focus window")
        return _agent_result(True, "Focused the requested window.")
    if name == "switch_workspace":
        try: workspace = int(args.get("workspace"))
        except (TypeError, ValueError): raise ValueError("Workspace must be a number from 1 to 10")
        if workspace < 1 or workspace > 10: raise ValueError("Workspace must be between 1 and 10")
        ok, out, err = hyprctl_command(["dispatch", "workspace", str(workspace)], timeout=5)
        if not ok: raise RuntimeError(err or out or "Could not switch workspace")
        return _agent_result(True, f"Switched to workspace {workspace}.")
    if name == "media_control":
        action = str(args.get("action", ""))
        if action not in {"play-pause", "play", "pause", "next", "previous"}: raise ValueError("Unsupported media action")
        player_action = {"play-pause":"play-pause", "play":"play", "pause":"pause", "next":"next", "previous":"previous"}[action]
        if not shutil.which("playerctl"): raise RuntimeError("playerctl is not installed")
        ok, out, err = run_command(["playerctl", player_action], timeout=5, env=graphical_environment())
        if not ok: raise RuntimeError(err or out or "No controllable media player was found")
        return _agent_result(True, f"Media action sent: {action}.")
    if name == "press_shortcut":
        combo = str(args.get("combo", ""))
        parts = _AGENT_SAFE_SHORTCUTS.get(combo)
        if not parts: raise ValueError("Shortcut is not in the safe allowlist")
        if not shutil.which("wtype"): raise RuntimeError("wtype is not installed")
        keymap = {"escape":"Escape", "tab":"Tab"}
        mods = parts[:-1] if len(parts) > 1 else []
        key = keymap.get(parts[-1], parts[-1])
        cmd = ["wtype"]
        for mod in mods: cmd += ["-M", "win" if mod == "logo" else mod]
        cmd += ["-k", key]
        for mod in reversed(mods): cmd += ["-m", "win" if mod == "logo" else mod]
        ok, out, err = run_command(cmd, timeout=5, env=graphical_environment())
        if not ok: raise RuntimeError(err or out or "Shortcut failed")
        return _agent_result(True, f"Sent shortcut {combo} to the focused app.")
    if name == "set_wallpaper":
        raw = str(args.get("path", ""))[:2048]
        safe = _safe_wallpaper_path(raw)
        if not safe or not safe.is_file(): raise ValueError("Choose an existing wallpaper path from the dashboard wallpaper gallery.")
        result = wallpaper_apply_internal(safe)
        return _agent_result(True, result)
    if name == "type_text":
        value = str(args.get("text", ""))
        if not value or len(value) > 160: raise ValueError("Text must contain 1 to 160 characters")
        if "\x00" in value or "\n" in value or "\r" in value: raise ValueError("Multiline text is not allowed by the agent")
        active_ok, active_out, _active_err = hyprctl_command(["-j", "activewindow"], timeout=4)
        if not active_ok:
            raise ValueError("Could not verify the focused window, so typing is blocked for safety.")
        try: active_window = json.loads(active_out or "{}")
        except json.JSONDecodeError: active_window = {}
        active_class = str(active_window.get("class", "") or active_window.get("initialClass", ""))
        if not active_class:
            raise ValueError("Could not identify the focused application, so typing is blocked for safety.")
        if re.search(r"terminal|kitty|foot|alacritty|wezterm|konsole|xterm|tilix|terminator", active_class, re.I):
            raise ValueError("The agent will not type into a terminal window. This prevents accidentally executing shell commands.")
        if not shutil.which("wtype"): raise RuntimeError("wtype is not installed")
        ok, out, err = run_command(["wtype", "--", value], timeout=5, env=graphical_environment())
        if not ok: raise RuntimeError(err or out or "Typing text failed")
        return _agent_result(True, "Typed the approved literal text into the currently focused app.")
    if name == "close_window":
        address = str(args.get("address", ""))[:80]
        if not address or not any(w["address"] == address for w in _agent_list_windows()):
            raise ValueError("Window address is not currently open. Call list_windows first.")
        ok, out, err = hyprctl_command(["dispatch", "closewindow", "address:" + address], timeout=5)
        if not ok: raise RuntimeError(err or out or "Could not close window")
        return _agent_result(True, "Close request sent to the selected window.")
    if name == "click_at":
        try: x, y = int(args.get("x")), int(args.get("y"))
        except (TypeError, ValueError): raise ValueError("Screen coordinates must be integers")
        button = str(args.get("button", "left"))
        if not (0 <= x <= 10000 and 0 <= y <= 10000) or button not in {"left", "right", "double_left"}:
            raise ValueError("Invalid mouse coordinates or button")
        if not shutil.which("ydotool"): raise RuntimeError("ydotool is not installed; mouse click action unavailable")
        ok, out, err = run_command(["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)], timeout=4, env=ydotool_environment())
        if not ok: raise RuntimeError(err or out or "Could not move pointer to the requested coordinate")
        click_code = {"left":"0xC0", "right":"0xC1", "double_left":"0xC0"}[button]
        count = "2" if button == "double_left" else "1"
        ok, out, err = run_command(["ydotool", "click", "-r", count, click_code], timeout=4, env=ydotool_environment())
        if not ok: raise RuntimeError(err or out or "Mouse click failed")
        return _agent_result(True, f"Mouse {button} click sent at ({x}, {y}).")
    if name == "terminate_process":
        try: pid = int(args.get("pid"))
        except (TypeError, ValueError): raise ValueError("PID must be an integer")
        if pid <= 1 or pid == os.getpid(): raise ValueError("Refusing to terminate a protected process")
        try:
            proc = psutil.Process(pid)
            proc_name = proc.name()
            if re.search(r"system-dashboard|serpantinum|quickshell|hyprland", proc_name, re.I):
                raise ValueError("Refusing to terminate a protected dashboard, compositor, or Serpantinum process")
            try: proc_cmdline = " ".join(proc.cmdline())
            except (psutil.AccessDenied, psutil.NoSuchProcess): proc_cmdline = ""
            if str(Path.home() / "system-dashboard" / "app.py") in proc_cmdline or "gunicorn" in proc_cmdline and "app:app" in proc_cmdline:
                raise ValueError("Refusing to terminate the dashboard server")
            proc_user = proc.username()
            if proc_user != DASHBOARD_USER:
                opened, message = _open_local_auth_terminal("process", "terminate", "", pid)
                if opened: return _agent_result(True, f"A local terminal opened to authorize termination of {proc_name} (PID {pid}).", terminal_opened=True)
                raise RuntimeError(message)
            proc.terminate()
            return _agent_result(True, f"Termination requested for {proc_name} (PID {pid}).")
        except psutil.NoSuchProcess: raise ValueError("That process no longer exists")
        except psutil.AccessDenied:
            opened, message = _open_local_auth_terminal("process", "terminate", "", pid)
            if opened: return _agent_result(True, f"A local terminal opened to authorize termination of PID {pid}.", terminal_opened=True)
            raise RuntimeError(message)
    if name == "package_action":
        package = str(args.get("package", "")).strip()
        action = str(args.get("action", ""))
        if not package or not PACKAGE_NAME_RE.fullmatch(package): raise ValueError("Invalid package name")
        if action not in {"update", "reinstall", "uninstall"}: raise ValueError("Unsupported package action")
        pacman = shutil.which("pacman")
        sudo = shutil.which("sudo")
        if not pacman or not sudo: raise RuntimeError("pacman or sudo is unavailable")
        known, known_out, known_err = run_command([pacman, "-Q", package], timeout=5)
        if not known: raise ValueError(f"Package is not installed: {known_err or known_out or package}")
        args_list = ["-Rns", "--noconfirm", package] if action == "uninstall" else ["-S", "--noconfirm", package]
        ok, out, err = run_command([sudo, "-n", pacman, *args_list], timeout=900, env=graphical_environment())
        if not ok:
            detail = (err or out or "Package action failed").strip()
            if any(marker in detail.lower() for marker in ("a password is required", "password is required", "no tty present", "authentication is required", "not allowed to execute")):
                opened, message = _open_local_auth_terminal("package", action, package, None)
                if opened: return _agent_result(True, f"A local terminal opened to authorize package {action}: {package}.", terminal_opened=True)
                raise RuntimeError(detail + ". " + message)
            raise RuntimeError(detail[:800])
        return _agent_result(True, f"Package {action} completed for {package}.", output=out[-1200:])
    if name == "control_ollama_service":
        action = str(args.get("action", ""))
        if action not in {"start", "stop"}: raise ValueError("Only start and stop are allowed")
        systemctl = shutil.which("systemctl")
        sudo = shutil.which("sudo")
        if not systemctl or not sudo: raise RuntimeError("systemctl or sudo is unavailable")
        ok, out, err = run_command([sudo, "-n", systemctl, action, "ollama.service"], timeout=20, env=graphical_environment())
        if not ok:
            opened, message = _open_local_auth_terminal("ollama", action, "", None)
            if opened: return _agent_result(True, "A local terminal opened on the PC. Enter sudo authorization there; the service action will run in that terminal.", terminal_opened=True)
            raise RuntimeError((err or out or "Service control failed") + ". " + message)
        return _agent_result(True, f"Ollama system service {action} completed.")
    if name == "reload_serpantinum":
        if not SERPANTINUM_RELOAD.exists(): raise RuntimeError("Serpantinum reload script was not found")
        ok, out, err = run_command(["bash", str(SERPANTINUM_RELOAD)], timeout=20, env=graphical_environment())
        if not ok: raise RuntimeError(err or out or "Serpantinum reload failed")
        return _agent_result(True, "Serpantinum reload requested.")
    raise ValueError("Unknown or unavailable tool")

def _agent_obvious_tool_request(prompt):
    """Route common simple intents directly so small Ollama models cannot answer instead of acting."""
    q = str(prompt or "").strip()
    low = q.lower()
    if not q:
        return None
    if re.search(r"\b(cpu|ram|memory usage|disk space|system status|system usage|uptime|how much memory|how much ram)\b", low) and re.search(r"\b(show|check|what|tell|report|display|current|usage|status|free|available|how much)\b", low):
        return ("get_system_status", {})
    if re.search(r"\b(list|show|display|what are)\b", low) and re.search(r"\b(open )?(windows|windows open|open windows)\b", low):
        return ("list_windows", {})
    if re.search(r"\b(list|show|display|what are)\b", low) and re.search(r"\b(apps|applications|installed apps|installed applications)\b", low):
        return ("list_apps", {})
    if re.search(r"\b(list|show|display|what are)\b", low) and re.search(r"\b(processes|running processes|tasks running)\b", low):
        return ("list_processes", {})
    if re.search(r"\b(list|show|display)\b", low) and re.search(r"\b(wallpapers|wallpaper list)\b", low):
        return ("list_wallpapers", {})
    m = re.search(r"\b(?:switch|go|move)\s+(?:to\s+)?(?:workspace|desktop)\s*(?:number\s*)?(\d{1,2})\b", low)
    if m and 1 <= int(m.group(1)) <= 10:
        return ("switch_workspace", {"workspace": int(m.group(1))})
    if re.search(r"\b(play|pause|resume|skip|next track|previous track|last track)\b", low) and re.search(r"\b(media|music|song|track|player|play|pause|resume|skip|next|previous)\b", low):
        action = "play-pause"
        if re.search(r"\b(next|skip)\b", low): action = "next"
        elif re.search(r"\b(previous|last track|go back)\b", low): action = "previous"
        elif re.search(r"\bpause\b", low) and not re.search(r"\bplay\b", low): action = "pause"
        elif re.search(r"\b(play|resume)\b", low) and not re.search(r"\bpause\b", low): action = "play"
        return ("media_control", {"action": action})
    m = re.search(r"\b(?:open|launch|start)\s+(?:the\s+)?(.+?)(?:\s+(?:app|application))?[.!?]*$", q, re.I)
    if m:
        target = re.sub(r"\s+(?:please|for me)$", "", m.group(1).strip(), flags=re.I).strip(" .!?\"'")
        if target and len(target) <= 100 and not re.search(r"\b(service|systemctl|command|terminal command)\b", target, re.I):
            return ("__open_app_by_name__", {"target": target})
    return None


def _agent_readable_result(name, result):
    if not result.get("ok"):
        return "That action failed: " + str(result.get("message", "Unknown error"))
    if name == "get_system_status":
        d = result.get("data", {})
        return (f"CPU: {d.get('cpu_percent', 'unknown')}%\nRAM: {d.get('ram_percent', 'unknown')}% ({d.get('ram_used_gb', '?')} / {d.get('ram_total_gb', '?')} GB)\n"
                f"Disk: {d.get('disk_percent', 'unknown')}% used, {d.get('disk_free_gb', '?')} GB free\nUptime: {d.get('uptime_minutes', '?')} minutes\nOS: {d.get('os', 'unknown')}")
    if name == "list_apps":
        return "Available applications:\n" + "\n".join(f"• {x.get('name')} (ID: {x.get('id')})" for x in result.get("apps", []))
    if name == "list_windows":
        return "Open windows:\n" + ("\n".join(f"• {x.get('title') or '(untitled)'} | {x.get('app')} | workspace {x.get('workspace')} | address {x.get('address')}" for x in result.get("windows", [])) or "No windows were returned.")
    if name == "list_processes":
        return "Top processes:\n" + "\n".join(f"• {x.get('name')} (PID {x.get('pid')}) | CPU {x.get('cpu_percent')}% | RAM {x.get('memory_percent')}%" for x in result.get("processes", []))
    if name == "list_wallpapers":
        return "Indexed wallpapers:\n" + "\n".join(f"• {x.get('name')} → {x.get('path')}" for x in result.get("wallpapers", []))
    return str(result.get("message") or ("Completed " + name.replace("_", " ") + "."))


@app.route("/api/agent/run", methods=["POST"])
def local_agent_run():
    payload = request.get_json(silent=True) or {}
    model = str(payload.get("model", "")).strip()
    incoming = payload.get("messages")
    if not model or not isinstance(incoming, list) or not incoming:
        return jsonify({"ok": False, "error": "Choose a model and enter a task."}), 400
    try:
        with urllib.request.urlopen(OLLAMA_BASE + "/api/tags", timeout=4) as resp:
            installed = {str(x.get("name", "")) for x in json.loads(resp.read().decode("utf-8", "replace")).get("models", [])}
        if model not in installed: return jsonify({"ok": False, "error": "That model is not installed or Ollama is unavailable."}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Ollama is unavailable: {exc}"}), 503
    messages = [{"role":"system", "content":_AGENT_SYSTEM_PROMPT}]
    latest_user_prompt = ""
    for item in incoming[-6:]:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}: continue
        content = str(item.get("content", ""))[:1000]
        if content:
            messages.append({"role":item["role"], "content":content})
            if item.get("role") == "user": latest_user_prompt = content
    # For a plainly single-step request, don't spend another full inference just to
    # paraphrase a tool result that the UI already displays below the chat.
    simple_single_step = not re.search(r"\b(and|then|after that|also|before|followed by|as well as)\b", latest_user_prompt, re.I)
    steps = []
    # Fast deterministic routing for common direct requests. This prevents tiny models
    # from merely narrating actions instead of invoking their tools.
    direct = _agent_obvious_tool_request(latest_user_prompt)
    if direct:
        direct_name, direct_args = direct
        try:
            if direct_name == "__open_app_by_name__":
                apps_result = _agent_execute_tool("list_apps", {})
                target = direct_args["target"].casefold()
                matches = apps_result.get("apps", []) if apps_result.get("ok") else []
                match = next((x for x in matches if str(x.get("name", "")).casefold() == target or str(x.get("id", "")).casefold() == target), None)
                if not match:
                    match = next((x for x in matches if target in str(x.get("name", "")).casefold() or target in str(x.get("id", "")).casefold()), None)
                if match:
                    direct_name, direct_args = "open_app", {"app_id": match["id"]}
                else:
                    result = _agent_result(False, f"I couldn't match '{direct_args['target']}' to an installed desktop app. Ask me to list apps to see exact names.")
                    return jsonify({"ok": True, "reply": result["message"], "steps": [{"tool":"list_apps", "result":apps_result}]})
            result = _agent_execute_tool(direct_name, direct_args)
        except Exception as exc:
            result = _agent_result(False, str(exc))
        steps.append({"tool":direct_name, "result":result})
        return jsonify({"ok":True, "reply":_agent_readable_result(direct_name, result), "steps":steps})
    refusal_retry_used = False
    action_retry_used = False
    try:
        for _ in range(3):
            request_body = {"model":model, "messages":messages, "tools":_AGENT_TOOLS, "stream":False,
                            "think":False, "options":{"temperature":0.1, "num_ctx":3072}, "keep_alive":"10m"}
            if action_retry_used:
                # Ask Ollama to return a tool call instead of another text-only answer.
                request_body["tool_choice"] = "required"
            body = json.dumps(request_body).encode()
            req = urllib.request.Request(OLLAMA_BASE + "/api/chat", data=body, headers={"Content-Type":"application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=150) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            msg = data.get("message", {})
            content = str(msg.get("content", ""))[:6000]
            calls = msg.get("tool_calls") or []
            if not calls:
                calls = _agent_parse_text_tool_call(content) or []
            if not calls:
                action_intent = re.search(r"\b(open|launch|start|close|switch|move|change|set|turn|enable|disable|restart|stop|kill|terminate|delete|remove|install|uninstall|update|run|execute|type|click|play|pause|skip|focus|reload|show|list|check|inspect|find|search|create|copy|move|rename|download|upload)\b", latest_user_prompt, re.I)
                if action_intent and not action_retry_used and latest_user_prompt:
                    action_retry_used = True
                    messages.append({"role":"assistant", "content":content})
                    messages.append({"role":"user", "content":
                        "Your previous response did not call a tool. This is an action/information request, so call the relevant tool now. "
                        "Use a dedicated tool when it matches; otherwise use run_shell_command to inspect or act. Do not just explain or claim the action is done. "
                        "Use a tool call and verify its result. Original request: " + latest_user_prompt
                    })
                    continue
                unsupported_claim = re.search(
                    r"(?:none of (?:the )?(?:provided|available) tools|no (?:available )?tools? can|cannot do that with (?:the )?(?:provided|available) tools|can't do that with (?:the )?(?:provided|available) tools|not supported by (?:the )?(?:available )?tools)",
                    content, re.I,
                )
                if unsupported_claim and not refusal_retry_used and latest_user_prompt:
                    refusal_retry_used = True
                    messages.append({"role":"assistant", "content":content})
                    messages.append({"role":"user", "content":
                        "Retry the task instead of refusing. You have the run_shell_command tool for general local PC tasks. "
                        "If a dedicated tool does not cover this request, use run_shell_command to inspect or do the task. "
                        "For a state-changing command, request the tool and let the approval gate handle it. "
                        "Do not claim unsupported unless the task truly requires visual access, root privileges, or unavailable hardware. "
                        "Original task: " + latest_user_prompt
                    })
                    continue
                return jsonify({"ok":True, "reply":content or "I finished checking the available actions.", "steps":steps})
            messages.append({"role":"assistant", "content":content, "tool_calls":calls})
            for call in calls[:2]:
                fn = call.get("function") or {}
                name = str(fn.get("name", ""))
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try: args = json.loads(args)
                    except json.JSONDecodeError: args = {}
                if name not in {x["function"]["name"] for x in _AGENT_TOOLS}:
                    result = _agent_result(False, "That tool is not available.")
                elif name in _AGENT_SENSITIVE_TOOLS or (name == "run_shell_command" and _agent_shell_requires_approval(args.get("command", ""))):
                    token = secrets.token_urlsafe(24)
                    with _AGENT_PENDING_LOCK:
                        now = time.time()
                        for old in [k for k,v in _AGENT_PENDING.items() if now-v["created"] > _AGENT_PENDING_TTL]: _AGENT_PENDING.pop(old, None)
                        _AGENT_PENDING[token] = {"name":name, "args":args if isinstance(args,dict) else {}, "created":now,
                            "owner":session.get("_agent_owner") or session.setdefault("_agent_owner", secrets.token_urlsafe(18))}
                    return jsonify({"ok":True, "reply":content or "This task needs your approval before I can continue.", "steps":steps,
                                    "pending":{"token":token, "tool":name, "args":args, "description":_agent_approval_description(name,args)}})
                else:
                    try: result = _agent_execute_tool(name, args)
                    except Exception as exc: result = _agent_result(False, str(exc))
                steps.append({"tool":name, "result":result})
                _audit_record(session.get("username"), "agent.tool." + name, str(result.get("message") or ("Success" if result.get("ok") else "Failed")), request.path, "AGENT", 200 if result.get("ok") else 400)
                messages.append({"role":"tool", "tool_name":name, "content":json.dumps(result, ensure_ascii=False)[:1200]})
                if simple_single_step and len(calls) == 1:
                    if not result.get("ok"):
                        reply = "That action failed: " + str(result.get("message", "Unknown error"))
                    elif name == "run_shell_command":
                        stdout = str(result.get("stdout", "")).strip()
                        stderr = str(result.get("stderr", "")).strip()
                        reply = f"Command finished (exit code {result.get('exit_code', 0)})."
                        if stdout:
                            reply += "\n\n" + stdout[:3500]
                        elif not stderr:
                            reply += " No output was produced."
                        if stderr:
                            reply += "\n\nStderr:\n" + stderr[:1200]
                    elif name == "get_system_status":
                        d = result.get("data", {})
                        reply = (f"CPU: {d.get('cpu_percent', 'unknown')}%\n"
                                 f"RAM: {d.get('ram_percent', 'unknown')}% "
                                 f"({d.get('ram_used_gb', '?')} / {d.get('ram_total_gb', '?')} GB)\n"
                                 f"Disk: {d.get('disk_percent', 'unknown')}% used, "
                                 f"{d.get('disk_free_gb', '?')} GB free\n"
                                 f"Uptime: {d.get('uptime_minutes', '?')} minutes\n"
                                 f"OS: {d.get('os', 'unknown')}")
                    else:
                        reply = str(result.get("message") or ("Completed " + name.replace("_", " ") + "."))
                    return jsonify({"ok":True, "reply":reply[:5000], "steps":steps})
        return jsonify({"ok":True, "reply":"I completed the available steps. Ask me to continue if more work is needed.", "steps":steps})
    except Exception as exc:
        app.logger.warning("Local agent request failed: %s", str(exc)[:350])
        return jsonify({"ok":False, "error":f"Agent request failed: {exc}"}), 502

def _agent_approval_description(name, args):
    labels = {"run_shell_command":"Run a shell command on your PC", "set_wallpaper":"Change your Serpantinum wallpaper", "type_text":"Type text into the currently focused application",
              "close_window":"Close a desktop window", "control_ollama_service":"Change the Ollama service state",
              "reload_serpantinum":"Reload the Serpantinum shell", "click_at":"Click a screen coordinate", "terminate_process":"Terminate a process", "package_action":"Change an installed package"}
    # JSON-safe preview is shown to the user, not used as executable shell text.
    return labels.get(name, "Perform a sensitive action") + ": " + json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)[:500]

@app.route("/api/agent/approve", methods=["POST"])
def local_agent_approve():
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", ""))
    approved = payload.get("approved") is True
    with _AGENT_PENDING_LOCK:
        pending = _AGENT_PENDING.pop(token, None)
    if not pending or time.time() - pending["created"] > _AGENT_PENDING_TTL:
        return jsonify({"ok":False, "error":"Approval expired. Please submit the task again."}), 410
    if pending.get("owner") != session.get("_agent_owner"):
        return jsonify({"ok":False, "error":"This approval belongs to a different session."}), 403
    if not approved:
        return jsonify({"ok":True, "approved":False, "message":"Action cancelled."})
    try:
        result = _agent_execute_tool(pending["name"], pending["args"])
        _audit_record(session.get("username"), "agent.approved." + pending["name"], str(result.get("message") or ("Success" if result.get("ok") else "Failed")), request.path, "AGENT", 200 if result.get("ok") else 400)
        app.logger.info("Agent action approved and executed: %s", pending["name"])
        return jsonify({"ok":True, "approved":True, "tool":pending["name"], "result":result})
    except Exception as exc:
        app.logger.warning("Approved agent action failed (%s): %s", pending["name"], str(exc)[:300])
        return jsonify({"ok":False, "error":str(exc)}), 400


# ---------------------------------------------------------------------------
# Admin-only desktop screen viewer and screenshot gallery (Hyprland/Wayland)
# ---------------------------------------------------------------------------
_SCREENSHOT_HOME = Path(pwd.getpwnam(DASHBOARD_USER).pw_dir) if DASHBOARD_USER in {entry.pw_name for entry in pwd.getpwall()} else Path.home()
# Screenshot captures are intentionally ephemeral: every dashboard process start
# clears the gallery, so these files never become a permanent screenshot archive.
_SCREENSHOT_DIR = Path(os.environ.get("DASHBOARD_SCREENSHOT_DIR", "/tmp/system-dashboard-screenshots"))
try:
    if _SCREENSHOT_DIR.is_dir() and not _SCREENSHOT_DIR.is_symlink():
        for _old_shot in _SCREENSHOT_DIR.glob("desktop-*.png"):
            try:
                if _old_shot.is_file() and not _old_shot.is_symlink(): _old_shot.unlink()
            except OSError: pass
    else:
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_SCREENSHOT_DIR, 0o700)
except OSError as _shot_dir_error:
    app.logger.warning("Could not initialize temporary screenshot directory: %s", _shot_dir_error)


def _screen_session_environment():
    """Build a Wayland environment for the configured desktop user.

    Flask may run as a system service and therefore lack the interactive
    Hyprland variables. Discover the user's runtime directory and socket rather
    than trusting the service manager's environment.
    """
    env = os.environ.copy()
    try:
        account = pwd.getpwnam(DASHBOARD_USER)
        uid = account.pw_uid
        home = Path(account.pw_dir)
    except KeyError:
        uid, home = os.getuid(), Path.home()
    runtime = Path(f"/run/user/{uid}")
    if runtime.is_dir():
        env["XDG_RUNTIME_DIR"] = str(runtime)
        sockets = sorted(x for x in runtime.glob("wayland-*") if not x.name.endswith(".lock"))
        if not env.get("WAYLAND_DISPLAY") or not (runtime / env.get("WAYLAND_DISPLAY", "")).exists():
            if sockets:
                env["WAYLAND_DISPLAY"] = sockets[0].name
        if not env.get("DBUS_SESSION_BUS_ADDRESS"):
            bus = runtime / "bus"
            if bus.exists():
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
        hypr_dir = runtime / "hypr"
        if hypr_dir.is_dir() and not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
            instances = sorted(x for x in hypr_dir.iterdir() if x.is_dir())
            if instances:
                env["HYPRLAND_INSTANCE_SIGNATURE"] = instances[-1].name
    env.setdefault("HOME", str(home))
    env.setdefault("USER", DASHBOARD_USER)
    env.setdefault("XDG_SESSION_TYPE", "wayland")
    env.setdefault("XDG_CURRENT_DESKTOP", "Hyprland")
    return env


def _screen_capture_bytes(fmt="png"):
    """Capture one frame from the active Wayland desktop."""
    env = _screen_session_environment()
    grim = shutil.which("grim") or ("/usr/bin/grim" if Path("/usr/bin/grim").exists() else None)
    if not grim:
        raise RuntimeError("Screen capture utility 'grim' is missing. Install it with: sudo pacman -S grim")
    if not env.get("WAYLAND_DISPLAY") or not env.get("XDG_RUNTIME_DIR"):
        raise RuntimeError("Could not find the desktop's Wayland socket. Keep the Hyprland session logged in and restart the dashboard service.")
    # -c asks grim to include the current pointer in both the live frames and screenshots.
    args = [grim, "-c"]
    if fmt == "jpeg":
        args += ["-t", "jpeg", "-q", "58"]
    args += ["-"]
    # If the dashboard is launched as root, execute grim as the desktop user so
    # the compositor socket and its permissions match the active session.
    if os.geteuid() == 0 and DASHBOARD_USER and DASHBOARD_USER != "root":
        runuser = shutil.which("runuser") or "/usr/sbin/runuser"
        args = [runuser, "-u", DASHBOARD_USER, "--", "/usr/bin/env"] + [f"{k}={v}" for k, v in env.items() if k in {"HOME", "USER", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "HYPRLAND_INSTANCE_SIGNATURE", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "PATH"}] + args
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=8, check=False, env=env if os.geteuid() != 0 else None)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Screen capture timed out. Check that Hyprland is running and allows screen capture.")
    expected = b"\xff\xd8\xff" if fmt == "jpeg" else b"\x89PNG\r\n\x1a\n"
    if result.returncode != 0 or not result.stdout.startswith(expected):
        detail = result.stderr.decode("utf-8", "replace").strip()[:350]
        if "permission" in detail.lower() or "denied" in detail.lower():
            raise RuntimeError("Hyprland denied screen capture. Check the compositor's screencopy permissions and allow grim if appropriate.")
        raise RuntimeError(detail or "Could not create a display capture. The dashboard service must access the active Wayland session.")
    return result.stdout


def _screen_capture_png():
    return _screen_capture_bytes("png")


def _screen_user_required():
    if not session.get("authenticated") or not session.get("username"):
        return jsonify({"ok": False, "error": "Please sign in to view the desktop."}), 401
    return None


@app.get("/api/screen/stream")
def screen_stream():
    denied = _screen_user_required()
    if denied: return denied
    # MJPEG lets the browser render frames continuously without a fetch/blob
    # cycle per frame. Moderate frame rate/quality are deliberate for the user's
    # low-power i3 desktop and LAN clients.
    def frames():
        boundary = b"frame"
        while True:
            try:
                frame = _screen_capture_bytes("jpeg")
                yield b"--" + boundary + b"\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\nCache-Control: no-store\r\n\r\n" + frame + b"\r\n"
            except GeneratorExit:
                return
            except Exception as exc:
                app.logger.warning("Screen stream frame failed: %s", str(exc)[:250])
                message = str(exc).encode("utf-8", "replace")[:200]
                # Send an empty multipart error header, then stop so clients
                # don't keep hammering a broken compositor session forever.
                yield b"--" + boundary + b"\r\nContent-Type: text/plain\r\nContent-Length: " + str(len(message)).encode() + b"\r\n\r\n" + message + b"\r\n"
                return
            time.sleep(0.28)
    response = Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame", direct_passthrough=True)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/api/screen/live")
def screen_live():
    denied = _screen_user_required()
    if denied: return denied
    try:
        data = _screen_capture_png()
        response = app.response_class(data, mimetype="image/png")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503


@app.post("/api/screen/capture")
def screen_capture_save():
    denied = _screen_user_required()
    if denied: return denied
    try:
        data = _screen_capture_png()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = _SCREENSHOT_DIR / f"desktop-{stamp}-{secrets.token_hex(3)}.png"
        path.write_bytes(data)
        try: os.chmod(path, 0o600)
        except OSError: pass
        _audit_record(session.get("username"), "screen.screenshot", path.name, request.path, request.method, 200)
        return jsonify({"ok": True, "name": path.name, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "url": "/api/screen/files/" + path.name})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503


@app.get("/api/screen/screenshots")
def screen_screenshots():
    denied = _screen_user_required()
    if denied: return denied
    items = []
    try:
        for path in sorted(_SCREENSHOT_DIR.glob("desktop-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            if path.is_file() and not path.is_symlink():
                items.append({"name": path.name, "size": path.stat().st_size,
                              "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)),
                              "url": "/api/screen/files/" + path.name})
    except OSError:
        pass
    return jsonify({"ok": True, "screenshots": items})


@app.get("/api/screen/files/<name>")
def screen_screenshot_file(name):
    denied = _screen_user_required()
    if denied: return denied
    if not re.fullmatch(r"desktop-[0-9]{8}-[0-9]{6}-[a-f0-9]{6}\.png", name):
        return jsonify({"ok": False, "error": "Invalid screenshot name."}), 400
    path = _SCREENSHOT_DIR / name
    if not path.is_file() or path.is_symlink():
        return jsonify({"ok": False, "error": "Screenshot not found."}), 404
    response = send_file(path, mimetype="image/png", conditional=True, max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.delete("/api/screen/files/<name>")
def screen_screenshot_delete():
    denied = _screen_user_required()
    if denied: return denied
    if not re.fullmatch(r"desktop-[0-9]{8}-[0-9]{6}-[a-f0-9]{6}\.png", name):
        return jsonify({"ok": False, "error": "Invalid screenshot name."}), 400
    path = _SCREENSHOT_DIR / name
    if not path.is_file() or path.is_symlink():
        return jsonify({"ok": False, "error": "Screenshot not found."}), 404
    try:
        path.unlink()
        _audit_record(session.get("username"), "screen.screenshot.delete", name, request.path, request.method, 200)
        return jsonify({"ok": True, "deleted": name})
    except OSError as exc:
        return jsonify({"ok": False, "error": "Could not delete screenshot: " + str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=DASHBOARD_PORT, debug=False)
