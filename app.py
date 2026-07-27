import os
import re

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from requests.exceptions import RequestException

from extensions import db, login_manager
from models import User
from services.openalex import get_author_profile, normalize_author_id, search_papers

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'vo_agri.db')}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
login_manager.init_app(app)

with app.app_context():
    db.create_all()


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@app.route("/")
def home():
    return render_template("index.html", query="", papers=[], error=None)


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    papers = []
    error = None

    if query:
        try:
            papers = search_papers(query)
        except RequestException:
            error = "Could not reach OpenAlex right now. Please try again."

    return render_template(
        "index.html",
        query=query,
        papers=papers,
        error=error,
    )


@app.route("/author/<author_id>")
def author_profile(author_id):
    author = None
    error = None

    try:
        author = get_author_profile(author_id)
    except RequestException:
        error = "Could not reach OpenAlex right now. Please try again."

    return render_template("author.html", author=author, error=error)


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


if __name__ == "__main__":
    app.run(debug=True)
