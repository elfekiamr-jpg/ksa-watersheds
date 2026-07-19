"""
Works around a Vercel-specific packaging gap: its Python function bundler
strips `.pyi` type-stub files from vendored dependencies (they're normally
dev/type-checking-only, so most bundlers assume they're safe to drop).
`scikit-image` (a `pysheds` dependency, in turn a `delineator` dependency)
uses `lazy_loader.attach_stub`, which reads that `.pyi` file *at runtime*
to know which real submodules/functions to lazily wire up — so when the
file is missing, import fails immediately with:

    ValueError: Cannot load imports from non-existent stub '.../skimage/__init__.pyi'

This is a known, cross-tool issue (PyInstaller and Nuitka have hit the
same thing packaging scikit-image) — not something specific to our code.

The fix: bundle our own copies of the `.pyi` files (see `skimage_stubs/`,
committed into this repo so they're never subject to Vercel's stripping),
and monkeypatch `lazy_loader.attach_stub` to fall back to our copy's
content whenever the real file next to the installed package is missing.
`lazy_loader.attach_stub` only *parses* the `.pyi` file to know which real
submodules exist — the real submodule code itself isn't affected by any
of this, so this only restores the missing piece of information, not the
functionality.

Must be imported before anything that imports `skimage` — see the top of
app.py.
"""
import ast
import os

import lazy_loader

_STUBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skimage_stubs")

_original_attach_stub = lazy_loader.attach_stub


def _bundled_stub_path(package_name):
    """Map e.g. 'skimage.measure' -> skimage_stubs/measure/__init__.pyi."""
    if package_name == "skimage":
        rel_parts = ["__init__.pyi"]
    elif package_name.startswith("skimage."):
        rel_parts = package_name.split(".")[1:] + ["__init__.pyi"]
    else:
        return None
    candidate = os.path.join(_STUBS_DIR, *rel_parts)
    return candidate if os.path.exists(candidate) else None


def _patched_attach_stub(package_name, filename):
    real_stub = (
        filename if filename.endswith("i") else f"{os.path.splitext(filename)[0]}.pyi"
    )
    if os.path.exists(real_stub):
        return _original_attach_stub(package_name, filename)

    fallback = _bundled_stub_path(package_name)
    if fallback is None:
        # No bundled fallback for this package — preserve the original
        # (informative) error rather than fail silently on something
        # unexpected.
        return _original_attach_stub(package_name, filename)

    with open(fallback) as f:
        stub_node = ast.parse(f.read())
    visitor = lazy_loader._StubVisitor()
    visitor.visit(stub_node)
    return lazy_loader.attach(package_name, visitor._submodules, visitor._submod_attrs)


lazy_loader.attach_stub = _patched_attach_stub
