# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.donialink_agent.models.agent_platform import KEY_NAME, check_platform_url


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


@tagged('post_install', '-at_install')
class TestAgentPlatform(TransactionCase):

    def setUp(self):
        super().setUp()
        self.platform = self.env['donialink.agent.platform']
        self.params = self.env['ir.config_parameter'].sudo()
        self.params.set_param('donialink_agent.api_enabled', 'True')
        self.params.set_param('donialink_agent.platform_announce', 'True')
        self.params.set_param('donialink_agent.platform_url', 'https://platform.example.com')
        for name in ('token', 'key_id', 'status', 'agent_id', 'base_url'):
            self.platform._set(name, False)
        self.agent = self.env.ref('donialink_agent.user_agent')
        # a real platform key may already exist in this database
        self.existing_keys = self._platform_keys()
        self.calls = []

    def _platform(self, *answers):
        answers = list(answers)

        def post(url, json=None, headers=None, timeout=None):
            self.calls.append({'url': url, 'json': json, 'headers': headers})
            return FakeResponse(*answers.pop(0))
        return patch('odoo.addons.donialink_agent.models.agent_platform.requests.post', side_effect=post)

    def _platform_keys(self):
        keys = self.env['res.users.apikeys'].sudo().search([('user_id', '=', self.agent.id), ('name', '=', KEY_NAME)])
        return keys - getattr(self, 'existing_keys', keys.browse())

    def test_announce_sends_no_key(self):
        with self._platform((200, {'ok': True, 'status': 'available'})):
            self.assertTrue(self.platform._heartbeat())
        call = self.calls[0]
        self.assertEqual(call['url'], 'https://platform.example.com/api/agents/announce')
        self.assertNotIn('key', call['json'])
        self.assertNotIn('Authorization', call['headers'])
        self.assertEqual(call['json']['database'], self.env.cr.dbname)
        self.assertTrue(call['json']['uuid'])
        self.assertEqual(self.platform._get('status'), 'available')

    def test_announce_can_be_switched_off(self):
        self.params.set_param('donialink_agent.platform_announce', False)
        with self._platform():
            self.assertFalse(self.platform._heartbeat())
        self.assertFalse(self.calls)

    def test_connect_sends_key_and_stores_token(self):
        answer = {'ok': True, 'status': 'connected', 'agent_id': 'a-1', 'agent_token': 'tok', 'client': 'Hajar'}
        with self._platform((200, answer)):
            self.platform._connect('https://platform.example.com', 'https://cafe.example.com', 'DL-CODE')
        body = self.calls[0]['json']
        self.assertTrue(self.calls[0]['url'].endswith('/api/agents/register'))
        self.assertEqual(body['enrollment_code'], 'DL-CODE')
        self.assertEqual(body['base_url'], 'https://cafe.example.com')
        # the key sent is a working agent key
        self.assertEqual(self.env['res.users.apikeys']._check_credentials(scope='donialink_agent', key=body['key']),
                         self.agent.id)
        self.assertEqual(len(self._platform_keys()), 1)
        self.assertEqual(self.platform._get('token'), 'tok')
        self.assertEqual(self.platform._get('status'), 'connected')

        # once connected, the job sends heartbeats with the token
        with self._platform((200, {'ok': True, 'status': 'connected', 'client': 'Hajar'})):
            self.platform._heartbeat()
        self.assertTrue(self.calls[1]['url'].endswith('/api/agents/heartbeat'))
        self.assertEqual(self.calls[1]['headers']['Authorization'], 'Bearer tok')

    def test_reconnect_replaces_the_key(self):
        answer = {'ok': True, 'status': 'pending', 'agent_id': 'a-1', 'agent_token': 'tok'}
        with self._platform((200, answer), (200, answer)):
            self.platform._connect('https://platform.example.com', 'https://cafe.example.com')
            self.platform._connect('https://platform.example.com', 'https://cafe.example.com')
        self.assertEqual(len(self._platform_keys()), 1)
        self.assertEqual(self.calls[1]['json']['agent_token'], 'tok')

    def test_refused_connection_removes_the_new_key(self):
        error = {'ok': False, 'error': {'code': 'invalid_code', 'message': 'Unknown enrollment code'}}
        with self._platform((400, error)), self.assertRaises(UserError):
            self.platform._connect('https://platform.example.com', 'https://cafe.example.com', 'WRONG')
        self.assertFalse(self._platform_keys())
        self.assertFalse(self.platform._get('token'))

    def test_platform_forgets_us(self):
        answer = {'ok': True, 'status': 'connected', 'agent_id': 'a-1', 'agent_token': 'tok'}
        with self._platform((200, answer), (401, {'ok': False, 'error': {'code': 'unauthorized'}})):
            self.platform._connect('https://platform.example.com', 'https://cafe.example.com')
            self.assertFalse(self.platform._heartbeat())
        self.assertFalse(self._platform_keys())
        self.assertFalse(self.platform._get('token'))
        self.assertEqual(self.platform._get('status'), 'disconnected')

    def test_disconnect_revokes_key(self):
        answer = {'ok': True, 'status': 'connected', 'agent_id': 'a-1', 'agent_token': 'tok'}
        with self._platform((200, answer), (200, {'ok': True})):
            self.platform._connect('https://platform.example.com', 'https://cafe.example.com')
            self.platform._disconnect()
        self.assertTrue(self.calls[1]['url'].endswith('/api/agents/disconnect'))
        self.assertFalse(self._platform_keys())

    def test_api_must_be_on_to_connect(self):
        self.params.set_param('donialink_agent.api_enabled', False)
        with self.assertRaises(UserError):
            self.platform._connect('https://platform.example.com', 'https://cafe.example.com')

    def test_wizard_address_without_request_is_the_stored_one(self):
        self.params.set_param('web.base.url', 'https://live.example.com')
        wizard = self.env['donialink.agent.platform.wizard'].create({'platform_url': 'https://app.donialink.com'})
        self.assertEqual(wizard.base_url, 'https://live.example.com')
        self.params.set_param('donialink_agent.platform_base_url', 'https://cafe.example.com')
        wizard = self.env['donialink.agent.platform.wizard'].create({'platform_url': 'https://app.donialink.com'})
        self.assertEqual(wizard.base_url, 'https://cafe.example.com')

    def test_platform_url_rules(self):
        self.assertEqual(check_platform_url('https://app.donialink.com/'), 'https://app.donialink.com')
        # plain http only inside local / private networks
        check_platform_url('http://donialink-app-1:8000')
        check_platform_url('http://10.8.0.1:8000')
        with self.assertRaises(UserError):
            check_platform_url('http://app.donialink.com')
        with self.assertRaises(UserError):
            check_platform_url('ftp://x')
