"""Job-related API routes."""

from typing import Optional
from fastapi import Depends, Form, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.connection import get_db
from app.models import Job, JobApplyRecord, Message
from services.auto_apply_service import AutoApplyService


def register_job_routes(app):
    """Register job-related routes."""

    @app.get("/api/jobs")
    async def api_jobs(
        search: Optional[str] = None,
        is_applied: Optional[bool] = None,
        source_type: Optional[str] = None,  # 'telegram' or 'website'
        limit: int = 10,
        offset: int = 0,
        db: AsyncSession = Depends(get_db),
    ):
        """Get jobs as JSON with search and filters."""
        # Build base query - show all jobs regardless of channel/website source status
        query = select(Job).filter(Job.is_hidden == False)

        # Filter by source type if specified
        if source_type:
            query = query.filter(Job.source_type == source_type)

        if is_applied is not None:
            query = query.filter(Job.is_applied == is_applied)

        # Apply search filter — searches all text fields
        if search:
            from sqlalchemy import cast, String
            search_pattern = f"%{search}%"
            query = query.where(
                (Job.title.ilike(search_pattern)) |
                (Job.company.ilike(search_pattern)) |
                (Job.location.ilike(search_pattern)) |
                (Job.summary.ilike(search_pattern)) |
                (Job.company_link.ilike(search_pattern)) |
                (Job.role_type.ilike(search_pattern)) |
                (cast(Job.skills, String).ilike(search_pattern)) |
                (Job.contact.ilike(search_pattern)) |
                (Job.channel_name.ilike(search_pattern)) |
                (Job.notes.ilike(search_pattern)) |
                (Job.translated_text.ilike(search_pattern))
            )

        # Get total count
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # Get jobs with pagination, eagerly load message, channel, and website_source
        # Also eagerly load job/developer on message to prevent circular lazy-loading
        from sqlalchemy import func as sql_func
        jobs_query = query.options(
            selectinload(Job.message).selectinload(Message.job),
            selectinload(Job.message).selectinload(Message.developer),
            selectinload(Job.channel),
            selectinload(Job.website_source)
        ).outerjoin(Job.message).order_by(sql_func.coalesce(Message.date, Job.analyzed_at).desc()).offset(offset).limit(limit)
        jobs_result = await db.execute(jobs_query)
        jobs = jobs_result.scalars().all()

        return {
            "jobs": [job.to_dict() for job in jobs],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/jobs/{job_id}")
    async def api_job_detail(job_id: int, db: AsyncSession = Depends(get_db)):
        """Get job detail as JSON."""
        result = await db.execute(
            select(Job).options(
                selectinload(Job.channel),
                selectinload(Job.message).selectinload(Message.job),
                selectinload(Job.message).selectinload(Message.developer),
                selectinload(Job.website_source)
            ).filter(Job.id == job_id)
        )
        job = result.scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return {"job": job.to_dict()}

    @app.post("/api/jobs/{job_id}/review")
    async def api_review_job(
        job_id: int,
        is_approved: bool = Form(...),
        notes: Optional[str] = Form(None),
        db: AsyncSession = Depends(get_db),
    ):
        """Mark job as reviewed."""
        try:
            result = await db.execute(select(Job).filter(Job.id == job_id))
            job = result.scalar_one_or_none()
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")

            job.is_reviewed = True
            job.is_approved = is_approved
            job.notes = notes
            await db.commit()

            return {"success": True}
        except HTTPException:
            await db.rollback()
            raise
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to review job: {str(e)}")

    @app.post("/api/jobs/{job_id}/toggle-applied")
    async def api_toggle_job_applied(
        job_id: int,
        notes: Optional[str] = Form(None),
        db: AsyncSession = Depends(get_db),
    ):
        """Toggle job applied status with optional notes."""
        try:
            result = await db.execute(select(Job).filter(Job.id == job_id))
            job = result.scalar_one_or_none()
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")

            job.is_applied = not job.is_applied
            if job.is_applied:
                from datetime import datetime
                job.applied_at = datetime.utcnow()
                job.notes = notes
            else:
                job.applied_at = None
                job.notes = None
            await db.commit()

            return {"success": True, "is_applied": job.is_applied}
        except HTTPException:
            await db.rollback()
            raise
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to toggle applied status: {str(e)}")

    @app.delete("/api/jobs/{job_id}")
    async def api_delete_job(job_id: int, db: AsyncSession = Depends(get_db)):
        """Hide a job (soft-delete). Message is kept to prevent duplicate re-fetching."""
        try:
            result = await db.execute(select(Job).filter(Job.id == job_id))
            job = result.scalar_one_or_none()
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")

            job.is_hidden = True
            await db.commit()

            return {"success": True}
        except HTTPException:
            await db.rollback()
            raise
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to hide job: {str(e)}")

    @app.post("/api/jobs/auto-apply-frontend-remote")
    async def api_auto_apply_frontend_remote(
        limit: int = 20,
        dry_run: bool = False,
        db: AsyncSession = Depends(get_db),
    ):
        """Auto-apply loaded jobs via Playwright.

        - Loads not-hidden, not-yet-applied jobs.
        - Attempts application for every loaded job (no skip branch).
        - Picks Chinese/English resume docx automatically from repo root.
        - Supports LinkedIn/Bossjob flows; unsupported jobs are recorded as failed.
        """
        try:
            if limit < 1 or limit > 200:
                raise HTTPException(status_code=400, detail="limit must be between 1 and 200")

            service = AutoApplyService(db)
            result = await service.run_frontend_remote_auto_apply(limit=limit, dry_run=dry_run)
            return {"success": True, **result}
        except HTTPException:
            raise
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to auto-apply jobs: {str(e)}")

    @app.get("/api/job-apply-records")
    async def api_job_apply_records(
        status: Optional[str] = None,
        site: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        db: AsyncSession = Depends(get_db),
    ):
        """List auto-apply records with optional status/site filters."""
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 500")

        query = select(JobApplyRecord).options(selectinload(JobApplyRecord.job))
        if status:
            query = query.filter(JobApplyRecord.status == status)
        if site:
            query = query.filter(JobApplyRecord.site == site)

        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        result = await db.execute(
            query.order_by(JobApplyRecord.created_at.desc()).offset(offset).limit(limit)
        )
        records = result.scalars().all()

        return {
            "records": [r.to_dict() for r in records],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @app.get("/api/jobs/{job_id}/apply-records")
    async def api_job_apply_records_by_job(
        job_id: int,
        limit: int = 50,
        offset: int = 0,
        db: AsyncSession = Depends(get_db),
    ):
        """List auto-apply records for a specific job."""
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 500")

        query = (
            select(JobApplyRecord)
            .options(selectinload(JobApplyRecord.job))
            .filter(JobApplyRecord.job_id == job_id)
        )

        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        result = await db.execute(
            query.order_by(JobApplyRecord.created_at.desc()).offset(offset).limit(limit)
        )
        records = result.scalars().all()

        return {
            "records": [r.to_dict() for r in records],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
