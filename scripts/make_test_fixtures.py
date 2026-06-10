#!/usr/bin/env python3
"""Generate binary test fixtures for static_rasters tests.

Run once to create:
  tests/fixtures/static_rasters/minimal.tif       - minimal LE TIFF (magic OK)
  tests/fixtures/static_rasters/bad_magic.bin     - random bytes with wrong magic
"""

import struct
from pathlib import Path

out = Path("tests/fixtures/static_rasters")
out.mkdir(parents=True, exist_ok=True)

# Minimal TIFF (little-endian): magic + IFD offset + empty IFD entry count
tif_bytes = b"\x49\x49\x2a\x00" + struct.pack("<I", 8) + struct.pack("<H", 0)
(out / "minimal.tif").write_bytes(tif_bytes)

# Bad magic — not a TIFF
(out / "bad_magic.bin").write_bytes(bytes(16))

print(f"minimal.tif ({len(tif_bytes)} bytes, magic={tif_bytes[:4].hex()})")
print("bad_magic.bin (16 zero bytes)")
