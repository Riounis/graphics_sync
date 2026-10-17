import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from graphics_sync.diagnose import Status, diagnose, diagnose_flatpak, flatpak_gl_id
from graphics_sync.remedy import install_plan, reboot_instructions, summarize_simulation
from graphics_sync.system import (FlatpakRef, FlatpakState, Gpu, Grub, Kernel, NvidiaDriver, SystemInfo,
                                  kernel_key, upstream_version)

NVIDIA_GPU = Gpu("0000:01:00.0", "NVIDIA", "10de:2184", "GeForce GTX 1660", None)


def make_info(kernel="7.0.0-38-generic", kernels=None, loaded=None, gpus=None, nvidia=True,
              grub_default="0", **kw) -> SystemInfo:
    if kernels is None:
        kernels = [Kernel("7.0.0-38-generic", True, None), Kernel("7.0.0-34-generic", True, "595.91.07")]
    driver = NvidiaDriver("nvidia-driver-595-open", "595", "-open", "595.91.07", loaded, None,
                          "linux-modules-nvidia-595-open-generic-hwe-24.04") if nvidia else None
    return SystemInfo("Ubuntu 24.04.5 LTS", "24.04", kernel, "5.27.12", kw.get("session", "x11"), False,
                      [NVIDIA_GPU] if gpus is None else gpus, kernels, driver,
                      Grub(grub_default, "Kubuntu", "0", "hidden", True), kw.get("modeset"))


class DiagnoseTest(unittest.TestCase):
    def test_new_kernel_without_module(self):
        d = diagnose(make_info())
        self.assertEqual(d.status, Status.MISSING_MODULE)
        self.assertEqual([k.version for k in d.good_kernels], ["7.0.0-34-generic"])

    def test_module_version_mismatch(self):
        info = make_info(kernels=[Kernel("7.0.0-38-generic", True, "595.99.02")])
        self.assertEqual(diagnose(info).status, Status.MODULE_MISMATCH)

    def test_stale_loaded_module_after_update(self):
        info = make_info(kernel="7.0.0-34-generic", loaded="595.84")
        self.assertEqual(diagnose(info).status, Status.STALE_LOADED)

    def test_ok_on_older_kernel_warns_about_newest(self):
        d = diagnose(make_info(kernel="7.0.0-34-generic", loaded="595.91.07"))
        self.assertEqual(d.status, Status.OK)
        self.assertEqual(d.broken_newest.version, "7.0.0-38-generic")
        self.assertIn("next reboot will lose graphics", d.warnings[0])

    def test_fully_in_sync(self):
        info = make_info(kernels=[Kernel("7.0.0-38-generic", True, "595.91.07")], loaded="595.91.07")
        d = diagnose(info)
        self.assertEqual(d.status, Status.OK)
        self.assertEqual(d.warnings, [])

    def test_not_loaded(self):
        info = make_info(kernels=[Kernel("7.0.0-38-generic", True, "595.91.07")])
        self.assertEqual(diagnose(info).status, Status.NOT_LOADED)

    def test_amd_needs_no_sync(self):
        d = diagnose(make_info(gpus=[Gpu("x", "AMD", "1002:73bf", "Radeon RX 6800", "amdgpu")], nvidia=False))
        self.assertEqual(d.status, Status.NOT_NVIDIA)
        self.assertTrue(d.healthy)

    def test_no_driver_installed(self):
        self.assertEqual(diagnose(make_info(nvidia=False)).status, Status.NO_DRIVER)

    def test_stale_grub_pin(self):
        d = diagnose(make_info(grub_default="Advanced options for Kubuntu>Kubuntu, with Linux 6.17.0-22-generic"))
        self.assertTrue(any("6.17.0-22-generic" in w for w in d.warnings))

    def test_valid_grub_pin_is_fine(self):
        d = diagnose(make_info(grub_default="Advanced options for Kubuntu>Kubuntu, with Linux 7.0.0-34-generic"))
        self.assertFalse(any("GRUB_DEFAULT" in w for w in d.warnings))

    def test_wayland_without_modeset(self):
        info = make_info(kernels=[Kernel("7.0.0-38-generic", True, "595.91.07")], loaded="595.91.07",
                         session="wayland", modeset=False)
        self.assertTrue(any("modeset" in w for w in diagnose(info).warnings))


def gl(version, arch="x86_64", inst="user", gl32=False):
    return FlatpakRef(flatpak_gl_id(version, gl32), arch, "1.4", inst, "flathub")


def make_flatpak(runtimes, apps=("user",)):
    return FlatpakState("x86_64", runtimes, list(apps), {"user/flathub": "https://dl.flathub.org/repo/"})


class FlatpakTest(unittest.TestCase):
    driver = NvidiaDriver("nvidia-driver-595-open", "595", "-open", "595.99.02", "595.91.07", None, None)

    def test_missing_extension_after_driver_update(self):
        fd = diagnose_flatpak(make_flatpak([gl("595.91.07"), gl("590.48.01")]), self.driver)
        self.assertEqual([n.ref for n in fd.missing],
                         ["runtime/org.freedesktop.Platform.GL.nvidia-595-99-02/x86_64/1.4"])
        self.assertEqual(fd.missing[0].installation, "user")
        # 595.91.07 is still loaded until reboot, so only 590 is unused.
        self.assertEqual([r.app_id for r in fd.stale], [flatpak_gl_id("590.48.01")])

    def test_in_sync(self):
        fd = diagnose_flatpak(make_flatpak([gl("595.99.02")]), self.driver)
        self.assertTrue(fd.healthy)

    def test_gl32_needed_when_32bit_runtime_present(self):
        rt = [gl("595.99.02"), FlatpakRef("org.freedesktop.Platform.GL32.default", "i386", "25.08", "user", "flathub")]
        fd = diagnose_flatpak(make_flatpak(rt), self.driver)
        self.assertEqual([n.ref for n in fd.missing],
                         ["runtime/org.freedesktop.Platform.GL32.nvidia-595-99-02/i386/1.4"])

    def test_no_apps_means_nothing_needed(self):
        self.assertTrue(diagnose_flatpak(make_flatpak([], apps=()), self.driver).healthy)

    def test_extension_must_be_in_the_apps_installation(self):
        fd = diagnose_flatpak(make_flatpak([gl("595.99.02", inst="system")]), self.driver)
        self.assertEqual(fd.missing[0].installation, "user")


class HelpersTest(unittest.TestCase):
    def test_kernel_ordering(self):
        versions = ["6.17.0-40-generic", "7.0.0-4-generic", "7.0.0-38-generic"]
        self.assertEqual(sorted(versions, key=kernel_key, reverse=True)[0], "7.0.0-38-generic")
        self.assertGreater(kernel_key("7.0.0-38-generic"), kernel_key("7.0.0-4-generic"))

    def test_upstream_version(self):
        self.assertEqual(upstream_version("1:595.91.07-0ubuntu0.24.04.1"), "595.91.07")

    def test_reboot_instructions_name_exact_entry(self):
        text = "\n".join(reboot_instructions(make_info(), Kernel("7.0.0-34-generic", True, "595.91.07")))
        self.assertIn('"Advanced options for Kubuntu"', text)
        self.assertIn('"Kubuntu, with Linux 7.0.0-34-generic"', text)
        self.assertIn("Esc", text)

    def test_kernel_ahead_of_driver_sends_you_back(self):
        """New kernel installed, no NVIDIA module published for it yet: point at the old kernel, change nothing."""
        from graphics_sync import cli
        info = make_info(kernels=[Kernel("7.0.0-42-generic", True, None), Kernel("7.0.0-38-generic", True, "595.91.07")],
                         kernel="7.0.0-42-generic")
        out = io.StringIO()
        with mock.patch.object(cli, "plan_module_fix", return_value=None), \
             mock.patch.object(cli, "reachable", return_value=True), \
             mock.patch.object(cli, "confirm", return_value=False), \
             mock.patch.object(cli, "subprocess") as sp, redirect_stdout(out):
            self.assertEqual(cli.resolve(info, diagnose(info)), 1)
        sp.run.assert_not_called()
        self.assertIn("hasn't been published yet", out.getvalue())
        self.assertIn('"Kubuntu, with Linux 7.0.0-38-generic"', out.getvalue())

    def test_new_userspace_from_simulation(self):
        sim = ("Inst libnvidia-gl-595:i386 [595.91.07-0ubuntu0.24.04.1] (595.99.02-0ubuntu0.24.04.1 Ubuntu:24.04)\n"
               "Inst libnvidia-gl-610 (610.10-0ubuntu1 Ubuntu:24.04)\n")
        with mock.patch("graphics_sync.remedy.run", side_effect=[(0, sim), (0, "")]):
            self.assertEqual(install_plan(["x"], "t").new_userspace, "595.99.02")

    def test_summarize_simulation(self):
        sim = ("Inst libnvidia-gl-595 [595.91.07-0ubuntu0.24.04.1] (595.99.02-0ubuntu0.24.04.1 Ubuntu:24.04)\n"
               "Remv linux-modules-nvidia-595-open-7.0.0-34-generic [7.0.0-34.34]\n"
               "Inst linux-modules-nvidia-595-open-7.0.0-38-generic (7.0.0-38.38~24.04.4+1 Ubuntu:24.04)\n")
        self.assertEqual(summarize_simulation(sim), [
            "libnvidia-gl-595  595.91.07 -> 595.99.02",
            "linux-modules-nvidia-595-open-7.0.0-34-generic  (remove)",
            "linux-modules-nvidia-595-open-7.0.0-38-generic  (new) 7.0.0-38.38~24.04.4+1",
        ])


if __name__ == "__main__":
    unittest.main()
