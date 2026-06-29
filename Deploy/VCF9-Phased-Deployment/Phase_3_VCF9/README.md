# Phase 3 - VCF Bringup + NSX Edge Cluster

Python CLI that drives the VCF Installer, NSX Manager, and vCenter using the
official **VMware VCF Python SDK** (`vcf-installer`, `vcf-nsx`) plus
`pyvmomi` for vCenter DRS operations. Three workflows:

1. **Depot + bundles** — configure the installer's Broadcom depot, kick off
   metadata syncs, list available bundles, trigger binary downloads. Replaces
   the manual "log into the installer UI and paste a token" pre-work.
2. **Bringup** — submits a VCF bringup spec to the installer
   (`POST /v1/sddcs` via `Sddcs.deploy_sddc`). Installer spawns vCenter, NSX
   Managers, SDDC Manager, VCF Operations, (and on 9.0 the Fleet Manager),
   and commissions the ESXi hosts.
3. **NSX Edge Cluster** — after bringup completes, deploys NSX edges directly
   via the NSX Manager + vCenter APIs (not SDDC Manager, intentionally, since
   SDDC Manager is going away in future VCF releases). Each edge peers BGP
   to one AWS VPC Route Server endpoint.

A single `deploy-vcf-and-edge` action chains all three end-to-end. See
[Quick start](#quick-start-deploy-everything-end-to-end) below — that's
the recommended entry point. The individual actions remain available for
troubleshooting and partial reruns.

## Architecture

### Why the SDK?

Every request/response is a typed object from the VCF SDK model. The CLI
builds an `SddcSpec`, serializes it via the SDK's own REST converter, and
the installer gets exactly the JSON it expects. No dict drift, no manual
schema upkeep. The only two bringup fields that aren't in the 9.1 SDK
typed model (`deployWithoutLicenseKeys` universally, and
`vcfOperationsFleetManagementSpec` on 9.0 only) get attached through
`VapiStruct._set_extra_fields`, so the wire JSON remains correct.

### Bringup

- `pre-evs-sync-config` (Phase 2) → typed `SddcSpec` built by
  `SddcSpecBuilder`, serialized to `Phase_3_VCF9/bringup_spec.json` with
  env-ID placeholders
- `post-evs-sync-config` (Phase 2) → regenerates the spec with env-ID-derived
  names filled in
- `start-bringup` (Phase 3) → deserializes the JSON back into an `SddcSpec`
  and hands it to `Sddcs.deploy_sddc`; returns a workflow ID
- `check-bringup` (Phase 3) → polls workflow state via `Sddcs.get_sddc_task_by_id`

### NSX Edge Cluster (NSX-direct)

- `pre-evs-sync-config` + `post-evs-sync-config` → generate `edge_cluster_spec.json`
- Phase 3 runs 7 ordered stages, each with its own CLI action. Every stage
  builds typed `vcf.nsx.model_client` objects and hands them to the matching
  SDK stub (policy API for Tier-0/Tier-1/segments/BGP; management API for
  edge transport nodes + clusters).

Stage breakdown:

| Stage | Action                  | Hits             | Resources                                                  |
|-------|-------------------------|------------------|------------------------------------------------------------|
| 1     | `prep-edge-cluster`     | NSX + vCenter    | DVS TRUNK port group, IP pool, uplink profile, VLAN TZ     |
| 2     | `deploy-edge-nodes`     | NSX              | Edge transport nodes (triggers OVA deployment, polls state)|
| 3     | `create-edge-cluster`   | NSX              | Edge cluster grouping TNs                                  |
| 4     | `create-tier0`          | NSX              | Tier-0 + locale-service + BGP enable                       |
| 5     | `create-tier1`          | NSX              | Tier-1 + locale-service attached to T0 + edge cluster      |
| 6     | `configure-routing`     | NSX              | Uplink segments, T0 uplink interfaces, prefix list, BGP neighbors, static routes, redistribution |
| 7     | `create-anti-affinity`  | vCenter          | VM-VM DRS anti-affinity rule                               |

Plus the one-shot combination `deploy-edge-cluster` that runs all 7 in
order.

### Depot + bundle management

Two tiers of actions here. The first tier uses the SDK's typed `Bundles`
client. It works cleanly on a **9.1** installer but can hit a schema-drift
bug on **9.0** where `Bundle.downloadStatus` comes back as a string when
the 9.1 SDK model expects a typed object.

The second tier uses raw REST against `/v1/bundles` via the shared
authenticated session. It side-steps the SDK's response decoder so 9.0
installers work the same as 9.1.

The two actions you actually need for a bringup are `configure-depot` and
`sync-depot`, followed by `download-all-product-binaries`. The
`prepare-depot` one-shot chains all three. Everything else is read-only
plumbing for debugging (confirm a sync succeeded, find a specific bundle
ID, probe what the installer knows about).

| Action                               | Tier | Required? | What it does                                                     |
|--------------------------------------|------|-----------|------------------------------------------------------------------|
| `prepare-depot`                      | both | **yes**¹  | One-shot: `configure-depot` → `sync-depot --wait` → `download-all-product-binaries --wait`. Same flags as those three combined |
| `configure-depot`                    | SDK  | yes¹      | Store a Broadcom download token on the installer                 |
| `sync-depot`                         | SDK  | yes¹      | Refresh the installer's bundle + release catalog from the depot  |
| `download-all-product-binaries`      | raw  | yes¹      | Batch: trigger downloads for every INSTALL-type bundle matching `--target-version`, then poll until they all finish |
| `get-depot-settings`                 | SDK  | debug     | Read the stored depot settings (token redacted)                  |
| `list-releases`                      | SDK  | debug     | List VCF releases from the release catalog                       |
| `list-bundles`                       | SDK  | debug     | List bundles (may fail on 9.0 with `UnresolvedError`)            |
| `get-bundle`                         | SDK  | debug     | Fetch a single bundle (same 9.0 caveat)                          |
| `download-bundle`                    | SDK  | debug     | Kick off a download for a single bundle via `/v1/bundles/{id}`   |
| `list-product-binaries`              | raw  | debug     | Same as `list-bundles` but raw-REST — works on 9.0 + 9.1         |
| `download-product-binary`            | raw  | debug     | Same as `download-bundle` but raw-REST                           |
| `probe-product-binaries`             | raw  | debug     | Probes a handful of catalog endpoints                            |

¹ Either run `prepare-depot` (one-shot) **or** the three individual
`configure-depot` + `sync-depot` + `download-all-product-binaries` calls.
Not both.

`sync-depot --wait`, `download-bundle --wait`, and
`download-all-product-binaries --wait` poll until the operation terminates
(success or failure) instead of returning immediately. The HTTPS session
has automatic retries (5 attempts with exponential backoff) to survive
idle-socket resets the installer can throw during long waits.

---

## Prerequisites

- Python 3.10+
- Phase 1 applied
- Phase 2 complete: `pre-evs-sync-config`, `create-environment`, `create-hosts`,
  `post-evs-sync-config`. This populates both `bringup_spec.json` and
  `edge_cluster_spec.json`.
- A Broadcom Download Token. Generate it in the Broadcom Support Portal →
  My Dashboard → Quick Links → Generate Download Token. Set it as
  `VCF_DEPOT_TOKEN` before running anything.
- Manual ESXi pre-work and VCF Installer OVA deploy — see
  [Manual pre-work](#manual-pre-work-before-anything-else).

## Jumpbox first-time setup

On a freshly created jumpbox, install the required tools before
proceeding. Run all commands in PowerShell as Administrator.

> **Note:** Exact commands may vary depending on your Windows Server version
> and PowerShell version. If `winget` is not available, download installers
> directly from python.org and aws.amazon.com/cli.

```powershell
# 1. Enable long paths (vcf-nsx package exceeds 260-char limit)
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" `
  -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force

# 2. Set execution policy
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# 3. Install Python (download from python.org or use winget)
winget install Python.Python.3.12 --source winget

# 4. Install AWS CLI
winget install Amazon.AWSCLI --source winget

# 5. Install Git
winget install Git.Git --source winget

# 6. Close and reopen PowerShell, then configure AWS credentials
aws configure
# Enter your Access Key, Secret Key, region (us-east-2), output (json)

# 7. Clone the repo
git clone https://github.com/aws/solutions-for-amazon-evs.git

# 8. Set up Python venv
cd Phase_3_VCF9\python
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## Manual pre-work (before anything else)

> **All Phase 3 commands must be run from the Windows jumpbox** (connected
> via RDP). The scripts require direct network access to the ESXi hosts,
> VCF Installer, NSX Manager, and vCenter on the management subnet, which
> is not accessible from your local machine.

These steps happen once per environment, before any Phase 3 CLI runs.
They're manual because they involve clicking around the ESXi UI and
uploading an OVA from a local machine — neither is currently automated.

### 0. Retrieve ESXi host passwords

You need the root password for the host with the EBS volume to log into
its ESXi web UI. EVS stores passwords for all hosts in Secrets Manager:

```powershell
$ENV_ID = "env-xxxxxxxxxx"  # from config.json environmentId
aws secretsmanager get-secret-value --secret-id "evs!${ENV_ID}_esxi01" --query SecretString --output text
aws secretsmanager get-secret-value --secret-id "evs!${ENV_ID}_esxi02" --query SecretString --output text
aws secretsmanager get-secret-value --secret-id "evs!${ENV_ID}_esxi03" --query SecretString --output text
```

### 1. Configure ESXi hosts

Log in to the host that has the 256 GB EBS volume attached (from Phase 2)
at `https://<host-IP>` as `root` (password from Secrets Manager:
`evs!<env_id>_<host>`):

**a) VLAN 20 tagging:**

1. Navigate: **Networking** → **Port groups** tab
2. Click **VM Network** → **Edit settings**
3. Set **VLAN ID** to `20` → **Save**

**b) VMFS datastore:**

1. Navigate: **Storage** (left menu)
2. Click **New Datastore**
3. Select **Create new VMFS datastore** → Next
4. Name it `VCF-Installer-VMFS`
5. Select the **256 GB NVMe device** (the EBS volume attached by Phase 2)
6. Use **VMFS 6**, full disk allocation → Finish

**Verification:**
- Host shows `VM Network` on VLAN 20
- Host has the new `VCF-Installer-VMFS` datastore

**Time:** ~5 minutes.

### 2. Deploy the VCF Installer OVA

Using the host from step 1 that has the VMFS datastore:

1. **Download** the VCF Installer OVA
   (`VCF-SDDC-Manager-Appliance-9.0.2.x.x.ova` or equivalent) from the
   Broadcom Support Portal
2. In the ESXi web UI, click **Actions** (top menu) → **Deploy OVF template**
3. Select the locally downloaded OVA file
4. Pick the `VCF-Installer-VMFS` datastore
5. Attach to the `VM Network` port group
6. Fill in the OVA deployment form:

   | Field | Value                                                                                                                                                       |
   |-------|-------------------------------------------------------------------------------------------------------------------------------------------------------------|
   | Host Name | `sddcm` (short hostname only — NOT the FQDN. The installer appends the domain from DNS Domain automatically)                                                |
   | Root Password | Choose and note (needed for appliance troubleshooting)                                                                                                          |
   | Admin (local) Password | Phase 2 generates one at `evs-<env>_sddcManagerLocal` in Secrets Manager — you can use that or choose your own. Either way, this becomes `VCF_INSTALLER_PASSWORD` for Phase 3 |
   | NTP Servers | `time.aws.com`                                                                                                                                              |
   | Network 1 IPv4 | A host IP address for SDDC Manager on the VM Management subnet — NOT the subnet address. Must match the DNS A-record for `sddcm.<fqdn>` (e.g. `10.0.60.12`) |
   | Subnet Mask | `255.255.255.0` (for /24 VM Management)                                                                                                                     |
   | Gateway | First IP of VM Management CIDR (e.g. `10.0.60.1`)                                                                                                           |
   | DNS Domain | `<fqdn>` (e.g. `evs.dev`)                                                                                                                                   |
   | Domain Search Path | `<fqdn>` (e.g. evs.dev)                                                                                                                                     |
   | Domain Name Servers | Same as `dnsServers` in config.json (e.g. `10.0.0.100,10.0.0.101`)                                                                                          |

7. Power on the VM

**Verification:**
- VM powers on and boot completes (~5 minutes)
- `https://sddcm.<fqdn>` loads the installer UI
- `admin@local` logs in successfully

**Time:** ~15-20 minutes for OVA upload and boot.

---

## Quick start: deploy everything end-to-end

`deploy-vcf-and-edge` is the **one-click SDDC + NSX edge cluster
deployment** — it chains all Phase 3 stages in order:

1. **`prepare-depot`** — configure the Broadcom token, sync the depot,
   download all INSTALL bundles for `--target-version`. ~30-60 min.
   Versions 9.0.2 and 9.1 use pinned bundle versions for a deterministic
   BOM; unpinned versions fall back to dynamic GA-bundle selection.
2. **`start-bringup --wait`** — POST the bringup spec, then poll the
   workflow every 10 minutes until it terminates. ~2-4 hours.
3. **`remove-installer-datastore`** — storage-vMotion any VMs off the
   local installer VMFS to vSAN, then unmount the datastore. ~5-10 min.
4. **`destroy-ebs-volume`** — detach + delete the EBS volume that
   hosted the now-unmounted VMFS. Tag-gated so it can't accidentally
   touch the wrong volume. ~30 sec.
5. **`deploy-edge-cluster`** — run all 7 NSX edge stages in order.
   ~30-50 min.
6. **`create-connector`** — register the VCF Operations Manager
   connector with EVS via `CreateEnvironmentConnector`. Polls until
   ACTIVE. ~2-5 min.

Total runtime: **~3-5 hours**, dominated by step 2 (bringup).

**Linux / macOS:**
```bash
read -rs VCF_INSTALLER_PASSWORD ; export VCF_INSTALLER_PASSWORD
read -rs VCF_DEPOT_TOKEN        ; export VCF_DEPOT_TOKEN

python -m src.main deploy-vcf-and-edge \
  --installer-host sddcm.my.fqdn.evs \
  --target-version 9.0.2 \
  --nsx-manager-host nsx.my.fqdn.evs \
  --vcenter-host vc.my.fqdn.evs \
  --aws-profile <profile>
```

**Windows (PowerShell):**
```powershell
$env:VCF_INSTALLER_PASSWORD = [Net.NetworkCredential]::new('', (Read-Host -AsSecureString -Prompt 'VCF_INSTALLER_PASSWORD')).Password
$env:VCF_DEPOT_TOKEN = [Net.NetworkCredential]::new('', (Read-Host -AsSecureString -Prompt 'VCF_DEPOT_TOKEN')).Password

python -m src.main deploy-vcf-and-edge --installer-host sddcm.my.fqdn.evs --target-version 9.0.2 --nsx-manager-host nsx.my.fqdn.evs --vcenter-host vc.my.fqdn.evs --aws-profile <profile>
```

The chain runs a Secrets Manager precheck (~2 sec) before step 1 — if any
required VCF appliance secret is missing, the whole pipeline fails fast
with a single error listing every absent role. NSX/vCenter/VCF Installer
passwords resolve through CLI > env > Secrets Manager > prompt. After a
fresh first-time prompt the installer password is stashed back into
Secrets Manager so subsequent runs are prompt-free.

If a substep fails, the chain aborts with a non-zero exit. Each substep
is idempotent — fixing the failure and re-running the one-click SDDC +
NSX deployment fast-forwards through the substeps that already
completed (depot bundles already downloaded, bringup already finished,
EBS volume already gone, etc.) and resumes at the broken one.

After the one-click SDDC + NSX deployment exits, verify end-to-end
connectivity — see
[Step 7 — Verify end-to-end connectivity](#step-7--verify-end-to-end-connectivity)
below.

## Individual actions (for troubleshooting or partial reruns)

The one-click SDDC + NSX deployment covers the happy path. The
individual actions below let you inspect state between phases, rerun a
single failed substep, or bypass parts of the pipeline. Every
individual action is also what the one-click SDDC + NSX deployment
invokes internally — same code path, same logs.

### Setting passwords as env vars

> **Tip:** Phase 2 pre-generates the SDDC Manager local password and stores it
> in Secrets Manager as `evs-<env>_sddcManagerLocal`. If you used that password
> when deploying the OVA, retrieve it with:
> ```bash
> # Linux / macOS
> aws secretsmanager get-secret-value --secret-id "evs-<env>_sddcManagerLocal" \
>   --region <region> --query SecretString --output text | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])"
> ```
> ```powershell
> # Windows (PowerShell)
> (aws secretsmanager get-secret-value --secret-id "evs-<env>_sddcManagerLocal" `
>   --region <region> --query 'SecretString' --output text | ConvertFrom-Json).password
> ```
> You are free to use this generated password or choose your own during OVA
> deployment — just ensure `VCF_INSTALLER_PASSWORD` matches whatever you set.

**Linux / macOS:**
```bash
read -rs VCF_INSTALLER_PASSWORD ; export VCF_INSTALLER_PASSWORD
read -rs VCF_DEPOT_TOKEN        ; export VCF_DEPOT_TOKEN
```

**Windows (PowerShell):**
```powershell
$env:VCF_INSTALLER_PASSWORD = [Net.NetworkCredential]::new('', (Read-Host -AsSecureString -Prompt 'VCF_INSTALLER_PASSWORD')).Password
$env:VCF_DEPOT_TOKEN = [Net.NetworkCredential]::new('', (Read-Host -AsSecureString -Prompt 'VCF_DEPOT_TOKEN')).Password
```

NSX Manager and vCenter SSO passwords are pulled from AWS Secrets
Manager (`evs-<env>_nsxAdmin` and `..._vcenterSso`) at runtime,
so you don't need to set them. Make sure your AWS credentials (profile
or default chain) have `secretsmanager:DescribeSecret` and
`GetSecretValue`. To override the SM lookup (e.g. you've rotated a
password manually), set `VCF_NSX_MANAGER_PASSWORD` /
`VCF_VCENTER_PASSWORD` or pass `--nsx-manager-password` /
`--vcenter-password`.

### Step 1 — Depot setup (`prepare-depot` or three substeps)

**One-shot:**

```bash
python -m src.main prepare-depot \
  --installer-host sddcm.my.fqdn.evs --target-version 9.0.2
```

Chains `configure-depot` → `sync-depot --wait` →
`download-all-product-binaries --target-version 9.0.2 --wait`. Internally
implicit `--wait` on every substep — there's no useful state mid-chain.

**Three substeps (when you want to inspect catalog state between):**

```bash
# Write the Broadcom token onto the installer:
python -m src.main configure-depot --installer-host sddcm.my.fqdn.evs

# Refresh the installer's bundle + release catalog:
python -m src.main sync-depot --installer-host sddcm.my.fqdn.evs --wait

# Download every INSTALL bundle for the target version:
python -m src.main download-all-product-binaries \
  --installer-host sddcm.my.fqdn.evs --target-version 9.0.2 --wait
```

**Verification:**
- `configure-depot` returns `status: "DEPOT_CONNECTION_SUCCESSFUL"` and
  `hasDownloadToken: true`
- `sync-depot` returns `syncStatus: "SYNCED"` with a non-null
  `lastSyncCompletionTimestamp`
- `download-all-product-binaries` logs a `start <id> (TYPE VERSION,
  <size>MB)` line per bundle and ends with `All bundles downloaded
  successfully.`
- Installer UI → Bundles shows every filtered bundle as `SUCCESSFUL`

**Troubleshooting:**
- `sync-depot` `FAILURE` with `errorMessage` — usually a bad/expired
  token or no outbound reachability to the Broadcom depot.
- Connection resets during long polls are handled automatically.
- If a bundle gets stuck `IN_PROGRESS`, ctrl-C and rerun — already-done
  bundles are skipped.

**Time:** 30-60 minutes total, dominated by binary downloads.

### Step 2 — Start VCF bringup

```bash
python -m src.main start-bringup \
  --installer-host sddcm.my.fqdn.evs \
  --aws-profile <profile> --wait
```

`--wait` blocks until the workflow terminates (poll cadence: 10 min;
typical runtime: 2-4 hours). Drop `--wait` to return with the workflow
ID immediately and poll separately with `check-bringup`.

**What this does:**
- Authenticates to the installer (`POST /v1/tokens`, Bearer)
- Loads `bringup_spec.json`, resolves every `__SECRET:<role>__`
  placeholder against AWS Secrets Manager
  (`evs-<env>_<role>` for appliance roles,
  `evs!<env>_<host>` for ESXi roles), then deserializes into a
  typed `SddcSpec`
- Overlays the operator-provided installer password into
  `sddcManagerSpec.localUserPassword` (the bringup's
  `useExistingDeployment=true` path means the local user creds must
  match what's already on the appliance)
- Calls `Sddcs.deploy_sddc(sddc_spec)` and (with `--wait`) polls
  `Sddcs.get_sddc_task_by_id` until terminal
- At the very end of bringup the appliance transforms in-place into the
  SDDC Manager and the installer API stops responding to its old
  workflow id. We probe `https://<host>/v1/system` to confirm SDDC
  Manager is up, and treat that as `COMPLETED_WITH_SUCCESS`

**Verification (`COMPLETED_WITH_SUCCESS`):**
- `https://vc.<fqdn>` → vCenter, `administrator@vsphere.local` logs in
  with the password from `evs-<env>_vcenterSso`
- `https://nsx.<fqdn>` → NSX Manager, `admin` logs in with the password
  from `evs-<env>_nsxAdmin`
- `https://sddcm.<fqdn>` → SDDC Manager UI loads
- All 3 ESXi hosts show as fabric nodes in NSX

**Troubleshooting:**
- `QUICK_START_VALIDATION_FAILED` with "Validation for X failed" —
  inspect the error's special-character or length advice; our generator
  targets the intersection of every appliance's complexity rules but
  validators occasionally surface new constraints.
- `SSL Certificate common name doesn't match ESX FQDN` — ESXi cert is
  factory `localhost.localdomain` and needs to be regenerated. Until
  upstream auto-regen is back online: SSH into each host
  (`/sbin/generate-certificates && /etc/init.d/hostd restart && /etc/init.d/vpxa restart`)
  to rotate the cert to match the FQDN.
- 401 on auth — installer password wrong, or `admin@local` PAM lockout
  from prior failed attempts (unlock with
  `faillock --user admin --reset` over SSH).
- **Cached failed submission:** If bringup validation fails due to a spec
  error (e.g., wrong hostname), the installer may cache the failed
  submission. Even after fixing the spec, retries can validate against the
  old cached spec. The reliable fix is to delete the installer VM and
  redeploy a fresh OVA. Fix the spec BEFORE redeploying to avoid repeating
  the issue.

**Poll without `--wait`:**

```bash
python -m src.main check-bringup \
  --installer-host sddcm.my.fqdn.evs --workflow-id <id-from-start>
```

The installer UI is usually a cleaner place to watch — per-task progress
and per-task error detail.

**Time:** 2-4 hours.

**Recovery after failed bringup:**

If bringup fails after the Host Configuration phase, hosts are left in a
dirty state (extra VMkernel adapters, vSAN disk partitions, distributed
switches, partially deployed VMs). Retrying bringup without cleanup will
fail with validation errors. The recommended recovery is:

1. Delete all hosts via EVS:
   ```bash
   aws evs delete-environment-host --environment-id <id> --host-name <host> --region <region>
   ```
2. Wait for deletion to complete
3. Re-run `create-hosts` (Phase 2)
4. Redo all manual pre-work (VLAN 20, VMFS, OVA deployment)
5. Re-run Phase 3

Manual cleanup (removing vSwitches, vmkernel adapters, vSAN disk groups
via the ESXi UI) is possible but error-prone and not recommended.

**Resuming after a crash:**

If `deploy-vcf-and-edge` crashes mid-way (e.g., token expiry), 
the bringup continues running on the installer. Check status in
the installer UI (`https://sddcm.<fqdn>`).

### Step 3 — Clean up the local installer datastore

```bash
python -m src.main remove-installer-datastore \
  --vcenter-host vc.my.fqdn.evs \
  --aws-profile <profile>
```

`--cluster-name` defaults to `clusterSpec.clusterName` from
`bringup_spec.json` when omitted. Pass it explicitly to override.

**What this does:**
- Finds the one non-vSAN datastore on the cluster whose capacity is
  256 ± 5 GB (the local VMFS sitting on the EBS volume — VMFS reports
  ~255 GB usable on a 256 GB EBS volume)
- For each VM still registered there, storage-vMotions to the cluster's
  vSAN datastore via `RelocateVM_Task` (synchronous; fails fast if no
  vSAN datastore is reachable or if any vMotion errors)
- Calls `HostDatastoreSystem.RemoveDatastore` on the owning host once
  the target is empty

**Verification:**
- Output shows `removed: true`, the datastore name, host, capacity, and
  list of VMs that were vMotion'd off
- vCenter UI → Datastores — only the vSAN datastore remains on the
  management cluster

**Doesn't touch AWS:** the EBS volume is still attached to the EC2
instance after this call. Run the EBS-cleanup follow-up in
[After the one-click SDDC + NSX deployment](#after-the-one-click-sddc--nsx-deployment) below to fully release it.

**Time:** 5-10 minutes (most of it the storage-vMotion).

### Step 4 — Destroy the EBS volume

```bash
python -m src.main destroy-ebs-volume --aws-profile <profile>
```

Pulls the env id from the bringup or edge cluster spec, then detaches
+ deletes the EBS volume Phase 2 created. Tag-gated: only volumes
carrying `ManagedBy=phase2-automation` and the matching
`EnvironmentId` are eligible. Idempotent — already-gone returns
`{"deleted": false, "reason": "not present"}` and exits clean.

**Verification:**
- EC2 console → Volumes — the 256 GB gp3 volume tagged for this env is gone

**Time:** ~30 seconds.

### Step 5 — Deploy the NSX edge cluster

**End-to-end:**

```bash
python -m src.main deploy-edge-cluster \
  --nsx-manager-host nsx.my.fqdn.evs --vcenter-host vc.my.fqdn.evs \
  --aws-profile <profile>
```

Runs all 7 stages in order, ~30-50 minutes total. Cross-stage NSX
resource lookups use poll-with-timeout helpers so the chain doesn't
race NSX's policy-side projection of management-API resources.

Add `--dry-run` to preview the whole plan.

**Per-stage runs** (when one stage fails or you want to inspect
intermediate state):

> Every edge stage resolves the `__SECRET:edgeAppliance__` placeholder
> from `edge_cluster_spec.json` against AWS Secrets Manager. Pass
> `--aws-profile <profile>` on every call so credentials resolve
> cleanly.

```bash
# Stage 1: prep-edge-cluster (~30 sec)
python -m src.main prep-edge-cluster \
  --nsx-manager-host nsx.my.fqdn.evs --vcenter-host vc.my.fqdn.evs \
  --aws-profile <profile>

# Stage 2: deploy-edge-nodes (~20-40 min — OVA deploys × 2)
python -m src.main deploy-edge-nodes \
  --nsx-manager-host nsx.my.fqdn.evs --vcenter-host vc.my.fqdn.evs \
  --aws-profile <profile>

# Stage 3: create-edge-cluster (~10 sec)
python -m src.main create-edge-cluster \
  --nsx-manager-host nsx.my.fqdn.evs --vcenter-host vc.my.fqdn.evs \
  --aws-profile <profile>

# Stage 4: create-tier0 (~10 sec)
python -m src.main create-tier0 \
  --nsx-manager-host nsx.my.fqdn.evs --vcenter-host vc.my.fqdn.evs \
  --aws-profile <profile>

# Stage 5: create-tier1 (~5 sec)
python -m src.main create-tier1 \
  --nsx-manager-host nsx.my.fqdn.evs --vcenter-host vc.my.fqdn.evs \
  --aws-profile <profile>

# Stage 6: configure-routing (~20 sec API calls + 1-2 min for BGP to come up)
python -m src.main configure-routing \
  --nsx-manager-host nsx.my.fqdn.evs --vcenter-host vc.my.fqdn.evs \
  --aws-profile <profile>

# Stage 7: create-anti-affinity (~5 sec)
python -m src.main create-anti-affinity \
  --nsx-manager-host nsx.my.fqdn.evs --vcenter-host vc.my.fqdn.evs \
  --aws-profile <profile>
```

**Per-stage verification** (resources created):

| Stage | Resources |
|-------|-----------|
| 1 | DVS port group `pg-edge-uplink1`; NSX IP pool `<env>-edge-tep-pool` (10.0.50.0/24); uplink profile (MTU 1500, VLAN 60, FAILOVER_ORDER); VLAN TZ `<env>-tz-vlan01` |
| 2 | 2 edge transport nodes (`edge01.<fqdn>`, `edge02.<fqdn>`); 2 new edge VMs in vCenter |
| 3 | Edge cluster `<env>-ec01` grouping both TNs |
| 4 | Tier-0 (HA=ACTIVE_STANDBY, failover=PREEMPTIVE); T0 locale-service; BGP enabled (ASN 65000) |
| 5 | Tier-1 linked to T0; T1 locale-service |
| 6 | VLAN segment on uplink VLAN; 2 T0 uplink interfaces; `<env>-rfc-1918-allow` prefix list; 2 BGP neighbors; 3 static routes; route redistribution config |
| 7 | DRS VM-VM anti-affinity rule keeping the two edges on different hosts |

**Stage 6 specifically** is where AWS-side connectivity comes alive:

- T0 → BGP → Neighbors must reach `Established` within a few minutes
- AWS VPC console → VPC Route Server → Peers shows `Established`
- NSX advertises connected networks to the Route Server; AWS propagates
  them into the service access route table

**Troubleshooting Stage 2:**
- Stalls at "deploying VM" — check vCenter events for OVA deployment
  errors (datastore full, port group missing)
- Stuck at "joining fabric" — NSX-to-edge management network
  connectivity issue
- 30-min per-edge timeout can be bumped in code if your env is slower

**Troubleshooting Stage 6:**
- BGP neighbors stuck `Down` — check the security group allows TCP/179
  on the uplink VLAN in both directions; confirm edge uplink IPs match
  Phase 1's Route Server peers (`10.0.80.10` / `10.0.80.11`)

### Step 6 — Create Ops Manager connector

```bash
python -m src.main create-connector --aws-profile <profile> --wait
```

Registers the VCF Operations Manager with EVS so the service can
perform lifecycle operations. Resolves the appliance FQDN from
`vcfOperationsSpec.loadBalancerFqdn` in the bringup spec and the
secret ARN from `evs-<env>_operationsAdmin` in Secrets Manager.

**What this does:**
- Checks if an OPERATIONS_MANAGER connector already exists (idempotent)
- Calls `CreateEnvironmentConnector` with the resolved FQDN and secret ARN
- With `--wait`, polls until the connector reaches `ACTIVE` (~2-5 min)

**Prerequisites:**
- The `evs-<env>_operationsAdmin` secret must be tagged with
  `EvsAccess: True` and stored as JSON: `{"username": "admin", "password": "..."}`
- Phase 2's `post-evs-sync-config` handles this automatically for new
  deployments

**Override:** Pass `--connector-secret-arn` to use a different secret ARN.

**Verification:**
```bash
aws evs list-environment-connectors --environment-id <env-id> --region <region>
```
Should show `state: ACTIVE`.

**Time:** ~2-5 minutes.

### Step 7 — Verify end-to-end connectivity

Once Stage 6 is `Established`:

1. Inside NSX, create a test segment on T1, attach a VM, confirm it
   gets an IP
2. Ping from the VM to an AWS resource (jumpbox in the public subnet,
   or an EC2 instance in the service access subnet)
3. Check Route Server peers in AWS console — learned routes should
   show NSX-originated CIDRs

---

## After the one-click SDDC + NSX deployment

Nothing — the one-click SDDC + NSX deployment handles every cleanup
step (datastore unmount + EBS detach/delete). The only post-run task
is verifying end-to-end
connectivity (see [Step 7 — Verify end-to-end
connectivity](#step-7--verify-end-to-end-connectivity) in the
Individual actions section).

**Verification:**
- EC2 console → Volumes — the 256 GB gp3 volume tagged for this env is gone
- vCenter UI → Datastores — only the vSAN datastore remains on the management cluster
- The VCF Installer / SDDC Manager VM still runs (now on vSAN)

---

## CLI options

| Flag                       | Required for                                                                 | Description                                                                 |
|----------------------------|------------------------------------------------------------------------------|-----------------------------------------------------------------------------|
| `action`                   | always                                                                       | See action tables above                                                     |
| `--installer-host`         | bringup + depot actions                                                      | IP or FQDN of the VCF Installer                                             |
| `--installer-username`     | -                                                                            | Defaults to `admin@local`                                                   |
| `--installer-password`     | -                                                                            | Falls back to `$VCF_INSTALLER_PASSWORD`, then Secrets Manager (`evs-<env>_vcfInstaller`), then prompt. First-prompt value is stashed back to SM so future runs skip the prompt. |
| `--depot-token`            | `configure-depot`, `prepare-depot`                                           | Falls back to `$VCF_DEPOT_TOKEN`                                            |
| `--depot-username`         | -                                                                            | Optional Broadcom account username                                          |
| `--depot-password`         | -                                                                            | Optional Broadcom account password                                          |
| `--wait`                   | `sync-depot`, `download-bundle`, `download-all-product-binaries`, `start-bringup` | Poll until the operation completes. `start-bringup` polls every 10 minutes (typical bringup is 2-4 hours) |
| `--bundle-id`              | `get-bundle`, `download-bundle`, `download-product-binary`                   | Bundle id                                                                   |
| `--bundle-product-type`    | `list-bundles`, `list-product-binaries`                                      | Filter: e.g. `VCF`, `NSX`, `VCENTER`                                        |
| `--bundle-type`            | `list-bundles`, `list-product-binaries`                                      | Filter: e.g. `INSTALL`, `PATCH`, `UPGRADE`                                  |
| `--target-version`         | `download-all-product-binaries`, `prepare-depot`                             | Only download bundles whose version starts with this (e.g. `9.0.2`)          |
| `--applicable-for-version` | `list-releases`                                                              | Filter: e.g. `9.0.1`                                                        |
| `--nsx-manager-host`       | NSX + edge actions                                                           | IP or FQDN of NSX Manager                                                   |
| `--nsx-manager-username`   | -                                                                            | Defaults to `admin`                                                         |
| `--nsx-manager-password`   | -                                                                            | Falls back to `$VCF_NSX_MANAGER_PASSWORD`, then Secrets Manager (`evs-<env>_nsxAdmin`), then prompt |
| `--vcenter-host`           | vCenter + edge actions                                                       | vCenter FQDN or IP                                                          |
| `--vcenter-username`       | -                                                                            | Defaults to `administrator@vsphere.local`                                   |
| `--vcenter-password`       | -                                                                            | Falls back to `$VCF_VCENTER_PASSWORD`, then Secrets Manager (`evs-<env>_vcenterSso`), then prompt |
| `--cluster-name`           | -                                                                            | Cluster name for `remove-installer-datastore`. Defaults to `clusterSpec.clusterName` from `bringup_spec.json`. |
| `--spec-path`              | -                                                                            | Override spec JSON path                                                     |
| `--workflow-id`            | `check-bringup`                                                              | Workflow ID returned by `start-bringup`                                     |
| `--aws-profile`            | bringup, edge, `deploy-vcf-and-edge`                                         | AWS profile for Secrets Manager. Falls back to `$AWS_PROFILE` / default chain |
| `--aws-region`             | -                                                                            | AWS region for Secrets Manager. Falls back to `$AWS_REGION`, then `__region__` from the bringup spec, then `us-east-1`. |
| `--no-secrets-manager`     | -                                                                            | Skip Secrets Manager wiring (dry-run only — real deploys will fail with placeholders) |
| `--connector-secret-arn`   | -                                                                            | Override the secret ARN for `create-connector`. If omitted, auto-resolves from `evs-<env>_operationsAdmin` |
| `--verify-tls`             | -                                                                            | Verify TLS cert (default: skip — appliance is self-signed)                  |
| `--dry-run`                | start/deploy actions                                                         | Show the plan without touching anything                                     |

---

## Security

VCF appliance passwords (vCenter root + SSO, NSX root/admin/audit, SDDC
Manager root/ssh/local, VCF Operations admin/master/collector, edge
appliance, plus 9.0-only fleet manager root + admin) are generated by
Phase 2's `post-evs-sync-config` and stored in AWS Secrets Manager under
`evs-<env_id>_<role>`. Each secret is stored as JSON
(`{"username": "<role-specific>", "password": "<generated>"}`) and tagged
with `EvsAccess: True` so the EVS service can access them for connector
registration. Phase 3 fetches them at runtime via boto3 in two ways:

- **Spec placeholder resolution** — the on-disk `bringup_spec.json` and
  `edge_cluster_spec.json` carry `__SECRET:<role>__` placeholders, never
  real plaintext. Phase 3 substitutes them in-memory just before POSTing.
- **Runtime auth fallback** — for NSX Manager admin and vCenter SSO
  administrator (the same passwords bringup deployed), the
  `--nsx-manager-password` / `--vcenter-password` flags fall back to
  Secrets Manager when neither the flag nor the env var is set, before
  prompting. Lookup order: CLI flag > env var (`VCF_NSX_MANAGER_PASSWORD`
  / `VCF_VCENTER_PASSWORD`) > Secrets Manager > interactive prompt.

The VCF Installer `admin@local` password (set during OVA deploy)
follows the same fallback chain. The first time the operator types it
at the prompt, Phase 3 stashes it back to
`evs-<env>_vcfInstaller` so subsequent runs are prompt-free.

ESXi root passwords flow through the placeholder path too. The bringup
spec carries `__SECRET:esxi:<host>__` per host; Phase 3's resolver
fetches each from the privileged service-managed secret named
`evs!<env_id>_<host>` (bang-form, set by EVS when the host
was provisioned).

`deploy-vcf-and-edge` runs a precheck before the first substep that
`DescribeSecret`s every required role. Missing secrets fail the run
fast with a single error listing all absent roles.

Required IAM permissions on the role/profile Phase 3 runs as:
- `secretsmanager:DescribeSecret` — for the precheck
- `secretsmanager:GetSecretValue` — for the per-stage resolution
- `secretsmanager:CreateSecret` and `secretsmanager:PutSecretValue` —
  only used for the installer-password stash on first prompt

Pass the AWS profile via `--aws-profile`, region via `--aws-region`,
or use the default credential chain (env vars, instance profile,
`~/.aws/credentials`). For dry runs and offline inspection, pass
`--no-secrets-manager` to skip the wiring.

Credentials that don't live in Secrets Manager:
- **Broadcom depot token** (`VCF_DEPOT_TOKEN`) — issued by Broadcom,
  external to AWS.

### TLS certificate verification

By default, Phase 3 skips TLS certificate verification (`--verify-tls`
opts in) because VCF appliances ship with self-signed certificates. All
connections to the VCF Installer, NSX Manager, and vCenter use HTTPS but
do not validate the server certificate unless `--verify-tls` is passed.

If you are running Phase 3 across an untrusted network (not within the
VPC), enable TLS verification and configure proper CA-signed certificates
on the appliances to prevent credential interception via MITM.

### ESXi thumbprint collection

Phase 2's `host_thumbprints.py` connects to each ESXi host with TLS
verification disabled (`ssl.CERT_NONE`) to fetch the host's certificate
fingerprint. This is required by design — you cannot verify a self-signed
certificate before you have fingerprinted it (trust-on-first-use). The
fetched thumbprints are written into the bringup spec for the installer's
host validation step. MITM risk is mitigated by running Phase 2 from
within the VPC where the ESXi hosts reside (private network, no external
routing).

---

## Troubleshooting

| Problem | Cause | Fix                                                                                      |
|---------|-------|------------------------------------------------------------------------------------------|
| `Connection refused` on installer host | Network connectivity lost to the installer appliance | Verify network access to the ESXi management subnet from the jumpbox                     |
| `LCM_MANIFEST_NOT_FOUND` | Bundles not downloaded | Run `prepare-depot --target-version <ver>`                                               |
| `WORKFLOW_OPTIONS_ERROR: VMwareProductVersion null` | Bundles incomplete or wrong version | Verify all pinned bundles show SUCCESSFUL                                                |
| `Failed to validate connectivity of VMOTION/VSAN` | Network not reachable from installer pre-check | Verify VLAN 20 tagging on the installer host                                             |
| `Incorrect SDDC Manager hostname` | OVA deployed with FQDN instead of short name | Redeploy OVA with short hostname (e.g., `sddcm` not `sddcm.evs.dev`)                     |
| `vSwitch already exists on ESX Host` | Dirty hosts from previous failed bringup | Delete and recreate hosts                                                                |
| Installer caches failed submission | Spec error cached in installer state | Redeploy fresh OVA; fix spec BEFORE redeploying                                          |
| JWT/token expired during bringup poll | Token not refreshed during long poll | Bringup continues on installer; check UI for status, resume remaining steps individually |
| `Distributed virtual switch not found` | Edge spec has stale env ID | Re-run `post-evs-sync-config` to regenerate edge spec. Then delete and recreate hosts    |
| `Unable to access secret` for connector | Secret missing `EvsAccess:True` tag or wrong format | Tag secret + ensure JSON format `{"username":"...","password":"..."}`                    |
| BGP neighbors stuck `Down` after edge deploy | Security group missing TCP/179 on uplink VLAN | Add rule; verify edge uplink IPs match Route Server peers                                |

### Rollback

- **After bringup fails partway:** Delete and recreate hosts (see Phase 2 rollback)
- **After bringup succeeds:** Environment is deployed; tear down by deleting the EVS environment
- **After edge cluster:** NSX resources persist; delete via NSX Manager UI or API
- **After connector:** `aws evs delete-environment-connector --environment-id <id> --connector-id <id> --region <region>`
