#!/usr/bin/env bash
# ------------------------------------------------------------------
# Build script for android_kernel_samsung_exynos9820 — d2s (Galaxy S10+)
#
# Produces:
#   out/arch/arm64/boot/Image
#   dist/d2s/dtb.img
#   dist/d2s/dtbo.img        (when overlays are produced by the tree)
#   dist/d2s/AnyKernel3-d2s-<stamp>.zip
#
# Toolchain: Proton Clang (kdrag0n/proton-clang) into ./toolchain/clang
# ------------------------------------------------------------------

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MODEL="${MODEL:-d2s}"
DEFCONFIG="${DEFCONFIG:-exynos9820-d2s_defconfig}"
JOBS="$(nproc 2>/dev/null || echo 4)"
OUT_DIR="$ROOT/out"
DIST_DIR="$ROOT/dist/$MODEL"
CLANG_DIR="$ROOT/toolchain/clang"

log()  { printf '\033[1;34m[build]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[fail ]\033[0m %s\n' "$*" >&2; exit 1; }

mkdir -p "$OUT_DIR" "$DIST_DIR"

# ------------------------------------------------------------------
# 1. Toolchain — Proton Clang (Clang 13, battle-tested on Exynos 4.14)
# ------------------------------------------------------------------
if [ ! -x "$CLANG_DIR/bin/clang" ]; then
    log "Proton Clang not found at $CLANG_DIR — cloning..."
    rm -rf "$CLANG_DIR"
    mkdir -p "$(dirname "$CLANG_DIR")"
    git clone --depth=1 https://github.com/kdrag0n/proton-clang.git "$CLANG_DIR" \
        || fail "git clone of proton-clang failed"
fi

export PATH="$CLANG_DIR/bin:${PATH}"
export ARCH=arm64
export SUBARCH=arm64
export KBUILD_BUILD_USER="github-actions"
export KBUILD_BUILD_HOST="d2s-builder"

# ccache wrappers if available
if command -v ccache >/dev/null 2>&1; then
    CC_WRAP="ccache clang"
    HOSTCC_WRAP="ccache gcc"
    HOSTCXX_WRAP="ccache g++"
else
    CC_WRAP="clang"
    HOSTCC_WRAP="gcc"
    HOSTCXX_WRAP="g++"
fi

MAKE_ARGS=(
    O="$OUT_DIR"
    ARCH=arm64
    LLVM=1
    LLVM_IAS=1
    CC="$CC_WRAP"
    HOSTCC="$HOSTCC_WRAP"
    HOSTCXX="$HOSTCXX_WRAP"
    CROSS_COMPILE=aarch64-linux-gnu-
    CROSS_COMPILE_ARM32=arm-linux-gnueabi-
    CLANG_TRIPLE=aarch64-linux-gnu-
)

log "Toolchain : $(clang --version | head -n1)"
log "Defconfig : $DEFCONFIG"
log "Jobs      : $JOBS"

# ------------------------------------------------------------------
# 2. Configure
# ------------------------------------------------------------------
log "make $DEFCONFIG"
make "${MAKE_ARGS[@]}" "$DEFCONFIG"

# ------------------------------------------------------------------
# 3. Build kernel + device trees
# ------------------------------------------------------------------
log "Building kernel..."
make -j"$JOBS" "${MAKE_ARGS[@]}" Image dtbs || fail "kernel build failed"

# Optional artifacts — ignore if the tree does not produce them
make -j"$JOBS" "${MAKE_ARGS[@]}" Image.gz       2>/dev/null || true
make -j"$JOBS" "${MAKE_ARGS[@]}" Image.gz-dtb   2>/dev/null || true
make -j"$JOBS" "${MAKE_ARGS[@]}" dtbo.img       2>/dev/null || true

# ------------------------------------------------------------------
# 4. Assemble dtb.img (concatenated DTBs for d2s)
# ------------------------------------------------------------------
KERNEL_IMG="$OUT_DIR/arch/arm64/boot/Image"
[ -f "$KERNEL_IMG" ] || fail "kernel Image not found at $KERNEL_IMG"

log "Concatenating d2s DTBs..."
DTB_OUT="$DIST_DIR/dtb.img"
: > "$DTB_OUT"

mapfile -t DTBS < <(
    {
        find "$OUT_DIR/arch/arm64/boot/dts/samsung" -type f -name 'exynos9820-d2s*.dtb' 2>/dev/null
        find "$OUT_DIR/arch/arm64/boot/dts/exynos"  -type f -name 'exynos9820*.dtb'     2>/dev/null
    } | sort -u
)

if [ "${#DTBS[@]}" -eq 0 ]; then
    log "WARNING: no DTBs found under out/arch/arm64/boot/dts/{samsung,exynos}"
else
    for f in "${DTBS[@]}"; do
        log "  + $(basename "$f")"
        cat "$f" >> "$DTB_OUT"
    done
fi

# ------------------------------------------------------------------
# 5. Pick up dtbo.img if the tree produced one
# ------------------------------------------------------------------
DTBO_SRC="$OUT_DIR/arch/arm64/boot/dtbo.img"
if [ -f "$DTBO_SRC" ]; then
    cp -v "$DTBO_SRC" "$DIST_DIR/dtbo.img"
else
    log "NOTE: out/arch/arm64/boot/dtbo.img not produced by this tree — skipping dtbo.img"
fi

# ------------------------------------------------------------------
# 6. Build flashable AnyKernel3 zip
# ------------------------------------------------------------------
log "Packaging AnyKernel3 zip..."
AK3_DIR="$ROOT/AnyKernel3"
if [ ! -d "$AK3_DIR" ]; then
    git clone --depth=1 https://github.com/osm0sis/AnyKernel3 "$AK3_DIR"
fi

git -C "$AK3_DIR" reset --hard --quiet
rm -f  "$AK3_DIR"/Image* "$AK3_DIR"/dtb* 2>/dev/null || true
cp -v "$KERNEL_IMG" "$AK3_DIR/Image"
[ -f "$DIST_DIR/dtb.img" ]  && cp -v "$DIST_DIR/dtb.img"  "$AK3_DIR/dtb.img"
[ -f "$DIST_DIR/dtbo.img" ] && cp -v "$DIST_DIR/dtbo.img" "$AK3_DIR/dtbo.img"

cat > "$AK3_DIR/anykernel.sh" <<'AKEOF'
### AnyKernel3 Linkit Fortress Mod
## Developer: Linkit / Original by osm0sis

### AnyKernel setup
# global properties
properties() { '
kernel.string=Linkit Fortress Kernel for d2s (N975F)
do.devicecheck=1
do.modules=0
do.systemless=0
do.cleanup=1
do.cleanuponabort=1
device.name1=beyond0lte
device.name2=beyond1lte
device.name3=beyond2lte
device.name4=beyondx
device.name5=d1
device.name6=d1x
device.name7=d2s
device.name8=d2x
device.name9=f62
supported.versions=11 - 16
'; } # end properties


### AnyKernel install
## boot files attributes
boot_attributes() {
set_perm_recursive 0 0 755 644 $RAMDISK/*;
set_perm_recursive 0 0 750 750 $RAMDISK/init* $RAMDISK/sbin;
} # end attributes

# boot shell variables
BLOCK=/dev/block/by-name/boot;
DTB_BLOCK=/dev/block/sda12; # المسار المؤكد من Termux
IS_SLOT_DEVICE=0;
RAMDISK_COMPRESSION=auto;
PATCH_VBMETA_FLAG=auto;

# import functions/variables and setup patching - see for reference (DO NOT REMOVE)
. tools/ak3-core.sh;

# --- [ الجزء الأول: فلش الكيرنل وتعديل الـ Ramdisk ] ---
dump_boot;

# تعديلات الـ init.rc الأصلية
backup_file init.rc;
replace_string init.rc "cpuctl cpu,timer_slack" "mount cgroup none /dev/cpuctl cpu" "mount cgroup none /dev/cpuctl cpu,timer_slack";

# تعديلات fstab (تحسين الأداء و writeback)
backup_file fstab.tuna;
patch_fstab fstab.tuna /system ext4 options "noatime,barrier=1" "noatime,nodiratime,barrier=0";
patch_fstab fstab.tuna /cache ext4 options "barrier=1" "barrier=0,nomblk_io_submit";
patch_fstab fstab.tuna /data ext4 options "data=ordered" "nomblk_io_submit,data=writeback";

write_boot; 


# --- [ الجزء الثاني: فلش الـ DTB المعدل إلى sda12 ] ---
if [ -f $home/dtb.img ]; then
  ui_print "- Linkit Fortress: Custom dtb.img detected.";
  ui_print "- Target partition: sda12";
  
  # مسح الكاش قبل الفلش لضمان الاستقرار
  sync;
  
  ui_print "- Flashing DTB tweaks (Battery & Charging)...";
  dd if=$home/dtb.img of=$DTB_BLOCK bs=4096;
  
  if [ $? -eq 0 ]; then
    ui_print "- DTB Flash Successful!";
  else
    ui_print "! Error: Flash to sda12 failed !";
  fi
  
  sync;
else
  ui_print "- Note: Separate dtb.img not found, using kernel-embedded dtb.";
fi

ui_print "- Linkit Fortress Installation Finished.";
AKEOF

STAMP="$(date -u +%Y%m%d-%H%M%S)"
ZIP="AnyKernel3-${MODEL}-${STAMP}.zip"
( cd "$AK3_DIR" && zip -r9 "$DIST_DIR/$ZIP" . -x '.git/*' -x '*.zip' >/dev/null )
log "Wrote $DIST_DIR/$ZIP"

# ------------------------------------------------------------------
# 7. Summary
# ------------------------------------------------------------------
log "Build complete. Artifacts in $DIST_DIR :"
ls -lah "$DIST_DIR"
