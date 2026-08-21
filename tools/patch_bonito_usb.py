#!/usr/bin/env python3
"""
patch_bonito_usb.py

Build a patched Google Pixel 3a XL (bonito) postmarketOS boot image by
replacing the kernel and DTB in an existing boot image.

This tool is intentionally conservative:
  * It does not modify kernel source.
  * It does not install kernel modules into a root filesystem.
  * It does not flash a phone.
  * It refuses to overwrite its input.
  * It verifies inputs and output and emits a SHA-256 manifest.

Requirements:
  * Python 3.8+
  * Android unpack_bootimg.py / unpack_bootimg available in PATH or supplied
    with --unpack-tool.
  * mkbootimg is NOT required for the tested bonito Android boot-header-v0
    layout; this tool repacks that format natively in Python.
  * A known-good Image.gz and bonito DTB produced from the patched kernel.

Example:
    python3 patch_bonito_usb.py \
        --input boot.img \
        --kernel Image.gz \
        --dtb sdm670-google-bonito-sdc.dtb \
        --output bonito-pm660-usbc.img

IMPORTANT:
The kernel modules installed in the postmarketOS root filesystem must match
the kernel release embedded in Image.gz. This utility cannot safely install
those modules into an arbitrary phone/rootfs image, so module deployment is
kept as an explicit separate step.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional


EXPECTED_DTB_NAME = "sdm670-google-bonito-sdc.dtb"
LINUX_VERSION_RE = re.compile(rb"Linux version ([^\s\x00]+)")


class PatcherError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def human_size(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def require_file(path: Path, label: str) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise PatcherError(f"{label} not found: {path}")
    if path.stat().st_size == 0:
        raise PatcherError(f"{label} is empty: {path}")
    return path


def find_tool(explicit: Optional[str], names: Iterable[str], label: str) -> str:
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return str(p.resolve())
        resolved = shutil.which(explicit)
        if resolved:
            return resolved
        raise PatcherError(f"{label} not found: {explicit}")

    for name in names:
        resolved = shutil.which(name)
        if resolved:
            return resolved

    raise PatcherError(
        f"Could not find {label}. Supply it explicitly on the command line."
    )


def command_prefix(tool: str) -> list[str]:
    p = Path(tool)
    try:
        first = p.open("rb").read(2)
    except OSError:
        first = b""

    if p.suffix == ".py" or first != b"#!":
        if p.suffix == ".py":
            return [sys.executable, tool]

    return [tool]


def run(cmd: list[str], *, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    printable = " ".join(str(x) for x in cmd)
    print(f"+ {printable}")
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.returncode:
        raise PatcherError(
            f"Command failed with exit status {proc.returncode}: {printable}"
        )
    return proc


def extract_kernel_release(image_gz: Path) -> Optional[str]:
    try:
        with gzip.open(image_gz, "rb") as f:
            # Kernel Image is ~tens of MiB; bounded read avoids pathological input.
            data = f.read(128 * 1024 * 1024)
    except (OSError, EOFError) as exc:
        raise PatcherError(f"Kernel is not a valid gzip stream: {exc}") from exc

    match = LINUX_VERSION_RE.search(data)
    if not match:
        return None
    return match.group(1).decode("ascii", errors="replace")


def validate_dtb(path: Path, allow_non_bonito_name: bool) -> None:
    data = path.read_bytes()
    if len(data) < 40:
        raise PatcherError("DTB is too small to be valid.")

    # Flattened Device Tree magic, big endian: d0 0d fe ed.
    if data[:4] != b"\xd0\x0d\xfe\xed":
        raise PatcherError(
            f"{path.name} does not begin with the flattened-device-tree magic."
        )

    if path.name != EXPECTED_DTB_NAME and not allow_non_bonito_name:
        raise PatcherError(
            f"Expected bonito DTB named {EXPECTED_DTB_NAME!r}, got {path.name!r}. "
            "Use --allow-non-bonito-dtb-name only if you have independently "
            "verified the DTB is for this device."
        )


def locate_unpacked_file(root: Path, names: Iterable[str]) -> Optional[Path]:
    wanted = set(names)
    candidates = [
        p for p in root.rglob("*")
        if p.is_file() and p.name in wanted
    ]
    if not candidates:
        return None
    # Prefer the shallowest path if a tool produced duplicate metadata copies.
    return sorted(candidates, key=lambda p: (len(p.parts), str(p)))[0]


def replace_file(src: Path, dst: Path) -> None:
    old_mode = None
    if dst.exists():
        old_mode = stat.S_IMODE(dst.stat().st_mode)
    shutil.copy2(src, dst)
    if old_mode is not None:
        os.chmod(dst, old_mode)


\
def align_up(value: int, alignment: int) -> int:
    if alignment <= 0:
        raise PatcherError(f"Invalid alignment: {alignment}")
    return (value + alignment - 1) // alignment * alignment


def parse_android_boot_v0(path: Path) -> dict:
    """
    Parse the legacy Android boot image v0 header.

    This is intentionally limited to header version 0, which is the format
    used by the tested Pixel 3a XL postmarketOS boot image.

    Layout:

        page 0: Android boot header
        page 1+: kernel
        then:    ramdisk
        then:    optional second-stage payload

    The DTB used by this bonito image is appended to the compressed kernel
    payload rather than represented as a separate boot-image component.
    """
    data = path.read_bytes()

    if len(data) < 1632:
        raise PatcherError("Boot image is too small to contain a v0 header.")

    if data[:8] != b"ANDROID!":
        raise PatcherError("Input does not contain Android boot magic.")

    (
        kernel_size,
        kernel_addr,
        ramdisk_size,
        ramdisk_addr,
        second_size,
        second_addr,
        tags_addr,
        page_size,
        header_version,
        os_version,
    ) = struct.unpack_from("<10I", data, 8)

    if header_version != 0:
        raise PatcherError(
            f"Native repacker supports Android boot header v0 only; "
            f"input reports header version {header_version}."
        )

    if page_size < 512 or page_size & (page_size - 1):
        raise PatcherError(f"Suspicious boot image page size: {page_size}")

    header_end = page_size

    kernel_offset = header_end
    ramdisk_offset = kernel_offset + align_up(kernel_size, page_size)
    second_offset = ramdisk_offset + align_up(ramdisk_size, page_size)
    image_end = second_offset + align_up(second_size, page_size)

    if image_end > len(data):
        raise PatcherError(
            "Boot image component sizes exceed the source image length."
        )

    ramdisk = data[ramdisk_offset:ramdisk_offset + ramdisk_size]
    second = data[second_offset:second_offset + second_size]

    return {
        "raw": data,
        "kernel_size": kernel_size,
        "kernel_addr": kernel_addr,
        "ramdisk_size": ramdisk_size,
        "ramdisk_addr": ramdisk_addr,
        "second_size": second_size,
        "second_addr": second_addr,
        "tags_addr": tags_addr,
        "page_size": page_size,
        "header_version": header_version,
        "os_version": os_version,
        "ramdisk": ramdisk,
        "second": second,
    }


def legacy_boot_id(
    kernel: bytes,
    ramdisk: bytes,
    second: bytes,
) -> bytes:
    """
    Generate the legacy Android boot-image SHA-1 id.

    mkbootimg's legacy id hashes each component followed by its little-endian
    uint32 size. The 20-byte SHA-1 digest occupies the start of the 32-byte
    id[] field and the remainder is zero-filled.
    """
    h = hashlib.sha1()

    h.update(kernel)
    h.update(struct.pack("<I", len(kernel)))

    h.update(ramdisk)
    h.update(struct.pack("<I", len(ramdisk)))

    h.update(second)
    h.update(struct.pack("<I", len(second)))

    return h.digest() + b"\x00" * 12


def write_android_boot_v0(
    source: Path,
    output: Path,
    kernel_payload: Path,
) -> None:
    """
    Repack a legacy Android boot header v0 image while preserving the original
    header fields, ramdisk, command line, load addresses, and page size.

    Only kernel_size and the legacy id[] digest are changed.
    """
    parsed = parse_android_boot_v0(source)

    original = parsed["raw"]
    page_size = parsed["page_size"]

    kernel = kernel_payload.read_bytes()
    ramdisk = parsed["ramdisk"]
    second = parsed["second"]

    # Preserve the complete original first page. This retains:
    #
    #   addresses
    #   OS version fields
    #   product/board name
    #   cmdline
    #   extra cmdline
    #   any padding bytes
    #
    # We modify only the fields which necessarily change.
    header = bytearray(original[:page_size])

    # kernel_size is immediately after ANDROID! magic.
    struct.pack_into("<I", header, 8, len(kernel))

    # Legacy id[] begins at offset:
    #
    #   8 magic
    #   10 x uint32
    #   16-byte board/name
    #   512-byte cmdline
    #
    # = 8 + 40 + 16 + 512 = 576
    id_offset = 576
    boot_id = legacy_boot_id(kernel, ramdisk, second)

    if id_offset + len(boot_id) > len(header):
        raise PatcherError("Boot header is too small for legacy id field.")

    header[id_offset:id_offset + 32] = boot_id

    def padded(component: bytes) -> bytes:
        padding = align_up(len(component), page_size) - len(component)
        return component + b"\x00" * padding

    result = bytearray()
    result += header
    result += padded(kernel)
    result += padded(ramdisk)

    if second:
        result += padded(second)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Replace the kernel and DTB in a bonito Android/postmarketOS boot image."
    )
    p.add_argument("--input", required=True, type=Path, help="Known-good source boot.img")
    p.add_argument("--kernel", required=True, type=Path, help="Patched arch/arm64/boot/Image.gz")
    p.add_argument("--dtb", required=True, type=Path, help=f"Patched {EXPECTED_DTB_NAME}")
    p.add_argument("--output", required=True, type=Path, help="Output boot image")
    p.add_argument(
        "--unpack-tool",
        help="Path/name of Android unpack_bootimg.py (auto-detected otherwise)",
    )
    p.add_argument(
        "--mkbootimg-tool",
        help="Path/name of Android mkbootimg.py (auto-detected otherwise)",
    )
    p.add_argument(
        "--allow-non-bonito-dtb-name",
        action="store_true",
        help="Allow a DTB filename other than the expected bonito DTB name",
    )
    p.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Preserve temporary unpack directory for inspection",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and show hashes/release without unpacking/repacking",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    source = require_file(args.input, "Input boot image")
    kernel = require_file(args.kernel, "Kernel")
    dtb = require_file(args.dtb, "DTB")
    output = args.output.expanduser().resolve()

    if source == output:
        raise PatcherError("Output must not overwrite the input boot image.")

    validate_dtb(dtb, args.allow_non_bonito_dtb_name)
    release = extract_kernel_release(kernel)

    print("=== INPUT VALIDATION ===")
    print(f"boot image : {source} ({human_size(source.stat().st_size)})")
    print(f"boot sha256: {sha256(source)}")
    print(f"kernel     : {kernel} ({human_size(kernel.stat().st_size)})")
    print(f"kernel sha : {sha256(kernel)}")
    print(f"kernel rel : {release or 'not found in decompressed Image'}")
    print(f"dtb        : {dtb} ({human_size(dtb.stat().st_size)})")
    print(f"dtb sha256 : {sha256(dtb)}")

    if args.dry_run:
        print("\nDry run complete; no output image was created.")
        return 0

    unpack_tool = find_tool(
        args.unpack_tool,
        ("unpack_bootimg.py", "unpack_bootimg"),
        "unpack_bootimg",
    )
    work_parent = Path(tempfile.mkdtemp(prefix="bonito-usbc-"))
    unpack_dir = work_parent / "unpacked"
    unpack_dir.mkdir()

    try:
        print("\n=== UNPACK ===")
        unpack_cmd = command_prefix(unpack_tool) + [
            "--boot_img", str(source),
            "--out", str(unpack_dir),
        ]
        run(unpack_cmd)

        unpacked_kernel = locate_unpacked_file(
            unpack_dir, ("kernel", "Image.gz", "boot.img-kernel")
        )
        unpacked_dtb = locate_unpacked_file(
            unpack_dir, ("dtb", EXPECTED_DTB_NAME, "boot.img-dtb")
        )

        if not unpacked_kernel:
            raise PatcherError(
                "Could not identify the unpacked kernel file. "
                "Inspect the unpack directory and use a compatible Android "
                "unpack_bootimg implementation."
            )

        print(f"unpacked kernel: {unpacked_kernel.relative_to(work_parent)}")

        print("\n=== REPLACE ===")

        if unpacked_dtb:
            # The source boot image has a standalone DTB component. Preserve
            # that layout and replace the kernel and DTB independently.
            print(f"unpacked dtb   : {unpacked_dtb.relative_to(work_parent)}")
            print("DTB layout     : standalone")
            replace_file(kernel, unpacked_kernel)
            replace_file(dtb, unpacked_dtb)
            kernel_payload = unpacked_kernel
            dtb_layout = "standalone"
        else:
            # Pixel 3a XL / postmarketOS boot images using Android boot header
            # v0 may store the DTB appended directly to the compressed kernel
            # payload rather than as a standalone boot-image component.
            #
            # Reproduce that layout explicitly:
            #
            #     kernel payload = Image.gz + DTB
            #
            # Do not add a --dtb argument to mkbootimg in this case.
            print("unpacked dtb   : none")
            print("DTB layout     : appended to kernel")
            print(
                "No standalone DTB component was found; "
                "building Image.gz + DTB kernel payload."
            )

            combined_kernel = work_parent / "kernel-with-dtb"
            with combined_kernel.open("wb") as out:
                with kernel.open("rb") as src:
                    shutil.copyfileobj(src, out)
                with dtb.open("rb") as src:
                    shutil.copyfileobj(src, out)

            replace_file(combined_kernel, unpacked_kernel)
            kernel_payload = unpacked_kernel
            dtb_layout = "appended"

            expected_size = kernel.stat().st_size + dtb.stat().st_size
            actual_size = unpacked_kernel.stat().st_size

            if actual_size != expected_size:
                raise PatcherError(
                    "Combined Image.gz + DTB payload has unexpected size: "
                    f"expected {expected_size}, got {actual_size}"
                )

            print(
                f"combined kernel: {unpacked_kernel.relative_to(work_parent)} "
                f"({human_size(actual_size)})"
            )

        print("\n=== REPACK ===")

        source_layout = parse_android_boot_v0(source)

        if dtb_layout == "appended":
            print("repacker       : native Android boot header v0")
            print(f"page size      : {source_layout['page_size']}")
            print(f"kernel address : 0x{source_layout['kernel_addr']:08x}")
            print(f"ramdisk address: 0x{source_layout['ramdisk_addr']:08x}")
            print(f"tags address   : 0x{source_layout['tags_addr']:08x}")
            print(f"ramdisk size   : {source_layout['ramdisk_size']}")
            print(f"second size    : {source_layout['second_size']}")

            write_android_boot_v0(
                source=source,
                output=output,
                kernel_payload=kernel_payload,
            )

        else:
            # A standalone-DTB layout is deliberately not handled by the
            # native v0 path. Fall back to an external mkbootimg implementation.
            mkbootimg_tool = find_tool(
                args.mkbootimg_tool,
                ("mkbootimg.py", "mkbootimg"),
                "mkbootimg",
            )

            arg_proc = run(
                command_prefix(unpack_tool) + [
                    "--boot_img", str(source),
                    "--out", str(unpack_dir),
                    "--format=mkbootimg",
                ]
            )

            mk_args_line = None
            for line in reversed(arg_proc.stdout.splitlines()):
                if "--kernel" in line or "--ramdisk" in line:
                    mk_args_line = line.strip()
                    break

            if not mk_args_line:
                raise PatcherError(
                    "unpack_bootimg did not provide mkbootimg-format arguments."
                )

            import shlex
            mk_args = shlex.split(mk_args_line)

            if mk_args and not mk_args[0].startswith("--"):
                mk_args = mk_args[1:]

            def replace_arg(flag: str, value: Path) -> None:
                if flag in mk_args:
                    i = mk_args.index(flag)
                    if i + 1 >= len(mk_args):
                        raise PatcherError(
                            f"Malformed mkbootimg arguments after {flag}"
                        )
                    mk_args[i + 1] = str(value)
                else:
                    mk_args.extend([flag, str(value)])

            replace_arg("--kernel", kernel_payload)
            replace_arg("--dtb", unpacked_dtb)

            cleaned: list[str] = []
            i = 0
            while i < len(mk_args):
                if mk_args[i] in ("--output", "-o"):
                    i += 2
                    continue
                cleaned.append(mk_args[i])
                i += 1

            output.parent.mkdir(parents=True, exist_ok=True)

            run(
                command_prefix(mkbootimg_tool)
                + cleaned
                + ["--output", str(output)]
            )

        if not output.is_file() or output.stat().st_size == 0:
            raise PatcherError(
                "Repack completed but did not create a valid output file."
            )

        manifest = {
            "tool": "patch_bonito_usb.py",
            "input_boot": {
                "name": source.name,
                "sha256": sha256(source),
                "size": source.stat().st_size,
            },
            "kernel": {
                "name": kernel.name,
                "sha256": sha256(kernel),
                "size": kernel.stat().st_size,
                "release": release,
            },
            "dtb": {
                "name": dtb.name,
                "sha256": sha256(dtb),
                "size": dtb.stat().st_size,
            },
            "output": {
                "name": output.name,
                "sha256": sha256(output),
                "size": output.stat().st_size,
            },
            "boot_layout": {
                "header_version": 0,
                "page_size": source_layout["page_size"],
                "dtb": dtb_layout,
                "repacker": (
                    "native-v0"
                    if dtb_layout == "appended"
                    else "external-mkbootimg"
                ),
            },
            "warning": (
                "Kernel modules in the target postmarketOS root filesystem must "
                "match the kernel release in this image."
            ),
        }

        manifest_path = output.with_suffix(output.suffix + ".manifest.json")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

        print("\n=== SUCCESS ===")
        print(f"output      : {output}")
        print(f"size        : {human_size(output.stat().st_size)}")
        print(f"sha256      : {manifest['output']['sha256']}")
        print(f"manifest    : {manifest_path}")
        print("\nThis tool does NOT flash the device.")
        print("Test with `fastboot boot` before permanently flashing a boot partition.")
        print("Ensure /lib/modules contains modules matching the reported kernel release.")

        return 0

    finally:
        if args.keep_workdir:
            print(f"\nWork directory preserved: {work_parent}")
        else:
            shutil.rmtree(work_parent, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatcherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
