# SDDC Spec Builder (standalone)

An interactive CLI that generates a VCF Installer bringup spec (SDDC
spec) JSON you paste into the installer UI by hand. It's a self-contained
alternative to the full Phase 1-3 automation for operators who want to
drive the installer manually.

It pre-populates every value our automation hardcodes and prompts for
the site-specific pieces item by item. The first question is always the
target VCF version, because the spec shape differs between 9.0 and 9.1.

## Self-contained

Standard library only — no `vcf-installer` SDK, no boto3, no Terraform.
Nothing outside this directory is imported. Copy the `spec-generator/`
folder anywhere with Python 3.14 and it runs.

Because it doesn't use the SDK's serializer, the JSON field names are
mirrored by hand from the SDDC spec schema (same wire format the full
automation produces). If VMware changes the schema, this builder has to
be updated to match — it won't catch drift the way the SDK-typed Phase 2
builder does.

## Usage

```bash
cd spec-generator
python sddc_spec_builder.py
# or choose the output path:
python sddc_spec_builder.py --out /tmp/my_sddc_spec.json
```

Answer each prompt. Values shown in `[brackets]` are defaults — press
Enter to accept. Ctrl-C bails out without writing anything.

Output defaults to `sddc_spec_<version>.json` in the current directory.

## What gets prompted vs. hardcoded

**Prompted (site-specific):**
- VCF version (9.0 / 9.1) — asked first
- Exact installer product version (e.g. `9.0.2.0`)
- Management domain / DNS suffix
- Naming prefix (drives `<prefix>-cl01` / `-dc01` / `-dvs01`)
- Simple vs. HA deployment
- EVS host EC2 instance type (drives EVC mode)
- DNS + NTP servers
- Appliance short hostnames (each defaulted)
- Per-VLAN CIDRs (gateway + IP-pool ranges are derived)
- ESXi host count, hostnames, root passwords, optional SSL thumbprints
- All appliance passwords (single shared password optional)

**Hardcoded (accept-with-Enter or fixed):** VLAN IDs, port group names,
MTUs, teaming policies, vSAN settings (ESA on, FTT 1), DVS layout,
EVC-mode lookup, vCenter SSO username/domain, appliance sizes, NTP
default, transport zone name, `skipEsxThumbprintValidation`,
`deployWithoutLicenseKeys`, `useExistingDeployment`. See `constants.py`.

## 9.0 vs 9.1

The only structural delta the builder branches on:

- **9.0** includes `vcfOperationsFleetManagementSpec` (Fleet Manager
  hostname + root/admin passwords) and prompts for the Fleet Manager
  hostname + 2 extra passwords.
- **9.1** omits it.

Both attach `deployWithoutLicenseKeys: true`.

## Deployment shape (simple vs. HA)

- **Simple** (default): 1 NSX Manager, 1 VCF Operations node (master).
- **HA**: 3 NSX Managers, 3 VCF Operations nodes (master/data/replica),
  and 2 extra password prompts.

## Password / role counts

| Version | Shape | Appliance passwords prompted |
|---------|-------|------------------------------|
| 9.0 | simple | 13 |
| 9.0 | HA | 15 |
| 9.1 | simple | 11 |
| 9.1 | HA | 13 |

(Edge appliance passwords are **not** collected — NSX edges are a
separate deployment, not part of the SDDC bringup spec.)

## Password validation

Every appliance password you enter is validated against that
appliance's complexity rules before it's accepted; a bad password is
rejected with the specific reason(s) and re-prompted. Rules live in
`password_rules.py` and mirror the automation's password *generator*
(`orchestrator/evs_environment/vcf_password_provisioner.py`).

**Shared rules (all appliances):**
- 15-20 characters
- all four character classes: lowercase, uppercase, digit, special
- at least 4 distinct characters
- no run of 3+ sequential characters (`abc`, `321`)
- no spaces

**Allowed special characters differ by appliance** — this is the part
that genuinely varies:

| Appliance | Allowed specials |
|-----------|------------------|
| NSX (admin/root/audit) | `@!#$%?^` |
| vCenter SSO | `@!#$%?^` |
| vCenter root | any (no documented restriction) |
| VCF Operations / Cloud Proxy | `!@#$%^&*+` |
| SDDC Manager | `!%@$^#?*` |
| Fleet Manager (9.0) | `!@#$^` (undocumented → safe intersection) |

The prompt prints the exact rule for each role before you type. If you
choose the **one shared password** option, it's validated against the
cross-appliance intersection (`!@#$^`, 15-20 chars) so the single value
is guaranteed acceptable everywhere.

Note: vowels are allowed. The automation's generator avoids them (so its
random output never spells words), but that's a generation nicety, not
an appliance requirement — the builder won't reject a vowel you type.

The 15-20 length window is the cross-appliance intersection: vCenter
caps at 20 and SDDC Manager floors at 15. Those are the only two
documented length bounds, so a single window covers every role.

## Security warning

The generated JSON contains all appliance and ESXi passwords in
**plaintext** — that's what the installer UI expects on paste. The tool
prints a reminder at the end. Keep the file out of version control,
delete it once you've pasted the spec, and don't share it. The repo
`.gitignore` already excludes `spec-generator/*.json` so a generated spec
won't be committed by accident.

## Files

| File | Purpose |
|------|---------|
| `sddc_spec_builder.py` | Interactive CLI — prompt orchestration, writes the JSON |
| `builder.py` | Assembles the spec dict from collected answers (version-aware) |
| `constants.py` | Every hardcoded value, mirrored from the Phase 2 automation |
| `password_rules.py` | Per-appliance password complexity validation |
| `prompts.py` | Input helpers (defaults, validation, secure password entry) |
| `cidr.py` | Gateway + IP-pool-range derivation from a CIDR |
