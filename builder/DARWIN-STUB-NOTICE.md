# SDK-free Darwin linker interface

The builder generates a minimal `CoreFoundation.tbd` containing symbol names
derived from the `core-foundation-sys` 0.8.7 Rust bindings at commit
`652bab0a62d87df8a974ffad2ad9f7be218a8b56`:

- [CFRelease](https://github.com/servo/core-foundation-rs/blob/652bab0a62d87df8a974ffad2ad9f7be218a8b56/core-foundation-sys/src/base.rs#L191)
- [CFTimeZoneCopySystem, CFTimeZoneGetName, CFTimeZoneResetSystem](https://github.com/servo/core-foundation-rs/blob/652bab0a62d87df8a974ffad2ad9f7be218a8b56/core-foundation-sys/src/timezone.rs#L62)
- [CFStringGetCStringPtr, CFStringGetLength, CFStringGetBytes](https://github.com/servo/core-foundation-rs/blob/652bab0a62d87df8a974ffad2ad9f7be218a8b56/core-foundation-sys/src/string.rs#L368)

Their Mach-O export names are `_CFRelease`, `_CFTimeZoneCopySystem`,
`_CFTimeZoneGetName`, `_CFTimeZoneResetSystem`, `_CFStringGetCStringPtr`,
`_CFStringGetLength`, and `_CFStringGetBytes`. These are the imports in
[iana-time-zone 0.1.65's Darwin backend](https://github.com/strawlab/iana-time-zone/blob/3726968181ae67d95c8de2f750a95b9c703ae4c8/src/tz_darwin.rs),
used by OxiDex's default release through chrono. The bindings are licensed
`MIT OR Apache-2.0`; this stub selects MIT and includes the notice below in
the image's `/opt/opensourceDarwinStubs/NOTICE`. The license is available in
[the pinned binding source](https://github.com/servo/core-foundation-rs/blob/652bab0a62d87df8a974ffad2ad9f7be218a8b56/core-foundation-sys/LICENSE-MIT).
The generated TBD's SHA-256 is
`e555b2500bcfd7471f5251deea43cf18c69ec8d0ff6b8e8de282279d91b971ca`.

This text stub contains no implementation and no proprietary Apple SDK
material. It records the runtime install name
`/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation`,
also present in [Apple's open-source CF Makefile](https://github.com/apple-oss-distributions/CF/blob/main/Makefile#L79).
The resulting program calls the real framework supplied by macOS. Do not ship
the stub as a runtime framework or replace the framework calls with placeholders.

## Build contract

The dedicated Linux builder supplies Rust 1.97.1's `aarch64-apple-darwin` and
`x86_64-apple-darwin` standard libraries, Zig 0.15.2 and cargo-zigbuild 0.23.4.
It removes `SDKROOT` and exports
`OXIDEX_DARWIN_FRAMEWORK_DIR=/opt/opensourceDarwinStubs`. Build each target
separately with `cargo zigbuild --release --locked --target <triple>`, adding
`-Lframework=/opt/opensourceDarwinStubs` to that target's existing Rust flags.
Cargo target-scoped `--config` arrays join repository arrays; `RUSTFLAGS` and
`CARGO_ENCODED_RUSTFLAGS` override config, so callers that provide them must
append the framework path there while preserving their other flags. Preserve
OxiDex's existing Darwin stripping/linker arguments. Set deployment targets
explicitly to match Rust 1.97.1 defaults: ARM64 `11.0`, x86_64 `10.12`.
Zig 0.15.2 accepts the existing `-Wl,-S,-x` arguments. cargo-zigbuild 0.23.4
omits Darwin exported-symbols lists with Zig versions below 0.16, so native
validation must also check the cdylib's expected FFI exports and account for
possibly broader symbol visibility than the native Apple linker produced.

[Rust 1.97.1's linker handling](https://github.com/rust-lang/rust/blob/1.97.1/compiler/rustc_codegen_ssa/src/back/apple.rs#L175)
allows missing SDK discovery and specifically documents `zig cc` as a driver
with bundled system-library interfaces. [cargo-zigbuild 0.23.4](https://docs.rs/crate/cargo-zigbuild/0.23.4/source/src/zig/cargo_env.rs)
permits absent SDKROOT on Linux. [Zig's vendored Darwin layout](https://github.com/ziglang/zig/blob/0.15.2/lib/std/zig/LibCDirs.zig#L153)
provides public-source libc headers and `libSystem.tbd`;
[its linker](https://github.com/ziglang/zig/blob/0.15.2/src/link/MachO.zig#L1089)
accepts `CoreFoundation.framework/CoreFoundation.tbd` in the framework path.
Zig's bundled files retain their own upstream license notices.

The current default release also compiles bundled zstd C with Zig's target
compiler. Optional `magika`/ONNX/native TLS features can add frameworks and
native libraries not covered by this seven-symbol interface. Missing symbols
must remain hard link failures; never use undefined dynamic lookup to conceal
an incomplete interface.

This backend is not deployment or runtime proof. Enable Darwin release routing
only after both architectures' CLI and cdylib link successfully on an admitted
Spot builder, and native Mac checks cover architecture, version/help, parsing,
CoreFoundation timezone use, and signing/notarization of the universal output.
Mac `lipo`, signing, DMG creation and native validation consume the remotely
compiled artifacts.

## MIT license

Copyright (c) 2012-2013 Mozilla Foundation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to
do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
