"""Rewrite Windows absolute paths in Hermes config files to Linux form.

Hermes agent configuration written on Windows (``config.yaml``,
``auth.json``, ``profiles/*/config.yaml``, ``profiles/*/.env``) contains
absolute paths in Windows form. After migrating to Linux (Modal) those
paths must point at the new mount point (e.g. ``/opt/data``). This module
replaces every occurrence of the given Windows path prefixes with a Linux
base path and normalizes the remainder of each path to forward slashes,
so the same config tree works on Linux unchanged.

Only the files listed above are ever touched; everything else under the
root is left alone. Files are read and written as UTF-8, and CRLF line
endings are normalized to LF (dos2unix style). Only files whose content
actually changed are written back.

``normalize_windows_paths`` is the public entry point (imported by
``hermes_migration.app``); running this module as a script exposes the
same operation as a small CLI.
"""

from __future__ import annotations

import argparse
import os
import pathlib

# Characters that terminate the backslash-delimited path tail following a
# replaced prefix. The tail is normalized only up to the first of these,
# so unrelated backslashes later on the same line (e.g. in other values)
# are left untouched.
_PATH_TAIL_STOP_CHARS = "\r\n\"',;:}]"

# Characters that may legally follow a replaced prefix. An occurrence whose
# next character is none of these (and not whitespace / end of text) is not
# a real path boundary — e.g. ``C:\...\hermes-cache\x`` must not be
# rewritten just because it starts with the ``...\hermes`` prefix.
_PREFIX_BOUNDARY_CHARS = "\\/\"',;:\r\n"


def _at_prefix_boundary(text: str, end: int) -> bool:
    """True if the character right after a prefix occurrence ends the path."""
    return (
        end >= len(text)
        or text[end] in _PREFIX_BOUNDARY_CHARS
        or text[end].isspace()
    )


def _is_target(rel_parts: tuple[str, ...], filename: str) -> bool:
    """Return True if (rel_parts, filename) matches a target file pattern."""
    if not rel_parts:
        return filename in ("config.yaml", "auth.json")
    return (
        len(rel_parts) == 2
        and rel_parts[0] == "profiles"
        and filename in ("config.yaml", ".env")
    )


def _target_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Recursively collect the config files that migration may rewrite."""
    targets: list[pathlib.Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        directory = pathlib.Path(dirpath)
        rel_parts = directory.relative_to(root).parts
        for filename in sorted(filenames):
            if _is_target(rel_parts, filename):
                targets.append(directory / filename)
    return sorted(targets, key=str)


def _replace_prefix(text: str, old_prefix: str, new_base: str) -> str:
    """Replace every occurrence of ``old_prefix`` with ``new_base`` in text.

    An occurrence counts only when the character right after the prefix is
    a path boundary (backslash, slash, quote, comma, colon, semicolon,
    newline, whitespace, or end of text). If a longer identifier such as
    ``hermes-cache`` follows instead, the occurrence is not a real path and
    is left alone. If a backslash-delimited path continues right after an
    occurrence (e.g. ``C:\\Users\\...\\hermes\\profiles\\default``), the
    backslashes of that tail are normalized to forward slashes, so the
    result is ``/opt/data/profiles/default``.
    """
    out: list[str] = []
    i = 0
    prefix_len = len(old_prefix)
    while True:
        pos = text.find(old_prefix, i)
        if pos == -1:
            out.append(text[i:])
            break
        end = pos + prefix_len
        if not _at_prefix_boundary(text, end):
            # Not a real path occurrence (e.g. 'hermes-cache'): keep the
            # character at pos and resume searching right after it.
            out.append(text[i : pos + 1])
            i = pos + 1
            continue
        out.append(text[i:pos])
        out.append(new_base)
        if end < len(text) and text[end] == "\\":
            # A Windows-style tail follows the prefix: convert its
            # backslashes (and any literal slashes) to forward slashes,
            # collapsing runs so JSON-escaped '\\\\' separators also work.
            j = end
            tail: list[str] = []
            while j < len(text) and text[j] not in _PATH_TAIL_STOP_CHARS:
                if text[j] == "\\" or text[j] == "/":
                    if not (tail and tail[-1] == "/"):
                        tail.append("/")
                else:
                    tail.append(text[j])
                j += 1
            out.extend(tail)
            i = j
        else:
            i = end
    return "".join(out)


def normalize_windows_paths(
    root: pathlib.Path, old_prefixes: list[str], new_base: str
) -> list[str]:
    """Rewrite Windows path prefixes in Hermes config files under root.

    Args:
        root: Directory to scan for target config files.
        old_prefixes: Windows path prefixes to replace, in any of the
            forms that may appear in the files (backslash-delimited,
            forward-slash, or MSYS ``/c/...`` style). For each prefix both
            the plain form and the JSON-escaped form (every backslash
            doubled) are tried, since JSON files such as ``auth.json``
            store the same path with doubled backslashes.
        new_base: Replacement path prefix, e.g. ``/opt/data``.

    Returns:
        Absolute paths of the files whose content was rewritten; empty
        list if nothing changed. A file that fails to read or write is
        reported on stdout and skipped, without aborting the whole run.
    """
    changed: list[str] = []
    if not root.is_dir():
        print(f"warning: root directory not found: {root}")
        return changed
    prefixes: list[str] = []
    for p in old_prefixes:
        if not p:
            continue
        for form in (p, p.replace("\\", "\\\\")):
            if form not in prefixes:
                prefixes.append(form)
    for path in _target_files(root):
        if not path.is_file():
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except Exception as exc:
            print(f"warning: skipped {path}: {type(exc).__name__}: {exc}")
            continue
        updated = text
        for prefix in prefixes:
            updated = _replace_prefix(updated, prefix, new_base)
        if "\r\n" in updated:
            updated = updated.replace("\r\n", "\n")
        if updated == text:
            continue
        try:
            # write_bytes, not write_text: text mode would translate LF
            # back to CRLF on Windows.
            path.write_bytes(updated.encode("utf-8"))
        except Exception as exc:
            print(f"error: failed to write {path}: {type(exc).__name__}: {exc}")
            continue
        changed.append(str(path.resolve()))
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Replace Windows absolute paths with a Linux base path in "
            "Hermes agent config files (config.yaml, auth.json, "
            "profiles/*/config.yaml, profiles/*/.env)."
        )
    )
    parser.add_argument("root", type=pathlib.Path, help="root directory to scan")
    parser.add_argument(
        "old_prefixes",
        help=(
            "comma-separated Windows path prefixes to replace, e.g. "
            "'C:\\Users\\Haruki\\AppData\\Local\\hermes,"
            "C:/Users/Haruki/AppData/Local/hermes,"
            "/c/Users/Haruki/AppData/Local/hermes'"
        ),
    )
    parser.add_argument("new_base", help="replacement base path, e.g. /opt/data")
    args = parser.parse_args()

    prefixes = [p.strip() for p in args.old_prefixes.split(",") if p.strip()]
    changed = normalize_windows_paths(args.root, prefixes, args.new_base)
    if changed:
        print(f"changed {len(changed)} file(s):")
        for path in changed:
            print(f"  {path}")
    else:
        print("no files changed")


if __name__ == "__main__":
    main()
