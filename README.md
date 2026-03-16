# Starhaven Homelab — Proxmox Monitoring Stack

A complete Prometheus + Grafana monitoring stack for a **Proxmox VE** homelab.

Includes a custom Python node exporter tailored for the Starhaven hardware profile,
a Grafana dashboard JSON, and example Prometheus configuration.

---

## Hardware Profile

| Component | Detail |
|---|---|
| Host | Dell (SMM hwmon sensors + coretemp) |
| CPU | Intel 6-core (`platform_coretemp_0`) |
| Storage | NVMe nvme0n1 → LVM pve-root (96 GB) + local-lvm thin pool (794 GB) |
| Backup HDD | sda1 → `/mnt/bckpHDD` (ext4, 931.5 GB) |
| ZFS pool | sdb → `vm-8tb-hdd0` (7.3 TB, QEMU zvols + LXC subvols) |
| RAM | 64 GB ECC |

---

## Components

```
starhaven-homelab/
├── proxmox-node-exporter.py   # Custom Prometheus exporter (port 9101)
├── install.sh                 # One-line installer for the exporter
├── grafana/
│   └── starhaven-dashboard.json  # Grafana dashboard (import this)
├── prometheus/
│   └── prometheus.yml         # Example Prometheus scrape config
└── README.md
```

---

## Quick Start

### 1 — Install the exporter on the Proxmox host

```bash
# On the PVE host (192.168.0.222) as root:
GITHUB_USER=BeanGreen247 bash <(curl -fsSL \
  https://raw.githubusercontent.com/BeanGreen247/starhaven-homelab/main/install.sh)
```

The installer will:
- Install OS deps (`lm-sensors`, `smartmontools`, `nvme-cli`, `zfsutils-linux`)
- Try `apt` for `python3-prometheus-client` + `python3-psutil`, falling back to venv
- Load `coretemp` + `dell-smm-hwmon` kernel modules
- Download and install `proxmox-node-exporter.py`
- Create and enable `proxmox-node-exporter.service`
- Verify metrics on `http://<host>:9101/metrics`

### 2 — Configure Prometheus

Add to your `/etc/prometheus/prometheus.yml`:

```yaml
scrape_configs:
  # Standard node_exporter (Debian package, port 9100)
  - job_name: 'node_exporter'
    static_configs:
      - targets: ['192.168.0.222:9100']

  # Custom Proxmox node exporter (port 9101)
  # Adds: ZFS pool capacity, PVE VM status, SMART, Dell SMM sensors
  - job_name: 'proxmox_node_exporter'
    scrape_interval: 30s
    static_configs:
      - targets: ['192.168.0.222:9101']

  # pve_exporter (port 9221, runs in LXC 108)
  - job_name: 'proxmox'
    metrics_path: /pve
    params:
      target: ['192.168.0.222:8006']
    static_configs:
      - targets: ['192.168.0.246:9221']
```

Restart Prometheus:
```bash
systemctl restart prometheus
```

### 3 — Import the Grafana dashboard

1. Open Grafana → **Dashboards → Import**
2. Upload `grafana/starhaven-dashboard.json`
3. Select your Prometheus datasource

---

## Metrics Reference

### Filesystems (`node_filesystem_*`)

Reported for all real mounts — excludes `tmpfs`, `devtmpfs`, `efivarfs`, `squashfs`, bind-mount duplicates (`/opt/proxmox-exporter`, `/var/tmp`).

| Mountpoint | Device | Type | Notes |
|---|---|---|---|
| `/` | `/dev/mapper/pve-root` | ext4 | NVMe LVM — Proxmox OS |
| `/mnt/bckpHDD` | `/dev/sda1` | ext4 | 931.5 GB backup HDD |
| `/vm-8tb-hdd0` | `vm-8tb-hdd0` | zfs | ZFS root dataset (root quota only) |
| `/vm-8tb-hdd0/subvol-*` | `vm-8tb-hdd0/subvol-*` | zfs | LXC container disks |

### ZFS Pool (`node_zfs_zpool_*`)

Real pool-level capacity from `zpool list`:

| Metric | Description |
|---|---|
| `node_zfs_zpool_size_bytes` | Total pool capacity (~7.98 TB) |
| `node_zfs_zpool_allocated_bytes` | Space allocated across all datasets + zvols |
| `node_zfs_zpool_free_bytes` | Free space in the pool |
| `node_zfs_zpool_fragmentation_percent` | Pool fragmentation % |
| `node_zfs_zpool_health` | 0=ONLINE, 1=DEGRADED, 2=FAULTED |

> **Note:** `node_filesystem_*` for `/vm-8tb-hdd0` shows only the root dataset's quota (~2.1 TB), not the full pool. The dashboard uses `node_zfs_zpool_*` for the pool row.

### ZFS ARC (`node_zfs_arc_*`)

| Metric | Description |
|---|---|
| `node_zfs_arc_size_bytes` | Current ARC size |
| `node_zfs_arc_c_bytes` | ARC target size |
| `node_zfs_arc_c_max_bytes` | ARC maximum size |
| `node_zfs_arc_hits_total` | ARC hit counter |
| `node_zfs_arc_misses_total` | ARC miss counter |

### Sensors (`node_hwmon_*`)

| Chip | Sensor | Description |
|---|---|---|
| `platform_coretemp_0` | `Package_id_0`, `Core_0`–`Core_5` | CPU package + per-core temps |
| `nvme_nvme0` | `Composite`, `Sensor_1` | NVMe temperatures |
| `platform_dell_smm_hwmon` | `temp1`–`tempN` | Dell SMM thermal sensors |
| `platform_dell_smm_hwmon` | `fan1`, `fan2` | Dell SMM fan speeds (RPM) |

### SMART (`node_disk_smart_*`)

| Metric | Labels | Description |
|---|---|---|
| `node_disk_smart_healthy` | `device`, `model` | 1 = PASSED, 0 = FAILED |
| `node_disk_smart_temperature_celsius` | `device`, `model` | Drive temperature |
| `node_disk_smart_power_on_hours_total` | `device`, `model` | Lifetime power-on hours |
| `node_disk_smart_power_cycles_total` | `device`, `model` | Power cycle count |

Monitored devices: `sda`, `sdb`, `nvme0`.

### PVE VMs & LXC (`pve_*`)

| Metric | Labels | Description |
|---|---|---|
| `pve_vm_status` | `vmid`, `name`, `type` | 1=running, 0=stopped |
| `pve_vm_cpu_usage_percent` | `vmid`, `name`, `type` | CPU usage % |
| `pve_vm_memory_used_bytes` | `vmid`, `name`, `type` | RAM in use |
| `pve_vm_memory_total_bytes` | `vmid`, `name`, `type` | RAM allocated |
| `pve_vm_uptime_seconds` | `vmid`, `name`, `type` | VM uptime |
| `pve_vm_count` | `type`, `status` | Count by type + status |
| `pve_version_info` | `version` | Proxmox VE version string |

---

## Dashboard Panels

| Panel | Type | Source |
|---|---|---|
| Temperatures | Stat | `node_hwmon_temp_celsius` (9100) |
| Load Average | Stat | `node_load*` (9100) |
| Swap | Stat | `node_memory_Swap*` (9100) |
| System Uptime | Stat | `node_boot_time_seconds` (9100) |
| ZFS ARC Size History | Time series | `node_zfs_arc_*` (9100) |
| ZFS ARC Hit Rate | Time series | `node_zfs_arc_hits/misses` (9100) |
| Host CPU Usage | Time series | `node_cpu_seconds_total` (9100) |
| Host RAM Usage | Time series | `node_memory_*` (9100) |
| Host Disk I/O | Time series | `node_disk_*` (9100) |
| Host Network I/O | Time series | `node_network_*` (9100) |
| **Disk Capacity** | **Table** | `node_filesystem_*` + `node_zfs_zpool_*` **(9101)** |
| Fan Speeds | Stat | `node_hwmon_fan_rpm` (9100) |
| APT Updates | Stat | `apt_upgrades_pending` (9100) |

---

## Service Management

```bash
# Status
systemctl status proxmox-node-exporter

# Logs
journalctl -u proxmox-node-exporter -f

# Restart
systemctl restart proxmox-node-exporter

# Check metrics
curl -s http://localhost:9101/metrics | grep -E "node_zfs_zpool|pve_vm_count"

# Enable debug mode
systemctl edit proxmox-node-exporter
# Add under [Service]:
#   Environment=DEBUG_MODE=true
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `EXPORTER_PORT` | `9101` | HTTP listen port |
| `COLLECTION_INTERVAL` | `15` | Seconds between collections |
| `DEBUG_MODE` | `false` | Verbose logging + include loopback metrics |
| `PARALLEL_COLLECTORS` | `true` | Run collectors concurrently |
| `MAX_WORKERS` | `4` | Thread pool size |

---

## Info

Made for:
- Clean ZFS pool capacity metrics
- Dell SMM hwmon support
- Bind-mount deduplication
- ZFS zvol exclusion from disk I/O
- Starhaven-specific labelling
