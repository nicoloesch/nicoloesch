#!/usr/bin/env python3
"""Build GitHub Pages site from a Reactive Resume v6 master CV export.

The input may be an original single-resume JSON object or a filtered
cv-public.json created with --export-public. The website never renders
email, phone, account data, or job applications.

Usage:
    python build.py --input cv.json --export-public cv-public.json
    python build.py --input cv-public.json
    python -m http.server 8000 --directory web

Requirements: pip install jinja2
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parent


class RichTextSanitizer(HTMLParser):
    """Allow only simple formatting from Reactive Resume rich text."""
    TAGS = {"p", "ul", "ol", "li", "strong", "em", "b", "i", "br"}
    VOID = {"br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.result: list[str] = []
        self.open_tags: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe"}:
            self.suppressed += 1
            return
        if self.suppressed:
            return
        if tag in self.TAGS:
            self.result.append(f"<{tag}>")
            if tag not in self.VOID:
                self.open_tags.append(tag)
        elif tag == "a":
            attrs_dict = dict(attrs)
            url = valid_url(attrs_dict.get("href", ""))
            if url:
                self.result.append(f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">')
                self.open_tags.append("a")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "iframe"}:
            self.suppressed = max(0, self.suppressed - 1)
            return
        if self.suppressed:
            return
        if tag in self.open_tags:
            # Close any nested tags in well-formed order.
            while self.open_tags:
                top = self.open_tags.pop()
                self.result.append(f"</{top}>")
                if top == tag:
                    break

    def handle_data(self, data):
        if not self.suppressed:
            self.result.append(html.escape(data))

    def rendered(self):
        while self.open_tags:
            self.result.append(f"</{self.open_tags.pop()}>")
        return "".join(self.result)


def rich_text(content: str | None) -> str:
    parser = RichTextSanitizer()
    parser.feed(content or "")
    return parser.rendered()


def valid_url(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    try:
        parts = urlparse(value)
    except ValueError:
        return ""
    return value if parts.scheme == "https" and parts.netloc else ""


def visible_items(section: dict | None) -> list[dict]:
    if not isinstance(section, dict) or section.get("hidden"):
        return []
    return [item for item in section.get("items", []) if isinstance(item, dict) and not item.get("hidden")]


def select_public_cv(raw: dict) -> dict:
    """Strict allowlist: safe subset of the source schema for the public Git repo."""
    if "data" in raw and isinstance(raw["data"], dict):
        raw = raw["data"]
    basics = raw.get("basics", {})
    if not isinstance(basics, dict):
        basics = {}

    fields = []
    for field in basics.get("customFields", []):
        if not isinstance(field, dict):
            continue
        label = (field.get("text") or "").strip()
        url = valid_url(field.get("link"))
        if label.lower() in {"github", "linkedin"} and url:
            fields.append({"text": label, "link": url})

    public: dict = {
        "basics": {
            "name": basics.get("name", ""),
            "headline": basics.get("headline", ""),
            "location": basics.get("location", ""),
            "customFields": fields,
        },
        "summary": {"content": (raw.get("summary") or {}).get("content", "") if not (raw.get("summary") or {}).get("hidden") else ""},
        "sections": {},
        "customSections": [],
    }

    allowed = {
        "experience": ("company", "position", "location", "period", "description"),
        "education": ("school", "degree", "area", "grade", "location", "period", "description"),
        "projects": ("name", "period", "description", "website"),
        "publications": ("title", "publisher", "date", "description", "website"),
    }
    sections = raw.get("sections") or {}
    for name, fields_to_keep in allowed.items():
        section = sections.get(name) or {}
        public["sections"][name] = {
            "items": [
                {k: item[k] for k in fields_to_keep if k in item and k != "website"}
                | ({"website": {"url": valid_url((item.get("website") or {}).get("url"))}} if "website" in fields_to_keep else {})
                for item in visible_items(section)
            ]
        }

    for section in raw.get("customSections", []):
        if section.get("hidden") or section.get("type") != "skills" or section.get("title", "").lower() != "skills":
            continue
        public["customSections"].append({
            "title": "Skills",
            "type": "skills",
            "items": [
                {"name": item.get("name", ""), "keywords": [str(x) for x in item.get("keywords", []) if isinstance(x, str)]}
                for item in visible_items(section)
            ],
        })
    return public


def view_model(data: dict) -> dict:
    basics = data.get("basics", {})
    contact = [{"text": f["text"], "url": f["link"]} for f in basics.get("customFields", []) if f.get("link")]
    personal = {
        "name": basics.get("name", ""),
        "title": basics.get("headline", ""),
        "address": basics.get("location", ""),
        "summary_html": rich_text(data.get("summary", {}).get("content", "")),
        "contacts": contact,
    }
    sections = data.get("sections", {})

    # Combine consecutive roles for the same employer, as the old Jinja template did.
    work = []
    for entry in visible_items(sections.get("experience")):
        company = entry.get("company", "")
        location = entry.get("location", "")
        if not work or work[-1]["employer"] != company or work[-1]["location"] != location:
            work.append({"employer": company, "location": location, "roles": []})
        work[-1]["roles"].append({
            "title": entry.get("position", ""),
            "period": entry.get("period", ""),
            "description_html": rich_text(entry.get("description", "")),
        })

    education = [
        {
            "institution": e.get("school", ""),
            "location": e.get("location", ""),
            "degree": " ".join(filter(None, [e.get("degree", ""), e.get("area", "")])),
            "grade": e.get("grade", ""),
            "period": e.get("period", ""),
            "description_html": rich_text(e.get("description", "")),
        }
        for e in visible_items(sections.get("education"))
    ]

    skills = [
        {"category": item.get("name", ""), "description": ", ".join(item.get("keywords", []))}
        for section in data.get("customSections", [])
        if section.get("title", "").lower() == "skills" and section.get("type") == "skills"
        for item in visible_items(section)
    ]

    publications = [
        {
            "title": p.get("title", ""),
            "date": p.get("date", ""),
            "year": (p.get("date", "") or "")[-4:] if (p.get("date") or "")[-4:].isdigit() else (p.get("date", "") or "")[:4],
            "url": valid_url((p.get("website") or {}).get("url")),
            "citation_html": rich_text(p.get("description", "")),
            "publisher": p.get("publisher", ""),
        }
        for p in visible_items(sections.get("publications"))
    ]

    projects = [
        {
            "name": p.get("name", ""),
            "description_html": rich_text(p.get("description", "")),
            "url": valid_url((p.get("website") or {}).get("url")),
            "period": p.get("period", ""),
        }
        for p in visible_items(sections.get("projects"))
    ]
    return dict(personal=personal, work=work, education=education, skills=skills, publications=publications, projects=projects)


def render(data: dict) -> Path:
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "templates")),
        autoescape=True,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    output_dir = ROOT / "web"
    output_dir.mkdir(exist_ok=True)
    rendered = env.get_template("index.html.j2").render(**view_model(data))
    index = output_dir / "index.html"
    index.write_text(rendered, encoding="utf-8")
    assets_dir = ROOT / "assets"
    if assets_dir.exists():
        shutil.copytree(
            assets_dir,
            output_dir / "assets",
            dirs_exist_ok=True,
        )
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the static website from Reactive Resume JSON")
    parser.add_argument("--input", type=Path, default=None, help="Single master-CV JSON")
    parser.add_argument("--export-public", type=Path, help="Write an allowlisted public JSON snapshot")
    args = parser.parse_args()
    source = args.input or (ROOT / ("cv-public.json" if (ROOT / "cv-public.json").exists() else "cv.json"))
    if not source.is_absolute():
        source = ROOT / source
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {source}: {exc}. Replace redaction markers with null.") from exc
    public = select_public_cv(raw)
    if args.export_public:
        dest = args.export_public if args.export_public.is_absolute() else ROOT / args.export_public
        dest.write_text(json.dumps(public, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Created privacy-filtered {dest.name}")
    index = render(public)
    print(f"Built {index.relative_to(ROOT)} from {source.name} (no email or phone).")


if __name__ == "__main__":
    main()
