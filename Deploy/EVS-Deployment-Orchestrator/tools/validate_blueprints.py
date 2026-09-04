#!/usr/bin/env python3
"""Validate blueprint YAML files against the orchestrator's expected schema.

Usage: python3 tools/validate_blueprints.py blueprints/*.yaml
Exit 0 if every parseable blueprint is valid; 1 otherwise. Files that
parse to nothing (all-comments examples) are skipped with a note.
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "spec-generator"))
from constants import EVC_MODE_BY_INSTANCE_TYPE  # noqa: E402

REQUIRED_SECTIONS = {"dns", "evs", "sizing", "hostnames", "jumpbox", "hcx", "phase3"}
REQUIRED_EVS_KEYS = {"environment_name", "instance_type", "vcf_version", "terms_accepted"}
SUPPORTED_VCF_PREFIXES = ("9.0", "9.1")
KNOWN_INSTANCE_TYPES = set(EVC_MODE_BY_INSTANCE_TYPE) | {"i4i.metal"}


def validate_blueprint(path: Path) -> list[str]:
    """Return a list of problems; empty list == valid."""
    problems = []
    data = yaml.safe_load(path.read_text())
    if data is None:
        return ["SKIP"]  # all-comments example file

    missing = REQUIRED_SECTIONS - set(data)
    if missing:
        problems.append(f"missing sections: {sorted(missing)}")
        return problems  # section checks below would KeyError

    evs = data["evs"]
    missing_evs = REQUIRED_EVS_KEYS - set(evs)
    if missing_evs:
        problems.append(f"evs section missing: {sorted(missing_evs)}")
    else:
        if evs["instance_type"] not in KNOWN_INSTANCE_TYPES:
            problems.append(f"unknown instance_type: {evs['instance_type']}")
        if not str(evs["vcf_version"]).startswith(SUPPORTED_VCF_PREFIXES):
            problems.append(f"unsupported vcf_version: {evs['vcf_version']}")

    esxi = data["hostnames"].get("esxi")
    if not (isinstance(esxi, list) and esxi):
        problems.append("hostnames.esxi must be a non-empty list")

    if not data["dns"].get("fqdn"):
        problems.append("dns.fqdn missing")

    return problems


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: validate_blueprints.py <blueprint.yaml> [...]", file=sys.stderr)
        return 2

    failed = False
    for arg in argv:
        path = Path(arg)
        problems = validate_blueprint(path)
        if problems == ["SKIP"]:
            print(f"  SKIP  {path} (no content — comments-only example)")
        elif problems:
            failed = True
            print(f"  FAIL  {path}")
            for p in problems:
                print(f"        - {p}")
        else:
            print(f"  OK    {path}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
