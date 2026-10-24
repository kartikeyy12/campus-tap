# Architecture notes

## Idea

The card is only a key. It stores no balance and no attendance. Every tap makes the chip build a fresh
URL containing:

- `p`: encrypted (UID, tap counter), AES-128 CBC under the meta-read key
- `m`: 8 byte CMAC over the same, keyed from the card's file-read key and the UID and counter

The reader forwards that URL to the server, signed with its own device secret. The server decrypts,
checks the MAC, checks the counter only ever goes up, and then acts. Cloning the visible data gains
nothing: a replayed URL has an old counter and is rejected.

## Flow of one tap

1. Reader selects the NDEF application, reads the NDEF file (32 byte chunks, one uninterrupted run, so
   the card's counter moves once).
2. Reader POSTs `{ndef, charge_id?}` to `/api/tap` with `X-Device-Id` and `X-Signature` (HMAC-SHA256 of
   the body).
3. Server, inside one `BEGIN IMMEDIATE` transaction: verify device, verify card, apply the mode rule,
   write the tap row (UNIQUE uid+counter), write ledger rows, return a short result for the OLED.
4. Reader polls `/api/poll` once a second while idle to learn the mode and any armed charge.

## Modes

Set per reader by the admin. `idle`, `attendance` (needs an open class session), `mess` (needs an open
meal session), `vendor` (needs an armed charge from the vendor page). Sessions are opened by an admin,
not by the clock.

## Rules enforced in the database, not just the code

- Ledger and events are append-only (triggers block UPDATE and DELETE).
- No overdraft on student or vendor accounts (trigger).
- One armed charge per reader, one open session per reader, one check-in per student per session.
- One live card per student. Blocking a card kills it at once; unblocking needs the student PIN.
- A duplicate tap (same uid+counter) returns the original answer with `duplicate: true` and changes nothing.
  A lower counter than already seen is a REPLAY.

## Money

Integer rupees, double entry. A system account funds demo top-ups. A charge goes
armed, then completed, expired (30 s) or cancelled. The reader sends the `charge_id` it showed, so if the
vendor changes the amount while the student is tapping the answer is CHARGE_CHANGED and nothing is paid.
This is campus credit in a demo ledger, not real money.

## Not done

- Rejecting two cards in the field at once (the reader reads whichever one the PN532 selects).
- Creating students and vendors from the UI (seed script only).
- Real card provisioning and PN532 reads on a real NTAG 424 (see README).
