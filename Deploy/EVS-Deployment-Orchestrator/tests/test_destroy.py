"""Unit tests for destroy.py resource discovery.

The safety-critical property here: destroy.py must only ever treat a VPC as
deletable (stack_created_vpc=True) when the bootstrap stack actually created it
(has an AWS::EC2::VPC resource named 'Vpc'). A customer's BYO-VPC is referenced,
not created, so it has no such resource and must never be flagged for deletion —
even when the stack failed early and has no VpcId output.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("destroy", REPO_ROOT / "destroy.py")
destroy = importlib.util.module_from_spec(_spec)
sys.modules["destroy"] = destroy
_spec.loader.exec_module(destroy)


class FakeCfn:
    """Minimal cloudformation client stub."""

    def __init__(self, outputs=None, resources=None):
        self._outputs = outputs or {}
        self._resources = resources or []

    def describe_stacks(self, StackName):
        return {
            "Stacks": [
                {
                    "StackName": StackName,
                    "Outputs": [
                        {"OutputKey": k, "OutputValue": v} for k, v in self._outputs.items()
                    ],
                }
            ]
        }

    def get_paginator(self, name):
        resources = self._resources

        class _Pager:
            def paginate(self, **kwargs):
                yield {"StackResourceSummaries": resources}

        return _Pager()


class FakeEvs:
    """EVS client stub that returns no environments (isolates the VPC logic)."""

    def get_paginator(self, name):
        class _Pager:
            def paginate(self, **kwargs):
                yield {"environmentSummaries": []}

        return _Pager()


class FakeSession:
    def __init__(self, cfn, evs):
        self._cfn = cfn
        self._evs = evs

    def client(self, service, **kwargs):
        return {"cloudformation": self._cfn, "evs": self._evs}[service]


def _discover(outputs, resources):
    session = FakeSession(FakeCfn(outputs, resources), FakeEvs())
    return destroy.discover_from_stack(session, "evs-ci-e2e-test")


VPC_RESOURCE = [{"LogicalResourceId": "Vpc", "ResourceType": "AWS::EC2::VPC",
                 "PhysicalResourceId": "vpc-stackcreated"}]


def test_vpcid_output_happy_path():
    """Normal case: VpcId output present, Vpc resource present -> deletable."""
    info = _discover({"VpcId": "vpc-abc"}, VPC_RESOURCE)
    assert info["vpc_id"] == "vpc-abc"
    assert info["stack_created_vpc"] is True


def test_early_failure_recovers_stack_created_vpc():
    """No VpcId output (early-failed) but a Vpc resource exists -> recover it and
    mark deletable, so the stack can be torn down instead of leaking."""
    info = _discover({}, VPC_RESOURCE)
    assert info["vpc_id"] == "vpc-stackcreated"
    assert info["stack_created_vpc"] is True


def test_byo_vpc_early_failure_never_deletes_customer_vpc():
    """SAFETY: BYO-VPC (no Vpc resource) that failed early has no VpcId output and
    no stack-created VPC. destroy.py must exit rather than proceed, and must NOT
    flag any VPC as stack-created (which would authorize deletion)."""
    with pytest.raises(SystemExit):
        _discover({}, [])  # no output, no Vpc resource -> nothing stack-owned to delete
