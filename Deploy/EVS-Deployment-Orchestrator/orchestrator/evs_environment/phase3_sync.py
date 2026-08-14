# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build the VCF bringup JSON spec for the Phase 3 installer POST.

Thin I/O shell around the typed ``SddcSpecBuilder``. The spec is built as a
real ``vmware.vcf_installer.model_client.SddcSpec`` and serialized by the
SDK's own converter, so the JSON on disk is exactly what Phase 3 POSTs to
``/v1/sddcs``. Field names/structure match the VCF Installer SddcSpec schema:
https://developer.broadcom.com/xapis/vcf-installer-api/latest/data-structures/SddcSpec/

The VCF target version (``9.0`` or ``9.1``) is derived from
``vcfInstallerProductVersion`` and controls which version-specific extras get
attached (see ``SddcSpecBuilder._version_extras``).

Two-pass build: ``pre-evs-sync-config`` writes every field known up front
(hostnames, network pools, DNS, NTP, passwords); ``post-evs-sync-config``
rewrites with the env-id-derived cluster/datacenter/DVS names. The second
pass is authoritative — rebuild the whole spec and overwrite.
"""

import logging
from pathlib import Path
from typing import Any

from sddc_spec_builder import SddcSpecBuilder
from sdk_serde import typed_to_json

logger = logging.getLogger(__name__)


class Phase3Sync:
    """Builds and writes the Phase 3 VCF bringup JSON spec.

    Args:
        output_path: Path to write the bringup spec JSON to.
        config: Phase 2 config dict.
        environment_id: Optional EVS environment ID. When None, env-derived
            fields (cluster/dc/dvs names) are stubbed with a placeholder.
    """

    def __init__(
        self,
        output_path: str | Path,
        config: dict[str, Any],
        environment_id: str | None = None,
    ) -> None:
        self._output_path = Path(output_path)
        self._builder = SddcSpecBuilder(config, environment_id=environment_id)

    def sync(self, dry_run: bool = False) -> str:
        """Build and write the bringup spec.

        Args:
            dry_run: If True, print the result instead of writing.

        Returns:
            The pretty-printed bringup spec JSON string.
        """
        spec = self._builder.build()
        json_text = typed_to_json(spec, indent=2)

        if dry_run:
            logger.info("DRY RUN — would write bringup spec to %s", self._output_path)
            print(json_text)
            return json_text

        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._output_path, "w") as f:
            f.write(json_text)
            f.write("\n")
        logger.info("Wrote bringup spec to: %s", self._output_path)
        return json_text
