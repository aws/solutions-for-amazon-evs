"""Unit tests for ci/run_deployment.py role naming and boundary selection.

These encode the contract with the EvsCicdPipelineCDK runner IAM: role names
must carry the mode's disjoint prefix (evs-lz-pr-* / evs-lz-main-*) and each
mode's RunnerRole must attach that mode's permissions boundary. A drift here
AccessDenies the deploy, so the values are asserted explicitly.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "run_deployment", REPO_ROOT / "ci" / "run_deployment.py"
)
run_deployment = importlib.util.module_from_spec(_spec)
sys.modules["run_deployment"] = run_deployment
_spec.loader.exec_module(run_deployment)

role_prefix = run_deployment.role_prefix
boundary_arn = run_deployment.boundary_arn

ACCOUNT = "026249143097"


def _args(mode, boundary=""):
    return SimpleNamespace(mode=mode, account=ACCOUNT, permissions_boundary_arn=boundary)


class TestRolePrefix:
    def test_stack_test_uses_pr_prefix(self):
        assert role_prefix("stack-test") == "evs-lz-pr-"

    def test_full_uses_main_prefix(self):
        assert role_prefix("full") == "evs-lz-main-"

    def test_prefixes_are_disjoint(self):
        # Neither prefix is a prefix of the other, so a pre-merge role name can
        # never match the post-merge IAM pattern or vice versa.
        pr, main = role_prefix("stack-test"), role_prefix("full")
        assert not pr.startswith(main)
        assert not main.startswith(pr)


class TestBoundaryArn:
    def test_stack_test_defaults_to_pre_merge_boundary(self):
        assert boundary_arn(_args("stack-test")) == (
            f"arn:aws:iam::{ACCOUNT}:policy/evs-lz-boundary"
        )

    def test_full_defaults_to_deploy_boundary(self):
        # The post-merge run performs the real deployment, so it must NOT get an
        # empty boundary (the CDK's ForceBoundaryPropagation would deny
        # CreateRole); it gets the deploy boundary, which allows EVS creation.
        assert boundary_arn(_args("full")) == (
            f"arn:aws:iam::{ACCOUNT}:policy/evs-lz-deploy-boundary"
        )

    def test_full_is_never_empty(self):
        assert boundary_arn(_args("full")) != ""

    def test_explicit_override_wins(self):
        override = "arn:aws:iam::026249143097:policy/some-other-boundary"
        assert boundary_arn(_args("full", boundary=override)) == override
        assert boundary_arn(_args("stack-test", boundary=override)) == override
