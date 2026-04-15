from typing import List, Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud, schemas
from app.api.login import require_auth

router = APIRouter()


@router.get("/logs", response_model=List[schemas.CrawlLog])
def get_crawl_logs(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    source: Optional[str] = None,
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth)
):
    logs = crud.get_crawl_logs(db, skip=skip, limit=limit, source=source)
    return logs
