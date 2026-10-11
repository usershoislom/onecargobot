# Playbooks by message type

First decide the batch's main `doc_type` (the document if there is one,
otherwise what the text is about). Then follow that type's playbook below:
what to extract, what to **check**, and which rows to create. Anything the
playbook says to check goes into `checks` as short findings, one per line:
"✓ Caption rates match contract Exhibit A", "✗ Carrier signature missing",
"? Effective date not stated in the document".

If the batch mixes types (e.g. a contract and a direct-deposit form for the
same driver), pick the main one for `doc_type`, but apply every relevant
playbook and create rows for all of them.

**Who to assign**: when the user @mentions people in the message, those
people are `assigned_to` (map each @username to their name via COLLEAGUES;
keep unknown @usernames as written). Otherwise use the role hints below
only if COLLEAGUES lists people with that role; else leave `null`.

---

## driver_contract: driver / independent contractor agreement

Extract: carrier (the motor carrier company), driver's full name, driver's own
company/LLC if any, effective/start date, pay structure (CPM loaded/empty, team
vs solo, percentage, bonuses), weeks out / days home, escrow, deductions, term.

Check:
- User's caption vs. the contract (rates, start date, schedule). Confirm each
  matching number with ✓ and flag each mismatch with ✗ quoting both values.
  Team rates are often split into base + performance bonus; add them up
  before comparing.
- Signatures and dates from **both** parties (driver and carrier). An empty
  carrier "BY:" line is ✗.
- Driver name consistent across pages.
- Compare with RECENT ROWS for this driver: a previous rate means this is a
  rate change. Mention the old → new rate.

Rows: one **Action** "Update pay rates in payroll for {driver}" with all rates in
`problem` (e.g. "Team loaded $1.00/mi (0.70 base + 0.30 bonus), team empty
$0.35/mi, solo loaded $0.65/mi, solo empty $0.30/mi; 4 weeks out, 4-5 days
home; effective 10/09/2026"). `amount` = null (rates per mile are not an amount).
Add a separate **Follow-up** row for each problem found (e.g. "Get carrier
signature on contract"). Role hint: accounting/payroll, safety for compliance.

Example. Caption: "New updated contract deal for John Carter starting
10.09.2026, 1.00 per mile as a team loaded, 0.35 unloaded, 0.65 solo loaded,
0.30 unloaded, 4 weeks out 4-5 days off @anna_acc @sam_safety". The PDF is
ABC Trucking LLC's ICD agreement with J-C Transport LLC, Exhibit A shows team
0.70 + 0.30 bonus loaded, 0.35 empty, solo 0.65/0.30, 4 weeks, 4-5 days, signed by
the driver on 10/07/2026, carrier signature blank. →
- doc_type `driver_contract`
- Row 1: company "ABC Trucking LLC", driver "John Carter", action "Update pay
  rates in payroll", status Action, assigned "Anna …, Sam …", deadline null,
  problem with all rates + "effective 10/09/2026; driver's LLC: J-C Transport LLC".
- Row 2: same company/driver, action "Get carrier signature on contract",
  status Follow-up, problem "Contract signed by driver 10/07/2026; carrier BY/DATE lines blank".
- checks: "✓ Team loaded $1.00 = 0.70 base + 0.30 bonus (Exhibit A)",
  "✓ Empty/solo rates match", "✓ 4 weeks out / 4-5 days home match",
  "✗ Carrier signature and date missing (pages 4–7)",
  "? Start date 10/09/2026 is from the caption; the contract only shows the signing date 10/07/2026".

## pay_rate_change: new pay terms without a full contract
Same as driver_contract, minus the signature checks. Always state old → new if
RECENT ROWS show the previous rate.

## direct_deposit: direct deposit / banking form

Extract: payee name and business, bank name, account type, form date,
**last 4 digits only** of the account (and routing) number.

Check: signature present, date present, account type ticked, account and
routing number fields filled in and readable (if not: ✗ and a Follow-up to get
a complete form), and any required attachment (voided check / bank letter). If the form says one
must be attached and the box/page is empty, that's ✗.

Rows: **Action** "Update direct deposit info in payroll" (problem: bank, account
type, acct ending ####, form date, effective before next settlement).
Add a **Follow-up** "Request voided check from {name}" when it's missing.
Role hint: accounting/payroll.

Example. Caption "New Direct Deposit Authorization Form for Mark Lee". The PDF
is signed 10/08/2026, Mark Lee / ML Cargo LLC, Chase, checking, acct ending
1234, voided check box empty. →
- doc_type `direct_deposit`
- Row 1: company "ML Cargo LLC", driver "Mark Lee", action "Update direct deposit
  info in payroll", status Action, problem "New DD form signed 10/08/2026: Chase
  checking, acct ending 1234. Replaces previous banking info."
- Row 2: action "Request voided check for direct deposit", status Follow-up,
  problem "Form requires a voided check for checking accounts; none attached."
- checks: "✓ Signed and dated 10/08/2026", "✓ Checking account selected",
  "✗ Voided check not attached (required by the form)".

## onboarding_docs: CDL, medical card, MVR, application, W-9, insurance cert
Extract: whose document, type, number's last 4 only, **expiration date**.
Check: expired or expiring within 30 days → ✗ and a Follow-up with deadline =
expiration date. Name matches the driver. Role hint: safety.

## termination: driver leaving / let go
Extract: driver, last day, reason, equipment to return, escrow.
Rows: Actions for final settlement, equipment/fuel card/ELD return, escrow
return (deadline 45 days after termination if the contract says so). Role
hint: accounting + safety.

## rate_confirmation: broker rate con
Extract: broker, load #, pickup/delivery dates and cities, rate (`amount`),
detention/lumper terms. Check: rate matches the caption, dates make sense.
Usually a **Note** unless the user asks for something.

## invoice: bill to pay or invoice sent
Extract: vendor/customer, invoice #, `amount`, due date → `deadline`.
Check: total = sum of lines, due date. Status Action (to pay/collect),
Done if marked paid. Role hint: accounting.

## settlement: driver pay settlement / statement
Extract: driver, period, gross, deductions, net (`amount` = net).
Check: math (gross − deductions = net), unusual deductions, rate per mile vs
known contract rate in RECENT ROWS. Flag issues as rows only when wrong.

## receipt_expense: lumper, fuel, repair, toll, scale receipts
Extract: vendor, `amount`, date, load # if shown, who paid.
Check: amount on the receipt matches the caption; receipt is legible.
Rows: Action "Reimburse {type} to {driver}" when a driver paid out of pocket,
else Note. Role hint: accounting.

## bol_pod: bill of lading / proof of delivery
Extract: shipper, receiver, load/PO #, pieces/weight, delivery date.
Check: signed by receiver, notes of shortage/damage ("OS&D", "refused") → ✗ and a
Follow-up. Usually Note / Action "Send POD to broker/factoring".

## inspection_violation: roadside inspection, DOT violation, ticket
Extract: driver, date, level (I/II/III), violations, out-of-service, fine.
Check: clean inspection (bonus may apply per contract) vs violations
(chargebacks may apply). Rows: Action "Submit inspection report" / "Process
inspection bonus" / "Review violation". Role hint: safety.

## accident_damage: crash, cargo/truck/trailer damage photos
Extract: driver, truck/trailer #, location, date, damage, injuries, police report #.
Rows: Action "Report to insurance" + Follow-ups for missing info. Role hint:
safety. Ask if it's unclear whether anyone was hurt.

## repair_maintenance: breakdown, shop, PM service
Extract: truck/trailer #, shop, issue, quote/`amount`, ETA.
Rows: Follow-up while waiting on a shop/quote; Action when approval/payment is needed.

## load_issue: late, detention, TONU, layover, rejected load
Extract: load #, broker, driver, hours, `amount` claimed.
Rows: Action "Bill detention/layover to broker" or Follow-up while waiting.

## payroll_payment: payments sent/received, advances, deductions
Extract: who, `amount`, what for. Status Done when it happened; close the
matching open row via `closes_rows`.

## general_note: anything else
Use the general rules.
