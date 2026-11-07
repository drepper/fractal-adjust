#!/usr/bin/env python3
"""Generate fractal-adjust.spec from fractal-adjust.spec.in.

Every value that also exists elsewhere in the project is taken from its single source
instead of being repeated in the spec:

  @NAME@      [project] name in pyproject.toml
  @SUMMARY@   [project] description in pyproject.toml
  @LICENSE@   [project] license in pyproject.toml
  @PACKAGER@  first [project] authors entry in pyproject.toml, as "Name <email>"
  @VERSION@   __version__ in fractal_adjust/__init__.py
  @APP_ID@    APP_ID in fractal_adjust/gui.py (also the desktop file's name)
  @MODULE@    the package listed in [tool.setuptools] packages in pyproject.toml
  @RELEASE@   the "%global baserelease N" line of the template itself

When the version changes, write the then-current version and release literally into the
old top %changelog entry and start a new top entry ending in "- @VERSION@-@RELEASE@".

The Python files are parsed, not imported, so no runtime dependency is needed.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

TOP = Path(__file__).resolve().parent.parent
PLACEHOLDER = re.compile(r"@([A-Z_]+)@")


def _assignment(path: Path, name: str) -> str:
    """Return the string constant assigned to @p name at the top level of @p path."""
    for node in ast.parse(path.read_text(encoding="utf-8"), str(path)).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    raise SystemExit(f"{path}: no string assignment to {name}")


def values() -> dict[str, str]:
    """Collect all substitution values from their sources."""
    with (TOP / "pyproject.toml").open("rb") as f:
        pyproject = tomllib.load(f)
    project = pyproject["project"]
    module = pyproject["tool"]["setuptools"]["packages"][0]
    author = project["authors"][0]
    return {
        "NAME": project["name"],
        "SUMMARY": project["description"],
        "LICENSE": project["license"],
        "PACKAGER": f"{author['name']} <{author['email']}>",
        "MODULE": module,
        "VERSION": _assignment(TOP / module / "__init__.py", "__version__"),
        "APP_ID": _assignment(TOP / module / "gui.py", "APP_ID"),
    }


def release(template: str) -> str:
    """The release number from the template's "%global baserelease N" line."""
    match = re.search(r"^%global\s+baserelease\s+(\d+)\s*$", template, re.MULTILINE)
    if match is None:
        raise SystemExit("template lacks a \"%global baserelease N\" line")
    return match.group(1)


def render(template: str, subst: dict[str, str]) -> str:
    """Replace every @KEY@ in @p template; unknown keys are an error."""
    unknown = sorted({m.group(1) for m in PLACEHOLDER.finditer(template)} - subst.keys())
    if unknown:
        raise SystemExit(f"unknown placeholders: {', '.join(unknown)}")
    return PLACEHOLDER.sub(lambda m: subst[m.group(1)], template)


def main() -> int:
    """Program entry point."""
    parser = argparse.ArgumentParser(description="Generate the RPM spec file from its template.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--get", metavar="KEY", help="print one value, e.g. VERSION")
    group.add_argument("template", nargs="?", type=Path, help="spec template to render to stdout")
    args = parser.parse_args()
    subst = values()
    if args.get is not None:
        if args.get not in subst:
            parser.error(f"unknown key {args.get}; known: {', '.join(subst)}")
        print(subst[args.get])
    else:
        template = args.template.read_text(encoding="utf-8")
        sys.stdout.write(render(template, subst | {"RELEASE": release(template)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
