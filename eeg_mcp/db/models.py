"""SQLAlchemy models mirroring the neuro-mcp data/EHR store.

**These definitions are deliberately a verbatim mirror of neuro-mcp's
``neuro_mcp/db/models.py``, not an import of it.** The reasoning:

* eeg-mcp must not depend on neuro-mcp. A real-time acquisition server should
  not drag in MNE-BIDS, source imaging, and a visualization stack just to write
  a row saying "this recording exists".
* But an analysis tool should be able to **read** a store written here without
  an import or conversion step. Matching the schema is what makes that work.

The two projects stay separate: eeg-mcp writes only its own database, at its own
``EEG_MCP_DATABASE_URL``. Compatibility means neuro-mcp can open that file and
understand every row; it does not mean the two write to one store.

Copying a schema is normally a bad idea because the copies drift. So the copy is
paired with an automated check: ``testing/verify_recording.py`` reflects both
projects' metadata objects, fails if any shared table's columns diverge, and
then reads a database eeg-mcp wrote back through neuro-mcp's own ORM. Run it
after changing either project.

Only the tables eeg-mcp actually touches are exercised here (subjects,
datasets, recordings, annotations, audit_log), but the full set is declared so
``create_all`` produces a schema neuro-mcp recognises even when eeg-mcp created
the file first.

Mirrors neuro-mcp 0.1.3.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# Status constants (FHIR-aligned).
STATUS_ACTIVE = "active"
STATUS_AMENDED = "amended"
STATUS_VOID = "entered-in-error"


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # FHIR Patient-shaped demographics (name, birthDate, gender, ...).
    demographics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    ehr_records: Mapped[list["EHRRecord"]] = relationship(back_populates="subject")
    recordings: Mapped[list["Recording"]] = relationship(back_populates="subject")


class EHRRecord(Base):
    """A versioned EHR entry holding a FHIR resource (Condition, Observation, …)."""

    __tablename__ = "ehr_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    logical_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("ehr_records.id"))
    status: Mapped[str] = mapped_column(String(32), default=STATUS_ACTIVE, index=True)

    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))  # FHIR resourceType
    fhir: Mapped[dict] = mapped_column(JSON, default=dict)  # the FHIR resource body

    author: Mapped[str] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(Text)  # reason for amendment/void
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    subject: Mapped[Subject] = relationship(back_populates="ehr_records")


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    source: Mapped[str | None] = mapped_column(String(128))  # e.g. in-house, OpenNeuro
    bids_root: Mapped[str | None] = mapped_column(Text)
    dataset_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    recordings: Mapped[list["Recording"]] = relationship(back_populates="dataset")


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey("datasets.id"), index=True)
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("subjects.id"), index=True)
    bids_path: Mapped[str | None] = mapped_column(Text)
    source_path: Mapped[str | None] = mapped_column(Text)
    modality: Mapped[str] = mapped_column(String(32), default="eeg")
    sfreq: Mapped[float | None] = mapped_column()
    n_channels: Mapped[int | None] = mapped_column(Integer)
    duration_sec: Mapped[float | None] = mapped_column()
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    dataset: Mapped[Dataset | None] = relationship(back_populates="recordings")
    subject: Mapped[Subject | None] = relationship(back_populates="recordings")
    annotations: Mapped[list["Annotation"]] = relationship(back_populates="recording")


class Annotation(Base):
    """A versioned clinician/algorithm annotation on a recording."""

    __tablename__ = "annotations"

    id: Mapped[int] = mapped_column(primary_key=True)
    logical_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("annotations.id"))
    status: Mapped[str] = mapped_column(String(32), default=STATUS_ACTIVE, index=True)

    recording_id: Mapped[int] = mapped_column(ForeignKey("recordings.id"), index=True)
    author: Mapped[str] = mapped_column(String(128))
    onset: Mapped[float] = mapped_column()  # seconds
    duration: Mapped[float] = mapped_column(default=0.0)
    label: Mapped[str] = mapped_column(String(256))
    channels: Mapped[list] = mapped_column(JSON, default=list)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(32), default="mne")  # neuroii|mne|auto
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    recording: Mapped[Recording] = relationship(back_populates="annotations")


class Derivative(Base):
    __tablename__ = "derivatives"

    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[int] = mapped_column(ForeignKey("recordings.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # psd|erp|ica|esi|tensors
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    uri: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(32))  # create|amend|void
    target_table: Mapped[str] = mapped_column(String(64), index=True)
    target_id: Mapped[int] = mapped_column(Integer, index=True)
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
