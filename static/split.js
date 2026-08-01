// "Write alongside" split view: toggles a right-hand panel that shows the
// current document (loaded via /write/<id>?embed=1 in an iframe) next to
// whatever the user is browsing on the left. Adding a citation while the
// panel is open posts via fetch instead of a normal form submit, so the
// left page never navigates away, and the iframe is refreshed afterward
// to reflect the new content.
document.addEventListener("DOMContentLoaded", function () {
    var body = document.body;
    var iframe = document.getElementById("split-iframe");
    var closeBtn = document.getElementById("split-close");

    function openSplit(docId) {
        var src = "/write/" + docId + "?embed=1";
        if (iframe.dataset.docId !== String(docId)) {
            iframe.src = src;
            iframe.dataset.docId = String(docId);
        }
        body.classList.add("split-active");
    }

    function closeSplit() {
        body.classList.remove("split-active");
    }

    document.addEventListener("click", function (event) {
        var toggle = event.target.closest("[data-write-alongside]");
        if (!toggle) {
            return;
        }
        if (body.classList.contains("split-active")) {
            closeSplit();
        } else {
            openSplit(toggle.dataset.writeAlongside);
        }
    });

    if (closeBtn) {
        closeBtn.addEventListener("click", closeSplit);
    }

    document.addEventListener("submit", function (event) {
        var form = event.target.closest(".add-to-doc-form");
        if (!form || !body.classList.contains("split-active")) {
            return;
        }
        event.preventDefault();

        var submitBtn = form.querySelector("button[type=submit]");
        if (submitBtn) {
            submitBtn.disabled = true;
        }

        fetch(form.action, { method: "POST", body: new FormData(form) })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("add_citation failed: " + response.status);
                }

                var replacement = document.createElement("button");
                replacement.type = "button";
                replacement.className = "btn-small btn-disabled";
                replacement.disabled = true;
                replacement.textContent = "Already added";
                form.replaceWith(replacement);

                if (iframe.contentWindow) {
                    iframe.contentWindow.location.reload();
                }
            })
            .catch(function () {
                // Fall back to a normal navigation if the AJAX add failed
                // for some reason (offline, server error, etc).
                if (submitBtn) {
                    submitBtn.disabled = false;
                }
                form.submit();
            });
    });
});
