# Copy-paste curl examples. Start the server first: ./run.sh
# All requests authenticate with a prepared account key in X-API-Key.

EDITOR='X-API-Key: key-editor-1'
AUTHOR1='X-API-Key: key-author-1'
REV1='X-API-Key: key-reviewer-1'
REV2='X-API-Key: key-reviewer-2'
BASE=http://127.0.0.1:8000

# --- Author: submit manuscript (revision 1 is created automatically) ---
curl -sS -X POST "$BASE/author/manuscripts" \
  -H "$AUTHOR1" -H 'Content-Type: application/json' \
  -d '{"title": "Example Paper", "content": "v1 full text"}'

# --- Author: append a new revision; the old one can never be overwritten ---
curl -sS -X POST "$BASE/author/manuscripts/1/revisions" \
  -H "$AUTHOR1" -H 'Content-Type: application/json' \
  -d '{"content": "v2 full text"}'

# --- Editor: list prepared accounts to get reviewer ids ---
curl -sS "$BASE/editor/accounts" -H "$EDITOR"

# --- Editor: invite a reviewer to a *specific* revision ---
curl -sS -X POST "$BASE/editor/invitations" \
  -H "$EDITOR" -H 'Content-Type: application/json' \
  -d '{"revision_id": 1, "reviewer_id": 4}'

# --- Reviewer: see own invitations (contains no invitation ids of others) ---
curl -sS "$BASE/reviewer/invitations" -H "$REV1"

# --- Reviewer: accept then submit a 1..5 integer-scored review (once only) ---
curl -sS -X POST "$BASE/reviewer/invitations/1/respond" \
  -H "$REV1" -H 'Content-Type: application/json' -d '{"accept": true}'
curl -sS -X POST "$BASE/reviewer/invitations/1/reviews" \
  -H "$REV1" -H 'Content-Type: application/json' \
  -d '{"content": "solid contribution", "score": 4}'

# --- Editor: register a conflict (blocks invites/acceptance/submission;
#     withdraws any published version that used this reviewer) ---
curl -sS -X POST "$BASE/editor/conflicts" \
  -H "$EDITOR" -H 'Content-Type: application/json' \
  -d '{"author_id": 2, "reviewer_id": 4, "reason": "shared lab"}'

# --- Editor: publish a decision on one revision with >=2 clean reviews ---
curl -sS -X POST "$BASE/editor/decisions" \
  -H "$EDITOR" -H 'Content-Type: application/json' \
  -d '{"revision_id": 1, "review_ids": [1, 2], "conclusion": "minor revision"}'

# --- Author: read the frozen anonymized decision (410 once withdrawn) ---
curl -sS "$BASE/author/decisions" -H "$AUTHOR1"
curl -sS "$BASE/author/decisions/1" -H "$AUTHOR1"

# --- Editor: original frozen content + withdrawal reason remain visible ---
curl -sS "$BASE/editor/decisions/1" -H "$EDITOR"

# ===========================================================================
# Conference scheduling / linked rescheduling
# Times are UTC ISO-8601; rooms come from review_system/rooms.json.
# ===========================================================================

# --- Editor: list locally configured rooms ---
curl -sS "$BASE/editor/rooms" -H "$EDITOR"

# --- Editor: submit a group of arrangements (one manuscript may appear once).
#     Every item names a live published decision, room and UTC start/end.
#     The whole group shares one author-confirmation deadline, which must be
#     strictly before every involved new (and current old) slot start. ---
curl -sS -X POST "$BASE/editor/schedule/groups" \
  -H "$EDITOR" -H 'Content-Type: application/json' \
  -d '{
        "confirmation_deadline": "2030-05-01T00:00:00Z",
        "items": [
          {"manuscript_id": 1, "decision_id": 1, "room": "hall-a",
           "starts_at": "2030-06-03T10:00:00Z", "ends_at": "2030-06-03T11:00:00Z"}
        ]
      }'

# --- Editor: linked swap of two existing talks. decision_id is omitted for
#     manuscripts that already have a confirmed arrangement: their decision is
#     reused and only room/time change. New slots may overlap this group’s own
#     old slots (the cross-wise swap below); back-to-back is not a conflict. ---
curl -sS -X POST "$BASE/editor/schedule/groups" \
  -H "$EDITOR" -H 'Content-Type: application/json' \
  -d '{
        "confirmation_deadline": "2030-05-01T00:00:00Z",
        "items": [
          {"manuscript_id": 1, "room": "hall-a",
           "starts_at": "2030-06-03T11:00:00Z", "ends_at": "2030-06-03T12:00:00Z"},
          {"manuscript_id": 2, "room": "hall-a",
           "starts_at": "2030-06-03T10:00:00Z", "ends_at": "2030-06-03T11:00:00Z"}
        ]
      }'

# --- Author: list own confirmed talks + pending/past confirmations ---
curl -sS "$BASE/author/schedule" -H "$AUTHOR1"
curl -sS "$BASE/author/schedule/grouped" -H "$AUTHOR1"

# --- Author: answer only their own item (404 for another author’s item) ---
curl -sS -X POST "$BASE/author/schedule/items/1/respond" \
  -H "$AUTHOR1" -H 'Content-Type: application/json' -d '{"accept": true}'
curl -sS -X POST "$BASE/author/schedule/items/2/respond" \
  -H "$AUTHOR2" -H 'Content-Type: application/json' -d '{"accept": false}'

# --- Editor: confirmed schedule (?history=true adds superseded/voided slots),
#     current occupancy (confirmed + reserved), and group progress/end reasons ---
curl -sS "$BASE/editor/schedule" -H "$EDITOR"
curl -sS "$BASE/editor/schedule?history=true" -H "$EDITOR"
curl -sS "$BASE/editor/schedule/occupancy" -H "$EDITOR"
curl -sS "$BASE/editor/schedule/groups" -H "$EDITOR"
curl -sS "$BASE/editor/schedule/groups/1" -H "$EDITOR"

# A conflict that withdraws a scheduling decision voids its session and
# cancels any pending group involving it; see the conflict response fields
# voided_session_ids / cancelled_schedule_group_ids.

# ===========================================================================
# Speaker travel reimbursement (all amounts are integer cents)
# ===========================================================================

# --- Editor: grant one allowance to a manuscript with a confirmed talk.
#     The beneficiary is the manuscript's author; a confirmed reschedule that
#     keeps the same decision does not affect it. One per (manuscript, decision). ---
curl -sS -X POST "$BASE/editor/reimbursements/allowances" \
  -H "$EDITOR" -H 'Content-Type: application/json' \
  -d '{"manuscript_id": 1, "amount_cents": 120000}'

# --- Editor: register receipts (unique voucher number, owning author, face value) ---
curl -sS -X POST "$BASE/editor/reimbursements/receipts" \
  -H "$EDITOR" -H 'Content-Type: application/json' \
  -d '{"voucher_no": "VOC-2026-001", "author_id": 2, "amount_cents": 80000}'

# --- Editor: approve an expense against (allowance, same-author receipt, cents).
#     Rejected if it would exceed the allowance, the receipt face value, or the
#     fixed conference budget. One receipt may be split across several claims. ---
curl -sS -X POST "$BASE/editor/reimbursements/claims" \
  -H "$EDITOR" -H 'Content-Type: application/json' \
  -d '{"allowance_id": 1, "receipt_id": 1, "amount_cents": 60000}'

# --- Editor: place approved (not-yet-delivered) cents into a payment batch.
#     All-or-nothing; a claim may be delivered in several partial batches.
#     Retrying the same idempotency_key AND content returns the original list
#     and payment numbers; a different content with that key is a 409. ---
curl -sS -X POST "$BASE/editor/reimbursements/batches" \
  -H "$EDITOR" -H 'Content-Type: application/json' \
  -d '{"idempotency_key": "batch-2026-0001",
       "items": [{"claim_id": 1, "amount_cents": 60000}]}'

# --- Editor: record finance results, one payment number at a time (repeatable
#     across rounds). paid_cents <= instructed; the unpaid difference is
#     released and NOT re-queued. Same result is a no-op; a different amount
#     for an already-final payment is 409. Unreported payments stay encumbered. ---
curl -sS -X POST "$BASE/editor/reimbursements/batches/1/results" \
  -H "$EDITOR" -H 'Content-Type: application/json' \
  -d '{"results": [{"payment_no": "PMT-000001-0001", "paid_cents": 55000}]}'

# --- Editor: batch progress, then conference/allowance/receipt exposure ---
curl -sS "$BASE/editor/reimbursements/batches" -H "$EDITOR"
curl -sS "$BASE/editor/reimbursements/batches/1" -H "$EDITOR"
curl -sS "$BASE/editor/reimbursements/summary" -H "$EDITOR"

# --- Withdrawal of the backing decision (via a conflict) stops new approvals
#     and releases only the undelivered cents; delivered instructions can still
#     be settled, unknown amounts stay encumbered, paid cents remain spend. ---
curl -sS "$BASE/editor/reimbursements/allowances/1" -H "$EDITOR"

# --- Author: only their own reimbursement progress ---
curl -sS "$BASE/author/reimbursements" -H "$AUTHOR1"
curl -sS "$BASE/author/reimbursements/allowances/1" -H "$AUTHOR1"

# ===========================================================================
# Audience seat reservations
# Every prepared account may register as audience.  A talk uses the audience
# capacity of the room its confirmed session is in (default zero).
# ===========================================================================

# --- Editor: set a room's audience capacity (re-evaluates every affected
#     talk in the same transaction; the response lists promoted/demoted
#     registration ids) ---
curl -sS -X PUT "$BASE/editor/rooms/hall-a/audience-capacity" \
  -H "$EDITOR" -H 'Content-Type: application/json' -d '{"capacity": 2}'

# --- Audience: confirmed talks with seat availability and my own status
#     (counts only — never other people's registrations) ---
curl -sS "$BASE/audience/talks" -H "$REV1"

# --- Audience: register for a talk.  Seats confirm in registration order;
#     overlapping talks may be waitlisted, but one person never holds two
#     seats whose slots overlap.  Re-registering while active is a no-op. ---
curl -sS -X POST "$BASE/audience/talks/1/register" -H "$REV1"
curl -sS -X POST "$BASE/audience/talks/1/register" -H "$REV2"

# --- Audience: my own seats / waitlist positions / history ---
curl -sS "$BASE/audience/registrations" -H "$REV1"

# --- Audience: cancel my registration (freed seats promote the earliest
#     eligible waitlisted applicant, here and in other affected talks) ---
curl -sS -X POST "$BASE/audience/talks/1/cancel" -H "$REV1"

# --- Editor: the talk's current admission list (seated + waitlist, in
#     order).  Confirmed reschedules carry it over automatically; a started
#     talk's list is frozen and stays readable even after a withdrawal. ---
curl -sS "$BASE/editor/talks/1/admissions" -H "$EDITOR"
curl -sS "$BASE/editor/sessions/1/admissions" -H "$EDITOR"
