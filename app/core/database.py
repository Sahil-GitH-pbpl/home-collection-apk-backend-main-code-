from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.mysql_url,
    pool_size=settings.mysql_pool_size,
    max_overflow=settings.mysql_max_overflow,
    pool_timeout=settings.mysql_pool_timeout,
    pool_recycle=settings.mysql_pool_recycle,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

catalog_engine = create_engine(
    settings.catalog_mysql_url,
    pool_size=settings.mysql_pool_size,
    max_overflow=settings.mysql_max_overflow,
    pool_timeout=settings.mysql_pool_timeout,
    pool_recycle=settings.mysql_pool_recycle,
    pool_pre_ping=True,
    future=True,
)

CatalogSessionLocal = sessionmaker(
    bind=catalog_engine,
    autoflush=False,
    autocommit=False,
    future=True,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_catalog_db():
    db = CatalogSessionLocal()
    try:
        yield db
    finally:
        db.close()
