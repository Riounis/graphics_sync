"""Pure decision logic: turn a SystemInfo snapshot into a Diagnosis. No I/O here."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .system import FlatpakRef, FlatpakState, Kernel, NvidiaDriver, SystemInfo

NVIDIA_GL_RE = re.compile(r"^org\.freedesktop\.Platform\.GL(32)?\.nvidia-(\d+(?:-\d+)+)$")


class Status(str, Enum):
    OK = "ok"
    NO_GPU = "no-gpu"
    NOT_NVIDIA = "not-nvidia"
    NO_DRIVER = "no-driver"
    MISSING_MODULE = "missing-module"    # running kernel has no nvidia.ko at all
    MODULE_MISMATCH = "module-mismatch"  # nvidia.ko exists but is a different version than userspace
    STALE_LOADED = "stale-loaded"        # driver updated on disk, old module still loaded -> reboot
    NOT_LOADED = "not-loaded"            # matching module installed but not loaded


HEALTHY = {Status.OK, Status.NO_GPU, Status.NOT_NVIDIA}


@dataclass
class Diagnosis:
    status: Status
    summary: str
    warnings: list[str] = field(default_factory=list)
    good_kernels: list[Kernel] = field(default_factory=list)  # bootable + matching module, newest first
    broken_newest: Kernel | None = None  # newest installed kernel, if it lacks a matching module

    @property
    def healthy(self) -> bool:
        return self.status in HEALTHY


def stale_grub_pin(info: SystemInfo) -> str | None:
    """Kernel version GRUB_DEFAULT is pinned to, if that kernel is no longer installed."""
    m = re.search(r"with Linux ([\w.+~-]+)", info.grub.default)
    if m and m[1] not in {k.version for k in info.kernels if k.bootable}:
        return m[1]
    return None


def module_matches(kernel: Kernel, userspace: str) -> bool:
    return kernel.nvidia_module == userspace


def diagnose(info: SystemInfo) -> Diagnosis:
    warnings = []
    pin = stale_grub_pin(info)
    if pin:
        warnings.append(
            f"GRUB_DEFAULT in /etc/default/grub pins kernel {pin}, which is no longer installed, so GRUB "
            "silently boots the newest kernel instead. Set GRUB_DEFAULT=0 (or an installed kernel) "
            "and run: sudo update-grub")

    if not info.gpus:
        return Diagnosis(Status.NO_GPU, "No display controller found on the PCI bus.", warnings)
    nvidia_gpus = [g for g in info.gpus if g.vendor == "NVIDIA"]
    if not nvidia_gpus:
        vendors = " + ".join(sorted({g.vendor for g in info.gpus}))
        return Diagnosis(Status.NOT_NVIDIA, f"{vendors} graphics use the in-kernel driver and Mesa, which ship "
                                            "with every kernel, so there is nothing to keep in sync.", warnings)

    nv = info.nvidia
    if nv is None:
        bound = nvidia_gpus[0].bound_driver or "no driver"
        return Diagnosis(Status.NO_DRIVER, f"No proprietary NVIDIA driver package is installed "
                                           f"(the GPU is using: {bound}).", warnings)

    u = nv.userspace_version
    good = [k for k in info.kernels if k.bootable and module_matches(k, u)]
    running = info.running_kernel()
    newest = next((k for k in info.kernels if k.bootable), None)
    broken_newest = newest if newest and not module_matches(newest, u) else None

    if broken_newest and broken_newest.version != running.version:
        boots_newest = info.grub.default == "0" or pin is not None
        warnings.append(
            f"The newest kernel, {broken_newest.version}, has "
            f"{'no NVIDIA module' if broken_newest.nvidia_module is None else 'NVIDIA module ' + broken_newest.nvidia_module}"
            f" (userspace is {u}). "
            + ("GRUB boots it by default, so your next reboot will lose graphics." if boots_newest
               else "Booting it will lose graphics."))

    if info.session_type == "wayland" and info.nvidia_drm_modeset is False:
        warnings.append("Plasma is running on Wayland but nvidia-drm.modeset is off; add "
                        "nvidia-drm.modeset=1 to GRUB_CMDLINE_LINUX_DEFAULT.")

    def result(status: Status, summary: str) -> Diagnosis:
        return Diagnosis(status, summary, warnings, good, broken_newest)

    if running.nvidia_module is None:
        return result(Status.MISSING_MODULE,
                      f"Kernel {running.version} has no NVIDIA {nv.branch}{nv.flavor} module installed, "
                      "so the driver cannot load (you are on a fallback framebuffer).")
    if not module_matches(running, u):
        return result(Status.MODULE_MISMATCH,
                      f"Kernel {running.version} has NVIDIA module {running.nvidia_module}, "
                      f"but the userspace driver is {u}; they must match exactly.")
    if nv.loaded_version and nv.loaded_version != u:
        return result(Status.STALE_LOADED,
                      f"The loaded NVIDIA module is {nv.loaded_version} but {u} is installed. The driver "
                      "was updated without a reboot ('Driver/library version mismatch').")
    if nv.loaded_version is None:
        return result(Status.NOT_LOADED,
                      f"A matching NVIDIA module ({u}) is installed for {running.version}, but it is not loaded.")
    return result(Status.OK, f"Kernel {running.version} is running NVIDIA {u} with matching userspace.")


def flatpak_gl_id(version: str, gl32: bool = False) -> str:
    """595.99.02 -> org.freedesktop.Platform.GL.nvidia-595-99-02 (GL32 for 32-bit apps like Steam)."""
    return f"org.freedesktop.Platform.GL{'32' if gl32 else ''}.nvidia-{version.replace('.', '-')}"


def nvidia_gl_version(ref: FlatpakRef) -> str | None:
    m = NVIDIA_GL_RE.match(ref.app_id)
    return m[2].replace("-", ".") if m else None


@dataclass
class FlatpakNeed:
    ref: str            # runtime/org.freedesktop.Platform.GL.nvidia-595-99-02/x86_64/1.4
    installation: str
    origin: str


@dataclass
class FlatpakDiagnosis:
    version: str                                              # driver version the extension must match
    installed: list[FlatpakRef] = field(default_factory=list)  # NVIDIA GL extensions present
    missing: list[FlatpakNeed] = field(default_factory=list)
    stale: list[FlatpakRef] = field(default_factory=list)      # match no installed or loaded driver

    @property
    def healthy(self) -> bool:
        return not self.missing


def diagnose_flatpak(fp: FlatpakState, nv: NvidiaDriver) -> FlatpakDiagnosis:
    """Flatpak apps get the host NVIDIA userspace via a GL extension whose version must match exactly."""
    version = nv.userspace_version
    in_use = {version, nv.loaded_version}  # the loaded one still serves apps until the next reboot
    d = FlatpakDiagnosis(version)
    for r in fp.runtimes:
        v = nvidia_gl_version(r)
        if v:
            d.installed.append(r)
            if v not in in_use:
                d.stale.append(r)

    for inst in fp.app_installations:
        here = [r for r in fp.runtimes if r.installation == inst]
        nv_here = [r for r in here if nvidia_gl_version(r)]
        template = nv_here[0] if nv_here else None
        branch = template.branch if template else "1.4"
        origin = template.origin if template else next((r.origin for r in here), "flathub")
        wanted = [(flatpak_gl_id(version), fp.arch)]
        if any(r.app_id.startswith("org.freedesktop.Platform.GL32.") for r in here):
            wanted.append((flatpak_gl_id(version, gl32=True), "i386"))
        for app_id, arch in wanted:
            if not any(r.app_id == app_id and r.arch == arch for r in here):
                d.missing.append(FlatpakNeed(f"runtime/{app_id}/{arch}/{branch}", inst, origin))
    return d
