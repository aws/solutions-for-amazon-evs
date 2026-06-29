# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build the VCF bringup JSON spec for the Phase 3 installer POST.

This is the thin I/O shell around the typed ``SddcSpecBuilder``. The spec
itself is built as a real ``vmware.vcf_installer.model_client.SddcSpec``
object and serialized to the wire JSON by the SDK's own converter, which
means the JSON on disk is exactly what Phase 3 will ultimately POST to
``/v1/sddcs``.

Field names and structure match the VCF Installer API SddcSpec schema:
https://developer.broadcom.com/xapis/vcf-installer-api/latest/data-structures/SddcSpec/

Multi-version support
---------------------

The VCF target version (``9.0`` or ``9.1``) is derived from
``vcfInstallerProductVersion`` and controls which set of version-specific
extras gets attached to the spec. See ``SddcSpecBuilder._version_extras``
for the version branch.

Two-pass build
--------------

  1. ``pre-evs-sync-config`` (no environment ID yet) writes every field we
     know up-front: hostnames, network pools, DNS, NTP, passwords.
  2. ``post-evs-sync-config`` (environment ID now known) rewrites the file
     with the env-id-derived cluster / datacenter / DVS names.

The second pass is authoritative — we rebuild the whole spec and overwrite.
"""

import logging
from pathlib import Path
from typing import Any

from src.sddc_spec_builder import SddcSpecBuilder
from src.sdk_serde import typed_to_json

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
