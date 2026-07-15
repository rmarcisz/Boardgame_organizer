// Intercepts .ajax-form submits (star toggle, "bring wish", comments, ...) so
// they update the page's .ajax-region containers in place instead of doing a
// full page navigation. Works on any page - regions are matched by id.
document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.classList.contains("ajax-form")) return;

    event.preventDefault();

    var regionIds = Array.prototype.map.call(
        document.querySelectorAll(".ajax-region[id]"),
        function (el) { return el.id; }
    );

    fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
    })
        .then(function (response) {
            if (response.url.indexOf("/login") !== -1) {
                // Session expired - do a real navigation to show the login page.
                window.location.href = response.url;
                return null;
            }
            return response.text();
        })
        .then(function (html) {
            if (html === null) return;
            var doc = new DOMParser().parseFromString(html, "text/html");

            regionIds.forEach(function (id) {
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
            updateCommentsLayout();
        })
        .catch(function () {
            form.submit(); // fall back to a normal submit if the fetch failed
        });
});

// While a game's comment thread is open, expand the games column to full
// width and hide the wishlist column (and vice versa for a wish's thread).
function updateCommentsLayout() {
    var gameOpen = document.querySelector("#games-section details.comments[open]") !== null;
    var wishOpen = document.querySelector("#wishes-section details.comments[open]") !== null;
    document.querySelectorAll(".games-wishes-row").forEach(function (el) {
        el.classList.toggle("comments-open-games", gameOpen);
        el.classList.toggle("comments-open-wishes", !gameOpen && wishOpen);
    });
}

// Marks a game's comments as seen (clearing the "unread" highlight) the
// moment its comment thread is opened. "toggle" doesn't bubble, so this
// listener is registered on the capture phase instead.
document.addEventListener("toggle", function (event) {
    var el = event.target;
    if (!el.matches || !el.matches("details.comments")) return;

    updateCommentsLayout();
    if (!el.open || el.dataset.id.indexOf("comments-game-") !== 0) return;

    var gameId = el.dataset.id.replace("comments-game-", "");
    fetch("/games/" + gameId + "/comments/seen", {
        method: "POST",
        credentials: "same-origin",
    }).then(function () {
        var card = el.closest(".card");
        if (card) card.classList.remove("unread");
    });
}, true);
