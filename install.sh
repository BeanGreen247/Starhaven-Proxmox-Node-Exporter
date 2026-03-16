#!/usr/bin/env bash
# install.sh — Starhaven Proxmox Node Exporter installer
#
# Installs proxmox-node-exporter.py as a systemd service on the Proxmox host.
# Python deps are resolved via apt first, falling back to venv (PEP 668 safe).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/BeanGreen247/Starhaven-Proxmox-Node-Exporter/main/install.sh | bash
#
# Or with a custom GITHUB_USER:
#   GITHUB_USER=myuser bash install.sh

set -euo pipefail

# -------- CONFIG --------
GITHUB_USER="BeanGreen247"
GITHUB_REPO="Starhaven-Proxmox-Node-Exporter"
GITHUB_BRANCH="main"

INSTALL_DIR="/opt/proxmox-exporter"
PY_SCRIPT="${INSTALL_DIR}/proxmox-node-exporter.py"
VENV_DIR="${INSTALL_DIR}/.venv"
SERVICE_NAME="proxmox-node-exporter"
EXPORTER_PORT="${EXPORTER_PORT:-9101}"

SCRIPT_URL="https://raw.githubusercontent.com/${GITHUB_USER}/${GITHUB_REPO}/${GITHUB_BRANCH}/proxmox-node-exporter.py"

# -------- UX --------
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34m'; N='\033[0m'
msg()  { echo -e "${Y}[*]${N} $*"; }
ok()   { echo -e "${G}[✓]${N} $*"; }
info() { echo -e "${B}[i]${N} $*"; }
die()  { echo -e "${R}[✗]${N} $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root: sudo bash install.sh"

echo
echo -e "${B}╔══════════════════════════════════════════════╗${N}"
echo -e "${B}║   Starhaven Proxmox Node Exporter Installer  ║${N}"
echo -e "${B}╚══════════════════════════════════════════════╝${N}"
echo

export DEBIAN_FRONTEND=noninteractive

# -------- APT DEPS --------
msg "Installing OS dependencies…"
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  python3 python3-venv curl ca-certificates \
  lm-sensors sysstat smartmontools nvme-cli \
  zfsutils-linux 2>/dev/null || \
apt-get install -y -qq --no-install-recommends \
  python3 python3-venv curl ca-certificates \
  lm-sensors sysstat smartmontools nvme-cli
ok "OS dependencies installed"

# -------- PYTHON DEPS: apt → venv fallback --------
PYBIN="/usr/bin/python3"

need_py=false
$PYBIN - <<'PY' 2>/dev/null || need_py=true
import importlib
for m in ("prometheus_client", "psutil"):
    importlib.import_module(m)
PY

if $need_py; then
  if apt-cache show python3-prometheus-client >/dev/null 2>&1 && \
     apt-cache show python3-psutil >/dev/null 2>&1; then
    msg "Installing Python libs via apt…"
    apt-get install -y -qq python3-prometheus-client python3-psutil
    PYBIN="/usr/bin/python3"
    ok "Python libs installed via apt"
  else
    msg "apt packages not found — building venv in ${VENV_DIR}…"
    mkdir -p "${VENV_DIR}"
    $PYBIN -m venv "${VENV_DIR}"
    "${VENV_DIR}/bin/pip" install -q --upgrade pip
    "${VENV_DIR}/bin/pip" install -q prometheus-client psutil
    PYBIN="${VENV_DIR}/bin/python3"
    ok "Virtualenv ready: ${PYBIN}"
  fi
else
  ok "Python libs already present"
fi

# -------- SENSORS SETUP --------
msg "Configuring hardware sensors…"
yes | sensors-detect --auto >/dev/null 2>&1 || true
modprobe coretemp 2>/dev/null || true
# Dell SMM (for Starhaven Dell hardware)
modprobe dell-smm-hwmon 2>/dev/null || modprobe dell_smm_hwmon 2>/dev/null || true
grep -q '^coretemp$' /etc/modules 2>/dev/null || echo coretemp >> /etc/modules || true
ok "Sensors configured"

# -------- FETCH EXPORTER SCRIPT --------
msg "Downloading exporter script from GitHub…"
mkdir -p "${INSTALL_DIR}"

curl -fsSL \
  --retry 3 --retry-connrefused --retry-delay 2 \
  -H 'Cache-Control: no-cache' \
  "${SCRIPT_URL}" \
  -o "${PY_SCRIPT}" || die "Download failed. Check your GITHUB_USER / network."

# Guard against HTML responses (e.g. 404 page)
if head -n1 "${PY_SCRIPT}" | grep -qi '<!DOCTYPE html>'; then
  rm -f "${PY_SCRIPT}"
  die "Got an HTML response — repo/path is probably wrong. Set GITHUB_USER correctly."
fi

chmod +x "${PY_SCRIPT}"
ok "Exporter script saved: ${PY_SCRIPT}"

# -------- SYSTEMD UNIT --------
msg "Writing systemd service unit…"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Starhaven Proxmox Node Exporter for Prometheus
Documentation=https://github.com/${GITHUB_USER}/${GITHUB_REPO}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=EXPORTER_PORT=${EXPORTER_PORT}
Environment=COLLECTION_INTERVAL=15
ExecStart=${PYBIN} ${PY_SCRIPT}
Restart=always
RestartSec=10
TimeoutStopSec=30

# Hardening
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null
systemctl restart "${SERVICE_NAME}"

sleep 3
if systemctl is-active --quiet "${SERVICE_NAME}"; then
  ok "Service is running: ${SERVICE_NAME}"
else
  echo
  echo "=== Service logs ==="
  journalctl -u "${SERVICE_NAME}" -b --no-pager | tail -n 40 >&2
  die "Service failed to start — see logs above"
fi

# -------- VERIFY --------
msg "Verifying metrics endpoint…"
IP="$(hostname -I | awk '{print $1}')"

if curl -fsS "http://127.0.0.1:${EXPORTER_PORT}/metrics" >/dev/null 2>&1; then
  echo
  ok "Metrics available at: http://${IP}:${EXPORTER_PORT}/metrics"
  echo
  info "Add this job to your Prometheus scrape_configs:"
  echo
  cat <<YAML
  - job_name: 'proxmox_node_exporter'
    scrape_interval: 30s
    static_configs:
      - targets: ['${IP}:${EXPORTER_PORT}']
YAML
  echo
  info "Available metric families:"
  curl -fsS "http://127.0.0.1:${EXPORTER_PORT}/metrics" 2>/dev/null \
    | grep '^# HELP' | awk '{print "  "$3}' | sort | head -30
  echo "  ..."
else
  die "No response on port ${EXPORTER_PORT} — check firewall / service logs"
fi

echo
echo -e "${G}Installation complete!${N}"
