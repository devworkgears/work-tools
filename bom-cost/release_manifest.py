"""List every file of the distributed zip (or of the folder it was extracted to) with its sha256,
and print one digest of that list.

Usage:
    python3 release_manifest.py <bom-cost-win64.zip | extracted folder> [--out manifest.txt]

The list has one line per file, "<sha256>  <path>", sorted by path, joined with LF and ending with LF.
Paths use "/" and start with the top folder (bom-cost/...). The digest is the sha256 of that text,
so a zip and the folder it was extracted to give the same digest.
"""

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path


def entries_of_zip(path: Path) -> list[tuple[str, str]]:
    rows = []
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if not info.is_dir():
                rows.append((info.filename, hashlib.sha256(z.read(info)).hexdigest()))
    names = [name for name, _ in rows]
    if len(names) != len(set(names)):
        raise ValueError(f"{path.name} has duplicate entries")
    return rows


def entries_of_dir(root: Path) -> list[tuple[str, str]]:
    rows = []
    for path in root.rglob("*"):
        if path.is_file():
            rows.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    return rows


def manifest_text(rows: list[tuple[str, str]]) -> str:
    return "".join(f"{sha}  {name}\n" for name, sha in sorted(rows))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest_of(source: Path) -> str:
    return manifest_text(entries_of_dir(source) if source.is_dir() else entries_of_zip(source))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 release_manifest.py", description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="the zip, or the folder it was extracted to")
    parser.add_argument("--out", type=Path, help="also write the list to this file (UTF-8, LF)")
    args = parser.parse_args(argv)
    try:
        text = manifest_of(args.source)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.out:
        args.out.write_bytes(text.encode("utf-8"))
    print(digest(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
