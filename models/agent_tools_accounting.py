# -*- coding: utf-8 -*-
"""Accounting tools: profit and loss, balance sheet, trial balance, partner balances,
point-of-sale vs accounting sales.

The profit and loss and the balance sheet come from DoniaLink Accounting
(``base_accounting_kit``, 18.0.5.1.0+) when installed, so the agent gives exactly the
figures clients print. Without it, they fall back to journal items grouped by Odoo's
standard account types (same structure; ``engine`` says which one answered).
The trial balance uses the kit's layout (debit, credit and balance of the period).
"""
from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models
from odoo.tools import float_round

from .agent_registry import ToolError, agent_tool

DATE_FROM = {'type': 'string', 'description': 'First day, YYYY-MM-DD. Default: first day of the month of date_to.'}
DATE_TO = {'type': 'string', 'description': 'Last day included, YYYY-MM-DD. Default: today.'}
COMPANY_ID = {'type': 'integer', 'description': 'Only this company (see instance_info). Default: all companies.'}
TARGET = {'type': 'string', 'enum': ['posted', 'all'], 'default': 'posted',
          'description': "'posted' = posted entries only (as accountants report); 'all' = include drafts."}

ASSET_TYPES = ('asset_receivable', 'asset_cash', 'asset_current', 'asset_non_current', 'asset_prepayments',
               'asset_fixed')
LIABILITY_TYPES = ('liability_payable', 'liability_credit_card', 'liability_current', 'liability_non_current')
EQUITY_TYPES = ('equity', 'equity_unaffected')
PL_SECTIONS = [
    # (key, title, account types, sign: +1 = income shown positive, -1 = expense shown positive)
    ('revenue', 'Revenue', ('income',), 1),
    ('cost_of_revenue', 'Cost of revenue', ('expense_direct_cost',), -1),
    ('other_income', 'Other income', ('income_other',), 1),
    ('expenses', 'Operating expenses', ('expense',), -1),
    ('depreciation', 'Depreciation', ('expense_depreciation',), -1),
]
PL_TYPES = tuple(t for _k, _t, types, _s in PL_SECTIONS for t in types)
# A café's account cannot move this much money: such turnover comes from a data error
# (e.g. a stock move with a wrong quantity), even when the balance looks normal.
IMPLAUSIBLE_TURNOVER = 1e12


def _r(value, digits=2):
    return float_round(value or 0.0, precision_digits=digits)


def _pct(part, total):
    return _r(part / total * 100) if total else None


class AgentTools(models.AbstractModel):
    _inherit = 'donialink.agent.tools'

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @api.model
    def _accounting_period(self, date_from, date_to):
        last = self._parse_date(date_to, 'date_to') if date_to else fields.Date.context_today(self)
        first = self._parse_date(date_from, 'date_from') if date_from else last.replace(day=1)
        if first > last:
            raise ToolError('invalid_params', "'date_from' must be before 'date_to'")
        return first, last

    @api.model
    def _move_line_domain(self, company_id=None, target='posted'):
        domain = [('display_type', 'not in', ('line_section', 'line_note'))]
        domain.append(('parent_state', '=', 'posted') if target == 'posted' else ('parent_state', '!=', 'cancel'))
        if company_id:
            domain.append(('company_id', '=', company_id))
        return domain

    @api.model
    def _account_totals(self, domain):
        """{account: {'debit', 'credit', 'balance'}} of the journal items matching ``domain``."""
        result = {}
        for account, debit, credit, balance in self.env['account.move.line'].sudo()._read_group(
                domain, ['account_id'], ['debit:sum', 'credit:sum', 'balance:sum']):
            result[account] = {'debit': debit, 'credit': credit, 'balance': balance}
        return result

    @api.model
    def _account_row(self, account, amount, **extra):
        return dict({'code': account.code, 'account': account.name, 'amount': _r(amount)}, **extra)

    @api.model
    def _type_labels(self):
        return dict(self.env['account.account']._fields['account_type']._description_selection(self.env))

    # ------------------------------------------------------------------
    # DoniaLink Accounting (base_accounting_kit) engine
    # ------------------------------------------------------------------

    @api.model
    def _kit_lines(self, tree, first, last, company_id, target):
        """Lines of a DoniaLink Accounting financial report, exactly as printed.

        Returns ({report xmlid: line}, [all lines]) or None when the kit is not installed
        or its standard report tree was removed. The wizard record is rolled back with
        the read tool.
        """
        if 'financial.report' not in self.env:
            return None
        report = self.env(su=True).ref(f'base_accounting_kit.{tree}', raise_if_not_found=False)
        if not report:
            return None
        company = self.env['res.company'].browse(company_id) if company_id else self.env.company
        Wizard = self.env['financial.report'].sudo().with_company(company)
        wizard = Wizard.create({
            'account_report_id': report.id, 'date_from': first, 'date_to': last,
            'target_move': 'posted' if target == 'posted' else 'all', 'company_id': company.id,
        })
        form = wizard._apply_cumulative(wizard.read(
            ['date_from', 'enable_filter', 'debit_credit', 'date_to', 'account_report_id',
             'target_move', 'view_format', 'company_id'])[0])
        form['used_context'] = wizard._build_contexts({'form': form})
        lines = wizard.get_account_lines(form)
        xmlids = {
            r.id: xid for r, xid in
            ((r, r.get_external_id().get(r.id, '')) for r in report._get_children_by_order())
        }
        by_xmlid = {xmlids.get(line['r_id'], '').split('.')[-1]: line for line in lines if line['type'] == 'report'}
        return by_xmlid, lines

    @api.model
    def _kit_accounts(self, lines, section_line, sign=1):
        """Account lines directly under a report line, as [{code, account, amount}]."""
        rows = []
        for line in lines:
            if line['type'] == 'account' and line['parent'] == section_line['id']:
                account = self.env['account.account'].sudo().browse(line['account'])
                rows.append(self._account_row(account, sign * line['balance']))
        return sorted(rows, key=lambda r: r['code'] or '')

    @api.model
    def _kit_profit_and_loss(self, first, last, company_id, target, detail):
        kit = self._kit_lines('account_financial_report_profitandloss0', first, last, company_id, target)
        sections_map = [
            # key, kit line, sign to show the amount positive
            ('revenue', 'account_financial_report_operating_income0', 1),
            ('cost_of_revenue', 'financial_report_cost_of_revenue', -1),
            ('other_income', 'account_financial_report_other_income0', 1),
            ('expenses', 'financial_report_operating_expenses', 1),
            ('depreciation', 'financial_report_depreciation', 1),
        ]
        if not kit or any(xid not in kit[0] for _k, xid, _s in sections_map):
            return None
        by_xmlid, lines = kit
        titles = dict((key, title) for key, title, _t, _s in PL_SECTIONS)
        revenue = by_xmlid['account_financial_report_operating_income0']['balance']
        sections = []
        for key, xid, sign in sections_map:
            line = by_xmlid[xid]
            section = {'key': key, 'title': line['name'] or titles[key], 'amount': _r(sign * line['balance']),
                       'pct_of_revenue': _pct(sign * line['balance'], revenue)}
            if detail:
                section['accounts'] = [dict(row, pct_of_revenue=_pct(row['amount'], revenue))
                                       for row in self._kit_accounts(lines, line, sign)]
            sections.append(section)
        gross = by_xmlid['financial_report_gross_profit']['balance']
        net = by_xmlid['account_financial_report_profitandloss0']['balance']
        return {
            'engine': 'base_accounting_kit',
            'period': {'from': str(first), 'to': str(last)},
            'sections': sections,
            'revenue': _r(revenue),
            'gross_profit': _r(gross),
            'gross_margin_pct': _pct(gross, revenue),
            'net_profit': _r(net),
            'net_margin_pct': _pct(net, revenue),
        }

    @api.model
    def _kit_balance_sheet(self, as_of, company_id, target, detail):
        kit = self._kit_lines('account_financial_report_balancesheet0', False, as_of, company_id, target)
        tops = ('account_financial_report_assets0', 'account_financial_report_liabilitysum0',
                'financial_report_equity_sum', 'account_financial_report_profitloss_toreport0')
        if not kit or any(xid not in kit[0] for xid in tops):
            return None
        by_xmlid, lines = kit
        children = defaultdict(list)
        for line in lines:
            if line['type'] == 'report' and line['parent']:
                children[line['parent']].append(line)
        earnings_line = by_xmlid['account_financial_report_profitloss_toreport0']

        def section(xid):
            top = by_xmlid[xid]
            groups = []
            for child in children[top['id']]:
                if child is earnings_line or not child['balance']:
                    continue
                group = {'type': child['name'], 'amount': _r(child['balance'])}
                if detail:
                    group['accounts'] = self._kit_accounts(lines, child)
                groups.append(group)
            return {'amount': _r(sum(g['amount'] for g in groups)), 'groups': groups}

        assets = section('account_financial_report_assets0')
        liabilities = section('account_financial_report_liabilitysum0')
        equity = section('financial_report_equity_sum')
        earnings = earnings_line['balance']
        return {
            'engine': 'base_accounting_kit',
            'date': str(as_of),
            'assets': assets,
            'liabilities': liabilities,
            'equity': equity,
            'unallocated_earnings': _r(earnings),
            'liabilities_and_equity': _r(liabilities['amount'] + equity['amount'] + earnings),
            'check': _r(assets['amount'] - liabilities['amount'] - equity['amount'] - earnings),
        }

    # ------------------------------------------------------------------
    # Profit and loss
    # ------------------------------------------------------------------

    @api.model
    def _profit_and_loss(self, first, last, company_id, target, detail):
        domain = self._move_line_domain(company_id, target) + [
            ('date', '>=', first), ('date', '<=', last), ('account_id.account_type', 'in', PL_TYPES)]
        totals = self._account_totals(domain)
        sections, amounts = [], {}
        for key, title, types, sign in PL_SECTIONS:
            rows = [(account, sign * -values['balance']) for account, values in totals.items()
                    if account.account_type in types]
            amount = sum(a for _acc, a in rows)
            amounts[key] = amount
            section = {'key': key, 'title': title, 'amount': _r(amount)}
            if detail:
                section['accounts'] = [self._account_row(acc, a) for acc, a in
                                       sorted(rows, key=lambda r: r[0].code or '') if a]
            sections.append(section)
        revenue = amounts['revenue']
        gross = revenue - amounts['cost_of_revenue']
        net = gross + amounts['other_income'] - amounts['expenses'] - amounts['depreciation']
        for section in sections:
            section['pct_of_revenue'] = _pct(section['amount'], revenue)
            for row in section.get('accounts', []):
                row['pct_of_revenue'] = _pct(row['amount'], revenue)
        return {
            'engine': 'account_types',
            'period': {'from': str(first), 'to': str(last)},
            'sections': sections,
            'revenue': _r(revenue),
            'gross_profit': _r(gross),
            'gross_margin_pct': _pct(gross, revenue),
            'net_profit': _r(net),
            'net_margin_pct': _pct(net, revenue),
        }

    @agent_tool(
        name='profit_and_loss',
        title='Profit and loss', title_ar='قائمة الأرباح والخسائر',
        category='accounting', requires=('account.move.line',),
        description="""
            Profit and loss statement from posted journal entries for a period: revenue, cost of
            revenue (cost of goods sold), gross profit, other income, operating expenses (salaries,
            rent, utilities...), depreciation and net profit, each with its accounts and % of revenue.
            Optionally compared with the previous period or the same period last year. Amounts are
            positive for both income and expenses; net_profit is negative for a loss.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID, 'target_moves': TARGET,
            'detail': {'type': 'boolean', 'default': True, 'description': 'List the accounts of each section.'},
            'compare': {'type': 'string', 'enum': ['none', 'previous_period', 'previous_year'], 'default': 'none'},
        }},
    )
    def _tool_profit_and_loss(self, date_from=None, date_to=None, company_id=None, target_moves='posted',
                              detail=True, compare='none'):
        first, last = self._accounting_period(date_from, date_to)
        result = self._kit_profit_and_loss(first, last, company_id, target_moves, detail) \
            or self._profit_and_loss(first, last, company_id, target_moves, detail)
        result['currency'] = self._currency_name(company_id)
        if compare != 'none':
            if compare == 'previous_period':
                days = (last - first).days + 1
                c_first, c_last = first - timedelta(days=days), first - timedelta(days=1)
            else:
                c_first, c_last = first.replace(year=first.year - 1), last.replace(year=last.year - 1)
            previous = self._kit_profit_and_loss(c_first, c_last, company_id, target_moves, False) \
                or self._profit_and_loss(c_first, c_last, company_id, target_moves, False)
            result['comparison'] = {
                'period': previous['period'],
                'revenue': previous['revenue'],
                'gross_profit': previous['gross_profit'],
                'net_profit': previous['net_profit'],
                'sections': [{k: s[k] for k in ('key', 'amount')} for s in previous['sections']],
                'revenue_change_pct': _pct(result['revenue'] - previous['revenue'], abs(previous['revenue'])),
                'net_profit_change': _r(result['net_profit'] - previous['net_profit']),
            }
        return result

    # ------------------------------------------------------------------
    # Balance sheet
    # ------------------------------------------------------------------

    @agent_tool(
        name='balance_sheet',
        title='Balance sheet', title_ar='المركز المالي',
        category='accounting', requires=('account.move.line',),
        description="""
            Balance sheet at a date, cumulative from the first entry: assets (cash and bank,
            receivables, stock, fixed assets...), liabilities (suppliers, employees payable, taxes...),
            equity and the profit or loss not yet closed into equity. Grouped by account type with
            accounts. 'check' must be 0: assets = liabilities + equity + earnings.
        """,
        params={'type': 'object', 'properties': {
            'date': {'type': 'string', 'description': 'Balance at the end of this day, YYYY-MM-DD. Default: today.'},
            'company_id': COMPANY_ID, 'target_moves': TARGET,
            'detail': {'type': 'boolean', 'default': True, 'description': 'List the accounts of each group.'},
        }},
    )
    def _tool_balance_sheet(self, date=None, company_id=None, target_moves='posted', detail=True):
        as_of = self._parse_date(date, 'date') if date else fields.Date.context_today(self)
        kit = self._kit_balance_sheet(as_of, company_id, target_moves, detail)
        if kit:
            return dict(kit, currency=self._currency_name(company_id))
        domain = self._move_line_domain(company_id, target_moves) + [('date', '<=', as_of)]
        totals = self._account_totals(domain)
        labels = self._type_labels()

        def section(types, sign):
            groups = []
            for account_type in types:
                rows = [(acc, sign * v['balance']) for acc, v in totals.items()
                        if acc.account_type == account_type and v['balance']]
                if not rows:
                    continue
                group = {'type': labels.get(account_type, account_type), 'amount': _r(sum(a for _x, a in rows))}
                if detail:
                    group['accounts'] = [self._account_row(acc, a) for acc, a in sorted(rows, key=lambda r: r[0].code or '')]
                groups.append(group)
            return {'amount': _r(sum(g['amount'] for g in groups)), 'groups': groups}

        assets = section(ASSET_TYPES, 1)
        liabilities = section(LIABILITY_TYPES, -1)
        equity = section(EQUITY_TYPES, -1)
        earnings = -sum(v['balance'] for acc, v in totals.items() if acc.account_type in PL_TYPES)
        return {
            'engine': 'account_types',
            'date': str(as_of),
            'currency': self._currency_name(company_id),
            'assets': assets,
            'liabilities': liabilities,
            'equity': equity,
            'unallocated_earnings': _r(earnings),
            'liabilities_and_equity': _r(liabilities['amount'] + equity['amount'] + earnings),
            'check': _r(assets['amount'] - liabilities['amount'] - equity['amount'] - earnings),
        }

    # ------------------------------------------------------------------
    # Trial balance
    # ------------------------------------------------------------------

    @agent_tool(
        name='trial_balance',
        title='Trial balance', title_ar='ميزان المراجعة',
        category='accounting', requires=('account.move.line',),
        description="""
            Trial balance for a period: debit, credit and balance of every account with movements
            (same layout as the base_accounting_kit Trial Balance). With include_opening, also the
            opening balance before the period and the ending balance. Accounts with implausible
            turnover (data errors such as a stock move with a wrong quantity) are flagged in
            'suspicious_accounts' even when their balance looks normal.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID, 'target_moves': TARGET,
            'include_opening': {'type': 'boolean', 'default': False},
            'account_types': {'type': 'string', 'enum': ['all', 'balance_sheet', 'profit_and_loss'], 'default': 'all'},
        }},
    )
    def _tool_trial_balance(self, date_from=None, date_to=None, company_id=None, target_moves='posted',
                            include_opening=False, account_types='all'):
        first, last = self._accounting_period(date_from, date_to)
        base = self._move_line_domain(company_id, target_moves)
        if account_types == 'profit_and_loss':
            base.append(('account_id.account_type', 'in', PL_TYPES))
        elif account_types == 'balance_sheet':
            base.append(('account_id.account_type', 'not in', PL_TYPES + ('off_balance',)))
        period = self._account_totals(base + [('date', '>=', first), ('date', '<=', last)])
        opening = self._account_totals(base + [('date', '<', first)]) if include_opening else {}
        labels = self._type_labels()
        accounts = sorted(set(period) | set(opening), key=lambda a: a.code or '')
        rows, suspicious = [], []
        for acc in accounts:
            p = period.get(acc, {'debit': 0.0, 'credit': 0.0, 'balance': 0.0})
            row = {'code': acc.code, 'account': acc.name, 'type': labels.get(acc.account_type),
                   'debit': _r(p['debit']), 'credit': _r(p['credit']), 'balance': _r(p['balance'])}
            if include_opening:
                o = opening.get(acc, {}).get('balance', 0.0)
                row['opening'] = _r(o)
                row['ending'] = _r(o + p['balance'])
            if max(p['debit'], p['credit']) >= IMPLAUSIBLE_TURNOVER:
                row['suspicious'] = True
                suspicious.append({'code': acc.code, 'account': acc.name, 'debit': _r(p['debit']),
                                   'credit': _r(p['credit']), 'balance': _r(p['balance'])})
            rows.append(row)
        return {
            'period': {'from': str(first), 'to': str(last)},
            'currency': self._currency_name(company_id),
            'accounts': len(rows),
            'total_debit': _r(sum(r['debit'] for r in rows)),
            'total_credit': _r(sum(r['credit'] for r in rows)),
            'suspicious_accounts': suspicious,
            'note': 'Balances of profit and loss accounts are not reset at year end in opening/ending.'
                    if include_opening else None,
            'list': rows,
        }

    # ------------------------------------------------------------------
    # Partner balances
    # ------------------------------------------------------------------

    @agent_tool(
        name='vendor_balances',
        title='Supplier balances', title_ar='أرصدة الموردين',
        category='accounting', requires=('account.move.line',),
        description="""
            What is owed to each supplier today (side='payable', default), or owed by each customer
            (side='receivable'), from open unreconciled journal items, with ageing by due date:
            not due, 1-30, 31-60, 61-90 and over 90 days overdue. A negative balance is an advance
            (we paid more than billed). Includes employee payables when they are on a payable account.
        """,
        params={'type': 'object', 'properties': {
            'side': {'type': 'string', 'enum': ['payable', 'receivable'], 'default': 'payable'},
            'company_id': COMPANY_ID,
            'search': {'type': 'string', 'maxLength': 100, 'description': 'Part of the partner name.'},
            'min_amount': {'type': 'number', 'minimum': 0, 'default': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 100},
        }},
    )
    def _tool_vendor_balances(self, side='payable', company_id=None, search=None, min_amount=0, limit=100):
        account_type = 'liability_payable' if side == 'payable' else 'asset_receivable'
        sign = -1 if side == 'payable' else 1
        domain = self._move_line_domain(company_id) + [
            ('account_id.account_type', '=', account_type), ('reconciled', '=', False),
            ('amount_residual', '!=', 0)]
        if search:
            domain.append(('partner_id.name', 'ilike', search))
        lines = self.env['account.move.line'].sudo().search(domain)
        today = fields.Date.context_today(self)
        buckets = ('not_due', 'd1_30', 'd31_60', 'd61_90', 'over_90')
        per_partner = defaultdict(lambda: dict({b: 0.0 for b in buckets}, total=0.0, items=0, oldest_due=None))
        for line in lines:
            amount = sign * line.amount_residual
            due = line.date_maturity or line.date
            late = (today - due).days
            bucket = 'not_due' if late <= 0 else 'd1_30' if late <= 30 else 'd31_60' if late <= 60 \
                else 'd61_90' if late <= 90 else 'over_90'
            item = per_partner[line.partner_id.name or '(no partner)']
            item[bucket] += amount
            item['total'] += amount
            item['items'] += 1
            if item['oldest_due'] is None or due < item['oldest_due']:
                item['oldest_due'] = due
        rows = [dict({b: _r(v[b]) for b in buckets}, partner=k, total=_r(v['total']), open_items=v['items'],
                     oldest_due=str(v['oldest_due']) if v['oldest_due'] else None)
                for k, v in per_partner.items() if abs(v['total']) >= min_amount]
        rows.sort(key=lambda r: -r['total'])
        return {
            'side': side,
            'date': str(today),
            'currency': self._currency_name(company_id),
            'partners': len(rows),
            'total': _r(sum(r['total'] for r in rows)),
            'overdue': _r(sum(r['total'] - r['not_due'] for r in rows)),
            'ageing_totals': {b: _r(sum(r[b] for r in rows)) for b in buckets},
            'list': rows[:limit],
        }

    @agent_tool(
        name='purchases_by_vendor',
        title='Purchases by supplier', title_ar='المشتريات حسب المورد',
        category='accounting', requires=('account.move',),
        description="""
            Posted vendor bills (minus vendor refunds) dated in a period, per supplier: number of bills,
            amount (tax included) and share of all purchases, largest first. Bills recorded through
            Expenses are vendor bills too, so they are included. Use it for "who do we buy from".
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID,
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 30},
        }},
    )
    def _tool_purchases_by_vendor(self, date_from=None, date_to=None, company_id=None, limit=30):
        first, last = self._accounting_period(date_from, date_to)
        domain = [('move_type', 'in', ('in_invoice', 'in_refund')), ('state', '=', 'posted'),
                  ('invoice_date', '>=', first), ('invoice_date', '<=', last)]
        if company_id:
            domain.append(('company_id', '=', company_id))
        bills = self.env['account.move'].sudo().search(domain)
        per_vendor = defaultdict(lambda: {'bills': 0, 'amount': 0.0})
        for bill in bills:
            item = per_vendor[bill.partner_id.name or '(no supplier)']
            item['bills'] += 1 if bill.move_type == 'in_invoice' else 0
            # amount_total_signed is negative for bills, positive for refunds
            item['amount'] += -bill.amount_total_signed
        total = sum(v['amount'] for v in per_vendor.values())
        rows = [{'vendor': k, 'bills': v['bills'], 'amount': _r(v['amount']), 'share_pct': _pct(v['amount'], total)}
                for k, v in sorted(per_vendor.items(), key=lambda kv: -kv[1]['amount'])]
        return {
            'period': {'from': str(first), 'to': str(last)},
            'currency': self._currency_name(company_id),
            'bills': sum(r['bills'] for r in rows),
            'vendors': len(rows),
            'total': _r(total),
            'list': rows[:limit],
        }

    # ------------------------------------------------------------------
    # POS vs accounting
    # ------------------------------------------------------------------

    @agent_tool(
        name='pos_vs_accounting_sales',
        title='POS vs accounting sales', title_ar='مطابقة مبيعات الكاشير مع الحسابات',
        category='accounting', requires=('account.move.line', 'pos.order'),
        description="""
            Reconciles point-of-sale sales (without tax) with the sales revenue posted in accounting
            for the same period, and explains the difference: sessions still open (not posted yet),
            sessions closed after the period (posted on their closing date), sessions of earlier
            days posted in the period, and cash differences posted on income accounts. 'unexplained'
            is what remains and needs an accountant's review.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID,
        }},
    )
    def _tool_pos_vs_accounting_sales(self, date_from=None, date_to=None, company_id=None):
        first, last = self._accounting_period(date_from, date_to)
        start, end, tz, _f, _l = self._local_range(str(first), str(last))
        sold = ('paid', 'done', 'invoiced')
        orders = self._pos_orders(start, end, sold, company_id)

        def untaxed(records):
            return sum(o.amount_total - o.amount_tax for o in records)

        pos_sales = untaxed(orders)
        # accounting: revenue lines of POS session entries and of invoices made from POS orders
        Line = self.env['account.move.line'].sudo()
        # cash differences are posted on the profit/loss accounts of the cash journals
        journals = self.env['pos.config'].sudo().search([]).journal_id             | self.env['pos.payment.method'].sudo().search([]).journal_id
        cash_diff_accounts = journals.profit_account_id | journals.loss_account_id
        pos_moves_domain = ['|', ('move_id.pos_session_ids', '!=', False),
                            ('move_id.pos_order_ids', '!=', False)] if 'pos_order_ids' in self.env['account.move']._fields \
            else [('move_id.pos_session_ids', '!=', False)]
        base = self._move_line_domain(company_id) + [
            ('date', '>=', first), ('date', '<=', last),
            ('account_id.account_type', 'in', ('income', 'income_other'))] + pos_moves_domain
        revenue_lines = Line.search(base)
        cash_diff_lines = revenue_lines.filtered(lambda l: l.account_id in cash_diff_accounts)
        accounting_sales = -sum((revenue_lines - cash_diff_lines).mapped('balance'))
        cash_diff_income = -sum(cash_diff_lines.mapped('balance'))

        # explanations
        open_orders = orders.filtered(lambda o: o.session_id.state != 'closed')
        closed = orders - open_orders
        closed_after = closed.filtered(lambda o: o.session_id.move_id and o.session_id.move_id.date > last)
        earlier = self._pos_orders(start - timedelta(days=62), start, sold, company_id).filtered(
            lambda o: o.session_id.move_id and first <= o.session_id.move_id.date <= last)
        explained = -untaxed(open_orders) - untaxed(closed_after) + untaxed(earlier)
        difference = accounting_sales - pos_sales
        return {
            'period': {'from': str(first), 'to': str(last), 'timezone': tz},
            'currency': self._currency_name(company_id),
            'pos_sales_untaxed': _r(pos_sales),
            'accounting_sales': _r(accounting_sales),
            'difference': _r(difference),
            'explained': {
                'open_sessions_not_posted': {'orders': len(open_orders), 'amount': _r(-untaxed(open_orders)),
                                             'sessions': open_orders.session_id.mapped('name')},
                'closed_after_period': {'orders': len(closed_after), 'amount': _r(-untaxed(closed_after)),
                                        'sessions': closed_after.session_id.mapped('name')},
                'earlier_orders_posted_in_period': {'orders': len(earlier), 'amount': _r(untaxed(earlier)),
                                                    'sessions': earlier.session_id.mapped('name')},
            },
            'cash_differences_on_income_accounts': _r(cash_diff_income),
            'unexplained': _r(difference - explained),
        }

    @agent_tool(
        name='stock_accounts_check',
        title='Stock accounts check', title_ar='فحص حسابات المخزون',
        category='accounting', requires=('account.move.line', 'stock.valuation.layer'),
        description="""
            Checks the stock accounts of automated (real-time) inventory valuation at the end of a period, in
            the company currency. The accounts are read from the product categories, not from codes.
            1) valuation: balance of the stock valuation account(s) vs the stock value (sum of the valuation
               layers); 'difference' should be about 0.
            2) incoming: balance of the stock input (interim received) account(s) + goods received not billed
               (confirmed purchase lines, (qty received - qty billed) x unit price) should be about 0; the
               account normally has a credit balance equal to what is received but not billed. Purchase lines
               whose received quantity is more than 10 times the ordered one are left out and listed as suspect
               (usually a unit of measure error).
            3) outgoing: balance of the stock output (interim delivered) account(s) should be about 0.
            Also explains the movements of the period on the incoming and outgoing accounts by kind (receipts,
            vendor bills, point of sale, deliveries, other) and lists the largest 'other' entries (stock count
            updates, miscellaneous entries): those usually cause the residuals.
        """,
        params={'type': 'object', 'properties': {
            'date_from': {'type': 'string',
                          'description': 'First day of the period whose movements are explained. Default: first '
                                         'day of the month of date_to.'},
            'date_to': {'type': 'string', 'description': 'Balances at the end of this day. Default: today.'},
            'company_id': {'type': 'integer', 'description': "One company (see instance_info). Default: the agent "
                                                             "user's main company."},
            'top': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 10,
                    'description': "How many of the largest 'other' entries to list."},
        }},
    )
    def _tool_stock_accounts_check(self, date_from=None, date_to=None, company_id=None, top=10):
        first, last = self._accounting_period(date_from, date_to)
        company = self.env['res.company'].sudo().browse(company_id).exists() if company_id else self.env.company
        if not company:
            raise ToolError('invalid_params', f'No company with id {company_id}')
        currency = company.currency_id
        categories = self.env['product.category'].sudo().with_company(company).search([]).filtered(
            lambda c: c.property_valuation == 'real_time')
        result = {
            'company': company.name,
            'as_of': str(last),
            'period': {'from': str(first), 'to': str(last)},
            'currency': currency.name,
        }
        if not categories:
            return dict(result, applicable=False,
                        note='No product category uses automated (real-time) valuation: there are no stock accounts.')
        valuation = categories.mapped('property_stock_valuation_account_id')
        incoming = categories.mapped('property_stock_account_input_categ_id')
        outgoing = categories.mapped('property_stock_account_output_categ_id')

        Line = self.env['account.move.line'].sudo()
        posted = [('parent_state', '=', 'posted'), ('company_id', '=', company.id)]

        def balance(accounts):
            if not accounts:
                return 0.0
            return sum(Line.search(posted + [('account_id', 'in', accounts.ids), ('date', '<=', last)])
                       .mapped('balance'))

        # stock value: valuation layers created up to the end of the day (local time)
        _start, end, _tz, _f, _l = self._local_range(str(last), str(last))
        layers = self.env['stock.valuation.layer'].sudo().search([
            ('company_id', '=', company.id), ('create_date', '<', end),
            ('product_id.categ_id', 'in', categories.ids)])
        stock_value = sum(layers.mapped('value'))

        received_not_billed = billed_not_received = 0.0
        suspect = []
        if 'purchase.order.line' in self.env:
            lines = self.env['purchase.order.line'].sudo().search([
                ('company_id', '=', company.id), ('state', 'in', ('purchase', 'done')),
                ('display_type', '=', False), ('product_id.categ_id', 'in', categories.ids)])
            for line in lines:
                pending = line.qty_received - line.qty_invoiced
                if not pending:
                    continue
                if line.product_qty and line.qty_received > 10 * line.product_qty:
                    suspect.append({
                        'order': line.order_id.name, 'product': line.product_id.display_name,
                        'ordered': line.product_qty, 'received': line.qty_received,
                        'billed': line.qty_invoiced, 'unit': line.product_uom.name})
                    continue
                price = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
                amount = line.currency_id._convert(pending * price, currency, company, last)
                if amount > 0:
                    received_not_billed += amount
                else:
                    billed_not_received -= amount

        valuation_balance, incoming_balance, outgoing_balance = (
            balance(valuation), balance(incoming), balance(outgoing))

        def kind_of(move):
            stock_move = move.stock_move_id if 'stock_move_id' in move._fields else move.browse()
            picking = stock_move.picking_id
            if move.move_type in ('in_invoice', 'in_refund'):
                return 'vendor_bills'
            if ('pos_session_ids' in move._fields and move.pos_session_ids) or (
                    picking and 'pos_session_id' in picking._fields and picking.pos_session_id):
                return 'point_of_sale'
            if stock_move and stock_move.picking_code == 'incoming':
                return 'receipts'
            if stock_move and stock_move.picking_code == 'outgoing':
                return 'deliveries'
            return 'other'

        def movements(accounts):
            by_kind, others = defaultdict(float), defaultdict(float)
            moves = {}
            if accounts:
                for line in Line.search(posted + [('account_id', 'in', accounts.ids),
                                                  ('date', '>=', first), ('date', '<=', last)]):
                    kind = kind_of(line.move_id)
                    by_kind[kind] += line.balance
                    if kind == 'other':
                        others[line.move_id] += line.balance
                        moves[line.move_id] = line.account_id
            return {k: _r(v) for k, v in by_kind.items()}, others, moves

        in_kinds, in_others, in_accounts = movements(incoming)
        out_kinds, out_others, out_accounts = movements(outgoing)
        other_entries = []
        for others, accounts in ((in_others, in_accounts), (out_others, out_accounts)):
            for move, amount in others.items():
                stock_move = move.stock_move_id if 'stock_move_id' in move._fields else move.browse()
                other_entries.append({
                    'entry': move.name, 'date': str(move.date),
                    'ref': (move.ref or stock_move.reference or stock_move.name or '').split('\n')[0],
                    'journal': move.journal_id.name,
                    'account': accounts[move].code, 'amount': _r(amount)})
        other_entries.sort(key=lambda e: -abs(e['amount']))

        def accounts_of(accounts):
            return [{'code': a.code, 'name': a.name} for a in accounts]

        return dict(result, **{
            'accounts': {'valuation': accounts_of(valuation), 'incoming': accounts_of(incoming),
                         'outgoing': accounts_of(outgoing)},
            'valuation': {'account_balance': _r(valuation_balance), 'stock_value': _r(stock_value),
                          'difference': _r(valuation_balance - stock_value)},
            'incoming': {'account_balance': _r(incoming_balance), 'received_not_billed': _r(received_not_billed),
                         'billed_not_received': _r(billed_not_received),
                         'residual': _r(incoming_balance + received_not_billed - billed_not_received),
                         'suspect_purchase_lines': suspect},
            'outgoing': {'account_balance': _r(outgoing_balance), 'residual': _r(outgoing_balance)},
            'period_movements': {'incoming': in_kinds, 'outgoing': out_kinds},
            'other_entries': other_entries[:top],
            'other_entries_count': len(other_entries),
            'note': 'Received-not-billed uses the quantities received and billed today, not at date_to.',
        })

    # ------------------------------------------------------------------
    # Domain note for AI models
    # ------------------------------------------------------------------

    @api.model
    def _agent_resources(self):
        resources = super()._agent_resources()
        if 'account.move.line' in self.env:
            resources.append({
                'name': 'accounting',
                'title': 'Accounting reports',
                'description': 'How the P&L, balance sheet, trial balance and balances are computed, how they '
                               'differ from base_accounting_kit reports, and when to export a report as PDF / XLSX.',
                'module': 'donialink_agent',
                'path': 'donialink_agent/agent/accounting.md',
            })
        return resources
