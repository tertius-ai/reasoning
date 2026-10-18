"""Permission-scoped Markdown blog with immutable publication snapshots."""

import functools
import json
import re
from datetime import UTC, datetime
from html import escape
from typing import Annotated
from urllib.parse import urlencode

from fastapi.responses import HTMLResponse, PlainTextResponse
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.texmath import texmath_plugin

from .knowledge_store import KnowledgeStore
from .references import link_references
from .store import StoreError

# Shared by the static reader and the React editor; never interpolate author text.
BLOG_STYLE = """
body[data-research-blog] { margin: 0; background: var(--background, #101112); color: var(--foreground, #ececec); }
body[data-research-blog] { --background: #101112; --surface: #1a1c1e; --surface-2: #222528; --foreground: #ececec;
  --muted: #9a9fa4; --border: rgba(255,255,255,.08); --blue: #83bff3; color-scheme: dark; }
body[data-research-blog] .blog-shell-header { display:flex; align-items:center; gap:12px; height:44px; padding:0 16px;
  border-bottom:1px solid var(--border); background:var(--surface); font:13px system-ui,sans-serif; }
.blog-shell-header a { color:var(--blue); text-decoration:none; }
.blog-shell-header a:last-child { margin-left:auto; }
@media (prefers-color-scheme: light) {
  :root:not([data-theme=dark]) body[data-research-blog] { --background:#f5f6f7; --surface:#fff; --surface-2:#eef0f2;
    --foreground:#20262d; --muted:#58636f; --border:rgba(23,33,43,.12); --blue:#246aab; color-scheme:light; }
}
:root[data-theme=light] body[data-research-blog] { --background:#f5f6f7; --surface:#fff; --surface-2:#eef0f2;
  --foreground:#20262d; --muted:#58636f; --border:rgba(23,33,43,.12); --blue:#246aab; color-scheme:light; }
.research-blog {
  --blog-ink: var(--foreground, #ececec); --blog-muted: var(--muted, #9a9fa4); --blog-line: var(--border, rgba(255,255,255,.08));
  --blog-link: var(--blue, #83bff3); --blog-paper: var(--surface, #1a1c1e);
  box-sizing: border-box; width: min(100%, 880px); margin: 0 auto;
  padding: 24px 32px 40px; background: var(--blog-paper); color: var(--blog-ink);
  font: 14px/1.65 var(--font-sans, system-ui), sans-serif; overflow-wrap: anywhere;
}
.research-blog *, .research-blog *::before, .research-blog *::after { box-sizing: border-box; }
.research-blog a, .org-app .research-blog a { color: var(--blog-link); text-underline-offset: 3px; }
.research-blog a:hover { text-decoration-thickness: 2px; }
.blog-site-nav, .blog-masthead, .blog-toolbar, .blog-actions, .blog-pages, .blog-meta {
  display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px;
}
.blog-site-nav { font-size: 12px; padding-bottom: 14px; border-bottom: 1px solid var(--blog-line); }
.blog-site-nav a:last-child { margin-left: auto; }
.blog-masthead { padding: 18px 0; }
.blog-masthead > div { margin-right: auto; }
.research-blog h1, .research-blog h2 { font: 600 28px/1.25 Georgia, serif; margin: 4px 0 12px; }
.blog-masthead h1, .blog-masthead h2 { margin-bottom: 0; }
.blog-eyebrow { color: var(--blog-muted); font-size: 10px; letter-spacing: .1em; text-transform: uppercase; }
.blog-toolbar { margin: 0 0 8px; padding: 10px 0; border-block: 1px solid var(--blog-line); font-size: 12px; }
.blog-toolbar label { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.blog-entry { padding: 16px 0; border-bottom: 1px solid var(--blog-line); }
.blog-entry h2, .blog-entry h3 { font: 600 19px/1.35 Georgia, serif; margin: 7px 0; }
.blog-entry h2 a, .blog-entry h3 a { text-decoration: none; }
.blog-meta { color: var(--blog-muted); font-size: 11px; gap: 6px 10px; }
.blog-meta time { margin-left: auto; }
.blog-reference-unavailable { color: var(--blog-muted); font-style: italic; }
.blog-tag { padding: 1px 7px; border: 1px solid var(--blog-line); border-radius: 4px; }
.blog-excerpt { color: var(--blog-muted); font-size: 13px; margin: 6px 0; }
.blog-pages { margin-top: 18px; font-size: 12px; }
.research-blog button, .research-blog input, .research-blog select, .research-blog textarea,
.org-app .research-blog button, .org-app .research-blog input, .org-app .research-blog select, .org-app .research-blog textarea {
  border: 1px solid var(--blog-line); border-radius: 6px; background: var(--surface, #1a1c1e);
  color: var(--blog-ink); font: inherit; font-size: 13px; padding: 5px 9px; min-height: 30px;
}
.research-blog button { cursor: pointer; }
.research-blog button:disabled { opacity: .5; cursor: default; }
.research-blog button:hover:not(:disabled), .research-blog button[aria-pressed=true] { background: var(--surface-2, #222528); }
.research-blog :focus-visible { outline: 2px solid var(--blog-link); outline-offset: 2px; }
.blog-toolbar select, .org-app .blog-toolbar select { width: auto; max-width: 100%; }
.blog-editor fieldset { border: 0; margin: 0; padding: 0; min-width: 0; }
.blog-editor label { display: grid; gap: 5px; margin-bottom: 14px; font-size: 12px; }
.blog-editor input, .blog-editor select, .blog-editor textarea { width: 100%; }
.research-blog .blog-editor textarea, .org-app .research-blog .blog-editor textarea {
  min-height: 260px; resize: vertical; font-family: ui-monospace, monospace; line-height: 1.6;
}
.blog-article { margin: 22px 0; }
.blog-article > h1 { margin: 12px 0; }
.blog-article .blog-meta { margin-bottom: 24px; padding-bottom: 12px; border-bottom: 1px solid var(--blog-line); }
.blog-article h2 { font-size: 22px; margin-top: 28px; }
.research-blog pre { padding: 12px; background: var(--surface-2, #222528); border: 1px solid var(--blog-line);
  border-radius: 6px; overflow: auto; }
.research-blog code { font: .9em ui-monospace, monospace; }
.research-blog table { border-collapse: collapse; display: block; overflow: auto; }
.research-blog td, .research-blog th { border: 1px solid var(--blog-line); padding: 5px 9px; }
.research-blog blockquote { border-left: 2px solid var(--blog-line); margin-left: 0; padding-left: 16px; color: var(--blog-muted); }
.research-blog img { max-width: 100%; }
.research-blog .katex-display { overflow-x: auto; overflow-y: hidden; padding: 4px 0; }
.research-blog .org-error { color: #a03232; }
@media (max-width: 600px) {
  .research-blog { padding: 16px; }
  .blog-toolbar label { margin-left: 0; width: 100%; }
  .blog-toolbar select { flex: 1; min-width: 0; }
  .blog-meta time { margin-left: 0; }
}
"""


def post_metadata(value):
    date = datetime.fromtimestamp(value["revised_at"], UTC).date().isoformat()
    return (f"<div class='blog-meta'><span class='blog-tag'>{escape(value['project_name'] or 'Organization')}</span>"
            f"<span>{escape(value['author_name'])}</span><time datetime='{date}'>{date}</time></div>")


def blog_path(org, post=None):
    return f"/blog/{org}" + (f"/{post}" if post else "")


def published_posts(store, user, org, *, before=None, project=None):
    with store.transaction() as db:
        role = store._require(db, user, org)
        if project:
            store._project_access(db, user, org, project)
        # Filter permissions before selecting published versions; drafts never
        # displace the public snapshot of an already-published entry.
        entries = KnowledgeStore._listing(db, org, kind="journal", user=user, role=role, before=before,
                                         limit=50, published_only=True, project_id=project)
        result = []
        for entry in entries:
            entry["project_name"] = db.execute("SELECT name FROM projects WHERE organization_id=? AND id=?",
                (org, entry["project_id"])).fetchone()[0] if entry["project_id"] else None
            result.append(entry)
        return {"posts": result, "next_before": f"{entries[-1]['revised_at']}:{entries[-1]['id']}" if len(entries) == 50 else None}


def published_post(store, user, org, post):
    with store.transaction() as db:
        store._require(db, user, org)
        current = KnowledgeStore._view(db, org, post)
        if current["topic_id"] is not None:
            store._topic_access(db, user, org, current["topic_id"])
        elif current["project_id"] is not None:
            store._project_access(db, user, org, current["project_id"])
        revision = db.execute("SELECT revision FROM blog_publications WHERE organization_id=? AND entry_id=?", (org, post)).fetchone()
        if not revision:
            raise StoreError(404, "published post not found")
        value = KnowledgeStore._view(db, org, post, revision[0])
        value["project_name"] = db.execute("SELECT name FROM projects WHERE organization_id=? AND id=?",
            (org, value["project_id"])).fetchone()[0] if value["project_id"] else None
        return value


# Delimiters that the browser math enhancement renders in plain text such as titles.
MATH_DELIMITERS = re.compile(r"\$|\\\(|\\\[")


def has_math(tokens):
    """Whether parsed Markdown contains math (dollar or bracket delimiters), ignoring code."""
    return any(token.type.startswith("math") or (token.children and has_math(token.children)) for token in tokens)


def blog_assets(frontend):
    """(theme, math) tags for the static blog. A release's assets never change in place; a rebuilt development
    directory changes its modification time."""
    return _blog_assets(frontend, (frontend / "assets").stat().st_mtime_ns)


@functools.lru_cache(maxsize=8)
def _blog_assets(frontend, _assets_version):
    theme = "".join(f"<script type='module' src='/assets/{escape(path.name)}'></script>"
                    for path in sorted((frontend / "assets").glob("blogTheme-*.js")))
    assets = ""
    if frontend:
        # Vite may extract KaTeX CSS into a shared dependency rather than a
        # chunk named after the math module. Follow the generated asset graph.
        manifest_path = frontend / ".vite" / "manifest.json"
        styles = set()
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            seen = set()

            def collect(key):
                if key in seen:
                    return
                seen.add(key)
                item = manifest.get(key, {})
                styles.update(item.get("css", []))
                for dependency in item.get("imports", []):
                    collect(dependency)

            collect("src/blogMathBootstrap.ts")
        else:
            # A compatible release may have no manifest. Select the actual
            # stylesheet carrying KaTeX's accessible/visual layout contract.
            styles.update("assets/" + path.name for path in (frontend / "assets").glob("*.css")
                          if ".katex-mathml" in path.read_text())
        for path in sorted(styles):
            assets += f"<link rel='stylesheet' href='/{escape(path, quote=True)}'>"
        for path in sorted((frontend / "assets").glob("blogMathBootstrap-*.js")):
            assets += f"<script type='module' src='/assets/{escape(path.name)}'></script>"
    return theme, assets


def page(title, content, org, frontend=None, *, math=False):
    """Server-rendered blog page; KaTeX enhancement is included only when the page shows math."""
    assets = ""
    if frontend:
        theme, enhancement = blog_assets(frontend)
        assets = theme + (enhancement if math else "")
    # MarkdownIt rejects dangerous links, raw HTML is disabled, and no author
    # content is inserted into a script or style. Static reading needs no JS.
    return HTMLResponse("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><link rel='stylesheet' href='/blog.css'>{assets}</head><body data-research-blog>"
        f"<header class='blog-shell-header'><a href='/organization.html?{escape(urlencode({'organization': org}), quote=True)}'>"
        "Reasoning</a><span>Research blog</span>"
        f"<a href='/organization.html?{escape(urlencode({'organization': org, 'view': 'journal'}), quote=True)}'>Edit posts</a></header>"
        f"<main class='research-blog'><nav class='blog-site-nav' aria-label='Research navigation'>"
        f"<a href='/organization.html?{escape(urlencode({'organization': org}), quote=True)}'>Chat</a>"
        f"<a href='{blog_path(org)}'>Research blog</a>"
        "</nav>"
        f"{content}</main></body></html>")


def install_routes(app, store, authenticated, *, frontend=None):
    from fastapi import Depends
    from pydantic import BaseModel, ConfigDict, StrictInt

    class DeletePost(BaseModel):
        model_config = ConfigDict(extra="forbid")
        client_id: str
        expected_revision: StrictInt

    User = Annotated[str, Depends(authenticated)]

    @app.delete("/api/organizations/{organization_id}/blog/{post_id}")
    def delete(organization_id: str, post_id: str, body: DeletePost, user: User):
        return KnowledgeStore(store).delete_post(user, organization_id, post_id, **body.model_dump())

    @app.get("/api/organizations/{organization_id}/blog")
    def listing(organization_id: str, user: User, before: str | None = None, project: str | None = None):
        return published_posts(store, user, organization_id, before=before, project=project)

    @app.get("/blog.css", include_in_schema=False)
    def style():
        return PlainTextResponse(BLOG_STYLE, media_type="text/css")

    @app.get("/blog/{organization_id}", include_in_schema=False)
    def index(organization_id: str, user: User, before: str | None = None, project: str | None = None):
        data = published_posts(store, user, organization_id, before=before, project=project)
        projects = sorted(store.visible_projects(user, organization_id), key=lambda value: value["name"].casefold())
        options = "<option value=''>All projects</option>" + "".join(
            f"<option value='{escape(value['id'], quote=True)}'{' selected' if value['id'] == project else ''}>"
            f"{escape(value['name'])}</option>" for value in projects)
        content = ("<header class='blog-masthead'><div><span class='blog-eyebrow'>Organization research</span>"
                   "<h1>Research blog</h1></div></header>"
                   f"<form class='blog-toolbar' method='get'><label>Project<select name='project'>{options}</select></label>"
                   "<button type='submit'>Filter</button></form>") + "".join(
            f"<article class='blog-entry'>{post_metadata(post)}"
            f"<h2><a href='{blog_path(organization_id, post['id'])}'>{escape(post['title'])}</a></h2></article>"
            for post in data["posts"])
        # The index stays compact: full Markdown is rendered on the article page.
        if not data["posts"]:
            content += "<p>No published posts.</p>"
        newest = escape(urlencode({'project': project or ''}), quote=True)
        if data["next_before"]:
            query = escape(urlencode({'before': data['next_before'], 'project': project or ''}), quote=True)
            content += f"<nav class='blog-pages' aria-label='Blog pages'><a href='?{newest}'>Newest</a>"
            content += f"<a href='?{query}'>Older posts</a></nav>"
        elif before:
            content += f"<nav class='blog-pages' aria-label='Blog pages'><a href='?{newest}'>Newest</a></nav>"
        # The index renders titles only; it needs math enhancement only for a title with TeX delimiters.
        math = any(MATH_DELIMITERS.search(post["title"]) for post in data["posts"])
        return page("Research blog", content, organization_id, frontend, math=math)

    @app.get("/blog/{organization_id}/{post_id}.md", include_in_schema=False)
    def source(organization_id: str, post_id: str, user: User):
        value = published_post(store, user, organization_id, post_id)
        return PlainTextResponse(value["body"], media_type="text/markdown")

    @app.get("/blog/{organization_id}/{post_id}", include_in_schema=False)
    def post(organization_id: str, post_id: str, user: User):
        value = published_post(store, user, organization_id, post_id)
        renderer = MarkdownIt("commonmark", {"html": False}).enable("table")
        renderer.use(texmath_plugin, delimiters="brackets")
        renderer.use(dollarmath_plugin, allow_labels=False, double_inline=True,
            renderer=lambda text, options: escape(("$$" if options["display_mode"] else "$") + text
                                                  + ("$$" if options["display_mode"] else "$")))
        # External images are not fetched automatically from private research.
        renderer.renderer.rules["image"] = lambda tokens, index, *_: escape(tokens[index].content)
        content = f"<article class='blog-article'><h1>{escape(value['title'])}</h1>{post_metadata(value)}"
        tokens = renderer.parse(value["body"])
        link_references(store, user, organization_id, tokens)
        content += renderer.renderer.render(tokens, renderer.options, {})
        content += f"</article><nav class='blog-pages'><a href='{blog_path(organization_id, post_id)}.md'>Markdown source</a></nav>"
        math = has_math(tokens) or MATH_DELIMITERS.search(value["title"]) is not None
        return page(value["title"], content, organization_id, frontend, math=math)
