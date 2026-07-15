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
                        current.querySelectorAll("details[data-id][open]"),
                        function (el) { return el.dataset.id; }
                    )
                );
                updated.querySelectorAll("details[data-id]").forEach(function (el) {
                    if (openIds.has(el.dataset.id)) el.open = true;
                });

                current.replaceWith(updated);
            });
        })
        .catch(function () {
            form.submit(); // fall back to a normal submit if the fetch failed
        });
});

// Marks a game's comments as seen (clearing the "unread" highlight) the
// moment its comment thread is opened. "toggle" doesn't bubble, so this
// listener is registered on the capture phase instead.
document.addEventListener("toggle", function (event) {
    var el = event.target;
    if (!el.matches || !el.matches("details.comments") || !el.open) return;

    var gameId = el.dataset.id.replace("comments-", "");
    fetch("/games/" + gameId + "/comments/seen", {
        method: "POST",
        credentials: "same-origin",
    }).then(function () {
        var card = el.closest(".card");
        if (card) card.classList.remove("unread");
    });
}, true);
