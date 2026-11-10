# -*- coding: utf-8 -*-
from odoo import api, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    @api.model
    def _donialink_agent_sync_companies(self):
        """The DoniaLink Agent user works for the whole client: it gets every company (run on install and
        upgrade, and for each company created afterwards)."""
        agent = self.env.ref('donialink_agent.user_agent', raise_if_not_found=False)
        if not agent:
            return
        agent = agent.sudo().with_context(active_test=False)
        missing = self.sudo().search([]) - agent.company_ids
        if missing:
            agent.write({'company_ids': [(4, company.id) for company in missing]})

    @api.model_create_multi
    def create(self, vals_list):
        companies = super().create(vals_list)
        self._donialink_agent_sync_companies()
        return companies
