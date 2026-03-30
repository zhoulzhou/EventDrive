from typing import List, Optional
from sqlalchemy.orm import Session
from app.models import FilterRule, News


def parse_keywords(keywords_str: Optional[str]) -> List[str]:
    if not keywords_str:
        return []
    keywords = [k.strip() for k in keywords_str.split(',')]
    return [k for k in keywords if k]


def should_include_news(news: News, include_keywords: List[str]) -> bool:
    if not include_keywords:
        return True
    
    text = (news.title or '') + ' ' + (news.content or '')
    text = text.lower()
    
    for keyword in include_keywords:
        if keyword.lower() in text:
            return True
    return False


def should_exclude_news(news: News, exclude_keywords: List[str]) -> bool:
    if not exclude_keywords:
        return False
    
    text = (news.title or '') + ' ' + (news.content or '')
    text = text.lower()
    
    for keyword in exclude_keywords:
        if keyword.lower() in text:
            return True
    return False


def filter_news(news_list: List[News], include_keywords: List[str], exclude_keywords: List[str]) -> List[News]:
    filtered = []
    for news in news_list:
        if should_include_news(news, include_keywords) and not should_exclude_news(news, exclude_keywords):
            filtered.append(news)
    return filtered


def get_filter_rules(db: Session) -> FilterRule:
    rule = db.query(FilterRule).order_by(FilterRule.id.desc()).first()
    if not rule:
        rule = FilterRule(include_keywords='', exclude_keywords='')
        db.add(rule)
        db.commit()
        db.refresh(rule)
    return rule


def update_filter_rules(db: Session, include_keywords: str, exclude_keywords: str) -> FilterRule:
    rule = get_filter_rules(db)
    rule.include_keywords = include_keywords
    rule.exclude_keywords = exclude_keywords
    db.commit()
    db.refresh(rule)
    return rule
