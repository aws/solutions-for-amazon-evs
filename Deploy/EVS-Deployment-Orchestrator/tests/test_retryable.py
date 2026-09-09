"""Unit tests for orchestrator/retryable.py — the M6 transient classifier."""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "retryable", REPO_ROOT / "orchestrator" / "retryable.py"
)
retryable = importlib.util.module_from_spec(_spec)
sys.modules["retryable"] = retryable
_spec.loader.exec_module(retryable)

is_transient = retryable.is_transient


class FakeClientError(Exception):
    """Duck-typed botocore ClientError."""

    def __init__(self, code, message=""):
        self.response = {"Error": {"Code": code, "Message": message}}
        super().__init__(message or code)


class TestAwsErrorCodes:
    def test_ice_is_NOT_transient(self):
        # Metal ICE won't clear by resuming in a tight loop — it needs an
        # On-Demand Capacity Reservation or a different AZ. Fail fast.
        assert not is_transient(FakeClientError("InsufficientInstanceCapacity"))
        assert not is_transient(FakeClientError("InsufficientCapacityException"))

    def test_throttling_is_transient(self):
        assert is_transient(FakeClientError("ThrottlingException"))
        assert is_transient(FakeClientError("RequestLimitExceeded"))

    def test_quota_exceeded_is_NOT_transient(self):
        # Needs a quota bump or leaked-env cleanup — retrying can't fix it.
        assert not is_transient(FakeClientError("ServiceQuotaExceededException"))

    def test_access_denied_is_NOT_transient(self):
        assert not is_transient(FakeClientError("AccessDeniedException"))

    def test_validation_is_NOT_transient(self):
        assert not is_transient(FakeClientError("ValidationException"))


class TestEvsStateDetails:
    """EVS reports host/VLAN failure causes as free text in stateDetails."""

    def test_metal_host_ice_statedetails_NOT_transient(self):
        # EVS surfaces metal ICE as free text; it is fatal, not retryable.
        assert not is_transient(
            "Host creation failed: There is insufficient capacity for the "
            "requested reserved instance"
        )

    def test_skylark_vlan_internal_error(self):
        assert is_transient("VLAN create failed: internal error, please retry")

    def test_vcpu_limit_statedetails_not_transient(self):
        # The runbook's example of a customer-fixable cause: an EC2 vCPU
        # LimitExceededException surfaced in stateDetails.
        assert not is_transient(
            "Host creation failed: LimitExceededException - vCPU limit exceeded"
        )


class TestNetworkFlakes:
    def test_depot_timeout(self):
        assert is_transient("bundle download: depot request timed out after 300s")

    def test_ovftool_connection_reset(self):
        assert is_transient("ovftool: connection reset during upload")

    def test_installer_not_ready(self):
        assert is_transient("Installer not reachable yet")

    def test_dns_blip(self):
        assert is_transient("Temporary failure in name resolution")


class TestCodeBugsStayFatal:
    def test_python_traceback_not_transient(self):
        assert not is_transient("KeyError: 'clusterSpec'")

    def test_generic_runtime_error_not_transient(self):
        assert not is_transient(RuntimeError("spec validation failed: bad CIDR"))

    def test_empty_string_not_transient(self):
        assert not is_transient("")
