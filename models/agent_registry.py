# -*- coding: utf-8 -*-
"""Tool registry of the DoniaLink agent connector.

A tool is a private method of ``donialink.agent.tools`` decorated with
:func:`agent_tool`. Other modules add tools by inheriting that model::

    class AgentTools(models.AbstractModel):
        _inherit = 'donialink.agent.tools'

        @agent_tool(
            name='recipe_costs',
            title='Recipe costs', title_ar='تكاليف الوصفات',
            description="Cost per portion, food cost % and margin of each recipe ...",
            params={'type': 'object', 'properties': {...}},
        )
        def _tool_recipe_costs(self, limit=50):
            return {...}

The description and the input schema are what AI models read to decide when
and how to call a tool: write them for a reader who has never seen the module.
Tools must return JSON-serializable data (dict, list, str, numbers, bool, None).

A tool can also return one file under :data:`FILE_KEY`::

    return {'rows': 12, FILE_KEY: {'name': 'Report.xlsx', 'mimetype': '...', 'content': b'...'}}

The controller stores it after the read-only rollback (so the files of read tools are
kept) and replaces the key with ``'file': {id, name, mimetype, size, download_url,
expires_at}``. Downloads use ``GET /donialink/agent/v1/files/<id>`` for 24 hours.
"""

API_VERSION = '1'
API_KEY_SCOPE = 'donialink_agent'
TOOL_ATTR = '_donialink_agent_tool'

ACCESS_READ = 'read'
ACCESS_WRITE = 'write'

FILE_KEY = '__file__'

_JSON_TYPES = {
    'string': (str,),
    'integer': (int,),
    'number': (int, float),
    'boolean': (bool,),
    'array': (list,),
    'object': (dict,),
}


class ToolError(Exception):
    """Error returned to the caller with a stable machine-readable code."""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def agent_tool(name, description, params=None, access=ACCESS_READ, title=None, title_ar=None,
               category='general', requires=()):
    """Register a method of ``donialink.agent.tools`` as an agent tool.

    :param str name: unique snake_case tool name, used in the URL and by AI models
    :param str description: what the tool returns and when to use it (English)
    :param dict params: JSON schema (type object) of the tool arguments
    :param str access: ``'read'`` (changes are rolled back) or ``'write'``
    :param str title: short English label for the control app
    :param str title_ar: short Arabic label for the control app
    :param str category: grouping in the control app (instance, sales, stock, ...)
    :param tuple requires: models the tool needs (e.g. ``('restaurant.recipe',)``), or
        ``'model:field'`` to require a field too (tells two versions of a module apart). The tool
        is hidden on databases where one is missing, so the connector does not depend on the
        modules it reads.
    """
    assert access in (ACCESS_READ, ACCESS_WRITE), access

    def decorator(func):
        setattr(func, TOOL_ATTR, {
            'name': name,
            'title': title or name.replace('_', ' ').capitalize(),
            'title_ar': title_ar or title or name,
            'description': ' '.join(description.split()),
            'input_schema': params or {'type': 'object', 'properties': {}},
            'access': access,
            'category': category,
            'requires': list(requires),
            'module': _module_of(func),
        })
        return func
    return decorator


def _module_of(func):
    parts = func.__module__.split('.')
    return parts[2] if len(parts) > 2 and parts[:2] == ['odoo', 'addons'] else func.__module__


def collect_tools(model_class):
    """Return {tool name: (method name, metadata)} for a model class.

    The metadata is looked up along the MRO, so a module overriding a tool
    method keeps it registered even without repeating the decorator.
    """
    tools = {}
    for attr in dir(model_class):
        if not attr.startswith('_tool_'):
            continue
        for klass in model_class.__mro__:
            meta = getattr(klass.__dict__.get(attr), TOOL_ATTR, None)
            if meta:
                tools[meta['name']] = (attr, meta)
                break
    return tools


def validate_params(schema, params):
    """Validate ``params`` against a small JSON-schema subset and apply defaults.

    Supported keywords: type, properties, required, enum, default, minimum,
    maximum, maxLength, items, additionalProperties (false by default).
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ToolError('invalid_params', "'params' must be a JSON object")
    properties = schema.get('properties', {})
    result = {}
    for key, value in params.items():
        if key not in properties:
            if schema.get('additionalProperties', False):
                result[key] = value
                continue
            raise ToolError('invalid_params', f"Unknown parameter '{key}'. Allowed: {sorted(properties)}")
        result[key] = _validate_value(properties[key], value, key)
    for key in schema.get('required', []):
        if key not in result:
            raise ToolError('invalid_params', f"Missing required parameter '{key}'")
    for key, prop in properties.items():
        if key not in result and 'default' in prop:
            result[key] = prop['default']
    return result


def _validate_value(prop, value, path):
    if value is None:
        if prop.get('nullable'):
            return None
        raise ToolError('invalid_params', f"'{path}' cannot be null")
    expected = prop.get('type')
    if expected:
        types = _JSON_TYPES[expected]
        # bool is a subclass of int in Python: refuse it for numbers
        if not isinstance(value, types) or (expected in ('integer', 'number') and isinstance(value, bool)):
            raise ToolError('invalid_params', f"'{path}' must be of type {expected}")
    if 'enum' in prop and value not in prop['enum']:
        raise ToolError('invalid_params', f"'{path}' must be one of {prop['enum']}")
    if 'minimum' in prop and value < prop['minimum']:
        raise ToolError('invalid_params', f"'{path}' must be >= {prop['minimum']}")
    if 'maximum' in prop and value > prop['maximum']:
        raise ToolError('invalid_params', f"'{path}' must be <= {prop['maximum']}")
    if 'maxLength' in prop and len(value) > prop['maxLength']:
        raise ToolError('invalid_params', f"'{path}' is too long (max {prop['maxLength']})")
    if expected == 'array' and 'items' in prop:
        value = [_validate_value(prop['items'], item, f'{path}[{i}]') for i, item in enumerate(value)]
    if expected == 'object' and 'properties' in prop:
        value = validate_params(prop, value)
    return value
