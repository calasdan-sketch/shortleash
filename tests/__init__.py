# Makes `python -m unittest discover -s tests` and `-s tests -t .` both work:
# the test modules import each other and _isolate as top-level names.
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
