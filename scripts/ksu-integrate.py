#!/usr/bin/env python3
"""
KernelSU-Next integration patcher for Samsung Exynos9820 (Linux 4.14 / non-GKI).

Run from the kernel source root:
    python3 scripts/ksu-integrate.py

Performs:
  1. Defconfig tweak  - enables KSU + KPROBES + OVERLAY_FS
                      - disables Samsung security flags that block KSU
                        (CONFIG_UH*, RKP*, KDP*, KNOX_NCM, SECURITY_DEFEX,
                         FIVE*, PROCA*) using exact-name matching so
                        unrelated symbols like CONFIG_UHID are untouched
  2. Manual hook injection in
       fs/exec.c            (do_execveat_common)
       fs/open.c            (faccessat syscall)
       fs/read_write.c      (vfs_read)
       fs/stat.c            (vfs_statx)
       drivers/input/input.c (input_handle_event)
     - Externs go after the last *top-level* #include (preprocessor depth 0)
       so they are never compiled out by an #ifdef block
     - Hook calls go AFTER the function's local variable declarations to
       satisfy -Wdeclaration-after-statement
  3. drivers/Kconfig + drivers/Makefile glue for KernelSU-Next
  4. Idempotent — running again is a no-op
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
    # Force manual-hook mode. KernelSU-Next's KPROBES path requires
    # syscall_fn_t (5.10+), which Linux 4.14 does not have; manual hooks
    # are exactly what scripts/ksu-integrate.py injects below.
    "CONFIG_KSU_MANUAL_HOOK=y",
    "CONFIG_OVERLAY_FS=y",
    "CONFIG_TMPFS_XATTR=y",
]

# Explicit disables for Samsung in-kernel exploit-mitigation /
# signature-enforcement layers that block KSU's su / overlayfs path.
EXPLICIT_DISABLE = [
    "CONFIG_UH",
    "CONFIG_UH_LKMAUTH",
    "CONFIG_UH_LKM_BLOCK",
    "CONFIG_RKP",
    "CONFIG_RKP_CFP",
    "CONFIG_RKP_CFP_JOPP",
    "CONFIG_KDP",
    "CONFIG_KDP_CRED",
    "CONFIG_KNOX_NCM",
    "CONFIG_SECURITY_DEFEX",
    "CONFIG_FIVE",
    "CONFIG_PROCA",
    # Force OFF the kprobes-based hook path inside KernelSU-Next; we use
    # manual hooks via scripts/ksu-integrate.py.
    "CONFIG_KSU_KPROBES_HOOK",
]

# Exact-name suffix regex (after stripping CONFIG_ prefix).  Anchored so
# CONFIG_UHID, CONFIG_RKPM_*, etc. are NOT matched.
DISABLE_RE = re.compile(
    r"^("
    r"UH|UH_.+|"
    r"RKP|RKP_.+|"
    r"KDP|KDP_.+|"
    r"KNOX_NCM|"
    r"SECURITY_DEFEX|DEFEX_.+|"
    r"FIVE|FIVE_.+|"
    r"PROCA|PROCA_.+"
    r")$"
)


def patch_defconfig() -> None:
    if not DEFCONFIG.exists():
        die(f"defconfig not found: {DEFCONFIG}")
    src = DEFCONFIG.read_text().splitlines()
    out: list[str] = []
    disabled: list[str] = []
    enable_keys = {kv.split("=", 1)[0] for kv in ENABLE}

    for line in src:
        m  = re.match(r"^(CONFIG_)([A-Z0-9_]+)=", line)
        m2 = re.match(r"^# (CONFIG_)([A-Z0-9_]+) is not set", line)
        key_full = (m or m2).group(1) + (m or m2).group(2) if (m or m2) else None
        key_name = (m or m2).group(2) if (m or m2) else None

        # Strip Samsung security layers
        if key_name and DISABLE_RE.match(key_name):
            out.append(f"# {key_full} is not set")
            disabled.append(key_full)
            continue
        # Drop any prior KSU-related lines so we re-emit canonically below
        if key_full in enable_keys:
            continue
        out.append(line)

    out.append("")
    out.append("# === KernelSU-Next (added by scripts/ksu-integrate.py) ===")
    for kv in ENABLE:
        out.append(kv)
    for k in EXPLICIT_DISABLE:
        out.append(f"# {k} is not set")
    out.append("# === end KernelSU-Next ===")

    DEFCONFIG.write_text("\n".join(out) + "\n")
    info(f"defconfig: {len(ENABLE)} enabled, "
         f"{len(disabled)} matched-disable, "
         f"{len(EXPLICIT_DISABLE)} explicit-disable")


# ---------------------------------------------------------------- helpers
DECL_KEYWORDS = (
    "static", "const", "volatile", "register", "extern", "inline",
    "signed", "unsigned",
    "struct", "enum", "union",
    "char", "short", "int", "long", "float", "double", "void", "bool",
    "u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64",
    "loff_t", "size_t", "ssize_t", "umode_t", "kuid_t", "kgid_t",
    "mode_t", "dev_t", "pid_t", "gfp_t", "atomic_t", "spinlock_t",
    "sigset_t", "fmode_t", "nlink_t", "off_t", "time_t",
    "uid_t", "gid_t", "clock_t", "cputime_t",
    "__be32", "__le32", "__be64", "__le64",
)
DECL_RE = re.compile(
    r"^\s*(?:" + "|".join(DECL_KEYWORDS) + r")\b"
)
BLANK_RE   = re.compile(r"^\s*$")
COMMENT_RE = re.compile(r"^\s*(?://|/\*|\*)")


def find_top_include_insert(lines: list[str]) -> int:
    """Return the index AFTER the last `#include` line that sits at
    preprocessor depth 0 (so the insertion is never inside an #ifdef)."""
    depth = 0
    last = -1
    for i, raw in enumerate(lines[:400]):
        s = raw.lstrip()
        if s.startswith(("#if", "#ifdef", "#ifndef")):
            if s.startswith("#include") and depth == 0:
                last = i
            depth += 1
        elif s.startswith("#endif"):
            depth = max(0, depth - 1)
        elif s.startswith("#include") and depth == 0:
            last = i
    return last + 1 if last >= 0 else -1


def find_first_statement(lines: list[str], brace_idx: int) -> int:
    """Walk forward from the line containing the function's opening `{`
    and return the index of the first non-declaration / non-blank line
    inside that body.  Used to satisfy -Wdeclaration-after-statement."""
    j = brace_idx + 1
    while j < len(lines):
        l = lines[j]
        if BLANK_RE.match(l) or COMMENT_RE.match(l) or DECL_RE.match(l):
            j += 1
            continue
        return j
    return brace_idx + 1  # fallback


# ---------------------------------------------------------------- patches
class Patch:
    def __init__(self, path: str, sentinel: str, extern_block: str,
                 anchor: str, hook: str) -> None:
        self.path = ROOT / path
        self.sentinel = sentinel
        self.extern_block = extern_block.strip("\n")
        self.anchor = re.compile(anchor)
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

        # 1. Insert extern block at depth-0 #include tail
        ins = find_top_include_insert(lines)
        if ins < 0:
            die(f"{self.path}: no top-level #include found")
        lines.insert(ins,     "")
        lines.insert(ins + 1, "/* KernelSU-Next manual hooks */")
        lines.insert(ins + 2, self.extern_block)

        # 2. Find anchor (function definition)
        anchor_line = -1
        for i, l in enumerate(lines):
            if self.anchor.search(l):
                anchor_line = i
                break
        if anchor_line < 0:
            die(f"{self.path}: anchor not matched: {self.anchor.pattern!r}")

        # 3. Walk to opening brace
        brace_idx = anchor_line
        while brace_idx < len(lines) and "{" not in lines[brace_idx]:
            brace_idx += 1
        if brace_idx >= len(lines):
            die(f"{self.path}: opening brace not found after anchor")

        # 4. Skip past local declarations to satisfy -Wdeclaration-after-statement
        inject_at = find_first_statement(lines, brace_idx)
        lines.insert(inject_at, self.hook)

        self.path.write_text("\n".join(lines) + "\n")
        info(f"patched {self.path.relative_to(ROOT)} "
             f"(extern@{ins+2}, hook@{inject_at})")


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
        anchor=r"^static\s+int\s+do_execveat_common\s*\(",
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
        anchor=r"SYSCALL_DEFINE3\s*\(\s*faccessat\s*,",
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
        anchor=r"^ssize_t\s+vfs_read\s*\(",
        hook="\tksu_handle_vfs_read(&file, &buf, &count, &pos);",
    ),
    # ---- fs/stat.c ------------------------------------------------------
    Patch(
        "fs/stat.c",
        sentinel="ksu_handle_stat",
        extern_block=(
            "extern int ksu_handle_stat(int *dfd, const char __user **filename_user, int *flags);"
        ),
        anchor=r"^int\s+vfs_statx\s*\(",
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
        anchor=r"^static\s+void\s+input_handle_event\s*\(",
        hook=(
            "\tif (unlikely(ksu_input_hook))\n"
            "\t\tksu_handle_input_handle_event(&type, &code, &value);"
        ),
    ),
    # ---- kernel/reboot.c ------------------------------------------------
    # Required: KernelSU-Next legacy Kbuild greps reboot.c for
    # `ksu_handle_sys_reboot` to confirm manual hooks are integrated.
    Patch(
        "kernel/reboot.c",
        sentinel="ksu_handle_sys_reboot",
        extern_block=(
            "extern int ksu_handle_sys_reboot(int magic1, int magic2,\n"
            "                                 unsigned int cmd, void __user **arg);"
        ),
        anchor=r"SYSCALL_DEFINE4\s*\(\s*reboot\s*,",
        hook="\tksu_handle_sys_reboot(magic1, magic2, cmd, (void __user **)&arg);",
    ),
]


# ---------------------------------------------------------------- drivers glue
def patch_drivers_glue() -> None:
    kconfig  = ROOT / "drivers/Kconfig"
    makefile = ROOT / "drivers/Makefile"

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
            text = text.rstrip() + '\n\nsource "drivers/kernelsu/Kconfig"\n'
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
