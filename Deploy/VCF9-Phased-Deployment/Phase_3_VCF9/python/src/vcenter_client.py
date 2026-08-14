# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client for vCenter operations needed by the NSX-direct edge deployment.

Uses pyvmomi (vSphere SOAP API) because the specific operations we need —
creating distributed port groups and VM-VM anti-affinity DRS rules — have
long-standing, well-supported SOAP bindings. The vSphere REST API exists but
has inconsistent coverage for advanced DRS rule types.

Scope (kept minimal — we only do what's needed for edge deployment):
  - Connect / disconnect
  - Ping (connectivity + auth check)
  - Look up a managed object by name (datacenter, cluster, DVS, VM)
  - Create a distributed port group
  - Create a VM-VM anti-affinity DRS rule
"""

import atexit
import logging
import ssl
from typing import Any

from pyVim import connect
from pyVmomi import vim  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)

# The local VMFS we attempt to remove sits on a 256 GB EBS volume
# (Phase 2 default — see
# ``Phase_2_evs_env/python/src/ebs_volume_manager.py``
# ``_DEFAULT_VOLUME_SIZE_GB``).
# VMFS reports usable capacity slightly under the raw block-device
# size because of metadata overhead; a 256 GB EBS volume typically
# surfaces as ~255 GB. Match within a few-GB tolerance so the
# detection survives both that filesystem overhead and any small
# operator-side variation.
_INSTALLER_DATASTORE_CAPACITY_GB = 256
_INSTALLER_DATASTORE_CAPACITY_TOLERANCE_GB = 5


class VcenterClient:
    """Narrow pyvmomi wrapper for edge-cluster-deployment-specific ops.

    Args:
        host: vCenter FQDN or IP.
        username: vSphere SSO user (typically administrator@vsphere.local).
        password: vSphere SSO password.
        verify_tls: If False, disable TLS verification (default; vCenter
            ships with a self-signed cert unless separately configured).
        port: HTTPS port vCenter listens on (default 443).
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_tls: bool = False,
        port: int = 443,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._verify_tls = verify_tls
        self._port = port
        self._si: Any = None  # ServiceInstance, type depends on pyvmomi version

    # ---- lifecycle ----

    def connect(self) -> None:
        """Open a service instance to vCenter."""
        if self._si is not None:
            return

        if self._verify_tls:
            context = ssl.create_default_context()
        else:
            context = ssl._create_unverified_context()

        logger.info("Connecting to vCenter at %s", self._host)
        self._si = connect.SmartConnect(
            host=self._host,
            user=self._username,
            pwd=self._password,
            port=self._port,
            sslContext=context,
        )
        # Ensure cleanup on interpreter shutdown.
        atexit.register(self._safe_disconnect)

    def disconnect(self) -> None:
        """Close the service instance."""
        self._safe_disconnect()

    def _safe_disconnect(self) -> None:
        if self._si is not None:
            try:
                connect.Disconnect(self._si)
            except Exception as exc:
                logger.debug("Ignoring disconnect error: %s", exc)
            self._si = None

    def _require_connected(self) -> Any:
        if self._si is None:
            self.connect()
        return self._si

    # ---- convenience ----

    def ping(self) -> dict[str, str]:
        """Connectivity + auth check. Returns vCenter API version info."""
        si = self._require_connected()
        about = si.content.about
        return {
            "name": about.name,
            "fullName": about.fullName,
            "version": about.version,
            "build": about.build,
            "apiType": about.apiType,
        }

    # ---- managed-object lookups ----

    def _find_by_name(self, vim_type: Any, name: str) -> Any:
        """Walk the inventory and return the first managed object with the given name."""
        si = self._require_connected()
        content = si.content

        container_view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim_type], True
        )
        try:
            for obj in container_view.view:
                if obj.name == name:
                    return obj
            return None
        finally:
            container_view.Destroy()

    def find_datacenter(self, name: str) -> Any:
        obj = self._find_by_name(vim.Datacenter, name)
        if obj is None:
            raise RuntimeError(f"Datacenter '{name}' not found in vCenter")
        return obj

    def find_cluster(self, name: str) -> Any:
        obj = self._find_by_name(vim.ClusterComputeResource, name)
        if obj is None:
            raise RuntimeError(f"Cluster '{name}' not found in vCenter")
        return obj

    def find_dvs(self, name: str) -> Any:
        obj = self._find_by_name(vim.DistributedVirtualSwitch, name)
        if obj is None:
            raise RuntimeError(f"Distributed virtual switch '{name}' not found in vCenter")
        return obj

    def find_vm(self, name: str) -> Any:
        obj = self._find_by_name(vim.VirtualMachine, name)
        if obj is None:
            raise RuntimeError(f"VM '{name}' not found in vCenter")
        return obj

    def find_portgroup(self, dvs_name: str, portgroup_name: str) -> Any:
        """Look up a distributed port group on the given DVS. None if not present."""
        dvs = self.find_dvs(dvs_name)
        for pg in dvs.portgroup:
            if pg.name == portgroup_name:
                return pg
        return None

    def find_datastore_on_cluster(self, cluster_name: str, datastore_name: str) -> Any:
        """Look up a datastore attached to a specific cluster by name.

        Returns None if no matching datastore is found on any host in the cluster.
        """
        cluster = self.find_cluster(cluster_name)
        for host in cluster.host:
            for ds in host.datastore:
                if ds.name == datastore_name:
                    return ds
        return None

    def find_vsan_datastore_on_cluster(self, cluster_name: str) -> Any:
        """Return the vSAN datastore attached to a cluster.

        Edge VMs MUST be deployed on vSAN — the per-host local VMFS is
        cluster-local (won't survive vMotion or host failure) and exists
        only as a transient one-time landing spot for the VCF Installer
        OVA before bringup. Picking anything other than vSAN risks edges
        getting stranded when the EBS-backed datastore is cleaned up
        post-bringup.

        Iterates each host in the cluster, looking for a datastore whose
        ``summary.type == "vsan"``. The first one wins (a cluster only
        has one vSAN datastore in practice).

        Raises:
            RuntimeError: if no vSAN datastore is attached to any host
                in the cluster. Bringup should have created one — if it
                didn't, edges can't deploy.
        """
        cluster = self.find_cluster(cluster_name)
        for host in cluster.host:
            for ds in host.datastore:
                ds_type = getattr(ds.summary, "type", None)
                if ds_type and ds_type.lower() == "vsan":
                    return ds
        raise RuntimeError(
            f"No vSAN datastore found on any host in cluster '{cluster_name}'. "
            f"Edges must deploy onto vSAN; check that bringup completed and "
            f"vSAN is healthy before re-running."
        )

    def remove_local_installer_datastore(self, cluster_name: str) -> dict[str, Any]:
        """Evacuate and remove the local VMFS that hosted the VCF Installer.

        Post-bringup, the management cluster has two datastores: the
        cluster-wide vSAN datastore and a local VMFS sitting on a 256 GB
        EBS volume that one host has attached. The VMFS was a one-time
        landing spot for the VCF Installer OVA — once SDDC Manager /
        vCenter / NSX are running on vSAN, it's dead weight.

        We identify the target by capacity match (non-vSAN AND within
        ±5 GB of the configured ``_INSTALLER_DATASTORE_CAPACITY_GB``).
        VMFS reports usable capacity slightly under the raw EBS volume
        size due to filesystem overhead, so the tolerance window
        accounts for that. Name-based matching would be more brittle —
        the operator picks the datastore name when they create the
        VMFS in the ESXi UI during bringup pre-work.

        Sequence:
          1. Identify the target datastore.
          2. If any VMs are still registered on it, locate the cluster's
             vSAN datastore and storage-vMotion each VM there. Fails
             fast if no vSAN datastore exists or if any vMotion errors —
             we don't want to accidentally orphan data.
          3. Once the target is empty, call
             ``HostDatastoreSystem.RemoveDatastore`` on the owning host.
             Synchronous; returns when the datastore is unmounted and
             forgotten.

        Doesn't touch AWS: the EBS volume is still attached to the EC2
        instance after this call. Run Phase 3's ``destroy-ebs-volume``
        action (``ebs_volume_destroyer.py``) afterward to fully detach +
        delete the volume.

        Safety:
          - Refuses if zero or 2+ candidates match the capacity profile
            (unexpected topology — manual review required).
          - Refuses if the target datastore is mounted on more than one
            host (it should be local to the host with the EBS volume; if
            multiple hosts mount it, something else is going on).
          - Refuses if no vSAN datastore is reachable for vMotion when
            VMs are present.

        Returns:
            Dict with ``removed`` boolean and (when removed) the
            datastore name, capacity in GB, type, the host it lived on,
            and the list of VMs that were vMotion'd off it.
        """
        cluster = self.find_cluster(cluster_name)

        # 1. Collect every datastore visible on the cluster's hosts that
        #    matches the installer-VMFS profile: non-vSAN AND capacity
        #    within tolerance of the expected 256 GB EBS volume size.
        #    Dedupe by managed-object id since a shared datastore would
        #    appear on multiple hosts (we'll refuse those below anyway).
        target_lo = (
            _INSTALLER_DATASTORE_CAPACITY_GB
            - _INSTALLER_DATASTORE_CAPACITY_TOLERANCE_GB
        )
        target_hi = (
            _INSTALLER_DATASTORE_CAPACITY_GB
            + _INSTALLER_DATASTORE_CAPACITY_TOLERANCE_GB
        )
        candidates: dict[str, Any] = {}
        for host in cluster.host:
            for ds in host.datastore:
                ds_type = (getattr(ds.summary, "type", "") or "").lower()
                if ds_type == "vsan":
                    continue
                capacity_bytes = getattr(ds.summary, "capacity", 0) or 0
                capacity_gb = capacity_bytes / (1024 ** 3)
                if not (target_lo <= capacity_gb <= target_hi):
                    logger.debug(
                        "Skipping datastore '%s' on host '%s' "
                        "(capacity %.1f GB outside %d-%d GB window)",
                        ds.name, host.name, capacity_gb, target_lo, target_hi,
                    )
                    continue
                candidates[ds._moId] = ds  # noqa: SLF001

        if not candidates:
            logger.info(
                "No %d ± %d GB non-vSAN datastore found on cluster '%s'; "
                "nothing to do.",
                _INSTALLER_DATASTORE_CAPACITY_GB,
                _INSTALLER_DATASTORE_CAPACITY_TOLERANCE_GB,
                cluster_name,
            )
            return {"removed": False, "reason": "no matching datastore found"}

        if len(candidates) > 1:
            names = sorted(
                f"{ds.name} ({(ds.summary.capacity or 0) / (1024 ** 3):.1f} GB)"
                for ds in candidates.values()
            )
            raise RuntimeError(
                f"Found {len(candidates)} datastores on cluster "
                f"'{cluster_name}' matching the installer-VMFS profile "
                f"(non-vSAN, {target_lo}-{target_hi} GB): {names}. "
                f"Expected exactly one. Refusing to act without manual "
                f"review — unmount the right one via the vSphere UI."
            )

        target = next(iter(candidates.values()))
        target_name = target.name

        # 2. Identify the owning host before doing anything else. Local
        #    VMFS should be mounted on exactly one host — anything else
        #    is unexpected and we bail before touching VMs.
        owning_hosts = [m.key for m in (target.host or [])]
        if not owning_hosts:
            raise RuntimeError(
                f"Datastore '{target_name}' isn't mounted on any host — "
                f"already in a weird state. Clean up manually via the "
                f"vSphere UI."
            )
        if len(owning_hosts) > 1:
            host_names = sorted(h.name for h in owning_hosts)
            raise RuntimeError(
                f"Datastore '{target_name}' is mounted on multiple hosts: "
                f"{host_names}. This isn't a local-only datastore; refusing "
                f"to auto-remove."
            )

        owning_host = owning_hosts[0]
        capacity_bytes = getattr(target.summary, "capacity", 0) or 0
        capacity_gb = capacity_bytes / (1024 ** 3)
        ds_type = getattr(target.summary, "type", "?")

        # 3. Storage-vMotion any VMs off the target onto vSAN. Resolve
        #    the vSAN datastore once up-front so we fail fast if it
        #    isn't there. ``find_vsan_datastore_on_cluster`` raises if
        #    no vSAN datastore exists.
        vms = list(getattr(target, "vm", None) or [])
        evacuated: list[str] = []
        if vms:
            vm_names = sorted(vm.name for vm in vms)
            logger.info(
                "Datastore '%s' has %d VM(s) registered: %s. "
                "Storage-vMotioning each to vSAN before removal.",
                target_name, len(vms), vm_names,
            )
            try:
                vsan_ds = self.find_vsan_datastore_on_cluster(cluster_name)
            except Exception as e:
                raise RuntimeError(
                    f"Datastore '{target_name}' has VMs to evacuate but no "
                    f"vSAN datastore could be located on cluster "
                    f"'{cluster_name}': {e}. Move the VMs manually via the "
                    f"vSphere UI, then re-run."
                ) from e

            for vm in vms:
                self._storage_vmotion(vm, vsan_ds)
                evacuated.append(vm.name)

            # Refresh the target's VM list — pyvmomi's view of
            # ``ds.vm`` is a managed-object reference list and reflects
            # the post-relocate state once the tasks complete.
            still_present = list(getattr(target, "vm", None) or [])
            if still_present:
                still_names = sorted(v.name for v in still_present)
                raise RuntimeError(
                    f"Storage-vMotion completed but '{target_name}' still "
                    f"shows {len(still_present)} VM(s) registered "
                    f"({still_names}). Refusing to remove the datastore."
                )

        logger.info(
            "Removing non-vSAN datastore '%s' (%.1f GB, type=%s) from host '%s'",
            target_name, capacity_gb, ds_type, owning_host.name,
        )

        # 4. Remove via the host's datastore system. Synchronous —
        #    returns when the datastore is unmounted and forgotten.
        ds_system = owning_host.configManager.datastoreSystem
        ds_system.RemoveDatastore(target)

        logger.info(
            "Datastore '%s' removed. The underlying EBS volume on host '%s' "
            "is still attached to the EC2 instance — run the Phase 2 EBS "
            "cleanup (terraform destroy or boto3 detach+delete) to release it.",
            target_name, owning_host.name,
        )

        return {
            "removed": True,
            "name": target_name,
            "host": owning_host.name,
            "capacityGB": round(capacity_gb, 1),
            "type": ds_type,
            "evacuatedVms": evacuated,
        }

    def _storage_vmotion(self, vm: Any, target_datastore: Any) -> None:
        """Storage-vMotion a single VM onto ``target_datastore``.

        Synchronous — blocks until the underlying RelocateVM_Task
        terminates. Raises ``RuntimeError`` on task failure (which
        bubbles all the way to the caller so partial evacuations fail
        cleanly).
        """
        spec = vim.vm.RelocateSpec(datastore=target_datastore)
        logger.info(
            "Storage-vMotion: '%s' -> datastore '%s'",
            vm.name, target_datastore.name,
        )
        task = vm.RelocateVM_Task(spec=spec)
        self._wait_for_task(task, f"storage-vMotion {vm.name}")

    @staticmethod
    def moid(obj: Any) -> str:
        """Return the managed object ID of a pyvmomi MO (e.g. 'domain-c123')."""
        return obj._moId  # noqa: SLF001 (pyvmomi does not expose a public accessor)

    # ---- operations ----

    def create_trunk_portgroup(
        self,
        dvs_name: str,
        portgroup_name: str,
        vlan_range_start: int = 0,
        vlan_range_end: int = 4094,
    ) -> Any:
        """Create an ephemeral distributed port group in TRUNK VLAN mode.

        Returns the existing port group if one with the same name already
        exists (idempotent).
        """
        existing = self.find_portgroup(dvs_name, portgroup_name)
        if existing is not None:
            logger.info(
                "Port group '%s' on DVS '%s' already exists; skipping create",
                portgroup_name,
                dvs_name,
            )
            return existing

        dvs = self.find_dvs(dvs_name)

        vlan_spec = vim.dvs.VmwareDistributedVirtualSwitch.TrunkVlanSpec()
        vlan_spec.vlanId = [vim.NumericRange(start=vlan_range_start, end=vlan_range_end)]

        port_config = vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy()
        port_config.vlan = vlan_spec

        pg_spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec()
        pg_spec.name = portgroup_name
        pg_spec.type = vim.dvs.DistributedVirtualPortgroup.PortgroupType.ephemeral
        pg_spec.defaultPortConfig = port_config

        logger.info(
            "Creating TRUNK port group '%s' on DVS '%s' (VLANs %d-%d)",
            portgroup_name,
            dvs_name,
            vlan_range_start,
            vlan_range_end,
        )
        task = dvs.AddDVPortgroup_Task([pg_spec])
        self._wait_for_task(task, f"create port group {portgroup_name}")

        # Reload and return the realized port group.
        return self.find_portgroup(dvs_name, portgroup_name)

    def create_vm_anti_affinity_rule(
        self,
        cluster_name: str,
        rule_name: str,
        vm_names: list[str],
    ) -> None:
        """Create a VM-VM anti-affinity DRS rule separating the given VMs.

        Idempotent — returns quietly if a rule with the same name already
        exists on the cluster. Does not attempt to reconcile VMs in that case.
        """
        cluster = self.find_cluster(cluster_name)

        for rule in cluster.configurationEx.rule or []:
            if rule.name == rule_name:
                logger.info(
                    "Anti-affinity rule '%s' already exists on cluster '%s'; skipping create",
                    rule_name,
                    cluster_name,
                )
                return

        vms = [self.find_vm(name) for name in vm_names]

        rule = vim.cluster.AntiAffinityRuleSpec()
        rule.name = rule_name
        rule.enabled = True
        rule.mandatory = False
        rule.vm = vms

        rule_spec = vim.cluster.RuleSpec()
        rule_spec.operation = vim.option.ArrayUpdateSpec.Operation.add
        rule_spec.info = rule

        cluster_config_spec = vim.cluster.ConfigSpecEx()
        cluster_config_spec.rulesSpec = [rule_spec]

        logger.info(
            "Creating VM-VM anti-affinity rule '%s' for VMs %s on cluster '%s'",
            rule_name,
            vm_names,
            cluster_name,
        )
        task = cluster.ReconfigureComputeResource_Task(cluster_config_spec, modify=True)
        self._wait_for_task(task, f"create anti-affinity rule {rule_name}")

    # ---- task polling ----

    def _wait_for_task(self, task: Any, description: str) -> Any:
        """Block until a vSphere task reaches success or error."""
        import time

        while task.info.state not in (
            vim.TaskInfo.State.success,
            vim.TaskInfo.State.error,
        ):
            time.sleep(2)

        if task.info.state == vim.TaskInfo.State.error:
            error = task.info.error
            msg = getattr(error, "msg", str(error))
            raise RuntimeError(f"vCenter task failed ({description}): {msg}")

        logger.info("vCenter task completed: %s", description)
        return task.info.result
