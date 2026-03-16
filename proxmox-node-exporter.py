#!/usr/bin/env python3
"""
Starhaven Proxmox Node Exporter for Prometheus
================================================
A self-contained, auto-detecting Prometheus exporter for Proxmox VE hosts.

Hardware profile (starhaven):
  - Dell PowerEdge-class host with SMM sensors + coretemp
  - NVMe SSD (pve-root / local-lvm LVM thin)
  - sda1 → /mnt/bckpHDD  (ext4, 931.5 GB backup HDD)
  - sdb  → ZFS pool vm-8tb-hdd0 (7.3 TB)
  - ZFS zvols for QEMU VMs (zd* block devices)
  - LXC containers via pct

Metrics exposed (port 9101):
  Base:       node_cpu_*, node_memory_*, node_filesystem_*, node_disk_*, node_network_*
  ZFS:        node_zfs_arc_*, node_zfs_zpool_size/free/allocated/health/fragmentation
  Sensors:    node_hwmon_temp_celsius, node_hwmon_fan_rpm (coretemp + dell_smm_hwmon)
  SMART:      node_disk_smart_* (sda, sdb, nvme0n1)
  PVE:        pve_vm_status, pve_vm_cpu/memory, pve_vm_count, pve_version_info
  Disks:      pve_disk_volsize_bytes, pve_disk_used_bytes (per VM/LXC ZFS zvol/subvol)
  System:     node_load*, node_procs_*, node_systemd_unit_state

Environment variables:
  EXPORTER_PORT       default: 9101
  COLLECTION_INTERVAL default: 15  (seconds)
  DEBUG_MODE          default: false
  PARALLEL_COLLECTORS default: true
  MAX_WORKERS         default: 4
"""

import subprocess
import re
import time
import socket
import os
import platform
import json
import glob
import shutil
import threading
import concurrent.futures
import signal
import sys
from collections import defaultdict, deque
from functools import wraps
from prometheus_client import start_http_server, Gauge, Info
from prometheus_client.core import CollectorRegistry
import logging

try:
    import psutil
except ImportError:
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'psutil'], check=True)
    import psutil

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
EXPORTER_PORT       = int(os.environ.get('EXPORTER_PORT', 9101))
COLLECTION_INTERVAL = int(os.environ.get('COLLECTION_INTERVAL', 15))
DEBUG_MODE          = os.environ.get('DEBUG_MODE', '').lower() in ('true', '1', 'yes')
PARALLEL_COLLECTORS = os.environ.get('PARALLEL_COLLECTORS', 'true').lower() in ('true', '1', 'yes')
MAX_WORKERS         = int(os.environ.get('MAX_WORKERS', 4))

# Filesystem mountpoints to exclude from node_filesystem_* metrics.
# Excludes kernel/virtual FSes and duplicate bind-mounts created by systemd
# (PrivateTmp, ReadWritePaths sandboxing).
EXCLUDED_MOUNTPOINTS_RE = re.compile(
    r'^/(dev|proc|run|sys|var/lib/lxcfs|opt/proxmox-exporter|var/tmp)(/|$)'
)
EXCLUDED_FSTYPES_RE = re.compile(
    r'^(tmpfs|devtmpfs|devfs|fuse\.lxcfs|squashfs|vfat|efivarfs|overlay|cgroup2?)$'
)

logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format='%(asctime)s %(levelname)-8s [%(funcName)s] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def timed(timeout=5):
    """Decorator: swallow TimeoutExpired and generic exceptions, return None."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except subprocess.TimeoutExpired:
                logger.warning("%s timed out after %ds", func.__name__, timeout)
            except Exception as exc:
                logger.error("Error in %s: %s", func.__name__, exc)
            return None
        return wrapper
    return decorator


class TTLCache:
    def __init__(self, default_ttl=60):
        self._cache = {}
        self.default_ttl = default_ttl

    def get(self, key, compute, ttl=None):
        ttl = ttl or self.default_ttl
        now = time.time()
        if key in self._cache:
            value, expiry = self._cache[key]
            if now < expiry:
                return value
        value = compute()
        self._cache[key] = (value, now + ttl)
        return value

    def clear_expired(self):
        now = time.time()
        self._cache = {k: v for k, v in self._cache.items() if v[1] > now}


# ──────────────────────────────────────────────
# Exporter
# ──────────────────────────────────────────────

class StarheavenExporter:
    VERSION = "1.0.0"

    def __init__(self):
        self.registry  = CollectorRegistry()
        self.hostname  = socket.gethostname()
        self.start_ts  = time.time()
        self.cache     = TTLCache()
        self.executor  = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) \
                         if PARALLEL_COLLECTORS else None

        self.features = {
            'sensors':         False,
            'zfs':             False,
            'qemu_vms':        False,
            'lxc_containers':  False,
            'smart':           False,
            'nvme':            False,
            'systemd':         False,
        }

        self._detect_features()
        self._init_metrics()
        logger.info("Starhaven exporter v%s — active features: %s",
                    self.VERSION, [k for k, v in self.features.items() if v])

    # ── Feature detection ──────────────────────

    def _detect_features(self):
        checks = {
            'sensors':        lambda: bool(shutil.which('sensors') and
                                           subprocess.run(['sensors'], capture_output=True, timeout=2).returncode == 0),
            'zfs':            lambda: os.path.exists('/proc/spl/kstat/zfs') or bool(shutil.which('zpool')),
            'qemu_vms':       lambda: bool(shutil.which('qm') and
                                           subprocess.run(['qm', 'list'], capture_output=True, timeout=2).returncode == 0),
            'lxc_containers': lambda: bool(shutil.which('pct') and
                                           subprocess.run(['pct', 'list'], capture_output=True, timeout=2).returncode == 0),
            'smart':          lambda: bool(shutil.which('smartctl')),
            'nvme':           lambda: len(glob.glob('/sys/class/nvme/nvme*')) > 0,
            'systemd':        lambda: bool(shutil.which('systemctl')),
        }
        for feature, check in checks.items():
            try:
                if check():
                    self.features[feature] = True
                    logger.info("  ✓ %s detected", feature)
            except Exception as exc:
                logger.debug("Feature detect %s: %s", feature, exc)

    # ── Metric initialisation ──────────────────

    def _init_metrics(self):
        r = self.registry
        # ── Exporter meta
        self.exporter_info = Info('node_exporter_info', 'Exporter metadata', registry=r)
        self.feature_enabled = Gauge('node_exporter_feature_enabled', 'Feature enabled',
                                     ['feature'], registry=r)
        self.collection_duration = Gauge('node_exporter_collection_duration_seconds',
                                         'Collection duration seconds', ['collector'], registry=r)
        self.collection_success = Gauge('node_exporter_collection_success',
                                        'Collection success', ['collector'], registry=r)
        # ── Node / PVE info
        self.node_info  = Info('node', 'Node information', registry=r)
        self.pve_version = Info('pve_version', 'Proxmox VE version', registry=r)
        self.boot_time  = Gauge('node_boot_time_seconds', 'Boot timestamp', registry=r)
        # ── CPU
        self.cpu_count     = Gauge('node_cpu_count', 'CPU count', ['type'], registry=r)
        self.cpu_seconds   = Gauge('node_cpu_usage_seconds_total', 'CPU time', ['cpu', 'mode'], registry=r)
        self.cpu_percent   = Gauge('node_cpu_usage_percent', 'CPU usage %', ['cpu'], registry=r)
        self.cpu_freq      = Gauge('node_cpu_frequency_hertz', 'CPU frequency', ['cpu', 'type'], registry=r)
        self.load1  = Gauge('node_load1',  '1m load average', registry=r)
        self.load5  = Gauge('node_load5',  '5m load average', registry=r)
        self.load15 = Gauge('node_load15', '15m load average', registry=r)
        # ── Memory
        self.mem_total   = Gauge('node_memory_MemTotal_bytes',     'Total RAM',     registry=r)
        self.mem_free    = Gauge('node_memory_MemFree_bytes',      'Free RAM',      registry=r)
        self.mem_avail   = Gauge('node_memory_MemAvailable_bytes', 'Available RAM', registry=r)
        self.mem_cached  = Gauge('node_memory_Cached_bytes',       'Cached RAM',    registry=r)
        self.mem_buffers = Gauge('node_memory_Buffers_bytes',      'Buffers',       registry=r)
        self.mem_shared  = Gauge('node_memory_Shared_bytes',       'Shared RAM',    registry=r)
        self.swap_total  = Gauge('node_memory_SwapTotal_bytes',    'Total swap',    registry=r)
        self.swap_free   = Gauge('node_memory_SwapFree_bytes',     'Free swap',     registry=r)
        # ── Filesystem
        self.fs_size  = Gauge('node_filesystem_size_bytes',  'FS size',      ['device', 'mountpoint', 'fstype'], registry=r)
        self.fs_free  = Gauge('node_filesystem_free_bytes',  'FS free',      ['device', 'mountpoint', 'fstype'], registry=r)
        self.fs_avail = Gauge('node_filesystem_avail_bytes', 'FS available', ['device', 'mountpoint', 'fstype'], registry=r)
        # ── Disk I/O (Gauge — values are raw kernel cumulative counters read each cycle)
        self.disk_read_bytes    = Gauge('node_disk_read_bytes_total',      'Disk bytes read',    ['device'], registry=r)
        self.disk_written_bytes = Gauge('node_disk_written_bytes_total',   'Disk bytes written', ['device'], registry=r)
        self.disk_io_time       = Gauge('node_disk_io_time_seconds_total', 'Disk I/O time',      ['device'], registry=r)
        # ── Network (Gauge — raw kernel cumulative counters read each cycle)
        self.net_rx      = Gauge('node_network_receive_bytes_total',    'Net RX bytes',    ['device'], registry=r)
        self.net_tx      = Gauge('node_network_transmit_bytes_total',   'Net TX bytes',    ['device'], registry=r)
        self.net_rx_err  = Gauge('node_network_receive_errs_total',     'Net RX errors',   ['device'], registry=r)
        self.net_tx_err  = Gauge('node_network_transmit_errs_total',    'Net TX errors',   ['device'], registry=r)
        self.net_rx_pkt  = Gauge('node_network_receive_packets_total',  'Net RX packets',  ['device'], registry=r)
        self.net_tx_pkt  = Gauge('node_network_transmit_packets_total', 'Net TX packets',  ['device'], registry=r)
        self.net_up      = Gauge('node_network_up', 'Interface is up', ['device'], registry=r)
        self.net_speed   = Gauge('node_network_speed_bytes', 'Interface speed', ['device'], registry=r)
        # ── Process
        self.procs_total   = Gauge('node_procs_total',   'Total processes', registry=r)
        self.procs_running = Gauge('node_procs_running', 'Running processes', registry=r)
        self.threads_total = Gauge('node_threads_total', 'Total threads', registry=r)
        # ── Sensors (conditional)
        if self.features['sensors']:
            self.temp_celsius  = Gauge('node_hwmon_temp_celsius',      'Temperature °C',  ['chip', 'sensor'], registry=r)
            self.temp_max      = Gauge('node_hwmon_temp_max_celsius',   'Max temp °C',     ['chip', 'sensor'], registry=r)
            self.temp_crit     = Gauge('node_hwmon_temp_crit_celsius',  'Crit temp °C',    ['chip', 'sensor'], registry=r)
            self.fan_rpm       = Gauge('node_hwmon_fan_rpm',            'Fan RPM',         ['chip', 'sensor'], registry=r)
        # ── ZFS (conditional)
        if self.features['zfs']:
            self.zfs_arc_size   = Gauge('node_zfs_arc_size_bytes',      'ZFS ARC size',        registry=r)
            self.zfs_arc_c      = Gauge('node_zfs_arc_c_bytes',         'ZFS ARC target',      registry=r)
            self.zfs_arc_c_max  = Gauge('node_zfs_arc_c_max_bytes',     'ZFS ARC max',         registry=r)
            self.zfs_arc_hits   = Gauge('node_zfs_arc_hits_total',   'ZFS ARC hits',   registry=r)
            self.zfs_arc_misses = Gauge('node_zfs_arc_misses_total', 'ZFS ARC misses', registry=r)
            self.zpool_size     = Gauge('node_zfs_zpool_size_bytes',    'ZFS pool size',       ['pool'], registry=r)
            self.zpool_free     = Gauge('node_zfs_zpool_free_bytes',    'ZFS pool free',       ['pool'], registry=r)
            self.zpool_alloc    = Gauge('node_zfs_zpool_allocated_bytes','ZFS pool allocated', ['pool'], registry=r)
            self.zpool_frag     = Gauge('node_zfs_zpool_fragmentation_percent', 'ZFS pool fragmentation %', ['pool'], registry=r)
            self.zpool_health   = Gauge('node_zfs_zpool_health',        'ZFS pool health (0=ONLINE, 1=DEGRADED, 2=FAULTED)',
                                        ['pool'], registry=r)
        # ── SMART (conditional)
        if self.features['smart']:
            self.smart_healthy  = Gauge('node_disk_smart_healthy',                  'SMART health (1=OK)', ['device', 'model'], registry=r)
            self.smart_temp     = Gauge('node_disk_smart_temperature_celsius',     'Disk temperature',    ['device', 'model'], registry=r)
            self.smart_hours    = Gauge('node_disk_smart_power_on_hours_total',    'Power-on hours',      ['device', 'model'], registry=r)
            self.smart_cycles   = Gauge('node_disk_smart_power_cycles_total',      'Power cycles',        ['device', 'model'], registry=r)
        # ── PVE VMs / LXC
        if self.features['qemu_vms'] or self.features['lxc_containers']:
            self.vm_count  = Gauge('pve_vm_count',  'VM/LXC count', ['type', 'status'], registry=r)
            self.vm_status = Gauge('pve_vm_status', 'VM status (1=running)', ['vmid', 'name', 'type'], registry=r)
            self.vm_cpu    = Gauge('pve_vm_cpu_usage_percent',    'VM CPU %',       ['vmid', 'name', 'type'], registry=r)
            self.vm_mem    = Gauge('pve_vm_memory_used_bytes',    'VM memory used', ['vmid', 'name', 'type'], registry=r)
            self.vm_maxmem = Gauge('pve_vm_memory_total_bytes',   'VM memory total',['vmid', 'name', 'type'], registry=r)
            self.vm_uptime = Gauge('pve_vm_uptime_seconds',       'VM uptime',      ['vmid', 'name', 'type'], registry=r)
        # ── PVE Virtual Disk allocation (ZFS zvols + subvols)
        if self.features['zfs']:
            self.disk_volsize = Gauge('pve_disk_volsize_bytes', 'ZFS virtual disk allocated size',
                                      ['vmid', 'disk', 'pool', 'type'], registry=r)
            self.disk_used    = Gauge('pve_disk_used_bytes',    'ZFS virtual disk physically used',
                                      ['vmid', 'disk', 'pool', 'type'], registry=r)

    # ──────────────────────────────────────────
    # Collectors
    # ──────────────────────────────────────────

    @timed(timeout=10)
    def collect_base(self):
        _t0 = time.time()
        if True:
            # ── Meta
            self.exporter_info.info({
                'version': self.VERSION,
                'hostname': self.hostname,
                'python': platform.python_version(),
            })
            for feat, enabled in self.features.items():
                self.feature_enabled.labels(feature=feat).set(1 if enabled else 0)

            # ── PVE version
            if shutil.which('pveversion'):
                try:
                    out = subprocess.run(['pveversion', '--verbose'],
                                         capture_output=True, text=True, timeout=2).stdout
                    for line in out.splitlines():
                        if line.startswith('pve-manager'):
                            ver = line.split('/')[1].split()[0] if '/' in line else 'unknown'
                            self.pve_version.info({'version': ver})
                            break
                except Exception:
                    pass

            # ── Boot time
            self.boot_time.set(psutil.boot_time())

            # ── Node info
            self.node_info.info({
                'hostname': self.hostname,
                'kernel': platform.release(),
                'arch': platform.machine(),
            })

            # ── CPU
            self.cpu_count.labels(type='logical').set(psutil.cpu_count(logical=True))
            self.cpu_count.labels(type='physical').set(psutil.cpu_count(logical=False) or 0)
            for i, times in enumerate(psutil.cpu_times(percpu=True)):
                cpu = f'cpu{i}'
                for mode in ('user', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal', 'guest'):
                    self.cpu_seconds.labels(cpu=cpu, mode=mode).set(getattr(times, mode, 0))
            pcts = psutil.cpu_percent(percpu=True, interval=None)
            for i, pct in enumerate(pcts):
                self.cpu_percent.labels(cpu=f'cpu{i}').set(pct)
            try:
                for i, f in enumerate(psutil.cpu_freq(percpu=True) or []):
                    cpu = f'cpu{i}'
                    self.cpu_freq.labels(cpu=cpu, type='current').set(f.current * 1e6)
                    self.cpu_freq.labels(cpu=cpu, type='min').set(f.min * 1e6)
                    self.cpu_freq.labels(cpu=cpu, type='max').set(f.max * 1e6)
            except Exception:
                pass
            load = os.getloadavg()
            self.load1.set(load[0]); self.load5.set(load[1]); self.load15.set(load[2])

            # ── Memory
            m = psutil.virtual_memory()
            self.mem_total.set(m.total); self.mem_free.set(m.free)
            self.mem_avail.set(m.available); self.mem_cached.set(getattr(m, 'cached', 0))
            self.mem_buffers.set(getattr(m, 'buffers', 0)); self.mem_shared.set(getattr(m, 'shared', 0))
            sw = psutil.swap_memory()
            self.swap_total.set(sw.total); self.swap_free.set(sw.free)

            # ── Filesystem
            for part in psutil.disk_partitions(all=False):
                if EXCLUDED_FSTYPES_RE.match(part.fstype or ''):
                    continue
                if EXCLUDED_MOUNTPOINTS_RE.match(part.mountpoint):
                    continue
                try:
                    u = psutil.disk_usage(part.mountpoint)
                    lbl = dict(device=part.device, mountpoint=part.mountpoint, fstype=part.fstype)
                    self.fs_size.labels(**lbl).set(u.total)
                    self.fs_free.labels(**lbl).set(u.free)
                    self.fs_avail.labels(**lbl).set(u.free)
                except Exception:
                    pass

            # ── Disk I/O (physical disks only — skip loop, ram, zram, zd*)
            io = psutil.disk_io_counters(perdisk=True, nowrap=True) or {}
            for dev, c in io.items():
                if re.match(r'^(loop|ram|zram|zd)', dev):
                    continue
                self.disk_read_bytes.labels(device=dev).set(c.read_bytes)
                self.disk_written_bytes.labels(device=dev).set(c.write_bytes)
                if hasattr(c, 'busy_time'):
                    self.disk_io_time.labels(device=dev).set(c.busy_time / 1000.0)

            # ── Network (skip lo unless DEBUG)
            net_io    = psutil.net_io_counters(pernic=True, nowrap=True) or {}
            net_stats = psutil.net_if_stats() or {}
            for iface, c in net_io.items():
                if iface == 'lo' and not DEBUG_MODE:
                    continue
                self.net_rx.labels(device=iface).set(c.bytes_recv)
                self.net_tx.labels(device=iface).set(c.bytes_sent)
                self.net_rx_err.labels(device=iface).set(c.errin)
                self.net_tx_err.labels(device=iface).set(c.errout)
                self.net_rx_pkt.labels(device=iface).set(c.packets_recv)
                self.net_tx_pkt.labels(device=iface).set(c.packets_sent)
                if iface in net_stats:
                    s = net_stats[iface]
                    self.net_up.labels(device=iface).set(1 if s.isup else 0)
                    self.net_speed.labels(device=iface).set(s.speed * 1e6 if s.speed > 0 else 0)

            # ── Processes
            total = running = threads = 0
            for proc in psutil.process_iter(['status', 'num_threads']):
                try:
                    total += 1
                    if proc.info['status'] == psutil.STATUS_RUNNING:
                        running += 1
                    threads += proc.info.get('num_threads', 1)
                except Exception:
                    pass
            self.procs_total.set(total)
            self.procs_running.set(running)
            self.threads_total.set(threads)

        self.collection_duration.labels(collector='base').set(time.time() - _t0)
        self.collection_success.labels(collector='base').set(1)

    @timed(timeout=5)
    def collect_sensors(self):
        """Collect hwmon temperatures and fan speeds via psutil + sysfs."""
        if not self.features['sensors']:
            return
        _t0 = time.time()
        if True:
            # psutil sensors
            if hasattr(psutil, 'sensors_temperatures'):
                for chip, sensors in (psutil.sensors_temperatures() or {}).items():
                    chip_name = chip.replace('-', '_')
                    for s in sensors:
                        sensor_name = (s.label or 'unknown').replace(' ', '_').replace('.', '_')
                        self.temp_celsius.labels(chip=chip_name, sensor=sensor_name).set(s.current)
                        if s.high and s.high > -273:
                            self.temp_max.labels(chip=chip_name, sensor=sensor_name).set(s.high)
                        if s.critical and s.critical > -273:
                            self.temp_crit.labels(chip=chip_name, sensor=sensor_name).set(s.critical)
            if hasattr(psutil, 'sensors_fans'):
                for chip, fans in (psutil.sensors_fans() or {}).items():
                    chip_name = chip.replace('-', '_')
                    for fan in fans:
                        self.fan_rpm.labels(chip=chip_name, sensor=fan.label or 'unknown').set(fan.current)

            # also walk /sys/class/hwmon for any chips psutil misses
            hwmon_base = '/sys/class/hwmon'
            if os.path.exists(hwmon_base):
                for hwmon_dir in glob.glob(os.path.join(hwmon_base, 'hwmon*')):
                    name_f = os.path.join(hwmon_dir, 'name')
                    if not os.path.exists(name_f):
                        continue
                    with open(name_f) as f:
                        chip = f.read().strip()
                    for temp_f in glob.glob(os.path.join(hwmon_dir, 'temp*_input')):
                        try:
                            num = re.search(r'temp(\d+)_input', temp_f).group(1)
                            label_f = temp_f.replace('_input', '_label')
                            label = f'temp{num}'
                            if os.path.exists(label_f):
                                with open(label_f) as f:
                                    label = f.read().strip().replace(' ', '_')
                            with open(temp_f) as f:
                                self.temp_celsius.labels(chip=chip, sensor=label).set(float(f.read()) / 1000.0)
                        except Exception:
                            pass
                    for fan_f in glob.glob(os.path.join(hwmon_dir, 'fan*_input')):
                        try:
                            num = re.search(r'fan(\d+)_input', fan_f).group(1)
                            label_f = fan_f.replace('_input', '_label')
                            label = f'fan{num}'
                            if os.path.exists(label_f):
                                with open(label_f) as f:
                                    label = f.read().strip()
                            with open(fan_f) as f:
                                self.fan_rpm.labels(chip=chip, sensor=label).set(float(f.read()))
                        except Exception:
                            pass
        self.collection_duration.labels(collector='sensors').set(time.time() - _t0)
        self.collection_success.labels(collector='sensors').set(1)

    @timed(timeout=10)
    def collect_zfs(self):
        """Collect ZFS ARC and pool capacity metrics."""
        if not self.features['zfs']:
            return
        _t0 = time.time()
        if True:
            # ARC from /proc/spl/kstat/zfs/arcstats
            arc_path = '/proc/spl/kstat/zfs/arcstats'
            if os.path.exists(arc_path):
                arc = {}
                with open(arc_path) as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) == 3:
                            arc[parts[0]] = int(parts[2])
                if 'size' in arc:
                    self.zfs_arc_size.set(arc['size'])
                if 'c' in arc:
                    self.zfs_arc_c.set(arc['c'])
                if 'c_max' in arc:
                    self.zfs_arc_c_max.set(arc['c_max'])
                hits   = arc.get('hits', 0)
                misses = arc.get('misses', 0)
                self.zfs_arc_hits.set(hits)
                self.zfs_arc_misses.set(misses)

            # Pool capacity via `zpool list -Hp`
            # Output: name  size  alloc  free  ckpoint  expandsz  frag  cap  dedup  health  altroot
            result = subprocess.run(
                ['zpool', 'list', '-Hp', '-o', 'name,size,alloc,free,frag,health'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    cols = line.split('\t')
                    if len(cols) < 6:
                        continue
                    name, size_s, alloc_s, free_s, frag_s, health = cols
                    try:
                        self.zpool_size.labels(pool=name).set(int(size_s))
                        self.zpool_alloc.labels(pool=name).set(int(alloc_s))
                        self.zpool_free.labels(pool=name).set(int(free_s))
                        frag = float(frag_s.rstrip('%')) if frag_s not in ('-', '') else 0
                        self.zpool_frag.labels(pool=name).set(frag)
                        health_map = {'ONLINE': 0, 'DEGRADED': 1, 'FAULTED': 2,
                                      'OFFLINE': 3, 'UNAVAIL': 4, 'REMOVED': 5}
                        self.zpool_health.labels(pool=name).set(health_map.get(health.strip(), 9))
                    except (ValueError, TypeError):
                        pass
        self.collection_duration.labels(collector='zfs').set(time.time() - _t0)
        self.collection_success.labels(collector='zfs').set(1)

    @timed(timeout=30)
    def collect_smart(self):
        """Collect SMART health, temperature, power-on hours, and power cycles."""
        if not self.features['smart']:
            return
        _t0 = time.time()
        if True:
            # Enumerate physical block devices (skip zd* zvols, loop, ram)
            devices = []
            for dev in glob.glob('/dev/sd?') + glob.glob('/dev/nvme?'):
                devices.append(dev)

            for dev in devices:
                dev_name = os.path.basename(dev)
                try:
                    result = subprocess.run(
                        ['smartctl', '-j', '-a', dev],
                        capture_output=True, text=True, timeout=10
                    )
                    data = json.loads(result.stdout)
                    model = data.get('model_name', data.get('device', {}).get('name', 'unknown'))

                    # Overall health
                    passed = data.get('smart_status', {}).get('passed', False)
                    self.smart_healthy.labels(device=dev_name, model=model).set(1 if passed else 0)

                    # Temperature
                    temp = data.get('temperature', {}).get('current', None)
                    if temp is not None:
                        self.smart_temp.labels(device=dev_name, model=model).set(temp)

                    # Power-on hours
                    hours = data.get('power_on_time', {}).get('hours', None)
                    if hours is not None:
                        self.smart_hours.labels(device=dev_name, model=model).set(hours)

                    # Power cycle count
                    for attr in data.get('ata_smart_attributes', {}).get('table', []):
                        if attr.get('id') == 12:  # ID 12 = Power Cycle Count
                            self.smart_cycles.labels(device=dev_name, model=model).set(
                                attr.get('raw', {}).get('value', 0))
                            break
                except Exception as exc:
                    logger.debug("SMART %s: %s", dev, exc)
        self.collection_duration.labels(collector='smart').set(time.time() - _t0)
        self.collection_success.labels(collector='smart').set(1)

    @timed(timeout=15)
    def collect_vms(self):
        """Collect QEMU VM and LXC container metrics via qm/pct."""
        if not (self.features['qemu_vms'] or self.features['lxc_containers']):
            return
        _t0 = time.time()
        if True:
            counts: dict = defaultdict(lambda: defaultdict(int))

            feature_key = {'qemu': 'qemu_vms', 'lxc': 'lxc_containers'}
            for vm_type, cmd in [('qemu', 'qm'), ('lxc', 'pct')]:
                if not self.features.get(feature_key[vm_type]):
                    continue
                result = subprocess.run([cmd, 'list'], capture_output=True, text=True, timeout=5)
                if result.returncode != 0:
                    continue
                for line in result.stdout.strip().splitlines()[1:]:  # skip header
                    cols = line.split()
                    if not cols:
                        continue
                    vmid   = cols[0]
                    name   = cols[1] if len(cols) > 1 else vmid
                    status = cols[2] if len(cols) > 2 else 'unknown'
                    counts[vm_type][status] += 1
                    self.vm_status.labels(vmid=vmid, name=name, type=vm_type).set(
                        1 if status == 'running' else 0)

                    # Detailed stats for running VMs
                    if status == 'running':
                        try:
                            stat_result = subprocess.run(
                                [cmd, 'status', vmid, '--verbose'],
                                capture_output=True, text=True, timeout=3)
                            cpu_pct = uptime = mem_used = mem_max = 0
                            for sline in stat_result.stdout.splitlines():
                                kv = sline.split(':', 1)
                                if len(kv) != 2:
                                    continue
                                k, v = kv[0].strip().lower(), kv[1].strip()
                                if 'cpu' in k and '%' in v:
                                    cpu_pct = float(v.rstrip('%'))
                                elif 'uptime' in k:
                                    uptime = int(v.split()[0]) if v.split() else 0
                                elif k in ('mem', 'memory') and '/' in v:
                                    mem_parts = v.split('/')
                                    mem_used = self._parse_mem(mem_parts[0])
                                    mem_max  = self._parse_mem(mem_parts[1])
                            self.vm_cpu.labels(vmid=vmid, name=name, type=vm_type).set(cpu_pct)
                            self.vm_uptime.labels(vmid=vmid, name=name, type=vm_type).set(uptime)
                            self.vm_mem.labels(vmid=vmid, name=name, type=vm_type).set(mem_used)
                            self.vm_maxmem.labels(vmid=vmid, name=name, type=vm_type).set(mem_max)
                        except Exception:
                            pass

            for vm_type, statuses in counts.items():
                for status, count in statuses.items():
                    self.vm_count.labels(type=vm_type, status=status).set(count)
        self.collection_duration.labels(collector='vms').set(time.time() - _t0)
        self.collection_success.labels(collector='vms').set(1)

    @timed(timeout=10)
    def collect_vm_disks(self):
        """Expose per-VM disk allocation and physical usage from ZFS zvols/subvols."""
        if not self.features['zfs']:
            return
        _t0 = time.time()
        # zfs list -t volume,filesystem: covers zvols (QEMU) and datasets (LXC)
        # Columns: name  volsize  used  refer
        result = subprocess.run(
            ['zfs', 'list', '-t', 'volume,filesystem', '-Hp',
             '-o', 'name,volsize,used,refer'],
            capture_output=True, text=True, timeout=8
        )
        if result.returncode != 0:
            return
        # Patterns: pool/vm-<vmid>-disk-<N>   (QEMU zvol)
        #           pool/subvol-<vmid>-disk-<N> (LXC subvol)
        qemu_re = re.compile(r'^([^/]+)/vm-([0-9]+)-(disk-[0-9]+)$')
        lxc_re  = re.compile(r'^([^/]+)/subvol-([0-9]+)-(disk-[0-9]+)$')
        for line in result.stdout.strip().splitlines():
            cols = line.split('\t')
            if len(cols) < 4:
                continue
            name, volsize_s, used_s, refer_s = cols
            for pattern, vm_type in [(qemu_re, 'qemu'), (lxc_re, 'lxc')]:
                m = pattern.match(name)
                if not m:
                    continue
                pool, vmid, disk = m.group(1), m.group(2), m.group(3)
                try:
                    # volsize is '-' for filesystems (LXC); fall back to refer for used space
                    volsize = int(volsize_s) if volsize_s not in ('-', 'none', '') else 0
                    used    = int(used_s)    if used_s    not in ('-', 'none', '') else 0
                    if volsize > 0:
                        self.disk_volsize.labels(vmid=vmid, disk=disk, pool=pool, type=vm_type).set(volsize)
                    self.disk_used.labels(vmid=vmid, disk=disk, pool=pool, type=vm_type).set(used)
                except (ValueError, TypeError):
                    pass
        self.collection_duration.labels(collector='vm_disks').set(time.time() - _t0)
        self.collection_success.labels(collector='vm_disks').set(1)

    @staticmethod
    def _parse_mem(s: str) -> int:
        """Parse '1.5 GiB' / '512 MiB' / '1024 MB' to bytes."""
        s = s.strip()
        units = {'B': 1, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3,
                 'KB': 1000, 'MB': 1000**2, 'GB': 1000**3}
        for unit, mult in sorted(units.items(), key=lambda x: -len(x[0])):
            if unit in s:
                try:
                    return int(float(s.replace(unit, '').strip()) * mult)
                except ValueError:
                    pass
        try:
            return int(float(s))
        except ValueError:
            return 0

    # ──────────────────────────────────────────
    # Collection orchestration
    # ──────────────────────────────────────────

    def collect_all(self):
        collectors = [
            ('base',     self.collect_base),
            ('sensors',  self.collect_sensors),
            ('zfs',      self.collect_zfs),
            ('smart',    self.collect_smart),
            ('vms',      self.collect_vms),
            ('vm_disks', self.collect_vm_disks),
        ]

        if PARALLEL_COLLECTORS and self.executor:
            futures = {self.executor.submit(fn): name for name, fn in collectors}
            for fut, name in futures.items():
                try:
                    fut.result(timeout=35)
                except Exception as exc:
                    logger.error("Collector %s failed: %s", name, exc)
        else:
            for name, fn in collectors:
                try:
                    fn()
                except Exception as exc:
                    logger.error("Collector %s failed: %s", name, exc)

        self.cache.clear_expired()

    # ──────────────────────────────────────────
    # Main loop
    # ──────────────────────────────────────────

    def run(self):
        start_http_server(EXPORTER_PORT, registry=self.registry)
        logger.info("Starhaven Exporter listening on http://0.0.0.0:%d/metrics", EXPORTER_PORT)

        # Initial collection
        self.collect_all()

        while True:
            try:
                time.sleep(COLLECTION_INTERVAL)
                self.collect_all()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.error("Collection loop error: %s", exc)
                time.sleep(5)

        if self.executor:
            self.executor.shutdown(wait=False)


# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────

def _signal_handler(signum, _frame):
    logger.info("Signal %d received — exiting", signum)
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if os.geteuid() != 0:
        logger.warning("Not running as root — some metrics (SMART, sensors, PVE) may be missing")

    StarheavenExporter().run()


if __name__ == '__main__':
    main()
