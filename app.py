import json
import os
import re
import uuid
from datetime import datetime, timedelta
from html import unescape

from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from markupsafe import escape
from requests.exceptions import RequestException
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from extensions import db, login_manager
from models import Annotation, Document, User, UploadedPaper
from services.openalex import (
    get_author_profile,
    get_paper,
    get_trending_papers,
    normalize_author_id,
    search_authors,
    search_papers,
)

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Home page feed: a few fixed sub-fields, titled casually rather than as
# bare academic keywords. Categories/copy are a first guess — worth
# revisiting once we can see what people actually engage with.
FEED_CATEGORIES = [
    {"query": "agricultural economics", "title": "Farmonomics 101"},
    {"query": "food science", "title": "What's Cooking in Food Science"},
    {"query": "agricultural technology", "title": "Smart Farms, Cool Tech"},
    {"query": "sustainable agriculture", "title": "Growing It Green"},
]

# Every visit to "/" would otherwise fire 4 fresh OpenAlex queries — for
# a page every visitor sees, that's needless load and an easy way to get
# rate-limited. Cache each category's results in-process for a while;
# results are the same for everyone, so there's nothing user-specific to
# invalidate on.
FEED_CACHE_TTL = timedelta(minutes=30)
_feed_cache = {}

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads", "papers")
os.makedirs(UPLOAD_DIR, exist_ok=True)

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY environment variable is not set. Copy .env.example to "
        ".env and fill in a real value locally, or set it in your host's "
        "environment variable settings in production."
    )

# Defaults to a local SQLite file for dev. Set DATABASE_URL in production
# to point at a persistent database instead — see .env.example for why
# this matters on hosts with an ephemeral filesystem (e.g. Render's free
# tier), where the SQLite file would otherwise be wiped on every deploy.
DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{os.path.join(BASE_DIR, 'vo_agri.db')}"
if DATABASE_URL.startswith("postgres://"):
    # SQLAlchemy 1.4+ requires the "postgresql://" scheme, but some hosts
    # (Render, Heroku) hand out connection strings using the old "postgres://".
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE

db.init_app(app)
login_manager.init_app(app)

with app.app_context():
    db.create_all()


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_error):
    flash("That file is too large. Maximum size is 10MB.", "error")
    return redirect(url_for("profile_edit"))


def _has_pdf_extension(filename):
    return filename.lower().endswith(".pdf")


def _looks_like_pdf(file_storage):
    header = file_storage.stream.read(5)
    file_storage.stream.seek(0)
    return header == b"%PDF-"


def _merge_author_results(external_authors, local_users):
    """Combine OpenAlex authors with vo.agri accounts matched by name.

    Local accounts are listed first (and always shown, even if OpenAlex
    hasn't indexed them yet or they haven't linked an OpenAlex id). When a
    local account IS linked to one of the external results, that external
    entry is folded into the local one instead of appearing twice, with
    the local account's own name/institution taking priority.
    """
    external_by_id = {a["id"]: a for a in external_authors if a["id"]}

    merged = []
    for user in local_users:
        match = external_by_id.get(user.openalex_author_id)
        merged.append({
            "id": user.openalex_author_id,
            "name": user.name,
            "affiliation": user.institution or (match["affiliation"] if match else "Unknown"),
            "works_count": match["works_count"] if match else None,
            "uploaded_count": len(user.uploaded_papers),
            "registered": True,
        })

    linked_ids = {user.openalex_author_id for user in local_users if user.openalex_author_id}
    for author in external_authors:
        if author["id"] in linked_ids:
            continue
        merged.append({**author, "uploaded_count": None, "registered": False})

    return merged


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def _current_document():
    """The document the "Add to Document" button on search cards targets.

    Tracked in the session as whichever document the user most recently
    opened or created, so it survives across searches without the user
    having to re-pick a target document for every citation.
    """
    if not current_user.is_authenticated:
        return None

    doc_id = session.get("current_document_id")
    if not doc_id:
        return None

    doc = db.session.get(Document, doc_id)
    if doc is None or doc.user_id != current_user.id:
        session.pop("current_document_id", None)
        return None

    return doc


CITATION_ID_RE = re.compile(r'data-paper-id="([^"]*)"')


def _existing_citation_ids(doc):
    """Which papers (by DOI, falling back to OpenAlex work id) are already
    cited in this document, read back out of the saved body_html."""
    if not doc or not doc.body_html:
        return set()
    return {unescape(raw_id) for raw_id in CITATION_ID_RE.findall(doc.body_html)}


def _annotate_already_added(papers, current_document):
    existing_citation_ids = _existing_citation_ids(current_document)
    for paper in papers:
        identifier = paper.get("doi") or paper.get("id")
        paper["already_added"] = bool(identifier) and identifier in existing_citation_ids


def _cached_trending_papers(query):
    now = datetime.utcnow()
    cached = _feed_cache.get(query)
    if cached and now - cached[0] < FEED_CACHE_TTL:
        return cached[1]

    try:
        papers = get_trending_papers(query)
    except RequestException:
        # Stale results beat none — keep showing the last good fetch
        # rather than a blank section until the next successful refresh.
        return cached[1] if cached else []

    _feed_cache[query] = (now, papers)
    return papers


def _build_feed(current_document):
    feed = []
    for category in FEED_CATEGORIES:
        # _annotate_already_added mutates each paper dict, and the cache
        # is shared across every visitor — copy before annotating so one
        # user's "already added" state can't leak into the shared cache
        # and bleed into everyone else's view of the feed.
        papers = [dict(p) for p in _cached_trending_papers(category["query"])]
        _annotate_already_added(papers, current_document)
        feed.append({"title": category["title"], "papers": papers})
    return feed


def _citation_block_html(title, authors, year, journal, doi, paper_id):
    # escape() returns a Markup, whose `+=` auto-escapes *anything* appended
    # to it — including our own literal `<em>`/`<p>` tags. Cast each escaped
    # piece back to plain str before splicing it into the surrounding markup
    # so the literal tags survive.
    safe_title = str(escape(title or "Untitled paper"))
    citation_line = str(escape(authors or "Unknown authors"))
    if year:
        citation_line += f" ({str(escape(year))})."
    if journal:
        citation_line += f" <em>{str(escape(journal))}</em>."

    link_html = ""
    if doi:
        safe_doi = str(escape(doi))
        link_html = f'<p><a href="{safe_doi}" target="_blank">{safe_doi}</a></p>'

    identifier = doi or paper_id
    id_attr = f' data-paper-id="{str(escape(identifier))}"' if identifier else ""

    return (
        f'<blockquote class="citation-block"{id_attr}>'
        '<button type="button" class="citation-remove" contenteditable="false" title="Remove citation">&times;</button>'
        f"<p><strong>{safe_title}</strong></p>"
        f"<p>{citation_line}</p>"
        f"{link_html}"
        "</blockquote>"
        '<p class="citation-note">Notes: </p>'
    )


@app.route("/")
def home():
    current_document = _current_document()
    return render_template(
        "index.html",
        query="",
        search_type="paper",
        papers=[],
        authors=[],
        error=None,
        current_document=current_document,
        feed=_build_feed(current_document),
    )


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    search_type = request.args.get("type", "paper")
    if search_type not in ("paper", "author"):
        search_type = "paper"

    papers = []
    authors = []
    error = None

    if query:
        if search_type == "author":
            external_authors = []
            try:
                external_authors = search_authors(query)
            except RequestException:
                error = "Could not reach OpenAlex right now. Showing results from vo.agri only."

            local_users = User.query.filter(User.name.ilike(f"%{query}%")).order_by(User.name).all()
            authors = _merge_author_results(external_authors, local_users)
        else:
            try:
                papers = search_papers(query)
            except RequestException:
                error = "Could not reach OpenAlex right now. Please try again."

    current_document = _current_document()
    _annotate_already_added(papers, current_document)

    return render_template(
        "index.html",
        query=query,
        search_type=search_type,
        papers=papers,
        authors=authors,
        error=error,
        current_document=current_document,
        feed=[],
    )


@app.route("/author/<author_id>")
def author_profile(author_id):
    author = None
    error = None

    try:
        author = get_author_profile(author_id)
    except RequestException:
        error = "Could not reach OpenAlex right now. Please try again."

    # A professor may have registered and linked their OpenAlex id to this
    # page, in which case we also show what they've uploaded themselves,
    # kept in a separate section from the OpenAlex-fetched list.
    professor = User.query.filter_by(openalex_author_id=author_id).first()
    uploaded_papers = professor.uploaded_papers if professor else []

    return render_template(
        "author.html",
        author=author,
        error=error,
        professor=professor,
        uploaded_papers=uploaded_papers,
    )


@app.route("/paper/<work_id>")
def paper_detail(work_id):
    paper = None
    error = None

    try:
        paper = get_paper(work_id)
    except RequestException:
        error = "Could not reach OpenAlex right now. Please try again."

    current_document = _current_document()
    if paper:
        _annotate_already_added([paper], current_document)

    return render_template(
        "paper.html",
        paper=paper,
        error=error,
        current_document=current_document,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("profile_edit"))

    form_data = {"email": "", "name": "", "institution": "", "openalex_author_id": ""}

    if request.method == "POST":
        form_data["email"] = request.form.get("email", "").strip()
        form_data["name"] = request.form.get("name", "").strip()
        form_data["institution"] = request.form.get("institution", "").strip()
        form_data["openalex_author_id"] = request.form.get("openalex_author_id", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        error = None
        if not form_data["email"] or not EMAIL_RE.match(form_data["email"]):
            error = "Please enter a valid email address."
        elif not form_data["name"]:
            error = "Please enter your name."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm_password:
            error = "Passwords do not match."
        elif User.query.filter_by(email=form_data["email"]).first():
            error = "An account with that email already exists."

        if error:
            flash(error, "error")
            return render_template("register.html", form=form_data)

        user = User(
            email=form_data["email"],
            name=form_data["name"],
            institution=form_data["institution"] or None,
            openalex_author_id=normalize_author_id(form_data["openalex_author_id"]),
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        flash("Welcome! Your account has been created.", "success")
        return redirect(url_for("profile_edit"))

    return render_template("register.html", form=form_data)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("profile_edit"))

    email = ""

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()
        if user is None or not user.check_password(password):
            flash("Invalid email or password.", "error")
            return render_template("login.html", email=email)

        login_user(user)

        next_url = request.args.get("next")
        if next_url and next_url.startswith("/"):
            return redirect(next_url)
        return redirect(url_for("profile_edit"))

    return render_template("login.html", email=email)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You've been logged out.", "success")
    return redirect(url_for("home"))


@app.route("/profile/edit", methods=["GET", "POST"])
@login_required
def profile_edit():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        institution = request.form.get("institution", "").strip()
        openalex_author_id = request.form.get("openalex_author_id", "").strip()

        if not name:
            flash("Name is required.", "error")
        else:
            current_user.name = name
            current_user.institution = institution or None
            current_user.openalex_author_id = normalize_author_id(openalex_author_id)
            db.session.commit()
            flash("Profile updated.", "success")

        return redirect(url_for("profile_edit"))

    return render_template("profile_edit.html")


@app.route("/profile/papers", methods=["POST"])
@login_required
def upload_paper():
    title = request.form.get("title", "").strip()
    abstract = request.form.get("abstract", "").strip()
    journal = request.form.get("journal", "").strip()
    year_raw = request.form.get("year", "").strip()
    external_url = request.form.get("external_url", "").strip()
    pdf_file = request.files.get("pdf_file")
    confirmed_original = request.form.get("confirm_original") == "on"

    has_file = pdf_file is not None and pdf_file.filename != ""
    has_url = bool(external_url)

    error = None
    if not title:
        error = "Title is required."
    elif year_raw and not year_raw.isdigit():
        error = "Year must be a number."
    elif has_file and has_url:
        error = "Provide either a PDF file or an external link, not both."
    elif not has_file and not has_url:
        error = "Provide either a PDF file or an external link."
    elif has_url and not (external_url.startswith("http://") or external_url.startswith("https://")):
        error = "External link must start with http:// or https://."
    elif has_file and not _has_pdf_extension(pdf_file.filename):
        error = "Only PDF files are allowed."
    elif has_file and not _looks_like_pdf(pdf_file):
        error = "That file doesn't look like a valid PDF."
    elif not confirmed_original:
        error = "You must confirm this is your original content before uploading."

    if error:
        flash(error, "error")
        return redirect(url_for("profile_edit"))

    pdf_filename = None
    if has_file:
        safe_name = secure_filename(pdf_file.filename)
        pdf_filename = f"{uuid.uuid4().hex}_{safe_name}"
        pdf_file.save(os.path.join(UPLOAD_DIR, pdf_filename))

    paper = UploadedPaper(
        user_id=current_user.id,
        title=title,
        abstract=abstract or None,
        journal=journal or None,
        year=int(year_raw) if year_raw else None,
        pdf_filename=pdf_filename,
        external_url=external_url or None,
        confirmed_original_content=True,
    )
    db.session.add(paper)
    db.session.commit()

    flash("Paper uploaded.", "success")
    return redirect(url_for("profile_edit"))


@app.route("/documents")
@login_required
def documents():
    docs = (
        Document.query.filter_by(user_id=current_user.id)
        .order_by(Document.updated_at.desc())
        .all()
    )
    return render_template("documents.html", documents=docs)


@app.route("/write/new", methods=["POST"])
@login_required
def new_document():
    doc = Document(user_id=current_user.id, title="Untitled document", body_html="")
    db.session.add(doc)
    db.session.commit()

    session["current_document_id"] = doc.id
    return redirect(url_for("write_document", doc_id=doc.id))


def _annotations_json_for(doc, user_id):
    """{highlight_id: [text, ...]} for this user's annotations on doc,
    JSON-encoded and with `</` escaped so it's safe to embed inside an
    inline <script> block (otherwise annotation text containing that
    substring could prematurely close the tag)."""
    by_highlight = {}
    annotations = Annotation.query.filter_by(document_id=doc.id, user_id=user_id).all()
    for annotation in annotations:
        by_highlight.setdefault(annotation.highlight_id, []).append(annotation.text)
    return json.dumps(by_highlight).replace("</", "<\\/")


@app.route("/write/<int:doc_id>")
@login_required
def write_document(doc_id):
    doc = db.session.get(Document, doc_id)
    if doc is None or doc.user_id != current_user.id:
        abort(404)

    session["current_document_id"] = doc.id
    return render_template(
        "write.html",
        doc=doc,
        annotations_json=_annotations_json_for(doc, current_user.id),
    )


@app.route("/write/<int:doc_id>/save", methods=["POST"])
@login_required
def save_document(doc_id):
    doc = db.session.get(Document, doc_id)
    if doc is None or doc.user_id != current_user.id:
        abort(404)

    doc.title = request.form.get("title", "").strip() or "Untitled document"
    doc.body_html = request.form.get("body_html", "")

    # Annotations are fully replaced from the client's current state each
    # save, same as body_html itself — simpler than diffing, and it means
    # deleting a highlighted passage naturally drops its annotation too.
    Annotation.query.filter_by(document_id=doc.id, user_id=current_user.id).delete()
    try:
        annotations_data = json.loads(request.form.get("annotations_json") or "{}")
    except ValueError:
        annotations_data = {}
    for highlight_id, texts in annotations_data.items():
        for text in texts:
            text = (text or "").strip()
            if text:
                db.session.add(Annotation(
                    document_id=doc.id,
                    user_id=current_user.id,
                    highlight_id=str(highlight_id)[:64],
                    text=text,
                ))

    db.session.commit()

    flash("Document saved.", "success")
    return redirect(url_for("write_document", doc_id=doc.id, embed=request.args.get("embed")))


@app.route("/write/<int:doc_id>/add_citation", methods=["POST"])
@login_required
def add_citation(doc_id):
    doc = db.session.get(Document, doc_id)
    if doc is None or doc.user_id != current_user.id:
        abort(404)

    doi = request.form.get("doi", "").strip()
    paper_id = request.form.get("paper_id", "").strip()
    identifier = doi or paper_id

    if identifier and identifier in _existing_citation_ids(doc):
        flash("That paper is already in this document.", "error")
        return redirect(url_for("write_document", doc_id=doc.id))

    citation_html = _citation_block_html(
        title=request.form.get("title", "").strip(),
        authors=request.form.get("authors", "").strip(),
        year=request.form.get("year", "").strip(),
        journal=request.form.get("journal", "").strip(),
        doi=doi,
        paper_id=paper_id,
    )
    doc.body_html = (doc.body_html or "") + citation_html
    db.session.commit()

    session["current_document_id"] = doc.id
    flash("Citation added to your document.", "success")
    return redirect(url_for("write_document", doc_id=doc.id))


if __name__ == "__main__":
    # Only used for local development (`python app.py`) — production runs
    # via gunicorn per the Procfile, which never executes this block.
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
