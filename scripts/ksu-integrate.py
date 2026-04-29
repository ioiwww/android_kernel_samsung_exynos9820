#!/usr/bin/env python3
"""
KernelSU-Next integration patcher for Samsung Exynos9820 (Linux 4.14 / non-GKI).

Performs four jobs against the kernel source tree (CWD = kernel root):

  1. Tweaks the d2s defconfig:
       - Enables CONFIG_KSU=y, CONFIG_KPROBES=y, CONFIG_OVERLAY_FS=y
       - Disables Samsung security flags that block KSU at runtime
         (CONFIG_UH_*, CONFIG_RKP, CONFIG_KDP*, CONFIG_KNOX_NCM,
          CONFIG_SECURITY_DEFEX, CONFIG_FIVE).
  2. Adds manual KernelSU hooks in:
       - fs/exec.c          (do_execveat_common)
       - fs/open.c          (faccessat syscall)
       - fs/read_write.c    (vfs_read)
       - fs/stat.c          (vfs_statx)
       - drivers/input/input.c (input_handle_event — safemode trigger)
  3. Hardens drivers/Kconfig + drivers/Makefile to include the
     KernelSU-Next driver tree (in case `setup.sh` did not patch them).
  4. Idempotent — running it twice is a no-op.

Run from the kernel root:
    python3 scripts/ksu-integrate.py
"""

from __future__ import annotations
import os
import re
import sys
from pathlib import Path

ROOT = Path(os.environ.get("KSRC", ".")).resolve()
DEFCONFIG = ROOT / "arch/arm64/configs/exynos9820-d2s_defconfig"


def info(msg: str) -> None:
    print(f"\033[1;34m[ksu]\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"\033[1;33m[ksu]\033[0m {msg}")


def die(msg: str) -> None:
    print(f"\033[1;31m[ksu] FAIL: {msg}\033[0m", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- defconfig
ENABLE = [
    "CONFIG_KSU=y",
    "CONFIG_KPROBES=y",
    "CONFIG_HAVE_KPROBES=y",
    "CONFIG_KPROBE_EVENTS=y",
    "CONFIG_OVERLAY_FS=y",
    "CONFIG_TMPFS_XATTR=y",
]
# Samsung's in-kernel exploit-mitigation / signature-enforcement layers
# These actively block KSU's su / overlayfs path. Turn them off for the build.
DISABLE_PREFIXES = (
    "CONFIG_UH",
    "CONFIG_RKP",
    "CONFIG_KDP",
    "CONFIG_KNOX_NCM",
    "CONFIG_SECURITY_DEFEX",
    "CONFIG_FIVE",
    "CONFIG_PROCA",
)


def patch_defconfig() -> None:
    if not DEFCONFIG.exists():
        die(f"defconfig not found: {DEFCONFIG}")
    src = DEFCONFIG.read_text().splitlines()
    out: list[str] = []
    seen: set[str] = set()

    for line in src:
        m = re.match(r"^(CONFIG_[A-Z0-9_]+)=", line)
        m2 = re.match(r"^# (CONFIG_[A-Z0-9_]+) is not set", line)
        key = (m or m2).group(1) if (m or m2) else None
        if key and key.startswith(DISABLE_PREFIXES):
            out.append(f"# {key} is not set")
            seen.add(key)
            continue
        # Drop any prior KSU enablement so we re-emit canonically below
        if key in {"CONFIG_KSU", "CONFIG_KPROBES", "CONFIG_OVERLAY_FS",
                  "CONFIG_HAVE_KPROBES", "CONFIG_KPROBE_EVENTS",
                  "CONFIG_TMPFS_XATTR"}:
            continue
        out.append(line)

    out.append("")
    out.append("# === KernelSU-Next (added by scripts/ksu-integrate.py) ===")
    for kv in ENABLE:
        out.append(kv)
    out.append("# === end KernelSU-Next ===")

    DEFCONFIG.write_text("\n".join(out) + "\n")
    info(f"defconfig updated: {len(ENABLE)} flags enabled, "
         f"{len(seen)} Samsung security flags disabled")


# ---------------------------------------------------------------- source patches
class Patch:
    """One source patch to apply, idempotent.

    `extern_block` is dropped near the top of the file (after the last
    `#include` in the first 200 lines).  `inject_after` is a regex that must
    match a single line; `hook` is appended on the next line.
    """

    def __init__(self, path: str, sentinel: str, extern_block: str,
                 inject_after: str, hook: str) -> None:
        self.path = ROOT / path
        self.sentinel = sentinel
        self.extern_block = extern_block.strip("\n")
        self.inject_after = re.compile(inject_after)
        self.hook = hook.rstrip("\n")

    def apply(self) -> None:
        if not self.path.exists():
            warn(f"skip {self.path.relative_to(ROOT)} (file missing)")
            return
        text = self.path.read_text()
        if self.sentinel in text:
            info(f"already patched: {self.path.relative_to(ROOT)}")
            return

        lines = text.splitlines()
        # 1. extern decls — after last #include in first 200 lines
        scan = lines[:200]
        last_inc = max((i for i, l in enumerate(scan)
                        if l.startswith("#include")), default=-1)
        if last_inc < 0:
            die(f"{self.path}: no #include lines found")
        lines.insert(last_inc + 1, "")
        lines.insert(last_inc + 2, "/* KernelSU-Next manual hooks */")
        lines.insert(last_inc + 3, self.extern_block)

        # 2. find injection point
        for i, l in enumerate(lines):
            if self.inject_after.search(l):
                # find the matching opening brace (same line or following)
                j = i
                while j < len(lines) and "{" not in lines[j]:
                    j += 1
                if j >= len(lines):
                    die(f"{self.path}: open brace not found after match")
                lines.insert(j + 1, self.hook)
                self.path.write_text("\n".join(lines) + "\n")
                info(f"patched {self.path.relative_to(ROOT)}")
                return
        die(f"{self.path}: injection anchor not matched: "
            f"{self.inject_after.pattern!r}")


PATCHES = [
    # ---- fs/exec.c -------------------------------------------------------
    Patch(
        "fs/exec.c",
        sentinel="ksu_handle_execveat",
        extern_block=(
            "extern bool ksu_execveat_hook __read_mostly;\n"
            "extern int ksu_handle_execveat(int *fd, struct filename **filename_ptr,\n"
            "                               void *argv, void *envp, int *flags);\n"
            "extern int ksu_handle_execveat_sucompat(int *fd, struct filename **filename_ptr,\n"
            "                                        void *argv, void *envp, int *flags);"
        ),
        inject_after=r"^static\s+int\s+do_execveat_common\s*\(",
        hook=(
            "\tif (unlikely(ksu_execveat_hook))\n"
            "\t\tksu_handle_execveat(&fd, &filename, &argv, &envp, &flags);\n"
            "\telse\n"
            "\t\tksu_handle_execveat_sucompat(&fd, &filename, &argv, &envp, &flags);"
        ),
    ),
    # ---- fs/open.c -------------------------------------------------------
    Patch(
        "fs/open.c",
        sentinel="ksu_handle_faccessat",
        extern_block=(
            "extern int ksu_handle_faccessat(int *dfd, const char __user **filename_user,\n"
            "                                int *mode, int *flags);"
        ),
        inject_after=r"SYSCALL_DEFINE3\s*\(\s*faccessat\s*,",
        hook="\tksu_handle_faccessat(&dfd, &filename, &mode, NULL);",
    ),
    # ---- fs/read_write.c ------------------------------------------------
    Patch(
        "fs/read_write.c",
        sentinel="ksu_handle_vfs_read",
        extern_block=(
            "extern int ksu_handle_vfs_read(struct file **file_ptr, char __user **buf_ptr,\n"
            "                               size_t *count_ptr, loff_t **pos);"
        ),
        inject_after=r"^ssize_t\s+vfs_read\s*\(",
        hook="\tksu_handle_vfs_read(&file, &buf, &count, &pos);",
    ),
    # ---- fs/stat.c ------------------------------------------------------
    Patch(
        "fs/stat.c",
        sentinel="ksu_handle_stat",
        extern_block=(
            "extern int ksu_handle_stat(int *dfd, const char __user **filename_user, int *flags);"
        ),
        inject_after=r"^int\s+vfs_statx\s*\(",
        hook="\tksu_handle_stat(&dfd, &filename, &flags);",
    ),
    # ---- drivers/input/input.c -----------------------------------------
    Patch(
        "drivers/input/input.c",
        sentinel="ksu_handle_input_handle_event",
        extern_block=(
            "extern bool ksu_input_hook __read_mostly;\n"
            "extern int ksu_handle_input_handle_event(unsigned int *type,\n"
            "                                         unsigned int *code, int *value);"
        ),
        inject_after=r"^static\s+void\s+input_handle_event\s*\(",
        hook=(
            "\tif (unlikely(ksu_input_hook))\n"
            "\t\tksu_handle_input_handle_event(&type, &code, &value);"
        ),
    ),
]


# ---------------------------------------------------------------- drivers/Kconfig + Makefile
def patch_drivers_glue() -> None:
    """Make sure KernelSU-Next is wired into the drivers tree even if
    setup.sh's edits were missed."""
    kconfig = ROOT / "drivers/Kconfig"
    makefile = ROOT / "drivers/Makefile"

    # The KernelSU-Next setup.sh creates a `KernelSU-Next/kernel/` directory
    # at repo root and patches the build to source it.  We add a thin
    # shim path so the standard `drivers/kernelsu` reference also works.
    ksu_root_kernel = ROOT / "KernelSU-Next/kernel"
    drv_link = ROOT / "drivers/kernelsu"
    if ksu_root_kernel.exists() and not drv_link.exists():
        try:
            drv_link.symlink_to(os.path.relpath(ksu_root_kernel, ROOT / "drivers"))
            info("symlinked drivers/kernelsu -> ../KernelSU-Next/kernel")
        except OSError as e:
            warn(f"could not create drivers/kernelsu symlink: {e}")

    if kconfig.exists():
        text = kconfig.read_text()
        if "drivers/kernelsu/Kconfig" not in text:
            text = text.rstrip() + (
                '\n\nsource "drivers/kernelsu/Kconfig"\n'
            )
            kconfig.write_text(text)
            info("appended source line to drivers/Kconfig")
    if makefile.exists():
        text = makefile.read_text()
        if "kernelsu/" not in text:
            text = text.rstrip() + "\nobj-$(CONFIG_KSU)\t\t+= kernelsu/\n"
            makefile.write_text(text)
            info("appended obj-$(CONFIG_KSU) line to drivers/Makefile")


# ---------------------------------------------------------------- main
def main() -> int:
    info(f"kernel root: {ROOT}")
    patch_defconfig()
    for p in PATCHES:
        p.apply()
    patch_drivers_glue()
    info("KernelSU-Next integration complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
