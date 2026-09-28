# Vendored: PyYAML

- Name: PyYAML
- Version: 6.0.3
- Source: https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz
- sha256: d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f
- License: MIT (see `yaml/LICENSE`, copied unmodified from the sdist)

Only the pure-Python package (`lib/yaml/*.py` in the sdist) is vendored, under
`proof_of_done/_vendor/yaml/`. The optional `_yaml` C extension (libyaml bindings) is not
vendored; `yaml/cyaml.py`'s import of it was rewritten from the upstream absolute
`from yaml._yaml import CParser, CEmitter` to a relative `from ._yaml import CParser, CEmitter`
so it never resolves against an unrelated top-level `yaml` install. `yaml/__init__.py` already
wraps `from .cyaml import *` in a `try/except ImportError`, so the missing `_yaml` module is the
expected, safe path: `__with_libyaml__` stays `False` and the pure-Python loader is used. No
other file needed an import change.

Only `yaml.safe_load` is used by this project (see `proof_of_done/yamlload.py`). The vendored
copy is otherwise unmodified from upstream.
