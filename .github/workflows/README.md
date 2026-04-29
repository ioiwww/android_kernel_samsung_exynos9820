# d2s kernel CI

GitHub Actions builds for `android_kernel_samsung_exynos9820` targeting the
**d2s** device (Samsung Galaxy S10+ Exynos).

## How it runs

* On every push to `lineage-23.2` that touches `build.sh`,
  `.github/workflows/build.yml`, or the d2s defconfig.
* Manually from the **Actions → Build d2s Kernel → Run workflow** menu, with
  two inputs:
  * `ksu` — `yes` (default) integrates KernelSU-Next via the official
    `setup.sh` and force-enables `CONFIG_KSU=y`.
  * `release` — `yes` publishes a GitHub Release with the artifacts attached.

## Toolchain

[Neutron Clang 18](https://github.com/Neutron-Toolchains) (snapshot
`05012024`). It is downloaded once and cached between runs.

## Outputs

Uploaded as a workflow artifact (and optionally attached to a release):

* `Image` — raw kernel image
* `dtb.img` — concatenated `exynos9820-d2s*.dtb` blobs
* `dtbo.img` — when the tree produces overlays
* `AnyKernel3-d2s-<timestamp>.zip` — flashable zip via TWRP / OrangeFox

## Local build

The workflow simply runs `./build.sh`, which is reproducible locally:

```bash
sudo apt-get install -y build-essential bc bison flex libssl-dev libelf-dev \
    zip cpio kmod ccache device-tree-compiler gcc-aarch64-linux-gnu
./build.sh
```

The script downloads Neutron Clang into `./toolchain/neutron` on first run.
