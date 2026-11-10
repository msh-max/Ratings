/* Riyadh Best Restaurants — static site. No build step, no dependencies except Leaflet
 * (loaded from cdnjs only when the map is opened). Data: window.DATA_URL or data/restaurants.json. */
(function () {
  "use strict";

  var DATA_URL = (typeof window.DATA_URL === "string" && window.DATA_URL) || "data/restaurants.json";
  var LEAFLET_JS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js";
  var LEAFLET_CSS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css";
  var PAGE = 40;
  var TYPE_LABEL = { guide: "Guide", ranking: "Ranking", award: "Award", editorial: "Editorial",
                     official: "Official", community: "Community" };
  var TYPE_ORDER = ["guide", "ranking", "award", "editorial", "official", "community"];
  var CONF_COLOR = { strong: "#1b7f4b", medium: "#b07a00", single: "#7a7568" };

  var $ = function (id) { return document.getElementById(id); };
  var state = { q: "", cuisine: "", area: "", price: "", confidence: "", type: "", sort: "score",
                view: "list", shown: PAGE };
  var data = null, places = [], filtered = [], maxScore = 1, sourceById = {};
  var map = null, markerLayer = null, leafletPromise = null;

  // ---------------------------------------------------------------- helpers
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function safeUrl(u) { return typeof u === "string" && /^https?:\/\//i.test(u) ? u : null; }
  function fold(s) {
    return String(s || "").normalize("NFD").replace(/[̀-ًͯ-ٰٟ]/g, "")
      .toLowerCase().replace(/[أإآٱ]/g, "ا").replace(/ة/g, "ه").replace(/ى/g, "ي");
  }
  var AR_RE = /[؀-ۿ]/;
  function nameHtml(s) {
    return AR_RE.test(s) ? '<bdi lang="ar" dir="rtl" class="ar">' + esc(s) + "</bdi>" : "<bdi>" + esc(s) + "</bdi>";
  }
  function dateKey(d) { return d ? String(d).replace(/\D/g, "").slice(0, 8).padEnd(8, "0") : "0"; }
  function uniqSorted(arr) {
    var seen = {}, out = [];
    arr.forEach(function (v) { if (v && !seen[v]) { seen[v] = 1; out.push(v); } });
    return out.sort(function (a, b) { return a.localeCompare(b); });
  }
  function fmtMonth(m) {
    if (!m) return "";
    var p = m.split("-"), d = new Date(Date.UTC(+p[0], +p[1] - 1, 1));
    return d.toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
  }

  // ---------------------------------------------------------------- data
  function prepare(json) {
    data = json;
    sourceById = {};
    (json.sources || []).forEach(function (s) { sourceById[s.source_id] = s; });
    places = (json.places || []).map(function (p) {
      var mentions = (p.mentions || []).map(function (m) {
        var s = sourceById[m.source_id] || {};
        return {
          source_id: m.source_id, tier: m.tier, position: m.position, weight: m.weight, note: m.note || null,
          publisher: m.publisher || s.publisher || m.source_id,
          source_type: m.source_type || s.source_type || "editorial",
          published_at: m.published_at !== undefined ? m.published_at : s.published_at,
          source_name: s.source_name || "",
          url: safeUrl(m.url) || safeUrl(s.url)
        };
      });
      var types = {}; mentions.forEach(function (m) { types[m.source_type] = 1; });
      var latest = mentions.reduce(function (a, m) { var k = dateKey(m.published_at); return k > a ? k : a; }, "0");
      return Object.assign({}, p, {
        mentions: mentions, _types: types, _latest: latest,
        _search: fold([p.name, p.name_ar, (p.aliases || []).join(" "), (p.cuisines || []).join(" "), p.area,
                       (p.branches || []).join(" "), mentions.map(function (m) { return m.publisher; }).join(" "),
                       (p.awards || []).join(" ")].join(" "))
      });
    });
    maxScore = places.reduce(function (a, p) { return Math.max(a, p.score || 0); }, 0) || 1;
    fillSelect("f-cuisine", uniqSorted([].concat.apply([], places.map(function (p) { return p.cuisines || []; }))));
    fillSelect("f-area", uniqSorted(places.map(function (p) { return p.area; })));
    fillSelect("f-price", uniqSorted(places.map(function (p) { return p.price_band; }))
      .sort(function (a, b) { return a.length - b.length; }));
    fillSelect("f-type", TYPE_ORDER.filter(function (t) { return places.some(function (p) { return p._types[t]; }); }),
      function (t) { return TYPE_LABEL[t]; });
  }

  function fillSelect(id, values, label) {
    var sel = $(id), keep = sel.options[0];
    sel.innerHTML = ""; sel.appendChild(keep);
    values.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v; o.textContent = label ? label(v) : v; sel.appendChild(o);
    });
  }

  // ---------------------------------------------------------------- filtering
  function applyFilters() {
    var terms = fold(state.q).split(/\s+/).filter(Boolean);
    filtered = places.filter(function (p) {
      if (state.cuisine && (p.cuisines || []).indexOf(state.cuisine) < 0) return false;
      if (state.area && p.area !== state.area) return false;
      if (state.price && p.price_band !== state.price) return false;
      if (state.confidence && p.confidence !== state.confidence) return false;
      if (state.type && !p._types[state.type]) return false;
      for (var i = 0; i < terms.length; i++) if (p._search.indexOf(terms[i]) < 0) return false;
      return true;
    });
    if (state.sort === "name") filtered.sort(function (a, b) { return a.name.localeCompare(b.name); });
    else if (state.sort === "recent") filtered.sort(function (a, b) { return a._latest < b._latest ? 1 : a._latest > b._latest ? -1 : a.rank - b.rank; });
    else filtered.sort(function (a, b) { return a.rank - b.rank; });
  }

  function activeFilterCount() {
    return ["cuisine", "area", "price", "confidence", "type"].filter(function (k) { return state[k]; }).length +
      (state.sort !== "score" ? 1 : 0);
  }

  // ---------------------------------------------------------------- rendering
  function badge(m) {
    var tier = m.tier || (m.position ? "#" + m.position : "listed");
    var title = (m.source_name || m.publisher) + " — " + tier + (m.published_at ? " (" + m.published_at + ")" : "") +
      " · " + TYPE_LABEL[m.source_type] + " · weight " + m.weight;
    var inner = esc(m.publisher) + ' <span class="tier">· ' + esc(tier) + "</span>";
    var cls = "badge t-" + esc(m.source_type);
    return m.url
      ? '<a class="' + cls + '" href="' + esc(m.url) + '" target="_blank" rel="noopener" title="' + esc(title) + '">' + inner + "</a>"
      : '<span class="' + cls + '" title="' + esc(title) + '">' + inner + "</span>";
  }

  function card(p) {
    var meta = [];
    if (p.cuisines && p.cuisines.length) meta.push(esc(p.cuisines.slice(0, 3).join(", ")));
    if (p.area) meta.push(nameHtml(p.area));
    if (p.price_band) meta.push('<span class="price">' + esc(p.price_band) + "</span>");
    var pct = Math.max(3, Math.round(100 * (p.score || 0) / maxScore));
    var nPub = {}; p.mentions.forEach(function (m) { nPub[m.publisher] = 1; });
    var confTitle = Object.keys(nPub).length + " independent publisher" + (Object.keys(nPub).length === 1 ? "" : "s");
    return '<li class="card" id="p-' + esc(p.id) + '">' +
      '<div class="card-head"><div class="rank' + (p.rank <= 10 ? " top" : "") + '" aria-label="Rank ' + p.rank + '">' + p.rank + "</div>" +
      '<div class="title"><h3>' + nameHtml(p.name) + "</h3>" +
      (p.name_ar ? '<bdi class="name-ar" lang="ar" dir="rtl">' + esc(p.name_ar) + "</bdi>" : "") +
      (meta.length ? '<p class="meta">' + meta.join(" · ") + "</p>" : "") + "</div></div>" +
      '<div class="scoreline"><div class="bar" aria-hidden="true"><span style="width:' + pct + '%"></span></div>' +
      '<span class="score" title="Score">' + (p.score || 0).toFixed(1) + "</span>" +
      '<span class="conf ' + esc(p.confidence) + '" title="' + esc(confTitle) + '">' + esc(p.confidence) + "</span></div>" +
      (p.why ? '<p class="why">' + esc(p.why) + "</p>" : "") +
      (p.awards && p.awards.length ? '<ul class="awards" aria-label="Awards">' +
        p.awards.map(function (a) { return "<li>" + esc(a) + "</li>"; }).join("") + "</ul>" : "") +
      '<div class="badges" aria-label="Sources">' + p.mentions.map(badge).join("") + "</div>" +
      (p.branches && p.branches.length ? '<p class="branches">Branches named by sources: ' +
        p.branches.map(nameHtml).join(", ") + "</p>" : "") +
      (p.lat != null ? '<div class="card-foot"><span></span><button type="button" data-map="' + esc(p.id) + '">Show on map</button></div>' : "") +
      "</li>";
  }

  function renderList() {
    var list = $("cards"), slice = filtered.slice(0, state.shown);
    list.innerHTML = slice.length ? slice.map(card).join("") : '<li class="empty">No restaurants match these filters.</li>';
    var more = $("more");
    more.hidden = filtered.length <= state.shown;
    more.textContent = "Show more (" + (filtered.length - slice.length) + " left)";
  }

  function renderStatus() {
    var n = filtered.length, total = places.length;
    $("status").textContent = n === total ? total + " restaurants, ranked by consensus score"
      : n + " of " + total + " restaurants match";
    var c = activeFilterCount(), el = $("filters-count");
    el.hidden = !c; el.textContent = c;
  }

  function render() {
    applyFilters();
    renderStatus();
    if (state.view === "list") renderList();
    if (state.view === "map" && map) renderMarkers();
    writeHash();
  }

  function renderSources() {
    var groups = {};
    (data.sources || []).forEach(function (s) { (groups[s.source_type] = groups[s.source_type] || []).push(s); });
    $("sources").innerHTML = TYPE_ORDER.filter(function (t) { return groups[t]; }).map(function (t) {
      var items = groups[t].slice().sort(function (a, b) { return a.publisher.localeCompare(b.publisher) || a.source_name.localeCompare(b.source_name); });
      return '<div class="src-group"><h3><span class="badge t-' + t + '">' + TYPE_LABEL[t] + "</span>" + items.length + " source" + (items.length > 1 ? "s" : "") + "</h3>" +
        '<ul class="src-list">' + items.map(function (s) {
          var url = safeUrl(s.url), name = nameHtml(s.source_name);
          return "<li>" + (url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener">' + name + "</a>" : name) +
            '<p class="meta">' + esc(s.publisher) + " · " + (s.published_at ? esc(s.published_at) : "undated") + " · " +
            s.n_entries + " entr" + (s.n_entries === 1 ? "y" : "ies") + "</p></li>";
        }).join("") + "</ul></div>";
    }).join("");
  }

  // ---------------------------------------------------------------- map
  function loadLeaflet() {
    if (window.L) return Promise.resolve(window.L);
    if (leafletPromise) return leafletPromise;
    leafletPromise = new Promise(function (resolve, reject) {
      var css = document.createElement("link");
      css.rel = "stylesheet"; css.href = LEAFLET_CSS; css.crossOrigin = "anonymous";
      document.head.appendChild(css);
      var js = document.createElement("script");
      js.src = LEAFLET_JS; js.crossOrigin = "anonymous";
      js.onload = function () { resolve(window.L); };
      js.onerror = function () { leafletPromise = null; reject(new Error("Leaflet failed to load")); };
      document.head.appendChild(js);
    });
    return leafletPromise;
  }

  function initMap() {
    return loadLeaflet().then(function (L) {
      if (map) return;
      map = L.map("map", { zoomControl: true, preferCanvas: true }).setView([24.7136, 46.6753], 11);
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'
      }).addTo(map);
      markerLayer = L.layerGroup().addTo(map);
      map.on("popupopen", function (e) {
        var btn = e.popup.getElement().querySelector("button[data-list]");
        if (btn) btn.addEventListener("click", function () { showInList(btn.getAttribute("data-list")); });
      });
      renderMarkers(true);
    }).catch(function () {
      $("map-note").textContent = "The map library could not be loaded (offline?). The list view still works.";
    });
  }

  function renderMarkers(fit) {
    var L = window.L; if (!L || !map) return;
    markerLayer.clearLayers();
    var pts = [], missing = 0;
    filtered.slice().reverse().forEach(function (p) {     // top ranks drawn last (on top)
      if (p.lat == null || p.lng == null) { missing++; return; }
      var r = p.rank <= 10 ? 9 : p.rank <= 50 ? 7 : 5;
      var m = L.circleMarker([p.lat, p.lng], { radius: r, color: "#fff", weight: 1.5, fillOpacity: .9,
        fillColor: CONF_COLOR[p.confidence] || "#7a7568" });
      m.bindPopup("<b>#" + p.rank + " " + esc(p.name) + "</b>" +
        (p.name_ar ? '<span class="ar" dir="rtl" lang="ar">' + esc(p.name_ar) + "</span>" : "") +
        "<br>" + esc([(p.cuisines || [])[0], p.area].filter(Boolean).join(" · ")) +
        "<br>Score " + (p.score || 0).toFixed(1) + " · " + esc(p.confidence) +
        '<br><button type="button" data-list="' + esc(p.id) + '">Show in list</button>');
      m.bindTooltip("#" + p.rank + " " + p.name);
      m.addTo(markerLayer);
      pts.push([p.lat, p.lng]);
    });
    if (fit && pts.length) map.fitBounds(pts, { padding: [30, 30], maxZoom: 14 });
    $("map-note").textContent = (pts.length ? pts.length + " shown on the map. " : "No mapped restaurants match. ") +
      (missing ? missing + " without coordinates (not found on OpenStreetMap) are only in the list. " : "") +
      "Green = strong, amber = medium, grey = single source.";
  }

  function focusOnMap(id) {
    var p = places.filter(function (x) { return x.id === id; })[0];
    if (!p) return;
    setView("map").then(function () {
      if (!map) return;
      map.setView([p.lat, p.lng], 16);
      markerLayer.eachLayer(function (l) { var ll = l.getLatLng(); if (ll.lat === p.lat && ll.lng === p.lng) l.openPopup(); });
    });
  }

  function showInList(id) {
    var idx = filtered.findIndex(function (p) { return p.id === id; });
    if (idx < 0) { resetFilters(); idx = filtered.findIndex(function (p) { return p.id === id; }); }
    if (idx >= state.shown) state.shown = idx + PAGE;
    setView("list");
    var el = $("p-" + id);
    if (el) {
      el.scrollIntoView({ block: "center" });
      el.classList.add("flash"); setTimeout(function () { el.classList.remove("flash"); }, 1600);
    }
  }

  // ---------------------------------------------------------------- views & state
  function setView(view) {
    state.view = view;
    ["list", "map", "sources"].forEach(function (v) {
      $("view-" + v).hidden = v !== view;
      $("tab-" + v).setAttribute("aria-selected", String(v === view));
    });
    $("filterbar").hidden = view === "sources";
    $("status").hidden = view === "sources";
    render();
    if (view === "map") {
      var p = initMap();
      if (map) setTimeout(function () { map.invalidateSize(); }, 0);
      return p;
    }
    return Promise.resolve();
  }

  var FIELDS = { q: "q", cuisine: "f-cuisine", area: "f-area", price: "f-price", confidence: "f-confidence",
                 type: "f-type", sort: "f-sort" };

  function writeHash() {
    var params = new URLSearchParams();
    Object.keys(FIELDS).forEach(function (k) { if (state[k] && !(k === "sort" && state[k] === "score")) params.set(k, state[k]); });
    if (state.view !== "list") params.set("view", state.view);
    var h = params.toString();
    if (("#" + h) !== location.hash && !(h === "" && location.hash === "")) {
      history.replaceState(null, "", h ? "#" + h : location.pathname + location.search);
    }
  }

  function readHash() {
    var params = new URLSearchParams(location.hash.slice(1));
    Object.keys(FIELDS).forEach(function (k) {
      var v = params.get(k); if (v == null) return;
      var el = $(FIELDS[k]);
      if (el.tagName === "SELECT" && !Array.prototype.some.call(el.options, function (o) { return o.value === v; })) return;
      state[k] = v; el.value = v;
    });
    var view = params.get("view");
    return view === "map" || view === "sources" ? view : "list";
  }

  function resetFilters() {
    Object.keys(FIELDS).forEach(function (k) { state[k] = k === "sort" ? "score" : ""; $(FIELDS[k]).value = state[k]; });
    state.shown = PAGE;
    render();
  }

  function bind() {
    var t;
    $("q").addEventListener("input", function (e) {
      clearTimeout(t);
      t = setTimeout(function () { state.q = e.target.value; state.shown = PAGE; render(); }, 80);
    });
    Object.keys(FIELDS).forEach(function (k) {
      if (k === "q") return;
      $(FIELDS[k]).addEventListener("change", function (e) { state[k] = e.target.value; state.shown = PAGE; render(); });
    });
    $("reset").addEventListener("click", resetFilters);
    $("more").addEventListener("click", function () { state.shown += PAGE; renderList(); });
    $("filters-toggle").addEventListener("click", function () {
      var open = $("filters").classList.toggle("open");
      this.setAttribute("aria-expanded", String(open));
    });
    document.querySelectorAll(".tabs button").forEach(function (b) {
      b.addEventListener("click", function () { setView(b.getAttribute("data-view")); window.scrollTo(0, 0); });
    });
    $("cards").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-map]");
      if (b) focusOnMap(b.getAttribute("data-map"));
    });
    $("theme-toggle").addEventListener("click", function () {
      var root = document.documentElement;
      var dark = root.getAttribute("data-theme") ? root.getAttribute("data-theme") === "dark"
        : window.matchMedia("(prefers-color-scheme: dark)").matches;
      root.setAttribute("data-theme", dark ? "light" : "dark");
      try { localStorage.setItem("theme", root.getAttribute("data-theme")); } catch (err) { /* ignore */ }
    });
  }

  function restoreTheme() {
    try {
      var t = localStorage.getItem("theme");
      if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    } catch (err) { /* storage unavailable */ }
  }

  function renderChrome() {
    var month = fmtMonth(data.month);
    $("subtitle").textContent = "Consensus of " + data.n_sources + " published sources · " + month;
    $("footer-stats").textContent = "Ranking for " + month + ": " + data.n_places + " restaurants from " +
      data.n_sources + " sources. Method v" + (data.method_version || "1.0") +
      (data.generated_at ? " · generated " + String(data.generated_at).slice(0, 10) : "") + ".";
    renderSources();
  }

  restoreTheme();
  bind();
  fetch(DATA_URL, { cache: "no-cache" })
    .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(function (json) {
      prepare(json);
      renderChrome();
      setView(readHash());
    })
    .catch(function (err) {
      $("status").textContent = "Could not load the ranking data (" + err.message + ").";
    });
})();
