from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import crud, schemas

router = APIRouter()


@router.get("/filter/rules", response_model=schemas.FilterRule)
def get_filter_rules(db: Session = Depends(get_db)):
    rule = crud.get_latest_filter_rule(db)
    if rule is None:
        rule = crud.create_filter_rule(db, schemas.FilterRuleCreate())
    return rule


@router.put("/filter/rules", response_model=schemas.FilterRule)
def update_filter_rules(
    rule_update: schemas.FilterRuleUpdate,
    db: Session = Depends(get_db)
):
    latest_rule = crud.get_latest_filter_rule(db)
    if latest_rule:
        rule = crud.update_filter_rule(db, latest_rule.id, rule_update)
    else:
        rule = crud.create_filter_rule(db, schemas.FilterRuleCreate(**rule_update.model_dump()))
    return rule
