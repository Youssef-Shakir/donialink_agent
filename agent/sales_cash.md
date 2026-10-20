# Sales, cash and expenses

## Orders

- A **POS order** is one ticket. Sales count only orders in state `paid`, `done` or
  `invoiced`. Amounts in `sales_summary` **include tax** unless the field says
  `untaxed`.
- **Cancelled orders** were entered at the register and then cancelled before payment.
  They are not sales. Some cancellations are normal (input mistakes, customer changed
  their mind). Many cancellations by one cashier, or of items that were already
  prepared (shisha/أراكيل, coffee), are a control issue. Managers usually require a
  manager approval and a reason for each cancellation.
- **Refunds** are orders with a negative total. They reduce sales.
- **Discounts** are either a % on a line or a global discount line (discount product).
  `discounts_summary` gives the amount given away (tax included).
- The **cashier** of an order is the POS employee when employee login is used,
  otherwise the Odoo user. The system does not record who pressed "cancel": the
  cashier of the order is the person responsible.

## Sessions and cash

- A **session** (جلسة كاشير) is one register shift. At closing the cashier counts the
  cash. *Difference = counted − expected*. **Negative = shortage** (نقص), positive =
  overage (زيادة). Shortages that repeat for the same cashier need follow-up.
- **Cash in / out** (`cashier_payouts`): money taken from the register during the
  shift to pay purchases, suppliers, staff advances (سلف) or salaries, or money put in.
  Each move is classified into an expense category or a partner payment.
  "Not classified" moves still have to be categorized by a manager.
- The cash that *should* be in the register = opening + cash sales − cash out + cash
  in. Compare it with the counted cash, and with the money handed to the owner.

## Expenses and bills

- `expenses_summary` covers expenses recorded in *Expenses & Cash Control*: paid now
  (cash or bank) or **to pay later** (a vendor bill). Cash taken from the register is
  in `cashier_payouts`, not here, so don't add the two without checking for
  duplicates.
- `bills_to_pay` lists what is still owed on "to pay later" expenses.
  Purchase-order vendor bills are separate.

## Reading the numbers

- Dates and hours are in the business timezone (usually Asia/Baghdad). Busy cafés
  often sell after midnight. The `day` keys follow local dates.
- Amounts are in the company currency (usually IQD, no decimals).
- Compare **sales** with **accounting** carefully. POS sales are posted when a session
  is closed, so open sessions are in POS figures but not yet in accounting.
