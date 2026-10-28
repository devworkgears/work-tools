"""配る zip を作るスクリプト。小さな偽の embeddable 版と wheel をその場で作って確かめる（ネットに出ない）。"""

import contextlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import build_dist

DEFAULT_PTH = "python312.zip\r\n.\r\n\r\n# Uncomment to run site.main() automatically\r\n#import site\r\n"


def _zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)


def make_cache(cache: Path) -> None:
    _zip(cache / "python-3.12.10-embed-amd64.zip", {
        "python.exe": b"MZ",
        "python312.zip": b"PK",
        "python312._pth": DEFAULT_PTH.encode("ascii"),
        "LICENSE.txt": b"PSF LICENSE",
    })
    _zip(cache / "openpyxl-3.1.5-py2.py3-none-any.whl", {
        "openpyxl/__init__.py": b"",
        "openpyxl-3.1.5.dist-info/LICENCE.rst": b"MIT openpyxl",
        "openpyxl-3.1.5.dist-info/METADATA": b"Name: openpyxl",
    })
    _zip(cache / "et_xmlfile-2.0.0-py3-none-any.whl", {
        "et_xmlfile/__init__.py": b"",
        "et_xmlfile-2.0.0.dist-info/LICENCE.rst": b"MIT et_xmlfile",
        "et_xmlfile-2.0.0.dist-info/LICENCE.python": b"PSF et_xmlfile",
    })


def make_app(app: Path) -> None:
    (app / "web").mkdir(parents=True)
    (app / "__pycache__").mkdir()
    (app / "__init__.py").write_text("")
    (app / "app.py").write_text("print('app')\n")
    (app / "web" / "index.html").write_text("<!doctype html>")
    (app / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\0")
    (app / "stray.pyc").write_bytes(b"\0")


class BuildDist(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cache = self.tmp / "cache"
        self.cache.mkdir()
        make_cache(self.cache)
        self.app = self.tmp / "src" / "bomcost"
        make_app(self.app)

    def build(self) -> zipfile.ZipFile:
        target = build_dist.build(self.cache, self.tmp / "dist", self.app)
        self.assertEqual(sorted(p.name for p in (self.tmp / "dist").iterdir()), ["bom-cost-win64.zip"])
        z = zipfile.ZipFile(target)
        self.addCleanup(z.close)
        return z

    def test_layout(self) -> None:
        names = set(self.build().namelist())
        self.assertEqual(names, {
            "bom-cost/起動.bat",
            "bom-cost/python/python.exe",
            "bom-cost/python/python312.zip",
            "bom-cost/python/python312._pth",
            "bom-cost/python/LICENSE.txt",
            "bom-cost/app/bomcost/__init__.py",
            "bom-cost/app/bomcost/app.py",
            "bom-cost/app/bomcost/web/index.html",
            "bom-cost/lib/openpyxl/__init__.py",
            "bom-cost/lib/openpyxl-3.1.5.dist-info/LICENCE.rst",
            "bom-cost/lib/openpyxl-3.1.5.dist-info/METADATA",
            "bom-cost/lib/et_xmlfile/__init__.py",
            "bom-cost/lib/et_xmlfile-2.0.0.dist-info/LICENCE.rst",
            "bom-cost/lib/et_xmlfile-2.0.0.dist-info/LICENCE.python",
            "bom-cost/licenses/python/LICENSE.txt",
            "bom-cost/licenses/openpyxl/LICENCE.rst",
            "bom-cost/licenses/et_xmlfile/LICENCE.rst",
            "bom-cost/licenses/et_xmlfile/LICENCE.python",
        })

    def test_pth_adds_app_and_lib_and_keeps_site_off(self) -> None:
        text = self.build().read("bom-cost/python/python312._pth").decode("ascii")
        self.assertEqual(text.split("\r\n"), [
            "python312.zip", ".", "..\\app", "..\\lib", "",
            "# Uncomment to run site.main() automatically", "#import site", "",
        ])

    def test_launcher_is_ascii_crlf_and_relative(self) -> None:
        data = self.build().read("bom-cost/起動.bat")
        text = data.decode("ascii")
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertIn('set "HERE=%~dp0"', text)
        self.assertIn('"%HERE%python\\python.exe" -E -m bomcost.app --db "%HERE%data\\bom-cost.sqlite"', text)
        self.assertIn('if not exist "%HERE%data" mkdir "%HERE%data"', text)
        self.assertNotRegex(text, r"[A-Za-z]:\\")

    def test_license_contents_are_copied(self) -> None:
        z = self.build()
        self.assertEqual(z.read("bom-cost/licenses/python/LICENSE.txt"), b"PSF LICENSE")
        self.assertEqual(z.read("bom-cost/licenses/openpyxl/LICENCE.rst"), b"MIT openpyxl")

    def test_rebuild_replaces_the_zip(self) -> None:
        self.build()
        self.build()

    def test_missing_or_duplicate_material_stops(self) -> None:
        (self.cache / "et_xmlfile-2.0.0-py3-none-any.whl").unlink()
        with self.assertRaises(FileNotFoundError):
            build_dist.build(self.cache, self.tmp / "dist", self.app)
        make_cache(self.cache)
        _zip(self.cache / "openpyxl-3.1.4-py2.py3-none-any.whl", {"openpyxl/__init__.py": b""})
        with self.assertRaises(FileNotFoundError):
            build_dist.build(self.cache, self.tmp / "dist", self.app)

    def test_wheel_without_license_stops(self) -> None:
        _zip(self.cache / "et_xmlfile-2.0.0-py3-none-any.whl", {"et_xmlfile/__init__.py": b""})
        with self.assertRaises(ValueError):
            build_dist.build(self.cache, self.tmp / "dist", self.app)

    def test_member_outside_the_folder_stops(self) -> None:
        _zip(self.cache / "et_xmlfile-2.0.0-py3-none-any.whl", {"../evil.py": b"", "x.dist-info/LICENCE.rst": b""})
        with self.assertRaises(ValueError):
            build_dist.build(self.cache, self.tmp / "dist", self.app)

    def test_default_app_is_the_real_package(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            code = build_dist.main(["--cache", str(self.cache), "--out", str(self.tmp / "out")])
        self.assertEqual(code, 0)
        with zipfile.ZipFile(self.tmp / "out" / "bom-cost-win64.zip") as z:
            names = z.namelist()
        self.assertIn("bom-cost/app/bomcost/app.py", names)
        self.assertIn("bom-cost/app/bomcost/web/index.html", names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc") or "/tests/" in n or "/samples/" in n])


if __name__ == "__main__":
    unittest.main()
