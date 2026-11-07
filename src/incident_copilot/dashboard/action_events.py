"""Append-only, idempotent responder observations, separate from diagnosis state."""
import json

from fastapi import APIRouter, HTTPException, Request

from incident_copilot.analysis.evidence_tools import redact_evidence
from incident_copilot.schemas.recommendations import ActionEvent, ActionEventWrite

from .store import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS action_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    client_event_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(incident_id, client_event_id)
);
CREATE INDEX IF NOT EXISTS action_events_incident ON action_events(incident_id, sequence);
"""

CHECK_STATUSES = {"supports", "weakens", "inconclusive", "blocked", "skipped"}
MITIGATION_STATUSES = {"in_progress", "applied", "verified", "failed", "rolled_back", "skipped"}
router = APIRouter(tags=["responder outcomes"])


def list_action_events(store, incident_id: str) -> list[ActionEvent]:
    with store.connection() as db:
        rows = db.execute("SELECT payload FROM action_events WHERE incident_id=? ORDER BY sequence", (incident_id,)).fetchall()
    return [ActionEvent.model_validate_json(row[0]) for row in rows]


def public_action_events(service, incident_id: str) -> list[ActionEvent]:
    return [ActionEvent.model_validate(service.configuration.redact(event.model_dump()))
            for event in list_action_events(service.store, incident_id)]


def append_action_event(service, incident_id: str, update: ActionEventWrite) -> ActionEvent:
    payload = service.configuration.redact(redact_evidence(update.model_dump(), service.configuration.effective()))
    with service.store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT payload FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Incident not found")
        record = json.loads(row[0])
        report = record.get("report")
        if not report:
            raise HTTPException(409, "Wait for the saved diagnosis before recording outcomes.")
        actions = {}
        for hypothesis in report.get("hypotheses", []):
            plan = hypothesis.get("action_plan") or {}
            for group, kind in (("checks", "check"), ("mitigations", "mitigation")):
                for action in plan.get(group, []):
                    actions[action["id"]] = (kind, action)
        if update.action_id not in actions:
            raise HTTPException(422, "Action does not belong to this saved report.")
        kind, action = actions[update.action_id]
        allowed = CHECK_STATUSES if kind == "check" else MITIGATION_STATUSES
        if update.status not in allowed:
            raise HTTPException(422, "Status is not valid for this action type.")
        if update.status == "verified" and not payload["observations"].strip():
            raise HTTPException(422, "Record recovery verification observations before marking verified.")
        if (kind == "mitigation" and action.get("missing_prerequisites")
                and update.status in {"in_progress", "applied", "verified"} and not payload["observations"].strip()):
            raise HTTPException(422, "Record how missing prerequisites were satisfied before proceeding.")
        previous = db.execute("SELECT payload FROM action_events WHERE incident_id=? AND client_event_id=?",
                              (incident_id, update.client_event_id)).fetchone()
        if previous:
            event = ActionEvent.model_validate_json(previous[0])
            if any(getattr(event, key) != value for key, value in payload.items()):
                raise HTTPException(409, "This event identifier was already used for different observations.")
            return event
        event = ActionEvent(**payload, incident_id=incident_id, action_kind=kind, recorded_at=utcnow())
        db.execute("INSERT INTO action_events(incident_id,client_event_id,payload) VALUES (?,?,?)",
                   (incident_id, update.client_event_id, event.model_dump_json()))
        return event


@router.get("/incidents/{incident_id}/action-events", response_model=list[ActionEvent])
def get_events(incident_id: str, request: Request):
    service = request.app.state.dashboard
    if service.store.get("incidents", incident_id) is None:
        raise HTTPException(404, "Incident not found")
    return public_action_events(service, incident_id)


@router.post("/incidents/{incident_id}/action-events", response_model=ActionEvent, status_code=201)
def post_event(incident_id: str, update: ActionEventWrite, request: Request):
    return append_action_event(request.app.state.dashboard, incident_id, update)
