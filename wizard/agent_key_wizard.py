# -*- coding: utf-8 -*-
import datetime

from odoo import _, fields, models
from odoo.addons.base.models.res_users import check_identity
from odoo.exceptions import AccessError

from ..models.agent_registry import API_KEY_SCOPE


class AgentKeyWizard(models.TransientModel):
    _name = 'donialink.agent.key.wizard'
    _description = 'Generate DoniaLink Agent API Key'

    name = fields.Char('Description', required=True, default='DoniaLink Gateway',
                       help='Where the key is used, e.g. "Gateway VPS 1".')
    duration = fields.Selection([
        ('90', '3 Months'),
        ('180', '6 Months'),
        ('365', '1 Year'),
        ('0', 'No Expiration'),
    ], required=True, default='365')

    @check_identity
    def action_generate(self):
        """Create an API key for the agent user, valid only for the agent API (not XML-RPC/JSON-RPC)."""
        self.ensure_one()
        if not self.env.user._is_system():
            raise AccessError(_('Only administrators can create agent API keys.'))
        agent = self.env.ref('donialink_agent.user_agent')
        days = int(self.duration)
        expiration = fields.Datetime.now() + datetime.timedelta(days=days) if days else None
        # sudo: an administrator creates the key on behalf of the agent user
        key = self.env['res.users.apikeys'].with_user(agent).sudo()._generate(API_KEY_SCOPE, self.name, expiration)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'res.users.apikeys.show',
            'name': _('Agent API Key Ready'),
            'views': [(False, 'form')],
            'target': 'new',
            'context': {'default_key': key},
        }
