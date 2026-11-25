# Test-run shim: prefer this checkout's Lib/ over any fontTools installed into
# site-packages, so running pytest from this directory tests the working tree.
import os
import sys

_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)
