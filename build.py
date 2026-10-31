"""Build the landing page.

    python build.py

Reads profile.toml, fetches your public GitHub repos, and writes site/index.html.
Uses only the Python standard library (Python 3.11+).
"""

import json
import tomllib
import urllib.request
from html import escape
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "site" / "index.html"


def fetch_repos(user: str) -> list[dict]:
    """Return the user's public, non-fork repos (newest first). Empty list on failure."""
    url = f"https://api.github.com/users/{user}/repos?per_page=100&sort=updated"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            repos = json.load(resp)
    except Exception as err:  # offline, rate-limited, etc. — build still works
        print(f"note: could not fetch repos ({err}); skipping projects")
        return []
    return [r for r in repos if not r.get("fork") and r["name"].lower() != user.lower()]


def pick_projects(repos: list[dict], cfg: dict) -> list[dict]:
    pinned = cfg.get("pinned") or []
    if pinned:
        by_name = {r["name"].lower(): r for r in repos}
        return [by_name[p.lower()] for p in pinned if p.lower() in by_name]
    return repos[: cfg.get("max", 6)]


def render(p: dict, projects: list[dict]) -> str:
    links = " · ".join(
        f'<a href="{escape(url)}">{escape(label)}</a>' for label, url in p.get("links", {}).items()
    )
    focus = "".join(
        f"<li><b>{escape(f['title'])}</b><span>{escape(f['text'])}</span></li>"
        for f in p.get("focus", [])
    )
    tools = "".join(f"<li>{escape(t)}</li>" for t in p.get("toolkit", {}).get("items", []))
    proj = "".join(
        f'<li><a href="{escape(r["html_url"])}">{escape(r["name"])}</a>'
        f'<span>{escape(r.get("description") or "")}</span>'
        f'<small>{escape(r.get("language") or "")}</small></li>'
        for r in projects
    )

    def section(title: str, body: str) -> str:
        return f"<section><h2>{title}</h2>{body}</section>" if body else ""

    now = p.get("now", "").strip()
    return TEMPLATE.format(
        name=escape(p["name"]),
        role=escape(p.get("role", "")),
        tagline=escape(p.get("tagline", "")),
        about=escape(" ".join(p.get("about", "").split())),
        links=links,
        github=escape(p["github"]),
        now=f'<p class="now"><span>Now</span>{escape(now)}</p>' if now else "",
        sections=(
            section("Focus", f'<ul class="focus">{focus}</ul>' if focus else "")
            + section("Projects", f'<ul class="projects">{proj}</ul>' if proj else "")
            + section("Toolkit", f'<ul class="tools">{tools}</ul>' if tools else "")
        ),
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} · {role}</title>
<meta name="description" content="{tagline}">
<link rel="icon" href="https://github.com/{github}.png">
<style>
  :root {{ --bg:#fafafa; --fg:#18181b; --muted:#71717a; --line:#e4e4e7; --accent:#0d9488; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0b0b0d; --fg:#e4e4e7; --muted:#8b8b94; --line:#26262b; --accent:#2dd4bf; }}
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--fg); font:16px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif; }}
  main {{ max-width:640px; margin:0 auto; padding:72px 20px 56px; }}
  a {{ color:var(--fg); text-decoration-color:var(--line); text-underline-offset:3px; }}
  a:hover {{ color:var(--accent); text-decoration-color:var(--accent); }}
  .mono, h2, small, .tools li, .now span {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  header img {{ width:64px; height:64px; border-radius:50%; margin-bottom:20px; }}
  h1 {{ font-size:28px; font-weight:600; letter-spacing:-.02em; }}
  .role {{ color:var(--accent); font-size:14px; margin-top:2px; }}
  .tagline {{ font-size:19px; margin:20px 0 12px; }}
  .about {{ color:var(--muted); }}
  .links {{ margin-top:16px; font-size:14px; color:var(--muted); }}
  .now {{ margin-top:28px; padding:12px 14px; border:1px solid var(--line); border-radius:8px; font-size:14px; }}
  .now span {{ color:var(--accent); margin-right:10px; font-size:12px; text-transform:uppercase; }}
  section {{ margin-top:48px; }}
  h2 {{ font-size:12px; font-weight:500; text-transform:uppercase; letter-spacing:.1em; color:var(--muted); margin-bottom:14px; }}
  ul {{ list-style:none; }}
  .focus li, .projects li {{ padding:12px 0; border-top:1px solid var(--line); display:grid; gap:2px; }}
  .focus span, .projects span {{ color:var(--muted); font-size:15px; }}
  .projects small {{ color:var(--accent); font-size:12px; }}
  .tools {{ display:flex; flex-wrap:wrap; gap:8px; }}
  .tools li {{ font-size:13px; padding:3px 10px; border:1px solid var(--line); border-radius:999px; }}
  footer {{ margin-top:64px; font-size:13px; color:var(--muted); }}
</style>
</head>
<body>
<main>
  <header>
    <img src="https://github.com/{github}.png" alt="">
    <h1>{name}</h1>
    <p class="role mono">{role}</p>
    <p class="tagline">{tagline}</p>
    <p class="about">{about}</p>
    <p class="links">{links}</p>
    {now}
  </header>
  {sections}
  <footer>Built with Python · <a href="https://github.com/{github}">@{github}</a></footer>
</main>
</body>
</html>
"""


def main() -> None:
    profile = tomllib.loads((ROOT / "profile.toml").read_text(encoding="utf-8"))
    projects = pick_projects(fetch_repos(profile["github"]), profile.get("projects", {}))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(render(profile, projects), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(projects)} projects)")


if __name__ == "__main__":
    main()
