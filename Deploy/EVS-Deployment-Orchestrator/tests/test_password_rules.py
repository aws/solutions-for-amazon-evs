"""Unit tests for spec-generator/password_rules.py.

The high-value cases: the cross-appliance INTERSECTION_SPECIALS
regression (``%`` passes most validators but not VSP on 9.1), and the
rule-engine ports (repeats, keyboard patterns, sequences) that a
previous version of the tool missed entirely.
"""

from spec_generator.password_rules import (
    INTERSECTION_SPECIALS,
    SPECIALS_BY_ROLE,
    allowed_specials_for_role,
    validate,
)

# A password that satisfies every shared rule, using only intersection
# specials. 16 chars, 4 classes, no repeats/sequences/keyboard patterns.
VALID = "Kw4!Pz7@Tm2#Xr9$"


class TestValidAccepted:
    def test_valid_password_has_no_errors(self):
        assert validate(VALID, INTERSECTION_SPECIALS) == []


class TestSharedRules:
    def test_too_short(self):
        errors = validate("Kw4!Pz7@", INTERSECTION_SPECIALS)
        assert any("15-20 characters" in e for e in errors)

    def test_too_long(self):
        errors = validate(VALID + "Qq5^W", INTERSECTION_SPECIALS)
        assert any("15-20 characters" in e for e in errors)

    def test_spaces_rejected(self):
        errors = validate("Kw4! Pz7@Tm2#Xr9", INTERSECTION_SPECIALS)
        assert any("spaces" in e for e in errors)

    def test_missing_character_class(self):
        # No digits.
        errors = validate("Kwl!Pzq@Tmy#Xrw$", INTERSECTION_SPECIALS)
        assert any("all four" in e for e in errors)

    def test_sequential_characters_rejected(self):
        errors = validate("Kwstu!7@Tm2#Xr9$", INTERSECTION_SPECIALS)
        assert any("sequential" in e for e in errors)

    def test_adjacent_repeat_rejected(self):
        errors = validate("Kww4!Pz7@Tm2#Xr9", INTERSECTION_SPECIALS)
        assert any("adjacent repeated" in e for e in errors)

    def test_adjacent_repeat_is_case_insensitive(self):
        errors = validate("KkW4!Pz7@Tm2#Xr9", INTERSECTION_SPECIALS)
        assert any("adjacent repeated" in e for e in errors)

    def test_keyboard_pattern_rejected(self):
        errors = validate("Xk4!qwerty7@Tm2#", INTERSECTION_SPECIALS)
        assert any("keyboard" in e for e in errors)

    def test_missing_special_rejected(self):
        errors = validate("Kw4rPz7sTm2vXr9d", INTERSECTION_SPECIALS)
        assert any("special character" in e for e in errors)


class TestSpecialCharacterSets:
    def test_percent_not_in_intersection(self):
        """Regression: % passes NSX/SSO/SDDC but NOT VSP on 9.1 — the
        cross-appliance intersection must exclude it."""
        assert "%" not in INTERSECTION_SPECIALS

    def test_percent_rejected_against_intersection(self):
        errors = validate("Kw4%Pz7@Tm2#Xr9$", INTERSECTION_SPECIALS)
        assert any("disallowed special" in e for e in errors)

    def test_percent_allowed_for_nsx(self):
        nsx = allowed_specials_for_role("nsxAdmin")
        assert "%" in nsx
        assert validate("Kw4%Pz7@Tm2#Xr9$", nsx) == []

    def test_unknown_role_falls_back_to_intersection(self):
        assert allowed_specials_for_role("noSuchRole") == INTERSECTION_SPECIALS

    def test_every_role_set_contains_the_intersection_or_is_documented(self):
        """Every per-role set should accept at least one intersection char,
        so a shared intersection-only password can pass everywhere."""
        for role, specials in SPECIALS_BY_ROLE.items():
            assert any(c in specials for c in INTERSECTION_SPECIALS), role
