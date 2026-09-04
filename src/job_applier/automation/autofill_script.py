from __future__ import annotations

import json
from pathlib import Path

from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
)


def generate_autofill_javascript(
    profile: CandidateProfile,
    cover_letter: str = "",
    cv_path: str = "",
) -> str:
    """
    Generates a browser-executable JavaScript script that reliably populates job application forms
    across modern React, Angular, Vue, and traditional HTML frameworks (Greenhouse, Lever, Workday,
    Indeed, Ashby, SmartRecruiters, BestJobs, and generic portals).
    """
    profile_data = {
        "firstName": profile.first_name,
        "lastName": profile.last_name,
        "fullName": profile.full_name,
        "email": profile.email,
        "phone": profile.phone,
        "city": profile.city,
        "country": profile.country,
        "address": profile.address,
        "postalCode": profile.postal_code,
        "linkedin": profile.linkedin_url,
        "github": profile.github_url,
        "portfolio": profile.portfolio_url,
        "coverLetter": cover_letter,
        "currentCompany": profile.current_company,
        "currentTitle": profile.current_title,
        "cvPath": cv_path,
    }

    data_json = json.dumps(profile_data)

    js_code = f"""(function() {{
    const data = {data_json};
    console.log("[JobApplier] Running 1-Click Form Autofill for: " + data.fullName);

    function showToast(title, message, isSuccess) {{
        const existing = document.getElementById("job-applier-toast");
        if (existing) existing.remove();

        const toast = document.createElement("div");
        toast.id = "job-applier-toast";
        const bg = isSuccess ? "rgb(27, 94, 32)" : "rgb(180, 83, 9)";

        toast.style.cssText = "position:fixed;bottom:24px;right:24px;z-index:2147483647;background:" + bg + ";color:rgb(255,255,255);padding:16px 20px;border-radius:12px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,0.35);max-width:440px;font-size:13px;line-height:1.5;border:1px solid rgba(255,255,255,0.2);";

        let html = '<div style="display:flex;align-items:center;justify-content:between;margin-bottom:8px;">';
        html += '<strong style="font-size:14px;letter-spacing:0.3px;">⚡ JobApplier Assistant</strong>';
        html += '<span style="font-size:10px;opacity:0.8;margin-left:12px;">' + (isSuccess ? "Autofill Complete" : "Notice") + '</span>';
        html += '</div>';
        html += '<div style="margin-bottom:10px;">' + message + '</div>';

        if (data.coverLetter) {{
            html += '<div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.2);display:flex;gap:8px;">';
            html += '<button id="ja-copy-cl" style="background:rgba(255,255,255,0.2);color:white;border:none;padding:5px 10px;border-radius:6px;font-size:11px;font-weight:bold;cursor:pointer;">📋 Copy Cover Letter</button>';
            if (data.cvPath) {{
                html += '<button id="ja-copy-cv" style="background:rgba(255,255,255,0.2);color:white;border:none;padding:5px 10px;border-radius:6px;font-size:11px;font-weight:bold;cursor:pointer;">📄 Copy CV Path</button>';
            }}
            html += '</div>';
        }}

        toast.innerHTML = html;
        document.body.appendChild(toast);

        const copyClBtn = document.getElementById("ja-copy-cl");
        if (copyClBtn) {{
            copyClBtn.onclick = function() {{
                navigator.clipboard.writeText(data.coverLetter);
                copyClBtn.innerText = "✅ Copied Letter!";
                setTimeout(() => copyClBtn.innerText = "📋 Copy Cover Letter", 3000);
            }};
        }}
        const copyCvBtn = document.getElementById("ja-copy-cv");
        if (copyCvBtn) {{
            copyCvBtn.onclick = function() {{
                navigator.clipboard.writeText(data.cvPath);
                copyCvBtn.innerText = "✅ Copied Path!";
                setTimeout(() => copyCvBtn.innerText = "📄 Copy CV Path", 3000);
            }};
        }}

        setTimeout(() => {{
            if (toast && toast.parentNode) toast.remove();
        }}, 12000);
    }}

    // Check if on Cloudflare challenge page
    const pageText = (document.body ? document.body.innerText : "");
    const titleText = (document.title || "").toLowerCase();
    if (titleText.includes("just a moment") || pageText.includes("Additional Verification Required") || pageText.includes("Ray ID")) {{
        showToast("Cloudflare Challenge", "🛡️ Cloudflare verification required! Please complete the human verification check first, then click Autofill again.", false);
        return;
    }}

    // React 16+ & modern framework synthetic event trigger
    function setNativeValue(el, val) {{
        if (!el || val === undefined || val === null) return;
        try {{
            const proto = Object.getPrototypeOf(el);
            const desc = Object.getOwnPropertyDescriptor(proto, "value");
            if (desc && desc.set) {{
                desc.set.call(el, val);
            }} else {{
                el.value = val;
            }}
        }} catch(e) {{
            el.value = val;
        }}
        el.dispatchEvent(new Event("input", {{ bubbles: true }}));
        el.dispatchEvent(new Event("change", {{ bubbles: true }}));
        el.dispatchEvent(new Event("blur", {{ bubbles: true }}));
    }}

    function matches(el, patterns) {{
        const text = [
            el.name || "",
            el.id || "",
            el.placeholder || "",
            el.getAttribute("aria-label") || "",
            el.getAttribute("data-automation-id") || "",
            el.getAttribute("autocomplete") || "",
            (el.labels && el.labels.length > 0 ? el.labels[0].innerText : "")
        ].join(" ").toLowerCase();

        return patterns.some(p => text.includes(p.toLowerCase()));
    }}

    function getRoots() {{
        const roots = [document];
        document.querySelectorAll("iframe").forEach(frame => {{
            try {{
                if (frame.contentDocument && frame.contentDocument.body) {{
                    roots.push(frame.contentDocument);
                }}
            }} catch(e) {{}}
        }});
        return roots;
    }}

    let filledCount = 0;
    const roots = getRoots();

    roots.forEach(docRoot => {{
        // 1. Text, Email, Tel, and Textarea inputs
        docRoot.querySelectorAll("input, textarea").forEach(el => {{
            const type = (el.type || "text").toLowerCase();
            if (type === "hidden" || type === "submit" || type === "file" || type === "checkbox" || type === "radio" || type === "button") return;

            // Skip if already filled with custom user content (unless cover letter field)
            if (el.value && el.value.length > 2 && !matches(el, ["cover", "letter"])) return;

            let val = null;
            if (matches(el, ["first_name", "firstname", "first name", "given_name", "given-name", "forename"])) {{
                val = data.firstName;
            }} else if (matches(el, ["last_name", "lastname", "last name", "family_name", "family-name", "surname"])) {{
                val = data.lastName;
            }} else if (matches(el, ["full_name", "fullname", "full name", "applicant_name", "candidate_name", "name"]) && !matches(el, ["company", "school", "user", "file"])) {{
                val = data.fullName;
            }} else if (matches(el, ["email", "e-mail"])) {{
                val = data.email;
            }} else if (matches(el, ["phone", "mobile", "tel", "cell"])) {{
                val = data.phone;
            }} else if (matches(el, ["linkedin"])) {{
                val = data.linkedin;
            }} else if (matches(el, ["github"])) {{
                val = data.github;
            }} else if (matches(el, ["website", "portfolio", "blog", "urls[portfolio]"])) {{
                val = data.portfolio || data.github;
            }} else if (matches(el, ["city", "town"])) {{
                val = data.city;
            }} else if (matches(el, ["address", "street"])) {{
                val = data.address;
            }} else if (matches(el, ["postal", "zip"])) {{
                val = data.postalCode;
            }} else if (matches(el, ["company", "employer", "organization", "org"])) {{
                val = data.currentCompany;
            }} else if (matches(el, ["title", "role", "designation"])) {{
                val = data.currentTitle;
            }} else if (matches(el, ["cover", "letter", "additional_info", "comments", "why us", "note"])) {{
                val = data.coverLetter;
            }}

            if (val) {{
                el.focus();
                setNativeValue(el, val);
                el.style.backgroundColor = "rgb(232, 245, 233)";
                el.style.border = "2px solid rgb(76, 175, 80)";
                filledCount++;
            }}
        }});

        // 2. Dropdown / Select elements
        docRoot.querySelectorAll("select").forEach(el => {{
            if (matches(el, ["country"])) {{
                for (let opt of el.options) {{
                    if (opt.text.toLowerCase().includes(data.country.toLowerCase())) {{
                        el.value = opt.value;
                        el.dispatchEvent(new Event("change", {{ bubbles: true }}));
                        filledCount++;
                        break;
                    }}
                }}
            }} else if (matches(el, ["sponsorship", "visa"])) {{
                for (let opt of el.options) {{
                    if (opt.text.toLowerCase() === "no" || opt.value.toLowerCase() === "no") {{
                        el.value = opt.value;
                        el.dispatchEvent(new Event("change", {{ bubbles: true }}));
                        filledCount++;
                        break;
                    }}
                }}
            }} else if (matches(el, ["authorized", "eligib", "right to work"])) {{
                for (let opt of el.options) {{
                    if (opt.text.toLowerCase() === "yes" || opt.value.toLowerCase() === "yes") {{
                        el.value = opt.value;
                        el.dispatchEvent(new Event("change", {{ bubbles: true }}));
                        filledCount++;
                        break;
                    }}
                }}
            }}
        }});

        // 3. Agreement / Consent checkboxes
        docRoot.querySelectorAll("input[type=checkbox]").forEach(cb => {{
            if (!cb.checked && matches(cb, ["agree", "consent", "terms", "policy", "privacy", "acknowledge"])) {{
                cb.checked = true;
                cb.dispatchEvent(new Event("change", {{ bubbles: true }}));
                filledCount++;
            }}
        }});
    }});

    // 4. Highlight resume upload fields
    const fileInputs = document.querySelectorAll("input[type=file]");
    fileInputs.forEach(fi => {{
        fi.style.outline = "3px dashed rgb(255, 152, 0)";
        fi.style.backgroundColor = "rgb(255, 243, 224)";
    }});

    // 5. Handle case where 0 fields were populated
    if (filledCount === 0) {{
        // Check for common apply buttons
        let applyBtn = null;
        for (const btn of document.querySelectorAll("button, a")) {{
            const t = (btn.innerText || btn.textContent || "").toLowerCase();
            if (t.includes("apply now") || t.includes("aplică acum") || t.includes("apply on company") || t.includes("easy apply") || t.includes("aplică")) {{
                applyBtn = btn;
                break;
            }}
        }}

        if (applyBtn) {{
            applyBtn.click();
            showToast("Opening Form", "⚡ Clicked 'Apply' button to open the application modal. Please click the Autofill bookmarklet again once the form appears!", false);
            return;
        }}
        showToast("No Fields Found", "⚡ 0 form fields found on this page. Make sure you are on the actual application form (click 'Apply' first)!", false);
        return;
    }}

    // 6. Show success toast with quick copy actions
    let msg = "Autofilled <b>" + filledCount + "</b> fields for <b>" + data.fullName + "</b>.";
    if (fileInputs.length > 0 && data.cvPath) {{
        msg += "<br>📂 Resume field highlighted in orange.";
    }}
    showToast("Autofill Success", msg, true);
}})();"""
    return js_code


def generate_bookmarklet_string(js_code: str) -> str:
    """
    Wraps JavaScript code into a one-click URI-safe bookmarklet string.
    Removes comments, collapses whitespace, and wraps cleanly without syntax errors.
    """
    lines = [
        line.strip()
        for line in js_code.split("\n")
        if line.strip() and not line.strip().startswith("//")
    ]
    minified = " ".join(lines)
    if not minified.endswith(";"):
        minified += ";"
    return f"javascript:{minified}"


def save_autofill_assets(
    app_dir: Path,
    profile: CandidateProfile,
    cover_letter: str = "",
    cv_path: str | None = None,
) -> None:
    """Generates and saves autofill JS and bookmarklet in the application folder."""
    app_dir.mkdir(parents=True, exist_ok=True)
    cv_str = cv_path or ""
    if not cv_str:
        cv_pdf = next(app_dir.glob("CV_*.pdf"), None)
        if cv_pdf:
            cv_str = str(cv_pdf)

    js_code = generate_autofill_javascript(profile, cover_letter, cv_str)
    bookmarklet = generate_bookmarklet_string(js_code)

    try:
        with open(app_dir / "autofill.js", "w", encoding="utf-8") as f:
            f.write(js_code)

        with open(app_dir / "autofill_bookmarklet.txt", "w", encoding="utf-8") as f:
            f.write(bookmarklet)
    except Exception as e:
        print(f"Notice: Could not save autofill assets in {app_dir}: {e}")
