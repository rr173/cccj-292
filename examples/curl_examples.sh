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
