"""graphics-sync: detect and fix kernel / NVIDIA driver mismatches."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, replace

from . import __version__
from .diagnose import Diagnosis, FlatpakDiagnosis, Status, diagnose, diagnose_flatpak, nvidia_gl_version
from .remedy import (Plan, apt_mirror_url, flatpak_cleanup_plans, flatpak_plans, plan_module_fix, reachable,
                     reboot_instructions)
from .system import SystemInfo, detect, detect_kernels, detect_nvidia

COLOR = sys.stdout.isatty()


def style(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


bold = lambda t: style(t, "1")      # noqa: E731
green = lambda t: style(t, "32")    # noqa: E731
yellow = lambda t: style(t, "33")   # noqa: E731
red = lambda t: style(t, "31")      # noqa: E731


def row(label: str, value: str) -> None:
    print(f"  {label:<15}{value}")


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def check_flatpak(info: SystemInfo) -> FlatpakDiagnosis | None:
    """Re-reads the NVIDIA userspace version, since a fix may have just changed it."""
    if not info.flatpak or not info.nvidia:
        return None
    return diagnose_flatpak(info.flatpak, detect_nvidia() or info.nvidia)


def print_flatpak(fd: FlatpakDiagnosis) -> None:
    print(bold("\nFlatpak NVIDIA GL extensions"))
    for r in fd.installed:
        v = nvidia_gl_version(r)
        mark = (green("OK  matches driver") if v == fd.version
                else red("unused (no installed driver matches)") if r in fd.stale
                else yellow("in use until reboot"))
        print(f"  {r.app_id.removeprefix('org.freedesktop.Platform.'):<24}{r.arch:<8}{r.installation:<8}{mark}")
    for need in fd.missing:
        name = need.ref.split("/")[1].removeprefix("org.freedesktop.Platform.")
        print(f"  {name:<24}{need.ref.split('/')[2]:<8}{need.installation:<8}{red('MISSING')}")
    if fd.missing:
        print(red(bold("OUT OF SYNC: ")) + f"Flatpak apps need the NVIDIA {fd.version} GL extension; without it "
              "they fall back to slow software rendering.")


def print_report(info: SystemInfo, diag: Diagnosis, fd: FlatpakDiagnosis | None) -> None:
    print(bold("System"))
    row("OS", info.os_name)
    row("Kernel", f"{info.kernel} (running)")
    row("KDE Plasma", f"{info.plasma} ({info.session_type} session)" if info.plasma
        else f"not detected ({info.session_type} session)")
    for g in info.gpus:
        name = g.name if g.name.startswith(g.vendor) else f"{g.vendor} {g.name}"
        row("GPU", f"{name} [{g.pci_id}], kernel driver in use: {g.bound_driver or 'none'}")
    nv = info.nvidia
    if nv:
        row("NVIDIA driver", f"{nv.package} (userspace {nv.userspace_version}"
                             f"{', DKMS' if nv.dkms_package else ''})")
        row("Loaded module", nv.loaded_version or "none")
    row("Firmware", "UEFI" if info.grub.uefi else "BIOS/legacy")
    row("Secure Boot", {True: "enabled", False: "disabled", None: "unknown"}[info.secure_boot])

    if nv:
        print(bold("\nInstalled kernels"))
        for k in info.kernels:
            if k.nvidia_module == nv.userspace_version:
                mod = green(f"OK  nvidia {k.nvidia_module}")
            elif k.nvidia_module:
                mod = yellow(f"BAD nvidia {k.nvidia_module} (userspace is {nv.userspace_version})")
            else:
                mod = red("BAD no nvidia module")
            tag = "  <- running" if k.version == info.kernel else ""
            print(f"  {k.version:<22}{mod}{tag}")

    print()
    color = green if diag.healthy else red
    print(color(bold("In sync: " if diag.healthy else "OUT OF SYNC: ")) + diag.summary)
    for w in diag.warnings:
        print(yellow("Warning: ") + w)
    if fd:
        print_flatpak(fd)


def show_plan(plan: Plan) -> None:
    print(bold(f"\nFix: {plan.title}"))
    for change in plan.changes:
        print(f"    {change}")
    if plan.note:
        print(f"  {plan.note}")
    print(f"  Command: {' '.join(plan.command)}")
    if plan.command[:2] == ["sudo", "apt-get"]:
        print("  Download: " + ("none, all packages are already in the apt cache (works offline)" if plan.offline
                                else f"{len(plan.download_urls)} package(s) from {plan.download_urls[0].split('/')[2]}"))


def execute(plan: Plan) -> bool:
    if subprocess.run(plan.command).returncode != 0:
        print(red(f"Failed: {' '.join(plan.command)} (see output above)."))
        return False
    return True


def run_plan(plan: Plan) -> bool:
    """Show, confirm and run one standalone plan. Returns True if it ran and succeeded."""
    show_plan(plan)
    if not plan.offline and not reachable(plan.download_urls):
        print(red("  Can't reach the server, and this needs a download."))
        return False
    if not confirm("\nApply this now?"):
        print("  Skipped.")
        return False
    return execute(plan)


def module_matches_now(kver: str) -> bool:
    nv = detect_nvidia()
    kernel = next((k for k in detect_kernels(kver) if k.version == kver), None)
    if nv and kernel and kernel.nvidia_module == nv.userspace_version:
        print(green(f"\nKernel {kver} now has NVIDIA module {kernel.nvidia_module} matching userspace."))
        return True
    print(yellow(f"\nThe install succeeded, but kernel {kver} still has no matching module. "
                 "Re-run graphics-sync to see the current state."))
    return False


def fix_forward(info: SystemInfo, kver: str) -> bool:
    """Bring kernel kver's module *and* Flatpak in line with one driver version, or change nothing.

    Downloading is fine, but a partial fix is not: if any piece can't be fetched (offline, or not
    published yet), stop. The currently working kernel + Flatpak extension pair stays intact.
    """
    online = reachable([apt_mirror_url()])
    plan = plan_module_fix(info, kver)
    if not plan and online:
        print(f"\nNo NVIDIA module for {kver} in apt's local package lists, but you're online.")
        if confirm("Refresh the package lists (sudo apt-get update) and look again?") \
                and subprocess.run(["sudo", "apt-get", "update"]).returncode == 0:
            plan = plan_module_fix(info, kver)
    if not plan:
        if online:
            print(yellow(f"\nThe NVIDIA {info.nvidia.branch}{info.nvidia.flavor} driver for kernel {kver} hasn't been "
                         "published yet (the kernel arrived first; Ubuntu usually catches up within a few days), "
                         "and no other driver series has one either."))
        else:
            print(yellow(f"\nNo NVIDIA module for {kver} is in the local package lists, and there's no network "
                         "connection to check for a newer one."))
        return False

    plans = [plan]
    target = plan.new_userspace or info.nvidia.userspace_version
    if info.flatpak:
        fd = diagnose_flatpak(info.flatpak, replace(info.nvidia, userspace_version=target))
        flatpak_installs, unavailable = flatpak_plans(info.flatpak, fd)
        if unavailable:
            names = ", ".join(n.ref.split("/")[1] for n in unavailable)
            print(yellow(f"\nNot applying the fix: driver {target} would leave Flatpak apps without GPU "
                         f"acceleration, because {names} isn't published yet (Flathub usually lags new drivers "
                         "by a few days)."))
            return False
        plans += flatpak_installs

    for p in plans:
        show_plan(p)
    if any(not p.offline and not reachable(p.download_urls) for p in plans):
        print(red("\nNot applying the fix: it needs to download packages and there's no network connection."))
        return False
    if not confirm(f"\nApply {'all ' + str(len(plans)) + ' steps' if len(plans) > 1 else 'this fix'} now?"):
        print("  Skipped.")
        return False
    if not execute(plan) or not module_matches_now(kver):
        return False
    if not all(execute(p) for p in plans[1:]):
        print(yellow("The kernel module is fixed, but a Flatpak step failed. Re-run graphics-sync to retry it."))
    return True


def offer_reboot() -> None:
    if confirm("A reboot is needed to load the driver. Reboot now?"):
        subprocess.run(["systemctl", "reboot"])
    else:
        print("Reboot when you're ready.")


def sync_flatpak(info: SystemInfo) -> None:
    """Make Flatpak's NVIDIA GL extension match the (possibly just updated) host driver."""
    fd = check_flatpak(info)
    if not fd:
        return
    plans, unavailable = flatpak_plans(info.flatpak, fd)
    for plan in plans:
        run_plan(plan)
    for need in unavailable:
        print(yellow(f"\n{need.ref.split('/')[1]} isn't published on {need.origin} yet (Flathub usually lags new "
                     "drivers by a few days). Flatpak apps will use software rendering until it is; "
                     "re-run graphics-sync later."))
    for plan in flatpak_cleanup_plans(fd):
        run_plan(plan)


def resolve(info: SystemInfo, diag: Diagnosis) -> int:
    if diag.status == Status.NO_DRIVER:
        print("\nUbuntu can pick and install the recommended driver: sudo ubuntu-drivers install  (needs network)")
        if confirm("Run it now?") and subprocess.run(["sudo", "ubuntu-drivers", "install"]).returncode == 0:
            offer_reboot()
        return 1

    if diag.healthy:
        # Proactively fix the kernel the next boot will probably use.
        if info.nvidia and diag.broken_newest and diag.broken_newest.version != info.kernel:
            kver = diag.broken_newest.version
            if fix_forward(info, kver):
                print(f"You can now reboot into {kver}.")
            else:
                print()
                for line in reboot_instructions(info, info.running_kernel(), next_boot=True):
                    print(line)
        sync_flatpak(info)
        return 0

    if diag.status == Status.STALE_LOADED:
        print("\nThe fix is just a reboot into the same kernel.")
        sync_flatpak(info)
        offer_reboot()
        return 1

    if diag.status == Status.NOT_LOADED:
        print("\nTry loading it:          sudo modprobe nvidia")
        print("Look for errors:         journalctl -k -b | grep -iE 'nvidia|nouveau'")
        print("Check for a blacklist:   grep -rn nvidia /etc/modprobe.d/")
        if info.secure_boot:
            print("Secure Boot is on: an unsigned (DKMS) module will be refused unless its key is enrolled with MOK.")
        sync_flatpak(info)
        return 1

    # MISSING_MODULE / MODULE_MISMATCH: fix forward if everything can be brought in line, else boot an older kernel.
    if fix_forward(info, info.kernel):
        sync_flatpak(info)  # offers to remove extensions the old driver used
        offer_reboot()
        return 0

    sync_flatpak(info)
    fallback = [k for k in diag.good_kernels if k.version != info.kernel]
    print()
    if fallback:
        for line in reboot_instructions(info, fallback[0]):
            print(line)
    else:
        print(red("No installed kernel has a matching NVIDIA module."))
        print("  - Connect to the internet (Ethernet still works) and run graphics-sync again")
        print("  - Or reinstall the recommended driver:  sudo ubuntu-drivers install")
    return 1


def notify(info: SystemInfo, diag: Diagnosis, fd: FlatpakDiagnosis | None) -> None:
    if not diag.healthy:
        message = diag.summary
    elif diag.broken_newest and info.nvidia:
        message = diag.warnings[-1]
    elif fd and fd.missing:
        message = f"Flatpak apps are missing the NVIDIA {fd.version} GL extension."
    else:
        return
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "--app-name=graphics-sync", "--icon=dialog-warning",
                        "Graphics driver out of sync", f"{message}\nRun graphics-sync in a terminal to fix it."])
    else:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graphics-sync", description=__doc__)
    parser.add_argument("--check", action="store_true", help="report only, never prompt; exit 1 if out of sync")
    parser.add_argument("--notify", action="store_true", help="desktop notification if out of sync (for autostart)")
    parser.add_argument("--json", action="store_true", help="print the detected state as JSON")
    parser.add_argument("--update", action="store_true", help="run 'sudo apt-get update' first (needs network)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    if args.update:
        subprocess.run(["sudo", "apt-get", "update"])
    info = detect()
    diag = diagnose(info)
    fd = check_flatpak(info)
    in_sync = diag.healthy and (fd is None or fd.healthy)

    if args.json:
        print(json.dumps({"system": asdict(info), "status": diag.status.value, "summary": diag.summary,
                          "warnings": diag.warnings, "flatpak": asdict(fd) if fd else None}, indent=2))
        return 0 if in_sync else 1
    if args.notify:
        notify(info, diag, fd)
        return 0 if in_sync else 1

    print_report(info, diag, fd)
    if args.check:
        return 0 if in_sync else 1
    return resolve(info, diag)
