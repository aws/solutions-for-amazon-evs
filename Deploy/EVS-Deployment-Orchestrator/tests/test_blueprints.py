"""Blueprint schema validation — every shipped blueprint must be
well-formed. A missing or mistyped field here is exactly the class of
bug the Stage 1 build check exists to catch before a 4-7h deployment
discovers it.
"""

import glob
from pathlib import Path

import pytest
import yaml

from spec_generator.constants import EVC_MODE_BY_INSTANCE_TYPE

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_SECTIONS = {"dns", "evs", "sizing", "hostnames", "jumpbox", "hcx", "phase3"}
REQUIRED_EVS_KEYS = {
    "environment_name",
    "instance_type",
    "vcf_version",
    "terms_accepted",
}
SUPPORTED_VCF_PREFIXES = ("9.0", "9.1")
# EVC-mapped types plus i4i.metal, which is valid but takes no EVC mode.
KNOWN_INSTANCE_TYPES = set(EVC_MODE_BY_INSTANCE_TYPE) | {"i4i.metal"}


def _blueprints():
    """All parseable blueprints (the custom example is all comments)."""
    for path in sorted(glob.glob(str(REPO_ROOT / "blueprints" / "*.yaml"))):
        data = yaml.safe_load(Path(path).read_text())
        if data is not None:
            yield path, data


BLUEPRINTS = list(_blueprints())
IDS = [Path(p).name for p, _ in BLUEPRINTS]


def test_at_least_one_real_blueprint_exists():
    assert BLUEPRINTS, "no parseable blueprints found"


@pytest.mark.parametrize("path,data", BLUEPRINTS, ids=IDS)
class TestBlueprintSchema:
    def test_required_sections_present(self, path, data):
        missing = REQUIRED_SECTIONS - set(data)
        assert not missing, f"{path} missing sections: {missing}"

    def test_evs_section_complete(self, path, data):
        missing = REQUIRED_EVS_KEYS - set(data["evs"])
        assert not missing, f"{path} evs section missing: {missing}"

    def test_instance_type_known(self, path, data):
        itype = data["evs"]["instance_type"]
        assert itype in KNOWN_INSTANCE_TYPES, f"{path}: unknown instance_type {itype}"

    def test_vcf_version_supported(self, path, data):
        version = str(data["evs"]["vcf_version"])
        assert version.startswith(SUPPORTED_VCF_PREFIXES), (
            f"{path}: unsupported vcf_version {version}"
        )

    def test_esxi_hostnames_are_a_nonempty_list(self, path, data):
        esxi = data["hostnames"]["esxi"]
        assert isinstance(esxi, list) and esxi, f"{path}: hostnames.esxi must be a non-empty list"

    def test_dns_fqdn_present(self, path, data):
        assert data["dns"].get("fqdn"), f"{path}: dns.fqdn missing"

    def test_filename_matches_content(self, path, data):
        """i4i blueprints must declare i4i, i7i blueprints i7i — a
        mismatched example file would mislead every customer who copies it."""
        name = Path(path).name
        itype = data["evs"]["instance_type"]
        if name.startswith("i4i"):
            assert itype.startswith("i4i"), f"{path}: filename says i4i, content says {itype}"
        if name.startswith("i7i"):
            assert itype.startswith("i7i"), f"{path}: filename says i7i, content says {itype}"
