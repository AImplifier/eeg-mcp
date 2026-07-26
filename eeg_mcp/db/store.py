"""Writing recording metadata into a store neuro-mcp can read.

**This store belongs to eeg-mcp alone.** It lives at ``EEG_MCP_DATABASE_URL``
(default: SQLite under ``EEG_MCP_HOME``). The bare ``DATABASE_URL`` variable
that neuro-mcp and other tools use is deliberately ignored, so eeg-mcp can never
inherit — and start writing into — another application's store.

The schema matches neuro-mcp's so that neuro-mcp can **read** a database written
here without an import or conversion step. That is interoperability, not a
shared store: nothing but eeg-mcp writes to this database.

eeg-mcp only ever *adds* rows: a subject if it is new, a dataset if it is new,
one recording, and one annotation per event. It never amends or voids — those
are versioned clinical operations that belong to an analysis tool, not to an
acquisition server.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_config
from .models import Annotation, AuditLog, Base, Dataset, Recording, Subject

_engine = None
_Session: sessionmaker | None = None

# Recorded on every row eeg-mcp writes, so a reader can tell which application
# produced it without inspecting provenance.
AUTHOR = "eeg-mcp"


def database_url() -> str:
    return get_config().database_url


def _ensure_engine():
    global _engine, _Session
    url = database_url()
    if _engine is not None and str(_engine.url) == url:
        return _engine
    if url.startswith("sqlite:///"):
        # Create the parent directory; SQLite will not do it and the error it
        # raises ("unable to open database file") does not say why.
        target = Path(url.replace("sqlite:///", "", 1))
        if target.parent and str(target.parent) not in ("", "."):
            target.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(url, future=True)
    _Session = sessionmaker(bind=_engine, future=True)
    return _engine


def init_db() -> str:
    """Create any missing tables. Idempotent, and safe to run against a database
    neuro-mcp already created -- ``create_all`` only adds what is absent."""
    engine = _ensure_engine()
    Base.metadata.create_all(engine)
    return str(engine.url)


@contextmanager
def session_scope() -> Iterator[Session]:
    init_db()
    assert _Session is not None
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Drop the cached engine so a changed DATABASE_URL takes effect (tests)."""
    global _engine, _Session
    _engine = None
    _Session = None


def _get_or_create_subject(db: Session, external_id: str,
                           demographics: dict | None) -> Subject:
    subject = db.scalar(select(Subject).where(Subject.external_id == external_id))
    if subject is None:
        subject = Subject(external_id=external_id, demographics=demographics or {})
        db.add(subject)
        db.flush()
    elif demographics:
        # Merge rather than replace: another application may have written
        # fields we know nothing about.
        merged = dict(subject.demographics or {})
        merged.update(demographics)
        subject.demographics = merged
    return subject


def _get_or_create_dataset(db: Session, name: str,
                           metadata: dict | None) -> Dataset:
    dataset = db.scalar(select(Dataset).where(Dataset.name == name))
    if dataset is None:
        dataset = Dataset(name=name, source=AUTHOR,
                          dataset_metadata=metadata or {})
        db.add(dataset)
        db.flush()
    return dataset


def register_recording(
    source_path: str,
    sfreq: float,
    n_channels: int,
    duration_sec: float,
    events: list[dict[str, Any]] | None = None,
    subject: str | None = None,
    demographics: dict[str, Any] | None = None,
    dataset: str | None = None,
    task: str | None = None,
    run: str | None = None,
    session_label: str | None = None,
    provenance: dict[str, Any] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Insert one recording, its subject/dataset, and its events as annotations.

    ``events`` onsets must already be **relative to the start of the recording**,
    matching the annotations written into the file itself, so the database and
    the file agree.
    """
    with session_scope() as db:
        subject_row = (
            _get_or_create_subject(db, subject, demographics) if subject else None
        )
        dataset_row = (
            _get_or_create_dataset(db, dataset, {"created_by": AUTHOR})
            if dataset else None
        )

        entities = {k: v for k, v in
                    {"sub": subject, "ses": session_label, "task": task, "run": run}
                    .items() if v}
        recording = Recording(
            dataset_id=dataset_row.id if dataset_row else None,
            subject_id=subject_row.id if subject_row else None,
            source_path=str(source_path),
            modality="eeg",
            sfreq=float(sfreq),
            n_channels=int(n_channels),
            duration_sec=float(duration_sec),
            provenance={
                "recorded_by": AUTHOR,
                "bids_entities": entities,
                "task": task,
                "run": run,
                "session": session_label,
                "notes": notes,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                **(provenance or {}),
            },
        )
        db.add(recording)
        db.flush()

        n_annotations = 0
        for event in events or []:
            db.add(Annotation(
                logical_id=uuid.uuid4().hex[:16],
                version=1,
                recording_id=recording.id,
                author=AUTHOR,
                onset=float(event.get("onset", 0.0)),
                duration=float(event.get("duration", 0.0) or 0.0),
                label=str(event.get("label", "event"))[:256],
                channels=[],
                payload=event.get("payload") or {},
                # 'auto' is neuro-mcp's vocabulary for machine-generated
                # annotations, which is what a marker or a dispatched
                # stimulation is.
                source="auto",
            ))
            n_annotations += 1

        db.add(AuditLog(
            actor=AUTHOR, action="create", target_table="recordings",
            target_id=recording.id, before=None,
            after={"source_path": str(source_path), "sfreq": float(sfreq),
                   "n_channels": int(n_channels), "duration_sec": float(duration_sec),
                   "n_annotations": n_annotations},
        ))
        db.flush()

        return {
            "recording_id": recording.id,
            "subject_id": subject_row.id if subject_row else None,
            "dataset_id": dataset_row.id if dataset_row else None,
            "n_annotations": n_annotations,
            "database_url": database_url(),
        }


def list_recordings(limit: int = 50, subject: str | None = None) -> list[dict[str, Any]]:
    """Recordings in the store, newest first -- including any neuro-mcp wrote."""
    with session_scope() as db:
        query = select(Recording).order_by(Recording.id.desc())
        if subject:
            subject_row = db.scalar(
                select(Subject).where(Subject.external_id == subject)
            )
            if subject_row is None:
                return []
            query = query.where(Recording.subject_id == subject_row.id)
        rows = db.scalars(query.limit(max(1, limit))).all()

        out = []
        for row in rows:
            subject_row = (
                db.get(Subject, row.subject_id) if row.subject_id else None
            )
            dataset_row = (
                db.get(Dataset, row.dataset_id) if row.dataset_id else None
            )
            n_annotations = len(row.annotations)
            provenance = row.provenance or {}
            out.append({
                "recording_id": row.id,
                "source_path": row.source_path,
                "subject": subject_row.external_id if subject_row else None,
                "dataset": dataset_row.name if dataset_row else None,
                "task": provenance.get("task"),
                "run": provenance.get("run"),
                "session": provenance.get("session"),
                "sfreq": row.sfreq,
                "n_channels": row.n_channels,
                "duration_sec": row.duration_sec,
                "n_annotations": n_annotations,
                "recorded_by": provenance.get("recorded_by"),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            })
        return out
