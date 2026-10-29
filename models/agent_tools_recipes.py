# -*- coding: utf-8 -*-
"""Recipe, food cost and stock tools.

They read ``pos_recipe_costing`` (recipes, stocktakes, waste, ingredient prices),
``stock`` and ``point_of_sale`` when those modules are installed: each tool
lists the models it needs in ``requires`` and is hidden otherwise.

These are curated read tools: they read with sudo and return only the figures
listed in their description, whatever the agent user's own access rights.
"""
from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models
from odoo.tools import float_round, html2plaintext

from .agent_registry import ToolError, agent_tool

DATE_FROM = {'type': 'string', 'description': 'First day, YYYY-MM-DD (local time). Default: 29 days before date_to.'}
DATE_TO = {'type': 'string', 'description': 'Last day included, YYYY-MM-DD (local time). Default: today.'}
COMPANY_ID = {'type': 'integer', 'description': 'Only this company (see instance_info). Default: all companies.'}
POS_SOLD_STATES = ('paid', 'done', 'invoiced')


def _r(value, digits=2):
    return float_round(value or 0.0, precision_digits=digits)


def _pct(part, total):
    return _r(part / total * 100) if total else None


class AgentTools(models.AbstractModel):
    _inherit = 'donialink.agent.tools'

    # ------------------------------------------------------------------
    # Recipes
    # ------------------------------------------------------------------

    @agent_tool(
        name='recipe_costs',
        title='Recipe costs', title_ar='تكاليف الوصفات',
        category='recipes', requires=('restaurant.recipe',),
        description="""
            Cost of every recipe (menu item) from its ingredients: cost per portion, selling price,
            food cost % (cost / price) and margin per portion, with a summary (average food cost,
            recipes without price or without ingredients). Use it to find the most expensive or least
            profitable items. Costs use the current ingredient costs. 'component' recipes are
            sub-recipes (sauces, doughs...) used inside other recipes and have no selling price.
        """,
        params={'type': 'object', 'properties': {
            'recipe_type': {'type': 'string', 'enum': ['dish', 'drink', 'dessert', 'component']},
            'search': {'type': 'string', 'maxLength': 100, 'description': 'Part of the recipe or menu item name.'},
            'min_food_cost_pct': {'type': 'number', 'minimum': 0,
                                  'description': 'Only recipes with a food cost % at least this high.'},
            'sort_by': {'type': 'string', 'default': 'food_cost_desc',
                        'enum': ['food_cost_desc', 'food_cost_asc', 'margin_asc', 'margin_desc', 'name']},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 50},
        }},
    )
    def _tool_recipe_costs(self, recipe_type=None, search=None, min_food_cost_pct=None,
                           sort_by='food_cost_desc', limit=50):
        Recipe = self.env['restaurant.recipe'].sudo()
        domain = []
        if recipe_type:
            domain.append(('recipe_type', '=', recipe_type))
        if search:
            domain += ['|', ('name', 'ilike', search), ('product_id.name', 'ilike', search)]
        recipes = Recipe.search(domain)
        types = dict(Recipe._fields['recipe_type']._description_selection(self.env))
        rows = [{
            'id': r.id,
            'recipe': r.name,
            'menu_item': r.product_id.display_name or None,
            'type': types.get(r.recipe_type),
            'portions': r.portion_size,
            'ingredients': r.ingredient_count,
            'cost_per_portion': _r(r.cost_per_portion),
            'selling_price': _r(r.selling_price),
            'food_cost_pct': _r(r.food_cost_percentage) if r.selling_price else None,
            'margin_per_portion': _r(r.profit_margin) if r.selling_price else None,
        } for r in recipes]
        priced = [row for row in rows if row['selling_price'] and row['cost_per_portion']]
        summary = {
            'recipes': len(rows),
            'average_food_cost_pct': _r(sum(r['food_cost_pct'] for r in priced) / len(priced)) if priced else None,
            'without_selling_price': sum(1 for r in rows if not r['selling_price'] and r['type'] != types.get('component')),
            'without_ingredients': sum(1 for r in rows if not r['ingredients']),
        }
        if min_food_cost_pct is not None:
            rows = [r for r in rows if (r['food_cost_pct'] or 0) >= min_food_cost_pct]
        sorters = {
            'food_cost_desc': (lambda r: r['food_cost_pct'] or 0, True),
            'food_cost_asc': (lambda r: r['food_cost_pct'] if r['food_cost_pct'] is not None else 1e9, False),
            'margin_asc': (lambda r: r['margin_per_portion'] if r['margin_per_portion'] is not None else 1e18, False),
            'margin_desc': (lambda r: r['margin_per_portion'] or 0, True),
            'name': (lambda r: r['recipe'].lower(), False),
        }
        key, reverse = sorters[sort_by]
        rows.sort(key=key, reverse=reverse)
        return {'summary': summary, 'returned': min(len(rows), limit), 'recipes': rows[:limit]}

    @agent_tool(
        name='recipe_card',
        title='Recipe card', title_ar='بطاقة الوصفة',
        category='recipes', requires=('restaurant.recipe',),
        description="""
            Full recipe of one menu item: each ingredient with quantity, unit, unit cost, cost and
            share of the recipe cost, plus portions, cost per portion, selling price and food cost %.
            Give recipe_id, or a name to search: when several recipes match, the candidates are
            returned instead so you can ask which one.
        """,
        params={'type': 'object', 'properties': {
            'recipe_id': {'type': 'integer'},
            'name': {'type': 'string', 'maxLength': 100, 'description': 'Recipe or menu item name (or part of it).'},
            'include_instructions': {'type': 'boolean', 'default': False},
        }},
    )
    def _tool_recipe_card(self, recipe_id=None, name=None, include_instructions=False):
        Recipe = self.env['restaurant.recipe'].sudo()
        if recipe_id:
            recipe = Recipe.browse(recipe_id).exists()
        elif name:
            recipe = Recipe.search(['|', ('name', '=ilike', name), ('product_id.name', '=ilike', name)], limit=1) \
                or Recipe.search(['|', ('name', 'ilike', name), ('product_id.name', 'ilike', name)], limit=20)
        else:
            raise ToolError('invalid_params', "Give 'recipe_id' or 'name'")
        if not recipe:
            raise ToolError('not_found', 'No recipe found', status=404)
        if len(recipe) > 1:
            return {'matches': [{'id': r.id, 'recipe': r.name, 'menu_item': r.product_id.display_name or None}
                                for r in recipe]}
        total = recipe.total_cost
        result = {
            'id': recipe.id,
            'recipe': recipe.name,
            'menu_item': recipe.product_id.display_name or None,
            'type': dict(Recipe._fields['recipe_type']._description_selection(self.env)).get(recipe.recipe_type),
            'portions': recipe.portion_size,
            'total_cost': _r(total),
            'cost_per_portion': _r(recipe.cost_per_portion),
            'selling_price': _r(recipe.selling_price),
            'food_cost_pct': _r(recipe.food_cost_percentage) if recipe.selling_price else None,
            'margin_per_portion': _r(recipe.profit_margin) if recipe.selling_price else None,
            'prep_minutes': recipe.prep_time or None,
            'cook_minutes': recipe.cook_time or None,
            'ingredients': [{
                'ingredient': line.product_id.display_name,
                'sub_recipe': line.is_component,
                'quantity': line.quantity,
                'unit': line.uom_id.name,
                'unit_cost': _r(line.unit_cost, 4),
                'cost': _r(line.cost),
                'share_pct': _pct(line.cost, total),
            } for line in recipe.ingredient_line_ids],
        }
        if include_instructions:
            result['instructions'] = html2plaintext(recipe.instructions or '') or None
        return result

    @agent_tool(
        name='products_without_recipe',
        title='Items without recipe', title_ar='أصناف بدون وصفة',
        category='recipes', requires=('restaurant.recipe', 'pos.order'),
        description="""
            Point-of-sale menu items that have no recipe. Selling them consumes no ingredients in
            stock, so food cost and stocktakes are wrong for them. Bottled or resold items that are
            tracked in stock themselves are flagged with tracked_in_stock = true and are usually fine.
        """,
        params={'type': 'object', 'properties': {
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 200},
            'sold_days': {'type': 'integer', 'minimum': 1, 'maximum': 366,
                          'description': 'Only items sold in the last N days, most sold first (with sold_qty). '
                                         'Default: every POS item, sold or not, by name.'},
        }},
    )
    def _tool_products_without_recipe(self, limit=200, sold_days=None):
        Template = self.env['product.template'].sudo()
        with_recipe = self.env['restaurant.recipe'].sudo().search([]).product_tmpl_id
        products = Template.search([
            ('available_in_pos', '=', True), ('sale_ok', '=', True),
            ('type', '!=', 'combo'), ('id', 'not in', with_recipe.ids),
        ], order='name')
        sold = {}
        if sold_days:
            since = fields.Datetime.now() - timedelta(days=sold_days)
            groups = self.env['pos.order.line'].sudo()._read_group(
                [('order_id.state', 'in', ('paid', 'done', 'invoiced')), ('order_id.date_order', '>=', since),
                 ('product_id.product_tmpl_id', 'in', products.ids)],
                ['product_id'], ['qty:sum'])
            for product, qty in groups:
                sold[product.product_tmpl_id.id] = sold.get(product.product_tmpl_id.id, 0.0) + qty
            products = products.filtered(lambda p: sold.get(p.id, 0) > 0).sorted(lambda p: -sold[p.id])
        rows = [dict({
            'id': p.id,
            'menu_item': p.display_name,
            'pos_categories': p.pos_categ_ids.mapped('name'),
            'price': _r(p.list_price),
            'tracked_in_stock': p.is_storable,
        }, **({'sold_qty': _r(sold[p.id], 3)} if sold_days else {})) for p in products]
        return {'count': len(rows), 'not_tracked_in_stock': sum(1 for r in rows if not r['tracked_in_stock']),
                **({'sold_days': sold_days} if sold_days else {}), 'products': rows[:limit]}

    # ------------------------------------------------------------------
    # Stocktakes, waste, prices
    # ------------------------------------------------------------------

    @agent_tool(
        name='stocktake_results',
        title='Stocktake results', title_ar='نتائج الجرد',
        category='stock', requires=('ingredient.stocktake',),
        description="""
            Results of ingredient stocktakes (physical counts): for each count, the stock value the
            system expected, the value actually counted and the difference (variance; negative =
            shortage: waste, theft or wrong recipes), with the ingredients that have the biggest
            variances in money. Variance % = variance / system value. Validated counts by default.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID,
            'reference': {'type': 'string', 'maxLength': 50, 'description': 'e.g. STK/2026/0009'},
            'state': {'type': 'string', 'enum': ['done', 'all'], 'default': 'done'},
            'top_lines': {'type': 'integer', 'minimum': 0, 'maximum': 200, 'default': 10,
                          'description': 'Ingredients with the biggest variances to list per count.'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 10},
        }},
    )
    def _tool_stocktake_results(self, date_from=None, date_to=None, company_id=None, reference=None,
                                state='done', top_lines=10, limit=10):
        Stocktake = self.env['ingredient.stocktake'].sudo()
        domain = self._company_domain('ingredient.stocktake', company_id)
        if reference:
            domain.append(('name', 'ilike', reference))
        else:
            _start, _end, _tz, first, last = self._local_range(date_from, date_to)
            domain += [('date', '>=', first), ('date', '<=', last)]
        if state == 'done':
            domain.append(('state', '=', 'done'))
        stocktakes = Stocktake.search(domain, limit=limit)
        states = dict(Stocktake._fields['state']._description_selection(self.env))
        rows = []
        for st in stocktakes:
            lines = st.line_ids.filtered(lambda l: l.variance_qty)
            lines = lines.sorted(lambda l: abs(l.variance_value), reverse=True)[:top_lines]
            rows.append({
                'reference': st.name,
                'date': str(st.date),
                'state': states.get(st.state),
                'responsible': st.user_id.name,
                'items_counted': st.line_count,
                'items_with_variance': st.variance_count,
                'system_value': _r(st.total_system_value),
                'counted_value': _r(st.total_counted_value),
                'variance_value': _r(st.total_variance_value),
                'variance_pct': _pct(st.total_variance_value, st.total_system_value),
                'biggest_variances': [{
                    'ingredient': l.product_id.display_name,
                    'unit': l.uom_id.name,
                    'system_qty': _r(l.system_qty, 3),
                    'counted_qty': _r(l.counted_qty, 3),
                    'variance_qty': _r(l.variance_qty, 3),
                    'unit_cost': _r(l.unit_cost, 4),
                    'variance_value': _r(l.variance_value),
                } for l in lines],
            })
        return {'count': len(rows), 'currency': self._currency_name(company_id), 'stocktakes': rows}

    @agent_tool(
        name='waste_summary',
        title='Waste summary', title_ar='ملخص الهدر',
        category='stock', requires=('ingredient.waste',),
        description="""
            Recorded ingredient waste (expired, spoiled, damaged, overproduction...) over a period:
            total cost, cost per reason and the most wasted ingredients. Only waste that staff
            recorded: unrecorded losses show up as stocktake variances instead.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID,
            'include_draft': {'type': 'boolean', 'default': False},
            'top': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 10},
        }},
    )
    def _tool_waste_summary(self, date_from=None, date_to=None, company_id=None, include_draft=False, top=10):
        Waste = self.env['ingredient.waste'].sudo()
        _start, _end, _tz, first, last = self._local_range(date_from, date_to)
        domain = [('date', '>=', first), ('date', '<=', last)] + self._company_domain('ingredient.waste', company_id)
        if not include_draft:
            domain.append(('state', '=', 'confirmed'))
        records = Waste.search(domain)
        reasons = dict(Waste._fields['reason']._description_selection(self.env))
        by_reason = defaultdict(lambda: {'records': 0, 'cost': 0.0})
        by_product = defaultdict(lambda: {'quantity': 0.0, 'cost': 0.0, 'unit': None})
        for w in records:
            by_reason[reasons.get(w.reason)]['records'] += 1
            by_reason[reasons.get(w.reason)]['cost'] += w.cost
            item = by_product[w.product_id.display_name]
            item['quantity'] += w.quantity
            item['cost'] += w.cost
            item['unit'] = w.uom_id.name
        total = sum(records.mapped('cost'))
        return {
            'period': {'from': str(first), 'to': str(last)},
            'records': len(records),
            'total_cost': _r(total),
            'by_reason': [{'reason': k, 'records': v['records'], 'cost': _r(v['cost']), 'share_pct': _pct(v['cost'], total)}
                          for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1]['cost'])],
            'top_ingredients': [{'ingredient': k, 'quantity': _r(v['quantity'], 3), 'unit': v['unit'], 'cost': _r(v['cost'])}
                                for k, v in sorted(by_product.items(), key=lambda kv: -kv[1]['cost'])[:top]],
        }

    @agent_tool(
        name='ingredient_price_changes',
        title='Ingredient price changes', title_ar='تغيرات أسعار المواد',
        category='stock', requires=('ingredient.cost.history',),
        description="""
            Changes of ingredient purchase costs over a period (old cost, new cost, change %), with
            the number of increases and decreases. Use it to explain why recipe costs or food cost %
            moved, or to spot suppliers raising prices.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO,
            'search': {'type': 'string', 'maxLength': 100, 'description': 'Part of the ingredient name.'},
            'direction': {'type': 'string', 'enum': ['all', 'increase', 'decrease'], 'default': 'all'},
            'min_change_pct': {'type': 'number', 'minimum': 0, 'default': 0,
                               'description': 'Ignore changes smaller than this (absolute %).'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500, 'default': 50},
        }},
    )
    def _tool_ingredient_price_changes(self, date_from=None, date_to=None, search=None, direction='all',
                                       min_change_pct=0, limit=50):
        History = self.env['ingredient.cost.history'].sudo()
        start, end, tz, first, last = self._local_range(date_from, date_to)
        domain = [('change_date', '>=', start), ('change_date', '<', end)]
        if search:
            domain.append(('product_id.name', 'ilike', search))
        if direction == 'increase':
            domain.append(('change_percentage', '>', 0))
        elif direction == 'decrease':
            domain.append(('change_percentage', '<', 0))
        changes = History.search(domain, order='change_date desc').filtered(
            lambda c: abs(c.change_percentage) >= min_change_pct)
        reasons = dict(History._fields['reason']._description_selection(self.env))
        return {
            'period': {'from': str(first), 'to': str(last), 'timezone': tz},
            'changes': len(changes),
            'increases': len(changes.filtered(lambda c: c.change_percentage > 0)),
            'decreases': len(changes.filtered(lambda c: c.change_percentage < 0)),
            'ingredients_affected': len(changes.product_id),
            'list': [{
                'ingredient': c.product_id.display_name,
                'old_cost': _r(c.old_cost, 4),
                'new_cost': _r(c.new_cost, 4),
                'change_pct': _r(c.change_percentage),
                'date_utc': self._json_safe(c.change_date),
                'reason': reasons.get(c.reason),
                'changed_by': c.changed_by.name or None,
            } for c in changes[:limit]],
        }

    @agent_tool(
        name='stock_levels',
        title='Stock levels', title_ar='مستويات المخزون',
        category='stock', requires=('stock.quant',),
        description="""
            Current quantity and value in stock of ingredients (or of all stock-tracked products),
            with flags: negative stock (sales recorded with no stock entered: missing purchase
            receipts or wrong recipes) and below the reorder minimum. Value = quantity x current cost.
        """,
        params={'type': 'object', 'properties': {
            'search': {'type': 'string', 'maxLength': 100},
            'only_ingredients': {'type': 'boolean', 'default': True,
                                 'description': "Only products marked 'Is Ingredient' (when the recipes module is installed)."},
            'only_problems': {'type': 'boolean', 'default': False,
                              'description': 'Only negative or below-minimum products.'},
            'sort_by': {'type': 'string', 'enum': ['value_desc', 'qty_asc', 'name'], 'default': 'value_desc'},
            'company_id': COMPANY_ID,
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 100},
        }},
    )
    def _tool_stock_levels(self, search=None, only_ingredients=True, only_problems=False, sort_by='value_desc',
                           company_id=None, limit=100):
        Product = self.env['product.product'].sudo()
        if company_id:
            Product = Product.with_company(company_id)
        domain = [('is_storable', '=', True)]
        if only_ingredients and 'is_ingredient' in Product._fields:
            domain.append(('is_ingredient', '=', True))
        if search:
            domain.append(('name', 'ilike', search))
        products = Product.search(domain)
        minimums = defaultdict(float)
        if 'stock.warehouse.orderpoint' in self.env:
            for op in self.env['stock.warehouse.orderpoint'].sudo().search([('product_id', 'in', products.ids)]):
                minimums[op.product_id.id] += op.product_min_qty
        rows = []
        for p in products:
            qty = p.qty_available
            minimum = minimums.get(p.id)
            row = {
                'id': p.id,
                'product': p.display_name,
                'quantity': _r(qty, 3),
                'unit': p.uom_id.name,
                'unit_cost': _r(p.standard_price, 4),
                'value': _r(qty * p.standard_price),
                'reorder_min': minimum,
                'negative': qty < 0,
                'below_min': bool(minimum) and qty < minimum,
            }
            if not only_problems or row['negative'] or row['below_min']:
                rows.append(row)
        sorters = {'value_desc': (lambda r: r['value'], True), 'qty_asc': (lambda r: r['quantity'], False),
                   'name': (lambda r: r['product'].lower(), False)}
        key, reverse = sorters[sort_by]
        rows.sort(key=key, reverse=reverse)
        return {
            'products': len(rows),
            'total_value': _r(sum(r['value'] for r in rows if r['quantity'] > 0)),
            'negative_count': sum(1 for r in rows if r['negative']),
            'below_min_count': sum(1 for r in rows if r['below_min']),
            'currency': self._currency_name(company_id),
            'returned': min(len(rows), limit),
            'list': rows[:limit],
        }

    # ------------------------------------------------------------------
    # Consumption and food cost (from point-of-sale stock moves)
    # ------------------------------------------------------------------

    @api.model
    def _pos_consumption(self, start, end, company_id=None):
        """Net stock consumed by POS sales between two UTC datetimes, per product.

        Recipes are kits: selling a menu item moves its ingredients out of stock. Refunds
        bring them back. Cost = stock valuation of the moves when available, else
        quantity x current cost.
        """
        Move = self.env['stock.move'].sudo()
        domain = [
            ('state', '=', 'done'), ('date', '>=', start), ('date', '<', end),
            '|', ('picking_id.pos_order_id', '!=', False), ('picking_id.pos_session_id', '!=', False),
        ] + self._company_domain('stock.move', company_id)
        moves = Move.search(domain)
        values = {}
        if 'stock.valuation.layer' in self.env and moves:
            for move, value in self.env['stock.valuation.layer'].sudo()._read_group(
                    [('stock_move_id', 'in', moves.ids)], ['stock_move_id'], ['value:sum']):
                values[move.id] = value
        per_product = defaultdict(lambda: {'quantity': 0.0, 'cost': 0.0})
        for move in moves:
            sign = -1 if move.location_id.usage == 'customer' else 1  # refund brings stock back
            item = per_product[move.product_id]
            item['quantity'] += sign * move.product_qty
            item['cost'] += -values[move.id] if move.id in values else sign * move.product_qty * move.product_id.standard_price
        return per_product, ('stock_valuation' if values else 'current_cost')

    @agent_tool(
        name='ingredient_consumption',
        title='Ingredient consumption', title_ar='استهلاك المواد',
        category='stock', requires=('stock.move', 'pos.order'),
        description="""
            Ingredients consumed by point-of-sale sales over a period (theoretical usage from the
            recipes), net of refunds: quantity and cost per ingredient. Compare with purchases and
            stocktakes to find losses. Days are cut in local time.
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID,
            'search': {'type': 'string', 'maxLength': 100},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 100},
        }},
    )
    def _tool_ingredient_consumption(self, date_from=None, date_to=None, company_id=None, search=None, limit=100):
        start, end, tz, first, last = self._local_range(date_from, date_to)
        per_product, basis = self._pos_consumption(start, end, company_id)
        rows = [{
            'product': p.display_name,
            'is_ingredient': bool(p['is_ingredient']) if 'is_ingredient' in p._fields else None,
            'quantity': _r(v['quantity'], 3),
            'unit': p.uom_id.name,
            'cost': _r(v['cost']),
        } for p, v in per_product.items() if not search or search.lower() in p.display_name.lower()]
        rows.sort(key=lambda r: -r['cost'])
        total = sum(r['cost'] for r in rows)
        for r in rows:
            r['share_pct'] = _pct(r['cost'], total)
        return {
            'period': {'from': str(first), 'to': str(last), 'timezone': tz},
            'cost_basis': basis,
            'total_cost': _r(total),
            'products': len(rows),
            'currency': self._currency_name(company_id),
            'list': rows[:limit],
        }

    @agent_tool(
        name='food_cost_overview',
        title='Food cost overview', title_ar='نسبة تكلفة المواد',
        category='recipes', requires=('stock.move', 'pos.order'),
        description="""
            Food cost % of point-of-sale sales over a period: net sales without tax, cost of the
            ingredients consumed by those sales (from recipes), food cost % = cost / sales and gross
            margin. Optionally per day. This is the theoretical food cost: real cost also includes
            waste and stocktake variances (see waste_summary and stocktake_results).
        """,
        params={'type': 'object', 'properties': {
            'date_from': DATE_FROM, 'date_to': DATE_TO, 'company_id': COMPANY_ID,
            'per_day': {'type': 'boolean', 'default': False},
        }},
    )
    def _tool_food_cost_overview(self, date_from=None, date_to=None, company_id=None, per_day=False):
        start, end, tz, first, last = self._local_range(date_from, date_to)
        Line = self.env['pos.order.line'].sudo()
        line_domain = [('order_id.state', 'in', POS_SOLD_STATES),
                       ('order_id.date_order', '>=', start), ('order_id.date_order', '<', end)]
        if company_id:
            line_domain.append(('order_id.company_id', '=', company_id))
        lines = Line.search(line_domain)
        sales = sum(lines.mapped('price_subtotal'))
        per_product, basis = self._pos_consumption(start, end, company_id)
        cost = sum(v['cost'] for v in per_product.values())
        result = {
            'period': {'from': str(first), 'to': str(last), 'timezone': tz},
            'orders': len(lines.order_id),
            'net_sales_untaxed': _r(sales),
            'ingredients_cost': _r(cost),
            'food_cost_pct': _pct(cost, sales),
            'gross_margin': _r(sales - cost),
            'cost_basis': basis,
            'currency': self._currency_name(company_id),
        }
        if per_day:
            days = []
            day = first
            while day <= last:
                d_start, d_end, *_rest = self._local_range(str(day), str(day))
                d_lines = lines.filtered(lambda l: d_start <= l.order_id.date_order < d_end)
                d_sales = sum(d_lines.mapped('price_subtotal'))
                d_cost = sum(v['cost'] for v in self._pos_consumption(d_start, d_end, company_id)[0].values())
                days.append({'date': str(day), 'net_sales_untaxed': _r(d_sales), 'ingredients_cost': _r(d_cost),
                             'food_cost_pct': _pct(d_cost, d_sales)})
                day = day.fromordinal(day.toordinal() + 1)
            result['per_day'] = days
        return result

    # ------------------------------------------------------------------
    # Domain note for AI models
    # ------------------------------------------------------------------

    @api.model
    def _agent_resources(self):
        resources = super()._agent_resources()
        if 'restaurant.recipe' in self.env:
            resources.append({
                'name': 'recipes',
                'title': 'Recipes, food cost and stock',
                'description': 'How recipes, kits, consumption, stocktakes, waste and food cost work in this system.',
                'module': 'donialink_agent',
                'path': 'donialink_agent/agent/recipes.md',
            })
        return resources
