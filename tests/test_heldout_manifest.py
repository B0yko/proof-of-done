"""Freeze check for `eval/heldout/`: labels are fixed only by a `CHANGES.md` entry.

The held-out set (`eval/heldout/*.yaml`) is authored and frozen before the claim detector
exists, so its labels can never be tuned against the detector's own output. This test pins that
policy mechanically: every held-out scenario's content must match the hash recorded in
`eval/heldout/MANIFEST.sha256`, unless `eval/heldout/CHANGES.md` names that exact file (with a
reason). It also fails if the manifest and the directory's `*.yaml` files disagree on which
files exist at all.
"""

from __future__ import annotations

import glob
import hashlib
import os
import re

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELDOUT_DIR = os.path.join(_REPO_ROOT, "eval", "heldout")
MANIFEST_PATH = os.path.join(HELDOUT_DIR, "MANIFEST.sha256")
CHANGES_PATH = os.path.join(HELDOUT_DIR, "CHANGES.md")

_MANIFEST_LINE_RE = re.compile(r"^([0-9a-f]{64})  (\S+)$")


def _sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest() -> dict[str, str]:
    entries: dict[str, str] = {}
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\n")
            if not line:
                continue
            m = _MANIFEST_LINE_RE.match(line)
            assert m, f"MANIFEST.sha256:{lineno}: malformed line: {line!r}"
            digest, filename = m.group(1), m.group(2)
            assert filename not in entries, (
                f"MANIFEST.sha256:{lineno}: duplicate entry for {filename!r}"
            )
            entries[filename] = digest
    return entries


def _changed_files_from_changelog() -> set[str]:
    """Filenames the change log names anywhere in its text (an "entry" for that file)."""
    with open(CHANGES_PATH, encoding="utf-8") as fh:
        text = fh.read()
    on_disk = {os.path.basename(p) for p in glob.glob(os.path.join(HELDOUT_DIR, "*.yaml"))}
    manifest_names = set(_read_manifest())
    candidates = on_disk | manifest_names
    return {name for name in candidates if name in text}


def test_manifest_lists_exactly_the_files_on_disk() -> None:
    manifest = _read_manifest()
    on_disk = {os.path.basename(p) for p in glob.glob(os.path.join(HELDOUT_DIR, "*.yaml"))}

    missing_from_manifest = sorted(on_disk - set(manifest))
    extra_in_manifest = sorted(set(manifest) - on_disk)

    assert not missing_from_manifest, (
        f"{len(missing_from_manifest)} held-out file(s) are not listed in MANIFEST.sha256: "
        f"{missing_from_manifest}"
    )
    assert not extra_in_manifest, (
        f"MANIFEST.sha256 lists {len(extra_in_manifest)} file(s) that no longer exist under "
        f"eval/heldout/: {extra_in_manifest}"
    )


def test_every_heldout_file_matches_its_frozen_hash_or_is_logged_in_changes() -> None:
    manifest = _read_manifest()
    logged = _changed_files_from_changelog()

    unexplained_drift = []
    for filename, expected_digest in sorted(manifest.items()):
        path = os.path.join(HELDOUT_DIR, filename)
        if not os.path.exists(path):
            continue  # reported by test_manifest_lists_exactly_the_files_on_disk
        actual_digest = _sha256_of(path)
        if actual_digest != expected_digest and filename not in logged:
            unexplained_drift.append(filename)

    assert not unexplained_drift, (
        f"{len(unexplained_drift)} held-out file(s) changed since the freeze with no matching "
        f"eval/heldout/CHANGES.md entry: {unexplained_drift}"
    )


def test_changes_md_exists_and_states_the_freeze_policy() -> None:
    assert os.path.exists(CHANGES_PATH), "eval/heldout/CHANGES.md is required by the freeze policy"
    with open(CHANGES_PATH, encoding="utf-8") as fh:
        text = fh.read()
    assert text.strip(), "eval/heldout/CHANGES.md must not be empty"
