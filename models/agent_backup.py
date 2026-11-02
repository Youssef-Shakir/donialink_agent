# -*- coding: utf-8 -*-
"""Full backups for the DoniaLink Platform (Odoo's standard zip: dump.sql, filestore, manifest.json).

    POST   /donialink/agent/v1/backups                      start one (or get the one running): {id, state}
    GET    /donialink/agent/v1/backups/<id>                 {state: running | done | failed, size, sha256, error}
    GET    /donialink/agent/v1/backups/<id>/download?offset=&length=   a piece of the zip (16 MB by default)
    DELETE /donialink/agent/v1/backups/<id>                 the platform has it: remove it here

The dump runs as its own process (backup_worker.py), so Odoo's time limits never cut it; the platform
downloads it in pieces, each a short request. Off until "Allow Backups to the Platform" is ticked in
Settings (a backup holds everything in the database). Files older than a day are removed.
"""
import json
import os
import re
import secrets
import subprocess
import sys
import time

from odoo import api, models, release
from odoo.modules import module as odoo_module
from odoo.tools import config
from odoo.tools.misc import exec_pg_environ, find_pg_tool

from .agent_registry import ToolError

CHUNK = 16 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
KEEP_SECONDS = 24 * 3600
STUCK_SECONDS = 6 * 3600
ID_RE = re.compile(r'^[0-9a-f]{32}$')


class AgentBackup(models.AbstractModel):
    _name = 'donialink.agent.backup'
    _description = 'DoniaLink Agent Backups'

    @api.model
    def _dir(self):
        path = os.path.join(config['data_dir'], 'donialink_backups', self.env.cr.dbname)
        os.makedirs(path, mode=0o700, exist_ok=True)
        return path

    @api.model
    def _paths(self, backup_id):
        if not ID_RE.match(backup_id or ''):
            raise ToolError('not_found', 'Unknown backup', status=404)
        base = os.path.join(self._dir(), backup_id)
        return base + '.zip', base + '.json'

    @api.model
    def _check_allowed(self):
        if self.env['ir.config_parameter'].sudo().get_param('donialink_agent.allow_backup') != 'True':
            raise ToolError('disabled', 'Backups to the platform are off on this database: tick "Allow Backups to '
                                        'the Platform" in Settings › DoniaLink Agent.', status=403)

    @api.model
    def _read_status(self, status_path):
        try:
            with open(status_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @api.model
    def _cleanup(self):
        """Remove backups older than a day (the platform took them, or gave up)."""
        now = time.time()
        for name in os.listdir(self._dir()):
            path = os.path.join(self._dir(), name)
            if os.path.isfile(path) and now - os.path.getmtime(path) > KEEP_SECONDS:
                os.remove(path)

    @api.model
    def _manifest(self):
        cr = self.env.cr
        cr.execute("SELECT name, latest_version FROM ir_module_module WHERE state = 'installed'")
        return {
            'odoo_dump': '1',
            'db_name': cr.dbname,
            'version': release.version,
            'version_info': release.version_info,
            'major_version': release.major_version,
            'pg_version': '%d.%d' % divmod(cr._obj.connection.server_version / 100, 100),
            'modules': dict(cr.fetchall()),
        }

    @api.model
    def _start(self):
        """Start a backup, or answer the one already running."""
        self._check_allowed()
        self._cleanup()
        for name in sorted(os.listdir(self._dir())):
            if name.endswith('.json'):
                status = self._read_status(os.path.join(self._dir(), name)) or {}
                if status.get('state') == 'running' and time.time() - status.get('started', 0) < STUCK_SECONDS:
                    return {'id': name[:-5], 'state': 'running'}
        backup_id = secrets.token_hex(16)
        target, status_path = self._paths(backup_id)
        job = {
            'db': self.env.cr.dbname,
            'pg_dump': find_pg_tool('pg_dump'),
            'pg_env': exec_pg_environ(),
            'filestore': config.filestore(self.env.cr.dbname),
            'manifest': self._manifest(),
            'zip': target,
            'status': status_path,
        }
        job_path = os.path.join(self._dir(), backup_id + '.job')
        descriptor = os.open(job_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as f:
            json.dump(job, f)
        with open(status_path, 'w') as f:
            json.dump({'state': 'running', 'started': time.time()}, f)
        worker = os.path.join(odoo_module.get_module_path('donialink_agent'), 'backup_worker.py')
        # its own session: it keeps running when the Odoo worker that started it is recycled
        subprocess.Popen([sys.executable, worker, job_path], start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {'id': backup_id, 'state': 'running'}

    @api.model
    def _status(self, backup_id):
        self._check_allowed()
        _target, status_path = self._paths(backup_id)
        status = self._read_status(status_path)
        if status is None:
            raise ToolError('not_found', 'Unknown backup', status=404)
        if status.get('state') == 'running' and time.time() - status.get('started', 0) > STUCK_SECONDS:
            status = {'state': 'failed', 'error': 'The backup process stopped (no result after 6 hours)'}
        status.pop('pid', None)
        stamp = time.strftime('%Y-%m-%d_%H-%M', time.gmtime(status.get('started', time.time())))
        return dict(status, id=backup_id, database=self.env.cr.dbname, filename=f'{self.env.cr.dbname}_{stamp}.zip')

    @api.model
    def _chunk(self, backup_id, offset=0, length=CHUNK):
        self._check_allowed()
        target, _status = self._paths(backup_id)
        if not os.path.exists(target):
            raise ToolError('not_ready', 'This backup is not ready', status=409)
        length = max(1, min(int(length), MAX_CHUNK))
        with open(target, 'rb') as f:
            f.seek(max(0, int(offset)))
            return f.read(length)

    @api.model
    def _delete(self, backup_id):
        target, status_path = self._paths(backup_id)
        for path in (target, status_path):
            if os.path.exists(path):
                os.remove(path)
        return {'id': backup_id, 'deleted': True}
