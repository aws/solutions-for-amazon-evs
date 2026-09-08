"""Drift test: the two EVC maps must stay identical.

``spec-generator/constants.py`` (EVC_MODE_BY_INSTANCE_TYPE) deliberately
mirrors the orchestrator's ``_EVC_MODE_BY_INSTANCE_TYPE`` in
``orchestrator/evs_environment/sddc_spec_builder.py``. The comment in
constants.py says they must match exactly; nothing enforced that until
now. The orchestrator copy is extracted via AST (not imported) so this
test has no dependency on the orchestrator's runtime imports.
"""

import ast
from pathlib import Path

from spec_generator.constants import EVC_MODE_BY_INSTANCE_TYPE

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCH_BUILDER = REPO_ROOT / "orchestrator" / "evs_environment" / "sddc_spec_builder.py"


def _extract_orchestrator_evc_map():
    """Pull _EVC_MODE_BY_INSTANCE_TYPE's dict literal out of the
    orchestrator source without importing it."""
    tree = ast.parse(ORCH_BUILDER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "_EVC_MODE_BY_INSTANCE_TYPE"
                ):
                    return ast.literal_eval(node.value)
    raise AssertionError(
        "_EVC_MODE_BY_INSTANCE_TYPE not found in orchestrator sddc_spec_builder.py"
    )


class TestEvcMapParity:
    def test_maps_are_identical(self):
        assert _extract_orchestrator_evc_map() == EVC_MODE_BY_INSTANCE_TYPE

    def test_i4i_metal_deliberately_excluded(self):
        """i4i must NOT get an EVC mode: setting one breaks VCF 9.1
        bringup with EVCAdmissionFailedVmActive (see constants.py)."""
        assert "i4i.metal" not in EVC_MODE_BY_INSTANCE_TYPE

    def test_i7i_maps_to_sapphire_rapids(self):
        # Only the instance types Amazon EVS actually supports in-region carry
        # an EVC mode. i7i.metal-24xl is the supported SapphireRapids metal type;
        # do not assert types EVS does not offer (the map and evs:GetVersions
        # must agree).
        assert EVC_MODE_BY_INSTANCE_TYPE["i7i.metal-24xl"] == "INTEL_SAPPHIRERAPIDS"
        assert all(mode == "INTEL_SAPPHIRERAPIDS" for mode in EVC_MODE_BY_INSTANCE_TYPE.values())
