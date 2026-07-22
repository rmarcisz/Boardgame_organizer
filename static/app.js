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

// BoardGameGeek-backed typeahead for the "add game" / "add to wishlist" name
// fields, and repeatable "Dodatek" (expansion) rows: as you type, search BGG
// so picking a result locks in its bgg_id and box art (hidden inputs) instead
// of leaving the card without cover art. Hidden inputs are looked up within
// the input's own .bgg-field-group rather than the whole form, since a game's
// form can hold several of these groups (the game name plus any expansions).
var bggSearchTimer = null;
var bggSearchSeq = 0;

function clearBggSelection(input) {
    var group = input.closest(".bgg-field-group");
    if (!group) return;
    var idInput = group.querySelector(".bgg-id-input");
    var imageInput = group.querySelector(".bgg-image-input");
    if (idInput) idInput.value = "";
    if (imageInput) imageInput.value = "";
}

function selectBggResult(input, box, result) {
    input.value = result.name;
    box.innerHTML = "";
    var group = input.closest(".bgg-field-group");
    if (!group) return;
    var idInput = group.querySelector(".bgg-id-input");
    var imageInput = group.querySelector(".bgg-image-input");
    if (idInput) idInput.value = result.id;
    if (!imageInput) return;
    var form = input.closest("form");
    fetch("/bgg/thing/" + result.id, { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (thing) {
            if (thing && thing.image) imageInput.value = thing.image;
            // Only the game name field has an image input - this is the base
            // game, so cache its expansions for the "Dodatek" fields to filter
            // against instead of a second round trip the first time one is used.
            if (form) form._knownExpansions = (thing && thing.expansions) || [];
        })
        .catch(function () {});
}

// Not every game is on BGG (or worth looking up) - a "Dodaj jako: ..." option
// at the bottom of the list lets the typed text stand on its own instead of
// forcing a BGG match, mirroring the same option the local KNOWN_GAMES
// typeahead already offers (see addGameSuggestion above).
function addFreetextBggOption(box, input, query) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "game-suggestion game-suggestion-add";
    btn.textContent = "Dodaj jako: " + query;
    btn.addEventListener("mousedown", function (event) {
        event.preventDefault();
        // The typed text is already the input's value - just make sure no
        // stale bgg_id/image from an earlier pick tags along with it.
        clearBggSelection(input);
        box.innerHTML = "";
    });
    box.appendChild(btn);
}

function renderBggSuggestions(input) {
    var box = input.parentElement.querySelector(".game-suggestions");
    if (!box) return;
    var query = input.value.trim();
    box.innerHTML = "";
    if (!query) return;

    var seq = ++bggSearchSeq;
    fetch("/bgg/search?q=" + encodeURIComponent(query), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (results) {
            // A newer keystroke already fired another search - drop this stale one.
            if (seq !== bggSearchSeq) return;
            box.innerHTML = "";
            results.forEach(function (result) {
                var btn = document.createElement("button");
                btn.type = "button";
                btn.className = "game-suggestion";
                btn.textContent = result.year ? result.name + " (" + result.year + ")" : result.name;
                btn.addEventListener("mousedown", function (event) {
                    event.preventDefault();
                    selectBggResult(input, box, result);
                });
                box.appendChild(btn);
            });

            // Skip it when a result already matches the typed text exactly -
            // clicking that suggestion above already covers this case.
            var exactMatch = results.some(function (r) { return r.name.toLowerCase() === query.toLowerCase(); });
            if (!exactMatch) addFreetextBggOption(box, input, query);
        })
        .catch(function () {
            // Search failed (offline, BGG down, no token, ...) - freetext is
            // the only option left, so offer it instead of a dead end.
            box.innerHTML = "";
            addFreetextBggOption(box, input, query);
        });
}

// A "Dodatek" field only offers expansions BGG lists under the game's own
// bgg_id (looked up by the "bgg_id" form field, which only the game name
// group has - expansion rows use "expansion_bgg_id" instead). The list is
// fetched once per form and cached on it: eagerly when picking the base game
// fresh (see selectBggResult above), or lazily here the first time an addon
// field is used against an already-known bgg_id (e.g. editing an existing
// game). Resolves to null when there's no base game to look up yet, or an
// array (possibly empty) once one has been fetched.
function ensureKnownExpansions(form) {
    if (form._knownExpansions) return Promise.resolve(form._knownExpansions);
    if (form._expansionsLoading) return form._expansionsLoading;
    var idInput = form.querySelector('input[name="bgg_id"]');
    var baseId = idInput ? idInput.value : "";
    if (!baseId) return Promise.resolve(null);
    form._expansionsLoading = fetch("/bgg/thing/" + baseId, { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (thing) { return (thing && thing.expansions) || []; })
        .catch(function () { return []; })
        .then(function (result) {
            form._knownExpansions = result;
            form._expansionsLoading = null;
            return result;
        });
    return form._expansionsLoading;
}

function addSuggestionHint(box, text) {
    var hint = document.createElement("div");
    hint.className = "game-suggestion-hint";
    hint.textContent = text;
    box.appendChild(hint);
}

function renderExpansionSuggestions(input) {
    var box = input.parentElement.querySelector(".game-suggestions");
    if (!box) return;
    var query = input.value.trim();
    box.innerHTML = "";
    if (!query) return;

    var form = input.closest("form");
    ensureKnownExpansions(form).then(function (known) {
        // The field changed while the (first, cache-filling) lookup was in
        // flight - a newer call to this function already owns the result.
        if (input.value.trim() !== query) return;
        box.innerHTML = "";
        var matches = known
            ? known.filter(function (exp) { return exp.name.toLowerCase().indexOf(query.toLowerCase()) !== -1; }).slice(0, 8)
            : [];
        matches.forEach(function (result) {
            var btn = document.createElement("button");
            btn.type = "button";
            btn.className = "game-suggestion";
            btn.textContent = result.name;
            btn.addEventListener("mousedown", function (event) {
                event.preventDefault();
                selectBggResult(input, box, result);
            });
            box.appendChild(btn);
        });

        if (!matches.length) {
            if (known === null) {
                addSuggestionHint(box, "Najpierw wybierz grę bazową z listy BGG, aby zobaczyć jej dodatki.");
            } else if (!known.length) {
                addSuggestionHint(box, "BGG nie zna dodatków tej gry.");
            } else {
                addSuggestionHint(box, "Brak dopasowań wśród dodatków tej gry.");
            }
        }

        // Not every expansion is on BGG (or linked to a base game that is) -
        // freetext stands on its own here too, same as the game name field.
        var exactMatch = matches.some(function (m) { return m.name.toLowerCase() === query.toLowerCase(); });
        if (!exactMatch) addFreetextBggOption(box, input, query);
    });
}

document.addEventListener("input", function (event) {
    if (!event.target.matches(".bgg-name-input")) return;
    var input = event.target;
    clearBggSelection(input);
    if (input.closest(".expansion-row")) {
        renderExpansionSuggestions(input);
        return;
    }
    // The base game identity may be changing - drop any cached expansion
    // list so the next addon lookup re-fetches against whatever's picked next.
    var form = input.closest("form");
    if (form) {
        form._knownExpansions = null;
        form._expansionsLoading = null;
    }
    clearTimeout(bggSearchTimer);
    bggSearchTimer = setTimeout(function () { renderBggSuggestions(input); }, 300);
});

document.addEventListener("focusout", function (event) {
    if (!event.target.matches(".bgg-name-input")) return;
    var box = event.target.parentElement.querySelector(".game-suggestions");
    if (box) box.innerHTML = "";
});

// Repeatable "Dodatek" (expansion) rows on the add/edit game forms: rows
// aren't shown until "+ Dodaj dodatek" is clicked, and each can be removed
// again. Renumbering after add/remove only touches placeholders, never a
// row the user has already typed into.
function renumberExpansions(list) {
    list.querySelectorAll(".expansion-row").forEach(function (row, i) {
        var input = row.querySelector(".bgg-name-input");
        if (input && !input.value) input.placeholder = "Dodatek " + (i + 1) + " (opcjonalnie)";
    });
}

document.addEventListener("click", function (event) {
    var addBtn = event.target.closest(".add-expansion-btn");
    if (addBtn) {
        var list = addBtn.closest(".expansions-field").querySelector(".expansions-list");
        var row = document.createElement("div");
        row.className = "expansion-row bgg-field-group";
        row.innerHTML =
            '<span class="game-name-field">' +
                '<input type="text" name="expansion_name" class="bgg-name-input" placeholder="Dodatek" maxlength="100" autocomplete="off">' +
                '<div class="game-suggestions"></div>' +
            "</span>" +
            '<input type="hidden" name="expansion_bgg_id" class="bgg-id-input">' +
            '<button type="button" class="link-btn danger remove-expansion-btn" title="Usuń dodatek" aria-label="Usuń dodatek">✕</button>';
        list.appendChild(row);
        renumberExpansions(list);
        return;
    }

    var removeBtn = event.target.closest(".remove-expansion-btn");
    if (removeBtn) {
        var removedFrom = removeBtn.closest(".expansions-list");
        removeBtn.closest(".expansion-row").remove();
        renumberExpansions(removedFrom);
        return;
    }

    var flagBtn = event.target.closest(".lang-flag-btn");
    if (flagBtn) {
        var select = flagBtn.closest(".lang-select");
        select.querySelector(".language-value").value = flagBtn.dataset.lang;
        select.querySelector(".lang-select-btn").textContent = flagBtn.textContent;
        select.querySelector(".language-input").value = "";
        select.open = false;
        return;
    }

    // Clicking anywhere outside an open language dropdown closes it, same as
    // a native <select> would - <details> otherwise stays open until its own
    // summary is clicked again.
    document.querySelectorAll(".lang-select[open]").forEach(function (el) {
        if (!el.contains(event.target)) el.open = false;
    });
});

// Typing a custom language live-updates the hidden value and the button's
// own label, so it's reflected even without picking one of the quick flags.
document.addEventListener("input", function (event) {
    if (!event.target.matches(".language-input")) return;
    var select = event.target.closest(".lang-select");
    var value = event.target.value.trim();
    select.querySelector(".language-value").value = value;
    select.querySelector(".lang-select-btn").textContent = value || "🏳 Język";
});
