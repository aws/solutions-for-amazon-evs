# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Serialize / deserialize VCF SDK typed objects to/from JSON.

Thin wrappers over the SDK's own serializers so the rest of the code has a
single clean entry point. Both phases use these helpers — Phase 2 to write
the human-readable spec files, Phase 3 to read them back into typed objects
before calling ``deploy_sddc``.

Why use the SDK's serializer rather than ``json.dumps``?
--------------------------------------------------------

The VCF wire format has a few subtleties (camelCase naming, how unknown
fields are serialized, how optionals are stripped from the output). The
SDK's own ``DataValueConverter`` produces exactly the bytes the installer
expects. Round-tripping through it keeps us honest.

REST mode
---------

The installer exposes a classic REST API (not JSON-RPC), so we use the
``SWAGGER_REST`` converter mode throughout. This makes optionals
transparent (the wire JSON either contains the field or omits it) instead
of wrapping them in an ``{ "type": "optional", "value": ... }`` envelope.
"""

import json
from typing import Any, TypeVar

from vmware.vapi.bindings.converter import RestConverter, TypeConverter
from vmware.vapi.bindings.struct import VapiStruct
from vmware.vapi.data.serializers.rest import DataValueConverter

T = TypeVar("T", bound=VapiStruct)

# The installer is a REST API, so use the Swagger-style REST converter mode
# throughout (no JSON-RPC optional wrappers on the wire).
_REST_MODE = RestConverter.SWAGGER_REST


def typed_to_json(obj: VapiStruct, *, indent: int | None = 2) -> str:
    """Convert a typed SDK object to a JSON string (pretty by default).

    Args:
        obj: Any ``VapiStruct`` subclass (e.g., ``SddcSpec``).
        indent: JSON indent level; pass ``None`` for compact output.
    """
    binding_type = type(obj).get_binding_type()
    data_value = TypeConverter.convert_to_vapi(
        obj, binding_type, rest_converter_mode=_REST_MODE
    )
    compact = DataValueConverter.convert_to_json(data_value)

    if indent is None:
        return compact
    # Round-trip through Python's json module for pretty-printing. Python
    # dict insertion order is preserved.
    return json.dumps(json.loads(compact), indent=indent)


def json_to_typed(json_text: str, typed_class: type[T]) -> T:
    """Convert wire-format JSON to the given typed SDK class.

    Args:
        json_text: Wire-format JSON (as written by ``typed_to_json``).
        typed_class: The ``VapiStruct`` subclass to deserialize into.
    """
    binding_type = typed_class.get_binding_type()
    data_value = DataValueConverter.convert_to_data_value(json_text)
    return TypeConverter.convert_to_python(
        data_value, binding_type, rest_converter_mode=_REST_MODE
    )


def wire_dict(obj: VapiStruct) -> dict[str, Any]:
    """Convert a typed SDK object to its wire-shape dict (for diffing/asserts)."""
    return json.loads(typed_to_json(obj, indent=None))
