from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    institution = db.Column(db.String(255))
    openalex_author_id = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False, default="Untitled document")
    body_html = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship(
        "User",
        backref=db.backref("documents", order_by="Document.updated_at.desc()"),
    )


class Annotation(db.Model):
    """A note attached to a highlighted span of text in a Document's
    body_html (the span itself — <span class="annotation-highlight"
    data-highlight-id="..."> — lives inside body_html and is matched to
    its annotations by highlight_id). Single-user for now (one row per
    document+user+highlight); kept as its own table rather than embedded
    in body_html so that sharing a document's annotations across
    multiple users later is a query, not a migration.
    """

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("document.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    highlight_id = db.Column(db.String(64), nullable=False, index=True)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    document = db.relationship(
        "Document",
        backref=db.backref("annotations", order_by="Annotation.created_at"),
    )
    user = db.relationship("User")


class UploadedPaper(db.Model):
    """A "user_posts" row: original content a user submitted directly,
    distinct from "verified_papers" (OpenAlex-sourced, DOI-backed works,
    which are fetched live and never stored locally). Always render this
    content with the "User-submitted, not peer-reviewed" badge, never the
    "Verified" one.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(500), nullable=False)
    abstract = db.Column(db.Text)
    journal = db.Column(db.String(255))
    year = db.Column(db.Integer)
    pdf_filename = db.Column(db.String(255))
    external_url = db.Column(db.String(1000))
    # Recorded proof that the uploader checked the originality/rights
    # confirmation at submission time — part of the creator-rights
    # protection mechanism, not just a UI nag.
    confirmed_original_content = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship(
        "User",
        backref=db.backref("uploaded_papers", order_by="UploadedPaper.year.desc()"),
    )
