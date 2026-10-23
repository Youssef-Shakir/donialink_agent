# -*- coding: utf-8 -*-
"""Sales, cash and expense tools.

They read ``point_of_sale`` (orders, payments, sessions) and DoniaLink's
``expense_cash_control`` (expenses, bills, categorized cash in/out, cash
difference classification) when installed: each tool lists the models it
needs in ``requires`` and is hidden otherwise.

These are curated read tools: they read with sudo and return only the figures
listed in their description, whatever the agent user's own access rights.
"""
from collections import defaultdict

import pytz

from odoo import api, models
from odoo.tools import float_round

from .agent_registry import agent_tool

DATE_FROM = {'type': 'string', 'description': 'First day, YYYY-MM-DD (local time). Default: 29 days before date_to.'}
DATE_TO = {'type': 'string', 'description': 'Last day included, YYYY-MM-DD (local time). Default: today.'}
COMPANY_ID = {'type': 'integer', 'description': 'Only this company (see instance_info). Default: all companies.'}
POS_ID = {'type': 'integer', 'description': 'Only this point of sale (pos.config id). Default: all.'}
SOLD_STATES = ('paid', 'done', 'invoiced')


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
    def _pos_orders(self, start, end, states, company_id=None, pos_id=None):
        domain = [('date_order', '>=', start), ('date_order', '<', end), ('state', 'in', states)]
        if company_id:
            domain.append(('company_id', '=', company_id))
        if pos_id:
            domain.append(('config_id', '=', pos_id))
        return self.env['pos.order'].sudo().search(domain)

    @api.model
    def _cashier_name(self, order):
        """Who took the order: the POS employee (pos_hr) when used, else the user."""
        if 'employee_id' in order._fields and order.employee_id:
            return order.employee_id.name
        return order.user_id.name or '(unknown)'

    @api.model
    def _local_dt(self, value):
        tz = pytz.timezone(self._business_tz())
        return pytz.utc.localize(value).astimezone(tz)

    @api.model
    def _discount_products(self):
        """Products used for global discounts (pos_discount), as a product.product recordset."""
        Config = self.env['pos.config'].sudo()
        if 'discount_product_id' in Config._fields:
            return Config.search([]).discount_product_id
        return self.env['product.product']

    # ------------------------------------------------------------------
    # Sales
    # ------------------------------------------------------------------

    @agent_tool(
        name='sales_summary',
        title='Sales summary', title_ar='ملخص المبيعات',
        category='sales', requires=('pos.order',),
        description="""
            Point-of-sale sales over a period: number of orders, sales with and without tax,
            average ticket, refunds, cancelled orders (count and value, not included in sales) and
            payments by method (cash, card...). Grouped by day, hour of the day, weekday, product,
            POS category, payment method, cashier or point of sale. Days and hours are local time.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID, 'pos_id': POS_ID,
            'group_by': {'type': 'string', 'default': 'day',
                         'enum': ['day', 'hour', 'weekday', 'product', 'category', 'payment_method',
                                  'cashier', 'point_of_sale', 'none']},
            'top': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 50,
                    'description': 'Rows to return when grouping by product, category or cashier.'},
        }},
    )
    def _tool_sales_summary(self, date_from=None, date_to=None, company_id=None, pos_id=None, group_by='day', top=50):
        start, end, tz, first, last = self._local_range(date_from, date_to)
        orders = self._pos_orders(start, end, SOLD_STATES, company_id, pos_id)
        cancelled = self._pos_orders(start, end, ('cancel',), company_id, pos_id)
        refunds = orders.filtered(lambda o: o.amount_total < 0)
        total = sum(orders.mapped('amount_total'))
        tax = sum(orders.mapped('amount_tax'))
        payments = defaultdict(lambda: {'amount': 0.0, 'orders': set()})
        for payment in orders.payment_ids:
            item = payments[payment.payment_method_id.name]
            item['amount'] += payment.amount
            item['orders'].add(payment.pos_order_id.id)
        result = {
            'period': {'from': str(first), 'to': str(last), 'timezone': tz},
            'currency': self.env.company.currency_id.name,
            'orders': len(orders),
            'sales_total': _r(total),
            'sales_untaxed': _r(total - tax),
            'tax': _r(tax),
            'average_ticket': _r(total / len(orders)) if orders else None,
            'refunds': {'orders': len(refunds), 'amount': _r(sum(refunds.mapped('amount_total')))},
            'cancelled': {'orders': len(cancelled), 'amount': _r(sum(cancelled.mapped('amount_total'))),
                          'pct_of_entered_value': _pct(sum(cancelled.mapped('amount_total')),
                                                       total + sum(cancelled.mapped('amount_total')))},
            'payment_methods': [{'method': k, 'amount': _r(v['amount']), 'orders': len(v['orders']),
                                 'share_pct': _pct(v['amount'], total)}
                                for k, v in sorted(payments.items(), key=lambda kv: -kv[1]['amount'])],
        }
        if group_by == 'none':
            return result
        groups = defaultdict(lambda: {'orders': set(), 'quantity': 0.0, 'amount': 0.0})
        if group_by in ('day', 'hour', 'weekday', 'cashier', 'point_of_sale'):
            for order in orders:
                local = self._local_dt(order.date_order)
                key = {
                    'day': lambda: local.strftime('%Y-%m-%d'),
                    'hour': lambda: f'{local.hour:02d}:00',
                    'weekday': lambda: local.strftime('%u %A'),
                    'cashier': lambda: self._cashier_name(order),
                    'point_of_sale': lambda: order.config_id.name,
                }[group_by]()
                groups[key]['orders'].add(order.id)
                groups[key]['amount'] += order.amount_total
        elif group_by == 'payment_method':
            result['groups'] = result['payment_methods']
            return result
        else:  # product, category: order lines, tax included
            for line in orders.lines:
                if group_by == 'product':
                    keys = [line.product_id.display_name]
                else:
                    keys = line.product_id.pos_categ_ids.mapped('name') or ['(no category)']
                for key in keys[:1]:
                    groups[key]['orders'].add(line.order_id.id)
                    groups[key]['quantity'] += line.qty
                    groups[key]['amount'] += line.price_subtotal_incl
        rows = [{'key': k, 'orders': len(v['orders']), 'amount': _r(v['amount']), 'share_pct': _pct(v['amount'], total),
                 **({'quantity': _r(v['quantity'], 3)} if group_by in ('product', 'category') else {})}
                for k, v in groups.items()]
        if group_by in ('day', 'hour', 'weekday'):
            rows.sort(key=lambda r: r['key'])
        else:
            rows.sort(key=lambda r: -r['amount'])
            rows = rows[:top]
        result['group_by'] = group_by
        result['groups'] = rows
        return result

    @agent_tool(
        name='cancelled_orders',
        title='Cancelled orders', title_ar='الطلبات الملغاة',
        category='sales', requires=('pos.order',),
        description="""
            Orders entered at the point of sale and then cancelled before payment: count, value,
            items, share of all entered orders, grouped by the cashier who handled them, by product
            or by day. A few cancellations are normal (input mistakes); many cancellations by the
            same cashier, or of items already prepared, are a control issue worth reviewing.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID, 'pos_id': POS_ID,
            'group_by': {'type': 'string', 'enum': ['cashier', 'product', 'day'], 'default': 'cashier'},
            'top': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 30},
        }},
    )
    def _tool_cancelled_orders(self, date_from=None, date_to=None, company_id=None, pos_id=None,
                               group_by='cashier', top=30):
        start, end, tz, first, last = self._local_range(date_from, date_to)
        cancelled = self._pos_orders(start, end, ('cancel',), company_id, pos_id)
        sold = self._pos_orders(start, end, SOLD_STATES, company_id, pos_id)
        value = sum(cancelled.mapped('amount_total'))
        entered = value + sum(sold.mapped('amount_total'))
        groups = defaultdict(lambda: {'orders': set(), 'items': 0.0, 'amount': 0.0})
        for line in cancelled.lines:
            order = line.order_id
            key = {
                'cashier': lambda: self._cashier_name(order),
                'product': lambda: line.product_id.display_name,
                'day': lambda: self._local_dt(order.date_order).strftime('%Y-%m-%d'),
            }[group_by]()
            groups[key]['orders'].add(order.id)
            groups[key]['items'] += line.qty
            groups[key]['amount'] += line.price_subtotal_incl
        rows = [{'key': k, 'orders': len(v['orders']), 'items': _r(v['items'], 3), 'amount': _r(v['amount']),
                 'share_pct': _pct(v['amount'], value)} for k, v in groups.items()]
        rows.sort(key=(lambda r: r['key']) if group_by == 'day' else (lambda r: -r['amount']))
        return {
            'period': {'from': str(first), 'to': str(last), 'timezone': tz},
            'currency': self.env.company.currency_id.name,
            'cancelled_orders': len(cancelled),
            'entered_orders': len(cancelled) + len(sold),
            'cancelled_amount': _r(value),
            'cancelled_items': _r(sum(cancelled.lines.mapped('qty')), 3),
            'pct_of_entered_value': _pct(value, entered),
            'group_by': group_by,
            'groups': rows if group_by == 'day' else rows[:top],
        }

    @agent_tool(
        name='discounts_summary',
        title='Discounts', title_ar='الخصومات',
        category='sales', requires=('pos.order',),
        description="""
            Discounts given at the point of sale over a period: discounted orders, total discount
            amount and its share of sales before discount, grouped by cashier, product or day.
            Counts both line discounts (%) and global discounts (discount product). Amounts include tax.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID, 'pos_id': POS_ID,
            'group_by': {'type': 'string', 'enum': ['cashier', 'product', 'day'], 'default': 'cashier'},
            'top': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 30},
        }},
    )
    def _tool_discounts_summary(self, date_from=None, date_to=None, company_id=None, pos_id=None,
                                group_by='cashier', top=30):
        start, end, tz, first, last = self._local_range(date_from, date_to)
        orders = self._pos_orders(start, end, SOLD_STATES, company_id, pos_id)
        discount_products = self._discount_products()
        groups = defaultdict(lambda: {'orders': set(), 'amount': 0.0})
        discounted_orders = set()
        total_discount = 0.0
        for line in orders.lines:
            if line.qty < 0:
                continue  # refund lines: the discount was counted when the item was sold
            if line.product_id in discount_products:
                amount = -line.price_subtotal_incl  # global discount line is negative
                product_key = '(global discount)'
            elif line.discount:
                # price before discount, tax included, minus price after discount
                full = line.price_subtotal_incl / (1 - line.discount / 100) if line.discount < 100 \
                    else line.price_unit * line.qty
                amount = full - line.price_subtotal_incl
                product_key = line.product_id.display_name
            else:
                continue
            if not amount:
                continue
            order = line.order_id
            key = {
                'cashier': lambda: self._cashier_name(order),
                'product': lambda: product_key,
                'day': lambda: self._local_dt(order.date_order).strftime('%Y-%m-%d'),
            }[group_by]()
            groups[key]['orders'].add(order.id)
            groups[key]['amount'] += amount
            discounted_orders.add(order.id)
            total_discount += amount
        sales = sum(orders.mapped('amount_total'))
        rows = [{'key': k, 'orders': len(v['orders']), 'discount': _r(v['amount']),
                 'share_pct': _pct(v['amount'], total_discount)} for k, v in groups.items()]
        rows.sort(key=(lambda r: r['key']) if group_by == 'day' else (lambda r: -r['discount']))
        return {
            'period': {'from': str(first), 'to': str(last), 'timezone': tz},
            'currency': self.env.company.currency_id.name,
            'orders': len(orders),
            'discounted_orders': len(discounted_orders),
            'discount_total': _r(total_discount),
            'sales_before_discount': _r(sales + total_discount),
            'discount_pct_of_sales_before_discount': _pct(total_discount, sales + total_discount),
            'group_by': group_by,
            'groups': rows if group_by == 'day' else rows[:top],
        }

    # ------------------------------------------------------------------
    # Sessions and cash
    # ------------------------------------------------------------------

    @agent_tool(
        name='pos_sessions',
        title='POS sessions', title_ar='جلسات الكاشير',
        category='cash', requires=('pos.session',),
        description="""
            Point-of-sale sessions (cash register shifts) opened in a period: point of sale,
            cashier, opening and closing time (local), state, number of orders, sales, and the
            cash check at closing: expected cash, counted cash and difference (negative = shortage).
            When the Expenses & Cash Control module is installed, also the review state and the
            cash in/out still to classify.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID, 'pos_id': POS_ID,
            'state': {'type': 'string', 'enum': ['all', 'open', 'closed'], 'default': 'all'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 100},
        }},
    )
    def _tool_pos_sessions(self, date_from=None, date_to=None, company_id=None, pos_id=None, state='all', limit=100):
        start, end, tz, first, last = self._local_range(date_from, date_to)
        Session = self.env['pos.session'].sudo()
        domain = [('start_at', '>=', start), ('start_at', '<', end)]
        if state == 'open':
            domain.append(('state', '!=', 'closed'))
        elif state == 'closed':
            domain.append(('state', '=', 'closed'))
        if company_id:
            domain.append(('config_id.company_id', '=', company_id))
        if pos_id:
            domain.append(('config_id', '=', pos_id))
        sessions = Session.search(domain, order='start_at desc', limit=limit)
        states = dict(Session._fields['state']._description_selection(self.env))
        has_review = 'cash_review_state' in Session._fields
        rows = []
        for s in sessions:
            sold = s.order_ids.filtered(lambda o: o.state in SOLD_STATES)
            row = {
                'session': s.name,
                'point_of_sale': s.config_id.name,
                'opened_by': s.user_id.name,
                'opened_at': self._local_dt(s.start_at).strftime('%Y-%m-%d %H:%M') if s.start_at else None,
                'closed_at': self._local_dt(s.stop_at).strftime('%Y-%m-%d %H:%M') if s.stop_at else None,
                'state': states.get(s.state),
                'orders': len(sold),
                'sales_total': _r(sum(sold.mapped('amount_total'))),
            }
            if s.config_id.cash_control:
                row.update(
                    cash_opening=_r(s.cash_register_balance_start),
                    cash_expected=_r(s.cash_register_balance_end),
                    cash_counted=_r(s.cash_register_balance_end_real) if s.state == 'closed' else None,
                    cash_difference=_r(s.cash_register_difference) if s.state == 'closed' else None,
                )
            if has_review:
                row.update(
                    review_state=dict(Session._fields['cash_review_state']._description_selection(self.env)).get(
                        s.cash_review_state),
                    cash_moves_to_classify=s.cash_to_classify_count,
                )
            rows.append(row)
        return {'period': {'from': str(first), 'to': str(last), 'timezone': tz},
                'currency': self.env.company.currency_id.name, 'sessions': len(rows), 'list': rows}

    @agent_tool(
        name='pos_sessions_to_review',
        title='POS sessions to review', title_ar='جلسات الكاشير بانتظار المراجعة',
        category='cash', requires=('pos.session:cash_review_state',),
        description="""
            Closed point-of-sale sessions whose cash review (Expenses & Cash Control) is not done yet,
            oldest first: closing time (local), cashier, cash difference, cash in/out still to classify
            and cash differences still to classify. The team reviews each closed session; use it for
            "what is waiting for review".
        """,
        params={'type': 'object', 'properties': {
            'company_id': COMPANY_ID, 'pos_id': POS_ID,
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 50},
        }},
    )
    def _tool_pos_sessions_to_review(self, company_id=None, pos_id=None, limit=50):
        Session = self.env['pos.session'].sudo()
        domain = [('state', '=', 'closed'), ('cash_review_state', '=', 'to_review')]
        if company_id:
            domain.append(('config_id.company_id', '=', company_id))
        if pos_id:
            domain.append(('config_id', '=', pos_id))
        sessions = Session.search(domain, order='stop_at')
        local = lambda dt: self._local_dt(dt).strftime('%Y-%m-%d %H:%M') if dt else None  # noqa: E731
        return {
            'timezone': self._business_tz(),
            'currency': self.env.company.currency_id.name,
            'to_review': len(sessions),
            'oldest_closed_at': local(sessions[:1].stop_at) if sessions else None,
            'cash_moves_to_classify': sum(sessions.mapped('cash_to_classify_count')),
            'differences_to_classify': sum(sessions.mapped('cash_difference_pending_count')),
            'list': [{
                'session': s.name,
                'point_of_sale': s.config_id.name,
                'opened_by': s.user_id.name,
                'closed_at': local(s.stop_at),
                'cash_difference': _r(s.cash_register_difference) if s.config_id.cash_control else None,
                'cash_moves_to_classify': s.cash_to_classify_count,
                'differences_to_classify': s.cash_difference_pending_count,
            } for s in sessions[:limit]],
        }

    @agent_tool(
        name='cash_differences',
        title='Cash differences', title_ar='فروقات الكاش',
        category='cash', requires=('pos.session',),
        description="""
            Differences between the cash counted at closing and the cash expected by the system,
            for closed POS sessions in a period: total shortages (negative) and overages (positive),
            grouped by cashier, session or day. Repeated shortages by the same cashier need follow-up.
            When the Expenses & Cash Control module is installed, also how many differences are
            still pending classification.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID, 'pos_id': POS_ID,
            'group_by': {'type': 'string', 'enum': ['cashier', 'session', 'day'], 'default': 'cashier'},
        }},
    )
    def _tool_cash_differences(self, date_from=None, date_to=None, company_id=None, pos_id=None, group_by='cashier'):
        start, end, tz, first, last = self._local_range(date_from, date_to)
        domain = [('state', '=', 'closed'), ('stop_at', '>=', start), ('stop_at', '<', end)]
        if company_id:
            domain.append(('config_id.company_id', '=', company_id))
        if pos_id:
            domain.append(('config_id', '=', pos_id))
        # cash_control is computed, not stored: filter after the search
        sessions = self.env['pos.session'].sudo().search(domain, order='stop_at').filtered('config_id.cash_control')
        groups = defaultdict(lambda: {'sessions': 0, 'shortage': 0.0, 'overage': 0.0})
        shortage = overage = 0.0
        with_difference = 0
        for s in sessions:
            diff = s.cash_register_difference
            if not diff:
                continue
            with_difference += 1
            key = {'cashier': s.user_id.name, 'session': s.name,
                   'day': self._local_dt(s.stop_at).strftime('%Y-%m-%d')}[group_by]
            groups[key]['sessions'] += 1
            if diff < 0:
                groups[key]['shortage'] += diff
                shortage += diff
            else:
                groups[key]['overage'] += diff
                overage += diff
        rows = [{'key': k, 'sessions': v['sessions'], 'shortage': _r(v['shortage']), 'overage': _r(v['overage']),
                 'net': _r(v['shortage'] + v['overage'])} for k, v in groups.items()]
        rows.sort(key=(lambda r: r['key']) if group_by == 'day' else (lambda r: r['net']))
        result = {
            'period': {'from': str(first), 'to': str(last), 'timezone': tz},
            'currency': self.env.company.currency_id.name,
            'closed_sessions': len(sessions),
            'sessions_with_difference': with_difference,
            'total_shortage': _r(shortage),
            'total_overage': _r(overage),
            'net_difference': _r(shortage + overage),
            'group_by': group_by,
            'groups': rows,
        }
        # older modules (pos_cash_category) have this model without a session link
        Reconciliation = self.env.get('cash.difference.reconciliation')
        if Reconciliation is not None and 'session_id' in Reconciliation._fields:
            result['pending_classification'] = self.env['cash.difference.reconciliation'].sudo().search_count(
                [('state', '=', 'pending'), ('session_id', 'in', sessions.ids)])
        return result

    @agent_tool(
        name='cashier_payouts',
        title='Cash in / out', title_ar='مصروفات ومقبوضات الكاشير',
        category='cash', requires=('pos.cash.move',),
        description="""
            Money taken out of (or put into) the cash register during POS sessions: purchases,
            supplier payments, staff advances, salaries... Totals of cash out and cash in, grouped by
            expense category, partner (supplier or employee paid), cashier or day, and how many moves
            are still not classified. From the Expenses & Cash Control module.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID, 'pos_id': POS_ID,
            'direction': {'type': 'string', 'enum': ['out', 'in', 'all'], 'default': 'out'},
            'group_by': {'type': 'string', 'enum': ['category', 'partner', 'cashier', 'day'], 'default': 'category'},
            'top': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 50},
        }},
    )
    def _tool_cashier_payouts(self, date_from=None, date_to=None, company_id=None, pos_id=None,
                              direction='out', group_by='category', top=50):
        _start, _end, tz, first, last = self._local_range(date_from, date_to)
        Move = self.env['pos.cash.move'].sudo()
        domain = [('date', '>=', first), ('date', '<=', last)]
        if direction != 'all':
            domain.append(('move_type', '=', direction))
        if company_id:
            domain.append(('company_id', '=', company_id))
        if pos_id:
            domain.append(('config_id', '=', pos_id))
        moves = Move.search(domain)
        groups = defaultdict(lambda: {'moves': 0, 'cash_out': 0.0, 'cash_in': 0.0})
        for m in moves:
            if group_by == 'category':
                if m.state == 'to_classify':
                    key = '(not classified)'
                elif m.classification_type == 'partner':
                    key = '(partner payment)'
                else:
                    key = m.category_id.name or '(not classified)'
            elif group_by == 'partner':
                key = m.partner_id.name or '(no partner)'
            elif group_by == 'cashier':
                key = m.user_id.name or '(unknown)'
            else:
                key = str(m.date)
            groups[key]['moves'] += 1
            groups[key]['cash_out' if m.move_type == 'out' else 'cash_in'] += m.amount_abs
        rows = [{'key': k, 'moves': v['moves'], 'cash_out': _r(v['cash_out']), 'cash_in': _r(v['cash_in'])}
                for k, v in groups.items()]
        rows.sort(key=(lambda r: r['key']) if group_by == 'day' else (lambda r: -(r['cash_out'] + r['cash_in'])))
        return {
            'period': {'from': str(first), 'to': str(last)},
            'currency': self.env.company.currency_id.name,
            'moves': len(moves),
            'cash_out_total': _r(sum(moves.filtered(lambda m: m.move_type == 'out').mapped('amount_abs'))),
            'cash_in_total': _r(sum(moves.filtered(lambda m: m.move_type == 'in').mapped('amount_abs'))),
            'not_classified': len(moves.filtered(lambda m: m.state == 'to_classify')),
            'group_by': group_by,
            'groups': rows if group_by == 'day' else rows[:top],
        }

    # ------------------------------------------------------------------
    # Expenses and bills
    # ------------------------------------------------------------------

    @agent_tool(
        name='expenses_summary',
        title='Expenses', title_ar='المصاريف',
        category='expenses', requires=('expense.express:payment_mode',),
        description="""
            Expenses recorded in the Expenses & Cash Control module over a period (rent, utilities,
            gas, maintenance, supplies...): total, split between paid now and bills to pay later,
            grouped by category, supplier, day or month. Posted expenses only unless include_draft.
            Cash taken from the register is in cashier_payouts, not here.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID,
            'group_by': {'type': 'string', 'enum': ['category', 'partner', 'day', 'month'], 'default': 'category'},
            'include_draft': {'type': 'boolean', 'default': False},
            'top': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 50},
        }},
    )
    def _tool_expenses_summary(self, date_from=None, date_to=None, company_id=None, group_by='category',
                               include_draft=False, top=50):
        _start, _end, _tz, first, last = self._local_range(date_from, date_to)
        Expense = self.env['expense.express'].sudo()
        domain = [('date', '>=', first), ('date', '<=', last)] + self._company_domain('expense.express', company_id)
        if not include_draft:
            domain.append(('state', '=', 'posted'))
        expenses = Expense.search(domain)
        groups = defaultdict(lambda: {'count': 0, 'amount': 0.0})
        for e in expenses:
            key = {
                'category': lambda: e.category_id.name or '(no category)',
                'partner': lambda: e.partner_id.name or '(no supplier)',
                'day': lambda: str(e.date),
                'month': lambda: e.date.strftime('%Y-%m'),
            }[group_by]()
            groups[key]['count'] += 1
            groups[key]['amount'] += e.amount
        total = sum(expenses.mapped('amount'))
        rows = [{'key': k, 'count': v['count'], 'amount': _r(v['amount']), 'share_pct': _pct(v['amount'], total)}
                for k, v in groups.items()]
        rows.sort(key=(lambda r: r['key']) if group_by in ('day', 'month') else (lambda r: -r['amount']))
        return {
            'period': {'from': str(first), 'to': str(last)},
            'currency': self.env.company.currency_id.name,
            'expenses': len(expenses),
            'total': _r(total),
            'paid_now': _r(sum(expenses.filtered(lambda e: e.payment_mode == 'paid').mapped('amount'))),
            'to_pay_later': _r(sum(expenses.filtered(lambda e: e.payment_mode == 'to_pay').mapped('amount'))),
            'group_by': group_by,
            'groups': rows if group_by in ('day', 'month') else rows[:top],
        }

    @agent_tool(
        name='bills_to_pay',
        title='Bills to pay', title_ar='فواتير مستحقة الدفع',
        category='expenses', requires=('expense.express:payment_mode',),
        description="""
            Unpaid or partly paid bills recorded as 'to pay later' expenses: amount still due per
            bill and per supplier, due dates, overdue bills. Use it to answer "what do we owe and to
            whom". Purchase-order vendor bills are not included unless recorded through Expenses.
        """,
        params={'type': 'object', 'properties': {
            'company_id': COMPANY_ID,
            'only_overdue': {'type': 'boolean', 'default': False},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 100},
        }},
    )
    def _tool_bills_to_pay(self, company_id=None, only_overdue=False, limit=100):
        Expense = self.env['expense.express'].sudo()
        domain = [('payment_mode', '=', 'to_pay'), ('state', '=', 'posted'),
                  ('payment_state', 'in', ('not_paid', 'partial', 'in_payment'))]
        domain += self._company_domain('expense.express', company_id)
        bills = Expense.search(domain, order='due_date, date')
        if only_overdue:
            bills = bills.filtered('is_overdue')
        per_partner = defaultdict(lambda: {'bills': 0, 'due': 0.0})
        for b in bills:
            per_partner[b.partner_id.name or '(no supplier)']['bills'] += 1
            per_partner[b.partner_id.name or '(no supplier)']['due'] += b.amount_residual
        return {
            'currency': self.env.company.currency_id.name,
            'bills': len(bills),
            'total_due': _r(sum(bills.mapped('amount_residual'))),
            'overdue_count': len(bills.filtered('is_overdue')),
            'overdue_due': _r(sum(bills.filtered('is_overdue').mapped('amount_residual'))),
            'by_supplier': [{'supplier': k, 'bills': v['bills'], 'due': _r(v['due'])}
                            for k, v in sorted(per_partner.items(), key=lambda kv: -kv[1]['due'])],
            'list': [{
                'bill': b.name,
                'supplier': b.partner_id.name,
                'category': b.category_id.name,
                'date': str(b.date),
                'due_date': str(b.due_date) if b.due_date else None,
                'amount': _r(b.amount),
                'due': _r(b.amount_residual),
                'overdue': b.is_overdue,
            } for b in bills[:limit]],
        }

    # ------------------------------------------------------------------
    # Domain note for AI models
    # ------------------------------------------------------------------

    @api.model
    def _agent_resources(self):
        resources = super()._agent_resources()
        if 'pos.order' in self.env:
            resources.append({
                'name': 'sales_cash',
                'title': 'Sales, cash and expenses',
                'description': 'How POS orders, cancellations, discounts, sessions, cash differences, '
                               'cash in/out and expenses work, and how to read them.',
                'module': 'donialink_agent',
                'path': 'donialink_agent/agent/sales_cash.md',
            })
        return resources
