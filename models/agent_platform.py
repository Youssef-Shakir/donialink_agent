# -*- coding: utf-8 -*-
"""Connection of this database to the DoniaLink Platform.

    POST {platform}/api/agents/announce    no key: "this database runs the connector" (shown as available)
    POST {platform}/api/agents/register    instance info + a new agent key (+ enrollment code)
    POST {platform}/api/agents/heartbeat   Bearer <agent token>: still here, versions, tool count
    POST {platform}/api/agents/disconnect  Bearer <agent token>

A scheduled job announces the database (or sends a heartbeat once connected) every
15 minutes. Only "Connect to platform" sends a key; the platform keeps calling this
database through the agent API as before. Without an enrollment code the platform
lists the database as pending until its owner approves it.
"""
import ipaddress
import logging
from urllib.parse import urlparse

import requests

from odoo import _, api, fields, models, release
from odoo.exceptions import UserError
from odoo.modules import module as odoo_module

from .agent_registry import API_KEY_SCOPE, API_VERSION

_logger = logging.getLogger(__name__)

DEFAULT_PLATFORM_URL = 'https://app.donialink.com'
TECH_GROUP = 'donialink_agent.group_donialink_technical'
PARAM = 'donialink_agent.platform_'
KEY_NAME = 'DoniaLink Platform'
TIMEOUT = 15


def _local_host(host):
    """Plain http is accepted only for local and private networks (tests, Docker, WireGuard)."""
    if host == 'localhost' or '.' not in host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback


def is_technical(env):
    """DoniaLink staff (hidden group, granted by the secret link) or a superuser environment."""
    return env.su or env.user.has_group(TECH_GROUP)


def check_platform_url(url):
    url = (url or '').strip().rstrip('/')
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise UserError(_('The platform address must look like https://app.donialink.com'))
    if parsed.scheme == 'http' and not _local_host(parsed.hostname):
        raise UserError(_('Use https:// for the platform: the agent key is sent to it.'))
    return url


class PlatformError(UserError):
    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


class AgentPlatform(models.AbstractModel):
    _name = 'donialink.agent.platform'
    _description = 'DoniaLink Platform Connection'

    @api.model
    def _params(self):
        return self.env['ir.config_parameter'].sudo()

    @api.model
    def _get(self, name, default=False):
        return self._params().get_param(PARAM + name, default)

    @api.model
    def _set(self, name, value):
        self._params().set_param(PARAM + name, value or False)

    @api.model
    def _platform_url(self):
        return check_platform_url(self._get('url') or DEFAULT_PLATFORM_URL)

    @api.model
    def _base_url(self):
        """Address the platform uses to reach this database (set when connecting, else web.base.url)."""
        return (self._get('base_url') or self._params().get_param('web.base.url') or '').rstrip('/')

    @api.model
    def _instance_info(self):
        module = self.env['ir.module.module'].sudo().search([('name', '=', 'donialink_agent')], limit=1)
        return {
            'uuid': self._params().get_param('database.uuid'),
            'database': self.env.cr.dbname,
            'base_url': self._base_url(),
            'company': self.env.company.name,
            'odoo_version': release.version,
            'connector_version': module.latest_version,
            'api_version': API_VERSION,
            'tools_count': len(self.env['donialink.agent.tools']._get_tool_manifest()),
            'api_enabled': self._params().get_param('donialink_agent.api_enabled') == 'True',
        }

    @api.model
    def _post(self, path, payload, token=None):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        platform = self._platform_url()
        try:
            response = requests.post(f'{platform}/api/agents/{path}', json=payload, headers=headers,
                                     timeout=TIMEOUT)
        except requests.RequestException as e:
            raise PlatformError('unreachable', _('Cannot reach the DoniaLink Platform at %(url)s: %(error)s',
                                                 url=platform, error=e)) from e
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400 or not data.get('ok'):
            error = data.get('error') if isinstance(data.get('error'), dict) else {}
            raise PlatformError(error.get('code') or 'error',
                                error.get('message') or _('The platform answered HTTP %s', response.status_code),
                                response.status_code)
        return data

    @api.model
    def _store_answer(self, data):
        self._set('status', data.get('status'))
        self._set('client', data.get('client'))
        self._set('last_error', False)
        self._set('last_seen', fields.Datetime.to_string(fields.Datetime.now()))

    @api.model
    def _connect(self, platform_url, base_url, enrollment_code=None):
        """Register with the platform and hand it a new agent key; returns the platform's answer."""
        if not is_technical(self.env):
            raise UserError(_('Only DoniaLink staff can connect this database to the platform.'))
        if self._params().get_param('donialink_agent.api_enabled') != 'True':
            raise UserError(_('Switch on the Agent API first: the platform reads this database through it.'))
        platform_url = check_platform_url(platform_url)
        base_url = (base_url or '').strip().rstrip('/')
        if urlparse(base_url).scheme not in ('http', 'https'):
            raise UserError(_('Enter the address the platform uses to reach this database, '
                              'e.g. https://cafe.example.com'))
        self._set('url', platform_url)
        self._set('base_url', base_url)

        # a new key for the platform; the previous one is revoked once the platform has the new one
        old_key_id = int(self._get('key_id') or 0)
        agent = self.env.ref('donialink_agent.user_agent')
        Keys = self.env['res.users.apikeys'].sudo()
        key = Keys.with_user(agent).sudo()._generate(API_KEY_SCOPE, KEY_NAME, None)
        key_record = Keys.search([('user_id', '=', agent.id), ('name', '=', KEY_NAME)], order='id desc', limit=1)
        if not odoo_module.current_test:
            # the platform may check the key by calling this database back before it answers
            self.env.cr.commit()

        payload = dict(self._instance_info(), key=key,
                       enrollment_code=(enrollment_code or '').strip() or None,
                       agent_token=self._get('token') or None)
        try:
            data = self._post('register', payload)
        except PlatformError:
            key_record.unlink()
            raise
        if old_key_id and old_key_id != key_record.id:
            Keys.browse(old_key_id).exists().unlink()
        self._set('key_id', str(key_record.id))
        self._set('agent_id', data.get('agent_id'))
        self._set('token', data.get('agent_token') or self._get('token'))
        self._store_answer(data)
        return data

    @api.model
    def _heartbeat(self):
        """Heartbeat when connected, else announce; returns True when the platform answered."""
        token = self._get('token')
        try:
            if token:
                data = self._post('heartbeat', self._instance_info(), token=token)
            # switched on by data/ir_config_parameter_data.xml; unticking removes the parameter
            elif self._params().get_param('donialink_agent.platform_announce') == 'True':
                data = self._post('announce', self._instance_info())
            else:
                return False
        except (PlatformError, UserError) as e:
            self._set('last_error', f'{getattr(e, "code", "error")}: {e.args[0]}'[:500])
            if token and getattr(e, 'status', None) == 401:
                # the platform removed this database: back to announcing, the key is useless now
                self._revoke_key()
                self._set('token', False)
                self._set('status', 'disconnected')
            return False
        self._store_answer(data)
        return True

    @api.model
    def _revoke_key(self):
        key_id = int(self._get('key_id') or 0)
        if key_id:
            self.env['res.users.apikeys'].sudo().browse(key_id).exists().unlink()
        self._set('key_id', False)

    @api.model
    def _disconnect(self):
        if not is_technical(self.env):
            raise UserError(_('Only DoniaLink staff can disconnect this database from the platform.'))
        token = self._get('token')
        if token:
            try:
                self._post('disconnect', {}, token=token)
            except PlatformError as e:
                _logger.warning('DoniaLink platform: disconnect not confirmed by the platform: %s', e)
        self._revoke_key()
        for name in ('agent_id', 'token', 'client', 'last_error'):
            self._set(name, False)
        self._set('status', 'disconnected')

    @api.model
    def _cron_heartbeat(self):
        try:
            self._heartbeat()
        except Exception:
            _logger.exception('DoniaLink platform heartbeat failed')
