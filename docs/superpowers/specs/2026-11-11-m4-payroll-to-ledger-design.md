# M4 Payroll to ledger: design

Status: draft for owner review, 2026-11-11. Implements SPEC §8 milestone M4 (F9, F10, §4.5, §4.6, §7 item 4).
Builds on M1-M3 (ledger, receivables v2.2.0, expenses v2.3.0). Target release: 2.4.0.

## 1. Goal and success criteria

Make the existing payroll calculator post to the ledger, track what is owed to each tax agency, and record
payments to those agencies. FUTA and Georgia SUTA are added to the run so every employer liability exists.

Done when:

1. A saved run is a draft; approving it posts exactly one balanced journal entry, voiding it reverses that
   entry, and the run's stored figures never change after approval.
2. FUTA and SUTA are calculated, stored on the run and posted to their own payable accounts.
3. A tax payment can be recorded for each of four agency targets, posts Dr payable / Cr bank, and can be voided.
4. The Payroll liability report shows, per agency, the ledger balance owed, the unpaid amount by run period
   (oldest payments first) and the next due date, and shows any difference from the ledger instead of hiding it.
5. The tax engine's existing outputs are unchanged: the 73 engine tests and the golden cases pass untouched,
   and every figure on a run saved before M4 is unchanged.
6. The QuickBooks entries view and CSV export keep working unchanged (QBO stays the books until M7).
7. All M1-M3 rules hold: lock date, owner-only mutations, CSRF, 422-not-500, strict 2-decimal amounts,
   append-only history, CSV formula neutralization.

## 2. Scope

In: run lifecycle (draft, approved, voided, imported), approval posting, FUTA and SUTA calculation, payroll
account roles, payroll settings, tax payments, Payroll liability report (HTML, CSV, PDF), ledger-entry preview on
the run page, demo data, docs.

Out (stays out): the Payroll Summary report and all returns and W-2 worksheets (M5); semiweekly deposit
schedules; paying the health insurer or funding the HSA (done with M3 transfers, section 5); penalties and
interest (M3 expenses); QBO import of history (M6); removing the QuickBooks account-name config (M7); multiple
employees; paying wages through a bank feed.

## 3. Facts about the code this builds on

- `PayrollRun` (`payroll/payroll_run/models.py`) stores inputs and every computed figure, `tax_year`, and is
  created only when the user saves (`save_run`). `pay_period` is `YYYY-MM`, indexed but not unique; the service
  refuses a second saved run for a period. Year-to-date FICA wages come from the latest earlier saved run in the
  same year (`ytd_fica_before`, `ytd_fica_after`).
- `PayrollRunService._build_journal_entries` builds four to six QuickBooks entries from stored amounts and the
  runtime account names in `PayrollConfig` (`acct_*`). The admin page edits those names.
- Delete is allowed for any saved run today. The JSON API (`payroll/payroll_run/routers.py`) and the HTMX pages
  (`payroll/frontend/routers.py`) call the same service.
- `SourceType.PAYROLL_RUN` and `SourceType.TAX_PAYMENT` already exist. The default chart already has the
  Payroll Liabilities accounts including FUTA Payable and GA SUTA Payable.

## 4. Data model

New migration `e5f1b3c9d7a2_payroll_ledger.py` (head after `d4e0a2b8c6f1`), additive plus one data step.

```
payroll_run    (new columns)
               status (draft | approved | void | imported), pay_date (nullable),
               net_pay_source (account | owner, nullable), net_pay_account_id (nullable),
               entry_id (nullable), void_entry_id, void_date,
               futa_wages, futa, suta_wages, suta, suta_rate (rate used, 6dp)
payroll_settings   one row per tax_year: tax_year (unique), futa_rate, futa_wage_base, suta_rate,
               suta_wage_base, + singleton deposit rules (see section 8)
tax_payment    id, payment_date, agency (irs_941 | irs_futa | ga_dor | ga_dol), bank_account_id,
               amount, memo, status (posted | void), entry_id, void_entry_id, void_date, created/updated
```

Data step: every existing `payroll_run` row gets `status = 'imported'`, no entry, `pay_date` null, FUTA and SUTA
columns 0.00 (history; not recalculated). New saves are `draft`.

Database CHECKs and triggers (as in M2/M3):

- payroll_run: `status` in the four values; `approved` requires `pay_date`, `entry_id` and `net_pay_source`;
  `net_pay_source = 'owner'` iff `net_pay_account_id IS NULL`; `void` requires `entry_id`, `void_entry_id` and
  `void_date >= pay_date`; `draft` and `imported` carry no entry or void fields; FUTA, SUTA and wages >= 0.
- Trigger: on a run whose status is not `draft`, UPDATE of any input or calculated column is refused (only
  status, void fields, `modify_date` may change, and `approved -> void` is the only status move). A run is
  never deleted unless it is a draft (trigger).
- tax_payment: `amount > 0`, status/void field consistency, `void_date >= payment_date`.

New `SystemRole` values (type in brackets), frozen like Due to Owner once any run is approved or any tax payment
exists: `payroll_wages` [expense], `payroll_tax_expense` [expense], `payroll_federal_payable` [liability],
`payroll_ga_withholding_payable` [liability], `payroll_futa_payable` [liability],
`payroll_suta_payable` [liability], `payroll_health_payable` [liability], `payroll_hsa_payable` [liability].
The default chart binds them to the accounts it already has (Wages, Taxes, Federal Taxes (941/943/944),
GA Income Tax, FUTA Payable, GA SUTA Payable, S-Corp Health Insurance, Co. HSA). `seed-chart` and the
migration's data step bind them on an existing install only when those exact accounts exist and the role is
unbound; otherwise the owner binds them in Settings > Ledger and approval says which role is missing.

## 5. Calculation

`calc_payroll` and the engine are not touched. A new pure function `calc_unemployment(wages_before, wages_after,
rates)` in `payroll/tax/` returns FUTA and SUTA wages and amounts:

- Unemployment wages for a run are its `fica_wages` (this follows what the engine already treats as wages; the
  CPA question in SPEC §4.5 covers it). Year-to-date wages come from `ytd_fica_before` and `ytd_fica_after`, so
  no new year-to-date columns are needed.
- `futa_wages = min(base, after) - min(base, before)` (never negative); `futa = round_money(futa_wages * rate)`
  with defaults rate 0.006 and base 7,000.00. `suta_wages` and `suta` use the SUTA rate and base the same way
  (default base 9,500.00; the owner enters the assigned rate). Rounding is `round_money` (half-up), per run.
- Rates come from `payroll_settings` for the run's `tax_year`. Saving a run when that year's settings are
  missing is a 422 telling the owner to open Settings > Payroll. Rates used are stored on the run.
- Figures are computed at save and stored. Approval never recalculates; to change a draft the owner deletes it and
  saves again.
- Adding FUTA and SUTA does not change net pay, withholding or the employee-side totals. `total_employer_taxes`
  and `total_employer_cost` (existing columns) are not redefined; FUTA and SUTA are separate columns and the
  posted employer-tax expense is `total_employer_taxes + futa + suta`.

## 6. Lifecycle and posting

States: `draft -> approved -> void`, and `imported` (existing rows, history only). Saving creates a draft.

- **Save/recalculate (draft):** as today, plus FUTA and SUTA. One non-void run per `pay_period`.
- **Delete:** drafts only. Approved, void and imported runs are refused (history is kept).
- **Approve** (owner only): inputs `pay_date` (default last day of the period), `net_pay_source` (bank account,
  default, or Due to Owner) and the bank account. Chain rule: every earlier run in the same tax year must be
  `approved`, `imported` or `void`; a draft earlier in the year blocks approval (its year-to-date wages feed
  this run). Posts one entry dated `pay_date`, source `payroll_run`, source id = run id:

| Side | Account (role) | Amount |
|---|---|---|
| Dr | Wages (`payroll_wages`) | gross + health addback when health is in income tax + HSA |
| Dr | Employer taxes (`payroll_tax_expense`) | `total_employer_taxes` + FUTA + SUTA |
| Cr | Federal payable (`payroll_federal_payable`) | federal withholding + employee Social Security + employee Medicare (incl. additional) + employer Social Security + employer Medicare |
| Cr | GA withholding payable | Georgia withholding |
| Cr | FUTA payable | FUTA |
| Cr | SUTA payable | SUTA |
| Cr | Health payable | health premium, only when it is in income tax |
| Cr | HSA payable | HSA contribution, when > 0 |
| Cr | Bank account or Due to Owner | net pay |

Zero lines are dropped. Memos name the component ("Federal income tax withheld", "Employee Social Security
6.2% on $x", etc.), copied from the existing builder. The entry balances by the engine's own identity; the service
asserts it and refuses to post (422, nothing written) if the stored figures do not balance.
- **Void** (owner only): void date on or after `pay_date` and after the lock; refused when any later run in
  the tax year is `approved` (void in reverse order); posts the reversal and sets `void` (links kept). A
  period with a void run can be saved and approved again.
- **Locking order:** the eight payroll role accounts FOR SHARE (id order) and the net-pay bank account FOR SHARE,
  then all runs of the tax year FOR UPDATE in id order (so two approvals or an approval and a void cannot both pass
  the chain check), then the period lock FOR SHARE, then the per-source entry counter.
- **Paying the insurer and the HSA** are not part of the run. Pay the premium with an M3 transfer from the bank
  to the health payable account, fund the HSA with a transfer to the HSA payable account, and a premium paid
  directly (health not in income tax) is an ordinary M3 expense.

## 7. Tax payments

`TaxPaymentService.create(*, actor, payment_date, agency, bank_account_id, amount, memo=None)` and
`void(id, *, actor, void_date, memo=None)`. Agency maps to the payable role:
`irs_941 -> payroll_federal_payable`, `irs_futa -> payroll_futa_payable`, `ga_dor -> payroll_ga_withholding_payable`,
`ga_dol -> payroll_suta_payable`. Posting: Dr the payable account, Cr the bank (an asset account that is not a bound
role). The amount may exceed the balance owed (the difference is an overpayment shown as a negative balance);
penalties and interest are M3 expenses. Date rules, validation order, owner-only, audit and error classes follow the
M3 transfer service. Void needs a void date on or after the payment date and after the lock.

## 8. Liability report

`payroll_liability_report(session, as_of)` -> per agency: `balance` (ledger credit minus debit on the role
account at `as_of`), `periods` (approved runs, oldest first, each with the liability it created and the unpaid
amount) and `next_due`.

- Run liability per agency is read from the run's stored columns (federal: the five components above;
  GA DOR: Georgia withholding; FUTA; SUTA). Void runs and imported runs create no period.
- Unpaid by period: posted, non-void tax payments for the agency up to `as_of` are applied oldest period first.
  No applications table: the result is derived.
- `other` = `balance` minus the sum of unpaid period amounts. A non-zero `other` (opening balance, manual
  journal, imported history, an overpayment) is shown on its own line and is never aged or hidden.
- Due dates come from settings (singleton row on `payroll_settings`): per agency a cadence (`monthly` or
  `quarterly`) and a due day. Defaults, to be confirmed by the owner: IRS 941 and GA DOR monthly, day 15 of
  the following month; FUTA and GA DOL quarterly, the last day of the month after the quarter (FUTA is due
  quarterly only once the accumulated liability exceeds $500; the report shows the quarterly date regardless).
  Overdue is `due < today`. The existing free-text notes on `PayrollConfig` stay.
- HTML page plus `.csv` and `.pdf` through the existing export layer (`csv_safe`, `NoFetch`).

## 9. Screens, routes and API

- **Calculator page (`/`)** keeps Calculate and Save. History shows each run's status. A saved draft's panel
  gains an Approve form (pay date, net pay from bank or Due to Owner, bank account) and, once approved, a link to
  its ledger entry and a Void form. The QuickBooks entries panel and CSV export are unchanged.
- **Ledger preview:** the draft panel shows the entry that Approve would post (the table in section 6).
- **`/payroll/tax-payments`** list, new and detail with void; **`/reports/payroll-liability`** (+ `.csv`, `.pdf`);
  **Settings > Payroll** (FUTA/SUTA rates, wage bases per tax year, deposit rules). The Settings > Ledger role page
  lists the eight new roles. Nav entry "Payroll" gains Tax payments and the liability report.
- Routes: `POST /htmx/runs/{id}/approve`, `POST /htmx/runs/{id}/void`, plus the tax-payment, settings and report
  routes above. Every POST is owner-only with CSRF and is added to the route-protection sweep in the task that adds
  it. All inputs parsed strictly (ids, dates, amounts, enums); bad input is a 422 page, never a 500.
- **JSON API:** existing endpoints unchanged except `DELETE /payroll/runs/{id}` is refused for non-drafts and run
  responses gain `status`, `payDate`, `entryId`, `futa`, `suta`. No new API routes (approve, void and tax payments are
  page-only, like M3). The OpenAPI schema test is updated for the added fields.

## 10. Migration and deploy

Additive; the data step only marks existing runs `imported`. Release notes (RUNBOOK "Upgrading to v2.4.0"): stop
Watchtower, `pg_dump` first, then merge; bind the payroll roles if any are unbound; enter this year's FUTA and SUTA
rates in Settings > Payroll before saving the next run; confirm the deposit due-date rules. Downgrade refuses when any
run is approved, void or has an entry, or any tax payment exists.

## 11. Decisions to confirm (rulings made in this spec)

1. Unemployment wages equal FICA wages. If wrong: FUTA/SUTA wages differ for health and HSA treatment; the CPA
   question in SPEC §4.5 decides; the formula lives in one function.
2. FUTA and SUTA are computed at save and stored; approval never recalculates. If wrong: a draft saved before
   the rate was entered must be deleted and saved again.
3. Existing runs become `imported` and cannot be deleted. If wrong: a mistaken old run cannot be removed until M6.
4. Void requires reverse order (no later approved run). If wrong: correcting a middle month means voiding the later
   ones first.
5. Tax payments are applied to periods oldest first by derivation, with an `other` line for differences. If wrong:
   a payment meant for a specific quarter can show as paying an older one; fixable later with an explicit
   applications table.
6. One federal payable role holds withholding, Social Security and Medicare (as in your chart today). If wrong: split
   needs new roles and a migration of balances.
7. Payroll roles freeze after first use. If wrong: rebinding needs a journal.
8. Paying the insurer and HSA use M3 transfers. If wrong: a dedicated "pay premium" action is a later add.
9. The QuickBooks account-name config stays until M7 (duplicate of the roles on purpose). If wrong: two places to
   keep in step until cutover.

## 12. Testing and review focus

Layers as in M2/M3: engine/golden unchanged (run first, before and after), service unit tests, route and
permission sweeps (viewer, accountant, anonymous, no-CSRF), migration and trigger tests, Hypothesis properties,
real two-session races, browser check on seeded books, PDF export in Docker.

Must be tested:

- The posted entry balances for any generated run (property) and equals the stored figures line by line; zero
  lines dropped; health in and out of income tax; HSA zero and non-zero; net pay to bank and to Due to Owner.
- `calc_unemployment`: wages crossing the base mid-run, already over the base, zero wages, rate zero, rounding
  half-up at the cent, year start reset, imported runs in the chain.
- Chain rule: approve with an earlier draft is refused; approve after imported runs works; void out of order is
  refused; void then re-save and re-approve the same period; two approvals racing in one tax year (exactly one
  proceeds when order matters), approve vs void race, approve vs role rebind race, approve vs lock-date race.
- Immutability trigger: UPDATE of a figure on an approved, void or imported run is refused; draft delete allowed,
  others refused; the API and the page delete refuse non-drafts with the right messages.
- Existing runs: migration marks them `imported`, figures byte-identical before and after, the YTD chain across
  imported and new runs, the QuickBooks entries view for an imported run unchanged.
- Tax payment: create/void, lock date, bank account rules (bound roles refused), overpayment shown negative,
  void restores; the four agency-to-role mappings.
- Liability report: the ledger balance equals the sum of unpaid periods plus `other` on clean data (`other` is
  zero); payments apply oldest first across a partial payment; a manual journal creates `other`; as-of in the past;
  due dates for monthly and quarterly rules including December-to-January and quarter ends; CSV neutralisation;
  PDF in Docker.
- Roles: unbound role refuses approval with the role named; rebind after first approval refused; seed-chart binds
  on a fresh chart and leaves an existing install alone.
- Inputs: garbage dates, ids, amounts, enums, pay date before the period start, pay date in a locked period,
  net pay account that is a bound role, Due to Owner unbound.

## 13. Known follow-ups (not in M4)

Payroll Summary report and the returns (M5); semiweekly deposits; explicit tax-payment applications; split federal
payables; removing the QuickBooks account names and CSV export (M7); how approved runs made during the parallel run
relate to the QBO general-ledger import (M6 design must decide whether to void them before import).
