# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.http import request

from ..models.agent_platform import DEFAULT_PLATFORM_URL


class AgentPlatformWizard(models.TransientModel):
    _name = 'donialink.agent.platform.wizard'
    _description = 'Connect to the DoniaLink Platform'

    def _default_platform_url(self):
        return self.env['donialink.agent.platform']._get('url') or DEFAULT_PLATFORM_URL

    def _default_base_url(self):
        # the address the admin opened Odoo with: web.base.url of a restored copy still names the live site
        if request:
            return request.httprequest.host_url.rstrip('/')
        return self.env['donialink.agent.platform']._base_url()

    platform_url = fields.Char('Platform Address', required=True, default=_default_platform_url)
    base_url = fields.Char(
        'Address of this Database', required=True, default=_default_base_url,
        help='How the platform reaches this Odoo, e.g. https://cafe.example.com (one address per database). '
             'Filled in with the address you opened Odoo with.')
    enrollment_code = fields.Char(
        'Enrollment Code', help='Optional one-time code from the platform. With it the connection is approved '
                                'right away; without it the platform owner approves it.')

    def action_connect(self):
        self.ensure_one()
        data = self.env['donialink.agent.platform']._connect(self.platform_url, self.base_url, self.enrollment_code)
        if data.get('status') == 'connected':
            message = _('Connected to the DoniaLink Platform as %s.', data.get('client') or self.env.cr.dbname)
        else:
            message = _('Sent to the DoniaLink Platform. It appears there as waiting for approval.')
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'type': 'success', 'message': message, 'sticky': False,
                       'next': {'type': 'ir.actions.client', 'tag': 'reload'}},
        }
