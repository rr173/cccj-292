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
