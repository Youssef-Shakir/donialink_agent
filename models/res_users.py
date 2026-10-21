# -*- coding: utf-8 -*-
from odoo import api, models
from odoo.osv import expression

from .agent_platform import is_technical


def _hidden(env, field):
    """The agent's own user and contact, for the screens of people who are not DoniaLink staff."""
    if is_technical(env):
        return []
    agent = env.ref('donialink_agent.user_agent', raise_if_not_found=False)
    if not agent:
        return []
    return [('id', '!=', agent.id if field == 'user' else agent.partner_id.id)]


class ResUsers(models.Model):
    """Keep the technical DoniaLink Agent user out of the client's user lists and pickers.

    Only what the web client shows is filtered (lists, kanban, groups, autocomplete); the user still
    exists for Odoo and for the agent API.
    """
    _inherit = 'res.users'

    @api.model
    def web_search_read(self, domain, specification, offset=0, limit=None, order=None, count_limit=None):
        return super().web_search_read(expression.AND([domain or [], _hidden(self.env, 'user')]), specification,
                                       offset=offset, limit=limit, order=order, count_limit=count_limit)

    @api.model
    def web_read_group(self, domain, fields, groupby, limit=None, offset=0, orderby=False, lazy=True):
        return super().web_read_group(expression.AND([domain or [], _hidden(self.env, 'user')]), fields, groupby,
                                      limit=limit, offset=offset, orderby=orderby, lazy=lazy)

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        return super().name_search(name, expression.AND([args or [], _hidden(self.env, 'user')]), operator, limit)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    @api.model
    def web_search_read(self, domain, specification, offset=0, limit=None, order=None, count_limit=None):
        return super().web_search_read(expression.AND([domain or [], _hidden(self.env, 'partner')]), specification,
                                       offset=offset, limit=limit, order=order, count_limit=count_limit)

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        return super().name_search(name, expression.AND([args or [], _hidden(self.env, 'partner')]), operator, limit)
