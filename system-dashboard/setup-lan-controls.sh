#!/bin/bash
set -euo pipefail

USER_NAME="${SUDO_USER:-${USER}}"

sudo pacman -S --needed avahi polkit ydotool wtype
YDOTOOLD_BIN="$(command -v ydotoold)"
if [ -z "$YDOTOOLD_BIN" ]; then
    echo "ERROR: ydotoold was not found after installing ydotool." >&2
    exit 1
fi
sudo hostnamectl set-hostname marwan-pc
sudo systemctl enable --now avahi-daemon.service

# ydotool daemon: the dashboard itself is a system service, so run ydotoold as a
# system service and expose its socket inside the normal user's runtime directory.
USER_UID="$(id -u "$USER_NAME")"
USER_GID="$(id -g "$USER_NAME")"
SOCKET_PATH="/tmp/system-dashboard-ydotool.sock"

if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx input; then
    :
else
    echo "Adding $USER_NAME to input group for ydotool mouse control. Log out/in once afterward."
    sudo usermod -aG input "$USER_NAME"
fi

sudo install -d -m 755 /etc/systemd/system
sudo tee /etc/systemd/system/system-dashboard-ydotool.service >/dev/null <<EOF
[Unit]
Description=ydotool daemon for System Dashboard
After=systemd-user-sessions.service

[Service]
Type=simple
ExecStartPre=/usr/bin/rm -f ${SOCKET_PATH}
ExecStart=${YDOTOOLD_BIN} --socket-path=${SOCKET_PATH} --socket-perm=0660 --socket-own=${USER_UID}:${USER_GID}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now system-dashboard-ydotool.service

sudo install -d -m 755 /etc/polkit-1/rules.d
sudo tee /etc/polkit-1/rules.d/49-system-dashboard-power.rules >/dev/null <<EOF
polkit.addRule(function(action, subject) {
    if (subject.user == "${USER_NAME}" &&
        (action.id == "org.freedesktop.login1.power-off" ||
         action.id == "org.freedesktop.login1.power-off-multiple-sessions" ||
         action.id == "org.freedesktop.login1.power-off-ignore-inhibit" ||
         action.id == "org.freedesktop.login1.reboot" ||
         action.id == "org.freedesktop.login1.reboot-multiple-sessions" ||
         action.id == "org.freedesktop.login1.reboot-ignore-inhibit" ||
         action.id == "org.freedesktop.login1.suspend" ||
         action.id == "org.freedesktop.login1.suspend-multiple-sessions")) {
        return polkit.Result.YES;
    }
});
EOF
sudo systemctl restart polkit.service || true

echo
echo "LAN name: http://marwan-pc.local:7171"
echo "Setup is normally a ONE-TIME installer. After this, use systemctl restart/enable for the dashboard; do not rerun this script for normal restarts."
echo "Verify: avahi-resolve-host-name marwan-pc.local"
echo "Verify ydotool: sudo systemctl status system-dashboard-ydotool.service"
echo "Verify socket: ls -l ${SOCKET_PATH}"
echo "A logout/login may be required after adding the input group."

# Do not create a legacy quickshell -> src/quickshell link. The old wallpaper
# IPC path resolves through that legacy location and expects shell.qml.
SERP_HOME="/home/${USER_NAME}/.local/share/serpantinum"
if [ -L "${SERP_HOME}/quickshell" ]; then
    sudo -u "${USER_NAME}" rm -f "${SERP_HOME}/quickshell" || true
    echo "Removed obsolete Serpantinum quickshell compatibility link."
fi
