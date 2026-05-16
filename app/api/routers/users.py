from fastapi import APIRouter, Depends, Query
from sqlalchemy import cast, func, String
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.models.user import User

router = APIRouter(prefix="/api/v1/users", tags=["Users"])


@router.get("/riders")
def suggest_riders(
    q: str = Query(..., min_length=2, max_length=100),
    limit: int = Query(default=8, ge=1, le=8),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    term = str(q or "").strip()
    if len(term) < 2:
        return {"items": []}

    like_term = f"%{term}%"
    rows = (
        db.query(User.id, User.name, User.designation)
        .filter(func.lower(func.coalesce(User.designation, "")) == "field")
        .filter(func.lower(func.coalesce(cast(User.status, String), "")) == "active")
        .filter(User.name.ilike(like_term))
        .order_by(User.name.asc())
        .limit(limit)
        .all()
    )

    return {
        "items": [
            {
                "id": int(r.id),
                "name": str(r.name or ""),
                "designation": str(r.designation or ""),
            }
            for r in rows
        ]
    }
