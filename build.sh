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
# Toolchain: Neutron Clang 18 (auto-downloaded into ./toolchain/neutron)
# ------------------------------------------------------------------

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

MODEL="${MODEL:-d2s}"
DEFCONFIG="${DEFCONFIG:-exynos9820-d2s_defconfig}"
JOBS="$(nproc 2>/dev/null || echo 4)"
OUT_DIR="$ROOT/out"
DIST_DIR="$ROOT/dist/$MODEL"
CLANG_DIR="$ROOT/toolchain/neutron"

log()  { printf '\033[1;34m[build]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[fail ]\033[0m %s\n' "$*" >&2; exit 1; }

mkdir -p "$OUT_DIR" "$DIST_DIR"

# ------------------------------------------------------------------
# 1. Toolchain
# ------------------------------------------------------------------
if [ ! -x "$CLANG_DIR/bin/clang" ]; then
    log "Neutron Clang not found at $CLANG_DIR — fetching..."
    mkdir -p "$CLANG_DIR"
    pushd "$CLANG_DIR" >/dev/null
    bash <(curl -fsSL "https://raw.githubusercontent.com/Neutron-Toolchains/antman/main/antman") -S=05012024
    bash <(curl -fsSL "https://raw.githubusercontent.com/Neutron-Toolchains/antman/main/antman") --patch=glibc
    popd >/dev/null
fi

export PATH="$CLANG_DIR/bin:${PATH}"
export ARCH=arm64
export SUBARCH=arm64
export KBUILD_BUILD_USER="github-actions"
export KBUILD_BUILD_HOST="d2s-builder"

# Use ccache when available to speed re-builds
if command -v ccache >/dev/null 2>&1; then
    export KBUILD_COMPILER_STRING="$(clang --version | head -n1)"
    CC_WRAP="ccache clang"
else
    CC_WRAP="clang"
fi

MAKE_ARGS=(
    O="$OUT_DIR"
    ARCH=arm64
    LLVM=1
    LLVM_IAS=1
    CC="$CC_WRAP"
    HOSTCC="ccache gcc"
    HOSTCXX="ccache g++"
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

# Optional compressed kernels — ignore if the tree does not enable them
make -j"$JOBS" "${MAKE_ARGS[@]}" Image.gz       2>/dev/null || true
make -j"$JOBS" "${MAKE_ARGS[@]}" Image.gz-dtb   2>/dev/null || true
make -j"$JOBS" "${MAKE_ARGS[@]}" dtbo.img       2>/dev/null || true

# ------------------------------------------------------------------
# 4. Assemble dtb.img
# ------------------------------------------------------------------
KERNEL_IMG="$OUT_DIR/arch/arm64/boot/Image"
[ -f "$KERNEL_IMG" ] || fail "kernel Image not found at $KERNEL_IMG"

log "Concatenating d2s DTBs..."
DTB_OUT="$DIST_DIR/dtb.img"
: > "$DTB_OUT"

# Prefer d2s-specific DTBs if present, otherwise fall back to all exynos9820 DTBs
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

# Reset working tree and stage our files
git -C "$AK3_DIR" reset --hard --quiet
rm -f  "$AK3_DIR"/Image* "$AK3_DIR"/dtb* 2>/dev/null || true
cp -v "$KERNEL_IMG" "$AK3_DIR/Image"
[ -f "$DIST_DIR/dtb.img" ]  && cp -v "$DIST_DIR/dtb.img"  "$AK3_DIR/dtb.img"
[ -f "$DIST_DIR/dtbo.img" ] && cp -v "$DIST_DIR/dtbo.img" "$AK3_DIR/dtbo.img"

# Minimal d2s-aware anykernel.sh
cat > "$AK3_DIR/anykernel.sh" <<'AKEOF'
### AnyKernel3 Ramdisk Mod Script
properties() { '
kernel.string=d2s kernel (lineage-23.2)
do.devicecheck=1
do.modules=0
do.systemless=1
do.cleanup=1
do.cleanuponabort=0
device.name1=d2s
device.name2=Galaxy S10+ (Exynos)
device.name3=beyond2lte
supported.versions=
supported.patchlevels=
'; }
. tools/ak3-core.sh
split_boot
flash_boot
flash_dtbo
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
