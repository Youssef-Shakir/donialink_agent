# -*- coding: utf-8 -*-
import io
import json
from unittest.mock import patch

from odoo import SUPERUSER_ID
from odoo.tools import config
from odoo.tests import HttpCase, tagged

from odoo.addons.donialink_agent.models.agent_log import FILE_MAX_SIZE
from odoo.addons.donialink_agent.models.agent_registry import API_KEY_SCOPE, FILE_KEY, agent_tool

PREFIX = '/donialink/agent/v1'
EXPORT_REPORTS = ('profit_and_loss', 'balance_sheet', 'trial_balance', 'general_ledger', 'partner_ledger',
                  'aged_balance')


@tagged('post_install', '-at_install')
class TestAgentApi(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agent = cls.env.ref('donialink_agent.user_agent')
        cls.key = cls.env['res.users.apikeys'].with_user(cls.agent).sudo()._generate(API_KEY_SCOPE, 'test', None)
        cls.env['ir.config_parameter'].sudo().set_param('donialink_agent.api_enabled', 'True')
        cls.env['ir.config_parameter'].sudo().set_param('donialink_agent.allowed_ips', False)
        # PDF exports: wkhtmltopdf would load the report assets from the test server, which waits for
        # the request rendering the PDF (one test cursor). A closed port makes it render without them.
        cls.env['ir.config_parameter'].sudo().set_param('report.url', 'http://127.0.0.1:9')

    def _call(self, path, body=None, key=None):
        headers = {'Authorization': f'Bearer {key or self.key}'}
        if body is None:
            response = self.url_open(PREFIX + path, headers=headers)
        else:
            headers['Content-Type'] = 'application/json'
            response = self.url_open(PREFIX + path, data=json.dumps(body), headers=headers)
        return response.status_code, response.json()

    def test_auth(self):
        status, body = self._call('/ping', key='0' * 40)
        self.assertEqual(status, 401)
        self.assertEqual(body['error']['code'], 'unauthorized')
        status, body = self._call('/ping')
        self.assertEqual(status, 200)
        self.assertEqual(body['database'], self.env.cr.dbname)

    def test_global_key_of_other_user_refused(self):
        """An ordinary API key of an admin is not an agent key."""
        admin_key = self.env['res.users.apikeys'].with_user(self.env.ref('base.user_admin')).sudo()._generate(
            None, 'rpc', None)
        status, _body = self._call('/ping', key=admin_key)
        self.assertEqual(status, 401)

    def test_manifest_and_core_tools(self):
        status, body = self._call('/tools')
        self.assertEqual(status, 200)
        names = {t['name'] for t in body['tools']}
        self.assertTrue({'instance_info', 'list_modules', 'instance_health', 'list_users',
                         'describe_model', 'search_records', 'aggregate_records'} <= names)
        status, body = self._call('/tools/instance_info', {'params': {}})
        self.assertEqual(status, 200)
        self.assertEqual(body['result']['database']['name'], self.env.cr.dbname)
        status, body = self._call('/tools/list_modules', {'params': {'search': 'donialink_agent'}})
        self.assertEqual(body['result']['modules'][0]['name'], 'donialink_agent')

    def test_validation_and_denials(self):
        status, body = self._call('/tools/list_modules', {'params': {'limit': 'x'}})
        self.assertEqual((status, body['error']['code']), (400, 'invalid_params'))
        status, body = self._call('/tools/nope', {})
        self.assertEqual((status, body['error']['code']), (404, 'unknown_tool'))
        status, body = self._call('/tools/search_records', {'params': {'model': 'ir.config_parameter'}})
        self.assertEqual((status, body['error']['code']), (403, 'model_not_allowed'))
        status, body = self._call('/tools/search_records', {'params': {'model': 'res.users', 'fields': ['password']}})
        self.assertEqual((status, body['error']['code']), (403, 'field_not_allowed'))

    def test_read_tool_cannot_write(self):
        partner = self.env['res.partner'].create({'name': 'Before'})
        tools_class = type(self.env['donialink.agent.tools'])

        @agent_tool(name='test_sneaky_write', description='Test only: a read tool that tries to write',
                    params={'type': 'object', 'properties': {'partner_id': {'type': 'integer'}}})
        def _tool_test_sneaky_write(self, partner_id=None):
            self.env['res.partner'].sudo().browse(partner_id).name = 'After'
            return {'done': True}

        tools_class._tool_test_sneaky_write = _tool_test_sneaky_write
        try:
            status, body = self._call('/tools/test_sneaky_write', {'params': {'partner_id': partner.id}})
            self.assertEqual(status, 200)
            self.assertTrue(body['result']['done'])
        finally:
            del tools_class._tool_test_sneaky_write
        partner.invalidate_recordset()
        self.assertEqual(partner.name, 'Before')

    def test_audit_log(self):
        Log = self.env['donialink.agent.log']
        before = Log.search_count([])
        self._call('/tools/instance_info', {'params': {}, 'actor': {'id': 's-1', 'name': 'Staff'},
                                            'request_id': 'r-1'})
        log = Log.search([], limit=1)
        self.assertEqual(Log.search_count([]), before + 1)
        self.assertEqual((log.tool, log.status, log.actor_name, log.request_ref),
                         ('instance_info', 'success', 'Staff', 'r-1'))

    def test_tool_hidden_when_model_missing(self):
        tools_class = type(self.env['donialink.agent.tools'])

        @agent_tool(name='test_needs_missing_model', description='Test only', requires=('no.such.model',))
        def _tool_test_needs_missing_model(self):
            return {}

        tools_class._tool_test_needs_missing_model = _tool_test_needs_missing_model
        try:
            _status, body = self._call('/tools')
            self.assertNotIn('test_needs_missing_model', {t['name'] for t in body['tools']})
            status, body = self._call('/tools/test_needs_missing_model', {})
            self.assertEqual((status, body['error']['code']), (404, 'unknown_tool'))
        finally:
            del tools_class._tool_test_needs_missing_model

    def test_recipe_and_stock_tools(self):
        if 'restaurant.recipe' not in self.env:
            self.skipTest('pos_recipe_costing is not installed')
        _status, body = self._call('/tools')
        names = {t['name'] for t in body['tools']}
        expected = {'recipe_costs', 'recipe_card', 'products_without_recipe', 'stocktake_results',
                    'waste_summary', 'ingredient_price_changes', 'stock_levels',
                    'ingredient_consumption', 'food_cost_overview'}
        self.assertTrue(expected <= names, expected - names)
        wide = {'date_from': '2025-11-01', 'date_to': '2026-10-31'}
        calls = {
            'recipe_costs': {},
            'products_without_recipe': {},
            'stocktake_results': dict(wide, state='all'),
            'waste_summary': wide,
            'ingredient_price_changes': wide,
            'stock_levels': {'only_problems': False},
            'ingredient_consumption': wide,
            'food_cost_overview': dict(wide, per_day=False),
        }
        for tool, params in calls.items():
            status, body = self._call(f'/tools/{tool}', {'params': params, 'lang': 'ar'})
            self.assertEqual(status, 200, f'{tool}: {body}')
            self.assertTrue(body['ok'], tool)
        recipe = self.env['restaurant.recipe'].search([], limit=1)
        if recipe:
            status, body = self._call('/tools/recipe_card', {'params': {'recipe_id': recipe.id}})
            self.assertEqual(status, 200, body)
            self.assertEqual(body['result']['recipe'], recipe.name)
        status, body = self._call('/tools/recipe_card', {'params': {}})
        self.assertEqual((status, body['error']['code']), (400, 'invalid_params'))
        status, body = self._call('/tools/waste_summary', {'params': {'date_from': '2026-10-31', 'date_to': '2026-01-01'}})
        self.assertEqual((status, body['error']['code']), (400, 'invalid_params'))

    def _technical_admin(self):
        return self.env['res.users'].create({
            'name': 'Console Admin', 'login': 'console_admin_dl', 'password': 'console_admin_pwd_123',
            'groups_id': [(6, 0, [self.env.ref('base.group_system').id,
                                  self.env.ref('donialink_agent.group_donialink_technical').id])]})

    def test_console(self):
        self._technical_admin()
        self.authenticate('console_admin_dl', 'console_admin_pwd_123')
        response = self.url_open('/donialink/agent/console')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Test Console', response.text)
        result = self.make_jsonrpc_request('/donialink/agent/console/call', {
            'kind': 'call', 'name': 'instance_info', 'body': {'params': {}}})
        self.assertEqual(result['status'], 200)
        self.assertTrue(result['response']['ok'])
        log = self.env['donialink.agent.log'].search([], limit=1)
        self.assertEqual(log.tool, 'instance_info')
        self.assertIn('(test console)', log.actor_name)

    def test_console_refused_to_non_admin(self):
        self.env['res.users'].create({'name': 'Basic', 'login': 'basic_dl', 'password': 'basic_dl_pwd_123',
                                      'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.authenticate('basic_dl', 'basic_dl_pwd_123')
        response = self.url_open('/donialink/agent/console')
        self.assertEqual(response.status_code, 404)

    def test_hidden_from_client_administrators(self):
        """A client's own administrator sees no DoniaLink menu, settings or console."""
        admin = self.env['res.users'].create({'name': 'Client Admin', 'login': 'client_admin_dl',
                                              'password': 'client_admin_pwd_123',
                                              'groups_id': [(6, 0, [self.env.ref('base.group_system').id])]})
        self.assertFalse(admin.has_group('donialink_agent.group_donialink_technical'))
        menus = self.env['ir.ui.menu'].with_user(admin)._visible_menu_ids()
        self.assertNotIn(self.env.ref('donialink_agent.menu_donialink_agent_root').id, menus)
        arch = self.env['res.config.settings'].with_user(admin).get_views([(False, 'form')])['views']['form']['arch']
        self.assertNotIn('donialink_agent_settings', arch)
        # nor the technical agent user, in user lists and pickers
        spec = {'display_name': {}}
        users = self.env['res.users'].with_user(admin).web_search_read([], spec)['records']
        self.assertNotIn(self.agent.id, [u['id'] for u in users])
        self.assertNotIn(self.agent.id, [i for i, _ in self.env['res.users'].with_user(admin).name_search('DoniaLink')])
        partners = self.env['res.partner'].with_user(admin).name_search('DoniaLink Agent')
        self.assertNotIn(self.agent.partner_id.id, [i for i, _ in partners])
        # DoniaLink staff still see it
        staff = self.env['res.users'].with_user(admin).sudo()
        self.assertIn(self.agent.id, [u['id'] for u in staff.web_search_read([], spec)['records']])
        self.assertNotIn('name="donialink_agent"', arch)
        # DoniaLink staff get it as its own app in Settings (so /odoo/settings#donialink_agent opens it)
        admin.write({'groups_id': [(4, self.env.ref('donialink_agent.group_donialink_technical').id)]})
        arch = self.env['res.config.settings'].with_user(admin).get_views([(False, 'form')])['views']['form']['arch']
        self.assertIn('name="donialink_agent"', arch)
        self.assertIn('donialink_agent_platform_last_seen', arch)
        admin.write({'groups_id': [(3, self.env.ref('donialink_agent.group_donialink_technical').id)]})
        self.authenticate('client_admin_dl', 'client_admin_pwd_123')
        self.assertEqual(self.url_open('/donialink/agent/console').status_code, 404)

    def test_secret_link(self):
        admin = self.env['res.users'].create({'name': 'DL Staff', 'login': 'dl_staff', 'password': 'dl_staff_pwd_123',
                                              'groups_id': [(6, 0, [self.env.ref('base.group_system').id])]})
        group = self.env.ref('donialink_agent.group_donialink_technical')
        token = 'x' * 8 + 'secret-token-123'
        self.authenticate('dl_staff', 'dl_staff_pwd_123')
        with patch.dict(config.options, {'donialink_agent_admin_token': token}):
            self.assertEqual(self.url_open('/donialink/agent/admin/wrong-token-1234567').status_code, 404)
            self.assertNotIn(group, admin.groups_id)
            self.url_open(f'/donialink/agent/admin/{token}', allow_redirects=False)
            self.assertIn(group, admin.groups_id)
            self.url_open(f'/donialink/agent/admin/{token}/lock', allow_redirects=False)
            self.assertNotIn(group, admin.groups_id)
        # no token configured: the link doesn't exist
        with patch.dict(config.options, {'donialink_agent_admin_token': ''}):
            self.assertEqual(self.url_open(f'/donialink/agent/admin/{token}').status_code, 404)

    def test_business_timezone_setting(self):
        if 'pos.order' not in self.env:
            self.skipTest('point_of_sale is not installed')
        self.env['ir.config_parameter'].sudo().set_param('donialink_agent.business_tz', 'Asia/Baghdad')
        status, body = self._call('/tools/food_cost_overview', {'params': {'date_from': '2026-09-01', 'date_to': '2026-09-01'}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['result']['period']['timezone'], 'Asia/Baghdad')
        start, end, tz, *_days = self.env['donialink.agent.tools']._local_range('2026-09-01', '2026-09-01')
        # Baghdad is UTC+3: the business day 1 Sept runs from 31 Aug 21:00 to 1 Sept 21:00 UTC
        self.assertEqual((str(start), str(end)), ('2026-08-31 21:00:00', '2026-09-01 21:00:00'))

    def test_sales_and_cash_tools(self):
        if 'pos.order' not in self.env:
            self.skipTest('point_of_sale is not installed')
        wide = {'date_from': '2025-11-01', 'date_to': '2026-10-31'}
        calls = [
            ('sales_summary', dict(wide, group_by=g)) for g in
            ('day', 'hour', 'weekday', 'product', 'category', 'payment_method', 'cashier', 'point_of_sale', 'none')
        ] + [
            ('cancelled_orders', dict(wide, group_by=g)) for g in ('cashier', 'product', 'day')
        ] + [
            ('discounts_summary', dict(wide, group_by=g)) for g in ('cashier', 'product', 'day')
        ] + [
            ('pos_sessions', wide),
            ('cash_differences', wide),
        ]
        if 'expense.express' in self.env:
            calls += [('cashier_payouts', dict(wide, direction='all', group_by=g)) for g in
                      ('category', 'partner', 'cashier', 'day')]
            calls += [('expenses_summary', dict(wide, group_by=g)) for g in ('category', 'partner', 'day', 'month')]
            calls += [('bills_to_pay', {})]
        for tool, params in calls:
            status, body = self._call(f'/tools/{tool}', {'params': params, 'lang': 'ar'})
            self.assertEqual(status, 200, f'{tool} {params}: {body}')
            self.assertTrue(body['ok'], tool)

    def test_sales_summary_matches_orders(self):
        if 'pos.order' not in self.env:
            self.skipTest('point_of_sale is not installed')
        self.env['ir.config_parameter'].sudo().set_param('donialink_agent.business_tz', 'UTC')
        orders = self.env['pos.order'].search([('state', 'in', ('paid', 'done', 'invoiced')),
                                               ('date_order', '>=', '2025-11-01'), ('date_order', '<', '2026-11-01')])
        status, body = self._call('/tools/sales_summary', {'params': {
            'date_from': '2025-11-01', 'date_to': '2026-10-31', 'group_by': 'none'}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['result']['orders'], len(orders))
        self.assertAlmostEqual(body['result']['sales_total'], sum(orders.mapped('amount_total')), places=1)

    def test_accounting_tools_reconcile(self):
        if 'account.move.line' not in self.env:
            self.skipTest('account is not installed')
        Line = self.env['account.move.line'].sudo()
        period = {'date_from': '2025-01-01', 'date_to': '2026-12-31'}
        pl_types = ('income', 'income_other', 'expense', 'expense_depreciation', 'expense_direct_cost')
        lines = Line.search([('parent_state', '=', 'posted'), ('date', '>=', '2025-01-01'),
                             ('date', '<=', '2026-12-31'), ('account_id.account_type', 'in', pl_types)])
        status, body = self._call('/tools/profit_and_loss', {'params': dict(period, compare='previous_year')})
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body['result']['net_profit'], -sum(lines.mapped('balance')), places=1)
        self.assertIn('comparison', body['result'])

        status, body = self._call('/tools/balance_sheet', {'params': {'date': '2026-12-31'}})
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body['result']['check'], 0, places=1)

        status, body = self._call('/tools/trial_balance', {'params': dict(period, include_opening=True)})
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body['result']['total_debit'], body['result']['total_credit'], places=1)

        open_payables = Line.search([('parent_state', '=', 'posted'), ('reconciled', '=', False),
                                     ('account_id.account_type', '=', 'liability_payable')])
        status, body = self._call('/tools/vendor_balances', {'params': {}})
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body['result']['total'], -sum(open_payables.mapped('amount_residual')), places=1)
        status, body = self._call('/tools/vendor_balances', {'params': {'side': 'receivable'}})
        self.assertEqual(status, 200, body)

        if 'pos.order' in self.env:
            status, body = self._call('/tools/pos_vs_accounting_sales', {'params': {
                'date_from': '2025-11-01', 'date_to': '2026-10-31'}})
            self.assertEqual(status, 200, body)
            result = body['result']
            explained = sum(v['amount'] for v in result['explained'].values())
            self.assertAlmostEqual(result['difference'] - explained, result['unexplained'], places=1)

    def test_kit_engine_matches_account_types(self):
        if 'financial.report' not in self.env:
            self.skipTest('DoniaLink Accounting (base_accounting_kit) is not installed')
        company = self.env.company.id
        status, body = self._call('/tools/profit_and_loss', {'params': {
            'date_from': '2025-01-01', 'date_to': '2026-12-31', 'company_id': company}})
        self.assertEqual(status, 200, body)
        kit = body['result']
        self.assertEqual(kit['engine'], 'base_accounting_kit')
        from datetime import date
        own = self.env['donialink.agent.tools']._profit_and_loss(date(2025, 1, 1), date(2026, 12, 31), company, 'posted', False)
        self.assertAlmostEqual(kit['net_profit'], own['net_profit'], places=1)
        self.assertAlmostEqual(kit['revenue'], own['revenue'], places=1)
        for k_section, o_section in zip(kit['sections'], own['sections']):
            self.assertEqual(k_section['key'], o_section['key'])
            self.assertAlmostEqual(k_section['amount'], o_section['amount'], places=1, msg=k_section['key'])

        status, body = self._call('/tools/balance_sheet', {'params': {'date': '2026-12-31', 'company_id': company}})
        self.assertEqual(status, 200, body)
        sheet = body['result']
        self.assertEqual(sheet['engine'], 'base_accounting_kit')
        self.assertAlmostEqual(sheet['check'], 0, places=1)
        Line = self.env['account.move.line']
        assets = sum(Line.search([('parent_state', '=', 'posted'), ('company_id', '=', company),
                                  ('date', '<=', '2026-12-31'),
                                  ('account_id.account_type', 'like', 'asset_')]).mapped('balance'))
        self.assertAlmostEqual(sheet['assets']['amount'], assets, places=1)

    # ------------------------------------------------------------------
    # Report exports and file downloads
    # ------------------------------------------------------------------

    def _skip_without_dynamic_reports(self):
        if 'account.dynamic.report' not in self.env:
            self.skipTest('DoniaLink Accounting (base_accounting_kit) is not installed')

    def _download(self, file_id, key=None, auth=True):
        headers = {'Authorization': f'Bearer {key or self.key}'} if auth else {}
        return self.url_open(f'{PREFIX}/files/{file_id}', headers=headers)

    def _export(self, report, fmt, options=None, lang='en'):
        return self._call('/tools/export_report', {'params': {'report': report, 'format': fmt,
                                                              'options': options or {}}, 'lang': lang})

    def test_export_report_all_reports_and_formats(self):
        self._skip_without_dynamic_reports()
        Log = self.env['donialink.agent.log']
        wide = {'date_from': '2025-01-01', 'date_to': '2026-12-31'}
        for i, report in enumerate(EXPORT_REPORTS):
            for fmt, magic in (('pdf', b'%PDF'), ('xlsx', b'PK')):
                lang = 'ar' if i % 2 else 'en'
                status, body = self._export(report, fmt, wide, lang)
                self.assertEqual(status, 200, f'{report} {fmt}: {body}')
                result = body['result']
                self.assertNotIn(FILE_KEY, result)
                file = result['file']
                self.assertTrue(file['download_url'].endswith(f"{PREFIX}/files/{file['id']}"), file)
                self.assertTrue(file['name'].endswith(f'.{fmt}'))
                self.assertTrue(file['size'] > 0 and file['expires_at'])
                call_log = Log.search([('tool', '=', 'export_report')], limit=1)
                attachment = self.env['ir.attachment'].browse(file['id'])
                self.assertEqual((attachment.res_model, attachment.res_id), (Log._name, call_log.id))
                self.assertFalse(attachment.public)

                response = self._download(file['id'])
                self.assertEqual(response.status_code, 200, f'{report} {fmt}')
                self.assertTrue(response.content.startswith(magic), f'{report} {fmt}: {response.content[:20]}')
                self.assertEqual(len(response.content), file['size'])
                self.assertEqual(response.headers['Content-Type'], file['mimetype'])
                self.assertIn('attachment', response.headers['Content-Disposition'])
                download_log = Log.search([], limit=1)
                self.assertEqual((download_log.tool, download_log.status, download_log.result_size),
                                 ('file_download', 'success', file['size']))
                self.assertEqual(json.loads(download_log.params), {'file_id': file['id']})

    def test_export_report_options(self):
        self._skip_without_dynamic_reports()
        status, body = self._export('aged_balance', 'xlsx', {'partner_type': 'payable', 'date_to': '2026-06-30'})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['result']['options']['date_to'], '2026-06-30')
        self.assertEqual(body['result']['options']['partner_type'], 'payable')
        refused = [
            ('trial_balance', {'no_such_option': 1}),
            ('trial_balance', {'comparison': 'previous_year'}),
            ('aged_balance', {'partner_type': 'both'}),
            ('profit_and_loss', {'date_from': '2026-12-31', 'date_to': '2026-01-01'}),
            ('profit_and_loss', {'date_to': '31/12/2026'}),
        ]
        for report, options in refused:
            status, body = self._export(report, 'xlsx', options)
            self.assertEqual((status, body['error']['code']), (400, 'invalid_params'), (report, options))

    def test_export_trial_balance_xlsx_matches_report(self):
        self._skip_without_dynamic_reports()
        import openpyxl
        options = {'date_from': '2025-01-01', 'date_to': '2026-12-31'}
        status, body = self._export('trial_balance', 'xlsx', options)
        self.assertEqual(status, 200, body)
        workbook = openpyxl.load_workbook(io.BytesIO(self._download(body['result']['file']['id']).content))
        rows = [row for row in workbook.active.iter_rows(values_only=True) if any(v is not None for v in row)]
        report = self.env['account.dynamic.report'].with_user(SUPERUSER_ID).get_report(
            'trial_balance', dict(options, date_filter='custom'))
        total = report['lines'][-1]
        self.assertEqual(total['kind'], 'total')
        last = rows[-1]
        self.assertEqual(last[0].strip(), total['name'])
        for value, expected in zip(last[1:5], total['values']):
            self.assertAlmostEqual(value or 0.0, expected, places=2)
        # one sheet row per report line, after the 5 header rows
        self.assertEqual(len(rows) - 5, len(report['lines']))

    def test_file_download_refused(self):
        self._skip_without_dynamic_reports()
        status, body = self._export('trial_balance', 'xlsx')
        self.assertEqual(status, 200, body)
        file_id = body['result']['file']['id']

        response = self._download(file_id, auth=False)
        self.assertEqual((response.status_code, response.json()['error']['code']), (401, 'unauthorized'))
        response = self._download(file_id, key='0' * 40)
        self.assertEqual(response.status_code, 401)

        other = self.env['ir.attachment'].create({'name': 'other.txt', 'raw': b'secret',
                                                  'res_model': 'res.partner', 'res_id': self.env.user.partner_id.id})
        for unknown in (other.id, 10 ** 9):
            response = self._download(unknown)
            self.assertEqual((response.status_code, response.json()['error']['code']), (404, 'unknown_file'))

        self.env.flush_all()
        self.env.cr.execute("UPDATE ir_attachment SET create_date = now() - interval '25 hours' WHERE id = %s",
                            [file_id])
        self.env['ir.attachment'].invalidate_model(['create_date'])
        response = self._download(file_id)
        self.assertEqual((response.status_code, response.json()['error']['code']), (404, 'expired'))
        log = self.env['donialink.agent.log'].search([], limit=1)
        self.assertEqual((log.tool, log.status, log.error_code), ('file_download', 'error', 'expired'))

        self.env['donialink.agent.log']._cron_purge_old_logs()
        self.assertFalse(self.env['ir.attachment'].browse(file_id).exists())
        self.assertTrue(other.exists())

    def test_read_tool_file_survives_rollback(self):
        partner = self.env['res.partner'].create({'name': 'Before'})
        tools_class = type(self.env['donialink.agent.tools'])

        @agent_tool(name='test_file_maker', description='Test only: a read tool returning a file',
                    params={'type': 'object', 'properties': {'partner_id': {'type': 'integer'},
                                                             'size': {'type': 'integer'}}})
        def _tool_test_file_maker(self, partner_id=None, size=5):
            self.env['res.partner'].sudo().browse(partner_id).name = 'After'
            return {'rows': 1, FILE_KEY: {'name': 'test.txt', 'mimetype': 'text/plain', 'content': b'x' * size}}

        tools_class._tool_test_file_maker = _tool_test_file_maker
        try:
            status, body = self._call('/tools/test_file_maker', {'params': {'partner_id': partner.id}})
            self.assertEqual(status, 200, body)
            self.assertEqual(body['result']['rows'], 1)
            file_id = body['result']['file']['id']
            Attachment = self.env['ir.attachment']
            count = Attachment.search_count([('res_model', '=', 'donialink.agent.log')])
            status, body = self._call('/tools/test_file_maker', {'params': {'size': FILE_MAX_SIZE + 1}})
            self.assertEqual((status, body['error']['code']), (413, 'file_too_large'))
            self.assertEqual(Attachment.search_count([('res_model', '=', 'donialink.agent.log')]), count)
        finally:
            del tools_class._tool_test_file_maker
        partner.invalidate_recordset()
        self.assertEqual(partner.name, 'Before')
        self.assertEqual(self.env['ir.attachment'].browse(file_id).raw, b'xxxxx')
        self.assertEqual(self._download(file_id).content, b'xxxxx')

    def test_console_export_and_download(self):
        self._skip_without_dynamic_reports()
        self._technical_admin()
        self.authenticate('console_admin_dl', 'console_admin_pwd_123')
        result = self.make_jsonrpc_request('/donialink/agent/console/call', {
            'kind': 'call', 'name': 'export_report',
            'body': {'params': {'report': 'balance_sheet', 'format': 'pdf'}, 'lang': 'ar'}})
        self.assertEqual(result['status'], 200, result)
        file = result['response']['result']['file']
        # the administrator's session is enough, as for the console's Download link
        response = self.url_open(f"{PREFIX}/files/{file['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertIn('(Odoo session)', self.env['donialink.agent.log'].search([], limit=1).actor_name)

    def test_purchases_by_vendor_matches_bills(self):
        if 'account.move' not in self.env:
            self.skipTest('account is not installed')
        bills = self.env['account.move'].sudo().search([
            ('move_type', 'in', ('in_invoice', 'in_refund')), ('state', '=', 'posted'),
            ('invoice_date', '>=', '2025-01-01'), ('invoice_date', '<=', '2026-12-31')])
        status, body = self._call('/tools/purchases_by_vendor', {'params': {
            'date_from': '2025-01-01', 'date_to': '2026-12-31', 'limit': 500}})
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body['result']['total'], -sum(bills.mapped('amount_total_signed')), places=1)
        self.assertEqual(body['result']['bills'], len(bills.filtered(lambda b: b.move_type == 'in_invoice')))

    def test_pos_sessions_to_review(self):
        if 'pos.session' not in self.env or 'cash_review_state' not in self.env['pos.session']._fields:
            self.skipTest('Expenses & Cash Control is not installed')
        waiting = self.env['pos.session'].sudo().search_count([('state', '=', 'closed'),
                                                                ('cash_review_state', '=', 'to_review')])
        status, body = self._call('/tools/pos_sessions_to_review', {'params': {}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['result']['to_review'], waiting)

    def test_companies(self):
        usd = self.env.ref('base.USD')
        eur = self.env.ref('base.EUR')
        eur.active = True
        main = self.env.company
        main.currency_id = usd
        branch = self.env['res.company'].create({'name': 'Branch Karrada', 'currency_id': eur.id})
        # the ping lists every company of the database with its currency
        status, body = self._call('/ping')
        self.assertEqual(status, 200, body)
        companies = {c['id']: c for c in body['companies']}
        self.assertEqual(companies[branch.id], {'id': branch.id, 'name': 'Branch Karrada', 'currency': 'EUR'})
        self.assertEqual(companies[main.id]['currency'], 'USD')
        if 'pos.order' not in self.env:
            return
        # a tool asked for one company answers in that company's currency
        status, body = self._call('/tools/sales_summary', {'params': {'group_by': 'none', 'company_id': branch.id}})
        self.assertEqual(status, 200, body)
        self.assertEqual(body['result']['currency'], 'EUR')
        status, body = self._call('/tools/sales_summary', {'params': {'group_by': 'none'}})
        self.assertEqual(body['result']['currency'], 'USD')
