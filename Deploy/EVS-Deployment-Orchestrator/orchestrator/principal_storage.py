# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Principal storage for the management cluster: vSAN, or FSx for NetApp ONTAP.

The blueprint chooses with ``storage.principal.type``. ``storage`` is optional,
and leaving it out is the same as ``type: vsan``:

    storage:
      principal:
        type: "vsan"               # vSAN ESA on the hosts' local NVMe (the default)

    storage:
      principal:                   # FSx for NetApp ONTAP NFS datastore
        type: "fsxn"
        name: "nfs-datastore"
        fsxn:                      # the settings block named after the type
          file_system_name: ...    # create mode: every CREATE_FIELDS key is required
          volume_id: "fsvol-..."   # or BYO-FSxN: the volume to mount (needs BYO-VPC)

With ``volume_id`` set the orchestrator uses that volume and ignores any
creation fields, with a warning. Without it, the orchestrator creates a
Single-AZ file system in the service access subnet, and only in a VPC it
creates. A later storage type adds its own settings block next to ``fsxn``.

Hosts with no local instance storage cannot build vSAN, so they need FSxN;
hosts with local storage use vSAN. Whether a type has local storage is read
from ec2:DescribeInstanceTypes at launch (``instance_has_local_storage``), and
whether EVS offers the type from evs:GetVersions, so no instance type is
hard-coded here.

Standard library only. Every AWS call takes a boto3 client from the caller, so
destroy.py can reuse the teardown without the orchestrator's dependencies.
"""

import ipaddress
import json
import re
import time

VSAN = "vsan"
FSXN = "fsxn"
TYPES = (VSAN, FSXN)
# Settings blocks of storage types a later release may add. Named here so a
# blueprint written for that release fails with a clear message.
FUTURE_TYPES = ("fsxz",)

# FSx for NetApp ONTAP, as offered by the FSx console's Quick create. Always
# Single-AZ: EVS hosts run in one Availability Zone, and Multi-AZ needs a
# standby subnet and route tables the orchestrator does not create.
DEPLOYMENT_TYPE = "SINGLE_AZ_2"
THROUGHPUT_MBPS = (384, 768, 1536, 3072, 6144)  # SINGLE_AZ_2, 1 HA pair
RECOMMENDED = "recommended"
RECOMMENDED_THROUGHPUT_MBPS = 384
MIN_CAPACITY_GB = 1024
# The file system's one volume is a FlexVol, sized to the whole file system, and
# a FlexVol holds at most 300 TiB. A larger file system would fail at create.
MAX_CAPACITY_GB = 307200

# Create mode: the console's Quick create fields, without network settings.
# All required, no defaults, so the customer chooses capacity and throughput.
CREATE_FIELDS = ("file_system_name", "storage_capacity_gb", "throughput_capacity_mbps",
                 "storage_efficiency", "s3_access")
VOLUME_ID_PATTERN = r"^fsvol-[0-9a-f]{17}$"
# Fixed in create mode. NFSv3 because the installer's nfsDatastoreSpec has no
# NFS version field; /vol1 matches the console's Quick create.
JUNCTION_PATH = "/vol1"
NFS_VERSION = "NFS3"
# rpcbind, mountd, nfsd, NLM, NSM: every port an NFSv3 client of ONTAP uses.
NFS_PORTS = ((111, 111), (635, 635), (2049, 2049), (4045, 4046))
HEALTHY_VOLUME_STATES = {"CREATED", "AVAILABLE"}
STACK_SUFFIX = "-evs-storage"

_DATASTORE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,42}$")
_FILE_SYSTEM_NAME = re.compile(r"^[\w\s+=.:/-]{1,256}$", re.UNICODE)


# ---------------------------------------------------------------- blueprint

def principal_entry(config: dict) -> dict | None:
    """The ``storage.principal`` mapping when it is FSxN, or None for vSAN."""
    storage = config.get("storage")
    entry = storage.get("principal") if isinstance(storage, dict) else None
    return entry if isinstance(entry, dict) and entry.get("type") == FSXN else None


def settings(entry: dict | None) -> dict:
    """The ``fsxn`` settings block of an FSxN principal entry ({} when absent)."""
    block = (entry or {}).get(FSXN)
    return block if isinstance(block, dict) else {}


def is_existing(entry: dict | None) -> bool:
    """BYO-FSxN: the settings block names a volume, so nothing is created."""
    return settings(entry).get("volume_id") is not None


def ignored_create_fields(entry: dict | None) -> list[str]:
    """Creation fields set next to ``volume_id``, which BYO-FSxN ignores."""
    block = settings(entry)
    return sorted(f for f in CREATE_FIELDS if is_existing(entry) and f in block)


def source_cidrs(config: dict) -> list[tuple[str, str]]:
    """CIDRs that must reach the file system: the host management VLAN subnet,
    which holds vmk0 (the mount path) and the VCF NFS network."""
    prefix = config["vpc"]["cidr_prefix"]
    return [(f"{prefix}10.0/24", "ESXi host management VLAN")]


def create_settings(entry: dict) -> dict:
    """The creation fields, with ``recommended`` throughput resolved."""
    block = settings(entry)
    values = {f: block.get(f) for f in CREATE_FIELDS}
    if values["throughput_capacity_mbps"] == RECOMMENDED:
        values["throughput_capacity_mbps"] = RECOMMENDED_THROUGHPUT_MBPS
    return values


def validate(config: dict, errors: list, warnings: list | None = None) -> None:
    """Static ``storage`` checks; makes no AWS calls.

    Appends blocking problems to ``errors`` and, when given, notes that do not
    block the run to ``warnings``.
    """
    storage = config.get("storage")
    if storage is None:
        principal = None
    elif not isinstance(storage, dict):
        errors.append(f"storage must be a mapping, got: {storage!r}")
        return
    else:
        unknown = sorted(set(storage) - {"principal"})
        if unknown:
            errors.append(f"storage has unknown field(s): {unknown}. Supported fields: ['principal'].")
        principal = storage.get("principal")

    where = "storage.principal"
    if principal is not None and not isinstance(principal, dict):
        errors.append(f"{where} must be a mapping with type: vsan or type: fsxn, got: {principal!r}")
        return
    kind = VSAN if principal is None else principal.get("type")
    if kind not in TYPES:
        errors.append(f"{where}.type must be one of {list(TYPES)}, got: {kind!r}")
        return
    other_blocks = sorted(k for k in (principal or {}) if k in (*TYPES, *FUTURE_TYPES) and k != kind)
    if other_blocks:
        errors.append(f"{where} has settings block(s) {other_blocks} for another storage type, but "
                      f"type is {kind}. Only the block named after the type is allowed"
                      + (f" ({kind})." if kind == FSXN else "; type: vsan takes no block."))
    if kind == VSAN:
        unknown = sorted(set(principal or {}) - {"type", *other_blocks})
        if unknown:
            errors.append(f"{where} with type: vsan takes no other fields, got: {unknown}")
        return

    unknown = sorted(set(principal) - {"type", "name", FSXN, *other_blocks})
    if unknown:
        errors.append(f"{where} has unknown field(s): {unknown}. Supported fields: "
                      f"['fsxn', 'name', 'type']. The FSx settings go in the {FSXN} block.")
    name = principal.get("name")
    if not isinstance(name, str) or not _DATASTORE_NAME.match(name):
        errors.append(f"{where}.name is required: the vSphere datastore name, 1-42 letters, "
                      f"digits, dots, underscores or hyphens. Got: {name!r}")
    block = principal.get(FSXN)
    if not isinstance(block, dict):
        errors.append(f"{where}.{FSXN} is required with type: fsxn: a mapping with volume_id "
                      f"(BYO-FSxN) or every creation field {list(CREATE_FIELDS)}, got: {block!r}")
        return
    unknown = sorted(set(block) - {"volume_id", *CREATE_FIELDS})
    if unknown:
        errors.append(f"{where}.{FSXN} has unknown field(s): {unknown}. Supported fields: "
                      f"{sorted({'volume_id', *CREATE_FIELDS})}.")

    # Who made the VPC is known only under the bootstrap stack, which always
    # passes --vpc-create. A manual run (no flag) uses a landing zone that already
    # exists, so both modes are allowed; verify() still checks a BYO volume's VPC
    # and AZ before any bare metal.
    vpc_create = (config.get("vpc") or {}).get("create") if config.get("_bootstrap_mode") else None
    if is_existing(principal):
        _validate_volume_id(block["volume_id"], vpc_create, errors)
        ignored = ignored_create_fields(principal)
        if ignored and warnings is not None:
            warnings.append(
                f"{where}.{FSXN}.volume_id is set, so the orchestrator uses that volume "
                f"(BYO-FSxN) and ignores the creation field(s) {ignored}.")
        return
    if vpc_create is False:
        errors.append(
            f"{where}: with BYO-VPC (the ExistingVpcId stack parameter), set {FSXN}.volume_id "
            f"to bring your own FSx for NetApp ONTAP volume. The orchestrator creates a file "
            f"system only in a VPC it creates.")
    _validate_create(block, errors)


def _validate_create(block: dict, errors: list) -> None:
    where = f"storage.principal.{FSXN}"
    missing = [f for f in CREATE_FIELDS if block.get(f) is None]
    if missing:
        errors.append(f"{where} without volume_id creates a file system, and every creation "
                      f"field is required. Missing: {missing}.")
    fs_name = block.get("file_system_name")
    if fs_name is not None and (not isinstance(fs_name, str) or not _FILE_SYSTEM_NAME.match(fs_name)):
        errors.append(f"{where}.file_system_name must be 1-256 letters, digits, whitespace or "
                      f"+ - = . _ : /, got: {fs_name!r}")
    capacity = block.get("storage_capacity_gb")
    if capacity is not None and (isinstance(capacity, bool) or not isinstance(capacity, int)
                                 or not MIN_CAPACITY_GB <= capacity <= MAX_CAPACITY_GB):
        errors.append(f"{where}.storage_capacity_gb must be a whole number from "
                      f"{MIN_CAPACITY_GB} to {MAX_CAPACITY_GB} (300 TiB, the largest FlexVol "
                      f"volume), got: {capacity!r}")
    throughput = block.get("throughput_capacity_mbps")
    if throughput not in (None, RECOMMENDED) and (isinstance(throughput, bool)
                                                  or throughput not in THROUGHPUT_MBPS):
        errors.append(f"{where}.throughput_capacity_mbps must be \"recommended\" or one of "
                      f"{list(THROUGHPUT_MBPS)}, got: {throughput!r}")
    for flag in ("storage_efficiency", "s3_access"):
        if block.get(flag) is not None and not isinstance(block[flag], bool):
            errors.append(f"{where}.{flag} must be true or false (unquoted), got: {block[flag]!r}")
    if block.get("s3_access") is True:
        errors.append(f"{where}.s3_access: true is not supported yet. An S3 access point would "
                      f"expose the management datastore over S3. Set it to false.")


def _validate_volume_id(volume_id, vpc_create, errors: list) -> None:
    where = f"storage.principal.{FSXN}.volume_id"
    if not isinstance(volume_id, str) or not re.match(VOLUME_ID_PATTERN, volume_id):
        errors.append(f"{where} is not a valid FSx volume ID ({VOLUME_ID_PATTERN}), "
                      f"got: {volume_id!r}")
    if vpc_create:
        errors.append(
            f"{where} needs BYO-VPC (the ExistingVpcId stack parameter). A VPC the stack "
            f"creates does not exist before the run, and an FSx file system cannot move to "
            f"another VPC. Deploy into the VPC that holds the file system, or leave out "
            f"volume_id to create one.")

def byo_ids(fsx, volume_id: str) -> dict:
    """File system, SVM and volume IDs for a BYO-FSxN volume, from DescribeVolumes.

    Raises:
        RuntimeError: when the volume is not an FSx for NetApp ONTAP volume in
            the client's Region.
    """
    try:
        vol = _first(fsx.describe_volumes(VolumeIds=[volume_id]), "Volumes")
    except Exception as e:  # noqa: BLE001
        if "VolumeNotFound" not in str(e) and "not found" not in str(e).lower():
            raise
        vol = {}
    svm_id = (vol.get("OntapConfiguration") or {}).get("StorageVirtualMachineId")
    if vol.get("VolumeType", "ONTAP") != "ONTAP" or not svm_id or not vol.get("FileSystemId"):
        raise RuntimeError(
            f"storage.principal.fsxn.volume_id {volume_id} is not an FSx for NetApp ONTAP volume in "
            f"this Region. No EVS environment or host has been created. Fix volume_id and "
            f"re-run with --resume.")
    return {"file_system_id": vol["FileSystemId"], "svm_id": svm_id, "volume_id": volume_id}


# ------------------------------------------------------- instance eligibility

def instance_has_local_storage(ec2, instance_type: str) -> bool | None:
    """Whether ``instance_type`` has local instance storage, per DescribeInstanceTypes.

    None when EC2 does not know the type in this Region.
    """
    types = ec2.describe_instance_types(InstanceTypes=[instance_type]).get("InstanceTypes") or []
    if not types:
        return None
    return bool(types[0].get("InstanceStorageSupported"))


def eligibility_errors(config: dict, has_local_storage: bool) -> list[str]:
    """Check the principal storage choice against the type's real disks.

    vSAN needs local instance storage; FSx for NetApp ONTAP is for types without
    it. Whether EVS offers the type at all is checked separately, against
    evs:GetVersions.
    """
    instance_type = config["evs"]["instance_type"]
    if principal_entry(config) is None and not has_local_storage:
        return [f"evs.instance_type '{instance_type}' has no local instance storage "
                f"(ec2:DescribeInstanceTypes), so vSAN cannot be built on it. Set "
                f"storage.principal.type to fsxn (FSx for NetApp ONTAP)."]
    if principal_entry(config) is not None and has_local_storage:
        return [f"evs.instance_type '{instance_type}' has local instance storage "
                f"(ec2:DescribeInstanceTypes), so it uses vSAN. FSx for NetApp ONTAP "
                f"principal storage is only for instance types without local storage."]
    return []


# ------------------------------------------------------------ create stack

def stack_name(config: dict) -> str:
    """<bootstrap-stack-name>-evs-storage (falls back to the environment name)."""
    base = config.get("aws", {}).get("bootstrap_stack_name") or config["evs"]["environment_name"]
    return stack_name_for(base)


def stack_name_for(base: str) -> str:
    """The storage stack name for a bootstrap stack name. destroy.py uses this too,
    so both compute the same name for long or unusual bootstrap names."""
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in base).strip("-")
    return f"{safe[:128 - len(STACK_SUFFIX)]}{STACK_SUFFIX}"


def build_template(config: dict, entry: dict, tags: list) -> dict:
    """CloudFormation template for create mode: SG, file system, SVM, volume."""
    vpc = config["vpc"]
    values = create_settings(entry)
    fs_name = values["file_system_name"]

    def named(name):
        return tags + [{"Key": "Name", "Value": name}]

    ingress = [
        {"IpProtocol": proto, "FromPort": low, "ToPort": high, "CidrIp": cidr,
         "Description": f"NFS {low}-{high}/{proto} from {label}"}
        for cidr, label in source_cidrs(config)
        for low, high in NFS_PORTS
        for proto in ("tcp", "udp")
    ]
    capacity = values["storage_capacity_gb"]
    resources = {
        "FsxnSecurityGroup": {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupDescription": "FSx for NetApp ONTAP principal datastore - NFS from the EVS ESXi VLANs",
                "VpcId": vpc["id"],
                "SecurityGroupIngress": ingress,
                "Tags": named(f"{fs_name}-nfs"),
            },
        },
        "FsxnFileSystem": {
            "Type": "AWS::FSx::FileSystem",
            "Properties": {
                "FileSystemType": "ONTAP",
                "StorageCapacity": capacity,
                "StorageType": "SSD",
                "SubnetIds": [vpc["service_access_subnet_id"]],
                "SecurityGroupIds": [{"Ref": "FsxnSecurityGroup"}],
                "OntapConfiguration": {
                    "DeploymentType": DEPLOYMENT_TYPE,
                    "PreferredSubnetId": vpc["service_access_subnet_id"],
                    "ThroughputCapacity": values["throughput_capacity_mbps"],
                    "HAPairs": 1,
                    # Off: backups outlive teardown and keep billing.
                    "AutomaticBackupRetentionDays": 0,
                },
                "Tags": named(fs_name),
            },
        },
        "FsxnSvm": {
            "Type": "AWS::FSx::StorageVirtualMachine",
            "Properties": {
                "FileSystemId": {"Ref": "FsxnFileSystem"},
                "Name": "fsx",
                "RootVolumeSecurityStyle": "UNIX",
                "Tags": named(f"{fs_name}-svm"),
            },
        },
        "FsxnVolume": {
            "Type": "AWS::FSx::Volume",
            "Properties": {
                "Name": "vol1",
                "VolumeType": "ONTAP",
                "OntapConfiguration": {
                    "StorageVirtualMachineId": {"Ref": "FsxnSvm"},
                    "JunctionPath": JUNCTION_PATH,
                    "SizeInBytes": str(capacity * 1024 ** 3),
                    "SecurityStyle": "UNIX",
                    "StorageEfficiencyEnabled": bool(values["storage_efficiency"]),
                    # All management appliance data stays on SSD; no snapshots
                    # eat datastore space (the orchestrator has no restore path).
                    "TieringPolicy": {"Name": "NONE"},
                    "SnapshotPolicy": "none",
                    "OntapVolumeType": "RW",
                    "CopyTagsToBackups": True,
                },
                "Tags": named(f"{fs_name}-vol1"),
            },
        },
    }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "EVS principal storage (FSx for NetApp ONTAP). Created by the EVS "
                       "Deployment Orchestrator; tear it down with destroy.py, not directly.",
        "Resources": resources,
        "Outputs": {
            "FileSystemId": {"Value": {"Ref": "FsxnFileSystem"}},
            "StorageVirtualMachineId": {"Value": {"Ref": "FsxnSvm"}},
            "VolumeId": {"Value": {"Ref": "FsxnVolume"}},
        },
    }


# Properties FSx changes on a live resource without replacing it. Anything else
# would make CloudFormation replace the file system and delete the datastore.
_IN_PLACE = {
    "AWS::FSx::FileSystem": {"Tags", "StorageCapacity", "OntapConfiguration.ThroughputCapacity",
                             "OntapConfiguration.AutomaticBackupRetentionDays"},
    "AWS::FSx::Volume": {"Tags", "OntapConfiguration.SizeInBytes",
                         "OntapConfiguration.StorageEfficiencyEnabled"},
    "AWS::FSx::StorageVirtualMachine": {"Tags"},
    "AWS::EC2::SecurityGroup": {"Tags", "SecurityGroupIngress"},
}
_FIELD_FOR = {
    "OntapConfiguration.DeploymentType": "the deployment type",
    "SubnetIds": "the service access subnet",
}


def replacement_errors(deployed: dict, desired: dict) -> list[str]:
    """Edits to a deployed storage stack that would replace a resource."""
    def flat(props):
        out = {}
        for key, value in (props or {}).items():
            if key == "OntapConfiguration" and isinstance(value, dict):
                out.update({f"{key}.{k}": v for k, v in value.items()})
            else:
                out[key] = value
        return out

    errors = []
    desired_res = desired.get("Resources") or {}
    for logical_id, res in sorted((deployed.get("Resources") or {}).items()):
        allowed = _IN_PLACE.get(res.get("Type"), {"Tags"})
        was = flat(res.get("Properties"))
        now = flat((desired_res.get(logical_id) or {}).get("Properties"))
        for path in sorted(set(was) | set(now)):
            if path in allowed or was.get(path) == now.get(path):
                continue
            errors.append(f"{_FIELD_FOR.get(path, path)} cannot change after the file system "
                          f"exists ({logical_id} has {json.dumps(was.get(path))}, the blueprint "
                          f"now gives {json.dumps(now.get(path))})")
        if res.get("Type") == "AWS::FSx::FileSystem":
            old_cap = was.get("StorageCapacity") or 0
            new_cap = now.get("StorageCapacity") or 0
            if new_cap and new_cap < old_cap:
                errors.append(f"storage_capacity_gb can only grow ({old_cap} -> {new_cap})")
    return errors


def deploy_stack(cfn, config: dict, entry: dict, tags: list, logger, fsx=None) -> dict:
    """Create or update the create-mode storage stack; return its outputs.

    Safe on --resume: an unchanged template is a no-op, and an edit that would
    replace the file system is refused before CloudFormation sees it. A stack
    left in ROLLBACK_FAILED is deleted first; with ``fsx``, any volume it still
    holds is deleted without a final backup, as in teardown.
    """
    name = stack_name(config)
    template = build_template(config, entry, tags)
    body = json.dumps(template, separators=(",", ":"))
    # The file system alone takes 20-30 minutes, then the SVM and the volume.
    waiter = {"Delay": 30, "MaxAttempts": 160}
    status = None
    try:
        status = cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
    except cfn.exceptions.ClientError as e:
        if "does not exist" not in str(e):
            raise  # throttle / AccessDenied must not look like "no stack"

    if status in ("CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS",
                  "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS", "ROLLBACK_IN_PROGRESS",
                  "UPDATE_ROLLBACK_IN_PROGRESS", "DELETE_IN_PROGRESS"):
        raise RuntimeError(
            f"Storage stack '{name}' is {status} from an earlier run. Wait for it to settle, "
            f"then re-run with --resume.")
    if status in ("ROLLBACK_COMPLETE", "ROLLBACK_FAILED"):
        logger.info("Storage stack %s is %s after a failed create; deleting it before "
                    "re-creating", name, status)
        if fsx is not None and status == "ROLLBACK_FAILED" and not delete_volumes(
                cfn, fsx, name, logger, time.time() + STORAGE_DELETE_TIMEOUT_S,
                STORAGE_DELETE_POLL_S):
            raise RuntimeError(f"Storage stack '{name}' is ROLLBACK_FAILED and its volume could "
                               f"not be deleted; see the warning above, then re-run with --resume.")
        cfn.delete_stack(StackName=name)
        _wait(cfn, "stack_delete_complete", name, waiter)
        status = None
    if status is not None and status not in ("CREATE_COMPLETE", "UPDATE_COMPLETE",
                                              "UPDATE_ROLLBACK_COMPLETE"):
        raise RuntimeError(f"Storage stack '{name}' is in {status}; restore it to a COMPLETE "
                           f"state, then re-run with --resume.")

    if status is None:
        cfn.create_stack(StackName=name, TemplateBody=body)
        logger.info("Storage stack %s create started; FSx typically takes 20-30 minutes", name)
        _wait(cfn, "stack_create_complete", name, waiter)
    else:
        deployed = cfn.get_template(StackName=name)["TemplateBody"]
        if isinstance(deployed, str):
            deployed = json.loads(deployed)
        refused = replacement_errors(deployed, template)
        if refused:
            raise ValueError(
                f"storage.principal: the file system in stack '{name}' already exists and the "
                f"blueprint asks for a change that would replace it and delete the datastore:"
                f"\n  • " + "\n  • ".join(refused))
        try:
            cfn.update_stack(StackName=name, TemplateBody=body)
            logger.info("Storage stack %s update started", name)
            _wait(cfn, "stack_update_complete", name, waiter)
        except cfn.exceptions.ClientError as e:
            if "No updates are to be performed" not in str(e):
                raise
            logger.info("Storage stack %s is up to date", name)
    stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def _wait(cfn, waiter_name: str, name: str, waiter_config: dict) -> None:
    try:
        cfn.get_waiter(waiter_name).wait(StackName=name, WaiterConfig=waiter_config)
    except Exception as e:  # noqa: BLE001
        try:
            status = cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
        except Exception:  # noqa: BLE001
            status = ""
        if status.endswith("_IN_PROGRESS"):
            raise RuntimeError(
                f"Storage stack '{name}' is still {status} after the wait. It may still "
                f"finish: wait until it leaves {status}, then re-run with --resume.") from e
        raise RuntimeError(
            f"Storage stack '{name}' failed: {first_failure(cfn, name)}. No EVS environment "
            f"or host exists yet. Fix the cause and re-run with --resume.") from e


def first_failure(cfn, name: str) -> str:
    """The earliest *_FAILED event reason: later ones are rollback consequences."""
    try:
        events = [e for page in cfn.get_paginator("describe_stack_events").paginate(StackName=name)
                  for e in page.get("StackEvents", [])]
    except Exception as e:  # noqa: BLE001
        return f"(could not read stack events: {e})"
    for event in reversed(events):
        reason = event.get("ResourceStatusReason") or ""
        if event.get("ResourceStatus", "").endswith("_FAILED") and reason \
                and "cancelled" not in reason.lower():
            return f"{event.get('LogicalResourceId', '?')}: {reason}"
    return "(no failure reason in the stack events)"


# ------------------------------------------------------------- verification

def verify(fsx, ec2, config: dict, entry: dict, ids: dict) -> dict:
    """Check the file system serves NFS, and return the datastore record.

    ``ids`` holds file_system_id, svm_id and volume_id (stack outputs in create
    mode, ``byo_ids`` from the blueprint's volume_id in BYO mode). BYO mode also checks
    VPC, AZ, volume shape and security group rules, because the orchestrator did
    not create them. Runs before any bare metal exists.

    Raises:
        RuntimeError: listing every problem found.
    """
    byo = is_existing(entry)
    problems = []
    fs_id, svm_id, vol_id = ids["file_system_id"], ids["svm_id"], ids["volume_id"]
    fs = _first(fsx.describe_file_systems(FileSystemIds=[fs_id]), "FileSystems")
    svm = _first(fsx.describe_storage_virtual_machines(StorageVirtualMachineIds=[svm_id]),
                 "StorageVirtualMachines")
    vol = _first(fsx.describe_volumes(VolumeIds=[vol_id]), "Volumes")
    ontap_vol = vol.get("OntapConfiguration") or {}

    if fs.get("FileSystemType") != "ONTAP":
        problems.append(f"{fs_id} is {fs.get('FileSystemType')}, not FSx for NetApp ONTAP")
    if fs.get("Lifecycle") != "AVAILABLE":
        problems.append(f"{fs_id} is {fs.get('Lifecycle')}, not AVAILABLE")
    if svm.get("FileSystemId") != fs_id:
        problems.append(f"SVM {svm_id} belongs to {svm.get('FileSystemId')}, not {fs_id}")
    if svm.get("Lifecycle") != "CREATED":
        problems.append(f"SVM {svm_id} is {svm.get('Lifecycle')}, not CREATED")
    nfs_ips = ((svm.get("Endpoints") or {}).get("Nfs") or {}).get("IpAddresses") or []
    if not nfs_ips:
        problems.append(f"SVM {svm_id} has no NFS endpoint IP address")
    if ontap_vol.get("StorageVirtualMachineId") != svm_id:
        problems.append(f"volume {vol_id} is not on SVM {svm_id}")
    if vol.get("Lifecycle") not in HEALTHY_VOLUME_STATES:
        problems.append(f"volume {vol_id} is {vol.get('Lifecycle')}")
    if not ontap_vol.get("JunctionPath"):
        problems.append(f"volume {vol_id} has no junction path, so NFS cannot mount it")

    warnings = []
    if byo:
        problems += _byo_problems(ec2, config, fs, ontap_vol, warnings)
    if problems:
        mode = "storage.principal.fsxn.volume_id" if byo else "the storage stack"
        raise RuntimeError(
            f"FSx for NetApp ONTAP principal storage from {mode} is not usable yet:\n  • "
            + "\n  • ".join(problems)
            + "\nNo EVS environment or host has been created. Fix these and re-run with --resume.")
    record = {
        "name": entry["name"],
        "mode": "byo" if byo else "create",
        "file_system_id": fs_id,
        "svm_id": svm_id,
        "volume_id": vol_id,
        "nfs_server": nfs_ips[0],
        "remote_path": ontap_vol["JunctionPath"],
        "nfs_version": NFS_VERSION,
    }
    if warnings:
        record["warnings"] = warnings
    return record


def _first(response: dict, key: str) -> dict:
    items = response.get(key) or []
    return items[0] if items else {}


def _byo_problems(ec2, config: dict, fs: dict, ontap_vol: dict,
                  warnings: list | None = None) -> list[str]:
    problems = []
    vpc_id = config["vpc"]["id"]
    if fs.get("VpcId") != vpc_id:
        problems.append(f"{fs.get('FileSystemId')} is in {fs.get('VpcId')}, not the deployment "
                        f"VPC {vpc_id}")
    deployment = (fs.get("OntapConfiguration") or {}).get("DeploymentType", "")
    if not deployment.startswith("SINGLE_AZ"):
        problems.append(f"{fs.get('FileSystemId')} is {deployment}; only Single-AZ file systems "
                        f"are supported")
    az = config.get("aws", {}).get("availability_zone")
    subnets = fs.get("SubnetIds") or []
    if az and subnets:
        subnet = _first(ec2.describe_subnets(SubnetIds=subnets[:1]), "Subnets")
        if subnet.get("AvailabilityZone") != az:
            problems.append(f"{fs.get('FileSystemId')} is in {subnet.get('AvailabilityZone')}, "
                            f"not the hosts' Availability Zone {az}")
    if ontap_vol.get("OntapVolumeType") not in (None, "RW"):
        problems.append(f"volume is {ontap_vol.get('OntapVolumeType')}, not RW")
    if ontap_vol.get("SecurityStyle") not in (None, "UNIX"):
        problems.append(f"volume security style is {ontap_vol.get('SecurityStyle')}, not UNIX")

    group_ids, groups = [], []
    eni_ids = fs.get("NetworkInterfaceIds") or []
    if eni_ids:
        enis = ec2.describe_network_interfaces(NetworkInterfaceIds=eni_ids)
        group_ids = sorted({g["GroupId"] for eni in enis.get("NetworkInterfaces", [])
                            for g in eni.get("Groups", [])})
    if group_ids:
        groups = ec2.describe_security_groups(GroupIds=group_ids).get("SecurityGroups", [])
    cidrs = [c for c, _ in source_cidrs(config)]
    missing = missing_nfs_rules(groups, cidrs)
    # A prefix list or security group reference on a rule for the same protocol
    # and port may admit the hosts, but can't be checked against their CIDR. Warn
    # for those instead of rejecting a group that may be correct (the first NFS
    # mount still proves access). Rules for other ports never count.
    unverifiable = [m for m in missing if _unresolved_rule_covers(groups, m)]
    blocking = [m for m in missing if m not in unverifiable]
    if blocking:
        problems.append(
            f"the file system's security groups {group_ids or '(none)'} do not admit NFS from "
            f"the EVS hosts. Add these inbound rules: " + "; ".join(_label(m) for m in blocking)
            + ". The orchestrator does not edit security groups it does not own.")
    elif unverifiable and warnings is not None:
        warnings.append(
            f"the file system's security groups {group_ids} admit some NFS ports only through "
            f"prefix lists or security group references, which the orchestrator can't check. "
            f"Make sure they admit the EVS hosts: "
            + "; ".join(_label(m) for m in unverifiable) + ".")
    return problems


def missing_nfs_rules(groups: list, cidrs: list[str]) -> list[tuple[str, int, int, str]]:
    """Inbound NFS requirements ``(proto, low, high, cidr)`` that no CIDR rule allows."""
    missing = []
    for cidr in cidrs:
        need = ipaddress.ip_network(cidr)
        for low, high in NFS_PORTS:
            for proto in ("tcp", "udp"):
                if not any(_rule_allows(perm, proto, low, high, need)
                           for group in groups for perm in group.get("IpPermissions", [])):
                    missing.append((proto, low, high, cidr))
    return missing


def _label(requirement: tuple[str, int, int, str]) -> str:
    proto, low, high, cidr = requirement
    ports = str(low) if low == high else f"{low}-{high}"
    return f"{proto.upper()} {ports} from {cidr}"


def _unresolved_rule_covers(groups: list, requirement: tuple[str, int, int, str]) -> bool:
    """Whether a rule for this protocol and port names its sources by prefix list
    or security group, so it may admit the hosts but can't be checked."""
    proto, low, high, _ = requirement
    return any((perm.get("PrefixListIds") or perm.get("UserIdGroupPairs"))
               and _rule_covers_port(perm, proto, low, high)
               for group in groups for perm in group.get("IpPermissions", []))


def _rule_covers_port(perm: dict, proto: str, low: int, high: int) -> bool:
    rule_proto = str(perm.get("IpProtocol"))
    if rule_proto not in ("-1", proto, {"tcp": "6", "udp": "17"}[proto]):
        return False
    return rule_proto == "-1" or (perm.get("FromPort", 0) <= low and high <= perm.get("ToPort", 65535))


def _rule_allows(perm: dict, proto: str, low: int, high: int, need) -> bool:
    if not _rule_covers_port(perm, proto, low, high):
        return False
    for ip_range in perm.get("IpRanges", []):
        try:
            if need.subnet_of(ipaddress.ip_network(ip_range.get("CidrIp", ""), strict=False)):
                return True
        except ValueError:
            continue
    return False


def config_record(record: dict) -> dict:
    """The ``principalStorage`` block written to config.json for the spec builder."""
    return {
        "datastoreName": record["name"],
        "type": FSXN,
        "mode": record["mode"],
        "nfsServer": record["nfs_server"],
        "remotePath": record["remote_path"],
        "nfsVersion": record["nfs_version"],
        "fileSystemId": record["file_system_id"],
    }


# ------------------------------------------------------ deletion protection

def _describe_stack(cfn, name: str) -> dict | None:
    """The storage stack's description, or None when it does not exist."""
    try:
        return cfn.describe_stacks(StackName=name)["Stacks"][0]
    except cfn.exceptions.ClientError as e:
        if "does not exist" in str(e):
            return None
        raise


def stack_exists(cfn, name: str) -> bool:
    """Whether the storage stack exists. Other errors (AccessDenied) propagate."""
    return _describe_stack(cfn, name) is not None


def protect_stack(cfn, name: str, logger) -> bool:
    """Turn on termination protection for the storage stack.

    Called once the whole deployment has succeeded: from then on the file
    system holds the management appliances, so deleting its stack needs an
    explicit teardown flag. A failed deployment leaves the stack unprotected,
    so it can be cleaned up or re-created. Returns False (with a warning) when
    protection could not be turned on; the deployment itself still succeeded.
    """
    try:
        cfn.update_termination_protection(StackName=name, EnableTerminationProtection=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not turn on termination protection for storage stack %s (%s). "
                       "Turn it on in the CloudFormation console to guard the datastore.", name, e)
        return False
    logger.info("Termination protection is on for storage stack %s", name)
    return True


# ---------------------------------------------------------------- teardown

# How long teardown waits for the storage stack to delete. FSx for NetApp ONTAP
# deletes vary widely: 24 minutes for one 7,168 GiB file system and over 40 for
# a 1,024 GiB one on the same day (2026-11-14).
STORAGE_DELETE_TIMEOUT_S = 2 * 3600
STORAGE_DELETE_POLL_S = 30


def delete_stack(cfn, fsx, name: str, logger, timeout: int = STORAGE_DELETE_TIMEOUT_S,
                 poll: int = STORAGE_DELETE_POLL_S) -> bool:
    """Delete the create-mode storage stack, leaving no FSx backup behind.

    A CloudFormation delete of an ONTAP volume always takes a final backup,
    which keeps billing, and CloudFormation has no option to skip it. So the
    volume is deleted first with SkipFinalBackup, then the stack; CloudFormation
    treats the already-deleted volume as deleted. Teardown never lists, matches
    or deletes backups, so a customer's own backups are never touched.

    Only ever touches the orchestrator's own stack, which is why BYO-FSxN file
    systems are never deleted: they are not in it. Callers run this only when
    the user asked to delete storage, so it turns termination protection off
    first. Returns True when the stack is gone, False when it was not deleted
    (logged as a warning, so the caller reports a partial run). Safe to re-run.
    """
    stack = _describe_stack(cfn, name)
    if stack is None:
        logger.info("No storage stack %s; nothing to delete", name)
        return True
    if stack.get("EnableTerminationProtection"):
        try:
            cfn.update_termination_protection(StackName=name, EnableTerminationProtection=False)
            logger.info("Termination protection is off for storage stack %s, to delete it", name)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not turn off termination protection for storage stack %s "
                           "(%s), so it was not deleted and keeps billing.", name, e)
            return False
    deadline = time.time() + timeout
    logger.info("Deleting storage stack %s (FSx for NetApp ONTAP bills by the hour; every VM "
                "on the datastore is deleted with it, and no backup is kept). This can take "
                "up to %d minutes.", name, timeout // 60)
    if not delete_volumes(cfn, fsx, name, logger, deadline, poll):
        return False
    try:
        cfn.delete_stack(StackName=name)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not start deleting storage stack %s (%s); checking its status.",
                       name, e)
    status = _wait_for_delete(cfn, name, max(0, int(deadline - time.time())), poll)
    if status == "DELETE_FAILED":
        logger.warning("Storage stack %s failed to delete: %s. Its file system may still be "
                       "billing; fix the cause and re-run the same command.",
                       name, _delete_failure_reason(cfn, name))
        return False
    if status != "DELETE_COMPLETE":
        logger.warning("Storage stack %s is still %s after %d minutes; its file system may "
                       "still be billing. Re-run the same command to finish the teardown once "
                       "it is deleted.", name, status, timeout // 60)
        return False
    return True


def delete_volumes(cfn, fsx, name: str, logger, deadline: float, poll: int) -> bool:
    """Delete the stack's FSx volumes with SkipFinalBackup and wait until they are gone.

    False (with a warning) when a volume could not be found in the stack, could
    not be deleted, or is still deleting at the deadline: the stack delete must
    not run then, because CloudFormation would take the final backup instead.
    """
    try:
        volumes = [r["PhysicalResourceId"]
                   for page in cfn.get_paginator("list_stack_resources").paginate(StackName=name)
                   for r in page.get("StackResourceSummaries", [])
                   if r.get("ResourceType") == "AWS::FSx::Volume" and r.get("PhysicalResourceId")
                   and r.get("ResourceStatus") != "DELETE_COMPLETE"]
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not list the volumes in storage stack %s (%s), so it was not "
                       "deleted and keeps billing. Fix the cause and re-run the same command.",
                       name, e)
        return False
    for volume_id in volumes:
        try:
            fsx.delete_volume(VolumeId=volume_id, OntapConfiguration={"SkipFinalBackup": True})
            logger.info("Deleting FSx volume %s without a final backup", volume_id)
        except Exception as e:  # noqa: BLE001
            if "VolumeNotFound" not in str(e) and "already being deleted" not in str(e).lower():
                logger.warning("Could not delete FSx volume %s in storage stack %s (%s), so the "
                               "stack was not deleted and keeps billing. Fix the cause and re-run "
                               "the same command.", volume_id, name, e)
                return False
    for volume_id in volumes:
        while True:
            state = _volume_state(fsx, volume_id)
            if state == "GONE":
                break
            if state == "FAILED" or time.time() >= deadline:
                logger.warning("FSx volume %s in storage stack %s is %s; the stack was not "
                               "deleted and keeps billing. Re-run the same command once the "
                               "volume is deleted.", volume_id, name,
                               "in a FAILED state" if state == "FAILED" else "still deleting")
                return False
            time.sleep(poll)
    return True


def _volume_state(fsx, volume_id: str) -> str:
    """GONE, FAILED, or the volume's lifecycle (UNKNOWN when describe fails)."""
    try:
        volumes = fsx.describe_volumes(VolumeIds=[volume_id]).get("Volumes", [])
    except Exception as e:  # noqa: BLE001
        return "GONE" if "VolumeNotFound" in str(e) else "UNKNOWN"
    if not volumes:
        return "GONE"
    lifecycle = volumes[0].get("Lifecycle", "")
    return "FAILED" if lifecycle == "FAILED" else lifecycle or "UNKNOWN"


def _wait_for_delete(cfn, name: str, timeout: int, poll: int) -> str:
    """Poll the stack until it is deleted or failed, or the deadline passes.

    Returns DELETE_COMPLETE (gone), DELETE_FAILED, or the last status seen
    ("UNKNOWN" when describe kept failing, for example on throttling).
    """
    deadline = time.time() + timeout
    while True:
        try:
            stack = _describe_stack(cfn, name)
            status = "DELETE_COMPLETE" if stack is None else str(stack.get("StackStatus", ""))
        except Exception:  # noqa: BLE001  (throttling: keep polling until the deadline)
            status = "UNKNOWN"
        if status in ("DELETE_COMPLETE", "DELETE_FAILED") or time.time() >= deadline:
            return status
        time.sleep(poll)


def _delete_failure_reason(cfn, name: str) -> str:
    """The first resource failure of the latest delete attempt.

    Events are newest first; stop at the stack's own DELETE_IN_PROGRESS event,
    so a failure from an earlier create or delete is never reported.
    """
    try:
        events = [e for page in cfn.get_paginator("describe_stack_events").paginate(StackName=name)
                  for e in page.get("StackEvents", [])]
    except Exception as e:  # noqa: BLE001
        return f"(could not read stack events: {e})"
    first = None
    for event in events:
        if event.get("ResourceType") == "AWS::CloudFormation::Stack" \
                and event.get("ResourceStatus") == "DELETE_IN_PROGRESS":
            break
        reason = event.get("ResourceStatusReason") or ""
        if event.get("ResourceStatus") == "DELETE_FAILED" and reason \
                and event.get("ResourceType") != "AWS::CloudFormation::Stack":
            first = f"{event.get('LogicalResourceId', '?')}: {reason}"
    return first or "(no failure reason in the stack events)"
