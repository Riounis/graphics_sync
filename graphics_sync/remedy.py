"""Work out how to fix a Diagnosis: install a matching module, or boot a kernel that already has one."""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .diagnose import FlatpakDiagnosis, FlatpakNeed
from .system import FlatpakState, Kernel, NvidiaDriver, SystemInfo, run, upstream_version


@dataclass
class Plan:
    title: str
    command: list[str]
    changes: list[str] = field(default_factory=list)        # human-readable package changes
    download_urls: list[str] = field(default_factory=list)  # empty = everything is in the apt cache
    note: str = ""
    new_userspace: str | None = None  # NVIDIA userspace version after this plan, if it changes it

    @property
    def offline(self) -> bool:
        return not self.download_urls


def apt_candidate(pkg: str) -> str | None:
    _, out = run(["apt-cache", "policy", pkg])
    m = re.search(r"Candidate:\s*(\S+)", out)
    return m[1] if m and m[1] != "(none)" else None


def summarize_simulation(sim: str) -> list[str]:
    changes = []
    for line in sim.splitlines():
        m = re.match(r"^Inst (\S+) (?:\[(\S+)\] )?\((\S+)", line)
        if m:
            name, old, new = m[1], m[2], m[3]
            if old and upstream_version(old) == upstream_version(new):
                changes.append(f"{name}  {old} -> {new}")  # e.g. linux-modules-*: only the revision differs
            elif old:
                changes.append(f"{name}  {upstream_version(old)} -> {upstream_version(new)}")
            else:
                changes.append(f"{name}  (new) {new}")
        elif line.startswith("Remv "):
            changes.append(f"{line.split()[1]}  (remove)")
    return changes


def install_plan(pkgs: list[str], title: str, note: str = "") -> Plan | None:
    """Simulate `apt-get install pkgs`; None if apt can't satisfy it."""
    rc, sim = run(["apt-get", "-s", "install", *pkgs])
    if rc != 0:
        return None
    _, uris = run(["apt-get", "--print-uris", "-qq", "install", *pkgs])
    urls = [m[1] for m in re.finditer(r"^'([^']+)'", uris, re.M)]
    gl = re.search(r"^Inst libnvidia-gl-\d+(?:-server)?(?::\w+)? (?:\[\S+\] )?\((\S+)", sim, re.M)
    return Plan(title, ["sudo", "apt-get", "install", *pkgs], summarize_simulation(sim), urls, note,
                upstream_version(gl[1]) if gl else None)


def apt_mirror_url() -> str:
    """First http(s) mirror from the apt sources, for a quick 'are we online?' check."""
    for f in [Path("/etc/apt/sources.list"), *sorted(Path("/etc/apt/sources.list.d").glob("*"))]:
        try:
            m = re.search(r"(https?://\S+)", f.read_text())
        except OSError:
            continue
        if m:
            return m[1]
    return "http://archive.ubuntu.com/ubuntu"


def reachable(urls: list[str], timeout: float = 3) -> bool:
    url = urlparse(urls[0])
    if url.scheme not in ("http", "https"):
        return True
    try:
        socket.create_connection((url.hostname, url.port or (443 if url.scheme == "https" else 80)), timeout).close()
        return True
    except OSError:
        return False


def meta_suffix(info: SystemInfo) -> str:
    """'generic-hwe-24.04' from linux-modules-nvidia-595-open-generic-hwe-24.04."""
    nv = info.nvidia
    if nv.modules_meta:
        return nv.modules_meta.removeprefix(f"linux-modules-nvidia-{nv.branch}{nv.flavor}-")
    return f"generic-hwe-{info.os_version_id}"


def newer_branches(nv: NvidiaDriver, kver: str) -> list[str]:
    """Driver branches newer than the installed one (same flavor) that ship a module for kver."""
    _, out = run(["apt-cache", "search", "--names-only",
                  f"^linux-modules-nvidia-[0-9]+{re.escape(nv.flavor)}-{re.escape(kver)}$"])
    branches = {m[1] for m in re.finditer(rf"^linux-modules-nvidia-(\d+){re.escape(nv.flavor)}-{re.escape(kver)} ",
                                         out, re.M)}
    return sorted((b for b in branches if int(b) > int(nv.branch)), key=int, reverse=True)


def plan_module_fix(info: SystemInfo, kver: str) -> Plan | None:
    """Best way to get a matching NVIDIA module for kernel kver, in order of least disruption."""
    nv = info.nvidia
    flavor = f"{nv.branch}{nv.flavor}"

    # 1. Ubuntu's prebuilt, signed module for this kernel, from the same driver branch.
    #    Installing the metapackage too keeps future kernel updates in sync.
    pkg = f"linux-modules-nvidia-{flavor}-{kver}"
    if apt_candidate(pkg):
        meta = nv.modules_meta or f"linux-modules-nvidia-{flavor}-{meta_suffix(info)}"
        pkgs = [pkg] + ([meta] if apt_candidate(meta) else [])  # the meta may vanish after a release upgrade
        plan = install_plan(pkgs, f"Install the NVIDIA {flavor} kernel module built for {kver}",
                            "apt may also update the NVIDIA userspace libraries so they match the new module. "
                            "Older kernels' modules will then no longer match, which is expected: boot the newest kernel.")
        if plan:
            return plan

    # 2. Locally built module via DKMS (needs the kernel headers, but no download).
    if nv.dkms_package and Path(f"/lib/modules/{kver}/build").is_dir():
        return Plan(f"Rebuild the NVIDIA module for {kver} with DKMS ({nv.dkms_package})",
                    ["sudo", "dkms", "autoinstall", "-k", kver])

    # 3. Switch to a newer driver branch that does have a module for this kernel.
    for branch in newer_branches(nv, kver):
        new = f"{branch}{nv.flavor}"
        pkgs = [f"nvidia-driver-{new}", f"linux-modules-nvidia-{new}-{kver}"]
        meta = f"linux-modules-nvidia-{new}-{meta_suffix(info)}"
        if apt_candidate(meta):
            pkgs.append(meta)
        plan = install_plan(pkgs, f"Switch from driver {flavor} to the newer {new} branch (has a module for {kver})",
                            f"This replaces the {flavor} driver packages with {new}.")
        if plan:
            return plan
    return None


def grub_entry(info: SystemInfo, kernel: Kernel) -> tuple[str, str]:
    dist = info.grub.distributor
    return f"Advanced options for {dist}", f"{dist}, with Linux {kernel.version}"


def reboot_instructions(info: SystemInfo, target: Kernel, next_boot: bool = False) -> list[str]:
    """GRUB steps to boot target. next_boot: we're already on target and must stay on it at the next boot."""
    g = info.grub
    submenu, entry = grub_entry(info, target)
    steps = [] if next_boot else ["Reboot the computer."]
    if g.menu_hidden:
        key = "tap Esc (once or twice, don't hold it)" if g.uefi else "hold down Left Shift"
        steps.append(f"As soon as the motherboard logo disappears, {key} to open the GRUB menu. "
                     f"It is normally hidden (GRUB_TIMEOUT_STYLE={g.timeout_style}, GRUB_TIMEOUT={g.timeout}).")
        if g.uefi:
            steps.append("If you get a bare 'grub>' prompt instead (Esc pressed too many times), type  normal  "
                         "and press Enter, then tap Esc once.")
    else:
        steps.append("When the GRUB menu appears, press an arrow key to stop the countdown.")
    steps += [f'Select "{submenu}" and press Enter.',
              f'Select "{entry}" and press Enter. Do not pick the "(recovery mode)" entry.']

    if next_boot:
        lines = [f"GRUB will try to boot the newer kernel. Until its driver is published, every time you "
                 f"reboot or power on, choose {target.version}:"]
    else:
        lines = [f"Boot kernel {target.version}; it has the matching NVIDIA {target.nvidia_module} module:"]
    lines += [f"  {i}. {s}" for i, s in enumerate(steps, 1)]
    if g.default == "saved":
        lines.append(f"Shortcut: sudo grub-reboot '{submenu}>{entry}' && reboot  (selects it for one boot)")
    lines.append("Run graphics-sync again in a day or two; it will fix the newer kernel once the driver is out."
                 if next_boot else
                 "Once it's booted, run graphics-sync again. It will offer to fix the newer kernel from there.")
    return lines


def installation_flag(installation: str) -> str:
    return {"user": "--user", "system": "--system"}.get(installation, f"--installation={installation}")


def flatpak_available(need: FlatpakNeed) -> bool:
    """Is the extension published? Uses the locally cached remote metadata first, then the network."""
    for cached in (["--cached"], []):
        rc, _ = run(["flatpak", "remote-info", installation_flag(need.installation), *cached,
                     need.origin, need.ref], timeout=30)
        if rc == 0:
            return True
    return False


def flatpak_plans(fp: FlatpakState, fd: FlatpakDiagnosis) -> tuple[list[Plan], list[FlatpakNeed]]:
    """Install plans per (installation, remote), plus the needs that aren't published yet."""
    groups: dict[tuple[str, str], list[str]] = {}
    unavailable = []
    for need in fd.missing:
        if flatpak_available(need):
            groups.setdefault((need.installation, need.origin), []).append(need.ref)
        else:
            unavailable.append(need)
    plans = []
    for (inst, origin), refs in groups.items():
        url = fp.remote_urls.get(f"{inst}/{origin}", "https://dl.flathub.org/repo/")
        plans.append(Plan(f"Install the Flatpak NVIDIA {fd.version} GL extension ({inst} installation)",
                          ["flatpak", "install", installation_flag(inst), "-y", origin, *refs],
                          [f"{ref.split('/')[1]}  (new)" for ref in refs], [url],
                          f"Download: from {origin}; NVIDIA GL extensions are roughly 300 MB."))
    return plans, unavailable


def flatpak_cleanup_plans(fd: FlatpakDiagnosis) -> list[Plan]:
    by_inst: dict[str, list[str]] = {}
    for r in fd.stale:
        by_inst.setdefault(r.installation, []).append(r.ref)
    return [Plan(f"Remove {len(refs)} unused Flatpak NVIDIA GL extension(s) ({inst} installation)",
                 ["flatpak", "uninstall", installation_flag(inst), "-y", *refs],
                 [f"{ref.split('/')[1]}  (remove)" for ref in refs])
            for inst, refs in by_inst.items()]
