# -*- coding: utf-8 -*-
import datetime
import os

import pytz

from odoo import api, fields, models, release
from odoo.tools import config, file_open

from .agent_registry import ToolError, agent_tool, collect_tools, validate_params

# Never readable through the generic tools, whatever the agent user's groups.
DENIED_MODELS = {
    'res.users.apikeys', 'res.users.apikeys.description', 'res.users.apikeys.show',
    'res.users.identitycheck', 'res.users.log', 'res.users.deletion',
    'change.password.wizard', 'change.password.user', 'auth_totp.device',
    'donialink.agent.log', 'donialink.agent.key.wizard',
}
DENIED_MODEL_PREFIXES = ('ir.', 'base.', 'bus.', 'base_import.', 'auth_', 'iap.', 'payment.provider')
DENIED_FIELDS = {'password', 'new_password', 'api_key_ids', 'totp_secret', 'signup_token', 'oauth_access_token'}
NON_READABLE_TYPES = {'binary', 'image'}

GENERIC_READ_MAX_LIMIT = 500
ODOO_AUTHOR = 'Odoo S.A.'


class AgentTools(models.AbstractModel):
    _name = 'donialink.agent.tools'
    _description = 'DoniaLink Agent Tools'

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------

    @api.model
    def _get_tools(self):
        """Return {tool name: (method name, metadata)} of the tools usable on this database."""
        return {
            name: tool for name, tool in collect_tools(type(self)).items()
            if all(self._requirement_met(req) for req in tool[1]['requires'])
        }

    @api.model
    def _requirement_met(self, requirement):
        """'model' or 'model:field' (a field tells two versions of a module apart)."""
        model, _sep, field = requirement.partition(':')
        return model in self.env and (not field or field in self.env[model]._fields)

    @api.model
    def _get_tool_manifest(self):
        tools = [meta for _method, meta in self._get_tools().values()]
        return sorted(tools, key=lambda t: (t['category'], t['name']))

    @api.model
    def _run_tool(self, name, params):
        tool = self._get_tools().get(name)
        if not tool:
            raise ToolError('unknown_tool', f"Unknown tool '{name}'", status=404)
        method, meta = tool
        params = validate_params(meta['input_schema'], params)
        return meta, getattr(self, method)(**params)

    @api.model
    def _agent_resources(self):
        """Documents that explain this database to AI models (served as MCP resources).

        Modules that add tools should also add a short domain note::

            def _agent_resources(self):
                return super()._agent_resources() + [{
                    'name': 'recipes', 'title': 'Recipes and food cost',
                    'description': '...', 'module': 'pos_recipe_costing_agent',
                    'path': 'pos_recipe_costing_agent/agent/AGENT.md',
                }]
        """
        return [{
            'name': 'connector',
            'title': 'DoniaLink connector: conventions',
            'description': 'How this connector works: dates, amounts, languages, generic read tools, limits.',
            'module': 'donialink_agent',
            'path': 'donialink_agent/agent/AGENT.md',
        }]

    @api.model
    def _read_resource(self, name):
        resource = next((r for r in self._agent_resources() if r['name'] == name), None)
        if not resource:
            raise ToolError('unknown_resource', f"Unknown resource '{name}'", status=404)
        with file_open(resource['path']) as f:
            return dict(resource, content=f.read())

    # ------------------------------------------------------------------
    # Helpers for tools (also used by modules that add tools)
    # ------------------------------------------------------------------

    @api.model
    def _parse_date(self, value, name='date'):
        try:
            return fields.Date.to_date(value)
        except ValueError:
            raise ToolError('invalid_params', f"'{name}' must be a date like 2026-09-30")

    @api.model
    def _business_tz(self):
        """Timezone used to cut business days: the setting, else the administrator's, else UTC."""
        tz = self._setting('business_tz') or self.env.ref('base.user_admin').sudo().tz or 'UTC'
        return tz if tz in pytz.all_timezones_set else 'UTC'

    @api.model
    def _local_range(self, date_from, date_to):
        """Turn local dates (inclusive) into a naive UTC datetime range [start, end) for the ORM.

        Returns (start_utc, end_utc, tz name, first day, last day).
        Defaults: date_to = today, date_from = date_to - 29 days.
        """
        tz = pytz.timezone(self._business_tz())
        today = datetime.datetime.now(tz).date()
        end_date = self._parse_date(date_to, 'date_to') if date_to else today
        start_date = self._parse_date(date_from, 'date_from') if date_from else end_date - datetime.timedelta(days=29)
        if start_date > end_date:
            raise ToolError('invalid_params', "'date_from' must be before 'date_to'")
        if (end_date - start_date).days > 366:
            raise ToolError('invalid_params', 'The period cannot be longer than one year')

        def to_utc(day):
            local = tz.localize(datetime.datetime.combine(day, datetime.time.min))
            return local.astimezone(pytz.utc).replace(tzinfo=None)

        return to_utc(start_date), to_utc(end_date + datetime.timedelta(days=1)), tz.zone, start_date, end_date

    @api.model
    def _currency_name(self, company_id=None):
        """Currency of the asked company (a database may have several, in different currencies); without one, of the
        agent user's main company."""
        if company_id:
            company = self.env['res.company'].sudo().browse(int(company_id)).exists()
            if company:
                return company.currency_id.name
        return self.env.company.currency_id.name

    @api.model
    def _company_domain(self, model, company_id):
        """Domain restricting ``model`` to one company, when asked and when the model has companies."""
        if company_id and 'company_id' in self.env[model]._fields:
            return [('company_id', 'in', [company_id, False])]
        return []

    @api.model
    def _setting(self, key, default=None):
        return self.env['ir.config_parameter'].sudo().get_param(f'donialink_agent.{key}', default)

    @api.model
    def _json_safe(self, value):
        """Convert ORM values to JSON-friendly ones (dates as ISO strings, records as ids)."""
        if isinstance(value, datetime.datetime):
            return fields.Datetime.to_string(value)
        if isinstance(value, datetime.date):
            return fields.Date.to_string(value)
        if isinstance(value, models.BaseModel):
            return value.ids
        if isinstance(value, dict):
            return {k: self._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._json_safe(v) for v in value]
        if isinstance(value, bytes):
            return None
        return value

    @api.model
    def _check_generic_model(self, model_name):
        if self._setting('allow_generic_read') != 'True':
            raise ToolError('disabled', 'Generic read tools are disabled on this database', status=403)
        if (model_name not in self.env or model_name in DENIED_MODELS
                or model_name.startswith(DENIED_MODEL_PREFIXES)):
            raise ToolError('model_not_allowed', f"Model '{model_name}' is not available", status=403)
        model = self.env[model_name]
        if model._abstract or model._transient:
            raise ToolError('model_not_allowed', f"Model '{model_name}' is not a stored model", status=403)
        if not model.has_access('read'):
            raise ToolError('access_denied', f"The agent user cannot read '{model_name}'", status=403)
        return model

    @api.model
    def _check_generic_fields(self, model, field_names):
        for name in field_names:
            base = name.split(':')[0]
            field = model._fields.get(base)
            if not field:
                raise ToolError('invalid_params', f"'{model._name}' has no field '{base}'")
            if base in DENIED_FIELDS or field.type in NON_READABLE_TYPES:
                raise ToolError('field_not_allowed', f"Field '{base}' is not available", status=403)

    # ------------------------------------------------------------------
    # Instance tools
    # ------------------------------------------------------------------

    @agent_tool(
        name='instance_info',
        title='Instance info', title_ar='معلومات النظام',
        category='instance',
        description="""
            Identity and size of this Odoo database: Odoo version, database name and size,
            creation date, URL, companies with their currency, installed languages, number of
            active internal users and installed modules (custom vs standard), connector version.
            Use it first to know which client database you are talking to.
        """,
        params={'type': 'object', 'properties': {
            'include_filestore_size': {
                'type': 'boolean', 'default': False,
                'description': 'Also measure the attachments folder on disk (slower).',
            },
        }},
    )
    def _tool_instance_info(self, include_filestore_size=False):
        env = self.env(su=True)
        cr = self.env.cr
        params = env['ir.config_parameter']
        cr.execute('SELECT pg_database_size(current_database())')
        db_size = cr.fetchone()[0]
        modules = env['ir.module.module'].search([('state', '=', 'installed')])
        custom = modules.filtered(lambda m: ODOO_AUTHOR not in (m.author or ''))
        connector = modules.filtered(lambda m: m.name == 'donialink_agent')
        result = {
            'odoo': {
                'version': release.version,
                'serie': release.serie,
                'edition': 'enterprise' if 'web_enterprise' in modules.mapped('name') else 'community',
            },
            'database': {
                'name': cr.dbname,
                'uuid': params.get_param('database.uuid'),
                'created_on': params.get_param('database.create_date'),
                'size_mb': round(db_size / 1024 / 1024, 1),
                'url': params.get_param('web.base.url'),
            },
            'companies': [{
                'id': c.id,
                'name': c.name,
                'currency': c.currency_id.name,
                'country': c.country_id.name or None,
            } for c in env['res.company'].search([])],
            'languages': env['res.lang'].search([]).mapped('code'),
            'users': {
                'internal_active': env['res.users'].search_count([('share', '=', False), ('active', '=', True)]),
                'portal_active': env['res.users'].search_count([('share', '=', True), ('active', '=', True)]),
            },
            'modules': {
                'installed': len(modules),
                'custom': len(custom),
                'standard': len(modules) - len(custom),
            },
            'connector_version': connector.latest_version,
            'server_time_utc': fields.Datetime.to_string(fields.Datetime.now()),
        }
        if include_filestore_size:
            total = 0
            for root, _dirs, files in os.walk(config.filestore(cr.dbname)):
                total += sum(os.path.getsize(os.path.join(root, f)) for f in files)
            result['database']['filestore_mb'] = round(total / 1024 / 1024, 1)
        return result

    @agent_tool(
        name='list_modules',
        title='Installed modules', title_ar='الموديولات المثبتة',
        category='instance',
        description="""
            List Odoo modules (apps) of this database with their state and versions.
            'version_installed' is the version active in the database, 'version_available' the
            version of the code on the server: when they differ, an upgrade is pending.
            'custom' is true for modules not made by Odoo S.A. (in-house and third-party).
        """,
        params={'type': 'object', 'properties': {
            'state': {
                'type': 'string', 'default': 'installed',
                'enum': ['installed', 'uninstalled', 'upgrade_pending', 'all'],
                'description': "'upgrade_pending' = installed modules whose code is newer than the database.",
            },
            'origin': {
                'type': 'string', 'default': 'all', 'enum': ['all', 'custom', 'standard'],
                'description': "'custom' = not made by Odoo S.A.",
            },
            'search': {'type': 'string', 'maxLength': 100, 'description': 'Filter on technical name or title.'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 300},
        }},
    )
    def _tool_list_modules(self, state='installed', origin='all', search=None, limit=300):
        Module = self.env['ir.module.module'].sudo()
        domain = []
        if state in ('installed', 'upgrade_pending'):
            domain.append(('state', '=', 'installed'))
        elif state == 'uninstalled':
            domain.append(('state', '=', 'uninstalled'))
        if search:
            domain += ['|', ('name', 'ilike', search), ('shortdesc', 'ilike', search)]
        modules = Module.search(domain, order='name')
        if origin != 'all':
            want_custom = origin == 'custom'
            modules = modules.filtered(lambda m: (ODOO_AUTHOR not in (m.author or '')) == want_custom)
        rows = []
        for module in modules:
            upgrade_pending = bool(module.state == 'installed' and module.installed_version
                                   and module.latest_version != module.installed_version)
            if state == 'upgrade_pending' and not upgrade_pending:
                continue
            rows.append({
                'name': module.name,
                'title': module.shortdesc,
                'state': module.state,
                'version_installed': module.latest_version,
                'version_available': module.installed_version,
                'upgrade_pending': upgrade_pending,
                'custom': ODOO_AUTHOR not in (module.author or ''),
                'author': module.author,
                'category': module.category_id.name,
                'application': module.application,
            })
        return {'count': len(rows), 'truncated': len(rows) > limit, 'modules': rows[:limit]}

    @agent_tool(
        name='instance_health',
        title='Instance health', title_ar='صحة النظام',
        category='instance',
        description="""
            Health check of this database. Returns a list of 'issues' (severity, code, message)
            and the details: scheduled jobs (crons) that fail or are late, module operations left
            pending, modules whose code is newer than the database, stuck or failed outgoing emails,
            point-of-sale sessions left open too long, and server errors logged in the last 24 hours.
            An empty 'issues' list means nothing needs attention.
        """,
        params={'type': 'object', 'properties': {
            'open_session_hours': {
                'type': 'integer', 'minimum': 1, 'maximum': 720, 'default': 24,
                'description': 'A POS session open longer than this is reported.',
            },
        }},
    )
    def _tool_instance_health(self, open_session_hours=24):
        env = self.env(su=True)
        now = fields.Datetime.now()
        issues = []
        details = {}

        failing = env['ir.cron'].search([('active', '=', True), ('failure_count', '>', 0)])
        late = env['ir.cron'].search([('active', '=', True), ('nextcall', '<', now - datetime.timedelta(hours=2))])
        details['crons_failing'] = [{
            'name': c.cron_name, 'failures': c.failure_count,
            'failing_since': self._json_safe(c.first_failure_date),
        } for c in failing]
        details['crons_late'] = [{
            'name': c.cron_name, 'next_run_was': self._json_safe(c.nextcall),
        } for c in late]
        if failing:
            issues.append(('error', 'cron_failing', f'{len(failing)} scheduled job(s) are failing'))
        if late:
            issues.append(('warning', 'cron_late', f'{len(late)} scheduled job(s) are more than 2 hours late'))

        Module = env['ir.module.module']
        pending = Module.search([('state', 'in', ('to install', 'to upgrade', 'to remove'))])
        details['module_operations_pending'] = [{'name': m.name, 'state': m.state} for m in pending]
        if pending:
            issues.append(('warning', 'module_operation_pending',
                           f'{len(pending)} module operation(s) were started but not finished'))
        outdated = Module.search([('state', '=', 'installed')]).filtered(
            lambda m: m.installed_version and m.latest_version != m.installed_version)
        details['modules_upgrade_pending'] = [{
            'name': m.name, 'version_installed': m.latest_version, 'version_available': m.installed_version,
        } for m in outdated]
        if outdated:
            issues.append(('info', 'module_upgrade_pending',
                           f'{len(outdated)} module(s) have newer code than the database (upgrade needed)'))

        if 'mail.mail' in env:
            failed = env['mail.mail'].search_count([('state', '=', 'exception')])
            stuck = env['mail.mail'].search_count([
                ('state', '=', 'outgoing'), ('create_date', '<', now - datetime.timedelta(hours=1))])
            details['emails'] = {'failed': failed, 'stuck_over_1h': stuck}
            if failed:
                issues.append(('warning', 'email_failed', f'{failed} outgoing email(s) failed'))
            if stuck:
                issues.append(('warning', 'email_stuck', f'{stuck} email(s) waiting for more than 1 hour'))

        if 'pos.session' in env:
            old = env['pos.session'].search([
                ('state', '!=', 'closed'),
                ('start_at', '<', now - datetime.timedelta(hours=open_session_hours)),
            ])
            details['pos_sessions_open_too_long'] = [{
                'session': s.name, 'point_of_sale': s.config_id.name, 'opened_by': s.user_id.name,
                'opened_at': self._json_safe(s.start_at), 'state': s.state,
            } for s in old]
            if old:
                issues.append(('warning', 'pos_session_open',
                               f'{len(old)} POS session(s) open for more than {open_session_hours} hours'))

        self.env.cr.execute("""
            SELECT count(*) FROM ir_logging
            WHERE type = 'server' AND level IN ('ERROR', 'CRITICAL')
              AND create_date > (now() at time zone 'utc') - interval '24 hours'
        """)
        errors = self.env.cr.fetchone()[0]
        details['server_errors_24h'] = errors
        if errors:
            issues.append(('warning', 'server_errors', f'{errors} server error(s) logged in the last 24 hours'))

        return {
            'status': 'ok' if not issues else ('error' if any(i[0] == 'error' for i in issues) else 'attention'),
            'issues': [{'severity': s, 'code': c, 'message': m} for s, c, m in issues],
            'details': details,
            'checked_at_utc': fields.Datetime.to_string(now),
            'note': 'server_errors_24h is only filled when Odoo runs with --log-db.',
        }

    @agent_tool(
        name='list_users',
        title='Users', title_ar='المستخدمون',
        category='instance',
        description="""
            Internal (back-office) users of this database: name, login, active, last login,
            administrator or not, company. With include_groups, also the access groups of each user
            (e.g. 'Point of Sale / Administrator'). Portal users and the DoniaLink agent itself are excluded.
        """,
        params={'type': 'object', 'properties': {
            'include_inactive': {'type': 'boolean', 'default': False},
            'include_groups': {'type': 'boolean', 'default': False},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 200},
        }},
    )
    def _tool_list_users(self, include_inactive=False, include_groups=False, limit=200):
        Users = self.env['res.users'].sudo().with_context(active_test=not include_inactive)
        agent = self.env.ref('donialink_agent.user_agent', raise_if_not_found=False)
        users = Users.search([('share', '=', False), ('id', 'not in', (agent | self.env.ref('base.user_root')).ids)],
                             order='name', limit=limit)
        rows = []
        for user in users:
            row = {
                'id': user.id,
                'name': user.name,
                'login': user.login,
                'active': user.active,
                'last_login': self._json_safe(user.login_date),
                'is_admin': user._is_system(),
                'company': user.company_id.name,
            }
            if include_groups:
                row['groups'] = sorted(user.groups_id.filtered(
                    lambda g: g.category_id and g.category_id.visible).mapped('full_name'))
            rows.append(row)
        return {'count': len(rows), 'users': rows}

    # ------------------------------------------------------------------
    # Generic read tools (bounded by the agent user's access rights)
    # ------------------------------------------------------------------

    @agent_tool(
        name='describe_model',
        title='Describe a model', title_ar='وصف جدول بيانات',
        category='generic',
        description="""
            List the fields of an Odoo model (table) that the agent can read: technical name, label,
            type, related model, help text, selection values. Use it before search_records or
            aggregate_records when no dedicated tool answers the question. Prefer dedicated tools:
            they return business-ready numbers.
        """,
        params={'type': 'object', 'required': ['model'], 'properties': {
            'model': {'type': 'string', 'maxLength': 100, 'description': "Technical model name, e.g. 'pos.order'."},
        }},
    )
    def _tool_describe_model(self, model):
        records = self._check_generic_model(model)
        description = records.fields_get(attributes=['string', 'type', 'relation', 'help', 'selection',
                                                     'required', 'store'])
        fields_out = {
            name: {k: v for k, v in attrs.items() if v not in (False, None, '', [])}
            for name, attrs in description.items()
            if name not in DENIED_FIELDS and attrs['type'] not in NON_READABLE_TYPES
        }
        ir_model = self.env['ir.model'].sudo()._get(model)
        return {
            'model': model,
            'name': ir_model.name,
            'record_count': records.search_count([]),
            'fields': fields_out,
        }

    @agent_tool(
        name='search_records',
        title='Search records', title_ar='بحث في السجلات',
        category='generic',
        description="""
            Read records of any allowed model with an Odoo domain filter, e.g.
            model='pos.order', domain=[["date_order", ">=", "2026-09-01"], ["state", "=", "cancel"]],
            fields=["name", "amount_total", "user_id"]. Many2one values come back as [id, name].
            Datetimes are UTC. Returns the total count and at most 'limit' records.
        """,
        params={'type': 'object', 'required': ['model'], 'properties': {
            'model': {'type': 'string', 'maxLength': 100},
            'domain': {'type': 'array', 'default': [], 'description': 'Odoo domain (list of conditions).'},
            'fields': {'type': 'array', 'items': {'type': 'string'}, 'default': ['display_name']},
            'order': {'type': 'string', 'maxLength': 200, 'description': "e.g. 'date_order desc'"},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': GENERIC_READ_MAX_LIMIT, 'default': 50},
            'offset': {'type': 'integer', 'minimum': 0, 'default': 0},
        }},
    )
    def _tool_search_records(self, model, domain=(), fields=('display_name',), order=None, limit=50, offset=0):
        records = self._check_generic_model(model)
        fields = list(fields) or ['display_name']
        if len(fields) > 50:
            raise ToolError('invalid_params', 'At most 50 fields per call')
        self._check_generic_fields(records, fields)
        try:
            total = records.search_count(domain)
            rows = records.search_read(domain, fields, offset=offset, limit=limit, order=order)
        except (ValueError, KeyError, TypeError) as e:
            raise ToolError('invalid_params', f'Invalid domain or order: {e}')
        return {'model': model, 'total': total, 'returned': len(rows), 'records': self._json_safe(rows)}

    @agent_tool(
        name='aggregate_records',
        title='Aggregate records', title_ar='تجميع السجلات',
        category='generic',
        description="""
            Group and total records of any allowed model (Odoo read_group), e.g. sales per day:
            model='pos.order', domain=[["state", "in", ["paid", "done", "invoiced"]]],
            groupby=["date_order:day"], measures=["amount_total:sum", "__count"].
            Date groupings: :day, :week, :month, :quarter, :year. Many2one groups come back as [id, name].
        """,
        params={'type': 'object', 'required': ['model', 'groupby'], 'properties': {
            'model': {'type': 'string', 'maxLength': 100},
            'domain': {'type': 'array', 'default': []},
            'groupby': {'type': 'array', 'items': {'type': 'string'},
                        'description': 'One or more fields, e.g. ["user_id", "date_order:month"].'},
            'measures': {'type': 'array', 'items': {'type': 'string'}, 'default': ['__count'],
                         'description': "'field:sum|avg|min|max|count_distinct' or '__count'."},
            'order': {'type': 'string', 'maxLength': 200},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': GENERIC_READ_MAX_LIMIT, 'default': 100},
        }},
    )
    def _tool_aggregate_records(self, model, groupby, domain=(), measures=('__count',), order=None, limit=100):
        records = self._check_generic_model(model)
        if not groupby or len(groupby) > 3:
            raise ToolError('invalid_params', 'Give 1 to 3 groupby fields')
        measures = [m for m in measures if m != '__count']
        self._check_generic_fields(records, list(groupby) + measures)
        try:
            groups = records.read_group(domain, measures, list(groupby), orderby=order, limit=limit, lazy=False)
        except (ValueError, KeyError, TypeError) as e:
            raise ToolError('invalid_params', f'Invalid aggregation: {e}')
        rows = []
        for group in groups:
            row = {k: v for k, v in group.items() if k not in ('__domain', '__context', '__fold', '__range')}
            row['count'] = row.pop('__count', None)
            rows.append(row)
        return {'model': model, 'groups': self._json_safe(rows)}
