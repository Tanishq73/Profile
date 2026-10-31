# Profile landing page

A minimal, Python-built landing page focused on AIOps.

## Edit

All content lives in **`profile.toml`** — name, tagline, links, focus areas, tools.
You don't need to touch any HTML.

## Preview locally

```bash
python build.py                              # writes site/index.html
python -m http.server -d site 8000           # open http://localhost:8000
```

Needs Python 3.11+, no packages to install.

## Publish (one-time setup)

1. On GitHub, open this repo → **Settings → Pages**.
2. Under **Build and deployment → Source**, pick **GitHub Actions**.
3. Push any change. The page goes live at `https://tanishq73.github.io/Profile/`.

It also rebuilds weekly, so new public repos show up under *Projects* automatically.
