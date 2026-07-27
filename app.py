from flask import Flask, render_template, request
from requests.exceptions import RequestException

from services.openalex import get_author_profile, search_papers

app = Flask(__name__)


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


if __name__ == "__main__":
    app.run(debug=True)
