# graphics-sync

> [!WARNING]
> **This code was AI-generated** using Claude (model: Claude Opus 5.5, `claude-opus-5-5`) in Claude Code.
> It has been tested on one machine only. Review it before running: it can run `sudo apt-get install`,
> `flatpak install/uninstall` and `systemctl reboot`. It always shows what it will do and asks first.

Detects and fixes the "new kernel booted, NVIDIA driver didn't come with it" problem on Ubuntu and
Ubuntu-based distributions (Kubuntu, Xubuntu, Pop!_OS and similar). It needs only Python 3 (standard
library), and it works offline whenever the fix is already in the apt cache.

On AMD and Intel GPUs it reports that there's nothing to sync: their drivers ship with every kernel.
Fedora, Arch and other non-apt distributions are not supported.

## What it checks

- OS release, running kernel, KDE Plasma version and session type (X11/Wayland)
- GPU (PCI vendor/device) and which kernel driver is bound to it
- Installed NVIDIA driver branch/flavor (e.g. `nvidia-driver-595-open`) and its userspace version
- For **every installed kernel**: is there an `nvidia.ko`, and does its version match userspace exactly?
- Which module is actually loaded right now
- GRUB settings: hidden menu, stale `GRUB_DEFAULT` pins, UEFI vs BIOS (which key opens the menu)
- **Flatpak**: Flatpak apps get the NVIDIA driver through a GL extension
  (`org.freedesktop.Platform.GL.nvidia-595-99-02`, plus `GL32.*` for 32-bit apps like Steam) whose
  version must match the host driver exactly. It checks every installation (user/system) that has apps.
  Without a matching extension, apps silently fall back to slow software rendering.

## What it does about it

When the running kernel is out of sync, it builds a fix from the first option that works:

1. **Install the matching prebuilt module** (`linux-modules-nvidia-<branch>-<kernel>`) plus the
   metapackage that keeps future kernels in sync.
2. **Rebuild with DKMS** if you use `nvidia-dkms-*` and the kernel headers are present.
3. **Switch to a newer driver branch** if your branch has no module for this kernel but a newer one does.

The fix always includes the **Flatpak GL extension for the driver version it lands on**. It shows every
package change and download, then asks once before doing anything. Downloading is fine; a *partial*
fix is not. If any piece can't be fetched (no network, or Flathub hasn't published the extension yet,
which usually takes a few days after a driver release), nothing is changed. Instead it tells you
**exactly which GRUB entry to boot**: the newest installed kernel whose module matches, and how to open
the hidden menu. If you're online but apt's package lists have nothing yet, it offers to refresh them.

Afterwards it offers to remove Flatpak NVIDIA extensions that no installed driver uses anymore.

When the current boot is fine but the *newest* kernel is broken (so the next reboot would break),
it offers to fix that kernel ahead of time.

## Usage

```bash
./install.sh               # links ~/.local/bin/graphics-sync
./install.sh --autostart   # also check at each desktop login and pop a notification if out of sync
./install.sh --uninstall   # remove both

graphics-sync              # report + interactive fix
graphics-sync --check      # report only; exit code 1 if out of sync
graphics-sync --update     # sudo apt-get update first (when online and apt has nothing yet)
graphics-sync --json       # machine-readable state
```

`install.sh` links to wherever you cloned the project, so re-run it if you move the folder. To run it
without installing, use `./bin/graphics-sync` or `python3 -m graphics_sync` from the project folder.

Run the tests with `python3 -m unittest discover -s tests`.

## Why this keeps happening

Ubuntu ships the NVIDIA kernel module as a separate package per kernel. When a new kernel arrives, the
matching module package usually needs a newer NVIDIA userspace, and upgrading that means *removing*
the previous kernel's module package. `sudo apt upgrade` won't remove packages, so it keeps the NVIDIA
stack back ("The following packages have been kept back: libnvidia-...") while still installing
the new kernel. You reboot into the new kernel and there's no driver for it.

**Prevention:** update with `sudo apt full-upgrade` (or Discover), not `sudo apt upgrade`, and check
that nothing NVIDIA-related is listed as "kept back".

## Manual cheat sheet (if this tool itself can't run)

```bash
uname -r                                          # running kernel
modinfo -k "$(uname -r)" -F version nvidia        # module for it (error = none installed)
dpkg -l 'libnvidia-gl-*' | grep ^ii               # userspace version; must equal the line above
cat /sys/module/nvidia/version                    # loaded module (missing = not loaded)

# Fix forward. BRANCH is your driver package minus "nvidia-driver-": see  dpkg -l 'nvidia-driver-*'
BRANCH=595-open
sudo apt install linux-modules-nvidia-$BRANCH-$(uname -r) \
    linux-modules-nvidia-$BRANCH-generic-hwe-$(. /etc/os-release; echo "$VERSION_ID")

# Flatpak extension for the new driver version, dots become dashes (e.g. 595.99.02 -> 595-99-02).
# Use --system instead of --user if your Flatpak apps are installed system-wide.
flatpak install --user flathub runtime/org.freedesktop.Platform.GL.nvidia-595-99-02/x86_64/1.4
flatpak list --runtime | grep GL.nvidia            # old versions can be removed with flatpak uninstall
sudo reboot
```

**Booting an older kernel with a hidden GRUB menu:** right after the motherboard logo, tap **Esc**
(UEFI) or hold **Left Shift** (BIOS). If you land at `grub>`, type `normal`, press Enter, then tap Esc
once. Choose *Advanced options for &lt;distro&gt;* (e.g. Ubuntu or Kubuntu), then
*&lt;distro&gt;, with Linux &lt;version&gt;* (not recovery mode).

To make the menu always visible, set `GRUB_TIMEOUT_STYLE=menu` and `GRUB_TIMEOUT=5` in
`/etc/default/grub`, then run `sudo update-grub`.
