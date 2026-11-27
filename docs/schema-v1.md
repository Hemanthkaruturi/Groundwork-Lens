# GroundWork Export Snapshot Schema (`1.0`)

`groundwork.py export --json` outputs a single JSON document that serves as the frozen contract between the GroundWork engine (`0.1.30`) and GroundWork Lens (`1.0`).

## Top-level fields

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | string | Semantic version of this schema (`"1.0"`). |
| `engine_version` | string | GroundWork engine version (`"0.1.30"`). |
| `kind` | `"repo" \| "workspace"` | Whether the snapshot represents a single repo or a workspace parent + sub-repos. |
| `name` | string | Project or workspace display name. |
| `commit` | string | Short commit SHA of the exported ref. |
| `generated_at` | string (ISO 8601) | UTC timestamp when the export ran. |
| `repos` | string[] | Sub-repo names (for workspaces) or `[name]` (for single repo). |
| `people` | Person[] | `{name, role, contact, aliases[]}` merged from `PROJECT.md` and `.groundwork` records. |
| `items` | Item[] | Unified list of RFCs, features, bugs, and requests. |
| `approvals_waiting` | ApprovalWaiting[] | Documents awaiting approval `{document, state, needs[]}`. |
| `events` | Event[] | Merged timeline `{at, type, item, who, source, via?, detail?}`. |
| `health` | object | `{check_errors, check_warnings, stale_documents}`. |
| `groundwork` | GroundWorkBlock | Doctor stage block `{stage, stage_number, found, missing, next_steps, unreadable}`. |
| `sub_repo_groundwork` | object | Map of `repo_name -> GroundWorkBlock` for workspaces. |
| `sub_repo_errors` | object | Map of `repo_name -> error message` when a sub-repo failed to export. |

## Item rules

- `kind`: `"rfc" | "feature" | "bug" | "request"`
- `request_kind` (when `kind == "request"`): `"feature" | "bug"`
- `stage`:
  - Features: `rfc`, `spec`, `plan`, `tasks`, `evals`, `build`, `handover`, `done`
  - Bugs: `open`, `diagnosed`, `fixed`, `closed`
  - Requests: `new`, `questions`, `interviewing`, `converted`, `declined`
- `approval` (on `documents[]`): `missing`, `draft`, `in-review`, `approved`, `stale`
- `documents[]`: `{type, path, title, approval, approvers[], signers[], signed[{who, when, via?}], still_needed[], can_approve, preflight_reason, blocking_rfc?, body_hash, changes_since_approval[]}`
- `waiting_on[]`: `{person, task, what, inferred}`
- `commits[]`: up to 20 linked commits `{hash, at, author, subject}`
- `events[].source`: `"ledger" | "approval" | "change-log" | "note" | "git"`
