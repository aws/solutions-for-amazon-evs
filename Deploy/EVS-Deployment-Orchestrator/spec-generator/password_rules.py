"""Per-role VCF appliance password validation.

Mirrors the complexity rules the automation's password *generator* uses
(``orchestrator/evs_environment/vcf_password_provisioner.py``) — but here
we validate operator-entered passwords instead of generating them.

Two kinds of rule:

1. **Shared complexity** — same for every appliance. Length 15-20, all
   four character classes (lower/upper/digit/special), >=4 unique
   characters, no run of 3+ sequential characters (``abc``, ``321``),
   no adjacent repeated characters (``aa``, ``11``), and no known
   keyboard/common pattern (``qwerty``, ``asdf``, ``123``, etc). The
   15-20 window is the intersection the automation trusts: vCenter caps
   passwords at 20, SDDC Manager floors them at 15, so 15-20 is
   guaranteed acceptable everywhere.

   The no-repetitive/no-keyboard-pattern checks are ported directly
   from ``orchestrator/evs_environment/password_validation_rule_engine.py``
   (itself a straight port of the battle-tested VCF5.x Java validator) —
   this tool previously only checked for 3+ MONOTONIC sequences
   (``abc``), missing repeated-character runs (``aa``) and known
   keyboard patterns (``asd``, ``qwerty``) entirely. Both are real
   things the VCF Installer's own validator rejects, independently of
   this tool's checks, so a password this tool accepted could still
   fail hours into a deployment with no link back to the real cause.

2. **Per-appliance allowed special characters** — this is the piece that
   genuinely differs by appliance. Transcribed from the VCF Installer
   validator error messages (see the ``_generate_complex_password``
   docstring in the provisioner):

       NSX admin/root/audit          @!#$%?^
       vCenter SSO                   @!#$%?^
       vCenter root                  (no documented restriction)
       VCF Operations / Cloud Proxy  !@#$%^&*+
       SDDC Manager                  !%@$^#?*
       VSP (9.1)                     !@#$^* (VCF_9_1_PASSWORD_RULES.md)
       intersection (all, incl. VSP) !@#$^

   ``?``, ``%``, ``*``, ``&`` and ``+`` each pass some validators and
   fail others — the cross-appliance-safe intersection must exclude
   any character not accepted by EVERY appliance. A prior version of
   this file used ``!@#$%^`` (including ``%``) as the intersection,
   which passes most validators but is NOT in VSP's allow-list on 9.1 —
   a shared password using ``%`` would be rejected specifically by VSP
   (and by extension, since VSP's systemUserPassword reuses the
   sddcManagerRoot placeholder, this also matters for sddcManagerRoot
   on any 9.1 deployment). The real intersection excluding ``%`` is
   ``!@#$^``.

Note: the generator also avoids vowels, but that's a generation nicety
(keeps random output from spelling words), NOT an appliance requirement.
We do **not** reject vowels in operator input.
"""

import string

# Shared complexity window (the intersection the automation trusts).
MIN_LENGTH = 15
MAX_LENGTH = 20
MIN_UNIQUE = 4
MIN_CLASSES = 4

# Per-appliance allowed special-character sets.
_SPECIALS_NSX = "@!#$%?^"
_SPECIALS_VCENTER_SSO = "@!#$%?^"
_SPECIALS_VCENTER_ROOT = string.punctuation  # documented as unrestricted
_SPECIALS_OPERATIONS = "!@#$%^&*+"
_SPECIALS_SDDC = "!%@$^#?*"
# VCF 9.1 VSP (VCF Services Platform) systemUserPassword. VSP reuses the
# sddcManagerRoot secret's value (see the orchestrator's
# _build_vsp_cluster_spec / this tool's _build_vsp_cluster_spec), so on
# a 9.1 deployment sddcManagerRoot's password must ALSO satisfy this set,
# not just _SPECIALS_SDDC.
_SPECIALS_VSP = "!@#$^*"

# Safe across every appliance INCLUDING VSP on 9.1. "%" is accepted by
# most validators (NSX, vCenter SSO, SDDC Manager) but is NOT in VSP's
# allow-list — since sddcManagerRoot's value is shared with VSP on 9.1,
# the true cross-appliance-safe set must exclude "%".
INTERSECTION_SPECIALS = "!@#$^"

# Role -> allowed special characters. Roles match builder.required_password_roles.
SPECIALS_BY_ROLE = {
    "vcenterRoot": _SPECIALS_VCENTER_ROOT,
    "vcenterSso": _SPECIALS_VCENTER_SSO,
    "nsxRoot": _SPECIALS_NSX,
    "nsxAdmin": _SPECIALS_NSX,
    "nsxAudit": _SPECIALS_NSX,
    # sddcManagerRoot's value is shared with VSP's systemUserPassword on
    # 9.1 (see module docstring) - intersect with VSP's allow-list so a
    # value accepted here is also accepted by VSP, instead of letting
    # sddcManagerRoot alone accept "%" or "?" and having VSP silently
    # reject the shared value hours into a 9.1 deployment.
    "sddcManagerRoot": "".join(sorted(set(_SPECIALS_SDDC) & set(_SPECIALS_VSP))),
    "sddcManagerSsh": _SPECIALS_SDDC,
    "sddcManagerLocal": _SPECIALS_SDDC,
    "operationsAdmin": _SPECIALS_OPERATIONS,
    "operationsMaster": _SPECIALS_OPERATIONS,
    "operationsData": _SPECIALS_OPERATIONS,
    "operationsReplica": _SPECIALS_OPERATIONS,
    "operationsCollector": _SPECIALS_OPERATIONS,
    # Fleet Manager isn't individually documented — fall back to the
    # cross-appliance-safe intersection.
    "fleetManagerRoot": INTERSECTION_SPECIALS,
    "fleetManagerAdmin": INTERSECTION_SPECIALS,
}


def allowed_specials_for_role(role):
    """Allowed special characters for a role (falls back to intersection)."""
    return SPECIALS_BY_ROLE.get(role, INTERSECTION_SPECIALS)


def _char_classes(password):
    classes = 0
    if any(c.islower() for c in password):
        classes += 1
    if any(c.isupper() for c in password):
        classes += 1
    if any(c.isdigit() for c in password):
        classes += 1
    if any((not c.isalnum()) and (not c.isspace()) for c in password):
        classes += 1
    return classes


def _has_sequence(password):
    """True if 3+ ascending/descending consecutive chars by codepoint."""
    if len(password) < 3:
        return False
    for i in range(len(password) - 2):
        a, b, c = password[i], password[i + 1], password[i + 2]
        if ord(b) - ord(a) == 1 and ord(c) - ord(b) == 1:
            return True
        if ord(a) - ord(b) == 1 and ord(b) - ord(c) == 1:
            return True
    return False


def _has_repetitive_chars(password):
    """True if any two adjacent characters are the same (case-insensitive).

    Ported from orchestrator/evs_environment/
    password_validation_rule_engine.py's has_repetitive_chars — a
    check the VCF Installer's own validator enforces that this tool
    previously had no equivalent for (e.g. "aabb", "11223").
    """
    lower = password.lower()
    return any(lower[i] == lower[i + 1] for i in range(len(lower) - 1))


# Keyboard/common patterns, verbatim from orchestrator/evs_environment/
# password_validation_rule_engine.py's _KEYBOARD_PATTERNS (itself a port
# of the battle-tested VCF 5.x Java validator's pattern list).
_KEYBOARD_PATTERNS = (
    "qwerty", "asdfgh", "zxcvbn", "qwertz", "azerty",
    "yuiop", "hjkl", "nm",
    "qwe", "asd", "zxc", "rty", "fgh", "vbn",
    "ewq", "dsa", "cxz", "ytr", "hgf", "bnv",
    "123456", "654321", "789456", "456789", "147258", "258147",
    "123", "456", "789", "321", "654", "987",
    "qaz", "wsx", "edc", "rfv", "tgb", "yhn", "ujm",
    "zaq", "xsw", "cde", "vfr", "bgt", "nhy", "mju",
    "abc", "xyz", "qwer", "asdf", "zxcv",
    "aaa", "sss", "ddd", "fff", "ggg", "hhh", "jjj", "kkk", "lll",
    "111", "222", "333", "444", "555", "666", "777", "888", "999", "000",
)


def _has_keyboard_pattern(password):
    """True if the password contains any known keyboard/common pattern.

    Ported from password_validation_rule_engine.py's
    has_keyboard_patterns — the VCF Installer's validator rejects these
    (e.g. "asd", "qwerty") independently of the sequential-character
    check above, which only catches strictly monotonic runs and misses
    non-monotonic keyboard-adjacency patterns like "asd" (a->s is +18,
    s->d is +1 — not a monotonic codepoint run).
    """
    lower = password.lower()
    return any(pattern in lower for pattern in _KEYBOARD_PATTERNS)


def _has_too_many_continuous(password):
    """Mirror the orchestrator engine's NO_CONTINUOUS rule: reject when more
    than 3 adjacent character pairs differ by exactly 1 codepoint
    (case-insensitive). e.g. 'abcd' -> 3 (ok), '12321' -> 4 (rejected).
    Ported verbatim from password_validation_rule_engine.
    has_too_many_continuous_chars so this tool matches what the VCF
    installer enforces."""
    lower = password.lower()
    count = 0
    for i in range(len(lower) - 1):
        if abs(ord(lower[i]) - ord(lower[i + 1])) == 1:
            count += 1
    return count > 3


def validate(password, allowed_specials):
    """Validate a password against the shared rules + an allowed special set.

    Returns a list of human-readable failure reasons. Empty list == valid.
    """
    errors = []

    if not (MIN_LENGTH <= len(password) <= MAX_LENGTH):
        errors.append(
            f"must be {MIN_LENGTH}-{MAX_LENGTH} characters (got {len(password)})"
        )

    if any(c.isspace() for c in password):
        errors.append("must not contain spaces")

    if len(set(password)) < MIN_UNIQUE:
        errors.append(f"must use at least {MIN_UNIQUE} distinct characters")

    if _char_classes(password) < MIN_CLASSES:
        errors.append(
            "must include all four: lowercase, uppercase, digit, and special"
        )

    specials_in = [c for c in password if (not c.isalnum()) and (not c.isspace())]
    if not specials_in:
        errors.append(f"must include a special character from: {allowed_specials}")
    else:
        bad = sorted({c for c in specials_in if c not in allowed_specials})
        if bad:
            errors.append(
                f"contains disallowed special character(s) {''.join(bad)} — "
                f"allowed for this appliance: {allowed_specials}"
            )

    if _has_sequence(password):
        errors.append(
            "must not contain 3+ sequential characters (e.g. abc, 321)"
        )

    if _has_too_many_continuous(password):
        errors.append(
            "must not contain too many near-sequential characters "
            "(more than 3 adjacent +/-1 pairs, e.g. abcd, 12321)"
        )

    if _has_repetitive_chars(password):
        errors.append(
            "must not contain adjacent repeated characters (e.g. aa, 11)"
        )

    if _has_keyboard_pattern(password):
        errors.append(
            "must not contain a keyboard or common pattern (e.g. qwerty, asd, 123)"
        )

    return errors


def rule_hint(allowed_specials):
    """A one-line human summary of the rules for display before a prompt."""
    if allowed_specials == string.punctuation:
        specials_display = "any"
    else:
        specials_display = allowed_specials
    return (
        f"{MIN_LENGTH}-{MAX_LENGTH} chars, upper+lower+digit+special, "
        f">={MIN_UNIQUE} unique, no 3+ sequences; "
        f"specials: {specials_display}"
    )
