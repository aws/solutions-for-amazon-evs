# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Check that a blueprint was written for this version of the orchestrator.

A blueprint declares the orchestrator releases it works with:

    requires:
      edo: ">=1.0.0, <2.0.0"

The range is a comma-separated list of comparisons (>=, >, <=, <, ==), each
against a Semantic Versioning 2.0.0 version. At launch the orchestrator
compares its own version, read from the VERSION file next to its folder, with
the range and stops before deploying anything when the version is outside it.

A blueprint without ``requires`` is treated as written for v1.0.0, the first
release, so it gets the range ``DEFAULT_REQUIREMENT`` (>=1.0.0, <2.0.0): it runs
on every 1.x release and stops on 2.0.0 or later. Existing blueprints keep
working without a change. Top-level sections the orchestrator
does not know are reported as warnings too: an older orchestrator would
otherwise silently ignore a section a newer blueprint relies on.

Standard library only: this runs before the orchestrator's other imports are
needed, and must not add a dependency to the runner.
"""

import re
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"

RELEASES_URL = "https://github.com/aws/solutions-for-amazon-evs/releases"

# The range assumed for a blueprint with no requires field: written for v1.0.0.
DEFAULT_REQUIREMENT = ">=1.0.0, <2.0.0"

# Every top-level section the orchestrator reads, including the ones the
# CloudFormation template adds to the blueprint before launch (aws, vpc,
# esxi_prework). Keys starting with "_" are set internally and are ignored.
KNOWN_SECTIONS = frozenset({
    "requires", "dns", "evs", "hcx", "hostnames", "jumpbox", "phase3",
    "sizing", "vpc", "aws", "aws_config", "esxi_prework",
    "notification_topic_arn", "checkpoint_path", "hosts", "tags",
})

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_COMPARISON = re.compile(r"^(>=|<=|==|>|<)\s*(\S+)$")


class BlueprintVersionError(ValueError):
    """The blueprint's ``requires`` field is invalid or excludes this version."""


def parse_version(text):
    """Return a sortable key for a SemVer version, or None if it isn't one.

    Follows SemVer 2.0.0 precedence: a pre-release (1.0.0-rc.1) sorts before
    its release, numeric identifiers sort numerically and before
    alphanumeric ones, and build metadata (+...) is ignored.
    """
    m = _SEMVER.match(str(text).strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    if pre is None:
        pre_key = (1,)
    else:
        pre_key = (0,) + tuple(
            (0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")
        )
    return (int(major), int(minor), int(patch), pre_key)


def read_orchestrator_version(path=VERSION_FILE):
    """The orchestrator's own version, or None when VERSION is missing or invalid."""
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return None
    return text if parse_version(text) else None


def parse_range(spec):
    """Turn ">=1.0.0, <2.0.0" into [(op, key, text), ...]."""
    if not isinstance(spec, str) or not spec.strip():
        raise BlueprintVersionError(
            f"requires.edo must be a version range such as \">=1.0.0, <2.0.0\", got {spec!r}")
    comparisons = []
    for part in spec.split(","):
        m = _COMPARISON.match(part.strip())
        key = parse_version(m.group(2)) if m else None
        if key is None:
            raise BlueprintVersionError(
                f"requires.edo has an invalid comparison {part.strip()!r} in {spec!r}. "
                f"Use >=, >, <=, < or == followed by a version such as 1.0.0, "
                f"for example \">=1.0.0, <2.0.0\"")
        comparisons.append((m.group(1), key, m.group(2)))
    return comparisons


def satisfies(version, spec):
    """True when ``version`` is inside the range ``spec``."""
    key = parse_version(version)
    if key is None:
        raise BlueprintVersionError(f"{version!r} is not a version")
    ops = {
        ">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
        "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
        "==": lambda a, b: a == b,
    }

    def upper(bound):
        # An upper bound on a release excludes that release's pre-releases:
        # "<2.0.0" means "before 2.0.0", so 2.0.0-rc.1, the candidate for the
        # MAJOR release that breaks the blueprint, is outside it. SemVer
        # precedence alone would put 2.0.0-rc.1 below 2.0.0.
        return key[:3] + ((1,),) if bound[3] == (1,) else key

    return all(ops[op](upper(bound) if op in ("<", "<=") else key, bound)
               for op, bound, _ in parse_range(spec))


def requirement(config):
    """The blueprint's requires.edo range and whether it was declared.

    A blueprint without ``requires`` is assumed to be written for v1.0.0 and
    gets DEFAULT_REQUIREMENT.
    """
    requires = config.get("requires")
    if requires is None:
        return DEFAULT_REQUIREMENT, False
    if not isinstance(requires, dict) or "edo" not in requires:
        raise BlueprintVersionError(
            f"requires must be a mapping with an 'edo' version range, such as "
            f"requires: {{ edo: \"{DEFAULT_REQUIREMENT}\" }}, got {requires!r}")
    return requires["edo"], True


def check_blueprint(config, orchestrator_version):
    """Check a loaded blueprint against the orchestrator's version.

    Returns a list of warnings. Raises BlueprintVersionError when the blueprint
    can't run on this version, so the caller stops before deploying anything.
    """
    warnings = []
    if not isinstance(config, dict):
        raise BlueprintVersionError("the blueprint must be a YAML mapping")

    unknown = sorted(k for k in config
                     if isinstance(k, str) and not k.startswith("_") and k not in KNOWN_SECTIONS)
    if unknown:
        warnings.append(
            f"the blueprint has sections this orchestrator doesn't use, which are ignored: "
            f"{', '.join(unknown)}. Check for a typo, or a blueprint written for a newer "
            f"release (see {RELEASES_URL}).")

    spec, _declared = requirement(config)
    parse_range(spec)

    if orchestrator_version is None:
        warnings.append(
            f"this orchestrator has no valid VERSION file, so the blueprint's requires.edo "
            f"({spec}) can't be checked.")
        return warnings
    if not satisfies(orchestrator_version, spec):
        raise BlueprintVersionError(
            f"this blueprint needs EVS Deployment Orchestrator {spec}, but this is "
            f"{orchestrator_version}. Deploy with the template from a release in that "
            f"range ({RELEASES_URL}), or use a blueprint written for "
            f"{orchestrator_version}. Nothing was deployed.")
    return warnings
