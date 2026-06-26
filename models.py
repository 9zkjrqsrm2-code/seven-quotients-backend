"""Database models for Seven Quotients Test"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Text, ForeignKey, JSON, create_engine
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def gen_id():
    return uuid.uuid4().hex[:12]


class User(Base):
    __tablename__ = "users"

    id = Column(String(12), primary_key=True, default=gen_id)
    client_ip = Column(String(45), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    paid = Column(Boolean, default=False)
    stripe_session_id = Column(String(255), nullable=True)

    sessions = relationship("TestSession", back_populates="user", cascade="all, delete-orphan")


class Question(Base):
    __tablename__ = "questions"

    id = Column(String(12), primary_key=True, default=gen_id)
    category = Column(String(10), nullable=False, index=True)  # mq, iq, eq, aq, fq, sq, hq
    question_text = Column(Text, nullable=False)
    options = Column(JSON, nullable=False)  # ["选项A", "选项B", ...]
    scores = Column(JSON, nullable=False)   # [5, 4, 3, 2, 1]
    sort_order = Column(Integer, default=0)
    is_paid = Column(Boolean, default=False)  # True = 付费版题目
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class TestSession(Base):
    __tablename__ = "test_sessions"

    id = Column(String(12), primary_key=True, default=gen_id)
    user_id = Column(String(12), ForeignKey("users.id"), nullable=False)
    is_paid_test = Column(Boolean, default=False)  # True = 付费完整版
    completed = Column(Boolean, default=False)
    results = Column(JSON, nullable=True)  # {"mq": 4.5, "iq": 3.0, ...}
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="sessions")
    answers = relationship("Answer", back_populates="session", cascade="all, delete-orphan")


class Answer(Base):
    __tablename__ = "answers"

    id = Column(String(12), primary_key=True, default=gen_id)
    session_id = Column(String(12), ForeignKey("test_sessions.id"), nullable=False)
    question_id = Column(String(12), ForeignKey("questions.id"), nullable=False)
    score = Column(Integer, nullable=False)  # 1-5
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    session = relationship("TestSession", back_populates="answers")


class Order(Base):
    __tablename__ = "orders"

    id = Column(String(12), primary_key=True, default=gen_id)
    user_id = Column(String(12), ForeignKey("users.id"), nullable=False)
    stripe_session_id = Column(String(255), nullable=True)
    amount = Column(Integer, nullable=False)  # 美分
    currency = Column(String(3), default="usd")
    payment_method = Column(String(20), nullable=True)  # wechat_pay, alipay, card
    status = Column(String(20), default="pending")  # pending, completed, failed
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)


class AdConfig(Base):
    __tablename__ = "ad_configs"

    id = Column(String(12), primary_key=True, default=gen_id)
    placement = Column(String(50), unique=True, nullable=False)  # result_top, result_bottom, sidebar
    ad_code = Column(Text, nullable=True)  # HTML/JS ad code
    enabled = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def init_db(database_url: str):
    engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(engine)
    return engine
