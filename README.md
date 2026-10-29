# DoniaLink Agent Connector | موصل وكيل دنيا لينك

Odoo 18 Community · Developed by **Yousif Shakir** · [donialink.com](https://www.donialink.com)

The bridge between a client's Odoo database and the DoniaLink control platform
(gateway, AI agents, task management). The gateway is the MCP server; this module
is the secure tool API it calls inside each client database.

```
DoniaLink app / AI agents ──MCP──▶ Gateway ──HTTPS + agent key──▶ Odoo (this module)
```

## What it provides

| Area | What it does |
|---|---|
| Agent API | `GET /donialink/agent/v1/ping`, `GET .../tools`, `POST .../tools/<name>`, `GET .../resources[/<name>]`, `GET .../files/<id>` |
| Tool registry | Each tool has a name, English description, Arabic/English title and JSON input schema that AI models read. Other modules add tools by inheriting `donialink.agent.tools`. |
| Technical user | **DoniaLink Agent** (`donialink_agent`), with no password. Tools run as this user, so its groups limit what the agent can read. |
| Authentication | Odoo API keys with the scope `donialink_agent`: hashed, can expire, can be revoked. These keys **do not** work for XML-RPC/JSON-RPC. |
| Protection | On/off switch, IP allow-list. Read tools are rolled back, so they can never change data. |
| Audit log | Every tool call: tool, who asked (from the gateway), parameters, status, duration, IP. Purged after N days. |
| Instance tools | `instance_info`, `list_modules`, `instance_health`, `list_users` |
| Generic read tools | `describe_model`, `search_records`, `aggregate_records`, limited by the agent user's access rights |
| Recipes and stock tools | `recipe_costs`, `recipe_card`, `products_without_recipe`, `food_cost_overview`, `ingredient_consumption`, `stocktake_results`, `waste_summary`, `ingredient_price_changes`, `stock_levels` |
| Sales tools | `sales_summary` (by day, hour, weekday, product, category, payment method, cashier, POS), `cancelled_orders`, `discounts_summary` |
| Cash and expense tools | `pos_sessions`, `cash_differences`, `cashier_payouts`, `expenses_summary`, `bills_to_pay` |
| Accounting tools | `profit_and_loss`, `balance_sheet`, `trial_balance`, `vendor_balances`, `pos_vs_accounting_sales` (figures as JSON) |
| Report exports | `export_report`: the PDF or XLSX file of a DoniaLink Accounting report (P&L, balance sheet, trial balance, general ledger, partner ledger, aged balance), the same file as the report buttons, Arabic labels with `lang=ar`. Needs `base_accounting_kit` 18.0.5.1.0+. Answers with a download link valid 24 hours (see below). |
| Soft requirements | Each tool lists the models it needs (`requires`). It is hidden where they are missing, so the connector installs on every client without depending on `pos_recipe_costing`, `point_of_sale` or `stock`. |
| Business timezone | Date tools cut days in local time (setting, else the administrator's timezone). Set it to `Asia/Baghdad`. |
| Test console | *Settings › DoniaLink Agent › Test Console* (`/donialink/agent/console`, administrators only): pick a tool, edit its JSON parameters, run it, copy a curl command. Runs the same pipeline as the API and is logged in the audit log. |
| AI notes | `agent/AGENT.md` (conventions) and `agent/recipes.md` (recipes, food cost, stocktakes) and `agent/sales_cash.md` (orders, cancellations, cash, expenses) and `agent/accounting.md` (accounting reports and exports) are served as resources. |
| DoniaLink Platform | Every 15 minutes the database **announces** itself to the platform (default `https://app.donialink.com`): name, address, company, versions, tool count, no key. It is listed there as an *available agent*. **Connect to platform** creates an agent key and sends it; with an enrollment code the platform approves it at once, without one the platform owner approves it. Then the job sends heartbeats instead. *Disconnect* revokes the key. |

## Connect to the DoniaLink Platform (recommended)

1. Install **DoniaLink Agent Connector** (18.0.1.2.0+). The database appears on the platform's
   *Agents* page as **available** within 15 minutes (*Contact platform now* to see it at once).
2. On the platform, *Agents › Enrollment code* (for an existing client or a new one).
3. In Odoo, *Settings › General Settings › DoniaLink Agent › DoniaLink Platform › Connect to platform*:
   check the address the platform should use for this database, paste the code, **Connect**.

Without a code the database shows as *waiting for approval* until the owner approves it. Switch off
**Announce** to stop the anonymous announcements.

## Manual setup on a client database

1. Install **DoniaLink Agent Connector**.
2. Go to *Settings › General Settings › DoniaLink Agent*: set the **Business Timezone** (`Asia/Baghdad`) and, optionally, the gateway IP addresses.
3. Go to *Settings › DoniaLink Agent › API Keys › New Agent Key* and copy the key (it is shown once).
4. In the gateway, add the tenant: base URL of the database plus the key.
5. Check the connection:

```bash
curl -H "Authorization: Bearer <key>" https://client.example.com/donialink/agent/v1/ping
```

The database is the one Odoo selects for the request, as for the web client. Behind Nginx, run Odoo with
`--proxy-mode` so that the IP allow-list sees the real client address.

### Several companies in one database

A database may hold several companies (branches). The connector reports them to the platform (`companies` in the
ping, announce and heartbeat: id, name, currency). The tools add all companies up unless given `company_id`, and
answer in the currency of the company asked for. On the platform a project picks one company (or all).

### Several databases on one address

One address may serve several databases (one Odoo server, no `dbfilter`, e.g. behind a Cloudflare Tunnel).
Odoo then picks a request's database from its session cookie, and without one the connector's routes answer
404. The DoniaLink Platform handles it: the connector reports its database name when it connects, and the
platform asks Odoo's login page for a session on that database (`/web/login?db=NAME`, the way the database
selector does it), then calls the API with that session. Nothing to set up; two conditions:

* the database must be allowed by the server's `dbfilter` (no filter: every database is);
* each database has its own agent key: a key made in one database is refused in another.

For calls of your own (curl), do the same: open `/web/login?db=NAME` once, keep the `session_id` cookie and send
it with the `Authorization` header. One hostname per database (with `dbfilter = ^%d$`) also works.

## Calling a tool

```bash
curl -X POST https://client.example.com/donialink/agent/v1/tools/list_modules \
  -H "Authorization: Bearer <key>" -H "Content-Type: application/json" \
  -d '{"params": {"origin": "custom"}, "lang": "ar",
       "actor": {"id": "staff-3", "name": "Yousif"}, "request_id": "c0ffee"}'
```

Response: `{"ok": true, "tool": "list_modules", "result": {...}}` or
`{"ok": false, "error": {"code": "invalid_params", "message": "..."}}`.

| HTTP | code | meaning |
|---|---|---|
| 400 | `invalid_params`, `invalid_json`, `invalid` | Fix the request |
| 401 | `unauthorized` | Missing, invalid or expired key |
| 403 | `ip_not_allowed`, `access_denied`, `model_not_allowed`, `field_not_allowed`, `disabled` | Not allowed |
| 404 | `unknown_tool`, `unknown_resource` | |
| 404 | `unknown_file`, `expired` | File download: not a tool file, or older than 24 hours (export it again) |
| 413 | `file_too_large` | The file a tool made is over 20 MB: shorter period or narrower search |
| 503 | `disabled` | Agent API switched off |
| 500 | `internal_error` | Bug: see the Odoo log |

## Files (report exports)

`export_report` answers with the file's metadata instead of its content:

```json
{"ok": true, "tool": "export_report", "result": {
  "report": "trial_balance", "format": "xlsx", "options": {"date_from": "2026-09-01", "date_to": "2026-09-30", ...},
  "file": {"id": 812, "name": "Trial Balance 2026-09-01 - 2026-09-30.xlsx", "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
           "size": 9214, "download_url": "https://client.example.com/donialink/agent/v1/files/812",
           "expires_at": "2026-10-22 09:15:02"}}}
```

```bash
curl -H "Authorization: Bearer <key>" -OJ https://client.example.com/donialink/agent/v1/files/812
```

* The download needs the agent key (same on/off switch and IP allow-list as the API), or the
  Odoo session of an administrator (the test console shows a **Download** link).
* The file is an attachment of the audit log line of the export (not public). It is
  stored after the read-only rollback, so read tools can return files. Downloads are logged too
  (`file_download`).
* Links expire after 24 hours (`expires_at`, UTC); the daily purge job deletes older files.
* Other tools can return a file the same way: `FILE_KEY` in `models/agent_registry.py`.

## Adding tools

Tools for DoniaLink's own modules live in this module (see `models/agent_tools_recipes.py`):
inherit `donialink.agent.tools` and declare the models the tool needs with `requires=(...)`.
Curated tools read with `sudo()` and return only what their description promises. The generic
tools stay limited by the agent user's access rights.

For third-party modules you can also create a small bridge module, e.g. `pos_recipe_costing_agent`, that depends on
`donialink_agent` and the module to expose. Set `auto_install: True` so that it
installs itself wherever both are present.

```python
from odoo import models
from odoo.addons.donialink_agent.models.agent_registry import ToolError, agent_tool


class AgentTools(models.AbstractModel):
    _inherit = 'donialink.agent.tools'

    @agent_tool(
        name='stocktake_results',
        title='Stocktake results', title_ar='نتائج الجرد',
        category='stock',
        description="System vs counted value of ingredient stocktakes, with the biggest variances ...",
        params={'type': 'object', 'properties': {
            'date_from': {'type': 'string', 'description': 'YYYY-MM-DD'},
            'top': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 10},
        }},
    )
    def _tool_stocktake_results(self, date_from=None, top=10):
        ...
        return {...}  # JSON-serializable data only

    def _agent_resources(self):
        return super()._agent_resources() + [{
            'name': 'recipes', 'title': 'Recipes and food cost', 'description': '...',
            'module': 'pos_recipe_costing_agent', 'path': 'pos_recipe_costing_agent/agent/AGENT.md',
        }]
```

The bridge module also gives the agent user the read groups its tools need:

```xml
<record id="donialink_agent.user_agent" model="res.users">
    <field name="groups_id" eval="[(4, ref('point_of_sale.group_pos_user'))]"/>
</record>
```

Rules for tools:

* Method name `_tool_<name>`: private, so it can't be called through RPC.
* Write the description for an AI model that has never seen the module: say what it
  returns and when to use it.
* Always accept date ranges and an optional `company_id` (multi-company is planned).
* Use `access='write'` only for tools that must change data. Read tools are rolled back.
* Return numbers, not formatted strings. Datetimes go out in UTC through `self._json_safe()`.
