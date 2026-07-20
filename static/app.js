// Per-list filter (by person, or "" for everyone) and sort (a-z / by number
// of interested people, ties broken alphabetically) controls. The toolbar
// lives outside the .ajax-region it controls, so it survives region swaps
// untouched - after a swap we just rebuild its options from the fresh cards
// and re-apply whatever filter/sort was already selected.
function collectPeople(region) {
    var set = new Set();
    region.querySelectorAll("[data-people]").forEach(function (el) {
        el.dataset.people.split(",").forEach(function (p) {
            p = p.trim();
            if (p) set.add(p);
        });
    });
    return Array.from(set).sort(function (a, b) { return a.localeCompare(b, "pl"); });
}

function refreshToolbar(toolbar, region) {
    var select = toolbar.querySelector(".filter-select");
    if (!select) return;
    var current = select.value;
    var people = collectPeople(region);

    select.innerHTML = "";
    var allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = "Wszyscy";
    select.appendChild(allOption);
    people.forEach(function (name) {
        var opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        select.appendChild(opt);
    });

    select.value = people.indexOf(current) !== -1 ? current : "";
}

function applyFilterSort(toolbar, region) {
    var filterValue = toolbar.querySelector(".filter-select").value;
    var sortValue = toolbar.querySelector(".sort-select").value;

    region.querySelectorAll(".card-grid").forEach(function (grid) {
        var cards = Array.prototype.slice.call(grid.children);

        cards.forEach(function (card) {
            var people = (card.dataset.people || "").split(",").map(function (p) { return p.trim(); });
            card.style.display = (!filterValue || people.indexOf(filterValue) !== -1) ? "" : "none";
        });

        cards.sort(function (a, b) {
            if (sortValue === "interest") {
                var diff = parseInt(b.dataset.interest || "0", 10) - parseInt(a.dataset.interest || "0", 10);
                if (diff !== 0) return diff;
            }
            return (a.dataset.name || "").localeCompare(b.dataset.name || "", "pl");
        });
        cards.forEach(function (card) { grid.appendChild(card); });
    });
}

function initToolbar(toolbar) {
    var region = document.getElementById(toolbar.dataset.region);
    if (!region) return;
    refreshToolbar(toolbar, region);
    applyFilterSort(toolbar, region);
}

document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".list-toolbar[data-region]").forEach(initToolbar);
});

document.addEventListener("change", function (event) {
    var el = event.target;
    if (!el.matches(".filter-select, .sort-select")) return;
    var toolbar = el.closest(".list-toolbar");
    var region = document.getElementById(toolbar.dataset.region);
    if (region) applyFilterSort(toolbar, region);
});

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

                var toolbar = document.querySelector('.list-toolbar[data-region="' + id + '"]');
                if (toolbar) {
                    refreshToolbar(toolbar, updated);
                    applyFilterSort(toolbar, updated);
                }
            });
            updateCommentsLayout();
        })
        .catch(function () {
            form.submit(); // fall back to a normal submit if the fetch failed
        });
});

// A comments thread only really counts as "open" if it (and every ancestor
// <details>, e.g. the card it lives in) is actually open - collapsing the
// outer card leaves the nested comments' [open] attribute untouched even
// though it's no longer visible.
function isVisiblyOpen(el) {
    while (el) {
        if (el.tagName === "DETAILS" && !el.open) return false;
        el = el.parentElement;
    }
    return true;
}

// While a comment thread is open in one panel of a two-col row (games/
// wishlist, szafa's own/others columns, ...), expand that panel to the full
// row width and hide its sibling panel(s).
function updateCommentsLayout() {
    document.querySelectorAll(".two-col").forEach(function (row) {
        var panels = Array.prototype.filter.call(row.children, function (child) {
            return child.classList.contains("panel");
        });
        var focused = null;
        panels.forEach(function (panel) {
            var hasOpenThread = Array.prototype.some.call(
                panel.querySelectorAll("details.comments[open]"),
                isVisiblyOpen
            );
            panel.classList.toggle("comments-focus", hasOpenThread);
            if (hasOpenThread) focused = panel;
        });
        row.classList.toggle("comments-active", !!focused);
    });
}

// Recheck the layout whenever any <details> toggles - not just the comments
// thread itself, but also the card (or quick-add panel, etc.) it's nested
// in, since collapsing an ancestor can hide a thread without touching its
// own [open] attribute. "toggle" doesn't bubble, so this listener is
// registered on the capture phase instead.
document.addEventListener("toggle", function (event) {
    var el = event.target;
    if (!el.matches || !el.matches("details")) return;

    updateCommentsLayout();
    if (!el.matches("details.comments") || !el.open || el.dataset.id.indexOf("comments-game-") !== 0) {
        return;
    }

    var gameId = el.dataset.id.replace("comments-game-", "");
    fetch("/games/" + gameId + "/comments/seen", {
        method: "POST",
        credentials: "same-origin",
    }).then(function () {
        var card = el.closest(".card");
        if (card) card.classList.remove("unread");
    });
}, true);

// Typeahead for the "propose an activity" game name field: as the organizer
// types, shows up to 5 already-known games (from the Gry tab) so they reuse
// an existing game's exact spelling instead of creating a near-duplicate.
var KNOWN_GAMES = (function () {
    var el = document.getElementById("known-games");
    if (!el) return [];
    try {
        return JSON.parse(el.textContent);
    } catch (e) {
        return [];
    }
})();

function matchGames(query) {
    query = query.trim().toLowerCase();
    if (!query) return [];
    return KNOWN_GAMES
        .filter(function (name) { return name.toLowerCase().indexOf(query) !== -1; })
        .slice(0, 5);
}

function addGameSuggestion(box, input, label, value, extraClass) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = extraClass ? "game-suggestion " + extraClass : "game-suggestion";
    btn.textContent = label;
    // mousedown fires before the input's blur, so the click still lands
    // even though clicking the button steals focus from the text field.
    btn.addEventListener("mousedown", function (event) {
        event.preventDefault();
        input.value = value;
        box.innerHTML = "";
        input.focus();
    });
    box.appendChild(btn);
}

function renderGameSuggestions(input) {
    var box = input.parentElement.querySelector(".game-suggestions");
    if (!box) return;
    box.innerHTML = "";
    var query = input.value.trim();
    if (!query) return;

    var matches = matchGames(query);
    matches.forEach(function (name) { addGameSuggestion(box, input, name, name); });

    // Skip the "add as new" option when what's typed already matches a known
    // game exactly - the matching suggestion above already covers that case.
    var exactMatch = matches.some(function (name) { return name.toLowerCase() === query.toLowerCase(); });
    if (!exactMatch) {
        addGameSuggestion(box, input, "Dodaj jako: " + query, query, "game-suggestion-add");
    }
}

document.addEventListener("input", function (event) {
    if (!event.target.matches(".game-name-input")) return;
    renderGameSuggestions(event.target);
});

document.addEventListener("focusout", function (event) {
    if (!event.target.matches(".game-name-input")) return;
    var box = event.target.parentElement.querySelector(".game-suggestions");
    if (box) box.innerHTML = "";
});
