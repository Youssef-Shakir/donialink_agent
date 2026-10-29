# Accounting reports

The profit and loss and the balance sheet come from **DoniaLink Accounting**
(`base_accounting_kit` 18.0.5.1.0+), the same engine clients use to print reports, so
the numbers you give match their PDFs. `engine` in the result says
`base_accounting_kit`. On a database without it, `engine` is `account_types` (same
structure, computed from journal items by account type). Entries are **posted** only,
unless `target_moves = all`.

## Profit and loss (`profit_and_loss`)

| Section | Account types |
|---|---|
| Revenue | income (sales) |
| Cost of revenue | expense_direct_cost (cost of goods sold, i.e. ingredients) |
| Gross profit | revenue − cost of revenue |
| Other income | income_other (cash gains, stocktake gains...) |
| Operating expenses | expense (salaries, rent, utilities, gas, maintenance, ads...) |
| Depreciation | expense_depreciation |
| **Net profit** | gross profit + other income − expenses − depreciation |

Amounts are positive for income and for expenses. A negative `net_profit` is a loss.
`pct_of_revenue` makes months comparable.

## Balance sheet (`balance_sheet`)

- Cumulative from the first entry up to the date. *Assets = liabilities + equity +
  unallocated earnings*, and `check` must be 0.
- **Unallocated earnings** = profit or loss not yet closed into equity (in Odoo, all
  P&L since the start, until a year-end closing entry moves it).
- Reports printed **before DoniaLink Accounting 18.0.5.1.0** were wrong in two ways:
  the balance sheet summed only the movements between *date from* and *date to*, and
  equity (e.g. setup capital, كلفة تأسيسية) appeared under "Liability". If a user
  compares with an old PDF, explain this. The current figures are the correct ones.

## Trial balance (`trial_balance`)

- Same layout as the kit's Trial Balance: debit, credit and balance of the period per
  account. `include_opening` adds the opening balance and ending balance.
- `total_debit` = `total_credit` when entries are balanced.
- **`suspicious_accounts`**: accounts whose debit or credit is implausibly large
  (≥ 10¹²). This comes from a data error, typically a stock move with a wrong quantity
  that was later reversed, so the balance looks normal while the turnover is absurd.
  Tell the user so it can be corrected.

## Supplier and customer balances (`vendor_balances`)

- Open (unreconciled) items on payable accounts (suppliers, and employees when they
  use a payable account) or receivable accounts, aged by due date.
- Negative = advance paid or credit note not yet used.
- POS sales use an internal receivable (*Accounts Receivable (PoS)*) that is cleared
  at session closing. A remaining balance there means a session problem.

## POS vs accounting (`pos_vs_accounting_sales`)

POS sales are posted to accounting **when the session is closed**, on the closing
date. Differences normally come from open sessions, sessions closed after the period
end, or earlier sessions posted in the period. `unexplained` is what remains.
Cash differences may be posted on income accounts and are shown separately.

## Exporting reports (`export_report`)

Makes the **PDF or Excel (XLSX) file** of a DoniaLink Accounting report, the same file
the user gets from the report's Print / Export buttons, with the same figures.

**When to use it:** the user wants a document: "send me the trial balance", "I need the
P&L as PDF for the accountant", "export the general ledger to Excel". **When not to:** to
answer a question about figures ("what was the net profit in September?"), use the data
tools (`profit_and_loss`, `balance_sheet`, `trial_balance`, `vendor_balances`): you can
read their JSON, you cannot read the file.

| report | what it is | period |
|---|---|---|
| `profit_and_loss` | revenue, costs, expenses, net profit | period (default this year) |
| `balance_sheet` | assets, liabilities, equity | as of `date_to` (default today) |
| `trial_balance` | initial balance, debit, credit, end balance per account | period (default this month) |
| `general_ledger` | every journal item per account, running balance | period (default this month) |
| `partner_ledger` | journal items per customer / supplier | period (default this year) |
| `aged_balance` | open amounts by age (not due, 1-30 … 91-120, older) | as of `date_to` (default today) |

Options (all optional, inside `options`): `date_filter` (`this_month`, `last_month`,
`this_quarter`, `last_quarter`, `this_year`, `last_year`, `today`, `custom`), or simply
`date_from` / `date_to` (means `custom`); `target_move` (`posted` / `all`); `comparison`
(`previous_period` / `previous_year`, P&L and balance sheet only); `hide_zero`;
`partner_type` (partner ledger: `both` / `receivable` / `payable`; aged balance:
`receivable` / `payable`); `search` (ledgers and aged balance). Unknown or misplaced
options are refused with `invalid_params`.

- `format`: `xlsx` (default; ledgers fully unfolded) or `pdf` (the printed report).
- `lang = ar` gives Arabic labels and an Arabic file name.
- `result.options` are the options actually used: tell the user the resolved period.
- Give the user `result.file.download_url`. It needs the agent key (the gateway proxies it)
  and is valid **24 hours** (`expires_at`, UTC). After that, export again.
- Large ledgers over a long period can exceed 20 MB (`file_too_large`): use a shorter
  period or `search`.

## Stock accounts check (`stock_accounts_check`)

For databases with automated (real-time) stock valuation. It reads the accounts from the product
categories (valuation, stock input, stock output) and checks, at the end of `date_to`:

- **valuation**: valuation account balance − stock value (valuation layers) ≈ 0;
- **incoming**: stock input balance + received not billed − billed not received ≈ 0 (the account
  normally holds a credit equal to goods received but not billed);
- **outgoing**: stock output balance ≈ 0.

`period_movements` splits the period's movements on the input / output accounts into receipts,
vendor bills, point of sale, deliveries and other; `other_entries` lists the largest "other"
entries (stock count updates such as "Product Quantity Updated", miscellaneous entries): they are
usually what leaves a residual. A residual that is not new (same as last period) is a historical
balance for the accountant to clear once.
