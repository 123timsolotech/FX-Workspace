"""
installer/verify_tor.py
=======================
Build step (installer/build.ps1): check that the Tor Expert Bundle the build downloaded is signed by the Tor Browser
developers, before a byte of it goes into the installer. No GnuPG needed: PGPy (pip, build-time only) checks the
detached OpenPGP signature against their key, and the key itself must have the pinned fingerprint below - a key
server or mirror can't swap in another.

    python verify_tor.py <bundle.tar.gz> <bundle.tar.gz.asc> <signing-key.asc>

Prints the bundle's SHA-256 and exits 0 when the signature is good; exits 1 otherwise.
"""
import hashlib
import sys

# The Tor Browser Developers signing key (torproject.org: "How can I verify Tor Browser's signature?").
TOR_BROWSER_KEY = "EF6E286DDA85EA2A4BA7DE684E2C6E8793298290"


def fingerprints(key) -> set:
    out = {str(key.fingerprint).replace(" ", "").upper()}
    out |= {str(k.fingerprint).replace(" ", "").upper() for k in key.subkeys.values()}
    return out


def main(bundle: str, sig_path: str, key_path: str) -> int:
    import warnings
    warnings.filterwarnings("ignore")          # PGPy's notes about checks it doesn't do (the key is pinned instead)
    import pgpy
    key, _ = pgpy.PGPKey.from_file(key_path)
    if str(key.fingerprint).replace(" ", "").upper() != TOR_BROWSER_KEY:
        print(f"wrong key: {key.fingerprint}", file=sys.stderr)
        return 1
    sig = pgpy.PGPSignature.from_file(sig_path)
    with open(bundle, "rb") as f:
        data = f.read()
    signer = str(sig.signer).upper()
    if not any(fp.endswith(signer) for fp in fingerprints(key)):
        print(f"signed by another key: {sig.signer}", file=sys.stderr)
        return 1
    signing = next((k for k in [key, *key.subkeys.values()] if str(k.fingerprint).replace(" ", "").upper().endswith(signer)), key)
    if not signing.verify(data, sig):
        print("BAD signature", file=sys.stderr)
        return 1
    print(hashlib.sha256(data).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]) if len(sys.argv) == 4 else 2)
