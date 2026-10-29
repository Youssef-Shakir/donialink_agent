# -*- coding: utf-8 -*-
"""DoniaLink agent API.

    GET  /donialink/agent/v1/ping                  connection check
    GET  /donialink/agent/v1/tools                 tool manifest (names, descriptions, input schemas)
    POST /donialink/agent/v1/tools/<name>          run a tool
    GET  /donialink/agent/v1/resources             domain notes for AI models
    GET  /donialink/agent/v1/resources/<name>      one note (markdown)
    GET  /donialink/agent/v1/files/<id>            download a file made by a tool (24 hours)

Every call needs ``Authorization: Bearer <agent API key>``. Tool calls take a
JSON body::

    {"params": {...}, "lang": "ar", "actor": {"id": "u-12", "name": "Yousif"}, "request_id": "..."}

and answer ``{"ok": true, "tool": ..., "result": ...}`` or
``{"ok": false, "error": {"code": ..., "message": ...}}``.

A tool that makes a file (``export_report``) returns it under ``FILE_KEY``: the file is
stored as an attachment of the audit log line, after the read-only rollback, and the
result gets ``"file": {"id", "name", "mimetype", "size", "download_url", "expires_at"}``.

The database is the one selected by Odoo's dbfilter for the request host
(one database per domain), like the web client.

Administrators can try every tool from the test console at ``/donialink/agent/console``
(Odoo session, no API key), which runs the same pipeline.
"""
import hmac
import ipaddress
import json
import logging
import re
import time

import werkzeug.exceptions

from odoo import SUPERUSER_ID, _, api, fields, http
from odoo.exceptions import AccessDenied, AccessError, UserError, ValidationError
from odoo.http import content_disposition, request
from odoo.tools import config, file_open

from ..models.agent_log import FILE_MAX_SIZE
from ..models.agent_platform import TECH_GROUP
from ..models.agent_registry import ACCESS_READ, API_KEY_SCOPE, API_VERSION, FILE_KEY, ToolError

_logger = logging.getLogger(__name__)

PREFIX = f'/donialink/agent/v{API_VERSION}'
# readonly=False: Odoo 18 gives auth='none' routes a read-only cursor by default, but every call
# writes the audit log. Read tools are still rolled back before the log is written.
ROUTE = dict(type='http', auth='none', csrf=False, save_session=False, readonly=False)


class AgentController(http.Controller):

    # ------------------------------------------------------------------
    # Agent API (bearer key)
    # ------------------------------------------------------------------

    @http.route(f'{PREFIX}/ping', methods=['GET'], **ROUTE)
    def ping(self):
        return self._response(*self._process('ping'))

    @http.route(f'{PREFIX}/tools', methods=['GET'], **ROUTE)
    def list_tools(self):
        return self._response(*self._process('tools'))

    @http.route(f'{PREFIX}/tools/<string:tool_name>', methods=['POST'], **ROUTE)
    def call_tool(self, tool_name):
        return self._response(*self._process('call', name=tool_name))

    @http.route(f'{PREFIX}/resources', methods=['GET'], **ROUTE)
    def list_resources(self):
        return self._response(*self._process('resources'))

    @http.route(f'{PREFIX}/resources/<string:name>', methods=['GET'], **ROUTE)
    def read_resource(self, name):
        return self._response(*self._process('resource', name=name))

    # auth='public': the bearer key as for the other routes, or the Odoo session of an
    # administrator (test console download links)
    @http.route(f'{PREFIX}/files/<int:file_id>', methods=['GET'], **dict(ROUTE, auth='public'))
    def download_file(self, file_id):
        started = time.monotonic()
        log = {'tool': 'file_download', 'access': ACCESS_READ, 'ip_address': self._remote_ip(),
               'params': {'file_id': file_id}}
        try:
            user = request.env.user
            if not request.httprequest.headers.get('Authorization') and user.has_group(TECH_GROUP):
                log.update(user_id=user.id, actor_ref=f'odoo-user-{user.id}', actor_name=f'{user.name} (Odoo session)')
            else:
                log['user_id'] = self._authenticate()
            attachment = request.env['donialink.agent.log'].sudo()._get_file(file_id)
            content = attachment.raw
            self._write_log(log, started, status='success', result_size=len(content))
            return request.make_response(content, headers=[
                ('Content-Type', attachment.mimetype or 'application/octet-stream'),
                ('Content-Disposition', content_disposition(attachment.name)),
                ('Content-Length', len(content)),
                ('Cache-Control', 'no-store'),
                ('X-Content-Type-Options', 'nosniff'),
            ])
        except Exception as e:
            return self._response(*self._handle_error(e, log, started, True, f'file {file_id}'))

    # ------------------------------------------------------------------
    # Test console (administrators, Odoo session)
    # ------------------------------------------------------------------

    @http.route('/donialink/agent/console', type='http', auth='user', methods=['GET'])
    def console(self):
        if not request.env.user.has_group(TECH_GROUP):
            raise werkzeug.exceptions.NotFound()
        with file_open('donialink_agent/static/src/console/console.html') as f:
            html = f.read()
        return request.make_response(html, headers=[
            ('Content-Type', 'text/html; charset=utf-8'), ('Cache-Control', 'no-store'),
        ])

    @http.route('/donialink/agent/console/call', type='json', auth='user', methods=['POST'], readonly=False)
    def console_call(self, kind, name=None, body=None):
        """Run what the gateway would run, as the agent user, without an API key.

        Same validation, read-only rollback and audit log as the API; the call is
        logged as requested by the administrator using the console.
        """
        user = request.env.user
        if not user.has_group(TECH_GROUP):
            raise AccessError(_('Only DoniaLink staff can use the agent test console.'))
        agent = request.env.ref('donialink_agent.user_agent')
        body = dict(body or {}, actor={'id': f'odoo-user-{user.id}', 'name': f'{user.name} (test console)'})
        started = time.monotonic()
        data, status = self._process(kind, name=name, uid=agent.id, body=body)
        return {'status': status, 'ms': int((time.monotonic() - started) * 1000), 'response': json.loads(data)}

    # ------------------------------------------------------------------
    # Secret link for DoniaLink staff (settings, menus, keys, log, console)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_admin_token(token):
        """404 unless the token matches donialink_agent_admin_token of the server configuration file.

        The token lives in odoo.conf (not in the database), so the client's administrators can't
        read it from Odoo. Unset or shorter than 16 characters = the link is switched off.
        """
        expected = str(config.get('donialink_agent_admin_token') or '')
        if len(expected) < 16 or not hmac.compare_digest(expected.encode(), str(token).encode()):
            raise werkzeug.exceptions.NotFound()

    @http.route('/donialink/agent/admin/<string:token>', type='http', auth='user', methods=['GET'])
    def technical_unlock(self, token):
        self._check_admin_token(token)
        user = request.env.user
        if not user._is_system():
            raise werkzeug.exceptions.NotFound()
        user.sudo().write({'groups_id': [(4, request.env.ref(TECH_GROUP).id)]})
        _logger.info('DoniaLink agent: technical access opened for %s', user.login)
        return request.redirect('/odoo/settings#donialink_agent')

    @http.route('/donialink/agent/admin/<string:token>/lock', type='http', auth='user', methods=['GET'])
    def technical_lock(self, token):
        self._check_admin_token(token)
        request.env.user.sudo().write({'groups_id': [(3, request.env.ref(TECH_GROUP).id)]})
        return request.redirect('/odoo')

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    def _process(self, kind, name=None, uid=None, body=None):
        """Run one API request and return (JSON text, HTTP status).

        :param kind: 'ping', 'tools', 'call' (run tool ``name``), 'resources', 'resource'
        :param uid: agent user already identified (test console); None = authenticate the bearer key
        """
        started = time.monotonic()
        is_call = kind == 'call'
        log = {'tool': name if is_call else kind, 'ip_address': self._remote_ip()}
        try:
            if uid is None:
                uid = self._authenticate()
                body = self._read_body() if is_call else {}
            body = body or {}
            actor = body.get('actor') if isinstance(body.get('actor'), dict) else {}
            log.update(
                user_id=uid,
                actor_ref=str(actor.get('id') or '')[:100] or False,
                actor_name=str(actor.get('name') or '')[:200] or False,
                request_ref=str(body.get('request_id') or '')[:100] or False,
                params=body.get('params') or {},
            )
            request.update_env(user=uid, context=self._context(body.get('lang')))
            tools = request.env['donialink.agent.tools']
            access = ACCESS_READ
            if is_call:
                # known before running, so failed calls are logged with it too
                tool = tools._get_tools().get(name)
                log['access'] = access = tool[1]['access'] if tool else False
                _meta, result = tools._run_tool(name, body.get('params'))
                file = self._pop_file(result)
                payload = {'ok': True, 'tool': name, 'result': result}
            else:
                file = None
                payload = dict(self._payload(kind, tools, name), ok=True)
            data = json.dumps(payload, ensure_ascii=False, default=str)
            if access == ACCESS_READ:
                # read tools never change data, even by accident
                self._rollback()
            if is_call:
                log_line = self._write_log(log, started, status='success', result_size=len(data.encode()))
                if file:
                    # after the rollback, in the transaction of the audit log
                    result['file'] = self._store_file(file, log_line)
                    data = json.dumps(payload, ensure_ascii=False, default=str)
            return data, 200
        except Exception as e:
            return self._handle_error(e, log, started, is_call, f'{kind} {name or ""}')

    def _handle_error(self, e, log, started, write_log, label):
        """(JSON text, HTTP status) of an exception; called in the ``except`` block."""
        if isinstance(e, ToolError):
            return self._error(log, started, e.status, e.code, e.message, write_log)
        if isinstance(e, AccessDenied):
            return self._error(log, started, 401, 'unauthorized', str(e) or 'Unauthorized', True)
        if isinstance(e, AccessError):
            return self._error(log, started, 403, 'access_denied', str(e), write_log)
        if isinstance(e, (UserError, ValidationError)):
            return self._error(log, started, 400, 'invalid', str(e), write_log)
        _logger.exception('DoniaLink agent: %s failed', label)
        return self._error(log, started, 500, 'internal_error', f'{type(e).__name__}: {e}', write_log)

    def _pop_file(self, result):
        """Take the file a tool returned under FILE_KEY out of its result (None if none)."""
        if not isinstance(result, dict) or FILE_KEY not in result:
            return None
        file = result.pop(FILE_KEY)
        if not isinstance(file, dict) or not isinstance(file.get('content'), bytes) or not file.get('name'):
            raise ToolError('internal_error', 'The tool returned an invalid file', status=500)
        if len(file['content']) > FILE_MAX_SIZE:
            raise ToolError('file_too_large', f'The file is larger than {FILE_MAX_SIZE // (1024 * 1024)} MB: '
                                              'choose a shorter period or a narrower search', status=413)
        return file

    def _store_file(self, file, log_line):
        if not log_line:
            raise ToolError('internal_error', 'The file could not be stored (audit log failed)', status=500)
        attachment = log_line._attach_file(file['name'], file.get('mimetype') or 'application/octet-stream',
                                           file['content'])
        return {
            'id': attachment.id,
            'name': attachment.name,
            'mimetype': attachment.mimetype,
            'size': len(file['content']),
            'download_url': f"{request.httprequest.host_url.rstrip('/')}{PREFIX}/files/{attachment.id}",
            'expires_at': fields.Datetime.to_string(log_line._file_expiry(attachment)),
        }

    def _payload(self, kind, tools, name):
        info = {
            'database': request.env.cr.dbname,
            'api_version': API_VERSION,
            'connector_version': self._connector_version(),
        }
        if kind == 'ping':
            # the platform keeps the companies of the database up to date from its regular ping
            return dict(info, companies=[{'id': c.id, 'name': c.name, 'currency': c.currency_id.name}
                                         for c in request.env['res.company'].sudo().search([], order='sequence, id')])
        if kind == 'tools':
            return dict(info, tools=tools._get_tool_manifest())
        if kind == 'resources':
            return {'resources': [{k: v for k, v in r.items() if k != 'path'} for r in tools._agent_resources()]}
        if kind == 'resource':
            return {k: v for k, v in tools._read_resource(name).items() if k != 'path'}
        raise ToolError('unknown_request', f"Unknown request '{kind}'", status=404)

    def _authenticate(self):
        """Return the uid of the agent user owning the bearer key, or raise."""
        env = api.Environment(request.env.cr, SUPERUSER_ID, {})
        params = env['ir.config_parameter']
        if params.get_param('donialink_agent.api_enabled') != 'True':
            raise ToolError('disabled', 'The agent API is disabled on this database', status=503)
        allowed = [i.strip() for i in (params.get_param('donialink_agent.allowed_ips') or '').split(',') if i.strip()]
        if allowed and not self._ip_allowed(self._remote_ip(), allowed):
            raise ToolError('ip_not_allowed', 'This IP address is not allowed', status=403)
        header = request.httprequest.headers.get('Authorization') or ''
        match = re.match(r'^bearer\s+(\S+)$', header, re.IGNORECASE)
        if not match:
            raise AccessDenied('Missing "Authorization: Bearer <key>" header')
        uid = env['res.users.apikeys']._check_credentials(scope=API_KEY_SCOPE, key=match.group(1))
        if not uid or not env['res.users'].browse(uid).has_group('donialink_agent.group_agent_api'):
            raise AccessDenied('Invalid or expired agent API key')
        return uid

    def _read_body(self):
        httprequest = request.httprequest
        if httprequest.mimetype != 'application/json':
            # Odoo already consumed a form-encoded body: refuse instead of silently ignoring it
            if httprequest.content_length or httprequest.form:
                raise ToolError('invalid_content_type', 'Send the body as JSON with "Content-Type: application/json"',
                                status=415)
            return {}
        raw = httprequest.get_data(as_text=True)
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except ValueError:
            raise ToolError('invalid_json', 'The request body is not valid JSON')
        if not isinstance(body, dict):
            raise ToolError('invalid_json', 'The request body must be a JSON object')
        return body

    def _context(self, lang):
        """Context of the tool call: 'ar' / 'en' or a full code picks the language of labels and names."""
        env = request.env
        user = env['res.users'].sudo().browse(env.uid)
        context = {'tz': user.tz or 'UTC', 'lang': user.lang or 'en_US', 'active_test': True}
        if lang:
            installed = [code for code, _name in env['res.lang'].get_installed()]
            wanted = 'en_US' if lang == 'en' else lang
            code = next((c for c in installed if c == wanted), None) or \
                next((c for c in installed if c.split('_')[0] == wanted.split('_')[0]), None)
            if code:
                context['lang'] = code
        return context

    def _rollback(self):
        env = request.env
        env.flush_all()
        env.cr.rollback()
        env.invalidate_all()

    def _write_log(self, log, started, **values):
        try:
            # superuser env: failed authentications have no user yet
            env = api.Environment(request.env.cr, SUPERUSER_ID, {})
            return env['donialink.agent.log']._log(
                **log, **values, duration_ms=int((time.monotonic() - started) * 1000))
        except Exception:
            _logger.exception('DoniaLink agent: could not write the audit log')
            return None

    def _error(self, log, started, status, code, message, write_log):
        try:
            request.env.cr.rollback()
            request.env.invalidate_all()
        except Exception:
            _logger.exception('DoniaLink agent: rollback failed')
        if write_log:
            self._write_log(log, started, status='denied' if status in (401, 403) else 'error',
                            error_code=code, error_message=message)
        return json.dumps({'ok': False, 'error': {'code': code, 'message': message}}, ensure_ascii=False), status

    def _response(self, data, status):
        return request.make_response(data, status=status, headers=[
            ('Content-Type', 'application/json; charset=utf-8'),
            ('Cache-Control', 'no-store'),
        ])

    def _remote_ip(self):
        return request.httprequest.remote_addr or ''

    @staticmethod
    def _ip_allowed(ip, allowed):
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(address in ipaddress.ip_network(net, strict=False) for net in allowed)

    def _connector_version(self):
        module = request.env['ir.module.module'].sudo().search([('name', '=', 'donialink_agent')], limit=1)
        return module.latest_version
