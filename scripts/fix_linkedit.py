#!/usr/bin/env python3
"""
Fix mis-aligned LINKEDIT string pool in a Mach-O dylib.

ld-1328.2 (macOS 15 / Xcode 16+) produces cdylib binaries whose LINKEDIT
string pool (LC_SYMTAB.stroff) and sometimes code-signature section are only
4-byte aligned. macOS 15's dyld requires 8-byte alignment for both and rejects
the binary with "mis-aligned LINKEDIT string pool/content". -ld_classic is no
longer supported, so we patch the binary in-place after linking.

The patch inserts the minimum padding needed at two insertion points:
  1. Before the string table to make stroff 8-byte aligned.
  2. Between the string table and the code signature to re-align the signature.
After patching the file is re-signed (ad-hoc) so dyld accepts it.
"""
from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path


def _r32(d: bytearray, o: int) -> int:
    return struct.unpack_from("<I", d, o)[0]


def _r64(d: bytearray, o: int) -> int:
    return struct.unpack_from("<Q", d, o)[0]


def _w32(d: bytearray, o: int, v: int) -> None:
    struct.pack_into("<I", d, o, v)


def _w64(d: bytearray, o: int, v: int) -> None:
    struct.pack_into("<Q", d, o, v)


def fix_linkedit(path: Path, *, verbose: bool = False) -> bool:
    """Return True if the binary was patched, False if already aligned."""
    data = bytearray(path.read_bytes())
    if _r32(data, 0) != 0xFEEDFACF:
        return False  # not a 64-bit Mach-O, skip

    ncmds = _r32(data, 16)
    off = 32  # load commands start after the 32-byte header
    sym_off = cs_off = li_off = None

    for _ in range(ncmds):
        cmd = _r32(data, off)
        if cmd == 0x2:    # LC_SYMTAB
            sym_off = off
        elif cmd == 0x1D:  # LC_CODE_SIGNATURE
            cs_off = off
        elif cmd == 0x19:  # LC_SEGMENT_64
            if data[off + 8 : off + 24].rstrip(b"\x00") == b"__LINKEDIT":
                li_off = off
        off += _r32(data, off + 4)

    if sym_off is None:
        return False

    stroff = _r32(data, sym_off + 16)
    strsize = _r32(data, sym_off + 20)
    pad1 = (8 - stroff % 8) % 8
    if pad1 == 0 and (cs_off is None or _r32(data, cs_off + 8) % 8 == 0):
        return False  # already aligned everywhere

    # --- patch 1: insert pad1 bytes before the string table ---
    if pad1:
        data[stroff:stroff] = b"\x00" * pad1
        _w32(data, sym_off + 16, stroff + pad1)

    new_str_end = stroff + pad1 + strsize

    # --- patch 2: re-align code signature if needed ---
    pad2 = 0
    if cs_off is not None:
        new_cs = _r32(data, cs_off + 8) + pad1
        pad2 = (8 - new_cs % 8) % 8
        if pad2:
            data[new_str_end:new_str_end] = b"\x00" * pad2
            new_cs += pad2
        _w32(data, cs_off + 8, new_cs)

    total = pad1 + pad2

    if li_off is not None:
        _w64(data, li_off + 32, _r64(data, li_off + 32) + total)  # vmsize
        _w64(data, li_off + 48, _r64(data, li_off + 48) + total)  # filesize

    path.write_bytes(data)

    # Re-sign ad-hoc so dyld accepts the modified binary.
    subprocess.run(
        ["codesign", "--sign", "-", "--force", str(path)],
        check=True,
        capture_output=not verbose,
    )
    if verbose:
        print(
            f"fix_linkedit: patched {path.name} "
            f"(+{total} bytes: {pad1} at stroff, {pad2} before code-sig)"
        )
    return True


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    any_patched = False
    for path in args.paths:
        patched = fix_linkedit(path, verbose=True)
        if patched:
            any_patched = True
        elif args.verbose:
            print(f"fix_linkedit: {path.name} already aligned, skipped")
    sys.exit(0 if any_patched or not args.paths else 0)
