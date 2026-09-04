# EVS Deployment Orchestrator — developer & CI checks.
# `make test` runs the same set the Stage 1 build check runs, so a
# change that passes locally passes the PR check.

PYTHON ?= python3

.PHONY: install test unit lint cfn blueprints scan

install:
	$(PYTHON) -m pip install --quiet \
	  -r requirements-dev.txt

test: unit lint cfn blueprints

unit:
	# --cov-fail-under starts at 20 (baseline measured 24% — the
	# interactive CLI modules are untested); ratchet up as tests land.
	$(PYTHON) -m pytest tests/ --cov=spec-generator --cov-report=term-missing --cov-fail-under=20

lint:
	$(PYTHON) -m pyflakes spec-generator/*.py tools/*.py tests/*.py

cfn:
	# Warnings print but don't block (W1030 fires on optional BYO-VPC
	# params with guarded empty defaults); errors fail the gate.
	cfn-lint --non-zero-exit-code error evs-deployment-orchestrator.yaml

blueprints:
	$(PYTHON) tools/validate_blueprints.py blueprints/*.yaml ci/blueprints/*.yaml

# Security scan (ASH). Not part of `test` yet: ASH is a separate
# install and the scanner choice is pending the AppSec review
# (ASH vs an internal scanner). Run explicitly with `make scan`.
scan:
	ash --source-dir . --output-dir .ash
