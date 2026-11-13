"""job_runs: one row per (job, key) marks a unit of work done (idempotency across restarts)."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.risk.models import JobRun


async def run_job_once(session: AsyncSession, job: str, key: str) -> bool:
    """Claim (job, key) in the current transaction. True = not done yet, run it now."""
    claimed = await session.scalar(
        insert(JobRun).values(job=job, key=key).on_conflict_do_nothing().returning(JobRun.job)
    )
    return claimed is not None


async def job_done(session: AsyncSession, job: str, key: str) -> bool:
    return await session.scalar(
        select(JobRun.job).where(JobRun.job == job, JobRun.key == key)
    ) is not None
