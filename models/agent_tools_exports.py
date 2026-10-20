# -*- coding: utf-8 -*-
"""Report exports: the PDF and XLSX files of DoniaLink Accounting's interactive reports.

The files are made by the report engine of ``base_accounting_kit`` (``account.dynamic.report``),
so they are the same files a user gets from the report buttons. The tool returns the file
under :data:`FILE_KEY`; the controller stores it after the read-only rollback and answers
with a download link (see ``controllers/main.py``).
"""
import json

from odoo import SUPERUSER_ID, _, api, models

from .agent_registry import FILE_KEY, ToolError, agent_tool

EXPORT_REPORTS = ['profit_and_loss', 'balance_sheet', 'trial_balance', 'general_ledger', 'partner_ledger',
                  'aged_balance']
COMPARABLE = ('profit_and_loss', 'balance_sheet')
LEDGERS = ('general_ledger', 'partner_ledger', 'aged_balance')
MIMETYPES = {
    'pdf': 'application/pdf',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}

OPTIONS = {
    'type': 'object',
    'description': 'Report options, as in the report header. All optional.',
    'properties': {
        'date_filter': {
            'type': 'string',
            'enum': ['today', 'this_month', 'last_month', 'this_quarter', 'last_quarter', 'this_year', 'last_year',
                     'custom'],
            'description': "Period. Default: this_year (P&L, partner ledger), today (balance sheet, aged balance), "
                           "this_month (trial balance, general ledger). 'custom' uses date_from / date_to; "
                           "giving dates without date_filter means custom.",
        },
        'date_from': {'type': 'string', 'description': 'First day, YYYY-MM-DD (custom period; ignored by the '
                                                      'balance sheet and the aged balance, which are as of date_to).'},
        'date_to': {'type': 'string', 'description': 'Last day included, or the "as of" date, YYYY-MM-DD. '
                                                    'Default: today.'},
        'target_move': {'type': 'string', 'enum': ['posted', 'all'],
                        'description': "'posted' (default, as accountants report) or 'all' to include drafts."},
        'comparison': {'type': 'string', 'enum': ['none', 'previous_period', 'previous_year'],
                       'description': 'Extra comparison column. Profit and loss and balance sheet only.'},
        'hide_zero': {'type': 'boolean', 'description': 'Hide accounts without amounts (default true). '
                                                       'Profit and loss, balance sheet, trial balance.'},
        'partner_type': {'type': 'string', 'enum': ['both', 'receivable', 'payable'],
                         'description': "Partner ledger: both (default), receivable (customers) or payable "
                                        "(suppliers). Aged balance: receivable (default) or payable."},
        'search': {'type': 'string', 'maxLength': 100,
                   'description': 'Ledgers and aged balance: only accounts / partners matching this text.'},
    },
}


class AgentTools(models.AbstractModel):
    _inherit = 'donialink.agent.tools'

    @api.model
    def _export_titles(self):
        return {
            'profit_and_loss': _('Profit and Loss'),
            'balance_sheet': _('Balance Sheet'),
            'trial_balance': _('Trial Balance'),
            'general_ledger': _('General Ledger'),
            'partner_ledger': _('Partner Ledger'),
            'aged_balance': _('Aged Partner Balance'),
        }

    @api.model
    def _check_export_options(self, report, options):
        options = dict(options)
        for key in ('date_from', 'date_to'):
            if options.get(key):
                self._parse_date(options[key], key)
        if (options.get('date_from') or options.get('date_to')) and 'date_filter' not in options:
            options['date_filter'] = 'custom'
        if options.get('date_from') and options.get('date_to') and options['date_from'] > options['date_to']:
            raise ToolError('invalid_params', "'date_from' must be before 'date_to'")
        if options.get('comparison', 'none') != 'none' and report not in COMPARABLE:
            raise ToolError('invalid_params', "'comparison' is only for profit_and_loss and balance_sheet")
        if 'partner_type' in options:
            if report not in ('partner_ledger', 'aged_balance'):
                raise ToolError('invalid_params', "'partner_type' is only for partner_ledger and aged_balance")
            if report == 'aged_balance' and options['partner_type'] == 'both':
                raise ToolError('invalid_params', "The aged balance is either 'receivable' or 'payable'")
        if options.get('search') and report not in LEDGERS:
            raise ToolError('invalid_params', "'search' is only for general_ledger, partner_ledger and aged_balance")
        return options

    @api.model
    def _render_report_pdf(self, engine, report, options):
        """Render the printed report like the web client does with the print action."""
        action = engine.action_print_pdf(report, options)
        # the action data travels as JSON in the web client: dates become strings
        data = json.loads(json.dumps(action.get('data'), default=str))
        # keep the action context: the aged balance reads its wizard from active_model / active_id;
        # force_report_rendering: a real PDF even when Odoo runs with tests enabled
        context = dict(action.get('context') or {}, force_report_rendering=True)
        docids = (data or {}).get('ids') or context.get('active_ids') or []
        content, _format = engine.env['ir.actions.report'].with_context(context)._render_qweb_pdf(
            action['report_name'], docids, data=data)
        return content

    @agent_tool(
        name='export_report',
        title='Export accounting report', title_ar='تصدير تقرير محاسبي',
        category='accounting', requires=('account.dynamic.report',),
        description="""
            Make the PDF or Excel (XLSX) file of an accounting report from DoniaLink Accounting, the
            same file a user gets from the Print / Export buttons, with the same figures as the
            client's printed reports. Use it when the user wants a document to download, print,
            send to an accountant or open in Excel; to answer questions about figures, read the data
            tools instead (profit_and_loss, balance_sheet, trial_balance, vendor_balances).
            Reports: profit_and_loss (revenue, costs, expenses and net profit of a period);
            balance_sheet (assets, liabilities and equity as of a date); trial_balance (initial
            balance, debit, credit and end balance per account for a period); general_ledger (every
            journal item per account, with running balance); partner_ledger (journal items per
            customer / supplier); aged_balance (open customer or supplier amounts by age: not due,
            1-30, 31-60, 61-90, 91-120 days, older, as of a date).
            Options that matter: date_filter or date_from / date_to for the period, target_move,
            comparison (P&L, balance sheet), partner_type (partner ledger, aged balance), search
            (ledgers). Labels are in Arabic when lang=ar. The result has file.download_url: give it
            to the user; downloading needs the same agent key and the link is valid 24 hours
            (file.expires_at, UTC). options in the result are the options actually used (resolved period).
        """,
        params={'type': 'object', 'required': ['report'], 'properties': {
            'report': {'type': 'string', 'enum': EXPORT_REPORTS},
            'format': {'type': 'string', 'enum': ['pdf', 'xlsx'], 'default': 'xlsx',
                       'description': "'xlsx' (Excel, ledgers fully unfolded) or 'pdf' (the printed report)."},
            'options': OPTIONS,
            'company_id': {'type': 'integer', 'description': 'Company of the report (see instance_info). '
                                                             "Default: the agent user's company."},
        }},
    )
    def _tool_export_report(self, report, format='xlsx', options=None, company_id=None):
        company = self.env['res.company'].sudo().browse(company_id) if company_id else self.env.company
        if not company.exists():
            raise ToolError('invalid_params', f'Unknown company {company_id}')
        options = self._check_export_options(report, options or {})
        # As the superuser: the engine checks the accounting groups of the current user, which the
        # agent user does not have (sudo() keeps the user). Business timezone for "today".
        engine = self.env['account.dynamic.report'].with_user(SUPERUSER_ID).with_company(company).with_context(
            tz=self._business_tz())
        used = engine.get_options(report, options)
        if format == 'pdf':
            content = self._render_report_pdf(engine, report, options)
        else:
            content = engine.get_xlsx(report, options)
        title = self._export_titles()[report]
        period = used['date_to'] if not used.get('date_from') else f"{used['date_from']} - {used['date_to']}"
        return {
            'report': report,
            'title': title,
            'format': format,
            'company': {'id': company.id, 'name': company.name},
            'options': {k: v for k, v in used.items() if k not in ('report_key', 'company_id', 'unfold_all')},
            FILE_KEY: {
                'name': f'{title} {period}.{format}',
                'mimetype': MIMETYPES[format],
                'content': content,
            },
        }
