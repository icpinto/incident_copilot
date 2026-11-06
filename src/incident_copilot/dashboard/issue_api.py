from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .issue_models import (
    ActivityPage,
    Dimension,
    ErrorIntelligence,
    IssueDetail,
    IssueFilters,
    IssueMerge,
    IssuePage,
    IssueSort,
    IssueSplit,
    IssueStatus,
    IssueUpdate,
    OccurrencePage,
)
from .issues import IssueService

router = APIRouter(tags=["error intelligence"])


def get_issues(request: Request) -> IssueService:
    return request.app.state.dashboard.issues


def filters(
    q: str = "",
    repository_id: str = "",
    service_name: str = "",
    error_class: str = "",
    file_path: str = "",
    function_name: str = "",
    owner: str = "",
    status: IssueStatus | None = None,
    incident_id: str = "",
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> IssueFilters:
    return IssueFilters(**locals())


def execute(call):
    try:
        return call()
    except KeyError:
        raise HTTPException(404, "Issue not found") from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


FilterQuery = Annotated[IssueFilters, Depends(filters)]
Service = Annotated[IssueService, Depends(get_issues)]


@router.get("/error-intelligence", response_model=ErrorIntelligence)
def intelligence(query: FilterQuery, service: Service, dimension: Dimension = "service"):
    return execute(lambda: service.intelligence(query, dimension))


@router.get("/issues", response_model=IssuePage)
def issues(
    query: FilterQuery,
    service: Service,
    sort: IssueSort = "occurrences",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return execute(lambda: service.list(query, sort, page, page_size))


@router.get("/issues/{issue_id}", response_model=IssueDetail)
def issue(issue_id: str, service: Service):
    return execute(lambda: service.detail(issue_id))


@router.patch("/issues/{issue_id}", response_model=IssueDetail)
def update_issue(issue_id: str, update: IssueUpdate, service: Service):
    return execute(lambda: service.update(issue_id, update))


@router.get("/issues/{issue_id}/occurrences", response_model=OccurrencePage)
def occurrences(
    issue_id: str,
    query: FilterQuery,
    service: Service,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return execute(lambda: service.occurrences(issue_id, query, page, page_size))


@router.get("/issues/{issue_id}/activity", response_model=ActivityPage)
def history(
    issue_id: str,
    service: Service,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return execute(lambda: service.history(issue_id, page, page_size))


@router.post("/issues/{issue_id}/merge", response_model=IssueDetail)
def merge(issue_id: str, body: IssueMerge, service: Service):
    return execute(lambda: service.merge(issue_id, body.source_issue_ids))


@router.post("/issues/{issue_id}/split", response_model=IssueDetail, status_code=201)
def split(issue_id: str, body: IssueSplit, service: Service):
    return execute(lambda: service.split(issue_id, body.occurrence_ids, body.title))
