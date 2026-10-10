"""Unpack the NovaAI database (datasets/exp1a/nova_ai.db.xz → nova_ai.db) and check it is
byte-for-byte the one every experiment ran on. Standard library only:

    python3 datasets/unpack.py          # or: make data
"""

from __future__ import annotations

import hashlib
import lzma
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "exp1a" / "nova_ai.db.xz"
DST = HERE / "exp1a" / "nova_ai.db"
SHA256 = "c5c7132aceeff6fecf653b2a89414240bd5abf848b7ecbe32ce072683a90b1a7"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if DST.exists() and sha256(DST) == SHA256:
        print(f"{DST.relative_to(HERE.parent)} already unpacked and verified")
        return 0
    with lzma.open(SRC) as src, DST.open("wb") as out:
        shutil.copyfileobj(src, out, 1 << 20)
    got = sha256(DST)
    if got != SHA256:
        print(f"checksum mismatch: {got} != {SHA256}", file=sys.stderr)
        return 1
    print(f"{DST.relative_to(HERE.parent)} unpacked ({DST.stat().st_size / 2**20:.0f} MiB), sha256 verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
