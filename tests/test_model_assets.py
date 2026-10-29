"""model_assets.py (D50): a known model is offered its vision file; the download is checked against the published
size and SHA-256 before models.ini gets its mmproj line - next to the model's own line, the rest untouched."""
import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

import llama_local
import model_assets

INI = ("[*]\r\nngl = auto\r\n\r\n; my note\r\n[Ornith-1.5-9B-Q4_K_M]\r\nmodel = {dir}/Ornith-1.5-9B-Q4_K_M.gguf\r\n"
       "ctx-size = 32256\r\n\r\n[Other]\r\nmodel = {dir}/other.gguf\r\n")


class _Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self, n):
        for i in range(0, len(self.data), n):
            yield self.data[i:i + n]


class _Client:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url):
        return _Resp(self.data)


class ModelAssetTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ini = os.path.join(self.dir, "models.ini")
        with open(self.ini, "wb") as f:
            f.write(INI.format(dir=self.dir).encode())
        for p in (patch.object(llama_local, "_ini_path", lambda: self.ini),):
            p.start()
            self.addCleanup(p.stop)
        llama_local._ini_cache.update(key=None, configs=None)
        self.addCleanup(llama_local._ini_cache.update, key=None, configs=None)
        self.payload = b"vision-tower" * 1000
        known = {"ornith-1.5-9b": dict(model_assets.KNOWN["ornith-1.5-9b"], size=len(self.payload),
                                       sha256=hashlib.sha256(self.payload).hexdigest())}
        p = patch.dict(model_assets.KNOWN, known)
        p.start()
        self.addCleanup(p.stop)
        model_assets._state.update(state="idle", model="", done=0, total=0, error="")

    def run_download(self, data):
        with patch.object(model_assets.netlayer, "client", lambda *a, **k: _Client(data)):
            offer = model_assets.offers()[0]
            model_assets._run(offer["model"], offer["dest"], model_assets.KNOWN[offer["key"]])
        return offer

    def test_windows_paths_are_read_as_windows_paths(self):
        self.assertEqual(model_assets.known_for(r"A:\ClankerLocalCpp\Clankers\Ornith-1.5-9B-Q4_K_M.gguf")["key"], "ornith-1.5-9b")
        self.assertIsNone(model_assets.known_for(r"E:\x\gemma-4-E4B_q4_0-it.gguf"))

    def test_only_a_known_model_without_one_is_offered(self):
        offers = model_assets.offers()
        self.assertEqual([o["model"] for o in offers], ["Ornith-1.5-9B-Q4_K_M"])
        self.assertTrue(offers[0]["dest"].endswith("mmproj-Ornith-1.5-9B-f16.gguf"))

    def test_a_checked_download_adds_the_line_and_keeps_the_rest(self):
        offer = self.run_download(self.payload)
        self.assertEqual(model_assets.status()["state"], "done")
        text = open(self.ini, "rb").read().decode()
        self.assertIn(f"Ornith-1.5-9B-Q4_K_M.gguf\r\nmmproj = {offer['dest']}\r\nctx-size = 32256", text)
        self.assertIn("; my note", text)                                 # comments survive
        self.assertNotIn("mmproj", text.split("[Other]")[1])
        self.assertEqual(model_assets.offers(), [])                      # it has one now
        self.assertTrue([n for n in os.listdir(self.dir) if n.startswith("models.ini.bak-")])

    def test_a_download_that_does_not_match_changes_nothing(self):
        before = open(self.ini, "rb").read()
        offer = self.run_download(self.payload[:-1] + b"X")
        self.assertEqual(model_assets.status()["state"], "failed")
        self.assertIn("SHA-256", model_assets.status()["error"])
        self.assertEqual(open(self.ini, "rb").read(), before)
        self.assertFalse(os.path.exists(offer["dest"]) or os.path.exists(offer["dest"] + ".part"))


if __name__ == "__main__":
    unittest.main()
