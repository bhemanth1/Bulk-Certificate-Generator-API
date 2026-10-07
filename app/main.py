from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Generator

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from sqlalchemy import DateTime, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

# Storage configuration
BASE_DIR = Path(os.getenv("APP_DATA_DIR", Path.cwd() / "data"))
CERTIFICATE_DIR = BASE_DIR / "certificates"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'certificates.db'}")
BASE_DIR.mkdir(parents=True, exist_ok=True)


# Database setup
class Base(DeclarativeBase):
    pass


# Job model
class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    course_name: Mapped[str] = mapped_column(String(160))
    issued_at: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(25), default="queued")
    total_recipients: Mapped[int] = mapped_column(Integer)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    certificates: Mapped[list[Certificate]] = relationship(back_populates="job", cascade="all, delete-orphan")


# Certificate model
class Certificate(Base):
    __tablename__ = "certificates"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id"))
    recipient_name: Mapped[str] = mapped_column(String(160))
    recipient_email: Mapped[str] = mapped_column(String(254))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    job: Mapped[GenerationJob] = relationship(back_populates="certificates")


# Request schemas
class RecipientInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    email: EmailStr


class JobCreate(BaseModel):
    course_name: str = Field(min_length=2, max_length=160)
    issued_at: date
    recipients: list[RecipientInput] = Field(min_length=1, max_length=500)


# Response schemas
class CertificateSummary(BaseModel):
    id: str
    recipient_name: str
    recipient_email: str
    status: str
    error: str | None
    download_url: str | None


class JobResponse(BaseModel):
    id: str
    course_name: str
    issued_at: str
    status: str
    total_recipients: int
    completed_count: int
    failed_count: int
    progress_percent: int
    certificates: list[CertificateSummary]


# Database engine and session helper
def make_engine(url: str = DATABASE_URL):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# Dependency for database sessions
def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


# Download URL generator
def certificate_url(certificate: Certificate) -> str | None:
    return f"/certificates/{certificate.id}/download" if certificate.status == "completed" else None


# Formats job data for API response
def job_response(job: GenerationJob) -> JobResponse:
    done = job.completed_count + job.failed_count
    percent = int(done * 100 / job.total_recipients) if job.total_recipients else 0
    return JobResponse(
        id=job.id,
        course_name=job.course_name,
        issued_at=job.issued_at,
        status=job.status,
        total_recipients=job.total_recipients,
        completed_count=job.completed_count,
        failed_count=job.failed_count,
        progress_percent=percent,
        certificates=[
            CertificateSummary(
                id=item.id,
                recipient_name=item.recipient_name,
                recipient_email=item.recipient_email,
                status=item.status,
                error=item.error,
                download_url=certificate_url(item),
            )
            for item in job.certificates
        ],
    )


# Generates Aereo-themed PDF certificate
def generate_pdf(certificate: Certificate, job: GenerationJob) -> Path:
    CERTIFICATE_DIR.mkdir(parents=True, exist_ok=True)
    output = CERTIFICATE_DIR / f"{certificate.id}.pdf"
    width, height = landscape(A4)
    pdf = canvas.Canvas(str(output), pagesize=(width, height))

    # Aereo color palette: Navy, Sky Blue, Soft Mist
    navy = colors.HexColor("#0D2745")
    sky_blue = colors.HexColor("#18A6D8")
    mist = colors.HexColor("#F2F9FC")
    dark_gray = colors.HexColor("#394B59")

    # Background
    pdf.setFillColor(mist)
    pdf.rect(0, 0, width, height, fill=1, stroke=0)

    # Outer border
    pdf.setStrokeColor(navy)
    pdf.setLineWidth(4)
    pdf.rect(25, 25, width - 50, height - 50, fill=0, stroke=1)

    # Inner accent border
    pdf.setStrokeColor(sky_blue)
    pdf.setLineWidth(1.5)
    pdf.rect(38, 38, width - 76, height - 76, fill=0, stroke=1)

    # Header branding
    pdf.setFillColor(navy)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawCentredString(width / 2, height - 85, "AEREO | DRONE & AERIAL INTELLIGENCE")

    pdf.setFont("Helvetica-Bold", 32)
    pdf.drawCentredString(width / 2, height - 135, "Certificate of Completion")

    pdf.setFillColor(dark_gray)
    pdf.setFont("Helvetica", 13)
    pdf.drawCentredString(width / 2, height - 185, "This certificate is proudly awarded to")

    # Recipient name with text width safety check
    pdf.setFillColor(navy)
    pdf.setFont("Helvetica-Bold", 28)
    name = certificate.recipient_name
    while stringWidth(name, "Helvetica-Bold", 28) > width - 160:
        name = name[:-1]
    pdf.drawCentredString(width / 2, height - 238, name)

    # Divider line
    pdf.setStrokeColor(sky_blue)
    pdf.setLineWidth(1.5)
    pdf.line(width / 2 - 170, height - 255, width / 2 + 170, height - 255)

    # Course details
    pdf.setFillColor(dark_gray)
    pdf.setFont("Helvetica", 13)
    pdf.drawCentredString(width / 2, height - 295, "for successfully completing the specialized training program")

    pdf.setFillColor(navy)
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawCentredString(width / 2, height - 330, job.course_name)

    # Issue date and certificate ID
    pdf.setFillColor(dark_gray)
    pdf.setFont("Helvetica", 11)
    pdf.drawCentredString(width / 2, 90, f"Issued on: {job.issued_at}   |   Certificate ID: {certificate.id}")

    # Official badge
    pdf.setFillColor(sky_blue)
    pdf.circle(width - 95, 95, 24, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawCentredString(width - 95, 91, "AEREO")

    pdf.save()
    return output


# Background worker to process certificate generation
def process_job(job_id: str) -> None:
    with SessionLocal() as session:
        job = session.get(GenerationJob, job_id)
        if not job:
            return
        job.status = "processing"
        session.commit()

        certificates = session.scalars(select(Certificate).where(Certificate.job_id == job_id)).all()
        for certificate in certificates:
            try:
                output = generate_pdf(certificate, job)
                certificate.status = "completed"
                certificate.file_path = str(output)
                job.completed_count += 1
            except Exception as exc:
                certificate.status = "failed"
                certificate.error = str(exc)[:500]
                job.failed_count += 1
            session.commit()

        job.status = "completed" if job.failed_count == 0 else "completed_with_errors"
        session.commit()


# Lifespan event handler for startup setup
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    yield


# FastAPI application factory
def create_app() -> FastAPI:
    app = FastAPI(
        title="Aereo Bulk Certificate Generator",
        description="Bulk certificate generation API for Aereo drone intelligence training programs.",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Root endpoint for health check and docs reference
    @app.get("/")
    def root():
        return {
            "message": "Welcome to Aereo Bulk Certificate Generator API",
            "status": "online",
            "docs": "/docs",
            "redoc": "/redoc",
        }


    # Create a bulk certificate generation job
    @app.post("/jobs", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
    def create_job(payload: JobCreate, background_tasks: BackgroundTasks, session: Session = Depends(get_session)) -> JobResponse:
        job = GenerationJob(
            id=str(uuid.uuid4()),
            course_name=payload.course_name,
            issued_at=payload.issued_at.isoformat(),
            total_recipients=len(payload.recipients),
            created_at=datetime.now(timezone.utc),
        )
        session.add(job)
        for recipient in payload.recipients:
            session.add(Certificate(
                id=str(uuid.uuid4()),
                job_id=job.id,
                recipient_name=recipient.name,
                recipient_email=str(recipient.email),
            ))
        session.commit()
        session.refresh(job)
        response = job_response(job)
        session.close()
        background_tasks.add_task(process_job, job.id)
        return response

    # List all certificate generation jobs
    @app.get("/jobs", response_model=list[JobResponse])
    def list_jobs(session: Session = Depends(get_session)) -> list[JobResponse]:
        jobs = session.scalars(select(GenerationJob).order_by(GenerationJob.created_at.desc())).all()
        return [job_response(j) for j in jobs]

    # Get job status, progress, and certificates
    @app.get("/jobs/{job_id}", response_model=JobResponse)
    def get_job(job_id: str, session: Session = Depends(get_session)) -> JobResponse:
        job = session.get(GenerationJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job_response(job)

    # Download an individual completed certificate PDF
    @app.get("/certificates/{certificate_id}/download")
    def download_certificate(certificate_id: str, session: Session = Depends(get_session)) -> FileResponse:
        certificate = session.get(Certificate, certificate_id)
        if not certificate:
            raise HTTPException(status_code=404, detail="Certificate not found")
        if certificate.status != "completed" or not certificate.file_path:
            raise HTTPException(status_code=409, detail="Certificate is not ready")
        file_path = Path(certificate.file_path)
        if not file_path.is_file():
            raise HTTPException(status_code=404, detail="Certificate file is unavailable")
        return FileResponse(file_path, media_type="application/pdf", filename=f"certificate-{certificate.id}.pdf")

    return app


app = create_app()
