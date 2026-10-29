"""The test suite. Run from the repo root: .venv\\Scripts\\python.exe -m unittest discover -s tests -t ."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.dirname(HERE), HERE):          # the app's modules, and helpers like testing_fakes
    if p not in sys.path:
        sys.path.insert(0, p)
