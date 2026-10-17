"""Read-only probes of the running system. Nothing in here needs root."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PCI_VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}
DRIVER_PKG_RE = re.compile(r"^nvidia-driver-(\d+)((?:-server)?(?:-open)?)$")


def run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    """Run a command and return (returncode, stdout). A missing binary returns 127."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, ""
    return proc.returncode, proc.stdout


def read(path: str | Path) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def kernel_key(version: str) -> tuple[int, ...]:
    """Sort key so that 7.0.0-38-generic > 7.0.0-34-generic > 6.17.0-40-generic."""
    return tuple(int(n) for n in re.findall(r"\d+", version))


def upstream_version(deb_version: str) -> str:
    """'1:595.91.07-0ubuntu0.24.04.1' -> '595.91.07'"""
    return re.sub(r"^\d+:", "", deb_version).split("-")[0]


def installed_packages(*patterns: str) -> dict[str, str]:
    """Fully installed ('ii') packages matching dpkg glob patterns -> version."""
    _, out = run(["dpkg-query", "-W", "-f=${db:Status-Abbrev}\t${Package}\t${Version}\n", *patterns])
    pkgs = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("ii"):
            pkgs[parts[1]] = parts[2]
    return pkgs


@dataclass
class Gpu:
    slot: str
    vendor: str
    pci_id: str
    name: str
    bound_driver: str | None


@dataclass
class Kernel:
    version: str
    bootable: bool              # /boot/vmlinuz-<version> exists
    nvidia_module: str | None   # version of the nvidia.ko installed for this kernel


@dataclass
class NvidiaDriver:
    package: str                # nvidia-driver-595-open
    branch: str                 # 595
    flavor: str                 # "", "-open", "-server" or "-server-open"
    userspace_version: str      # 595.91.07 (libnvidia-gl, what the module must match)
    loaded_version: str | None  # version of the module currently in the kernel
    dkms_package: str | None    # nvidia-dkms-595-open, if modules are built locally
    modules_meta: str | None    # linux-modules-nvidia-595-open-generic-hwe-24.04


@dataclass
class Grub:
    default: str
    distributor: str
    timeout: str
    timeout_style: str
    uefi: bool

    @property
    def menu_hidden(self) -> bool:
        try:
            no_timeout = float(self.timeout) <= 0
        except ValueError:
            no_timeout = False
        return self.timeout_style == "hidden" or no_timeout


@dataclass
class FlatpakRef:
    app_id: str          # org.freedesktop.Platform.GL.nvidia-595-91-07
    arch: str
    branch: str
    installation: str    # "user" or "system"
    origin: str          # remote, e.g. flathub

    @property
    def ref(self) -> str:
        return f"runtime/{self.app_id}/{self.arch}/{self.branch}"


@dataclass
class FlatpakState:
    arch: str                          # flatpak --default-arch
    runtimes: list[FlatpakRef]
    app_installations: list[str]       # installations that have apps, which need the GL extension
    remote_urls: dict[str, str]        # "user/flathub" -> https://dl.flathub.org/repo/


@dataclass
class SystemInfo:
    os_name: str
    os_version_id: str
    kernel: str
    plasma: str | None
    session_type: str
    secure_boot: bool | None
    gpus: list[Gpu]
    kernels: list[Kernel]       # newest first, always includes the running kernel
    nvidia: NvidiaDriver | None
    grub: Grub
    nvidia_drm_modeset: bool | None
    flatpak: FlatpakState | None = None   # None if flatpak isn't installed

    def running_kernel(self) -> Kernel:
        return next((k for k in self.kernels if k.version == self.kernel), Kernel(self.kernel, False, None))


def os_release() -> dict[str, str]:
    data = {}
    for line in (read("/etc/os-release") or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key] = value.strip('"')
    return data


def gpu_name(slot: str) -> str:
    _, out = run(["lspci", "-nn", "-s", slot])
    # "NVIDIA Corporation TU116 [GeForce GTX 1660] [10de:2184] (rev a1)" -> "GeForce GTX 1660"
    m = re.search(r"\[([^\]]+)\]\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]", out)
    if m:
        return m[1]
    desc = out.split(": ", 1)[-1]
    return re.sub(r"\s*\[[0-9a-f]{4}:[0-9a-f]{4}\].*", "", desc).strip() or "unknown"


def detect_gpus() -> list[Gpu]:
    gpus = []
    for dev in sorted(Path("/sys/bus/pci/devices").glob("*")):
        if not (read(dev / "class") or "").startswith("0x03"):
            continue
        vendor_id = read(dev / "vendor") or ""
        device_id = read(dev / "device") or ""
        driver = dev / "driver"
        bound = os.path.basename(os.readlink(driver)) if driver.is_symlink() else None
        gpus.append(Gpu(dev.name, PCI_VENDORS.get(vendor_id, vendor_id),
                        f"{vendor_id[2:]}:{device_id[2:]}", gpu_name(dev.name), bound))
    return gpus


def detect_kernels(running: str) -> list[Kernel]:
    versions = {p.name.removeprefix("vmlinuz-") for p in Path("/boot").glob("vmlinuz-*")}
    versions.add(running)
    kernels = []
    for version in sorted(versions, key=kernel_key, reverse=True):
        rc, out = run(["modinfo", "-k", version, "-F", "version", "nvidia"])
        module = out.strip() if rc == 0 and out.strip() else None
        kernels.append(Kernel(version, Path(f"/boot/vmlinuz-{version}").exists(), module))
    return kernels


def detect_nvidia() -> NvidiaDriver | None:
    drivers = {}
    for pkg, version in installed_packages("nvidia-driver-*").items():
        m = DRIVER_PKG_RE.match(pkg)
        if m:
            drivers[pkg] = (m[1], m[2], version)
    if not drivers:
        return None
    package = max(drivers, key=lambda p: int(drivers[p][0]))
    branch, flavor, driver_version = drivers[package]
    server = "-server" if "server" in flavor else ""
    gl = installed_packages(f"libnvidia-gl-{branch}{server}")
    userspace = upstream_version(next(iter(gl.values()), driver_version))
    dkms = next(iter(installed_packages(f"nvidia-dkms-{branch}{flavor}")), None)
    meta = next(iter(installed_packages(f"linux-modules-nvidia-{branch}{flavor}-generic*")), None)
    return NvidiaDriver(package, branch, flavor, userspace, read("/sys/module/nvidia/version"), dkms, meta)


def detect_grub(os_name: str) -> Grub:
    conf = {}
    for f in [Path("/etc/default/grub"), *sorted(Path("/etc/default/grub.d").glob("*.cfg"))]:
        for line in (read(f) or "").splitlines():
            m = re.match(r"^\s*(GRUB_\w+)=(.*)$", line)
            if m:
                conf[m[1]] = m[2].strip().strip("'\"")
    default = conf.get("GRUB_DEFAULT", "0")
    distributor = conf.get("GRUB_DISTRIBUTOR", "")
    if not distributor or "$" in distributor or "`" in distributor:
        # A shell expression (Ubuntu's default) - infer the menu title instead.
        m = re.match(r"Advanced options for (.+?)>", default)
        distributor = m[1] if m else os_name
    return Grub(default, distributor, conf.get("GRUB_TIMEOUT", "10"),
                conf.get("GRUB_TIMEOUT_STYLE", "menu"), Path("/sys/firmware/efi").is_dir())


def detect_plasma() -> str | None:
    _, out = run(["plasmashell", "--version"])
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", out)
    return m[1] if m else None


def detect_secure_boot() -> bool | None:
    _, out = run(["mokutil", "--sb-state"])
    if "enabled" in out:
        return True
    if "disabled" in out:
        return False
    return None


def detect_flatpak() -> FlatpakState | None:
    rc, arch = run(["flatpak", "--default-arch"])
    if rc != 0:
        return None
    _, out = run(["flatpak", "list", "--runtime", "--columns=application,arch,branch,installation,origin"])
    runtimes = [FlatpakRef(*parts) for parts in (line.split("\t") for line in out.splitlines()) if len(parts) == 5]
    _, out = run(["flatpak", "list", "--app", "--columns=installation"])
    apps = sorted({line.strip() for line in out.splitlines() if line.strip()})
    _, out = run(["flatpak", "remotes", "--columns=name,url,options"])
    urls = {}
    for parts in (line.split("\t") for line in out.splitlines()):
        if len(parts) == 3:
            installation = "user" if "user" in parts[2].split(",") else "system"
            urls[f"{installation}/{parts[0]}"] = parts[1]
    return FlatpakState(arch.strip(), runtimes, apps, urls)


def detect() -> SystemInfo:
    osr = os_release()
    kernel = os.uname().release
    modeset = read("/sys/module/nvidia_drm/parameters/modeset")
    return SystemInfo(
        os_name=osr.get("PRETTY_NAME", "Linux"),
        os_version_id=osr.get("VERSION_ID", ""),
        kernel=kernel,
        plasma=detect_plasma(),
        session_type=os.environ.get("XDG_SESSION_TYPE", "unknown"),
        secure_boot=detect_secure_boot(),
        gpus=detect_gpus(),
        kernels=detect_kernels(kernel),
        nvidia=detect_nvidia(),
        grub=detect_grub(osr.get("NAME", "Ubuntu")),
        nvidia_drm_modeset=None if modeset is None else modeset == "Y",
        flatpak=detect_flatpak(),
    )
