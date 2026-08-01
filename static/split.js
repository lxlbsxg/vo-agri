// "Write alongside" split view: toggles a right-hand panel that shows the
// current document (loaded via /write/<id>?embed=1 in an iframe) next to
// whatever the user is browsing on the left. Adding a citation while the
// panel is open posts via fetch instead of a normal form submit, so the
// left page never navigates away. The write panel also reports its current
// citation list back to us on every load (see write.html) — that's the
// single source of truth we use to keep every "Add to document" button in
// sync, including flipping one back to addable when its citation is
// removed (and saved) from the document.
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

    function citationFormIdentifier(form) {
        var doi = form.querySelector('input[name="doi"]');
        var paperId = form.querySelector('input[name="paper_id"]');
        return (doi && doi.value) || (paperId && paperId.value) || null;
    }

    function setAddButtonState(form, added) {
        var submitBtn = form.querySelector("button[type=submit]");
        if (!submitBtn) {
            return;
        }
        if (added) {
            if (!submitBtn.disabled) {
                submitBtn.dataset.addLabel = submitBtn.textContent;
            }
            submitBtn.disabled = true;
            submitBtn.classList.add("btn-disabled");
            submitBtn.textContent = "Already added";
        } else if (submitBtn.disabled) {
            submitBtn.disabled = false;
            submitBtn.classList.remove("btn-disabled");
            if (submitBtn.dataset.addLabel) {
                submitBtn.textContent = submitBtn.dataset.addLabel;
            }
        }
    }

    // Authoritative sync: whenever the write panel (re)loads, it tells us
    // exactly which papers are cited right now. Reconcile every visible
    // "Add to document" form against that list, in both directions.
    window.addEventListener("message", function (event) {
        if (event.source !== iframe.contentWindow || event.origin !== window.location.origin) {
            return;
        }
        var data = event.data;
        if (!data || data.source !== "vo-agri-write" || !Array.isArray(data.citationIds)) {
            return;
        }
        var citedIds = data.citationIds;
        document.querySelectorAll(".add-to-doc-form").forEach(function (form) {
            var identifier = citationFormIdentifier(form);
            if (!identifier) {
                return;
            }
            setAddButtonState(form, citedIds.indexOf(identifier) !== -1);
        });
    });

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

                setAddButtonState(form, true);

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
