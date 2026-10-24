# Demo script (about 4 minutes)

Setup: server running, database freshly seeded, reader (or `tools.simtap`) connected, admin page open on
the laptop, student page open on a phone logged in as 24ECE1020 (PIN 1234), vendor page on a second tab
logged in as `canteen`.

1. **Attendance.** Admin: set reader to attendance, open a class session. Tap the card. OLED says
   PRESENT with the roll number; the admin list shows it. Tap again: it says already marked, nothing is
   double counted.
2. **Mess.** Admin: set reader to mess, open lunch. Tap: MEAL OK. Tap again: already eaten. Student phone
   shows the meal in history.
3. **Payment.** Admin: set reader to vendor. Vendor page: charge 40. The reader shows the amount and the
   countdown. Student taps: PAID, balance drops by 40 on the phone. Vendor balance goes up by 40.
4. **Refusals.** Charge 5000: NO FUNDS, nothing moves. Charge 30, change it to 50 while the student is
   tapping: the reader refuses the old amount.
5. **Lost card.** Student phone: Block card. Tap: BLOCKED. Unblock with the PIN: works again.
6. **Why it is safe.** Admin ledger page: every row balances, nothing can be edited. A tampered or
   replayed tap shows as a rejected event.

Backup if the reader misbehaves: run the same steps with `python -m tools.simtap` or
`python -m tools.smoke`, same server, same pages.
