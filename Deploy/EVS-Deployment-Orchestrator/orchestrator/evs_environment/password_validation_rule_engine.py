# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A password validation rule engine implementing Broadcom's password
complexity standards.

A self-contained port of VMware Cloud Foundation's password complexity
rules — no external dependencies, kept behaviorally identical rule-for-rule
so upstream fixes port the same way.
Rules follow Broadcom's password complexity standard:
https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-5-2-and-earlier/5-2/vmware-cloud-foundation-operations-5-2/password-policy-configuration-operations/configuring-password-complexity-policies-for-vmware-cloud-foundation-operations.html

Replaces this project's previous ad hoc 4-check validator
(``vcf_password_provisioner._is_complex``/``_has_sequence``) with the full
12-rule engine, toggleable the same way the Java version is.
"""

from dataclasses import dataclass
from enum import Enum, auto


class PasswordValidationRule(Enum):
    """One password complexity rule. Mirrors the Java enum member-for-member."""

    LENGTH = auto()
    NO_REPETITIVE = auto()
    NO_CONTINUOUS = auto()
    UPPERCASE = auto()
    LOWERCASE = auto()
    NO_VOWELS = auto()
    SPECIAL = auto()
    NUMBER = auto()
    NO_KEYBOARD_PATTERNS = auto()
    MIN_UNIQUE = auto()
    MIN_CLASS = auto()
    MAX_SEQUENCE = auto()


@dataclass(frozen=True)
class PasswordPolicyConfig:
    """Numeric thresholds for password policy validation.

    Maps to VCF/pam_pwquality policy fields. Defaults match the Java
    version's ``@Builder.Default`` values.
    """

    min_length: int = 17
    # Minimum number of distinct characters required in the password.
    min_unique: int = 4
    # Minimum number of character classes (lowercase, uppercase, digit, special).
    min_class: int = 4
    # Maximum allowed length of monotonic character sequences (e.g. abc, 321).
    # 0 rejects runs of 3+ (most restrictive), 1 rejects 4+, etc. Note: unlike
    # pam_pwquality, where maxsequence=0 disables the check, here 0 is the
    # most restrictive setting.
    max_sequence: int = 0


# Keyboard/common patterns checked by NO_KEYBOARD_PATTERNS. Verbatim port of
# the Java string array.
_KEYBOARD_PATTERNS: tuple[str, ...] = (
    # QWERTY patterns
    "qwerty", "asdfgh", "zxcvbn", "qwertz", "azerty",
    "yuiop", "hjkl", "nm",
    "qwe", "asd", "zxc", "rty", "fgh", "vbn",
    "ewq", "dsa", "cxz", "ytr", "hgf", "bnv",
    # Numeric patterns
    "123456", "654321", "789456", "456789", "147258", "258147",
    "123", "456", "789", "321", "654", "987",
    # Diagonal patterns
    "qaz", "wsx", "edc", "rfv", "tgb", "yhn", "ujm",
    "zaq", "xsw", "cde", "vfr", "bgt", "nhy", "mju",
    # Common words or patterns
    "abc", "xyz", "qwer", "asdf", "zxcv",
    # Repeated characters
    "aaa", "sss", "ddd", "fff", "ggg", "hhh", "jjj", "kkk", "lll",
    "111", "222", "333", "444", "555", "666", "777", "888", "999", "000",
)

_SPECIAL_CHARS = set("!@#$%^&*()_+-=[]{};':\"\\|,.<>/?")
_VOWELS = set("aeiouAEIOU")


def has_repetitive_chars(password: str) -> bool:
    """True if any two adjacent characters are the same (case-insensitive).

    e.g. patterns: "aabb", "11223", "$$##"
    """
    lower = password.lower()
    for i in range(len(lower) - 1):
        if lower[i] == lower[i + 1]:
            return True
    return False


def has_too_many_continuous_chars(password: str) -> bool:
    """True if the password contains too many continuous character runs.

    "Continuous" means any char that is +1/-1 (ASCII or alphabet,
    case-insensitive) from its immediately preceding char.
    e.g. "abcd" -> 3 continuous chars ('b','c','d')
    e.g. "12321" -> 4 continuous chars ('2','3','2','1'), even though not
    monotonically increasing/decreasing overall.
    e.g. "aB1Ef2Nm" -> 3 continuous chars ('B','f','m'), even split across
    separate segments and case.

    Threshold is 4 continuous chars (i.e. count > 3), from an experiment
    on CloudBuilder for a 17-char string — ported verbatim from Java.
    """
    lower = password.lower()
    continuous_count = 0
    for i in range(len(lower) - 1):
        a, b = lower[i], lower[i + 1]
        if ord(a) + 1 == ord(b) or ord(a) - 1 == ord(b):
            continuous_count += 1
    return continuous_count > 3


def contains_vowels(password: str) -> bool:
    """True if the password contains any vowel (case-insensitive)."""
    return any(c in _VOWELS for c in password)


def count_unique_chars(password: str) -> int:
    """Number of distinct characters. Maps to pam_pwquality's minUnique/difok."""
    return len(set(password))


def count_character_classes(password: str) -> int:
    """Number of character classes present (0-4): lowercase, uppercase, digit, special."""
    classes = 0
    if any(c.islower() for c in password):
        classes += 1
    if any(c.isupper() for c in password):
        classes += 1
    if any(c.isdigit() for c in password):
        classes += 1
    if any(not c.isalnum() for c in password):
        classes += 1
    return classes


def has_monotonic_sequence(password: str, max_sequence: int) -> bool:
    """True if the password contains a monotonic run exceeding ``max_sequence``.

    When max_sequence is 0, any run of 3+ monotonically sequential
    characters is rejected (e.g. "abc", "321", "cba"). When max_sequence is
    1, runs of 4+ are rejected, etc.

    Note: this does NOT match pam_pwquality semantics where maxsequence=0
    disables the check.

    A sequence is monotonic if every consecutive pair moves in the same
    direction (all +1 or all -1), evaluated case-insensitively.
    """
    if not password:
        return False

    forbidden_run_length = max_sequence + 3
    length = len(password)
    if length < forbidden_run_length:
        return False

    run_length = 1
    prev_diff = 0

    for i in range(1, length):
        curr = ord(password[i].lower())
        prev = ord(password[i - 1].lower())
        diff = curr - prev

        if diff in (1, -1) and diff == prev_diff:
            run_length += 1
            if run_length >= forbidden_run_length:
                return True
        else:
            run_length = 2 if diff in (1, -1) else 1
            prev_diff = diff

    return False


def has_keyboard_patterns(password: str) -> bool:
    """True if the password contains any known keyboard/common pattern."""
    lower = password.lower()
    return any(pattern in lower for pattern in _KEYBOARD_PATTERNS)


def get_all_password_validation_rules() -> list[PasswordValidationRule]:
    """Return all 12 validation rules, matching the Java version's list order."""
    return [
        PasswordValidationRule.LENGTH,
        PasswordValidationRule.NO_REPETITIVE,
        PasswordValidationRule.NO_CONTINUOUS,
        PasswordValidationRule.UPPERCASE,
        PasswordValidationRule.LOWERCASE,
        PasswordValidationRule.NO_VOWELS,
        PasswordValidationRule.SPECIAL,
        PasswordValidationRule.NUMBER,
        PasswordValidationRule.NO_KEYBOARD_PATTERNS,
        PasswordValidationRule.MIN_UNIQUE,
        PasswordValidationRule.MIN_CLASS,
        PasswordValidationRule.MAX_SEQUENCE,
    ]


_RULE_MESSAGES: dict[PasswordValidationRule, str] = {
    PasswordValidationRule.NO_REPETITIVE: "Password cannot contain repetitive characters.",
    PasswordValidationRule.NO_CONTINUOUS: "Password cannot contain continuous characters.",
    PasswordValidationRule.UPPERCASE: "Password must contain at least one uppercase letter.",
    PasswordValidationRule.LOWERCASE: "Password must contain at least one lowercase letter.",
    PasswordValidationRule.NO_VOWELS: "Password cannot contain vowels.",
    PasswordValidationRule.SPECIAL: "Password must contain at least one special character.",
    PasswordValidationRule.NUMBER: "Password must contain at least one number.",
    PasswordValidationRule.NO_KEYBOARD_PATTERNS: "Password cannot contain keyboard patterns.",
}


def is_password_complex(
    password: str,
    rules: list[PasswordValidationRule],
    config: PasswordPolicyConfig | None = None,
) -> tuple[bool, list[str]]:
    """Check if ``password`` satisfies every rule in ``rules``.

    Args:
        password: The password to validate.
        rules: Which PasswordValidationRule members to check.
        config: Numeric thresholds. Defaults to PasswordPolicyConfig().

    Returns:
        (is_valid, failure_reasons) — failure_reasons is empty when
        is_valid is True. Unlike the Java version (which only logs
        failures and returns a bool), this also returns the reasons so
        callers can decide what to do with them (e.g. logging, tests)
        without re-deriving them.
    """
    if config is None:
        config = PasswordPolicyConfig()

    is_valid = True
    reasons: list[str] = []

    for rule in rules:
        if rule == PasswordValidationRule.LENGTH:
            if len(password) < config.min_length:
                reasons.append(f"Password must be at least {config.min_length} characters long.")
                is_valid = False
        elif rule == PasswordValidationRule.NO_REPETITIVE:
            if has_repetitive_chars(password):
                reasons.append(_RULE_MESSAGES[rule])
                is_valid = False
        elif rule == PasswordValidationRule.NO_CONTINUOUS:
            if has_too_many_continuous_chars(password):
                reasons.append(_RULE_MESSAGES[rule])
                is_valid = False
        elif rule == PasswordValidationRule.UPPERCASE:
            if not any(c.isupper() for c in password):
                reasons.append(_RULE_MESSAGES[rule])
                is_valid = False
        elif rule == PasswordValidationRule.LOWERCASE:
            if not any(c.islower() for c in password):
                reasons.append(_RULE_MESSAGES[rule])
                is_valid = False
        elif rule == PasswordValidationRule.NO_VOWELS:
            if contains_vowels(password):
                reasons.append(_RULE_MESSAGES[rule])
                is_valid = False
        elif rule == PasswordValidationRule.SPECIAL:
            if not any(c in _SPECIAL_CHARS for c in password):
                reasons.append(_RULE_MESSAGES[rule])
                is_valid = False
        elif rule == PasswordValidationRule.NUMBER:
            if not any(c.isdigit() for c in password):
                reasons.append(_RULE_MESSAGES[rule])
                is_valid = False
        elif rule == PasswordValidationRule.NO_KEYBOARD_PATTERNS:
            if has_keyboard_patterns(password):
                reasons.append(_RULE_MESSAGES[rule])
                is_valid = False
        elif rule == PasswordValidationRule.MIN_UNIQUE:
            if count_unique_chars(password) < config.min_unique:
                reasons.append(f"Password must contain at least {config.min_unique} unique characters.")
                is_valid = False
        elif rule == PasswordValidationRule.MIN_CLASS:
            if count_character_classes(password) < config.min_class:
                reasons.append(f"Password must contain at least {config.min_class} character classes.")
                is_valid = False
        elif rule == PasswordValidationRule.MAX_SEQUENCE:
            if has_monotonic_sequence(password, config.max_sequence):
                reasons.append(
                    f"Password contains a monotonic character sequence exceeding "
                    f"maxSequence={config.max_sequence}."
                )
                is_valid = False
        # Unknown rules are silently ignored, matching the Java version's
        # default: log.warn() branch (no logger dependency here — callers
        # control their own logging of the returned reasons).

    return is_valid, reasons
