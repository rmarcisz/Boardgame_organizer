// Intercepts .ajax-form submits (star toggle, "bring wish") so they update
// the games/wishes lists in place instead of doing a full page navigation.
document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.classList.contains("ajax-form")) return;

    event.preventDefault();

    fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
    })
        .then(function (response) {
            if (response.url.indexOf("/gry") === -1) {
                // Session expired or redirected somewhere unexpected - do a real navigation.
                window.location.href = response.url;
                return null;
            }
            return response.text();
        })
        .then(function (html) {
            if (html === null) return;
            var doc = new DOMParser().parseFromString(html, "text/html");

            ["games-section", "wishes-section"].forEach(function (id) {
                var current = document.getElementById(id);
                var updated = doc.getElementById(id);
                if (!current || !updated) return;

                var openIds = new Set(
                    Array.prototype.map.call(
                        current.querySelectorAll("details.card[open]"),
                        function (el) { return el.dataset.id; }
                    )
                );
                updated.querySelectorAll("details.card").forEach(function (el) {
                    if (openIds.has(el.dataset.id)) el.open = true;
                });

                current.replaceWith(updated);
            });
        })
        .catch(function () {
            form.submit(); // fall back to a normal submit if the fetch failed
        });
});
