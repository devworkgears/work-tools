"""The file list with sha256 and its digest, as written into the release notes."""

import contextlib
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import release_manifest

FILES = {
    "bom-cost/start.bat": b"@echo off\r\n",
    "bom-cost/python/python.exe": b"MZ",
    "bom-cost/lib/openpyxl/__init__.py": b"",
    "bom-cost/app/bomcost/web/index.html": b"<!doctype html>",
}


class ReleaseManifest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.zip = self.tmp / "dist.zip"
        with zipfile.ZipFile(self.zip, "w") as z:
            z.writestr("bom-cost/", b"")
            for name, data in FILES.items():
                z.writestr(name, data)

    def extract(self) -> Path:
        out = self.tmp / "x"
        with zipfile.ZipFile(self.zip) as z:
            z.extractall(out)
        return out

    def test_text_is_sorted_sha_and_path(self) -> None:
        text = release_manifest.manifest_of(self.zip)
        expected = "".join(f"{hashlib.sha256(FILES[n]).hexdigest()}  {n}\n" for n in sorted(FILES))
        self.assertEqual(text, expected)

    def test_zip_and_extracted_folder_agree(self) -> None:
        self.assertEqual(release_manifest.manifest_of(self.zip), release_manifest.manifest_of(self.extract()))

    def test_a_changed_file_changes_the_digest(self) -> None:
        before = release_manifest.digest(release_manifest.manifest_of(self.zip))
        folder = self.extract()
        (folder / "bom-cost" / "start.bat").write_bytes(b"@echo on\r\n")
        self.assertNotEqual(before, release_manifest.digest(release_manifest.manifest_of(folder)))

    def test_main_prints_digest_and_writes_list(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = release_manifest.main([str(self.zip), "--out", str(self.tmp / "m.txt")])
        self.assertEqual(code, 0)
        text = (self.tmp / "m.txt").read_bytes().decode("utf-8")
        self.assertEqual(out.getvalue().strip(), hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_duplicate_entries_stop(self) -> None:
        with zipfile.ZipFile(self.zip, "a") as z, self.assertWarns(UserWarning):
            z.writestr("bom-cost/start.bat", b"again")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(release_manifest.main([str(self.zip)]), 1)


if __name__ == "__main__":
    unittest.main()
