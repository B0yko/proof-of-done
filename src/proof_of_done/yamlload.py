"""Minimal YAML loading via the vendored, stdlib-only PyYAML copy.

The hook path must not import any third-party package, so this module reaches into
``proof_of_done._vendor.yaml`` instead of a real ``yaml`` install. The import happens inside
the function (not at module import time) so importing this module has no side effect beyond
making ``safe_load`` available, and so the vendored package is only loaded when YAML parsing is
actually needed.
"""

from __future__ import annotations

from typing import Any


def safe_load(text: str) -> Any:
    """Parse ``text`` as YAML using only the vendored, pure-Python PyYAML loader.

    Equivalent to upstream ``yaml.safe_load``: safe against arbitrary object construction.
    """
    from proof_of_done._vendor.yaml import safe_load as _vendored_safe_load

    return _vendored_safe_load(text)
