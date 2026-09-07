"""Small dependency-free JSON Schema builders and input checks for MCP."""
from __future__ import annotations

import inspect
import math
import re
import types
import typing
from collections import abc
from pathlib import Path


def object_schema(properties=None, required=(), *, additional=False):
    return {'type': 'object', 'properties': dict(properties or {}),
            'required': list(required), 'additionalProperties': additional}


STRING = {'type': 'string'}
BOOLEAN = {'type': 'boolean'}
INTEGER = {'type': 'integer'}
NUMBER = {'type': 'number'}
HANDLE = {'anyOf': [
    {'type': 'string', 'pattern': '^h_[0-9a-f]+$'},
    object_schema({'$automation_bridge': {'const': 'handle'}, '$handle': STRING,
                   '$type': STRING, 'value': {}}, ('$automation_bridge', '$handle', '$type')),
]}
ELEMENT = object_schema({'$automation_bridge': {'const': 'element'},
                         '$type': {'const': 'automation_bridge.elements.Element'},
                         'raw': {'type': 'object'}, 'value': {'type': 'object'}},
                        ('$automation_bridge', '$type', 'raw'))
SCREENSHOT_RECEIPT = object_schema({
    'raw': object_schema({'path': STRING}, ('path',), additional=True),
    'capture_id': INTEGER, 'state': STRING, 'path': STRING, 'frame': INTEGER,
    'scene_sequence': INTEGER, 'width': INTEGER, 'height': INTEGER,
    'sha256': {'anyOf': [STRING, {'type': 'null'}]},
    'failure_reason': {'anyOf': [STRING, {'type': 'null'}]},
}, ('raw',))
BYTES = object_schema({'$automation_bridge': {'const': 'bytes'}, '$type': {'const': 'bytes'},
                       'encoding': {'const': 'base64'}, 'data': STRING},
                      ('$automation_bridge', '$type', 'encoding', 'data'))
SELECTOR = object_schema({
    **{key: STRING for key in (
        'id', 'instance_id', 'logical_id', 'type', 'type_exact', 'name', 'name_exact',
        'text', 'text_exact', 'url', 'url_exact', 'path', 'kind', 'automation_id', 'localization_key', 'role')},
    **{key: BOOLEAN for key in ('enabled', 'has_bounds', 'visible_and_enabled', 'visible', 'case_sensitive')},
    'include': {'anyOf': [STRING, {'type': 'array', 'items': STRING}]},
    'limit': {'type': 'integer', 'minimum': 0, 'maximum': 500},
    'offset': {'type': 'integer', 'minimum': 0, 'maximum': 4294967295},
    'cursor': {'type': 'string', 'pattern': '^[0-9]+$'},
})
POINT = {'anyOf': [object_schema({'x': NUMBER, 'y': NUMBER}, ('x', 'y')),
                   {'type': 'array', 'items': NUMBER, 'minItems': 2, 'maxItems': 2}]}
TARGET = {'anyOf': [STRING, NUMBER, ELEMENT, POINT]}
WAIT = {'anyOf': [BOOLEAN, {'type': 'string', 'enum': ['accepted', 'started', 'released']},
                  {'type': 'number', 'minimum': 0}, {'type': 'null'}]}


def validate(value, schema, path='$'):
    """Validate the subset we advertise; errors identify the failing field."""
    if not schema:
        return
    if 'anyOf' in schema:
        for candidate in schema['anyOf']:
            try:
                validate(value, candidate, path)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f'{path} does not match an allowed shape')
    kind = schema.get('type')
    matches = {
        'object': isinstance(value, dict), 'array': isinstance(value, list),
        'string': isinstance(value, str), 'boolean': isinstance(value, bool),
        'integer': isinstance(value, int) and not isinstance(value, bool),
        'number': isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
        'null': value is None,
    }
    if kind is not None and not matches.get(kind, False):
        raise ValueError(f'{path} must be {kind}')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path} must be one of {schema["enum"]}')
    if 'const' in schema and value != schema['const']:
        raise ValueError(f'{path} must equal {schema["const"]!r}')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        missing = set(schema.get('required', ())) - set(value)
        if missing:
            raise ValueError(f'{path} is missing {sorted(missing)}')
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], f'{path}.{key}')
            elif schema.get('additionalProperties') is False:
                raise ValueError(f'{path}.{key} is not an accepted field')
            elif isinstance(schema.get('additionalProperties'), dict):
                validate(item, schema['additionalProperties'], f'{path}.{key}')
    elif isinstance(value, list):
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', math.inf):
            raise ValueError(f'{path} has an invalid number of items')
        for index, item in enumerate(value):
            validate(item, schema.get('items', {}), f'{path}[{index}]')
    elif isinstance(value, str):
        if 'pattern' in schema and re.search(schema['pattern'], value) is None:
            raise ValueError(f'{path} has an invalid format')
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            raise ValueError(f'{path} is outside the allowed range')


def annotation_schema(annotation):
    if annotation is inspect.Signature.empty or annotation is typing.Any:
        return {}
    if annotation is None or annotation is type(None):
        return {'type': 'null'}
    if annotation in (str, Path):
        return STRING
    if annotation is bytes:
        return BYTES
    if annotation in (bool, int, float):
        return {bool: BOOLEAN, int: INTEGER, float: NUMBER}[annotation]
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        return {'anyOf': [annotation_schema(arg) for arg in args]}
    if origin is typing.Literal:
        return {'enum': list(args)}
    if origin in (list, tuple, set, frozenset, abc.Sequence, abc.Iterable):
        result = {'type': 'array', 'items': annotation_schema(args[0]) if args else {}}
        if origin is tuple and args and Ellipsis not in args:
            result.update(minItems=len(args), maxItems=len(args))
        return result
    if origin in (dict, abc.Mapping) or annotation is dict:
        return {'type': 'object', 'additionalProperties': annotation_schema(args[1]) if len(args) == 2 else True}
    if getattr(annotation, '__name__', None) == 'Element':
        return ELEMENT
    if getattr(annotation, '__name__', None) == 'ScreenshotReceipt':
        return SCREENSHOT_RECEIPT
    if inspect.isclass(annotation) and annotation.__module__.startswith('automation_bridge'):
        return HANDLE
    return {}


def operation_arguments(spec):
    """Describe JSON inputs, including the explicit adaptations in the registry."""
    if spec.kind == 'property' or spec.adapter == 'mcp_request_cancellation':
        return object_schema()
    function = getattr(spec.owner, spec.member)
    signature = inspect.signature(function)
    try:
        hints = typing.get_type_hints(function)
    except (NameError, TypeError):
        hints = {}
    properties, required, additional = {}, [], False
    for name, parameter in signature.parameters.items():
        if name in ('self', 'cls') or name in spec.unsupported_parameters:
            continue
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            if name == 'selector':
                properties.update(SELECTOR['properties'])
            else:
                additional = True
            continue
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            properties[name] = {'type': 'array', 'items': STRING}
            continue
        properties[name] = annotation_schema(hints.get(name, parameter.annotation))
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
        elif parameter.default is None or isinstance(parameter.default, (str, bool, int, float)):
            properties[name] = {**properties[name], 'default': parameter.default}
    if spec.adapter == 'capabilities_array_to_varargs':
        properties['capabilities'] = {'type': 'array', 'items': STRING}
    if spec.adapter in ('cause_text_to_RuntimeError', 'error_text_to_RuntimeError'):
        name = 'cause' if spec.adapter == 'cause_text_to_RuntimeError' else 'error'
        properties[name] = STRING
    if spec.adapter == 'decimal_string_keys_to_int':
        properties['names'] = {'type': 'object', 'additionalProperties': STRING, 'description': 'Decimal integer keys'}
    return object_schema(properties, required, additional=additional)
