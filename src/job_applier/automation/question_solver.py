from __future__ import annotations

import os
import re

from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
)
from job_applier.utils import get_project_root


class QuestionSolver:
    """Answers job application screening questions using rule heuristics and AI."""

    def __init__(self, profile: CandidateProfile, api_key: str | None = None):
        self.profile = profile
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY")

    def answer_question(
        self,
        question_text: str,
        options: list[str] | None = None,
        job_title: str = "",
        company: str = "",
    ) -> str:
        """Determines the best answer for an application question."""
        cleaned_q = question_text.strip().lower()

        # 1. Check custom user-defined answers
        for key, ans in self.profile.custom_answers.items():
            if key.lower() in cleaned_q:
                return self._match_to_options(ans, options)

        # 2. Rule-based heuristics for standard compliance/screening questions
        # Work authorization
        if any(
            w in cleaned_q
            for w in [
                "authorized to work",
                "legally eligible",
                "right to work",
                "legal right",
            ]
        ):
            return self._match_to_options(
                self.profile.work_authorization, options, default="Yes"
            )

        # Visa sponsorship
        if "sponsorship" in cleaned_q or "visa" in cleaned_q:
            # Check if asking if candidate DOES need sponsorship vs DOES NOT need
            if any(
                w in cleaned_q
                for w in ["require", "need", "future", "now or in the future"]
            ):
                return self._match_to_options(
                    self.profile.sponsorship_required, options, default="No"
                )
            return self._match_to_options("No", options, default="No")

        # Age requirement
        if any(
            w in cleaned_q
            for w in ["18 years", "at least 18", "age of 18", "legal age"]
        ):
            return self._match_to_options("Yes", options, default="Yes")

        # Prior employment
        if any(
            w in cleaned_q
            for w in [
                "previously employed",
                "previously worked",
                "former employee",
                "ever worked",
            ]
        ):
            return self._match_to_options("No", options, default="No")

        # Background check
        if any(w in cleaned_q for w in ["background check", "screening", "drug test"]):
            return self._match_to_options("Yes", options, default="Yes")

        # Relocation
        if "relocate" in cleaned_q:
            return self._match_to_options(
                self.profile.willing_to_relocate, options, default="No"
            )

        # Notice period / availability
        if any(
            w in cleaned_q
            for w in ["notice period", "how soon", "when can you start", "availability"]
        ):
            return self._match_to_options(
                self.profile.notice_period, options, default="1 month"
            )

        # Desired salary / compensation
        if any(
            w in cleaned_q
            for w in ["salary", "compensation", "remuneration", "pay rate"]
        ):
            return self._match_to_options(
                self.profile.salary_expectation, options, default="Negotiable"
            )

        # Referral / How did you hear
        if any(w in cleaned_q for w in ["how did you hear", "source", "hear about us"]):
            return self._match_to_options("LinkedIn", options, default="LinkedIn")

        # Gender / EEO
        if "gender" in cleaned_q:
            return self._match_to_options(
                self.profile.gender, options, default="Prefer not to say"
            )

        # Veteran status
        if "veteran" in cleaned_q:
            return self._match_to_options(
                self.profile.veteran_status,
                options,
                default="I am not a protected veteran",
            )

        # Disability
        if "disability" in cleaned_q:
            return self._match_to_options(
                self.profile.disability_status,
                options,
                default="No, I do not have a disability",
            )

        # Years of experience general
        if (
            "years of experience" in cleaned_q
            or "years of relevant experience" in cleaned_q
        ):
            return self._match_to_options(
                self.profile.years_of_experience,
                options,
                default=self.profile.years_of_experience,
            )

        # 3. If Gemini is available, use AI to answer custom question
        ai_answer = self._answer_with_ai(question_text, options, job_title, company)
        if ai_answer:
            return ai_answer

        # 4. Fallback: if options provided, pick first reasonable or positive option
        if options:
            return self._match_to_options("Yes", options, default=options[0])

        return "Yes"

    def _match_to_options(
        self, target: str, options: list[str] | None, default: str = ""
    ) -> str:
        """Matches a target answer against a list of dropdown/radio choices."""
        if not options:
            return target

        target_lower = target.lower()
        # Exact match
        for opt in options:
            if opt.strip().lower() == target_lower:
                return opt

        # Substring match
        for opt in options:
            opt_lower = opt.strip().lower()
            if target_lower in opt_lower or opt_lower in target_lower:
                return opt

        # Yes/No matching
        if target_lower in ["yes", "true", "y"]:
            for opt in options:
                if opt.strip().lower() in ["yes", "i do", "i agree", "true"]:
                    return opt
        elif target_lower in ["no", "false", "n"]:
            for opt in options:
                if opt.strip().lower() in ["no", "i do not", "false"]:
                    return opt

        # Decline / Prefer not to say
        if "prefer" in target_lower or "decline" in target_lower:
            for opt in options:
                if any(
                    w in opt.lower() for w in ["prefer not", "decline", "choose not"]
                ):
                    return opt

        return default or options[0]

    def _answer_with_ai(
        self,
        question_text: str,
        options: list[str] | None,
        job_title: str,
        company: str,
    ) -> str | None:
        """Uses Gemini to answer screening questions based on resume content."""
        if not self.api_key:
            return None

        try:
            from google import genai  # type: ignore[attr-defined]

            client = genai.Client(api_key=self.api_key)

            # Load master resume context
            master_resume_path = get_project_root() / "data" / "master_resume.json"
            resume_context = ""
            if master_resume_path.exists():
                try:
                    with open(master_resume_path, encoding="utf-8") as f:
                        resume_context = f.read()
                except Exception:
                    resume_context = ""

            options_prompt = ""
            if options:
                options_prompt = (
                    "Available options to choose from:\n"
                    + "\n".join(f"- {opt}" for opt in options)
                    + "\nYou MUST return exactly one of the available options verbatim."
                )
            else:
                options_prompt = "Provide a concise, direct answer (under 50 words, or single number/phrase if appropriate)."

            prompt = f"""
You are assisting candidate {self.profile.full_name} in answering a job application question for {job_title} at {company}.

Candidate Profile:
- Name: {self.profile.full_name}
- Email: {self.profile.email}
- Current role: {self.profile.current_title} at {self.profile.current_company}
- Location: {self.profile.city}, {self.profile.country}
- Years of experience: {self.profile.years_of_experience}

Resume data:
{resume_context[:2500]}

Question:
"{question_text}"

{options_prompt}

Output ONLY the final answer text with no extra commentary or formatting.
"""

            response = client.models.generate_content(
                model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
                contents=prompt,
            )
            raw_answer = (response.text or "").strip()
            # Clean quotes if returned
            clean_answer = re.sub(r'^["\']|["\']$', "", raw_answer).strip()

            if options:
                return self._match_to_options(clean_answer, options)
            return clean_answer

        except Exception as e:
            print(f"Notice: Gemini question answering fallback: {e}")
            return None
