import re

# URL chars only — stops before Khmer/text glued without a space after the link
URL_RE = re.compile(
    r"https?://[a-zA-Z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE,
)

RESERVED = {"names", "summary", "start", "clear", "help", "forward"}


def extract_link_from_line(line: str) -> tuple[str | None, str]:
    """Return (link, remainder of line after the URL)."""
    m = URL_RE.search(line)
    if not m:
        return None, line
    link = m.group(0).rstrip(".,;)")
    rest = (line[: m.start()] + " " + line[m.end() :]).strip()
    return link, rest


def parse_report_message(text: str) -> dict | None:
    """
    Parse a user report message.
    Supports:
        https://...
        /name
        description of work done (e.g. បានដាក់ចេញ 15-20 comment like / report 1000 Accounts)
    Also handles missing space between URL and Khmer/English text on the same line.
    """
    if not text:
        return None

    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]

    link = None
    desc_parts: list[str] = []
    target_name = None

    for line in lines:
        line_link, remainder = extract_link_from_line(line)
        if line_link:
            if not link:
                link = line_link
            if remainder:
                desc_parts.append(remainder)
            continue

        if line.startswith("/"):
            candidate = line[1:].strip().lower()
            if candidate not in RESERVED:
                target_name = line[1:].strip()
            continue

        desc_parts.append(line)

    if not target_name and not link:
        return None

    desc = " ".join(desc_parts)

    if not target_name and not desc.strip():
        return None

    if not target_name:
        target_name = "មិនស្គាល់"

    action = "comment"
    action_detail = ""

    if re.search(r"report", desc, re.IGNORECASE):
        action = "report"
        m = re.search(r"បានដាក់(?:ចេញ)?\s*(.+)", desc, re.IGNORECASE)
        if m:
            action_detail = m.group(1).strip().rstrip("🙏✍️ ").strip()
        else:
            m = re.search(r"report\s+([\d,]+)\s*account", desc, re.IGNORECASE)
            if m:
                action_detail = f"report {m.group(1)} Accounts"
            else:
                m = re.search(r"([\d,]+)\s+report", desc, re.IGNORECASE)
                if m:
                    action_detail = f"{m.group(1)} report"
                else:
                    action_detail = "report"
    else:
        action = "comment"
        m = re.search(r"បានដាក់(?:ចេញ)?\s*(.+)", desc, re.IGNORECASE)
        if m:
            action_detail = m.group(1).strip().rstrip("🙏✍️ ").strip()
        else:
            m = re.search(
                r"([\d][\d\-,]*\s*(?:cm\s*)?(?:comment|like|comment\s*like)?)",
                desc,
                re.IGNORECASE,
            )
            action_detail = m.group(1).strip() if m else ""

    return {
        "target_name": target_name,
        "link": link,
        "action": action,
        "action_detail": action_detail,
    }
