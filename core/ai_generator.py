"""core/ai_generator.py — Together.ai-powered email template generator"""

import re
import requests

_DEFAULT_MODEL = "mistralai/Mixtral-8x7B-Instruct-v0.1"
_API_ENDPOINT = "https://api.together.xyz/v1/chat/completions"

_CATEGORY_HINTS = {
    "business": "professional B2B business email, formal tone, corporate context",
    "personal": "friendly personal email, casual tone, conversational style",
    "security": "account security notification, urgent tone, action required",
    "marketing": "promotional marketing email, persuasive tone, clear CTA",
    "finance": "financial services email, trusted tone, specific numbers",
    "ecommerce": "e-commerce order/shipping email, helpful tone, product focus",
}

_PROMPT_TEMPLATE = """\
You are an expert email copywriter. Create a complete, professional HTML email template.

Theme/Prompt: {theme}
Category: {category_hint}

Requirements:
1. Write a complete, visually polished HTML email (<!DOCTYPE html> ... </html>) with inline CSS and responsive design.
2. Use these SynthTel personalization tags where appropriate:
   - #EMAIL — recipient email address
   - #FIRSTNAME — recipient first name
   - #COMPANY — recipient company name
   - #DATE — today's date
   - #EXPIRES_DATE — expiration date (3 days from now)
   - #DEADLINE_DATE — deadline date (7 days from now)
   - #INVOICE_NUM — invoice/reference number
   - #VERIFICATION_CODE — 6-digit code
   - #RAND1 — random 5-digit number
   - #WORDSNUM1 — random 20-char alphanumeric token
   - #LINK — main action/CTA link
   - #FROMNAME — sender name
   - #FROMDOMAIN — sender domain
   - #DOMAIN_LOGO_URL — recipient company logo URL
3. After the HTML, output exactly 5 subject lines under the header "SUBJECT LINES:".
4. After subject lines, output exactly 5 sender names under the header "SENDER NAMES:".

Format your response EXACTLY like this:
[HTML_START]
<!DOCTYPE html>
...complete HTML here...
</html>
[HTML_END]

SUBJECT LINES:
Subject line 1
Subject line 2
Subject line 3
Subject line 4
Subject line 5

SENDER NAMES:
Sender Name 1
Sender Name 2
Sender Name 3
Sender Name 4
Sender Name 5
"""


def generate_template(
    api_key: str, theme: str, category: str = "business", model: str = _DEFAULT_MODEL
) -> dict:
    """
    Call Together.ai to generate an HTML email template + subject lines + sender names.

    Returns:
        {"html": str, "subjects": [str, ...], "from_names": [str, ...]}
    Raises:
        ValueError on API error or unparseable response.
    """
    category_hint = _CATEGORY_HINTS.get(category, _CATEGORY_HINTS["business"])
    prompt = _PROMPT_TEMPLATE.format(theme=theme, category_hint=category_hint)

    try:
        resp = requests.post(
            _API_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "top_p": 0.7,
                "max_tokens": 3000,
            },
            timeout=90,
        )
        resp.raise_for_status()
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:
            pass
        raise ValueError(f"Together.ai HTTP {e.response.status_code}: {body}") from e
    except requests.RequestException as e:
        raise ValueError(f"Together.ai request failed: {e}") from e

    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise ValueError(f"Unexpected Together.ai response shape: {e}") from e

    return _parse_response(content)


def _parse_response(text: str) -> dict:
    """Extract HTML, subjects, and from_names from the AI response."""
    html = ""
    html_match = re.search(r"\[HTML_START\]\s*(.*?)\s*\[HTML_END\]", text, re.DOTALL)
    if html_match:
        html = html_match.group(1).strip()
    else:
        raw_match = re.search(
            r"(<!DOCTYPE html.*?</html>)", text, re.DOTALL | re.IGNORECASE
        )
        if raw_match:
            html = raw_match.group(1).strip()

    subjects = []
    subj_match = re.search(
        r"SUBJECT LINES?:\s*\n(.*?)(?:\n\n|\nSENDER|$)", text, re.DOTALL | re.IGNORECASE
    )
    if subj_match:
        lines = [
            line.strip().lstrip("0123456789.-) ") for line in subj_match.group(1).splitlines()
        ]
        subjects = [line for line in lines if line][:5]

    from_names = []
    names_match = re.search(
        r"SENDER NAMES?:\s*\n(.*?)(?:\n\n|$)", text, re.DOTALL | re.IGNORECASE
    )
    if names_match:
        lines = [
            line.strip().lstrip("0123456789.-) ")
            for line in names_match.group(1).splitlines()
        ]
        from_names = [line for line in lines if line][:5]

    if not html:
        raise ValueError(
            "AI response did not contain recognizable HTML or subject lines"
        )

    return {
        "html": html,
        "subjects": subjects,
        "from_names": from_names,
    }
