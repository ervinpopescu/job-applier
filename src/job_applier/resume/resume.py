import argparse
import json
import sys
from pathlib import Path

from fpdf import FPDF

from job_applier.utils import get_project_root, sanitize_name


class PDF(FPDF):
    def header(self):
        pass

    def footer(self):
        self.set_y(-15)
        self.set_font("helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}", 0, 0, "C")

    def section_title(self, label):
        self.set_font("helvetica", "B", 12)
        self.ln(5)
        self.cell(0, 6, label.upper(), 0, 1, "L")
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(2)

    def chapter_body(self, text):
        self.set_font("helvetica", "", 10)
        self.multi_cell(0, 5, text)
        self.ln()

    def job_entry(self, role, company, dates, details):
        self.set_font("helvetica", "B", 10)
        self.cell(0, 5, f"{role} at {company}", 0, 1)
        self.set_font("helvetica", "I", 9)
        self.cell(0, 5, dates, 0, 1)
        self.set_font("helvetica", "", 10)
        for detail in details:
            self.multi_cell(0, 5, f"- {detail}")
        self.ln(3)


def sanitize_text(text):
    if not isinstance(text, str):
        return text
    # Replace common unicode characters that latin-1 cannot handle
    replacements = {
        "\u2013": "-",  # en dash
        "\u2014": "--",  # em dash
        "\u2018": "'",  # left single quote
        "\u2019": "'",  # right single quote
        "\u201c": '"',  # left double quote
        "\u201d": '"',  # right double quote
        "\u2022": "-",  # bullet
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.encode("latin-1", "replace").decode("latin-1")


def generate_resume(data, output_filename):
    pdf = PDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # --- HEADER (Contact) ---
    contact = data.get("contact", {})
    pdf.set_font("helvetica", "B", 16)
    pdf.cell(0, 10, sanitize_text(contact.get("name", "Name Missing")), 0, 1, "C")

    pdf.set_font("helvetica", "", 10)

    # Helper for links
    def add_link(label, link, protocol=""):
        if not link:
            return
        full_link = f"{protocol}{link}" if protocol else link
        if (
            not protocol
            and not link.startswith("http")
            and ("github" in label.lower() or "linkedin" in label.lower())
        ):
            full_link = "https://" + link

        w_label = pdf.get_string_width(label)
        w_link = pdf.get_string_width(link)

        pdf.set_x((210 - (w_label + w_link)) / 2)
        pdf.write(5, sanitize_text(label))
        pdf.write(5, sanitize_text(link), link=full_link)
        pdf.ln()

    if "phone" in contact:
        add_link("Phone: ", contact["phone"], "tel:")

    if "email" in contact:
        add_link("Email: ", contact["email"], "mailto:")

    if "linkedin" in contact:
        add_link("LinkedIn: ", contact["linkedin"])

    if "github" in contact:
        add_link("Github: ", contact["github"])

    if "location" in contact:
        pdf.cell(0, 5, sanitize_text(f"Location: {contact['location']}"), 0, 1, "C")

    if "languages" in contact:
        pdf.cell(0, 5, sanitize_text(f"Languages: {contact['languages']}"), 0, 1, "C")

    pdf.ln(5)

    # --- SUMMARY ---
    if "summary" in data:
        pdf.section_title("Professional Summary")
        if pdf.get_y() > 250:
            pdf.add_page()
        pdf.chapter_body(sanitize_text(data["summary"]))

    # --- EXPERIENCE ---
    if "experience" in data and data["experience"]:
        pdf.section_title("Work Experience")
        for job in data["experience"]:
            pdf.job_entry(
                sanitize_text(job.get("role", "")),
                sanitize_text(job.get("company", "")),
                sanitize_text(job.get("dates", "")),
                [sanitize_text(d) for d in job.get("details", [])],
            )

    # --- SKILLS ---
    if "skills" in data and data["skills"]:
        pdf.section_title("Technical Skills")
        pdf.set_font("helvetica", "", 10)
        for skill in data["skills"]:
            pdf.multi_cell(0, 5, sanitize_text(f"- {skill}"))
        pdf.ln(3)

    # --- EDUCATION ---
    if "education" in data:
        edu = data["education"]
        pdf.section_title("Education")
        pdf.set_font("helvetica", "B", 10)
        pdf.cell(0, 5, sanitize_text(edu.get("institution", "")), 0, 1)
        pdf.set_font("helvetica", "", 10)
        pdf.cell(0, 5, sanitize_text(edu.get("degree", "")), 0, 1)
        if "details" in edu:
            pdf.multi_cell(0, 5, sanitize_text(edu["details"]))
        pdf.ln(3)

    # --- PROJECTS ---
    if "projects" in data and data["projects"]:
        pdf.section_title("Open Source & Personal Projects")
        pdf.set_font("helvetica", "", 10)
        for proj in data["projects"]:
            name = proj.get("name", "")
            desc = proj.get("description", "")
            url = proj.get("url", "")

            pdf.write(5, "- ")
            pdf.write(5, sanitize_text(name), link=url)
            pdf.write(5, sanitize_text(f": {desc}"))
            pdf.ln()

    # --- OUTPUT ---
    try:
        output_path = Path(output_filename)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pdf.output(str(output_path))
        print(f"PDF generated successfully: {output_filename}")
    except Exception as e:
        print(f"Error generating PDF: {e}")


if __name__ == "__main__":
    project_root = get_project_root()
    default_input = project_root / "data" / "master_resume.json"
    default_output = project_root / "output" / "tailored_resume.pdf"
    if default_input.exists():
        try:
            with open(default_input, encoding="utf-8") as f:
                cand_name = json.load(f).get("contact", {}).get("name", "")
                if cand_name:
                    default_output = (
                        project_root / "output" / f"{sanitize_name(cand_name)}_CV.pdf"
                    )
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Generate PDF resume from JSON.")
    parser.add_argument(
        "--input",
        "-i",
        default=str(default_input),
        help="Input JSON file (default: data/master_resume.json)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=str(default_output),
        help=f"Output PDF file (default: {default_output})",
    )

    args = parser.parse_args()

    try:
        with open(args.input, encoding="utf-8") as f:
            resume_data = json.load(f)
        generate_resume(resume_data, args.output)
    except FileNotFoundError:
        print(f"Error: Input file '{args.input}' not found.")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error: Failed to parse JSON from '{args.input}'.")
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        sys.exit(1)
