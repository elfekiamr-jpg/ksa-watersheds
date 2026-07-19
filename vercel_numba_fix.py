"""
Works around a suspected cause of the completely silent crashes we saw on
Vercel (no Python traceback at all — not even our own logging output,
which only happens with a hard native-level crash, not a normal
exception).

`numba` (a `pysheds` dependency, in turn a `delineator` dependency) JIT-
compiles functions to machine code tailored to the exact CPU it detects
at import time. Vercel Functions run inside a virtualized sandbox
(similar to AWS Lambda), and there's a known category of issue where
numba's autodetected CPU target includes instructions the sandbox's
virtual CPU doesn't actually support — causing an immediate, silent,
native-level crash (illegal instruction / segfault) the moment a
JIT-compiled function is first touched, with no chance for Python to
print anything.

The fix: force numba to generate conservative, generic machine code
instead of guessing the host's exact capabilities. This does not disable
JIT compilation or meaningfully change correctness — only which specific
CPU instructions the generated code is allowed to use. Verified locally
that a full real delineation still produces identical results with this
set (see chat notes).

Must be imported before numba gets imported by anything else — see the
top of app.py. Harmless on Docker/Render too, just unnecessary there.
"""
import os

os.environ.setdefault("NUMBA_CPU_NAME", "generic")
os.environ.setdefault("NUMBA_CPU_FEATURES", "")
