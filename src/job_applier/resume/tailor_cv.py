import argparse
import json
import os
from pathlib import Path

from google import genai  # type: ignore[attr-defined]
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

from job_applier.utils import get_project_root


@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=20))
def generate_content_with_retry(client, model, contents):
    return client.models.generate_content(model=model, contents=contents)


def tailor_resume(
    api_key,
    master_resume_path,
    job_description,
    output_dir=None,
    custom_instructions="",
    target_role="",
):
    """
    job_description can be a path or the actual text.
    """
    effective_key = (api_key or os.environ.get("GOOGLE_API_KEY", "")).strip()
    if not effective_key:
        print(
            "Error: GOOGLE_API_KEY is required. Please set it in .env or pass api_key."
        )
        return None, None
    client = genai.Client(api_key=effective_key)

    # 2. Load Data
    try:
        with open(master_resume_path) as f:
            master_resume = f.read()
    except FileNotFoundError:
        print(f"Error: Master resume not found at {master_resume_path}")
        return None, None

    # Handle JD as path or text
    jd_text = job_description
    if os.path.isfile(job_description):
        try:
            with open(job_description) as f:
                jd_text = f.read()
        except Exception as e:
            print(f"Error reading JD file: {e}")
            return None, None

    # 3. Construct Prompt
    role_clause = (
        f"specializing in {target_role} roles."
        if target_role
        else "specializing in software engineering, cloud, architecture, and professional technical roles."
    )
    custom_clause = (
        f"\n    **Candidate Guidance & Preferences:**\n    {custom_instructions}\n"
        if custom_instructions
        else ""
    )
    prompt = f"""
    You are an expert Resume Writer and Career Coach {role_clause}
    {custom_clause}
    **Task:**
    1. Tailor the following "Master Resume" (JSON) to perfectly match the provided "Job Description".
    2. Generate a highly personalized "Cover Letter" based on the tailored resume and the JD.

    **Instructions for Resume:**
    1. **Language:** Detect the language of the Job Description. If it is in German, French, Spanish, Romanian, Italian, or another language, provide the "Professional Summary", "Experience" bullet points, and Cover Letter in that language. If it is English, use English.
    2. **Analyze** the JD for keywords (specific tools, soft skills, methodologies).
    3. **Rewrite** the "Professional Summary" to strictly align with the JD.
    4. **Select & Prioritize** the "Experience" bullet points. Rephrase them slightly to match the JD's terminology.
    5. **Filter** the "Skills" list to prioritize what is mentioned in the JD.

    **Instructions for Cover Letter:**
    1. **Language:** Match the language of the Job Description.
    2. **Tone:** Professional, enthusiastic, and confident.
    3. **Structure:** Standard cover letter format (Salutation, Introduction, Why Me, Why the Company, Call to Action).
    4. **Personalize:** Mention specific requirements from the JD and how your background fits.

    **Output Format:**
    Return a JSON object with two keys:
    - "tailored_resume": The modified resume JSON.
    - "cover_letter": The text of the cover letter.

    Do not add markdown formatting (like ```json), just the raw JSON string.

    **Master Resume (JSON):**
    {master_resume}

    **Job Description:**
    {jd_text}
    """

    print("Sending request to Gemini... (this may take a few seconds)")

    # 4. Generate
    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    try:
        try:
            response = generate_content_with_retry(
                client, model=gemini_model, contents=prompt
            )
        except Exception as model_err:
            print(
                f"Notice: Model {gemini_model} error ({model_err}). Retrying with gemini-2.5-flash..."
            )
            response = generate_content_with_retry(
                client, model="gemini-2.5-flash", contents=prompt
            )
        cleaned_text = response.text.replace("```json", "").replace("```", "").strip()

        # Validate JSON
        combined_output = json.loads(cleaned_text)
        tailored_json = combined_output.get("tailored_resume")
        cover_letter = combined_output.get("cover_letter")

        # 5. Save if output_dir is provided
        if output_dir:
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)

            resume_file = output_path / "tailored_resume.json"
            with open(resume_file, "w") as f:
                json.dump(tailored_json, f, indent=2)

            cl_file = output_path / "cover_letter.txt"
            with open(cl_file, "w") as f:
                f.write(cover_letter)

            print(f"Success! Saved to {output_dir}")

        return tailored_json, cover_letter

    except RetryError:
        print(
            "Error: Gemini model is overloaded and timed out after multiple retries. Try again later or switch models."
        )
        return None, None
    except Exception as e:
        print(f"Error communicating with Gemini: {e}")
        return None, None


if __name__ == "__main__":
    # Get the project root directory (one level up from this script)
    project_root = get_project_root()
    default_master = project_root / "data" / "master_resume.json"
    default_output_dir = project_root / "output" / "latest_application"

    parser = argparse.ArgumentParser(description="Tailor a resume using Gemini API")
    parser.add_argument(
        "--api_key", help="Google Gemini API Key (or set GOOGLE_API_KEY env var)"
    )
    parser.add_argument(
        "--master", default=str(default_master), help="Path to master resume JSON"
    )
    parser.add_argument(
        "--jd", required=True, help="Path to text file containing the Job Description"
    )
    parser.add_argument(
        "--output_dir",
        default=str(default_output_dir),
        help="Output directory for results",
    )

    args = parser.parse_args()

    # Get API Key from arg or env
    api_key = args.api_key or os.environ.get("GOOGLE_API_KEY")

    if not api_key:
        print(
            "Error: API Key is required. Pass it via --api_key or set GOOGLE_API_KEY environment variable."
        )
        print("Get one here: https://aistudio.google.com/app/apikey")
        exit(1)

    tailor_resume(api_key, args.master, args.jd, args.output_dir)
