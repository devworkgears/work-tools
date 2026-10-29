"""Windows で展開して start.bat を押すだけで動く zip を作る（PC の Python 環境には何も入れない）。

使い方:
    python3 build_dist.py --cache <材料のフォルダ> [--out dist]
        [--version <版>] [--commit <sha>] [--materials-sha256 materials.sha256]

材料のフォルダに置くもの（ネットには出ない）:
    python-3.12.*-embed-amd64.zip（python.org の embeddable 版）
    openpyxl-*.whl・et_xmlfile-*.whl（PyPI の wheel）
    --materials-sha256 を渡すと、使う材料がそのファイルの全桁の sha256 と合うかを確かめてから作る

zip の中:
    bom-cost/start.bat     --version を渡すとウィンドウの title に版を出す
    bom-cost/VERSION.txt   --version を渡したときだけ（版と commit の sha）
    bom-cost/python/   embeddable 版を展開したもの（._pth に ..\\app と ..\\lib を足す）
    bom-cost/app/bomcost/
    bom-cost/lib/      wheel を展開したもの
    bom-cost/licenses/ Python・openpyxl・et_xmlfile のライセンス
    （DB は start.bat が bom-cost/data/bom-cost.sqlite に作る）
"""

import argparse
import hashlib
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "bomcost"
TOP = "bom-cost"
ZIP_NAME = "bom-cost-win64.zip"
EMBED_PATTERN = "python-*-embed-amd64.zip"
WHEEL_PATTERNS = {"openpyxl": "openpyxl-*.whl", "et_xmlfile": "et_xmlfile-*.whl"}
PTH_EXTRA = ("..\\app", "..\\lib")
LICENSE_PREFIXES = ("LICEN", "COPYING", "AUTHORS", "NOTICE")
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
LAUNCHER_NAME = "start.bat"
# 版は start.bat の title にそのまま入るので、cmd.exe の特殊文字（& | < > ^ % 等）を通さない
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
SHA256_LINE = re.compile(r"^([0-9a-f]{64}) [ *]?(\S+)$")


def launcher(version: str | None = None) -> str:
    """start.bat の中身。cmd.exe はコードページで読むので ASCII だけにする。改行は CRLF。"""
    title = "bom-cost" if version is None else f"bom-cost {version}"
    return "\r\n".join([
        "@echo off",
        "setlocal",
        f"title {title}",
        'set "HERE=%~dp0"',
        'cd /d "%HERE%"',
        'if not exist "%HERE%data" mkdir "%HERE%data"',
        '"%HERE%python\\python.exe" -E -m bomcost.app --db "%HERE%data\\bom-cost.sqlite"',
        "if errorlevel 1 pause",
        "",
    ])


LAUNCHER = launcher()


def version_text(version: str, commit: str | None = None) -> str:
    lines = [version] + ([commit] if commit else [])
    return "\r\n".join(lines) + "\r\n"


def read_sums(path: Path) -> dict[str, str]:
    """sha256sum 形式（<64桁>  <名前>）のファイルを {名前: sha256} にする。形の崩れ・重複は止める。"""
    sums: dict[str, str] = {}
    for n, line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        if not line.strip():
            continue
        m = SHA256_LINE.match(line)
        if not m or m.group(2) in sums:
            raise ValueError(f"{path.name} の {n} 行目が読めない")
        sums[m.group(2)] = m.group(1)
    return sums


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_materials(files: list[Path], sums_path: Path) -> None:
    """使う材料がちょうど sums_path の一覧どおりで、全桁の sha256 が合うことを確かめる。"""
    sums = read_sums(sums_path)
    names = sorted(p.name for p in files)
    if names != sorted(sums):
        raise ValueError(f"材料が {sums_path.name} の一覧と違う: {names} / {sorted(sums)}")
    for path in files:
        if sha256_of(path) != sums[path.name]:
            raise ValueError(f"{path.name} の sha256 が {sums_path.name} と合わない")


def find_one(cache: Path, pattern: str) -> Path:
    """cache の直下で pattern に当たるファイルがちょうど1つであることを確かめて返す。"""
    found = sorted(p for p in cache.glob(pattern) if p.is_file())
    if len(found) != 1:
        raise FileNotFoundError(f"{cache} に {pattern} がちょうど1つ必要（{len(found)} 個）")
    return found[0]


def pth_text(original: str) -> str:
    """元の ._pth のパスの行の後ろにアプリとライブラリの場所を足す（コメントの行・import site の扱いはそのまま）。"""
    lines = original.splitlines()
    last_path = max((i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")), default=-1)
    extra = [p for p in PTH_EXTRA if p not in lines]
    lines[last_path + 1:last_path + 1] = extra
    return "\r\n".join(lines) + "\r\n"


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or ":" in name:
        raise ValueError(f"zip の中の名前が不正: {name}")
    return path


def _is_license(path: PurePosixPath) -> bool:
    return path.name.upper().startswith(LICENSE_PREFIXES) or "licenses" in path.parts[1:-1]


def build(cache: Path, out_dir: Path, app_dir: Path = APP_DIR, version: str | None = None,
          commit: str | None = None, materials_sha256: Path | None = None) -> Path:
    """zip を out_dir に作ってそのパスを返す。同じ名前の zip は置き換える。"""
    if version is not None and not VERSION_RE.match(version):
        raise ValueError(f"版の形が不正: {version!r}")
    if commit is not None and (version is None or not COMMIT_RE.match(commit)):
        raise ValueError(f"commit は --version と一緒に、16進の sha で渡す: {commit!r}")
    embed = find_one(cache, EMBED_PATTERN)
    wheels = {name: find_one(cache, pattern) for name, pattern in WHEEL_PATTERNS.items()}
    if materials_sha256 is not None:
        verify_materials([embed, *wheels.values()], materials_sha256)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / ZIP_NAME
    partial = target.with_suffix(".zip.part")
    top = PurePosixPath(TOP)
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr(str(top / LAUNCHER_NAME), launcher(version).encode("ascii"))
        if version is not None:
            out.writestr(str(top / "VERSION.txt"), version_text(version, commit).encode("ascii"))

        with zipfile.ZipFile(embed) as z:
            pths = [n for n in z.namelist() if n.endswith("._pth")]
            if len(pths) != 1:
                raise ValueError(f"{embed.name} に ._pth がちょうど1つ必要（{len(pths)} 個）")
            if "LICENSE.txt" not in z.namelist():
                raise ValueError(f"{embed.name} に LICENSE.txt が無い")
            for info in z.infolist():
                if info.is_dir():
                    continue
                member = _safe_member(info.filename)
                data = z.read(info)
                if info.filename == pths[0]:
                    data = pth_text(data.decode("utf-8")).encode("utf-8")
                out.writestr(str(top / "python" / member), data)
            out.writestr(str(top / "licenses" / "python" / "LICENSE.txt"), z.read("LICENSE.txt"))

        for name, wheel in wheels.items():
            licenses = 0
            with zipfile.ZipFile(wheel) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    member = _safe_member(info.filename)
                    data = z.read(info)
                    out.writestr(str(top / "lib" / member), data)
                    if member.parts[0].endswith(".dist-info") and _is_license(member):
                        out.writestr(str(top / "licenses" / name / member.name), data)
                        licenses += 1
            if not licenses:
                raise ValueError(f"{wheel.name} にライセンスのファイルが無い")

        for path in sorted(app_dir.rglob("*")):
            rel = path.relative_to(app_dir)
            if not path.is_file() or SKIP_DIRS.intersection(rel.parts) or path.suffix in SKIP_SUFFIXES:
                continue
            out.writestr(str(top / "app" / app_dir.name / PurePosixPath(*rel.parts)), path.read_bytes())
    partial.replace(target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 build_dist.py", description="Windows 向けの配る zip を作る")
    parser.add_argument("--cache", required=True, type=Path, help="embeddable 版の zip と wheel を置いたフォルダ")
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="zip を置くフォルダ（既定 dist/）")
    parser.add_argument("--version", help="版（例 bom-cost-v0.1.0）。VERSION.txt と start.bat の title に出す")
    parser.add_argument("--commit", help="版の commit の sha（VERSION.txt に書く・--version と一緒に）")
    parser.add_argument("--materials-sha256", type=Path, help="材料の全桁の sha256 の一覧（例 materials.sha256）")
    args = parser.parse_args(argv)
    try:
        target = build(args.cache, args.out, version=args.version, commit=args.commit,
                       materials_sha256=args.materials_sha256)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
