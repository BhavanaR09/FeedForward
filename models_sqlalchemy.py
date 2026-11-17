from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Enum
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class SQLAlchemyUser(Base):
    """
    SQLAlchemy model representing the users table in the FeedForward system.
    Provides complete type definition and mapping to SQLite.
    """
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=True)
    full_name = Column(String(50), nullable=False)
    email = Column(String(100), unique=True, nullable=False)
    phone = Column(String(10), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False)  # 'donor', 'ngo', 'volunteer', or 'admin'
    status = Column(String(20), nullable=False, default='active')  # 'active', 'pending', etc.
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<SQLAlchemyUser(id={self.id}, full_name='{self.full_name}', email='{self.email}', role='{self.role}')>"
