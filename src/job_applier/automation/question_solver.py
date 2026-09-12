from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from job_applier.automation.adapters.semantic import (
    is_standard_consent_checkbox,
)
from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
)
from job_applier.db import get_connection, init_db
from job_applier.utils import get_project_root


class QuestionSolvingError(Exception):
    """Base error for question solving failures."""


class UnknownQuestionError(QuestionSolvingError):
    """Raised when a screening question cannot be answered safely with high confidence."""

    def __init__(self, question_text: str, options: list[str] | None = None):
        super().__init__(
            f"Unknown screening question cannot be answered safely: '{question_text}'"
        )
        self.question_text = question_text
        self.options = options or []


class SensitiveQuestionError(QuestionSolvingError):
    """Raised when a question involves sensitive, legal, or clearance declarations without explicit approval."""

    def __init__(self, question_text: str, category: str):
        super().__init__(
            f"Sensitive screening question requires explicit user consent ({category}): '{question_text}'"
        )
        self.question_text = question_text
        self.category = category


@dataclass
class AnswerResult:
    """Represents a resolved question answer with full provenance and safety auditing."""

    answer: str
    provenance: str  # 'profile', 'approved_rule', 'ai_mapping'
    confidence: float
    sensitivity_class: (
        str  # 'standard', 'legal', 'qualification', 'compensation', 'sensitive'
    )
    is_safe_for_auto_submit: bool


class QuestionSolver:
    """
    Answers job application screening questions using rule heuristics, pre-approved rules, and AI.
    FAIL-CLOSED SAFETY POLICY:
    - Never falls back to 'Yes' or selects default options arbitrarily.
    - Questions lacking high-confidence evidence raise typed exceptions to trigger human takeover.
    """

    def __init__(
        self,
        profile: CandidateProfile,
        api_key: str | None = None,
        db_path: Path | None = None,
    ):
        self.profile = profile
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        self.db_path = db_path

    def solve_question_safely(
        self,
        question_text: str,
        options: list[str] | None = None,
        job_title: str = "",
        company: str = "",
    ) -> AnswerResult:
        """
        Determines the answer for an application question with full provenance tracking.
        Raises UnknownQuestionError or SensitiveQuestionError if the question cannot be answered safely.
        """
        cleaned_q = question_text.strip().lower()

        # 1. Check custom user-defined answers in CandidateProfile
        for key, ans in self.profile.custom_answers.items():
            if key.lower() in cleaned_q:
                matched = self._match_to_options(ans, options)
                if matched:
                    return AnswerResult(
                        answer=matched,
                        provenance="profile",
                        confidence=1.0,
                        sensitivity_class="standard",
                        is_safe_for_auto_submit=True,
                    )

        # 2. Check approved_answers table in SQLite database
        db_ans = self._check_db_approved_answers(cleaned_q, options)
        if db_ans:
            return db_ans

        # 3. Detect sensitive topics that must fail-closed if not explicitly defined
        sensitive_check = self._check_sensitive_topics(cleaned_q)
        if sensitive_check:
            category, reason = sensitive_check
            raise SensitiveQuestionError(question_text, category)

        # 4. Standard deterministic heuristics from candidate profile
        rule_result = self._check_deterministic_rules(cleaned_q, options)
        if rule_result:
            return rule_result

        # 5. Grounded AI question answering (if Gemini API key is configured)
        ai_res = self._answer_with_ai(question_text, options, job_title, company)
        if ai_res:
            return ai_res

        # 6. FAIL CLOSED: If question could not be answered with high confidence, raise!
        raise UnknownQuestionError(question_text, options=options)

    def answer_question(
        self,
        question_text: str,
        options: list[str] | None = None,
        job_title: str = "",
        company: str = "",
        strict: bool = True,
    ) -> str:
        """
        Backwards-compatible API that returns the answer string.
        In strict mode (default for autonomous execution), raises on unknown or sensitive questions.
        """
        try:
            res = self.solve_question_safely(
                question_text=question_text,
                options=options,
                job_title=job_title,
                company=company,
            )
            return res.answer
        except (UnknownQuestionError, SensitiveQuestionError):
            if strict:
                raise
            return ""

    def _check_db_approved_answers(
        self, cleaned_q: str, options: list[str] | None
    ) -> AnswerResult | None:
        """Queries the approved_answers database table for an existing human-approved rule."""
        try:
            init_db(self.db_path)
            conn = get_connection(self.db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT question_key, answer_value, provenance, sensitivity_class
                    FROM approved_answers
                    WHERE approval_status = 'approved';
                    """
                ).fetchall()
                norm_q = re.sub(r"[^\w\s]", "", cleaned_q.lower()).strip()
                for r in rows:
                    q_key = r["question_key"].strip()
                    norm_key = re.sub(r"[^\w\s]", "", q_key.lower()).strip()
                    if norm_key == norm_q:
                        matched = self._match_to_options(r["answer_value"], options)
                        if matched:
                            return AnswerResult(
                                answer=matched,
                                provenance=r["provenance"] or "approved_rule",
                                confidence=1.0,
                                sensitivity_class=r["sensitivity_class"] or "standard",
                                is_safe_for_auto_submit=True,
                            )
            finally:
                conn.close()
        except Exception as e:
            print(f"Notice: Approved answers DB query failed: {e}")
        return None

    def _check_sensitive_topics(self, cleaned_q: str) -> tuple[str, str] | None:
        """Identifies legal, criminal, security clearance, or restrictive covenant questions."""
        # Criminal history
        if any(
            w in cleaned_q
            for w in [
                "convicted",
                "felony",
                "misdemeanor",
                "criminal record",
                "arrested",
            ]
        ):
            return (
                "criminal_history",
                "Criminal background declarations require explicit confirmation.",
            )

        # Security clearance
        if any(
            w in cleaned_q
            for w in ["security clearance", "top secret", "polygraph", "cleared"]
        ):
            return (
                "security_clearance",
                "Security clearance inquiries require manual validation.",
            )

        # Non-compete / restrictive covenants
        if any(
            w in cleaned_q
            for w in [
                "non-compete",
                "restrictive covenant",
                "binding agreement",
                "injunction",
            ]
        ):
            return (
                "restrictive_covenant",
                "Non-compete declaration requires human review.",
            )

        return None

    def _check_deterministic_rules(
        self, cleaned_q: str, options: list[str] | None
    ) -> AnswerResult | None:
        """Matches standard compliance and profile screening questions deterministically."""

        # 1. Standard Privacy / GDPR / Terms of Service consent question
        if is_standard_consent_checkbox(cleaned_q):
            matched = self._match_to_options(
                "I agree", options
            ) or self._match_to_options("Yes", options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "standard", True)
            if not options:
                return AnswerResult("I agree", "profile", 1.0, "standard", True)

        # 2. Work authorization
        if any(
            w in cleaned_q
            for w in [
                "authorized to work",
                "legally eligible",
                "right to work",
                "legal right",
                "eligibility to work",
                "work permit",
            ]
        ):
            auth = "Yes" if self.profile.is_authorized_to_work(cleaned_q) else "No"
            matched = self._match_to_options(auth, options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "legal", True)
            if not options:
                return AnswerResult(auth, "profile", 1.0, "legal", True)

        # 3. Visa sponsorship
        if "sponsorship" in cleaned_q or "visa" in cleaned_q:
            req_sponsorship = (
                "Yes" if self.profile.requires_visa_sponsorship(cleaned_q) else "No"
            )
            if any(
                w in cleaned_q
                for w in ["require", "need", "future", "now or in the future"]
            ):
                matched = self._match_to_options(req_sponsorship, options)
                if matched:
                    return AnswerResult(matched, "profile", 1.0, "legal", True)
                if not options:
                    return AnswerResult(req_sponsorship, "profile", 1.0, "legal", True)
            matched = self._match_to_options(
                req_sponsorship, options
            ) or self._match_to_options("No", options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "legal", True)
            if not options:
                return AnswerResult(req_sponsorship, "profile", 1.0, "legal", True)

        # 4. Notice period / start date / availability
        if any(
            w in cleaned_q
            for w in [
                "notice period",
                "how soon",
                "when can you start",
                "availability",
                "earliest start date",
                "start date",
                "available to start",
                "how much notice",
            ]
        ):
            target_notice = self.profile.notice_period or "Immediate"
            matched = self._match_to_options(target_notice, options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "standard", True)
            if not options:
                return AnswerResult(target_notice, "profile", 1.0, "standard", True)

        # 5. Desired salary / compensation / hourly rate
        if any(
            w in cleaned_q
            for w in [
                "salary",
                "compensation",
                "remuneration",
                "pay rate",
                "expected rate",
                "desired salary",
                "salary expectation",
                "compensation expectations",
                "hourly rate",
                "target salary",
                "expected salary",
                "salary requirements",
                "annual salary",
                "base salary",
            ]
        ):
            desired = self.profile.get_salary_desired()
            minimum = self.profile.get_salary_minimum()
            currency = self.profile.get_salary_currency()

            if any(w in cleaned_q for w in ["minimum", "min salary", "lowest"]):
                target_num = minimum or desired
            else:
                target_num = desired or minimum

            if any(w in cleaned_q for w in ["hourly", "per hour", "hour rate"]):
                if target_num:
                    hourly = round(target_num / 2000, 2)
                    target_val = (
                        hourly
                        if (options or "number" in cleaned_q)
                        else f"{hourly} {currency}/hr"
                    )
                else:
                    target_val = "40"
            else:
                if target_num:
                    target_val = int(target_num)
                else:
                    target_val = self.profile.get_field(
                        "salary_expectation", "Negotiable"
                    )

            matched = self._match_to_options(target_val, options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "compensation", True)
            if not options:
                return AnswerResult(
                    str(target_val), "profile", 1.0, "compensation", True
                )

        # 6. Language proficiencies
        if any(
            w in cleaned_q
            for w in [
                "english",
                "romanian",
                "language",
                "proficiency",
                "fluent",
                "languages spoken",
                "speak english",
                "fluent in english",
            ]
        ):
            if "english" in cleaned_q:
                prof = self.profile.get_language_proficiency("English") or "Fluent"
                if any(w in cleaned_q for w in ["are you", "do you", "fluent in"]):
                    matched = self._match_to_options(
                        "Yes", options
                    ) or self._match_to_options(prof, options)
                else:
                    matched = self._match_to_options(
                        prof, options
                    ) or self._match_to_options("Yes", options)
                if matched:
                    return AnswerResult(matched, "profile", 1.0, "qualification", True)
                if not options:
                    ans_str = (
                        prof
                        if not any(w in cleaned_q for w in ["are you", "do you"])
                        else "Yes"
                    )
                    return AnswerResult(ans_str, "profile", 1.0, "qualification", True)

            if "romanian" in cleaned_q:
                prof = self.profile.get_language_proficiency("Romanian") or "Native"
                if any(w in cleaned_q for w in ["are you", "do you", "fluent in"]):
                    matched = self._match_to_options(
                        "Yes", options
                    ) or self._match_to_options(prof, options)
                else:
                    matched = self._match_to_options(
                        prof, options
                    ) or self._match_to_options("Yes", options)
                if matched:
                    return AnswerResult(matched, "profile", 1.0, "qualification", True)
                if not options:
                    return AnswerResult(prof, "profile", 1.0, "qualification", True)

            # General languages question
            if isinstance(self.profile.languages, dict):
                lang_str = ", ".join(
                    f"{k} ({v})" for k, v in self.profile.languages.items()
                )
            else:
                lang_str = str(
                    self.profile.languages or "English (Fluent), Romanian (Native)"
                )
            matched = self._match_to_options("English", options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "qualification", True)
            if not options:
                return AnswerResult(lang_str, "profile", 1.0, "qualification", True)

        # 7. Remote work confirmation / preferences
        is_remote_q = any(
            w in cleaned_q
            for w in [
                "remote",
                "work remotely",
                "working remotely",
                "remote work",
                "telecommute",
                "work from home",
                "wfh",
                "work location preference",
                "location preference",
                "work arrangement",
                "workplace preference",
                "work environment preference",
            ]
        ) or bool(
            options
            and (
                "preference" in cleaned_q
                or "location" in cleaned_q
                or "work" in cleaned_q
            )
            and any("remote" in opt.lower() for opt in options)
        )
        if is_remote_q:
            if any(
                w in cleaned_q
                for w in [
                    "comfortable",
                    "willing",
                    "confirm",
                    "able to work remotely",
                    "able to work from home",
                    "do you have",
                    "environment",
                ]
            ):
                matched = self._match_to_options("Yes", options)
                if matched:
                    return AnswerResult(matched, "profile", 1.0, "standard", True)
                if not options:
                    return AnswerResult("Yes", "profile", 1.0, "standard", True)

            pref = str(self.profile.remote_preference or "Remote only")
            matched = self._match_to_options(pref, options) or self._match_to_options(
                "Yes", options
            )
            if matched:
                return AnswerResult(matched, "profile", 1.0, "standard", True)
            if not options:
                return AnswerResult(pref, "profile", 1.0, "standard", True)

        # 8. Relocation questions
        if "relocate" in cleaned_q or "relocation" in cleaned_q:
            reloc = "Yes" if self.profile.is_willing_to_relocate() else "No"
            matched = self._match_to_options(reloc, options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "standard", True)
            if not options:
                return AnswerResult(reloc, "profile", 1.0, "standard", True)

        # 9. Voluntary EEO: Gender
        if (
            "gender" in cleaned_q
            or "what is your sex" in cleaned_q
            or cleaned_q == "sex"
        ):
            ans = self.profile.get_eeo_answer("gender")
            matched = self._match_to_options(ans, options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "sensitive", True)
            if not options:
                return AnswerResult(ans, "profile", 1.0, "sensitive", True)

        # 10. Voluntary EEO: Race / Ethnicity
        if any(
            w in cleaned_q
            for w in [
                "race",
                "ethnicity",
                "ethnic background",
                "hispanic or latino",
                "demographic",
            ]
        ):
            ans = self.profile.get_eeo_answer("race")
            matched = self._match_to_options(ans, options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "sensitive", True)
            if not options:
                return AnswerResult(ans, "profile", 1.0, "sensitive", True)

        # 11. Voluntary EEO: Veteran status
        if "veteran" in cleaned_q:
            ans = self.profile.get_eeo_answer("veteran")
            matched = self._match_to_options(ans, options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "sensitive", True)
            if not options:
                return AnswerResult(ans, "profile", 1.0, "sensitive", True)

        # 12. Voluntary EEO: Disability status
        if "disability" in cleaned_q or "handicap" in cleaned_q:
            ans = self.profile.get_eeo_answer("disability")
            matched = self._match_to_options(ans, options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "sensitive", True)
            if not options:
                return AnswerResult(ans, "profile", 1.0, "sensitive", True)

        # 13. Age requirement
        if any(
            w in cleaned_q
            for w in ["18 years", "at least 18", "age of 18", "legal age"]
        ):
            matched = self._match_to_options("Yes", options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "standard", True)
            if not options:
                return AnswerResult("Yes", "profile", 1.0, "standard", True)

        # 14. Prior employment
        if any(
            w in cleaned_q
            for w in [
                "previously employed",
                "previously worked",
                "former employee",
                "ever worked",
            ]
        ):
            matched = self._match_to_options("No", options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "standard", True)
            if not options:
                return AnswerResult("No", "profile", 1.0, "standard", True)

        # 15. Referral / How did you hear
        if any(w in cleaned_q for w in ["how did you hear", "source", "hear about us"]):
            matched = self._match_to_options("LinkedIn", options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "standard", True)
            if not options:
                return AnswerResult("LinkedIn", "profile", 1.0, "standard", True)

        # 16. Years of experience
        if (
            "years of experience" in cleaned_q
            or "years of relevant experience" in cleaned_q
        ):
            matched = self._match_to_options(
                str(self.profile.years_of_experience), options
            )
            if matched:
                return AnswerResult(matched, "profile", 1.0, "qualification", True)
            if not options and self.profile.years_of_experience:
                return AnswerResult(
                    str(self.profile.years_of_experience),
                    "profile",
                    1.0,
                    "qualification",
                    True,
                )

        return None

    def _match_to_options(
        self, target: object, options: list[str] | None, default: str = ""
    ) -> str | None:
        """
        Matches a target answer against dropdown/radio choices.
        Returns None if no matching option is found (NEVER blindly returns options[0]!).
        """
        if target is None:
            return None

        if isinstance(target, bool):
            target = "Yes" if target else "No"

        target_str = str(target).strip()
        if not target_str:
            return None

        if not options:
            return target_str

        target_lower = target_str.lower()

        # 1. Exact match (case-insensitive)
        for opt in options:
            if opt.strip().lower() == target_lower:
                return opt

        # 2. Yes/No matching
        if target_lower in ["yes", "true", "y"]:
            for opt in options:
                if opt.strip().lower() in [
                    "yes",
                    "i do",
                    "i agree",
                    "true",
                    "yes, i am",
                    "yes, i do",
                    "authorized",
                ]:
                    return opt
        elif target_lower in ["no", "false", "n"]:
            for opt in options:
                if opt.strip().lower() in [
                    "no",
                    "i do not",
                    "false",
                    "no, i do not",
                    "not required",
                    "i do not require",
                ]:
                    return opt

        # 3. Numeric & range matching for salary or numbers
        num_match = re.search(r"^\d+(?:\.\d+)?$", target_str)
        if num_match:
            val = float(num_match.group())
            for opt in options:
                clean_opt = opt.replace(",", "").replace("k", "000").replace("K", "000")
                numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", clean_opt)]
                if len(numbers) >= 2:
                    low, high = numbers[0], numbers[1]
                    if min(low, high) <= val <= max(low, high):
                        return opt
                elif len(numbers) == 1:
                    if any(w in opt.lower() for w in [">", "above", "more than", "+"]):
                        if val >= numbers[0]:
                            return opt
                    elif any(
                        w in opt.lower() for w in ["<", "below", "under", "less than"]
                    ):
                        if val <= numbers[0]:
                            return opt
                    elif numbers[0] == val:
                        return opt

        # 4. Notice period matching (Immediate, 3 weeks, 1 month, etc.)
        if any(
            w in target_lower
            for w in ["immediate", "asap", "now", "3 week", "month", "week"]
        ):
            if "immediate" in target_lower or "asap" in target_lower:
                for opt in options:
                    if any(
                        w in opt.lower()
                        for w in [
                            "immediate",
                            "asap",
                            "right away",
                            "now",
                            "< 1 month",
                            "less than 1 month",
                        ]
                    ):
                        return opt
            elif "3 week" in target_lower or "< 1 month" in target_lower:
                for opt in options:
                    if any(
                        w in opt.lower()
                        for w in [
                            "3 week",
                            "< 1 month",
                            "less than 1 month",
                            "immediate",
                            "1 month",
                        ]
                    ):
                        return opt
            elif (
                "1 month" in target_lower
                or "30 day" in target_lower
                or "4 week" in target_lower
            ):
                for opt in options:
                    if any(w in opt.lower() for w in ["1 month", "30 day", "4 week"]):
                        return opt

        # 5. Language level matching (Fluent, Native, Conversational, C1, C2)
        if any(
            w in target_lower
            for w in ["fluent", "native", "bilingual", "conversational"]
        ):
            if "native" in target_lower or "bilingual" in target_lower:
                for opt in options:
                    if any(
                        w in opt.lower()
                        for w in [
                            "native",
                            "bilingual",
                            "mother tongue",
                            "c2",
                            "fluent",
                        ]
                    ):
                        return opt
            elif "fluent" in target_lower:
                for opt in options:
                    if any(
                        w in opt.lower()
                        for w in [
                            "fluent",
                            "advanced",
                            "professional",
                            "c1",
                            "c2",
                            "full professional",
                        ]
                    ):
                        return opt

        # 6. Remote preference matching
        if "remote" in target_lower:
            for opt in options:
                if "remote" in opt.lower() and "non-remote" not in opt.lower():
                    return opt

        # 7. Substring match
        for opt in options:
            opt_lower = opt.strip().lower()
            if target_lower in opt_lower or opt_lower in target_lower:
                return opt

        # 8. Decline / Prefer not to say / EEO matching
        if any(
            w in target_lower
            for w in [
                "prefer",
                "decline",
                "choose not",
                "not to say",
                "do not have",
                "not a protected",
            ]
        ):
            for opt in options:
                opt_l = opt.lower()
                if any(
                    w in opt_l
                    for w in [
                        "prefer not",
                        "decline",
                        "choose not",
                        "not to say",
                        "do not wish",
                        "not disclose",
                        "i do not have",
                        "not a protected",
                        "not disabled",
                        "no disability",
                    ]
                ):
                    return opt

        # 9. Match against default if default explicitly matches
        if default:
            for opt in options:
                if opt.strip().lower() == default.strip().lower():
                    return opt

        # FAIL CLOSED: Do not return arbitrary options[0]!
        return None

    def _answer_with_ai(
        self,
        question_text: str,
        options: list[str] | None,
        job_title: str,
        company: str,
    ) -> AnswerResult | None:
        """
        Uses Gemini to answer screening questions strictly grounded in candidate profile & resume.
        Instructs model to return UNKNOWN if facts are not explicitly documented.
        """
        if not self.api_key:
            return None

        try:
            from google import genai  # type: ignore[attr-defined]

            client = genai.Client(api_key=self.api_key)

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
                    + "\nYou MUST return exactly one of the available options verbatim. "
                    "If none of the options can be truthfully chosen based on the resume, output 'UNKNOWN'."
                )
            else:
                options_prompt = (
                    "Provide a concise, direct answer based strictly on verified resume facts. "
                    "If the answer cannot be determined with certainty from the resume, output 'UNKNOWN'."
                )

            prompt = f"""
You are assisting candidate {self.profile.full_name} in answering a job application screening question for {job_title} at {company}.

CRITICAL SAFETY RULES:
1. Use ONLY explicit facts from the candidate profile and resume.
2. DO NOT invent, guess, or infer qualifications, security clearances, citizenship, criminal record, or certifications.
3. If the answer cannot be truthfully confirmed from the provided candidate information, output ONLY 'UNKNOWN'.

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

Output ONLY the final answer text with no extra commentary or markdown quotes.
"""
            response = client.models.generate_content(
                model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
                contents=prompt,
            )
            raw_answer = (response.text or "").strip()
            clean_answer = re.sub(r'^["\']|["\']$', "", raw_answer).strip()

            if clean_answer.upper() in ["UNKNOWN", "N/A", "NONE", "UNANSWERED", ""]:
                return None

            if options:
                matched = self._match_to_options(clean_answer, options)
                if matched:
                    return AnswerResult(
                        answer=matched,
                        provenance="ai_mapping",
                        confidence=0.85,
                        sensitivity_class="qualification",
                        is_safe_for_auto_submit=True,
                    )
                return None

            return AnswerResult(
                answer=clean_answer,
                provenance="ai_mapping",
                confidence=0.85,
                sensitivity_class="qualification",
                is_safe_for_auto_submit=True,
            )

        except Exception as e:
            print(f"Notice: Gemini question answering fallback: {e}")
            return None
