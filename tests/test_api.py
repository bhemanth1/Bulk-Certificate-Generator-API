from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException


# Test fixture for isolated database and file storage
@pytest.fixture()
def app_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    import app.main as main

    main.BASE_DIR = tmp_path / "data"
    main.CERTIFICATE_DIR = main.BASE_DIR / "certificates"
    main.engine = main.make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    main.SessionLocal.configure(bind=main.engine)
    main.Base.metadata.create_all(main.engine)
    yield main
    main.engine.dispose()


# Helper sample payload
def sample_payload():
    return {
        "course_name": "Drone Data Fundamentals",
        "issued_at": "2026-10-07",
        "recipients": [
            {"name": "Asha Rao", "email": "asha@example.com"},
            {"name": "Ravi Shah", "email": "ravi@example.com"},
        ],
    }


# Route lookup helper
def get_routes(main):
    routes = {}
    for route in main.app.routes:
        if hasattr(route, "endpoint"):
            methods = getattr(route, "methods", {"GET"})
            for method in methods:
                routes[(route.path, method.upper())] = route.endpoint
                routes[route.path] = route.endpoint
    return routes


# Route endpoint helper by path and method
def get_endpoint(main, path: str, method: str = "GET"):
    for route in main.app.routes:
        if route.path == path and hasattr(route, "endpoint"):
            methods = getattr(route, "methods", {"GET"})
            if method.upper() in methods:
                return route.endpoint
    raise KeyError(f"Route {method} {path} not found")


# Helper to invoke create_job route
def create_job(main, data):
    endpoint = get_endpoint(main, "/jobs", "POST")
    session = main.SessionLocal()
    response = endpoint(main.JobCreate(**data), BackgroundTasks(), session)
    session.close()
    return response



# 1. Test creating a generation job
def test_create_generation_job(app_module):
    job = create_job(app_module, sample_payload())
    assert job.id is not None
    assert job.status == "queued"
    assert job.total_recipients == 2
    assert job.completed_count == 0
    assert len(job.certificates) == 2


# 2. Test input validation
def test_input_validation_invalid_email(app_module):
    data = sample_payload()
    data["recipients"][0]["email"] = "invalid-email-address"
    with pytest.raises(Exception):
        app_module.JobCreate(**data)


def test_input_validation_empty_recipients(app_module):
    data = {"course_name": "Drone Mapping", "issued_at": "2026-10-07", "recipients": []}
    with pytest.raises(Exception):
        app_module.JobCreate(**data)


# 3 & 4. Test certificate generation and progress tracking
def test_certificate_generation_and_progress(app_module):
    job = create_job(app_module, sample_payload())
    app_module.process_job(job.id)

    session = app_module.SessionLocal()
    result = get_routes(app_module)["/jobs/{job_id}"](job.id, session)

    assert result.status == "completed"
    assert result.progress_percent == 100
    assert result.completed_count == 2
    assert result.failed_count == 0
    for cert in result.certificates:
        assert cert.status == "completed"
        assert cert.download_url == f"/certificates/{cert.id}/download"
    session.close()


# 5. Test handling individual certificate failure without stopping the job
def test_individual_certificate_failure_isolation(app_module, monkeypatch: pytest.MonkeyPatch):
    real_generator = app_module.generate_pdf

    def faulty_generator(certificate, job):
        if certificate.recipient_name == "Failing Student":
            raise RuntimeError("PDF rendering error simulated")
        return real_generator(certificate, job)

    monkeypatch.setattr(app_module, "generate_pdf", faulty_generator)

    data = sample_payload()
    data["recipients"].append({"name": "Failing Student", "email": "failing@example.com"})

    job = create_job(app_module, data)
    app_module.process_job(job.id)

    session = app_module.SessionLocal()
    result = get_routes(app_module)["/jobs/{job_id}"](job.id, session)

    assert result.status == "completed_with_errors"
    assert result.completed_count == 2
    assert result.failed_count == 1
    assert result.progress_percent == 100

    failing_cert = next(c for c in result.certificates if c.recipient_name == "Failing Student")
    assert failing_cert.status == "failed"
    assert failing_cert.download_url is None
    assert "PDF rendering error simulated" in failing_cert.error

    successful_cert = next(c for c in result.certificates if c.recipient_name == "Asha Rao")
    assert successful_cert.status == "completed"
    assert successful_cert.download_url is not None
    session.close()


# 6. Test retrieving generated certificate PDF
def test_retrieve_generated_certificate(app_module):
    job = create_job(app_module, sample_payload())
    app_module.process_job(job.id)

    session = app_module.SessionLocal()
    cert_id = job.certificates[0].id
    file_response = get_routes(app_module)["/certificates/{certificate_id}/download"](cert_id, session)

    assert file_response.media_type == "application/pdf"
    assert Path(file_response.path).is_file()
    session.close()


# 7. Test not found error responses
def test_not_found_handlers(app_module):
    session = app_module.SessionLocal()
    routes = get_routes(app_module)

    with pytest.raises(HTTPException) as exc_job:
        routes["/jobs/{job_id}"]("non-existent-id", session)
    assert exc_job.value.status_code == 404

    with pytest.raises(HTTPException) as exc_cert:
        routes["/certificates/{certificate_id}/download"]("non-existent-id", session)
    assert exc_cert.value.status_code == 404

    session.close()


# 8. Test listing all jobs
def test_list_jobs(app_module):
    create_job(app_module, sample_payload())
    session = app_module.SessionLocal()
    endpoint = get_endpoint(app_module, "/jobs", "GET")
    jobs_list = endpoint(session)
    assert len(jobs_list) == 1
    session.close()



