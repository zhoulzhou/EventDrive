from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud, schemas
from app.api.login import require_auth

router = APIRouter()


@router.get("/news", response_model=List[schemas.News])
def get_news_list(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    source: Optional[str] = None,
    include_keywords: Optional[str] = None,
    exclude_keywords: Optional[str] = None,
    db: Session = Depends(get_db),
    auth: bool = Depends(require_auth)
):
    include_kw_list = None
    if include_keywords:
        include_kw_list = [kw.strip() for kw in include_keywords.split(",") if kw.strip()]
    
    exclude_kw_list = None
    if exclude_keywords:
        exclude_kw_list = [kw.strip() for kw in exclude_keywords.split(",") if kw.strip()]
    
    news_list = crud.get_news_list(
        db, skip=skip, limit=limit, 
        source=source,
        include_keywords=include_kw_list,
        exclude_keywords=exclude_kw_list
    )
    return news_list


@router.get("/news/{news_id}", response_model=schemas.News)
def get_news_detail(news_id: int, db: Session = Depends(get_db), auth: bool = Depends(require_auth)):
    news = crud.get_news(db, news_id=news_id)
    if news is None:
        raise HTTPException(status_code=404, detail="News not found")
    return news
