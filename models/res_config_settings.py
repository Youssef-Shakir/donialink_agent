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
    donialink_agent_platform_url = fields.Char(
        'Platform Address', config_parameter='donialink_agent.platform_url',
        help='Address of the DoniaLink Platform, e.g. https://app.donialink.com')
    donialink_agent_platform_announce = fields.Boolean(
        'Announce to Platform', config_parameter='donialink_agent.platform_announce',
        help='Tell the platform every 15 minutes that this database runs the connector (no key is sent), '
             'so it is listed there as an available agent.')
    donialink_agent_platform_status = fields.Char('Platform Status', compute='_compute_donialink_agent_platform')
    donialink_agent_platform_summary = fields.Char(compute='_compute_donialink_agent_platform')

    def _compute_donialink_agent_platform(self):
        platform = self.env['donialink.agent.platform']
        status = platform._get('status') or 'disconnected'
        labels = {
            'available': _('Announced: listed on the platform as available, not connected'),
            'pending': _('Waiting for approval on the platform'),
            'connected': _('Connected'),
            'rejected': _('Rejected by the platform'),
            'disconnected': _('Not connected'),
        }
        parts = [labels.get(status, status)]
        if platform._get('client'):
            parts.append(_('client: %s', platform._get('client')))
        if platform._get('last_seen'):
            parts.append(_('last contact %s UTC', platform._get('last_seen')))
        if platform._get('last_error'):
            parts.append(_('last error: %s', platform._get('last_error')))
        for settings in self:
            settings.donialink_agent_platform_status = status
            settings.donialink_agent_platform_summary = ' · '.join(parts)

    def action_donialink_platform_connect(self):
        self.ensure_one()
        self.execute()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'donialink.agent.platform.wizard',
            'name': _('Connect to DoniaLink Platform'),
            'view_mode': 'form',
            'target': 'new',
        }

    def action_donialink_platform_heartbeat(self):
        self.ensure_one()
        self.execute()
        platform = self.env['donialink.agent.platform']
        ok = platform._heartbeat()
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'type': 'success' if ok else 'warning',
                'message': _('The platform answered.') if ok else (platform._get('last_error') or _('No answer')),
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }

    def action_donialink_platform_disconnect(self):
        self.ensure_one()
        self.env['donialink.agent.platform']._disconnect()
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    @api.constrains('donialink_agent_allowed_ips')
    def _check_donialink_agent_allowed_ips(self):
        for settings in self:
            for item in (settings.donialink_agent_allowed_ips or '').split(','):
                if item.strip():
                    try:
                        ipaddress.ip_network(item.strip(), strict=False)
                    except ValueError:
                        raise ValidationError(_('"%s" is not a valid IP address or network.', item.strip()))
