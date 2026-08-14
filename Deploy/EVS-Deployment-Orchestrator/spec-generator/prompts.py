"""Interactive input helpers for the SDDC spec builder CLI.

Small, dependency-free wrappers around ``input()`` / ``getpass`` that
handle defaults, validation, and re-prompting. Every helper loops until
it gets an acceptable value so the caller never has to validate.
"""

import getpass
import ipaddress


def prompt_str(question, default=None, required=True):
    """Prompt for a free-text string.

    Args:
        question: The prompt text (no trailing colon needed).
        default: Value returned when the operator hits Enter. When set,
            the prompt shows it in brackets.
        required: When True and no default is given, an empty answer
            re-prompts instead of returning "".
    """
    suffix = f" [{default}]" if default is not None else ""
    while True:
        answer = input(f"{question}{suffix}: ").strip()
        if answer:
            return answer
        if default is not None:
            return default
        if not required:
            return ""
        print("  A value is required.")


def prompt_choice(question, choices, default=None):
    """Prompt for one of a fixed set of choices (case-insensitive)."""
    choices_display = "/".join(choices)
    suffix = f" [{default}]" if default is not None else ""
    lowered = {c.lower(): c for c in choices}
    while True:
        answer = input(f"{question} ({choices_display}){suffix}: ").strip()
        if not answer and default is not None:
            # Resolve the default through the choices map so an out-of-choices
            # default can never be returned — re-prompt instead of crashing
            # a caller that expects a valid choice.
            if default.lower() in lowered:
                return lowered[default.lower()]
            print(f"  Please choose one of: {choices_display}")
            continue
        if answer.lower() in lowered:
            return lowered[answer.lower()]
        print(f"  Please choose one of: {choices_display}")


def prompt_bool(question, default=True):
    """Prompt for a yes/no answer, returning a bool."""
    default_str = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{question} [{default_str}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def prompt_int(question, default=None, minimum=None, maximum=None):
    """Prompt for an integer with optional bounds."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        answer = input(f"{question}{suffix}: ").strip()
        if not answer and default is not None:
            return default
        try:
            value = int(answer)
        except ValueError:
            print("  Please enter a whole number.")
            continue
        if minimum is not None and value < minimum:
            print(f"  Must be >= {minimum}.")
            continue
        if maximum is not None and value > maximum:
            print(f"  Must be <= {maximum}.")
            continue
        return value


def prompt_cidr(question, default=None):
    """Prompt for a valid IPv4 CIDR (e.g. 10.0.20.0/24).

    Requires IPv4 (this tool's whole spec shape assumes IPv4 VLANs — an
    IPv6 CIDR was previously silently accepted and would produce
    nonsensical downstream values from cidr.py's IPv4-oriented host-
    offset math) and a prefix length of /27 or shorter. Below /27,
    cidr.tenth_host()/sixth_from_end() (network+10 / broadcast-5) can
    produce an INVERTED or out-of-subnet range: e.g. a /29 has only 8
    addresses, so network+10 lands past the broadcast address entirely,
    and a /28 makes start==end. The resulting spec would silently carry
    a broken IP-pool range that the installer rejects with a cryptic
    error, with no link back to "the CIDR you entered was too small."
    """
    suffix = f" [{default}]" if default is not None else ""
    min_prefix_len = 27
    while True:
        answer = input(f"{question}{suffix}: ").strip()
        if not answer and default is not None:
            answer = default
        try:
            net = ipaddress.ip_network(answer, strict=False)
        except ValueError:
            print("  Not a valid CIDR (expected something like 10.0.20.0/24).")
            continue
        if net.version != 4:
            print("  Must be an IPv4 CIDR (this tool doesn't support IPv6 VLANs).")
            continue
        if net.prefixlen > min_prefix_len:
            print(
                f"  /{net.prefixlen} is too small — need at least a "
                f"/{min_prefix_len} for the derived gateway + IP-pool "
                f"range to fit inside the subnet."
            )
            continue
        # Return the canonical network form (e.g. "10.0.20.0/24"), not
        # the raw operator input — a prior version returned the answer
        # verbatim, so a host-bit-set input like "10.0.20.5/24" was
        # stored as the "subnet" while gateway/pool math (via
        # strict=False) derived from the ACTUAL network 10.0.20.0/24,
        # producing an internally inconsistent spec.
        return str(net)


def prompt_ip(question, default=None, required=True):
    """Prompt for a single IPv4 address."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        answer = input(f"{question}{suffix}: ").strip()
        if not answer and default is not None:
            return default
        if not answer and not required:
            return ""
        try:
            ipaddress.ip_address(answer)
            return answer
        except ValueError:
            print("  Not a valid IPv4 address.")


def prompt_password(
    question,
    allow_reuse_value=None,
    allow_reuse_label=None,
    validator=None,
):
    """Prompt for a password without echoing.

    Re-prompts once for confirmation. When ``allow_reuse_value`` is set,
    an empty answer reuses that value (used for the "same password for
    all roles" shortcut).

    When ``validator`` is given (a callable taking the password and
    returning a list of failure-reason strings), the entry is checked
    before the confirmation step; a non-empty result prints each reason
    and re-prompts.
    """
    while True:
        hint = ""
        if allow_reuse_value is not None:
            hint = f" (Enter to reuse {allow_reuse_label})"
        first = getpass.getpass(f"{question}{hint}: ")
        if not first and allow_reuse_value is not None:
            return allow_reuse_value
        if not first:
            print("  A value is required.")
            continue
        if validator is not None:
            errors = validator(first)
            if errors:
                print("  Password doesn't meet this appliance's requirements:")
                for reason in errors:
                    print(f"    - {reason}")
                continue
        second = getpass.getpass("  Confirm: ")
        if first != second:
            print("  Passwords didn't match, try again.")
            continue
        return first


def section(title):
    """Print a section header to visually group prompts."""
    print()
    print(f"=== {title} ===")
