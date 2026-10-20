# -*- coding: utf-8 -*-
import ipaddress

from odoo import _, api, fields, models
from odoo.addons.base.models.res_partner import _tz_get
from odoo.exceptions import ValidationError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    donialink_agent_api_enabled = fields.Boolean(
        'Agent API Enabled', config_parameter='donialink_agent.api_enabled')
    donialink_agent_allowed_ips = fields.Char(
        'Allowed IP Addresses', config_parameter='donialink_agent.allowed_ips',
        help='Comma-separated IP addresses or networks (e.g. 10.8.0.1, 10.8.0.0/24) allowed to call '
             'the agent API. Empty = any address. Behind a reverse proxy, run Odoo with --proxy-mode.')
    donialink_agent_allow_generic_read = fields.Boolean(
        'Generic Read Tools', config_parameter='donialink_agent.allow_generic_read',
        help='Allow describe_model, search_records and aggregate_records, limited by the access '
             'rights of the DoniaLink Agent user.')
    donialink_agent_business_tz = fields.Selection(
        _tz_get, string='Business Timezone', config_parameter='donialink_agent.business_tz',
        help='Timezone used to cut business days in reports (e.g. Asia/Baghdad). Empty = the '
             "administrator's timezone.")
    donialink_agent_log_retention_days = fields.Integer(
        'Keep Audit Log (days)', config_parameter='donialink_agent.log_retention_days', default=180)

    @api.constrains('donialink_agent_allowed_ips')
    def _check_donialink_agent_allowed_ips(self):
        for settings in self:
            for item in (settings.donialink_agent_allowed_ips or '').split(','):
                if item.strip():
                    try:
                        ipaddress.ip_network(item.strip(), strict=False)
                    except ValueError:
                        raise ValidationError(_('"%s" is not a valid IP address or network.', item.strip()))
