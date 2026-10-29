"""update_cleanup.py (1.4.2): what an update removes - only what the old version brought and nothing still needs."""
import os
import shutil
import tempfile
import time
import unittest

import update_cleanup as uc


class _Dist:
    def __init__(self, requires=()):
        self.requires = list(requires)


class PackageTests(unittest.TestCase):
    def setUp(self):
        # app-a needs shared; old-only needs its-dep and shared; voice-pkg came from a feature and needs feature-dep.
        self.installed = {
            "app-a": _Dist(["shared>=1"]),
            "old-only": _Dist(["its-dep", "shared", 'extra-thing ; extra == "full"']),
            "its-dep": _Dist(),
            "shared": _Dist(),
            "extra-thing": _Dist(),
            "voice-pkg": _Dist(["feature-dep", 'win-only ; sys_platform == "nonexistent"']),
            "feature-dep": _Dist(),
            "win-only": _Dist(),
            "pip": _Dist(), "setuptools": _Dist(),
        }

    def test_removes_what_only_the_old_requirements_brought(self):
        old = {"app-a": set(), "old-only": set()}
        new = {"app-a": set()}
        self.assertEqual(uc.unneeded(old, new, self.installed), ["its-dep", "old-only"])

    def test_a_requested_extra_counts_and_feature_packages_stay(self):
        old = {"app-a": set(), "old-only": {"full"}}
        gone = uc.unneeded(old, {"app-a": set()}, self.installed)
        self.assertIn("extra-thing", gone)
        for kept in ("shared", "voice-pkg", "feature-dep", "win-only", "pip", "setuptools"):
            self.assertNotIn(kept, gone)

    def test_nothing_changes_when_the_requirements_didnt(self):
        same = {"app-a": set(), "old-only": set()}
        self.assertEqual(uc.unneeded(same, same, self.installed), [])

    def test_reads_names_extras_and_skips_options(self):
        path = os.path.join(tempfile.mkdtemp(), "r.txt")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# comment\n-r other.txt\nUvicorn[standard]==0.30\nsome_pkg>=1 ; python_version>'3'\n\n")
        self.assertEqual(uc.read_requirements(path), {"uvicorn": {"standard"}, "some-pkg": set()})

    def test_the_real_environment_resolves(self):
        # The app's own requirements against its own .venv: nothing they need is ever "unneeded".
        req = uc.read_requirements(os.path.join(uc.ROOT, "requirements.txt"))
        self.assertEqual(uc.unneeded(req, req, uc.installed_distributions()), [])


class TidyTests(unittest.TestCase):
    def test_removes_pycache_and_old_ini_backups_but_not_data_or_the_venv(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        for d in ("__pycache__", "agent/__pycache__", ".venv/Lib/__pycache__", "data/__pycache__"):
            os.makedirs(os.path.join(root, d))
            open(os.path.join(root, d, "x.pyc"), "wb").close()
        for i in range(5):
            p = os.path.join(root, f"models.ini.bak-2026100{i}")
            open(p, "w").close()
            os.utime(p, (time.time() + i, time.time() + i))
        uc.tidy(root)
        self.assertFalse(os.path.exists(os.path.join(root, "__pycache__")))
        self.assertFalse(os.path.exists(os.path.join(root, "agent", "__pycache__")))
        self.assertTrue(os.path.exists(os.path.join(root, ".venv", "Lib", "__pycache__")))
        self.assertTrue(os.path.exists(os.path.join(root, "data", "__pycache__")))
        self.assertEqual(sorted(f for f in os.listdir(root) if f.startswith("models.ini")),
                         ["models.ini.bak-20261002", "models.ini.bak-20261003", "models.ini.bak-20261004"])


if __name__ == "__main__":
    unittest.main()
