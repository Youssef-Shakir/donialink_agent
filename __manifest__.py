# -*- coding: utf-8 -*-
{
    'name': 'DoniaLink Agent Connector',
    'version': '18.0.1.2.0',
    'category': 'Administration',
    'summary': 'Secure tool API that lets the DoniaLink control app and its AI agents read this database',
    'description': """
DoniaLink Agent Connector | موصل وكيل دنيا لينك
==============================================

The bridge between this Odoo database and the DoniaLink control platform
(gateway, AI agents, task management).

* Versioned JSON API: ``/donialink/agent/v1/...``
* Tool registry: every tool has a name, a description and an input schema
  that AI models read. Other modules add their own tools by inheriting
  ``donialink.agent.tools``.
* Dedicated technical user, so Odoo access rights limit what the agent sees
* Authentication with Odoo API keys restricted to the agent scope
* IP allow-list, on/off switch, read-only enforcement for read tools
* Audit log of every call (tool, who asked, parameters, result, duration)
* Instance tools: version, installed modules, health, users
* Accounting report exports (PDF / XLSX) with 24-hour download links
* DoniaLink Platform: announces itself (no key) and connects with one click

Developed by Yousif Shakir - Donialink | تطوير يوسف شاكر
    """,
    'author': 'Donialink, Yousif Shakir',
    'maintainer': 'Yousif Shakir',
    'website': 'https://www.donialink.com',
    'license': 'LGPL-3',
    'depends': ['base_setup'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/agent_user_data.xml',
        'data/ir_config_parameter_data.xml',
        'data/ir_cron_data.xml',
        'wizard/agent_key_wizard_views.xml',
        'wizard/agent_platform_wizard_views.xml',
        'views/agent_log_views.xml',
        'views/agent_key_views.xml',
        'views/res_config_settings_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
