"""Playwright-based auto-apply service for remote frontend jobs."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, JobApplyRecord, WebsiteSource
from web_crawler.config import HEADLESS, USER_AGENT

logger = logging.getLogger(__name__)


_SAME_SITE_MAP = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": "Lax",
    "": "Lax",
}

_FRONTEND_KEYWORDS = [
    "frontend",
    "front-end",
    "web ui",
    "react",
    "vue",
    "next.js",
    "nextjs",
    "typescript",
    "javascript",
    "前端",
    "web前端",
]

_REMOTE_KEYWORDS = [
    "remote",
    "work from home",
    "wfh",
    "distributed",
    "远程",
    "居家",
    "在家办公",
]

_LINKEDIN_PROFILE_URL = "https://www.linkedin.com/in/felix-fang-47131a417/"


@dataclass
class ApplyResult:
    success: bool
    reason: str
    details: dict[str, Any]


class AutoApplyService:
    """Auto-apply frontend remote jobs with Playwright."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo_root = Path(__file__).resolve().parents[2]
        self.resume_zh_candidates = [
            self.repo_root / "xiaolin.fang-zh.docx",
            self.repo_root / "Xiaolin.Fang-zh.docx",
        ]
        self.resume_en_candidates = [
            self.repo_root / "xiaolin.fang-en.docx",
            self.repo_root / "Xiaolin.Fang-en.docx",
        ]
        self.resume_zh_md_candidates = [
            self.repo_root / "xiaolin.fang-en.md",
            self.repo_root / "Resume.ZH-CN.md",
        ]
        self.resume_en_md_candidates = [
            self.repo_root / "Resume.EN.md",
            self.repo_root / "xiaolin.fang-en.EN.md",
        ]
        self.linkedin_profile_usage_log = self.repo_root / "backend" / "session" / "linkedin_profile_usage.jsonl"

    def _sanitize_cookies(self, cookies: list[dict]) -> list[dict]:
        allowed_keys = {
            "name",
            "value",
            "domain",
            "path",
            "expires",
            "httpOnly",
            "secure",
            "sameSite",
        }
        out = []
        for raw in cookies:
            cookie: dict[str, Any] = {}
            for k, v in raw.items():
                if k == "expirationDate":
                    cookie["expires"] = int(v)
                elif k in allowed_keys:
                    cookie[k] = v
            same_site = str(cookie.get("sameSite", "")).strip().lower()
            cookie["sameSite"] = _SAME_SITE_MAP.get(same_site, "Lax")
            out.append(cookie)
        return out

    def _pick_existing(self, candidates: list[Path]) -> Optional[Path]:
        for p in candidates:
            if p.exists() and p.is_file():
                return p
        return None

    def _has_chinese(self, text: str) -> bool:
        return bool(re.search(r"[\u4e00-\u9fff]", text or ""))

    def pick_resume_for_job(self, job: Job) -> Optional[Path]:
        text = " ".join(
            [
                job.title or "",
                job.summary or "",
                job.location or "",
                job.source_published_text or "",
            ]
        )
        if self._has_chinese(text):
            return self._pick_existing(self.resume_zh_candidates) or self._pick_existing(self.resume_en_candidates)
        return self._pick_existing(self.resume_en_candidates) or self._pick_existing(self.resume_zh_candidates)

    def _pick_resume_language(self, resume_path: Path) -> str:
        name = resume_path.name.lower()
        if "-zh" in name:
            return "zh"
        if "-en" in name:
            return "en"
        return "unknown"

    def _extract_contact_value(self, content: str, labels: list[str]) -> Optional[str]:
        for label in labels:
            pattern = rf"(?:^|\\n)\\s*[-*]?\\s*{re.escape(label)}\\s*[:：]\\s*(.+)$"
            match = re.search(pattern, content, flags=re.IGNORECASE | re.MULTILINE)
            if match:
                value = match.group(1).strip()
                if value:
                    return value
        return None

    def _extract_section_bullets(self, content: str, headers: list[str], max_items: int = 2) -> str:
        for header in headers:
            pattern = rf"##\\s*{re.escape(header)}\\s*\\n([\\s\\S]*?)(?:\\n##\\s+|\\Z)"
            match = re.search(pattern, content, flags=re.IGNORECASE)
            if not match:
                continue
            block = match.group(1)
            bullets = []
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("- "):
                    bullets.append(line[2:].strip())
                if len(bullets) >= max_items:
                    break
            if bullets:
                return " ".join(bullets)
        return ""

    def _extract_name(self, content: str) -> str:
        match = re.search(r"^#\\s+(.+)$", content, flags=re.MULTILINE)
        if match:
            return match.group(1).strip()
        return ""

    def _load_resume_profile(self, language: str) -> dict[str, str]:
        candidates = self.resume_zh_md_candidates if language == "zh" else self.resume_en_md_candidates
        content = ""
        source = ""
        for p in candidates:
            if p.exists() and p.is_file():
                try:
                    content = p.read_text(encoding="utf-8")
                    source = str(p)
                    break
                except Exception:
                    continue

        if not content:
            fallback = self.resume_en_md_candidates if language == "zh" else self.resume_zh_md_candidates
            for p in fallback:
                if p.exists() and p.is_file():
                    try:
                        content = p.read_text(encoding="utf-8")
                        source = str(p)
                        break
                    except Exception:
                        continue

        name = self._extract_name(content) or ("方小林" if language == "zh" else "Fang Xiaolin")
        email = self._extract_contact_value(content, ["Email", "邮箱", "E-mail"]) or "18111250878@163.com"
        phone_line = self._extract_contact_value(content, ["Phone / WeChat", "手机 / 微信", "Phone", "手机"])
        phone_match = re.search(r"\\+?\\d[\\d\\s-]{7,}\\d", phone_line or "")
        phone = phone_match.group(0).strip() if phone_match else "+86 18111250878"
        telegram = self._extract_contact_value(content, ["Telegram"]) or "@Felix_YL"
        summary = self._extract_section_bullets(content, ["Personal Strengths", "个人优势"], max_items=2)
        if not summary:
            summary = "Experienced frontend/full-stack engineer with remote collaboration background."
        skills = self._extract_section_bullets(content, ["Core Skills", "核心技能"], max_items=2)

        return {
            "name": name,
            "email": email,
            "phone": phone,
            "wechat": phone,
            "telegram": telegram,
            "linkedin": _LINKEDIN_PROFILE_URL,
            "company": "Bybit",
            "years_experience": "14",
            "summary": summary,
            "skills": skills,
            "source": source,
        }

    async def _append_linkedin_profile_usage(self, job_url: str, descriptor: str) -> None:
        try:
            self.linkedin_profile_usage_log.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "timestamp": datetime.utcnow().isoformat(),
                "job_url": job_url,
                "linkedin_profile": _LINKEDIN_PROFILE_URL,
                "field_descriptor": descriptor,
            }
            with self.linkedin_profile_usage_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\\n")
        except Exception as e:
            logger.warning("[AUTO APPLY] Failed writing LinkedIn profile usage log: %s", e)

    def _guess_form_value(self, descriptor: str, profile: dict[str, str]) -> Optional[tuple[str, str]]:
        d = descriptor.lower()

        if "linkedin" in d or "领英" in d:
            return "linkedin", profile["linkedin"]
        if "email" in d or "邮箱" in d or "电子邮件" in d:
            return "email", profile["email"]
        if any(k in d for k in ["phone", "mobile", "tel", "联系电话", "手机", "电话"]):
            return "phone", profile["phone"]
        if "wechat" in d or "微信" in d:
            return "wechat", profile["wechat"]
        if "telegram" in d:
            return "telegram", profile["telegram"]
        if "company" in d and "name" in d:
            return "company", profile["company"]
        if "experience" in d or "经验" in d or "years" in d:
            return "years_experience", profile["years_experience"]
        if any(k in d for k in ["portfolio", "website", "homepage", "个人主页"]):
            return "portfolio", profile["linkedin"]
        if any(k in d for k in ["summary", "about", "cover letter", "self introduction", "自我介绍", "简介", "介绍"]):
            return "summary", profile["summary"]
        if any(k in d for k in ["skill", "stack", "技术栈", "技能"]):
            return "skills", profile["skills"]

        if "name" in d or "姓名" in d:
            if "company" not in d:
                return "name", profile["name"]
        return None

    async def _fill_input_value(self, loc, value: str) -> bool:
        try:
            await loc.click(timeout=1000)
            await loc.fill(value, timeout=1500)
            return True
        except Exception:
            try:
                await loc.click(timeout=1000)
                await loc.press("Control+a")
                await loc.type(value, delay=1)
                return True
            except Exception:
                return False

    async def _autofill_form_from_profile(self, page: Page, profile: dict[str, str], job_url: str) -> dict[str, int]:
        filled_count = 0
        linkedin_fills = 0
        try:
            fields = page.locator("input, textarea")
            count = await fields.count()
        except Exception:
            return {"filled_count": 0, "linkedin_fills": 0}

        seen_descriptors: set[str] = set()
        for i in range(count):
            loc = fields.nth(i)
            try:
                if not await loc.is_visible():
                    continue
            except Exception:
                continue

            tag = (await loc.evaluate("el => el.tagName.toLowerCase()")) if loc else ""
            input_type = (await loc.get_attribute("type") or "").lower()
            if tag == "input" and input_type in {"hidden", "file", "checkbox", "radio", "submit", "button"}:
                continue
            readonly = await loc.get_attribute("readonly")
            disabled = await loc.get_attribute("disabled")
            if readonly is not None or disabled is not None:
                continue

            descriptor_parts = [
                await loc.get_attribute("name") or "",
                await loc.get_attribute("id") or "",
                await loc.get_attribute("placeholder") or "",
                await loc.get_attribute("aria-label") or "",
            ]
            descriptor = " ".join(x.strip() for x in descriptor_parts if x).strip()
            if not descriptor:
                continue
            if descriptor in seen_descriptors:
                continue
            seen_descriptors.add(descriptor)

            guessed = self._guess_form_value(descriptor, profile)
            if not guessed:
                continue

            _, value = guessed
            if not value:
                continue

            if await self._fill_input_value(loc, value):
                filled_count += 1
                if "linkedin" in descriptor.lower() or "领英" in descriptor.lower():
                    linkedin_fills += 1
                    await self._append_linkedin_profile_usage(job_url, descriptor)

        return {"filled_count": filled_count, "linkedin_fills": linkedin_fills}

    def is_frontend_remote_job(self, job: Job) -> bool:
        text = " ".join(
            [
                (job.title or "").lower(),
                (job.summary or "").lower(),
                (job.location or "").lower(),
                (str(job.skills) or "").lower(),
            ]
        )
        has_frontend = any(k in text for k in _FRONTEND_KEYWORDS)
        has_remote = bool(job.is_remote) or any(k in text for k in _REMOTE_KEYWORDS)
        return has_frontend and has_remote

    async def _load_job_cookies(self, job: Job) -> list[dict]:
        if not job.website_source_id:
            return []
        result = await self.db.execute(select(WebsiteSource).filter(WebsiteSource.id == job.website_source_id))
        source = result.scalar_one_or_none()
        if not source or not source.cookies:
            return []
        try:
            parsed = json.loads(source.cookies)
            if not isinstance(parsed, list):
                parsed = [parsed]
            return self._sanitize_cookies(parsed)
        except Exception:
            logger.warning("[AUTO APPLY] Invalid cookies JSON for website source %s", job.website_source_id)
            return []

    async def _click_first_visible(self, page: Page, selectors: list[str], timeout: int = 2000) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=timeout)
                    return True
            except Exception:
                continue
        return False

    async def _upload_resume(self, page: Page, resume_path: Path) -> int:
        uploaded = 0
        try:
            inputs = page.locator("input[type='file']")
            count = await inputs.count()
            for i in range(count):
                try:
                    await inputs.nth(i).set_input_files(str(resume_path))
                    uploaded += 1
                except Exception:
                    continue
        except Exception:
            pass
        return uploaded

    async def _apply_linkedin(self, page: Page, job_url: str, resume_path: Path, profile: dict[str, str]) -> ApplyResult:
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            return ApplyResult(False, f"goto_failed: {e}", {})

        current = (page.url or "").lower()
        if "/login" in current or "session_redirect" in current:
            return ApplyResult(False, "redirected_to_login", {"url": page.url})

        # Already applied quick check.
        if await page.locator("text=/applied|已申请/i").count() > 0:
            return ApplyResult(True, "already_applied", {"url": page.url})

        easy_apply_selectors = [
            "button:has-text('Easy Apply')",
            "button:has-text('快速申请')",
            "button[aria-label*='Easy Apply']",
            "button.jobs-apply-button",
        ]
        clicked = await self._click_first_visible(page, easy_apply_selectors, timeout=5000)
        if not clicked:
            return ApplyResult(False, "easy_apply_button_not_found", {"url": page.url})

        await page.wait_for_timeout(1200)

        submitted = False
        uploaded_count = 0
        autofilled_fields = 0
        linkedin_profile_fills = 0
        for _ in range(12):
            filled_result = await self._autofill_form_from_profile(page, profile, job_url)
            autofilled_fields += filled_result["filled_count"]
            linkedin_profile_fills += filled_result["linkedin_fills"]
            uploaded_count += await self._upload_resume(page, resume_path)

            if await self._click_first_visible(
                page,
                [
                    "button:has-text('Submit application')",
                    "button:has-text('提交申请')",
                    "button[aria-label*='Submit application']",
                ],
                timeout=3000,
            ):
                submitted = True
                break

            progressed = await self._click_first_visible(
                page,
                [
                    "button:has-text('Review')",
                    "button:has-text('Next')",
                    "button:has-text('继续')",
                    "button:has-text('下一步')",
                    "button[aria-label*='Continue to next step']",
                ],
                timeout=3000,
            )
            if not progressed:
                break
            await page.wait_for_timeout(800)

        if not submitted and await page.locator("text=/application submitted|申请已提交/i").count() > 0:
            submitted = True

        if submitted:
            return ApplyResult(
                True,
                "submitted",
                {
                    "uploaded_files": uploaded_count,
                    "autofilled_fields": autofilled_fields,
                    "linkedin_profile_fills": linkedin_profile_fills,
                    "resume_profile_source": profile.get("source", ""),
                },
            )
        return ApplyResult(
            False,
            "submit_not_reached",
            {
                "uploaded_files": uploaded_count,
                "autofilled_fields": autofilled_fields,
                "linkedin_profile_fills": linkedin_profile_fills,
                "resume_profile_source": profile.get("source", ""),
            },
        )

    async def _apply_bossjob(self, page: Page, job_url: str, resume_path: Path, profile: dict[str, str]) -> ApplyResult:
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            return ApplyResult(False, f"goto_failed: {e}", {})

        current = (page.url or "").lower()
        if "login" in current or "sign-in" in current:
            return ApplyResult(False, "redirected_to_login", {"url": page.url})

        # Already applied quick check.
        if await page.locator("text=/applied|已投递|已申请/i").count() > 0:
            return ApplyResult(True, "already_applied", {"url": page.url})

        apply_selectors = [
            "button:has-text('Apply')",
            "button:has-text('Apply Now')",
            "button:has-text('立即申请')",
            "button:has-text('投递')",
            "button[class*='apply']",
            "a:has-text('Apply')",
        ]

        clicked = await self._click_first_visible(page, apply_selectors, timeout=5000)
        if not clicked:
            return ApplyResult(False, "apply_button_not_found", {"url": page.url})

        await page.wait_for_timeout(1000)

        filled_result = await self._autofill_form_from_profile(page, profile, job_url)

        uploaded_count = await self._upload_resume(page, resume_path)

        submitted = await self._click_first_visible(
            page,
            [
                "button:has-text('Submit')",
                "button:has-text('Confirm')",
                "button:has-text('提交')",
                "button:has-text('确认')",
            ],
            timeout=3000,
        )

        if not submitted and await page.locator("text=/applied|投递成功|申请成功/i").count() > 0:
            submitted = True

        if submitted:
            return ApplyResult(
                True,
                "submitted",
                {
                    "uploaded_files": uploaded_count,
                    "autofilled_fields": filled_result["filled_count"],
                    "linkedin_profile_fills": filled_result["linkedin_fills"],
                    "resume_profile_source": profile.get("source", ""),
                },
            )
        return ApplyResult(
            False,
            "submit_not_reached",
            {
                "uploaded_files": uploaded_count,
                "autofilled_fields": filled_result["filled_count"],
                "linkedin_profile_fills": filled_result["linkedin_fills"],
                "resume_profile_source": profile.get("source", ""),
            },
        )

    async def _record_apply_attempt(
        self,
        job: Job,
        status: str,
        reason: str,
        details: Optional[dict[str, Any]] = None,
        resume_path: Optional[Path] = None,
    ) -> None:
        site = "unknown"
        job_url = (job.contact or "").strip()
        if "linkedin.com" in job_url.lower():
            site = "linkedin"
        elif "bossjob" in job_url.lower():
            site = "bossjob"

        rec = JobApplyRecord(
            job_id=job.id,
            status=status,
            reason=reason,
            site=site,
            job_url=job_url,
            resume_language=self._pick_resume_language(resume_path) if resume_path else None,
            resume_file=resume_path.name if resume_path else None,
            details=details or {},
        )
        self.db.add(rec)

    async def apply_to_job(self, job: Job) -> ApplyResult:
        job_url = (job.contact or "").strip()
        if not job_url:
            return ApplyResult(False, "missing_job_url", {})

        resume_path = self.pick_resume_for_job(job)
        if not resume_path:
            return ApplyResult(False, "resume_not_found", {})

        resume_language = self._pick_resume_language(resume_path)
        profile = self._load_resume_profile("zh" if resume_language == "zh" else "en")

        cookies = await self._load_job_cookies(job)

        async with async_playwright() as p:
            browser: Browser = await p.chromium.launch(headless=HEADLESS)
            try:
                context: BrowserContext = await browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1440, "height": 900},
                )
                if cookies:
                    try:
                        await context.add_cookies(cookies)
                    except Exception:
                        logger.warning("[AUTO APPLY] Failed to inject cookies for job %s", job.id)
                page = await context.new_page()

                if "linkedin.com" in job_url.lower():
                    result = await self._apply_linkedin(page, job_url, resume_path, profile)
                elif "bossjob" in job_url.lower():
                    result = await self._apply_bossjob(page, job_url, resume_path, profile)
                else:
                    result = ApplyResult(False, "unsupported_site", {"url": job_url})

                await context.close()
                return result
            finally:
                await browser.close()

    async def apply_and_record_job(self, job: Job, dry_run: bool = False) -> ApplyResult:
        """Apply to a single job and persist apply record/audit fields.

        This is used by both batch auto-apply and fetch-time immediate apply.
        """
        resume_path = self.pick_resume_for_job(job)

        if dry_run:
            await self._record_apply_attempt(
                job,
                status="dry_run",
                reason="dry_run",
                details={"url": job.contact},
                resume_path=resume_path,
            )
            return ApplyResult(True, "dry_run", {"url": job.contact})

        result = await self.apply_to_job(job)
        await self._record_apply_attempt(
            job,
            status="success" if result.success else "failed",
            reason=result.reason,
            details=result.details,
            resume_path=resume_path,
        )

        if result.success:
            job.is_applied = True
            job.applied_at = datetime.utcnow()
            note_suffix = f"auto-apply:{result.reason}"
            job.notes = f"{(job.notes or '').strip()}\n{note_suffix}".strip()

        return result

    async def run_frontend_remote_auto_apply(self, limit: int = 20, dry_run: bool = False) -> dict[str, Any]:
        query = (
            select(Job)
            .filter(Job.is_hidden == False)
            .filter(Job.is_applied == False)
            .order_by(Job.source_published_at.desc().nullslast(), Job.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(query)
        jobs = result.scalars().all()

        attempted = 0
        succeeded = 0
        details: list[dict[str, Any]] = []

        for job in jobs:
            attempted += 1
            result = await self.apply_and_record_job(job, dry_run=dry_run)
            details.append({
                "job_id": job.id,
                "status": "dry_run" if dry_run else ("success" if result.success else "failed"),
                "reason": result.reason,
                "details": result.details,
            })

            if result.success and not dry_run:
                succeeded += 1

        if not dry_run:
            await self.db.commit()

        return {
            "total_loaded": len(jobs),
            "attempted": attempted,
            "succeeded": succeeded,
            "skipped": 0,
            "dry_run": dry_run,
            "details": details,
        }
