"""업로드 파일과 병합·내보내기 작업의 영속 메타데이터."""
from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ProcessingRecord(Base):
    __tablename__ = "processing_records"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("cases.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
