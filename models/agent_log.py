# -*- coding: utf-8 -*-
import datetime
import json

from odoo import api, fields, models

from .agent_registry import ToolError

PARAMS_MAX_LENGTH = 10000
# files returned by tools (report exports) are attached to their log line and kept this long
FILE_LIFETIME = datetime.timedelta(hours=24)
FILE_MAX_SIZE = 20 * 1024 * 1024


class AgentLog(models.Model):
    _name = 'donialink.agent.log'
    _description = 'DoniaLink Agent Audit Log'
    _order = 'id desc'
    _rec_name = 'tool'

    tool = fields.Char(readonly=True, index=True)
    access = fields.Selection([('read', 'Read'), ('write', 'Write')], readonly=True)
    status = fields.Selection([
        ('success', 'Success'),
        ('error', 'Error'),
        ('denied', 'Denied'),
    ], readonly=True, index=True)
    error_code = fields.Char(readonly=True)
    error_message = fields.Text(readonly=True)
    actor_ref = fields.Char('Requested By (ID)', readonly=True, index=True,
                            help='Identifier of the DoniaLink platform user or agent, sent by the gateway.')
    actor_name = fields.Char('Requested By', readonly=True)
    request_ref = fields.Char('Request ID', readonly=True, index=True,
                              help='Correlation id sent by the gateway, to match its own logs.')
    user_id = fields.Many2one('res.users', 'Odoo User', readonly=True)
    ip_address = fields.Char('IP Address', readonly=True)
    params = fields.Text('Parameters', readonly=True)
    duration_ms = fields.Integer('Duration (ms)', readonly=True)
    result_size = fields.Integer('Result Size (bytes)', readonly=True)

    @api.model
    def _log(self, **values):
        if 'params' in values and not isinstance(values['params'], str):
            values['params'] = json.dumps(values['params'], ensure_ascii=False, default=str)
        if values.get('params') and len(values['params']) > PARAMS_MAX_LENGTH:
            values['params'] = values['params'][:PARAMS_MAX_LENGTH] + '…'
        return self.sudo().create(values)

    def _attach_file(self, name, mimetype, content):
        """Store a file returned by the tool of this log line; return the attachment."""
        self.ensure_one()
        return self.env['ir.attachment'].sudo().create({
            'name': name,
            'mimetype': mimetype,
            'raw': content,
            'res_model': self._name,
            'res_id': self.id,
            'public': False,
        })

    @api.model
    def _file_expiry(self, attachment):
        return attachment.create_date + FILE_LIFETIME

    @api.model
    def _get_file(self, file_id):
        """The attachment of a tool file that can still be downloaded, or raise (404)."""
        attachment = self.env['ir.attachment'].sudo().search(
            [('id', '=', file_id), ('res_model', '=', self._name)], limit=1)
        if not attachment:
            raise ToolError('unknown_file', 'Unknown file', status=404)
        if self._file_expiry(attachment) < fields.Datetime.now():
            raise ToolError('expired', 'This file has expired: export it again', status=404)
        return attachment

    @api.model
    def _purge_expired_files(self):
        self.env['ir.attachment'].sudo().search([
            ('res_model', '=', self._name), ('create_date', '<', fields.Datetime.now() - FILE_LIFETIME),
        ]).unlink()

    @api.model
    def _cron_purge_old_logs(self):
        self._purge_expired_files()
        days = int(self.env['ir.config_parameter'].sudo().get_param('donialink_agent.log_retention_days', 180))
        if days > 0:
            limit = fields.Datetime.now() - datetime.timedelta(days=days)
            self.sudo().search([('create_date', '<', limit)]).unlink()
