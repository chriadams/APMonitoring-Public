/* AP Monitor — topology map (switches + APs) */
(function () {
  'use strict';

  // Attach a custom header to same-origin state-changing requests so the server
  // can reject cross-site (CSRF) requests, which cannot set custom headers.
  const _nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = (init.method || (typeof input !== 'string' && input && input.method) || 'GET').toUpperCase();
    const sameOrigin = url.startsWith('/') || url.startsWith(window.location.origin);
    if (sameOrigin && method !== 'GET' && method !== 'HEAD') {
      init = { ...init, headers: { ...(init.headers || {}), 'X-Requested-With': 'fetch' } };
    }
    return _nativeFetch(input, init);
  };

  // HTML-escape a value for safe insertion into HTML text / quoted-attribute
  // contexts. Used wherever a user/DB-controlled string (device name, note,
  // etc.) is interpolated into an innerHTML template literal.
  const esc = v => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  const REFRESH_MS = 30_000;
  const AP_R = 10;
  const SW_W = 120;
  const SW_H = 40;
  // Over the aerial photo, switches shrink to a small rounded box close to the APs'
  // size (labels are hidden there, so the big labelled box isn't needed and it was
  // swamping the buildings). The plain grid keeps the full-size labelled box.
  const SW_W_GEO = 34;
  const SW_H_GEO = 22;
  const OTHER_S = 15;   // "other device" diamond size (square, rotated 45°)
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  let topology = { switches: [], devices: [] };
  let selectedIp = null;
  // Map view (zoom/pan) captured just before a device was focused from the list,
  // so closing the detail panel can animate back to where the user was.
  let preFocusTransform = null;

  // ── Edit Map mode + position persistence (server-side) ────────────
  // Positions are saved to the server (POST /api/devices/<ip>/position) so the
  // layout is shared across every browser/device and survives restarts. The
  // server serves them back via /api/topology (db.get_position → yaml → default),
  // so there is no client-side overlay — server x/y is authoritative.
  let editMode = false;
  // "Set map area" mode: drag a rectangle to redefine the visible part of the aerial
  // photo. Suppresses zoom-pan and node drag while active.
  let areaMode = false;
  // "Set map area" rubber-band: while dragging a rectangle we suppress zoom + drag.
  const drawingRect = () => areaMode;

  // Which stored map layout this client edits/reads: mobile phones get their own,
  // matching the ≤768px breakpoint used for the mobile UI.
  const mapLayout = () =>
    window.matchMedia('(max-width: 768px)').matches ? 'mobile' : 'desktop';

  // POST every node's current position to the server. Called on "Done".
  //
  // Over an aerial basemap, positions are saved as lat/lng (un-projected from the
  // world pixel the node was dragged to) so they stay correct if the imagery is ever
  // replaced or re-cropped. There is no desktop/mobile split for those — a building
  // is in one place. Otherwise this saves logical x/y per layout, as before.
  async function savePositions() {
    const nodes = [...topology.switches, ...topology.devices]
      .filter(n => Number.isFinite(n.x) && Number.isFinite(n.y));
    const geoMode = basemap.active;
    const layout = mapLayout();
    let failed = 0;
    await Promise.all(nodes.map(n => {
      let url, body;
      if (geoMode) {
        const [lat, lng] = fromPx(n.x, n.y);
        url = `/api/devices/${devRef(n)}/geo-position`;
        body = { lat, lng };
      } else {
        url = `/api/devices/${devRef(n)}/position`;
        body = { x: n.x, y: n.y, layout };
      }
      return fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }).then(resp => {
        if (resp.status === 401) { window.location = '/login'; return; }
        if (!resp.ok) failed++;
      }).catch(() => { failed++; });
    }));
    if (failed) {
      alert(`Couldn't save ${failed} device position(s) — the layout may not persist. Check your connection and try again.`);
    }
  }

  // ── SVG setup ─────────────────────────────────────────────────────
  const svg = d3.select('#map-svg');
  const container = svg.append('g').attr('class', 'pan-container');

  // Grid background
  const defs = svg.append('defs');
  defs.append('pattern')
    .attr('id', 'grid').attr('width', 40).attr('height', 40)
    .attr('patternUnits', 'userSpaceOnUse')
    .append('path').attr('d', 'M 40 0 L 0 0 0 40')
    .attr('fill', 'none').attr('stroke', '#12273f').attr('stroke-width', '1');

  // Arrowheads for switch→child-switch links, showing inheritance direction
  // (parent → child). One marker per line color (normal / down) so they match.
  // markerUnits is userSpaceOnUse, so over an aerial basemap (where nodes hold a
  // constant screen size) these are rescaled by 1/k in rescaleNodes.
  const ARROW_SIZE = 11;
  function addArrow(id, color) {
    defs.append('marker')
      .attr('id', id).attr('viewBox', '0 0 10 10')
      .attr('refX', 10).attr('refY', 5)
      .attr('markerWidth', ARROW_SIZE).attr('markerHeight', ARROW_SIZE)
      .attr('markerUnits', 'userSpaceOnUse').attr('orient', 'auto')
      .append('path').attr('d', 'M0,0 L10,5 L0,10 z').attr('fill', color);
  }
  addArrow('arrow-sw', '#a7bad2');
  addArrow('arrow-sw-down', '#ef4444');

  // Aerial-imagery layers, BELOW the grid and everything else. Order matters: these
  // are appended first so tiles paint under the links/nodes.
  //   tileGroup — the basemap tiles (see the basemap section below)
  //   scrim     — a dark wash over the photo so the green/red status colours stay
  //               legible against bright grass, roofs and water
  const tileGroup = container.append('g').attr('class', 'basemap-tiles');
  const scrim = container.append('rect').attr('class', 'basemap-scrim').attr('display', 'none');

  // Grid background. Sized to cover both coordinate spaces: the legacy layout (which
  // straddles the origin) and a basemap's image-pixel space (0..~7000).
  const gridRect = container.append('rect')
    .attr('class', 'grid-bg')
    .attr('x', -6000).attr('y', -6000).attr('width', 20000).attr('height', 20000)
    .attr('fill', 'url(#grid)');

  // Transparent capture layer for marquee multi-select in Edit Map. It sits BELOW the
  // links/nodes (appended before them) so node drags still work; pointer-events are
  // enabled only while in select mode (otherwise empty-space drags pan as usual).
  const selectLayer = container.append('rect').attr('class', 'select-layer')
    .attr('x', -6000).attr('y', -6000).attr('width', 20000).attr('height', 20000)
    .attr('fill', 'transparent').style('display', 'none');

  // Render layers (order = z-index)
  const linkGroup  = container.append('g').attr('class', 'links');
  const swGroup    = container.append('g').attr('class', 'switches');
  const apGroup    = container.append('g').attr('class', 'aps');
  const otherGroup = container.append('g').attr('class', 'others');

  // ── Zoom / pan ────────────────────────────────────────────────────
  // Two modes:
  //   plain — the original logical map: free pan, scale 0.15–4, node geometry in
  //           world units (so icons grow/shrink with the map).
  //   geo   — an aerial basemap underneath: pan clamped to the map area, the zoom
  //           floor is "the whole area fits", and nodes are counter-scaled so they
  //           hold a constant on-screen size (see nodeScale).
  const PLAIN_SCALE_EXTENT = [0.15, 4];
  // 1.0 = one world unit (one source-image pixel, ~0.21 m of ground) per CSS pixel,
  // i.e. native resolution — already close enough to read individual buildings. The
  // imagery holds no detail past that, so allow a modest 2x over-zoom for placing
  // icons precisely and stop there rather than letting people zoom into mush.
  const GEO_MAX_SCALE = 2;
  // Over the photo, node icons scale WITH the map but their on-screen size is clamped
  // to this band (as a multiple of their base world size) so they never shrink to an
  // unreadable sliver at the zoom-out floor nor swallow a whole building at the
  // ceiling. on-screen size ∝ k·nodeScale = clamp(k, MIN_MUL, MAX_MUL). See nodeScale.
  const GEO_NODE_MIN_MUL = 0.55;
  const GEO_NODE_MAX_MUL = 0.9;
  // How much empty background is allowed around the map area at full zoom-out. The
  // floor CONTAINS the map area (× this margin) rather than covering it, so you can
  // pull back and see the whole property at once with a border of dark background.
  const GEO_FLOOR_MARGIN = 1.18;
  let zoomK = 1;              // current zoom scale, mirrored for the render helpers
  let selectMode = false;     // Edit Map marquee sub-mode (drag empty space = select)

  const zoom = d3.zoom()
    .scaleExtent(PLAIN_SCALE_EXTENT)
    .filter(event => {
      if (drawingRect()) return false;
      // In marquee select mode, let the select layer capture empty-space drags (so
      // they draw a selection box instead of panning); wheel-zoom still works.
      const down = event.type === 'mousedown' || event.type === 'pointerdown'
        || event.type === 'touchstart';
      if (editMode && selectMode && down) return false;
      if (editMode) return !event.target.closest('.node')
        && !event.target.closest('.other-node') && !event.target.closest('.switch-node');
      return true;
    })
    .on('zoom', event => {
      container.attr('transform', event.transform);
      zoomK = event.transform.k;
      onZoomed(event.transform);
    });

  svg.call(zoom);

  // The visible map area in world units, or null outside geo mode.
  function viewRect() {
    return basemap.active ? basemap.viewPx : null;
  }

  function viewportSize() {
    const svgEl = document.getElementById('map-svg');
    const W = svgEl.clientWidth || 900;
    const H = svgEl.clientHeight || 700;
    // On mobile the fixed bottom toggle bar overlays the lower part of the map, so
    // reserve its height and fit/center the content into the visible area above it
    // (0 on desktop, where the toggle is display:none).
    const toggle = document.querySelector('.view-toggle');
    const bottomInset = (toggle && getComputedStyle(toggle).display !== 'none')
      ? toggle.offsetHeight : 0;
    return { W, H, availH: Math.max(100, H - bottomInset) };
  }

  // Smallest scale at which the map area still fills the viewport. Used as the geo
  // zoom floor so you can't zoom out into empty space around the imagery.
  //
  // Measured against the FULL height, not availH: the mobile toggle bar overlays the
  // map rather than shortening it, so the imagery has to cover the whole svg box or a
  // dark band shows through beneath the bar. (fitView still centres within availH so
  // the content isn't hidden behind it.)
  function geoFitScale() {
    const r = viewRect();
    if (!r) return PLAIN_SCALE_EXTENT[0];
    const { W, H } = viewportSize();
    return Math.max(W / (r.x1 - r.x0), H / (r.y1 - r.y0));
  }

  // The zoom FLOOR: how far out you can pull back. Unlike geoFitScale (which COVERS
  // the map area so the opening view has no dead space), this CONTAINS the whole map
  // area with a margin of empty background around it, so you can pull back and see
  // the entire property at once. translateExtent stays the map area, so below the
  // cover scale d3 centres it and the margin shows as dark background symmetrically —
  // panning is disabled there (nothing to pan to) rather than fighting the zoom.
  function geoFloorScale() {
    const r = viewRect();
    if (!r) return PLAIN_SCALE_EXTENT[0];
    const { W, H } = viewportSize();
    return Math.min(W / ((r.x1 - r.x0) * GEO_FLOOR_MARGIN),
                    H / ((r.y1 - r.y0) * GEO_FLOOR_MARGIN));
  }

  // Re-apply the zoom limits for the current mode + viewport. Called on basemap
  // load/toggle, when the map area changes, and on resize.
  function applyZoomLimits() {
    if (basemap.active) {
      const floor = geoFloorScale();
      zoom.scaleExtent([floor, Math.max(floor, GEO_MAX_SCALE)]);
      const r = viewRect();
      zoom.translateExtent([[r.x0, r.y0], [r.x1, r.y1]]);
    } else {
      zoom.scaleExtent(PLAIN_SCALE_EXTENT);
      zoom.translateExtent([[-Infinity, -Infinity], [Infinity, Infinity]]);
    }
  }

  // The camp property boundary (from the manifest) as a world-px rect, or null if the
  // basemap carries none. Lets the opening view frame the property (the whole camp)
  // rather than the full, much wider image — robust to where individual devices sit.
  function propertyRectPx() {
    const corners = basemap.cfg && basemap.cfg.georef && basemap.cfg.georef.property_corners_latlng;
    if (!hasBasemap() || !Array.isArray(corners) || !corners.length) return null;
    const pts = corners.map(c => toPx(c.lat, c.lng));
    const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
    return { x0: Math.min(...xs), y0: Math.min(...ys),
             x1: Math.max(...xs), y1: Math.max(...ys) };
  }

  // Fit the view: frame the camp property in geo mode (falling back to covering the
  // whole map area), or the node bounding box otherwise.
  function fitView() {
    // Bail while the map is hidden / zero-size (e.g. mobile lands on the device list,
    // so #map-container is display:none). viewportSize() would fall back to 900×700 and
    // fit to the wrong-shaped viewport, leaving the map off-centre once it's shown. The
    // mobile toggle re-calls fitView when the map becomes visible (with real dimensions).
    const svgEl = document.getElementById('map-svg');
    if (!svgEl || !svgEl.clientWidth || !svgEl.clientHeight) return;
    const { W, availH } = viewportSize();
    const r = viewRect();
    let minX, minY, maxX, maxY, scale;

    if (r) {
      // Phones frame a wide desktop view poorly, so they get their own saved view
      // (falling back to the desktop one, then the property frame).
      const cfg = basemap.cfg || {};
      const ov = (mapLayout() === 'mobile' && cfg.open_view_mobile) || cfg.open_view;
      const box = ov && ov.box;
      if (box && Number.isFinite(box.north) && Number.isFinite(box.south)
          && Number.isFinite(box.east) && Number.isFinite(box.west)) {
        // An admin drew an auto-crop rectangle. CONTAIN it in the CURRENT viewport —
        // compute the fit scale each time, so an odd/small window fits the more
        // constraining dimension and shows extra map on the other axis (rather than
        // baking in a zoom that only suited the window it was drawn on).
        const [x0, y0] = toPx(box.north, box.west);   // NW corner
        const [x1, y1] = toPx(box.south, box.east);   // SE corner
        minX = Math.min(x0, x1); maxX = Math.max(x0, x1);
        minY = Math.min(y0, y1); maxY = Math.max(y0, y1);
        scale = Math.min(W / (maxX - minX), availH / (maxY - minY));
        scale = Math.max(geoFloorScale(), Math.min(scale, GEO_MAX_SCALE));
      } else if (ov && Number.isFinite(ov.lat) && Number.isFinite(ov.lng) && ov.k > 0) {
        // Legacy saved view (centre lat/lng + fixed zoom) — reproduce it verbatim.
        const [cx, cy] = toPx(ov.lat, ov.lng);
        minX = maxX = cx; minY = maxY = cy;
        scale = Math.max(geoFloorScale(), Math.min(ov.k, GEO_MAX_SCALE));
      } else {
        const pr = propertyRectPx();
        if (pr) {
          // Open framed on the property, not the whole image — the imagery extends well
          // past the property for context, so covering it all would open far too zoomed
          // out. CONTAIN the property rect (small margin), then clamp to the zoom limits.
          const mx = (pr.x1 - pr.x0) * 0.06, my = (pr.y1 - pr.y0) * 0.06;
          minX = pr.x0 - mx; maxX = pr.x1 + mx;
          minY = pr.y0 - my; maxY = pr.y1 + my;
          scale = Math.min(W / (maxX - minX), availH / (maxY - minY));
          scale = Math.max(geoFloorScale(), Math.min(scale, GEO_MAX_SCALE));
        } else {
          // No property polygon: COVER the whole map area (fills the viewport).
          ({ x0: minX, y0: minY, x1: maxX, y1: maxY } = r);
          scale = geoFitScale();
        }
      }
    } else {
      const all = [...topology.switches, ...topology.devices];
      if (!all.length) return;
      const xs = all.map(d => d.x);
      const ys = all.map(d => d.y);
      minX = Math.min(...xs) - 120;
      minY = Math.min(...ys) - 60;
      maxX = Math.max(...xs) + 120;
      maxY = Math.max(...ys) + 60;
      scale = Math.min(0.9, W / (maxX - minX), availH / (maxY - minY));
    }

    const tx = (W - scale * (minX + maxX)) / 2;
    const ty = (availH - scale * (minY + maxY)) / 2;
    applyZoomLimits();
    svg.call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale));
  }

  // Over the aerial photo, nodes SCALE WITH the map (so a switch box stays sized
  // relative to the building it sits on) but with the on-screen size clamped to a
  // readable band: fully world-locked sizing shrinks labels to unreadable slivers at
  // the zoom-out floor and lets a box swallow a whole building at the ceiling. This
  // hybrid keeps the icon proportional to the imagery through the normal range and
  // only clamps at the extremes (on-screen size ∝ k·nodeScale = clamp(k, MIN, MAX)).
  // In plain mode this is 1 — geometry stays in world units exactly as before, so the
  // legacy grid map is untouched.
  const nodeScale = () => basemap.active
    ? clamp(zoomK, GEO_NODE_MIN_MUL, GEO_NODE_MAX_MUL) / zoomK
    : 1;
  const nodeTransform = d => basemap.active
    ? `translate(${d.x},${d.y}) scale(${nodeScale()})`
    : `translate(${d.x},${d.y})`;

  // ── Aerial basemap ────────────────────────────────────────────────
  // A location can have a georeferenced aerial photo under the map, so devices sit
  // on the buildings they're actually in. The photo is served as a tile pyramid
  // (static/basemap/<site>/v<n>/, built by maps/build_tiles.py) rather than one
  // 44-megapixel JPEG, which would be a 17 MB download and a decode that fails on
  // mobile Safari.
  //
  // WORLD SPACE in geo mode is level-0 image pixels: (0,0) at the raster's
  // top-left. Device positions are stored as lat/lng and projected here at render
  // time, so replacing the imagery (a sharper photo, a different crop) doesn't move
  // any device. app/geo.py holds the same formulas server-side.
  const MERC_R = 6378137;
  const mercX = lng => MERC_R * lng * Math.PI / 180;
  const mercY = lat => MERC_R * Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
  const invMercX = x => x / MERC_R * 180 / Math.PI;
  const invMercY = y => (2 * Math.atan(Math.exp(y / MERC_R)) - Math.PI / 2) * 180 / Math.PI;

  // Photo visibility is a per-browser display preference, not shared state. Demo
  // mode forces it off — an aerial view of the camp identifies it as plainly as its
  // name would.
  const PHOTO_KEY = 'apmon.basemap.on';
  const basemap = {
    cfg: null,        // /api/basemap payload, null until fetched (or {} if none)
    active: false,    // is the photo currently drawn (configured AND toggled on)
    // Resolved in loadBasemap rather than here: DEMO is declared further down, and
    // reading it during this object literal would hit its temporal dead zone.
    on: true,
    viewPx: null,     // the map area as a world-space rect {x0,y0,x1,y1}
    resX: 1, resY: 1, // EPSG:3857 metres per level-0 pixel
  };

  const hasBasemap = () => !!(basemap.cfg && basemap.cfg.tile_base);

  function toPx(lat, lng) {
    const b = basemap.cfg.georef.epsg3857_bbox;
    return [(mercX(lng) - b.xmin) / basemap.resX, (b.ymax - mercY(lat)) / basemap.resY];
  }

  function fromPx(x, y) {
    const b = basemap.cfg.georef.epsg3857_bbox;
    return [invMercY(b.ymax - y * basemap.resY), invMercX(b.xmin + x * basemap.resX)];
  }

  function viewToPx(view) {
    const [x0, y0] = toPx(view.north, view.west);
    const [x1, y1] = toPx(view.south, view.east);
    return { x0: Math.min(x0, x1), y0: Math.min(y0, y1),
             x1: Math.max(x0, x1), y1: Math.max(y0, y1) };
  }

  // Recompute the derived projection values after cfg or the map area changes.
  function refreshBasemapGeometry() {
    if (!hasBasemap()) { basemap.active = false; basemap.viewPx = null; return; }
    const g = basemap.cfg.georef;
    const b = g.epsg3857_bbox;
    basemap.resX = (b.xmax - b.xmin) / g.width_px;
    basemap.resY = (b.ymax - b.ymin) / g.height_px;
    basemap.viewPx = viewToPx(basemap.cfg.view);
    basemap.active = basemap.on;
  }

  async function loadBasemap() {
    // Demo mode forces the photo off; otherwise honour the saved per-browser choice
    // (default on).
    basemap.on = !DEMO && localStorage.getItem(PHOTO_KEY) !== '0';
    try {
      const resp = await fetch('/api/basemap?' + siteQuery());
      basemap.cfg = resp.ok ? await resp.json() : {};
    } catch { basemap.cfg = {}; }
    refreshBasemapGeometry();
    updateBasemapChrome();
    // On the very first load over a photo, apply the opening (auto-crop) transform
    // BEFORE the tiles first paint — otherwise the map briefly shows the image's
    // top-left corner at native zoom until fetchAndRender's fitView (100ms later)
    // repositions it. Nodes aren't rendered yet, but fitView only needs the
    // georeferencing, so this is safe. The later fitView re-applies the same view.
    if (firstLoad && basemap.active) fitView();
    drawTiles();
  }

  // Project stored lat/lng onto world coordinates. Called on every render before
  // drawing, so a device that was dragged (or added) shows up in the right place.
  // A device with no lat/lng yet — one added after the initial seed — is placed just
  // below its parent switch so it's findable rather than stacked at (0,0).
  function projectNodes(data) {
    if (!basemap.active) return;
    const swByName = {};
    (data.switches || []).forEach(s => {
      if (s.lat != null && s.lng != null) {
        [s.x, s.y] = toPx(s.lat, s.lng);
      } else {
        const c = basemap.viewPx;
        s.x = (c.x0 + c.x1) / 2; s.y = (c.y0 + c.y1) / 2;
      }
      swByName[s.name] = s;
    });
    const unplaced = {};
    (data.devices || []).forEach(d => {
      if (d.lat != null && d.lng != null) { [d.x, d.y] = toPx(d.lat, d.lng); return; }
      const sw = swByName[d.switch_name];
      const base = sw || { x: (basemap.viewPx.x0 + basemap.viewPx.x1) / 2,
                          y: (basemap.viewPx.y0 + basemap.viewPx.y1) / 2 };
      const i = (unplaced[d.switch_name] = (unplaced[d.switch_name] || 0) + 1);
      // Fan unplaced siblings out so several new devices don't land on each other.
      const angle = (i - 1) * (Math.PI / 4) - Math.PI / 2;
      d.x = base.x + 90 * Math.cos(angle);
      d.y = base.y + 90 * Math.sin(angle);
    });
  }

  // ── Tile layer ────────────────────────────────────────────────────
  // Draw only the tiles that are visible, at the level matching the current zoom.
  // Level 0 is full resolution; each level up is a 2x downscale — so at k=0.25 the
  // level-2 tiles are ~1 screen pixel per tile pixel and finer levels would be
  // wasted bytes.
  let tileFrame = 0;

  function tileLevel(k) {
    const levels = basemap.cfg.levels.length;
    return Math.max(0, Math.min(levels - 1, Math.floor(Math.log2(1 / Math.max(k, 1e-6)))));
  }

  // Tiles for one level intersecting the visible world rect, as data for the join.
  function tilesFor(level, vis) {
    const meta = basemap.cfg.levels[level];
    const ts = basemap.cfg.tile_size;
    const world = ts * meta.scale;         // one tile's footprint in world units
    const out = [];
    const c0 = Math.max(0, Math.floor(vis.x0 / world));
    const c1 = Math.min(meta.cols - 1, Math.floor(vis.x1 / world));
    const r0 = Math.max(0, Math.floor(vis.y0 / world));
    const r1 = Math.min(meta.rows - 1, Math.floor(vis.y1 / world));
    for (let row = r0; row <= r1; row++) {
      for (let col = c0; col <= c1; col++) {
        // Right/bottom edge tiles are partial (not padded), so size them from the
        // level's true pixel dims — otherwise the imagery would stretch.
        const w = Math.min(ts, meta.width_px - col * ts) * meta.scale;
        const h = Math.min(ts, meta.height_px - row * ts) * meta.scale;
        out.push({ key: `${level}/${col}/${row}`, level, col, row,
                   x: col * world, y: row * world, w, h });
      }
    }
    return out;
  }

  function drawTiles() {
    if (!basemap.active) {
      tileGroup.selectAll('image').remove();
      tileGroup.attr('display', 'none');
      scrim.attr('display', 'none');
      gridRect.attr('display', null);
      return;
    }
    tileGroup.attr('display', null);
    gridRect.attr('display', 'none');

    const t = d3.zoomTransform(svg.node());
    const { W, H } = viewportSize();
    // Visible world rect, with a margin so panning reveals ready tiles rather than
    // blank space.
    const pad = 256 / t.k;
    const vis = {
      x0: (-t.x) / t.k - pad, y0: (-t.y) / t.k - pad,
      x1: (W - t.x) / t.k + pad, y1: (H - t.y) / t.k + pad,
    };

    const level = tileLevel(t.k);
    const coarsest = basemap.cfg.levels.length - 1;
    // Always keep the coarsest level mounted underneath (4 tiles, ~150 KB): it fills
    // any gap while a finer tile is still decoding, so the map never flashes empty.
    const data = level === coarsest
      ? tilesFor(coarsest, vis)
      : [...tilesFor(coarsest, vis), ...tilesFor(level, vis)];

    const base = basemap.cfg.tile_base;
    const ext = basemap.cfg.tile_ext || 'jpg';
    const sel = tileGroup.selectAll('image').data(data, d => d.key);
    sel.enter().append('image')
      .attr('href', d => `${base}/l${d.level}/${d.col}_${d.row}.${ext}`)
      .attr('preserveAspectRatio', 'none')
      .merge(sel)
      .attr('x', d => d.x).attr('y', d => d.y)
      .attr('width', d => d.w).attr('height', d => d.h);
    sel.exit().remove();

    // The scrim covers the imagery, not the infinite canvas, so panning past the
    // edge shows plain background rather than a dimmed void.
    const g = basemap.cfg.georef;
    scrim.attr('display', null)
      .attr('x', 0).attr('y', 0)
      .attr('width', g.width_px).attr('height', g.height_px);
  }

  // Everything that has to follow the zoom transform. Coalesced into one animation
  // frame so a fast pinch/wheel doesn't run the tile join dozens of times.
  function onZoomed() {
    if (tileFrame) return;
    tileFrame = requestAnimationFrame(() => {
      tileFrame = 0;
      drawTiles();
      rescaleNodes();
    });
  }

  // Re-apply the 1/k counter-scale to nodes, and the screen-space geometry that
  // depends on it (link trim points and arrowhead size). In plain mode nodeScale() is
  // 1, so this restores the original world-unit geometry — it must still run there,
  // or an arrowhead sized during a geo session would stay oversized after the photo
  // is switched off.
  function rescaleNodes() {
    const s = nodeScale();
    swGroup.selectAll('g.switch-node').attr('transform', nodeTransform);
    apGroup.selectAll('g.node').attr('transform', nodeTransform);
    otherGroup.selectAll('g.other-node').attr('transform', nodeTransform);
    // Arrowheads are in userSpaceOnUse units, so they need the same treatment.
    const a = ARROW_SIZE * s;
    defs.selectAll('marker').attr('markerWidth', a).attr('markerHeight', a);
    positionAllSwLinks();
  }

  // ── Utility ───────────────────────────────────────────────────────
  const cls = ip => 'n' + ip.replace(/\./g, '-');
  const statusColor = s => s === 'up' ? '#22c55e' : s === 'down' ? '#ef4444'
    : s === 'disabled' ? '#a855f7' : '#6b7280';

  // Timestamps are stored as naive-UTC ISO strings (no offset). Append 'Z' so
  // they parse as UTC, then render in US Eastern — America/New_York tracks
  // EST/EDT automatically — with the zone label shown.
  const TZ = 'America/New_York';
  // ── Demo mode (window.DEMO, set by DEMO_MODE=1) ───────────────────────────
  // Abstracts device/location/event identifiers for a public-shareable video. It's
  // display-only: the real `ip` stays every lookup key, so routing/API are unchanged.
  // Devices get a STABLE number from their sorted-by-IP position so the same device
  // reads as the same "Device N" across polls.
  const DEMO = window.DEMO === true;
  // Demo-DATA build (DEMO_DATA=1): everything on screen is fake sample data. Show a
  // one-time disclaimer popup on load so nobody mistakes it for a live system.
  const DEMO_DATA = window.DEMO_DATA === true;
  if (DEMO_DATA) {
    const demoOverlay = document.getElementById('demo-overlay');
    if (demoOverlay) {
      demoOverlay.hidden = false;
      const dismiss = () => { demoOverlay.hidden = true; };
      document.getElementById('demo-ack')?.addEventListener('click', dismiss);
      demoOverlay.addEventListener('click', e => { if (e.target === demoOverlay) dismiss(); });
      document.addEventListener('keydown', function esc(e) {
        if (e.key === 'Escape') { dismiss(); document.removeEventListener('keydown', esc); }
      });
    }
  }
  // Admin (edit-capable) vs viewer (read-only). Non-admins get a view-only UI: every
  // edit control is withheld here, and the server independently rejects mutations
  // (so hiding is purely cosmetic — bypassing the JS still hits a 403).
  const IS_ADMIN_BASE = window.IS_ADMIN !== false;   // server-granted admin (default true in open/dev mode)
  // Admins can preview the read-only (viewer) UI without logging out. This only hides
  // edit affordances client-side; the server still independently rejects mutations.
  let previewViewer = IS_ADMIN_BASE && sessionStorage.getItem('apmon.previewViewer') === '1';
  const isAdmin = () => IS_ADMIN_BASE && !previewViewer;
  let _demoSig = '';
  const _demoMap = new Map();   // ip -> { label, id }
  function _buildDemoMap() {
    _demoMap.clear();
    [...topology.switches].sort((a, b) => a.ip.localeCompare(b.ip))
      .forEach((s, i) => _demoMap.set(s.ip, { label: `Switch ${i + 1}`, id: `switch-${i + 1}-id` }));
    [...topology.devices].sort((a, b) => a.ip.localeCompare(b.ip))
      .forEach((d, i) => _demoMap.set(d.ip, { label: `Device ${i + 1}`, id: `device-${i + 1}-id` }));
  }
  function _demoEntry(node) {
    const sig = `${topology.switches.length}:${topology.devices.length}`;
    if (sig !== _demoSig) { _buildDemoMap(); _demoSig = sig; }
    return _demoMap.get(node.ip) || { label: 'Device', id: 'device-id' };
  }
  const demoName = node => _demoEntry(node).label;   // "Switch N" / "Device N"
  const demoId = node => _demoEntry(node).id;        // "device-N-id"

  // The user-facing device label is its friendly "Name" (the `location` field),
  // falling back to the unique "Device ID" (`name`) when no Name is set.
  const displayName = n => DEMO ? demoName(n) : ((n.location && n.location.trim()) || n.name);
  // Centered spinner markup for a log pane while its data loads.
  const LOG_LOADING = '<div class="log-loading"><span class="log-spinner"></span></div>';
  // A device with Slack notifications paused but still monitored (enabled). Monitoring
  // pauses (enabled=false) turn notify back on, so this is only the notifications case.
  const notifPaused = n => n.notify === false && n.enabled !== false;

  // Small transient popup (bottom-right). `kind` tints the accent bar (up/down/info).
  // Auto-dismisses; click to dismiss early. textContent keeps it XSS-safe and renders
  // "\n" as a line break (CSS white-space: pre-line).
  function showToast(message, kind = 'info', ms = 7000) {
    let host = document.getElementById('toast-host');
    if (!host) { host = document.createElement('div'); host.id = 'toast-host'; document.body.appendChild(host); }
    const el = document.createElement('div');
    el.className = 'toast toast-' + kind;
    el.textContent = message;
    host.appendChild(el);
    requestAnimationFrame(() => el.classList.add('show'));
    const remove = () => { el.classList.remove('show'); setTimeout(() => el.remove(), 250); };
    const t = setTimeout(remove, ms);
    el.addEventListener('click', () => { clearTimeout(t); remove(); });
    return el;
  }

  const fmtTime = iso => iso
    ? new Date(iso + 'Z').toLocaleTimeString('en-US', { timeZone: TZ, timeZoneName: 'short' })
    : '—';
  const fmtDateTime = iso => iso
    ? new Date(iso + 'Z').toLocaleString('en-US', { timeZone: TZ, timeZoneName: 'short' })
    : 'Never';
  const nowTime = () =>
    new Date().toLocaleTimeString('en-US', { timeZone: TZ, timeZoneName: 'short' });

  // Endpoints for a switch→child-switch link: from the parent's center to a
  // point just outside the child switch box, so the inheritance arrowhead is
  // visible at the box edge instead of hidden under it.
  //
  // The box half-extents are SCREEN sizes, so they must be converted to world units
  // (÷ k) before being compared against world-space deltas. In plain mode nodeScale()
  // is 1 and this reduces to the original arithmetic.
  function swLinkPoints(parent, child) {
    const dx = parent.x - child.x, dy = parent.y - child.y;
    const s = nodeScale();
    const hw = (SW_W / 2 + 4) * s, hh = (SW_H / 2 + 4) * s;
    let t = Infinity;
    if (dx !== 0) t = Math.min(t, hw / Math.abs(dx));
    if (dy !== 0) t = Math.min(t, hh / Math.abs(dy));
    if (!isFinite(t)) t = 0;
    // Clamp so a very close pair (or deep zoom, where the box is large in world
    // units) can't overshoot past the parent and draw a backwards arrow.
    t = Math.min(t, 1);
    return { x1: parent.x, y1: parent.y, x2: child.x + dx * t, y2: child.y + dy * t };
  }

  // Recompute a single switch→parent link's geometry (used during drag).
  function positionSwLink(child) {
    const parent = topology.switches.find(s => s.name === child.uplink);
    if (!parent) return;
    const p = swLinkPoints(parent, child);
    linkGroup.select(`line.sl-${cls(child.ip)}`)
      .attr('x1', p.x1).attr('y1', p.y1).attr('x2', p.x2).attr('y2', p.y2);
  }

  // All of them — the trim points depend on the zoom scale in geo mode, so they're
  // recomputed on zoom as well as on drag.
  function positionAllSwLinks() {
    (topology.switches || []).forEach(s => { if (s.uplink) positionSwLink(s); });
  }

  // ── Multi-select + group move (Edit Map) ──────────────────────────
  // `selected` holds the IPs currently picked out with the marquee. Dragging any one
  // of them moves the whole set together; dragging an un-selected node starts a fresh
  // single move (and clears the selection). All selections are data-bound, so no
  // per-node id classes are needed.
  const selected = new Set();
  let groupDrag = null;          // { ex, ey, items:[{n,sx,sy}] } while moving a group
  const allNodes = () => [...(topology.switches || []), ...(topology.devices || [])];

  function applySelectionClasses() {
    swGroup.selectAll('g.switch-node').classed('group-selected', s => selected.has(s.ip));
    apGroup.selectAll('g.node').classed('group-selected', d => selected.has(d.ip));
    otherGroup.selectAll('g.other-node').classed('group-selected', d => selected.has(d.ip));
  }
  function clearSelection() { selected.clear(); applySelectionClasses(); }

  // Re-apply transforms to every node and re-anchor all links — used on a group drag,
  // where any number of switches/APs move at once.
  function refreshMovedGeometry() {
    swGroup.selectAll('g.switch-node').attr('transform', nodeTransform);
    apGroup.selectAll('g.node').attr('transform', nodeTransform);
    otherGroup.selectAll('g.other-node').attr('transform', nodeTransform);
    const swByName = Object.fromEntries((topology.switches || []).map(s => [s.name, s]));
    linkGroup.selectAll('line.ap-link').each(function (d) {
      const sw = swByName[d.switch_name];
      d3.select(this).attr('x1', sw ? sw.x : 600).attr('y1', sw ? sw.y : 400)
        .attr('x2', d.x).attr('y2', d.y);
    });
    positionAllSwLinks();
  }

  // ── Drag: any node (AP / switch / other), single or as a selected group ──
  // Over the aerial photo a switch moves alone (independent placement); on the plain
  // grid a single switch still carries its child APs. A selected group moves exactly
  // the picked nodes, in either mode.
  function makeNodeDrag() {
    return d3.drag()
      .filter(() => editMode && !drawingRect())
      .on('start', function (event, d) {
        d3.select(this).raise();
        if (selected.has(d.ip) && selected.size > 1) {
          groupDrag = { ex: event.x, ey: event.y,
            items: allNodes().filter(n => selected.has(n.ip)).map(n => ({ n, sx: n.x, sy: n.y })) };
          return;
        }
        groupDrag = null;
        if (selected.size) clearSelection();   // dragging outside the selection resets it
        d._dragStartX = d.x; d._dragStartY = d.y;
        if (d.type === 'switch' && !basemap.active) {
          topology.devices.filter(x => x.switch_name === d.name)
            .forEach(x => { x._startX = x.x; x._startY = x.y; });
        }
      })
      .on('drag', function (event, d) {
        if (groupDrag) {
          const dx = event.x - groupDrag.ex, dy = event.y - groupDrag.ey;
          groupDrag.items.forEach(({ n, sx, sy }) => { n.x = sx + dx; n.y = sy + dy; });
          refreshMovedGeometry();
          return;
        }
        d.x = event.x; d.y = event.y;
        d3.select(this).attr('transform', nodeTransform(d));
        if (d.type === 'switch') {
          if (!basemap.active) {
            const dx = event.x - d._dragStartX, dy = event.y - d._dragStartY;
            topology.devices.filter(x => x.switch_name === d.name).forEach(x => {
              x.x = x._startX + dx; x.y = x._startY + dy;
              (x.type === 'other' ? otherGroup : apGroup).select(`g.${cls(x.ip)}`)
                .attr('transform', nodeTransform(x));
              linkGroup.select(`line.${cls(x.ip)}`)
                .attr('x1', d.x).attr('y1', d.y).attr('x2', x.x).attr('y2', x.y);
            });
          }
          positionSwLink(d);
          topology.switches.filter(s => s.uplink === d.name).forEach(c => positionSwLink(c));
        } else {
          linkGroup.select(`line.${cls(d.ip)}`).attr('x2', d.x).attr('y2', d.y);
        }
      })
      .on('end', function () { groupDrag = null; });
  }

  // Marquee: in select mode, dragging empty space draws a box; nodes inside it become
  // the selection. A shift-drag adds to the current selection; a plain click clears it.
  let marqueeRect = null, marqueeStart = null;
  selectLayer.call(d3.drag()
    .filter(() => editMode && selectMode && !drawingRect())
    .on('start', (event) => {
      marqueeStart = [event.x, event.y];
      marqueeRect = container.append('rect').attr('class', 'marquee-rect')
        .attr('x', event.x).attr('y', event.y).attr('width', 0).attr('height', 0);
    })
    .on('drag', (event) => {
      if (!marqueeStart) return;
      marqueeRect
        .attr('x', Math.min(marqueeStart[0], event.x))
        .attr('y', Math.min(marqueeStart[1], event.y))
        .attr('width', Math.abs(event.x - marqueeStart[0]))
        .attr('height', Math.abs(event.y - marqueeStart[1]));
    })
    .on('end', (event) => {
      if (marqueeRect) { marqueeRect.remove(); marqueeRect = null; }
      if (!marqueeStart) return;
      const [x0, y0] = marqueeStart; marqueeStart = null;
      const minX = Math.min(x0, event.x), maxX = Math.max(x0, event.x);
      const minY = Math.min(y0, event.y), maxY = Math.max(y0, event.y);
      if (maxX - minX < 4 && maxY - minY < 4) { clearSelection(); return; }  // a click clears
      if (!(event.sourceEvent && event.sourceEvent.shiftKey)) selected.clear();
      allNodes().forEach(n => {
        if (Number.isFinite(n.x) && n.x >= minX && n.x <= maxX && n.y >= minY && n.y <= maxY)
          selected.add(n.ip);
      });
      applySelectionClasses();
    }));

  // ── Render ────────────────────────────────────────────────────────
  function render(data) {
    // Over an aerial basemap, world x/y is derived from each device's stored lat/lng
    // rather than the server's logical layout — must happen before anything reads
    // .x/.y below.
    projectNodes(data);
    topology = data;
    // Over the photo the always-on text labels are hidden (hover shows a status
    // tooltip); CSS keys off this class and unhides them while editing.
    svg.classed('photo-on', basemap.active);
    const { switches, devices } = data;
    const swByName = Object.fromEntries(switches.map(s => [s.name, s]));

    // Device → switch links (APs and 'other' devices that have a parent switch).
    // Devices with no valid parent draw no link (avoids dangling lines).
    const links = linkGroup.selectAll('line.ap-link')
      .data(devices.filter(d => d.switch_name && swByName[d.switch_name]), d => d.ip);

    links.enter().append('line')
      .attr('class', d => `ap-link ${cls(d.ip)}`)
      .merge(links)
      .attr('x1', d => swByName[d.switch_name]?.x ?? 600)
      .attr('y1', d => swByName[d.switch_name]?.y ?? 400)
      .attr('x2', d => d.x)
      .attr('y2', d => d.y)
      .attr('stroke', d => {
        const sw = swByName[d.switch_name];
        if (sw?.status === 'down') return '#ef4444';
        return '#a7bad2';
      })
      .attr('stroke-width', 1)
      .attr('stroke-dasharray', '4 3');

    links.exit().remove();

    // Over the aerial photo both link kinds are visual noise — that view is about
    // physical location, not logical topology — so hide them (the dashed device→switch
    // links here and the solid switch→uplink backbone below). On the plain grid both
    // are shown. togglePhoto refetches → render re-runs, so visibility follows the photo.
    linkGroup.selectAll('line.ap-link').attr('display', basemap.active ? 'none' : null);

    // Switch → parent-switch (uplink) links — solid, brighter, thicker so the
    // backbone topology reads clearly against the AP links.
    const swLinkData = switches.filter(s => s.uplink && swByName[s.uplink]);
    const swLinks = linkGroup.selectAll('line.sw-link')
      .data(swLinkData, s => s.ip);

    swLinks.enter().append('line')
      .attr('class', s => `sw-link sl-${cls(s.ip)}`)
      .merge(swLinks)
      .each(function (s) {
        const p = swLinkPoints(swByName[s.uplink], s);
        d3.select(this).attr('x1', p.x1).attr('y1', p.y1).attr('x2', p.x2).attr('y2', p.y2);
      })
      .attr('stroke', s => {
        const parent = swByName[s.uplink];
        return (s.status === 'down' || parent?.status === 'down') ? '#ef4444' : '#a7bad2';
      })
      .attr('stroke-width', 1.5)
      .attr('marker-end', s => {
        const parent = swByName[s.uplink];
        return (s.status === 'down' || parent?.status === 'down')
          ? 'url(#arrow-sw-down)' : 'url(#arrow-sw)';
      });

    swLinks.exit().remove();

    // Hide the solid switch→uplink backbone over the photo too (see the ap-link note).
    linkGroup.selectAll('line.sw-link').attr('display', basemap.active ? 'none' : null);

    // Switch nodes
    const swSel = swGroup.selectAll('g.switch-node')
      .data(switches, s => s.ip);

    const swEnter = swSel.enter().append('g')
      .attr('class', 'switch-node')
      .on('click', onSwClick)
      .on('mouseenter', showTooltip)
      .on('mouseleave', hideTooltip)
      .call(makeNodeDrag());

    swEnter.append('rect')
      .attr('x', -SW_W / 2).attr('y', -SW_H / 2)
      .attr('width', SW_W).attr('height', SW_H)
      .attr('rx', 6).attr('ry', 6);

    // The IP line was removed, so the name is now vertically centered in the box
    // (dy 0.32em is the standard single-line centering offset for text-anchor:middle).
    swEnter.append('text').attr('class', 'sw-label').attr('text-anchor', 'middle').attr('dy', '0.32em');
    swEnter.append('text').attr('class', 'sw-ip').attr('text-anchor', 'middle').attr('dy', 12);
    // Purple corner dot = Slack notifications paused (device is still monitored).
    swEnter.append('circle').attr('class', 'notif-dot').attr('r', 4.5)
      .attr('cx', SW_W / 2).attr('cy', -SW_H / 2);

    const swAll = swSel.merge(swEnter);
    swAll.attr('transform', nodeTransform);
    swAll.attr('class', s => `switch-node status-${s.status}${s.frequent ? ' frequent' : ''}${selectedIp === s.ip ? ' selected' : ''}`);
    // Small rounded box over the photo (AP-scale), full labelled box on the plain grid.
    const swBox = basemap.active ? { w: SW_W_GEO, h: SW_H_GEO, rx: 4 } : { w: SW_W, h: SW_H, rx: 6 };
    swAll.select('rect')
      .attr('x', -swBox.w / 2).attr('y', -swBox.h / 2)
      .attr('width', swBox.w).attr('height', swBox.h)
      .attr('rx', swBox.rx).attr('ry', swBox.rx);
    swAll.select('circle.notif-dot').attr('cx', swBox.w / 2).attr('cy', -swBox.h / 2);
    swAll.select('text.sw-label').text(s => DEMO ? demoName(s) : s.name);
    // IP is intentionally not shown on the map's switch labels (see the single-device
    // panel for a device's IP). Kept as an empty <text> node to avoid churn.
    swAll.select('text.sw-ip').text('');
    swAll.select('circle.notif-dot').attr('display', s => notifPaused(s) ? null : 'none');

    swSel.exit().remove();

    // AP nodes (circles) — kind 'ap' only
    const aps = devices.filter(d => d.type !== 'other');
    const apSel = apGroup.selectAll('g.node')
      .data(aps, d => d.ip);

    const apEnter = apSel.enter().append('g')
      .attr('class', d => `node ${cls(d.ip)}`)
      .on('click', onApClick)
      .on('mouseenter', showTooltip)
      .on('mouseleave', hideTooltip)
      .call(makeNodeDrag());

    apEnter.append('circle').attr('class', 'ring').attr('r', AP_R + 5);
    apEnter.append('circle').attr('class', 'bg').attr('r', AP_R);
    apEnter.append('text').attr('class', 'ap-label')
      .attr('text-anchor', 'middle').attr('dy', AP_R + 13);
    apEnter.append('circle').attr('class', 'notif-dot').attr('r', 3.5)
      .attr('cx', AP_R * 0.72).attr('cy', -AP_R * 0.72);

    const apAll = apSel.merge(apEnter);
    apAll.attr('transform', nodeTransform);
    apAll.attr('class', d => `node ${cls(d.ip)} status-${d.status}${d.frequent ? ' frequent' : ''}${selectedIp === d.ip ? ' selected' : ''}`);
    apAll.select('text.ap-label').text(d => DEMO ? demoName(d) : d.name);
    apAll.select('circle.notif-dot').attr('display', d => notifPaused(d) ? null : 'none');

    apSel.exit().remove();

    // Other devices (diamonds) — kind 'other'. Reuses the AP drag/click/link
    // logic; a rotated square distinguishes them from AP circles & switch rects.
    const others = devices.filter(d => d.type === 'other');
    const otSel = otherGroup.selectAll('g.other-node')
      .data(others, d => d.ip);

    const otEnter = otSel.enter().append('g')
      .attr('class', d => `other-node ${cls(d.ip)}`)
      .on('click', onApClick)
      .on('mouseenter', showTooltip)
      .on('mouseleave', hideTooltip)
      .call(makeNodeDrag());

    otEnter.append('rect').attr('class', 'bg')
      .attr('x', -OTHER_S / 2).attr('y', -OTHER_S / 2)
      .attr('width', OTHER_S).attr('height', OTHER_S)
      .attr('rx', 2).attr('transform', 'rotate(45)');
    otEnter.append('text').attr('class', 'ap-label')
      .attr('text-anchor', 'middle').attr('dy', OTHER_S + 6);
    otEnter.append('circle').attr('class', 'notif-dot').attr('r', 3.5)
      .attr('cx', OTHER_S * 0.6).attr('cy', -OTHER_S * 0.6);

    const otAll = otSel.merge(otEnter);
    otAll.attr('transform', nodeTransform);
    otAll.attr('class', d => `other-node ${cls(d.ip)} status-${d.status}${d.frequent ? ' frequent' : ''}${selectedIp === d.ip ? ' selected' : ''}`);
    otAll.select('text.ap-label').text(d => DEMO ? demoName(d) : d.name);
    otAll.select('circle.notif-dot').attr('display', d => notifPaused(d) ? null : 'none');

    otSel.exit().remove();

    // Arrowhead size tracks the node scale (it's in userSpaceOnUse units). Set it here
    // too, not only on zoom, so the first paint after a load or a photo toggle is
    // already right rather than waiting for the user to zoom.
    const arrow = ARROW_SIZE * nodeScale();
    defs.selectAll('marker').attr('markerWidth', arrow).attr('markerHeight', arrow);

    updateSidebar(switches, devices);
    updateHeaderStats(switches, devices);
    applySelectionClasses();
  }

  // ── Tooltip ───────────────────────────────────────────────────────
  const tooltip = document.getElementById('tooltip');

  function showTooltip(event, d) {
    if (editMode) return;   // suppress hover info while dragging nodes in Edit Map
    const sinceLabel = d.status === 'disabled' ? 'Monitoring'
      : d.status === 'up' ? 'Up since' : d.status === 'down' ? 'Down since' : 'Unknown since';
    const sinceVal = d.status === 'disabled' ? 'Paused'
      : (d.since ? fmtTime(d.since) : '—');
    const typeLabel = d.type === 'switch' ? 'Switch' : d.type === 'other' ? 'Other' : 'AP';
    let swLabel = '';
    if (d.type === 'ap' && d.switch_name) {
      const sw = topology.switches.find(s => s.name === d.switch_name);
      swLabel = DEMO ? (sw ? demoName(sw) : 'Switch')
                     : d.switch_name;
    }
    const extra = swLabel ? `<div class="tip-row"><span>Switch</span><span>${esc(swLabel)}</span></div>` : '';
    // Demo hides the IP and the location (both org-identifying); the friendly name is
    // already abstracted by displayName.
    const ipRow = DEMO ? '' : `<div class="tip-row"><span>IP</span><span>${esc(d.ip)}</span></div>`;
    const locRow = DEMO ? '' : `<div class="tip-row"><span>Location</span><span>${esc(d.location) || '—'}</span></div>`;

    tooltip.innerHTML = `
      <div class="tip-name">${esc(DEMO ? demoName(d) : d.name)} <small style="color:#90a9c3">${typeLabel}</small></div>
      ${ipRow}
      <div class="tip-row"><span>Status</span>
        <span style="color:${statusColor(d.status)};font-weight:600">${d.status.toUpperCase()}</span></div>
      ${locRow}
      ${extra}
      <div class="tip-row"><span>${sinceLabel}</span><span>${sinceVal}</span></div>`;
    tooltip.classList.add('visible');
    moveTooltip(event);
  }

  function hideTooltip() { tooltip.classList.remove('visible'); }

  svg.on('mousemove', event => { if (tooltip.classList.contains('visible')) moveTooltip(event); });

  function moveTooltip(event) {
    const x = event.clientX + 16, y = event.clientY + 16;
    tooltip.style.left = (x + tooltip.offsetWidth > window.innerWidth ? x - tooltip.offsetWidth - 32 : x) + 'px';
    tooltip.style.top  = (y + tooltip.offsetHeight > window.innerHeight ? y - tooltip.offsetHeight - 32 : y) + 'px';
  }

  // ── Click handlers ────────────────────────────────────────────────
  // Clicking a node on the map opens its detail panel AND zooms/centers on it — the
  // same behavior as clicking it in the devices list (focusDevice also records the
  // pre-zoom view so closing the panel returns there). Don't zoom while editing the
  // map, where a click is part of dragging/selecting, not a focus.
  function onSwClick(event, s) {
    event.stopPropagation();
    if (editMode) {
      selectedIp = s.ip;
      showDetail(s, topology.devices.filter(d => d.switch_name === s.name));
      highlightSwitch(s.ip);
    } else {
      window.focusDevice(s.ip);
    }
  }

  function onApClick(event, d) {
    event.stopPropagation();
    if (editMode) {
      selectedIp = d.ip;
      showDetail(d, []);
      document.querySelectorAll('.device-item').forEach(el =>
        el.classList.toggle('selected', el.dataset.ip === d.ip));
    } else {
      window.focusDevice(d.ip);
    }
  }

  // Close the detail panel and clear any selection/highlight. Called from the
  // panel's × button and from clicking empty map space.
  function closeDetail() {
    selectedIp = null;
    document.getElementById('detail-panel').classList.remove('visible');
    document.querySelectorAll('.device-item').forEach(el => el.classList.remove('selected'));
    swGroup.selectAll('.switch-node').classed('selected', false);
    swGroup.selectAll('.switch-node').classed('dimmed', false);
    apGroup.selectAll('.node').classed('dimmed', false);
    otherGroup.selectAll('.other-node').classed('dimmed', false);
    linkGroup.selectAll('.ap-link').classed('dimmed', false);
    linkGroup.selectAll('.sw-link').classed('dimmed', false);
    // Animate back to the view we were at before focusing this device.
    if (preFocusTransform) {
      const t = preFocusTransform;
      preFocusTransform = null;
      svg.transition().duration(600).call(zoom.transform, t);
    }
  }

  svg.on('click', closeDetail);

  function highlightSwitch(swIp) {
    const sw = topology.switches.find(s => s.ip === swIp);
    if (!sw) return;
    const connectedIps = new Set(
      topology.devices.filter(d => d.switch_name === sw.name).map(d => d.ip)
    );
    // Related switches: this one, its parent, and its direct children.
    const relatedSwIps = new Set([swIp]);
    const parent = topology.switches.find(s => s.name === sw.uplink);
    if (parent) relatedSwIps.add(parent.ip);
    topology.switches.filter(s => s.uplink === sw.name).forEach(c => relatedSwIps.add(c.ip));

    swGroup.selectAll('.switch-node')
      .classed('selected', s => s.ip === swIp)
      .classed('dimmed', s => !relatedSwIps.has(s.ip));
    apGroup.selectAll('.node')
      .classed('dimmed', d => !connectedIps.has(d.ip));
    otherGroup.selectAll('.other-node')
      .classed('dimmed', d => !connectedIps.has(d.ip));
    linkGroup.selectAll('.ap-link')
      .classed('dimmed', d => !connectedIps.has(d.ip));
    // Keep this switch's own uplink line and its children's uplink lines lit.
    linkGroup.selectAll('.sw-link')
      .classed('dimmed', s => s.ip !== swIp && s.uplink !== sw.name);

    document.querySelectorAll('.device-item').forEach(el =>
      el.classList.toggle('selected', el.dataset.ip === swIp));
  }

  // ── Detail panel ──────────────────────────────────────────────────
  // Size/position the floating detail column to match the whole devices sidebar
  // (full height, not just the inner list) and sit directly to its left, over the
  // map. The panel has a fixed height + overflow-y:auto, so a short device view
  // just leaves empty space at the bottom. (On mobile a media query overrides this
  // to a full-screen overlay.)
  function positionDetail() {
    const panel = document.getElementById('detail-panel');
    if (!panel.classList.contains('visible')) return;
    const r = document.getElementById('sidebar').getBoundingClientRect();
    panel.style.top = `${r.top}px`;
    panel.style.left = `${r.left - r.width}px`;
    panel.style.width = `${r.width}px`;
    panel.style.height = `${r.height}px`;
  }
  window.addEventListener('resize', positionDetail);

  // IPs of everything downstream of a switch (child APs + child switches and
  // their devices, recursively). Mirrors the server's _descendant_ips.
  function descendantIps(switchNode) {
    const out = new Set(), seen = new Set([switchNode.name]), stack = [switchNode.name];
    while (stack.length) {
      const parent = stack.pop();
      topology.devices.forEach(d => { if (d.switch_name === parent) out.add(d.ip); });
      topology.switches.forEach(s => {
        if (s.uplink === parent && !seen.has(s.name)) { seen.add(s.name); out.add(s.ip); stack.push(s.name); }
      });
    }
    return [...out];
  }

  // "Up/Down since …" from the durable last-transition time (node.since) — accurate
  // across restarts and while an agent is silent, unlike the flaky last_check.
  function sinceRow(node) {
    if (node.status === 'disabled')
      return `<div class="detail-row"><span>Monitoring</span><span>Paused</span></div>`;
    const label = node.status === 'up' ? 'Up since'
      : node.status === 'down' ? 'Down since' : 'Unknown since';
    const val = node.since ? fmtDateTime(node.since) : 'Awaiting first check';
    return `<div class="detail-row"><span>${label}</span><span>${val}</span></div>`;
  }

  function showDetail(node, children) {
    const panel = document.getElementById('detail-panel');
    panel.classList.add('visible');
    const isSwitch = node.type === 'switch';
    const deleted = !!node.deleted;   // opened from the Deleted Devices page (read-only)
    const typeLabel = isSwitch ? 'Switch'
      : node.type === 'other' ? 'Other device' : 'Access Point';

    // Show the parent in the panel: for an AP it's its switch; for a switch
    // it's its uplink switch. Either links to that node's panel.
    let switchHtml = '';
    const parentName = isSwitch ? node.uplink : node.switch_name;
    if (parentName) {
      const parent = topology.switches.find(s => s.name === parentName);
      const parentVal = parent
        ? `<a href="#" onclick="focusDevice('${esc(parent.ip)}'); return false;">${esc(DEMO ? demoName(parent) : parent.name)}</a>`
        : esc(DEMO ? 'Switch' : parentName);
      const label = isSwitch ? 'Uplink switch' : 'Switch';
      switchHtml = `<div class="detail-row"><span>${label}</span><span>${parentVal}</span></div>`;
    }

    let childrenHtml = '';
    if (isSwitch) {
      // Child switches that uplink to this one.
      const childSwitches = topology.switches.filter(s => s.uplink === node.name);
      if (childSwitches.length) {
        childrenHtml += `
          <div class="detail-row"><span>Downlink switches</span><span>${childSwitches.length}</span></div>
          <div class="ap-child-list">
            ${childSwitches.map(s => `
              <div class="ap-child ${s.status}" onclick="focusDevice('${esc(s.ip)}')">
                <span class="device-status-dot ${s.status}"></span>
                ${esc(DEMO ? demoName(s) : s.name)}
              </div>`).join('')}
          </div>`;
      }
      if (children.length) {
        const up = children.filter(d => d.status === 'up').length;
        childrenHtml += `
          <div class="detail-row"><span>APs connected</span><span>${children.length} (${up} up)</span></div>
          <div class="ap-child-list">
            ${children.map(d => `
              <div class="ap-child ${d.status}" onclick="focusDevice('${esc(d.ip)}')">
                <span class="device-status-dot ${d.status}"></span>
                ${esc(DEMO ? demoName(d) : d.name)}
              </div>`).join('')}
          </div>`;
      }
    }

    panel.innerHTML = `
      <div class="detail-head">
        <h3>${esc(displayName(node))}</h3>
        <button class="detail-close" id="detail-close" title="Close">&times;</button>
      </div>
      <div class="detail-row"><span>Type</span><span>${typeLabel}</span></div>
      <div class="detail-row"><span>Device ID</span><span>${esc(DEMO ? demoId(node) : node.name)}</span></div>
      ${DEMO ? '' : `<div class="detail-row"><span>IP</span><span>${esc(node.ip)}</span></div>`}
      ${(!DEMO && node.hostname) ? `<div class="detail-row"><span>DNS</span><span class="detail-dns">${esc(node.hostname)}</span></div>` : ''}
      ${(!DEMO && node.intermapper_url) ? `<div class="detail-row"><span>Intermapper</span><span class="detail-intermapper"><a href="${esc(node.intermapper_url)}" target="_blank" rel="noopener noreferrer">View device in Intermapper ↗</a></span></div>` : ''}
      ${deleted
        ? `<div class="detail-row"><span>Status</span><span style="color:var(--text-muted);font-weight:600">DELETED</span></div>
           ${node.decommissioned_at ? `<div class="detail-row"><span>Removed</span><span>${fmtDateTime(node.decommissioned_at)}</span></div>` : ''}`
        : `<div class="detail-row"><span>Status</span>
             <span style="color:${statusColor(node.status)};font-weight:600">${node.status.toUpperCase()}</span></div>`}
      ${deleted ? '' : switchHtml}
      ${deleted ? '' : sinceRow(node)}
      ${deleted ? '' : childrenHtml}
      <div class="detail-activity">
        <button class="btn btn-soft" id="device-log">View recent activity</button>
      </div>
      ${(deleted && isAdmin()) ? `<div class="detail-actions">
        <button class="btn btn-edit" id="device-restore">Restore device</button>
      </div>` : ''}
      ${DEMO ? '' : `<div class="notes-block">
        <label for="device-note">Notes</label>
        <textarea id="device-note" maxlength="2000" ${(isAdmin() && !deleted) ? '' : 'readonly'}
          placeholder="${(isAdmin() && !deleted) ? 'Add a note (e.g. install details, known issues)…' : 'No notes.'}"></textarea>
        ${(isAdmin() && !deleted) ? `<div class="notes-actions">
          <button class="btn" id="note-save">Save note</button>
          <span class="note-status" id="note-status"></span>
        </div>` : ''}
      </div>`}
      ${(isAdmin() && !deleted) ? `<div class="detail-actions">
        ${node.enabled === false ? '' :
          `<button class="btn btn-soft" id="device-check-now" title="Ask the agent to re-ping this device now">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
            </svg>Check now</button>`}
        <button class="btn btn-edit" id="device-edit">Edit device</button>
      </div>
      <div class="detail-monitor">
        <button class="btn ${(node.enabled === false || node.notify === false) ? 'btn-resume' : 'btn-pause'}" id="device-toggle">${
          node.enabled === false ? 'Resume monitoring'
          : node.notify === false ? 'Resume notifications'
          : 'Pause monitoring'}</button>
        <button class="btn btn-schedule" id="device-event">Schedule event</button>
        ${node.notify === false && node.enabled !== false
          ? '<div class="detail-muted-note">🔕 Slack notifications paused — still monitored.</div>' : ''}
      </div>` : ''}`;

    if (!DEMO) loadNote(node.name);   // notes are hidden in demo mode (Device ID ref)
    const logBtn = document.getElementById('device-log');
    if (logBtn) logBtn.onclick = () => openDeviceLog(node);
    const evBtn = document.getElementById('device-event');
    if (evBtn) evBtn.onclick = () => openSchedForm(node.ip);
    const editBtnEl = document.getElementById('device-edit');
    if (editBtnEl) editBtnEl.onclick = () => openEditModal(node);

    // Restore a deleted device: bring it back to the fleet, then show it live.
    const restoreBtn = document.getElementById('device-restore');
    if (restoreBtn) restoreBtn.onclick = async () => {
      restoreBtn.disabled = true;
      restoreBtn.textContent = 'Restoring…';
      try {
        const resp = await fetch(`/api/devices/${devRef(node)}/restore?` + siteQuery(), { method: 'POST' });
        if (resp.status === 401) { window.location = '/login'; return; }
        if (!resp.ok) {
          const data = await resp.json().catch(() => ({}));
          showToast(data.error || 'Could not restore device.', 'down');
          restoreBtn.disabled = false; restoreBtn.textContent = 'Restore device';
          return;
        }
        closeDetail();
        await fetchAndRender();
        window.focusDevice(node.ip);        // open its live panel + center the map
        showToast(`${displayName(node)} restored.`, 'up', 4000);
      } catch (e) {
        showToast('Could not restore device.', 'down');
        restoreBtn.disabled = false; restoreBtn.textContent = 'Restore device';
      }
    };

    // Per-device "Check now": trigger the agent's next sweep (it re-pings its whole
    // target list, this device included) and watch THIS device's last_check advance,
    // then refresh the panel in place with the fresh status.
    const checkBtn = document.getElementById('device-check-now');
    if (checkBtn) checkBtn.onclick = async () => {
      const prevCheck = node.last_check || '';
      checkBtn.disabled = true;
      checkBtn.textContent = 'Checking…';
      // Targeted check: the agent pings ONLY this device (not the whole list).
      try {
        await fetch('/api/check-now?' + siteQuery(), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device: node.name }),
        });
      } catch (e) { /* the poll below still reflects any update */ }
      const deadline = Date.now() + 60000;   // agent polls ~every 20s; allow a minute
      const poll = async () => {
        // Panel was closed or the user navigated to another device — stop quietly.
        if (selectedIp !== node.ip ||
            !document.getElementById('detail-panel').classList.contains('visible')) return;
        await fetchAndRender();
        const fresh = topology.switches.find(s => s.ip === node.ip)
                   || topology.devices.find(d => d.ip === node.ip);
        if (fresh && (fresh.last_check || '') !== prevCheck) {
          // Got a fresh result — re-render the panel (no map pan) with new status,
          // then confirm with a popup.
          if (fresh.type === 'switch')
            showDetail(fresh, topology.devices.filter(d => d.switch_name === fresh.name));
          else showDetail(fresh, []);
          const nm = displayName(fresh);
          const st = fresh.status === 'disabled' ? 'paused (monitoring off)' : fresh.status;
          showToast(`Your requested check for ${nm} is complete.\n${nm} is ${st}.`,
                    fresh.status === 'up' ? 'up' : fresh.status === 'down' ? 'down' : 'info');
          return;
        }
        if (Date.now() < deadline) { setTimeout(poll, 4000); return; }
        // Timed out waiting for the agent — reset the button with a hint.
        checkBtn.disabled = false;
        checkBtn.textContent = 'No response yet — retry';
      };
      setTimeout(poll, 4000);
    };
    const toggleBtn = document.getElementById('device-toggle');
    if (toggleBtn) toggleBtn.onclick = () => {
      const setEnabled = async (val, includeChildren = false, mode = null) => {
        const body = { enabled: val, include_children: includeChildren };
        if (mode) body.mode = mode;
        const resp = await fetch(`/api/devices/${devRef(node)}/enabled`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (resp.status === 401) { window.location = '/login'; return false; }
        if (!resp.ok) throw new Error('Request failed');
        return true;
      };
      const isSwitch = node.type === 'switch';
      const childCount = isSwitch ? descendantIps(node).length : 0;
      const paused = node.enabled === false || node.notify === false;
      if (paused) {
        // Resuming is harmless — clears either pause kind; for a switch, bring its subtree back too.
        (async () => {
          toggleBtn.disabled = true;
          try { if (await setEnabled(true, isSwitch)) { await fetchAndRender(); window.focusDevice(node.ip); } }
          finally { toggleBtn.disabled = false; }
        })();
      } else {
        // Pausing — pick a mode; confirm first, or offer to schedule it instead.
        const childOpt = childCount > 0
          ? `<label class="confirm-check"><input type="checkbox" id="pause-children" checked>
             Also pause its ${childCount} connected device${childCount !== 1 ? 's' : ''}</label>`
          : '';
        openConfirm({
          title: `Pause for ${displayName(node)}?`,
          message: `<div class="pause-mode">
              <label><input type="radio" name="pause-mode" value="notifications" checked>
                <span class="pause-mode-text">
                  <span class="pause-mode-title">Slack notifications only</span>
                  <span class="pause-mode-desc">Stop Slack alerts for this device, with no change to this website.</span>
                </span></label>
              <label><input type="radio" name="pause-mode" value="monitoring">
                <span class="pause-mode-text">
                  <span class="pause-mode-title">Monitoring entirely</span>
                  <span class="pause-mode-desc">Status changes will be untracked. It will appear as 'paused' on this website.</span>
                </span></label>
            </div>${childOpt}`,
          confirmLabel: 'Pause now',
          onConfirm: async () => {
            const mode = document.querySelector('input[name="pause-mode"]:checked')?.value || 'monitoring';
            const withChildren = document.getElementById('pause-children')?.checked || false;
            if (await setEnabled(false, withChildren, mode)) {
              closeConfirm();
              await fetchAndRender();
              window.focusDevice(node.ip);
            }
          },
          extraLabel: 'Schedule event',
          onExtra: () => openSchedForm(node.ip),   // prefill the schedule form with this device
        });
      }
    };
    const closeBtn = document.getElementById('detail-close');
    if (closeBtn) closeBtn.onclick = (e) => { e.stopPropagation(); closeDetail(); };
    positionDetail();
  }

  // Open the confirm dialog to remove a node from the map / list / ping targets.
  // This is a soft delete: the device's logs are kept in Device Logs. Invoked from
  // the Edit modal's Delete button.
  function deleteNodeFlow(node) {
    const isSwitch = node.type === 'switch';
    const kind = isSwitch ? 'switch' : 'device';
    let extra = '';
    if (isSwitch) {
      const children = topology.devices.filter(d => d.switch_name === node.name);
      if (children.length) {
        extra = `<br><br>Its ${children.length} connected device(s) will remain on the map but lose their parent switch.`;
      }
    }
    openConfirm({
      title: `Delete ${kind}?`,
      message: `This will remove <strong>${esc(displayName(node))}</strong>${DEMO ? '' : ` (${esc(node.ip)})`} from the map, device list and ping targets. Its <strong>logs are kept</strong> in Device Logs.${extra}`,
      confirmLabel: `Delete ${kind}`,
      onConfirm: async () => {
        const resp = await fetch(`/api/devices/${devRef(node)}`, { method: 'DELETE' });
        if (resp.status === 401) { window.location = '/login'; return; }
        if (!resp.ok) {
          const data = await resp.json().catch(() => ({}));
          throw new Error(data.error || 'Delete failed');
        }
        closeConfirm();
        closeModal();          // close the Edit modal
        closeDetail();         // hide the detail panel
        await fetchAndRender();
      },
    });
  }

  // Notes are stored server-side per IP (works for APs and switches), fetched
  // lazily when the detail panel opens so /api/topology stays lean.
  async function loadNote(ref) {   // ref = Device ID (server resolves to the current IP)
    const ta = document.getElementById('device-note');
    const saveBtn = document.getElementById('note-save');
    const statusEl = document.getElementById('note-status');
    if (!ta) return;
    const encRef = encodeURIComponent(ref);
    try {
      const resp = await fetch(`/api/devices/${encRef}/note`);
      if (resp.status === 401) { window.location = '/login'; return; }
      const data = await resp.json();
      ta.value = data.note || '';
    } catch (e) { /* leave empty on failure */ }

    // Viewers (read-only) get the note text but no save controls.
    if (!saveBtn) return;

    // Only show "Save note" when the text differs from what's stored.
    let original = ta.value;
    const refresh = () => { saveBtn.style.display = (ta.value !== original) ? '' : 'none'; };
    refresh();
    ta.oninput = refresh;

    saveBtn.onclick = async () => {
      saveBtn.disabled = true;
      if (statusEl) statusEl.textContent = 'Saving…';
      try {
        const resp = await fetch(`/api/devices/${encRef}/note`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ note: ta.value }),
        });
        if (resp.status === 401) { window.location = '/login'; return; }
        if (resp.ok) original = ta.value;
        if (statusEl) statusEl.textContent = resp.ok ? 'Saved' : 'Error saving';
      } catch (e) {
        if (statusEl) statusEl.textContent = 'Error saving';
      } finally {
        saveBtn.disabled = false;
        refresh();   // hide again once saved (no remaining changes)
        if (statusEl) setTimeout(() => { statusEl.textContent = ''; }, 2000);
      }
    };
  }

  // ── Sidebar ───────────────────────────────────────────────────────
  // Section keys the user has collapsed (persists across the 30s re-render).
  // 'frequent' starts collapsed so the Frequent Outages list opens closed each page
  // load and only expands when the user clicks it open.
  const collapsedSections = new Set(['frequent']);

  // In the special sections (down / paused / notif / frequent), a switch whose every
  // connected device is in the same state collapses into one "… & connected devices"
  // row with a caret; these are the group ids currently expanded (default collapsed).
  const GROUPED_SECTIONS = new Set(['down', 'paused', 'notif', 'frequent']);
  const expandedGroups = new Set();
  window.toggleDeviceGroup = function (gid) {
    if (expandedGroups.has(gid)) expandedGroups.delete(gid);
    else expandedGroups.add(gid);
    updateSidebar(topology.switches, topology.devices);   // re-render caret + children
  };

  function updateSidebar(switches, devices) {
    const list = document.getElementById('device-list');
    const all = [...switches, ...devices];
    const byName = (a, b) => a.name.localeCompare(b.name);
    const badgeFor = d => d.type === 'switch' ? 'SW' : d.type === 'other' ? 'OTH' : '';

    // Currently-down devices first (so an active outage is the first thing you see);
    // then paused, notifications-paused, frequent, then every switch / AP / other
    // alphabetically. A down or frequent device therefore appears twice (special
    // section + its kind section).
    const down = all.filter(d => d.status === 'down').sort(byName);
    const paused = all.filter(d => d.status === 'disabled').sort(byName);
    // Notifications-paused devices are still monitored, so they also show in the
    // down/frequent/kind sections below — this section just gathers them together.
    const notifPausedList = all.filter(notifPaused).sort(byName);
    const frequent = all.filter(d => d.frequent && d.status !== 'down')
      .sort((a, b) => (b.outage_count - a.outage_count) || byName(a, b));
    const sws    = switches.slice().sort(byName);
    const aps    = devices.filter(d => d.type !== 'other').sort(byName);
    const others = devices.filter(d => d.type === 'other').sort(byName);

    // For a grouped section: any switch in the set whose EVERY connected device (its
    // APs/'other' children) is also in the set collapses into one head row + its
    // children nested beneath it. Un-grouped items render flat as before. DOM stays
    // flat (head + children are siblings) so applyFilters keeps iterating linearly.
    const groupedItems = (items, key) => {
      const inSet = new Set(items.map(d => d.ip));
      const kidsOf = {};                 // switch ip -> its in-set children (sorted)
      const consumed = new Set();        // child ips rendered under a switch head
      for (const d of items) {
        if (d.type !== 'switch') continue;
        const kids = topology.devices.filter(x => x.switch_name === d.name);
        if (kids.length && kids.every(k => inSet.has(k.ip))) {
          kidsOf[d.ip] = kids.slice().sort(byName);
          kids.forEach(k => consumed.add(k.ip));
        }
      }
      let html = '';
      for (const d of items) {
        if (consumed.has(d.ip)) continue;   // shown nested under its switch instead
        if (kidsOf[d.ip]) {
          const gid = `${key}:${d.ip}`;
          const expanded = expandedGroups.has(gid);
          html += deviceRow(d, badgeFor(d), key, { groupHead: gid, expanded });
          html += kidsOf[d.ip].map(c => deviceRow(c, badgeFor(c), key, { groupChild: gid })).join('');
        } else {
          html += deviceRow(d, badgeFor(d), key);
        }
      }
      return html;
    };

    const section = (label, items, key) => {
      if (!items.length) return '';
      const header = `<div class="sidebar-section-label${collapsedSections.has(key) ? ' collapsed' : ''}" data-section="${key}">
           <span class="sec-caret">${collapsedSections.has(key) ? '▸' : '▾'}</span>${label}</div>`;
      const body = GROUPED_SECTIONS.has(key)
        ? groupedItems(items, key)
        : items.map(d => deviceRow(d, badgeFor(d), key)).join('');
      return header + body;
    };

    // Currently-down at the very top (only rendered when outages exist), then the
    // paused sections, frequent outages, then the kind sections.
    list.innerHTML =
      section('CURRENTLY DOWN', down, 'down') +
      section('MONITORING PAUSED', paused, 'paused') +
      section('NOTIFICATIONS PAUSED', notifPausedList, 'notif') +
      section('FREQUENT OUTAGES', frequent, 'frequent') +
      section('SWITCHES', sws, 'switch') +
      section('ACCESS POINTS', aps, 'ap') +
      section('OTHER DEVICES', others, 'other');

    // Reapply any active filters/search across the rebuilt list (30s refresh).
    applyFilters();
  }

  // Checked values for a filter group (data-filter="<group>"). Checkboxes start
  // all-checked, so a row shows only when its value is checked in each group.
  const checkedValues = group =>
    new Set([...document.querySelectorAll(`input[data-filter="${group}"]:checked`)]
      .map(el => el.value));

  // Filter the sidebar rows by search text + the Type / Monitoring / Status
  // filter groups. Hides a section label when all its rows are hidden. A paused
  // device's visibility is governed by the Monitoring group (Paused), not the
  // Status group — its status is "disabled", which has no Status checkbox.
  function applyFilters() {
    const list = document.getElementById('device-list');
    if (!list) return;
    const q = (document.getElementById('device-search')?.value || '').trim().toLowerCase();
    const types = checkedValues('type');     // switch | ap | other
    const mons  = checkedValues('mon');      // active | paused
    const stats = checkedValues('status');   // up | down | unknown

    // When filtering to ONLY down, the kind sections already show exactly the
    // down devices, so the redundant "Currently Down" section is suppressed.
    const onlyDown = stats.size === 1 && stats.has('down');

    let currentLabel = null;
    let currentSection = null;
    let labelHasMatch = false;
    const matched = new Set();            // unique IPs matching filters (for the count)
    const finishSection = () => {
      // A section label shows whenever it has any filter-matching rows — even if
      // it's collapsed (so you can still see/expand it).
      if (currentLabel) currentLabel.style.display = labelHasMatch ? '' : 'none';
    };

    for (const el of list.children) {
      if (el.classList.contains('sidebar-section-label')) {
        finishSection();
        currentLabel = el;
        currentSection = el.dataset.section;
        labelHasMatch = false;
      } else if (el.classList.contains('device-item')) {
        const paused = el.dataset.enabled === '0';
        let ok = !q || (el.dataset.search || '').includes(q);
        if (ok) ok = types.has(el.dataset.type);
        if (ok) ok = mons.has(paused ? 'paused' : 'active');
        if (ok && !paused) ok = stats.has(el.dataset.status);
        if (ok && onlyDown && currentSection === 'down') ok = false;
        if (ok) { labelHasMatch = true; matched.add(el.dataset.ip); }
        // A group child is also hidden while its group is collapsed — unless a search
        // is active, when matching children are revealed regardless.
        const pg = el.dataset.groupParent;
        const groupHidden = pg && !q && !expandedGroups.has(pg);
        // A row is visible when it matches the filters AND its section isn't collapsed.
        el.style.display = (ok && !collapsedSections.has(currentSection) && !groupHidden) ? '' : 'none';
      }
    }
    finishSection();

    // While searching, report how many devices match (dedupe: a down device can
    // appear in both its kind section and "Currently Down"; count unique IPs, and
    // count matches regardless of collapse).
    const countEl = document.getElementById('search-count');
    if (countEl) {
      if (q) {
        countEl.textContent = `${matched.size} result${matched.size === 1 ? '' : 's'}`;
        countEl.hidden = false;
      } else {
        countEl.hidden = true;
      }
    }
  }

  function deviceRow(d, badge, section = '', opts = {}) {
    const sel = d.ip === selectedIp ? ' selected' : '';
    const freqCls = d.frequent ? ' frequent' : '';
    const pausedCls = d.status === 'disabled' ? ' paused' : '';
    const warnHtml = d.frequent
      ? `<span class="badge badge-warn" title="${d.outage_count} outages in the last 30 days">⚠ ${d.outage_count}</span>`
      : '';
    const pausedHtml = d.status === 'disabled' ? '<span class="badge badge-paused">PAUSED</span>' : '';
    const notifHtml = notifPaused(d)
      ? '<span class="badge badge-notif" title="Slack notifications paused — still monitored">🔕</span>' : '';
    // Blue event symbol (same as the device log) for devices in an ongoing event.
    const eventHtml = d.event_cat
      ? `<span class="device-event-icon" title="Part of an ongoing ${esc(EV_CATS[d.event_cat] || d.event_cat)} event">${evIconSvg(d.event_cat)}</span>` : '';
    const search = `${d.name} ${d.ip} ${d.location || ''}`.toLowerCase();

    // Group head (a switch whose connected devices are all in this section) gets a
    // dropdown caret + "& connected devices"; children get an indent + parent tag so
    // applyFilters can hide them while the group is collapsed.
    const isHead = !!opts.groupHead, isChild = !!opts.groupChild;
    const groupCls = isHead ? ' group-head' : isChild ? ' group-child' : '';
    const groupAttr = isHead ? ` data-group="${esc(opts.groupHead)}"`
                    : isChild ? ` data-group-parent="${esc(opts.groupChild)}"` : '';
    const caretHtml = isHead
      ? `<button class="group-caret" title="Show/hide connected devices" onclick="event.stopPropagation(); window.toggleDeviceGroup('${esc(opts.groupHead)}')">${opts.expanded ? '▾' : '▸'}</button>`
      : '';
    const suffixHtml = isHead ? ' <span class="group-suffix">&amp; connected devices</span>' : '';

    return `
      <div class="device-item${sel}${freqCls}${pausedCls}${groupCls}"${groupAttr} data-ip="${esc(d.ip)}"
           data-search="${esc(search)}" data-type="${d.type}" data-status="${d.status}"
           data-section="${section}"
           data-enabled="${d.enabled === false ? '0' : '1'}"
           data-notify="${d.notify === false ? '0' : '1'}" onclick="focusDevice('${esc(d.ip)}')">
        ${caretHtml}
        <div class="device-status-dot ${d.status}"></div>
        <div class="device-info">
          <div class="name">${warnHtml}${pausedHtml}${notifHtml}${esc(displayName(d))}${suffixHtml}</div>
        </div>
        ${eventHtml}
      </div>`;
  }

  window.focusDevice = function (ip) {
    const sw = topology.switches.find(s => s.ip === ip);
    const ap = topology.devices.find(d => d.ip === ip);
    const node = sw || ap;
    if (!node) return;

    // Capture the current view only on the FIRST focus — a device→device switch
    // (panel already open) keeps the original pre-focus view to return to.
    if (selectedIp == null) preFocusTransform = d3.zoomTransform(svg.node());

    selectedIp = ip;
    const svgEl = document.getElementById('map-svg');
    // The detail panel opens over the right edge of the map (same width as the
    // sidebar), so center the device in the visible area to its LEFT, not the full
    // svg — otherwise it lands behind the panel. (On mobile the panel is full-screen
    // and the sidebar is hidden → width 0 → normal centering, which is moot there.)
    const detailW = document.getElementById('sidebar').getBoundingClientRect().width || 0;
    const cx = Math.max(1, svgEl.clientWidth - detailW) / 2;
    const cy = svgEl.clientHeight / 2;
    svg.transition().duration(600)
      .call(zoom.transform, d3.zoomIdentity.translate(cx - node.x, cy - node.y).scale(1));

    if (sw) {
      highlightSwitch(ip);
      showDetail(sw, topology.devices.filter(d => d.switch_name === sw.name));
    } else {
      document.querySelectorAll('.device-item').forEach(el =>
        el.classList.toggle('selected', el.dataset.ip === ip));
      showDetail(ap, []);
    }
  };

  // ── Header stats ──────────────────────────────────────────────────
  // Which metric the collapsed header shows; chosen from the dropdown. Default
  // is the number of devices UP out of the total.
  let summaryMetric = 'up';
  let activeStatFilter = null;   // which status the dropdown is filtering the list to (or null)
  let lastCounts = { up: 0, down: 0, unknown: 0, paused: 0 };
  let lastTotal = 0;
  const METRIC_DOT = { up: 'up-dot', down: 'down-dot', unknown: 'unk-dot', paused: 'paused-dot' };

  function renderSummary() {
    document.getElementById('stat-summary-text').textContent =
      `${lastCounts[summaryMetric] ?? 0}/${lastTotal} ${summaryMetric}`;
    document.getElementById('stat-summary-dot').className = 'dot ' + METRIC_DOT[summaryMetric];
    // Highlight the status the list is currently filtered to (if any).
    document.querySelectorAll('.stat-menu-row[data-metric]').forEach(r =>
      r.classList.toggle('active', r.dataset.metric === activeStatFilter));
  }

  function updateHeaderStats(switches, devices) {
    const all = [...switches, ...devices];
    lastCounts = {
      up:      all.filter(d => d.status === 'up').length,
      down:    all.filter(d => d.status === 'down').length,
      unknown: all.filter(d => d.status === 'unknown').length,
      paused:  all.filter(d => d.status === 'disabled').length,
    };
    lastTotal = all.length;   // total number of devices (paused included)

    document.getElementById('stat-up').textContent = lastCounts.up;
    document.getElementById('stat-down').textContent = lastCounts.down;
    document.getElementById('stat-unk').textContent = lastCounts.unknown;
    document.getElementById('stat-paused').textContent = lastCounts.paused;
    renderSummary();

    // Location status dot (left of the location selector):
    // red = any device down; green = something up & none down; grey = no devices
    // or nothing responding (no agent pinging / all unknown or paused).
    const dot = document.getElementById('loc-dot');
    if (dot) {
      const cls = all.length === 0 ? 'off'
        : lastCounts.down > 0 ? 'down'
        : lastCounts.up > 0 ? 'up' : 'off';
      dot.className = 'loc-dot ' + cls;
    }

    document.getElementById('last-updated').textContent =
      `Last refreshed: ${nowTime()}`;
  }

  // Map positions are persisted server-side (see savePositions), committed on
  // "Done" in Edit Map mode so the layout is shared across devices.

  // ── Check Now button ──────────────────────────────────────────────
  document.getElementById('btn-check-now')?.addEventListener('click', async () => {
    const btn = document.getElementById('btn-check-now');
    btn.disabled = true;
    btn.textContent = 'Checking…';
    try {
      await fetch('/api/check-now?' + siteQuery(), { method: 'POST' });
      setTimeout(fetchAndRender, 3000);
    } finally {
      setTimeout(() => {
        btn.disabled = false;
        btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>
          <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
        </svg>Check Now`;
      }, 6000);
    }
  });

  // ── Mobile view toggle (Map ⇄ Devices) ────────────────────────────
  // Only visible on mobile (CSS). Switches which full-screen view shows by
  // toggling `view-list` on <body>; re-fits the map when it becomes visible again
  // (its dimensions are stale while it was display:none).
  const viewToggle = document.getElementById('view-toggle');
  if (viewToggle) {
    viewToggle.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-view]');
      if (!btn) return;
      const showList = btn.dataset.view === 'list';
      document.body.classList.toggle('view-list', showList);
      viewToggle.querySelectorAll('button').forEach(b =>
        b.classList.toggle('active', b === btn));
      if (!showList) setTimeout(fitView, 60);
    });
  }

  // ── Mobile: collapse the bottom map controls behind one expandable icon ──
  // Desktop shows the full control row (CSS); on mobile only #map-ctl-toggle shows
  // and tapping it expands the rest via `body.map-ctl-open`.
  const mapCtlToggle = document.getElementById('map-ctl-toggle');
  mapCtlToggle?.addEventListener('click', e => {
    e.stopPropagation();
    const open = !document.body.classList.contains('map-ctl-open');
    document.body.classList.toggle('map-ctl-open', open);
    mapCtlToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  document.addEventListener('click', e => {
    if (!document.body.classList.contains('map-ctl-open')) return;
    if (!e.target.closest('.map-controls')) document.body.classList.remove('map-ctl-open');
  });


  // ── Status dropdown toggle ────────────────────────────────────────
  const statDropdown = document.getElementById('stat-dropdown');
  const statBtn      = document.getElementById('stat-summary-btn');
  const statMenu     = document.getElementById('stat-menu');

  function setStatMenu(open) {
    statMenu.hidden = !open;
    statBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  statBtn.addEventListener('click', e => { e.stopPropagation(); setStatMenu(statMenu.hidden); });
  document.addEventListener('click', e => {
    if (!statDropdown.contains(e.target)) setStatMenu(false);
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') setStatMenu(false); });

  // Selecting a status filters the device list to it (click the same one again to
  // clear). It drives the sidebar Type/Monitoring/Status filters, so the list — and
  // its section labels — update exactly as the Filters panel would. On mobile we also
  // jump to the list so the filtered result is visible.
  function setDeviceFilters({ types = null, mons = null, stats = null }) {
    const apply = (group, allowed) =>
      document.querySelectorAll(`input[data-filter="${group}"]`).forEach(cb => {
        cb.checked = allowed ? allowed.has(cb.value) : true;
      });
    apply('type', types); apply('mon', mons); apply('status', stats);
  }
  function applyStatFilter(metric) {
    // Paused devices report status "disabled" (governed by the Monitoring group);
    // up/down/unknown are non-paused statuses.
    if (metric === 'paused') setDeviceFilters({ mons: new Set(['paused']) });
    else setDeviceFilters({ mons: new Set(['active']), stats: new Set([metric]) });
    applyFilters();
  }
  function showListView() {
    document.body.classList.add('view-list');
    document.getElementById('view-toggle')?.querySelectorAll('button')
      .forEach(b => b.classList.toggle('active', b.dataset.view === 'list'));
  }
  document.querySelectorAll('.stat-menu-row[data-metric]').forEach(row =>
    row.addEventListener('click', () => {
      const metric = row.dataset.metric;
      if (activeStatFilter === metric) {          // toggle the same filter back off
        activeStatFilter = null;
        summaryMetric = 'up';
        setDeviceFilters({});   // all checked = show everything
        applyFilters();
      } else {
        activeStatFilter = metric;
        summaryMetric = metric;
        applyStatFilter(metric);
        if (window.matchMedia('(max-width: 768px)').matches) showListView();
      }
      renderSummary();
      setStatMenu(false);
    }));

  // ── Edit Map mode ─────────────────────────────────────────────────
  // The Edit Map button toggles: enter edit (drag nodes) → "Done" persists the
  // new layout to the server. There is no Cancel — Done saves what you see.
  // "Edit Map" has two entry points that both toggle edit mode: the on-map button
  // (toggles Edit Map ⇄ Done) and the hamburger-menu item — kept in sync by
  // reflectEditState().
  const editBtnMap  = document.getElementById('btn-edit-map');        // on-map toggle
  const editBtnMenu = document.getElementById('btn-edit-map-menu');   // hamburger item
  const editMapLabel = editBtnMap?.querySelector('.btn-label');
  const gatherBtn = document.getElementById('btn-gather');
  const defViewBtn = document.getElementById('btn-set-default-view');  // "Set auto-crop"
  const selectBtn = document.getElementById('btn-select');
  const recenterBtn = document.getElementById('btn-recenter');
  const mapSvgEl  = document.getElementById('map-svg');

  // Keep both Edit-Map controls showing the current state.
  function reflectEditState() {
    if (editBtnMap) editBtnMap.classList.toggle('active', editMode);
    if (editMapLabel) editMapLabel.textContent = editMode ? 'Done' : 'Edit Map';
    if (editBtnMenu) {
      editBtnMenu.classList.toggle('active', editMode);
      editBtnMenu.textContent = editMode ? 'Done editing' : 'Edit Map';
    }
  }

  // Marquee select sub-mode: while on, the select layer captures empty-space drags to
  // draw a selection box (see makeNodeDrag + the marquee handler above).
  function setSelectMode(on) {
    selectMode = !!on && editMode;
    selectLayer.style('display', selectMode ? null : 'none');
    if (selectBtn) selectBtn.classList.toggle('active', selectMode);
    mapSvgEl.classList.toggle('selecting', selectMode);
  }

  function enterEdit() {
    editMode = true;
    hideTooltip();   // drop any hover info that was showing when Edit Map started
    if (menuPanel) menuPanel.hidden = true;   // if entered from the hamburger menu
    reflectEditState();
    // Multi-select works in either mode; the aerial-only tools (Stack all / Set
    // auto-crop / Set map area) are shown by updateBasemapChrome below.
    if (selectBtn) selectBtn.hidden = false;
    mapSvgEl.classList.add('editing');
    updateBasemapChrome();
  }

  async function exitEdit() {
    setSelectMode(false);
    clearSelection();
    await savePositions();  // persist before the refetch below reflects state
    editMode = false;
    reflectEditState();
    if (selectBtn) selectBtn.hidden = true;
    mapSvgEl.classList.remove('editing');
    updateBasemapChrome();   // hide the aerial edit tools again
    fetchAndRender();  // resume live updates
  }

  const toggleEdit = () => editMode ? exitEdit() : enterEdit();
  editBtnMap?.addEventListener('click', toggleEdit);
  editBtnMenu?.addEventListener('click', toggleEdit);
  selectBtn?.addEventListener('click', () => setSelectMode(!selectMode));
  // Recenter: return the map to its default opening view (available to everyone).
  recenterBtn?.addEventListener('click', () => fitView());

  // "Stack all": pile every device at the CENTER of the aerial image so you can drag
  // each out to its building and see at a glance what's left to place (the shrinking
  // pile is the un-placed set). Positions are transient until "Done" saves them. No
  // projectNodes here — it would immediately re-scatter the nodes back to their lat/lng.
  function gatherNodes() {
    const all = [...topology.switches, ...topology.devices];
    if (!all.length) return;
    const r = viewRect();
    let gx, gy;
    if (r) {
      // Center of the map area (the aerial image).
      gx = (r.x0 + r.x1) / 2;
      gy = (r.y0 + r.y1) / 2;
    } else {
      // Plain grid fallback: center of the current view.
      const t = d3.zoomTransform(svg.node());
      const { W, H } = viewportSize();
      gx = t.invertX(W / 2);
      gy = t.invertY(H / 2);
    }
    all.forEach(n => { n.x = gx; n.y = gy; });
    swGroup.selectAll('g.switch-node').attr('transform', nodeTransform);
    apGroup.selectAll('g.node').attr('transform', nodeTransform);
    otherGroup.selectAll('g.other-node').attr('transform', nodeTransform);
    linkGroup.selectAll('line.ap-link')
      .attr('x1', gx).attr('y1', gy).attr('x2', gx).attr('y2', gy);
    positionAllSwLinks();
  }

  gatherBtn?.addEventListener('click', () => {
    if (!editMode) return;
    if (!confirm('Stack every device at one point so you can drag each to its spot?\n\n'
      + 'This replaces the current layout when you click Done.')) return;
    gatherNodes();
  });

  // "Set opening view": capture EXACTLY what's on screen now as the view everyone
  // opens the map to (and lands on via Recenter). No rubber-band drag — that was
  // imprecise on touch. Pan/zoom to frame it, then tap. Stored per layout (desktop
  // and phones each get their own). It does NOT restrict panning.
  defViewBtn?.addEventListener('click', () => {
    if (!editMode || !basemap.active) return;
    captureOpenView();
  });

  function captureOpenView() {
    // The visible viewport corners (screen 0,0 → W,availH) mapped back to world
    // (image-pixel) space via the current zoom transform.
    const t = d3.zoomTransform(svg.node());
    const { W, availH } = viewportSize();
    const [ax, ay] = t.invert([0, 0]);
    const [bx, by] = t.invert([W, availH]);
    saveCrop(Math.min(ax, bx), Math.min(ay, by), Math.abs(bx - ax), Math.abs(by - ay));
  }

  async function saveCrop(x0, y0, w, h) {
    // Save the crop as a lat/lng RECTANGLE (version-robust). fitView re-derives the
    // zoom that CONTAINS this box in the current viewport every time, so an odd/small
    // window fits the more-constraining dimension and shows extra map on the other
    // axis. Centre + a `k` are sent too as a fallback for older clients.
    const { W, availH } = viewportSize();
    const [lat, lng] = fromPx(x0 + w / 2, y0 + h / 2);
    const [north, west] = fromPx(x0, y0);
    const [south, east] = fromPx(x0 + w, y0 + h);
    let k = Math.min(W / w, availH / h);
    k = Math.max(geoFloorScale(), Math.min(k, GEO_MAX_SCALE));
    const layout = mapLayout();
    try {
      const resp = await fetch('/api/basemap/open-view?' + siteQuery(), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lat, lng, k, box: { south, west, north, east }, layout }),
      });
      if (resp.status === 401) { window.location = '/login'; return; }
      if (!resp.ok) throw new Error();
      const cfg = await resp.json();
      if (basemap.cfg) {
        basemap.cfg.open_view = cfg.open_view;
        basemap.cfg.open_view_mobile = cfg.open_view_mobile;
      }
      fitView();   // jump to the new view so you can see what you set
      const where = layout === 'mobile' ? 'phones' : 'computers';
      showToast(`Saved — ${where} now open the map here.`, 'up', 4000);
    } catch {
      showToast("Couldn't save the crop. Please try again.", 'down');
    }
  }

  // ── Aerial photo controls ─────────────────────────────────────────
  const photoBtn  = document.getElementById('btn-photo');
  const labelsBtn = document.getElementById('btn-labels');
  const areaBtn   = document.getElementById('btn-map-area');
  const areaLabel = areaBtn?.querySelector('.btn-label');
  const areaHint  = document.getElementById('map-area-hint');
  const attribEl  = document.getElementById('map-attrib');

  // Device labels over the photo are a per-browser display preference, default OFF
  // (a hover tooltip covers the details); the toggle only appears while the photo is on.
  const LABELS_KEY = 'apmon.labels.on';
  let labelsOn = localStorage.getItem(LABELS_KEY) === '1';

  // Show/hide the photo controls and attribution for the current location. A
  // location with no imagery shows neither.
  function updateBasemapChrome() {
    // In demo mode the imagery stays unreachable, toggle included: an aerial view of
    // the camp identifies it as plainly as its name would, so offering a "Show photo"
    // button would undo the whole point of the mode.
    const have = hasBasemap() && !DEMO;
    if (photoBtn) {
      // Locked while editing: toggling the photo mid-edit would refetch + switch
      // coordinate spaces (geo lat/lng ↔ plain x/y) out from under the drag, which
      // breaks the edit session. Pick the view first, then Edit Map.
      photoBtn.hidden = !have || editMode;
      photoBtn.classList.toggle('active', basemap.active);
      photoBtn.setAttribute('aria-pressed', basemap.active ? 'true' : 'false');
      const lbl = photoBtn.querySelector('.btn-label');
      if (lbl) lbl.textContent = basemap.active ? 'Hide photo' : 'Show photo';
    }
    // The aerial Edit-Map tools (Set map area / Set auto-crop / Stack all) only show
    // while editing over the photo — centralized here so toggling the photo mid-edit
    // hides/shows them correctly.
    const editingGeo = editMode && basemap.active;
    if (areaBtn) areaBtn.hidden = !editingGeo;
    if (gatherBtn) gatherBtn.hidden = !editingGeo;
    if (defViewBtn) defViewBtn.hidden = !editingGeo;
    if (!editingGeo && areaMode) exitAreaMode();
    // Labels toggle: only over the photo (on the plain grid labels always show), and
    // not while editing (labels are force-shown then, so the toggle would be a no-op).
    if (labelsBtn) {
      labelsBtn.hidden = !have || !basemap.active || editMode;
      labelsBtn.classList.toggle('active', labelsOn);
      labelsBtn.setAttribute('aria-pressed', labelsOn ? 'true' : 'false');
      const l = labelsBtn.querySelector('.btn-label');
      if (l) l.textContent = labelsOn ? 'Hide labels' : 'Show labels';
    }
    svg.classed('labels-on', labelsOn);
    if (attribEl) {
      attribEl.hidden = !basemap.active;
      attribEl.textContent = basemap.active ? (basemap.cfg.attribution || '') : '';
    }
  }

  async function togglePhoto() {
    basemap.on = !basemap.on;
    localStorage.setItem(PHOTO_KEY, basemap.on ? '1' : '0');
    if (areaMode) exitAreaMode();
    refreshBasemapGeometry();
    updateBasemapChrome();
    // REFETCH rather than re-render the data we already have: projectNodes overwrites
    // x/y in place, so `topology` holds projected pixels once the photo has been on.
    // Re-rendering that in plain mode would show the logical layout at image-pixel
    // coordinates — and an Edit Map save from there would write those pixels into the
    // logical layout. A fresh fetch restores the server's authoritative x/y.
    firstLoad = true;         // makes fetchAndRender refit for the new coordinate space
    await fetchAndRender();
    drawTiles();
  }

  photoBtn?.addEventListener('click', togglePhoto);

  // Toggle device labels over the photo (per-browser; a hover tooltip still shows the
  // full details either way).
  labelsBtn?.addEventListener('click', () => {
    labelsOn = !labelsOn;
    localStorage.setItem(LABELS_KEY, labelsOn ? '1' : '0');
    updateBasemapChrome();
  });

  // "Set map area": drag a rectangle over the photo to redefine which part of it the
  // map shows. Saved per-location on the server, so it survives a redeploy and is
  // shared across browsers — and a future, larger raster can be cropped to just the
  // part that matters without a code change.
  let areaRect = null;      // the rubber-band <rect> while dragging
  let areaStart = null;     // drag origin in world coords

  function enterAreaMode() {
    areaMode = true;
    // "Set map area" is now an Edit-Map sub-tool — it runs WITHIN edit mode (doesn't
    // exit it), so the button stays visible and you return to editing when done.
    hideTooltip();
    areaBtn.classList.add('active');
    if (areaLabel) areaLabel.textContent = 'Cancel';
    mapSvgEl.classList.add('setting-area');
    if (areaHint) areaHint.hidden = false;
  }

  function exitAreaMode() {
    areaMode = false;
    areaStart = null;
    if (areaRect) { areaRect.remove(); areaRect = null; }
    areaBtn?.classList.remove('active');
    if (areaLabel) areaLabel.textContent = 'Set map area';
    mapSvgEl.classList.remove('setting-area');
    if (areaHint) areaHint.hidden = true;
  }

  areaBtn?.addEventListener('click', () => areaMode ? exitAreaMode() : enterAreaMode());

  // Rubber-band drag, in world coordinates so the rectangle tracks the imagery
  // rather than the screen while it's being drawn.
  // d3.pointer on a raw TouchEvent reads event.clientX (undefined) → NaN coords,
  // so on touch devices the rectangle never draws. Pull the active touch first.
  function rectPointer(event) {
    const t = (event.touches && event.touches[0]) ||
              (event.changedTouches && event.changedTouches[0]);
    return d3.pointer(t || event, container.node());
  }

  svg.on('mousedown.area touchstart.area', function (event) {
    if (!drawingRect()) return;
    event.preventDefault();
    const [x, y] = rectPointer(event);
    areaStart = { x, y };
    areaRect = container.append('rect').attr('class', 'area-select')
      .attr('x', x).attr('y', y).attr('width', 0).attr('height', 0).node();
  });

  svg.on('mousemove.area touchmove.area', function (event) {
    if (!drawingRect() || !areaStart || !areaRect) return;
    event.preventDefault();
    const [x, y] = rectPointer(event);
    d3.select(areaRect)
      .attr('x', Math.min(areaStart.x, x)).attr('y', Math.min(areaStart.y, y))
      .attr('width', Math.abs(x - areaStart.x)).attr('height', Math.abs(y - areaStart.y));
  });

  svg.on('mouseup.area touchend.area', async function (event) {
    if (!drawingRect() || !areaStart || !areaRect) return;
    const r = d3.select(areaRect);
    const w = +r.attr('width'), h = +r.attr('height');
    const x0 = +r.attr('x'), y0 = +r.attr('y');
    areaStart = null;
    areaRect.remove();
    areaRect = null;
    // Ignore a click or a sliver — almost certainly not an intended rectangle.
    if (w < 40 || h < 40) {
      showToast('Drag a larger rectangle.', 'info', 4000);
      return;
    }
    const [north, west] = fromPx(x0, y0);
    const [south, east] = fromPx(x0 + w, y0 + h);
    await saveMapArea({ south, west, north, east });
  });

  async function saveMapArea(view) {
    try {
      const resp = await fetch('/api/basemap/view?' + siteQuery(), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(view),
      });
      if (resp.status === 401) { window.location = '/login'; return; }
      if (resp.status === 403) {
        showToast("You don't have permission to change the map area.", 'down');
        exitAreaMode();
        return;
      }
      const data = await resp.json();
      if (!resp.ok) { showToast(data.error || "Couldn't save the map area.", 'down'); return; }
      basemap.cfg = data;
      refreshBasemapGeometry();
      exitAreaMode();
      updateBasemapChrome();
      drawTiles();
      fitView();
      showToast(view ? 'Map area updated.' : 'Map area reset to its default.', 'up', 4000);
    } catch {
      showToast("Couldn't save the map area — check your connection.", 'down');
    }
  }

  document.getElementById('btn-map-area-reset')?.addEventListener('click', async () => {
    const resp = await fetch('/api/basemap/view?' + siteQuery(), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reset: true }),
    }).catch(() => null);
    if (!resp || !resp.ok) {
      showToast("Couldn't reset the map area.", 'down');
      return;
    }
    basemap.cfg = await resp.json();
    refreshBasemapGeometry();
    exitAreaMode();
    updateBasemapChrome();
    drawTiles();
    fitView();
    showToast('Map area reset to its default.', 'up', 4000);
  });

  // Keep the fit and the pan clamp correct when the window changes size. Refits only
  // in geo mode, where the zoom floor depends on the viewport — in plain mode a
  // resize shouldn't yank the user's chosen view around.
  let resizeTimer = 0;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!basemap.active) return;
      applyZoomLimits();
      drawTiles();
    }, 200);
  });

  // ── Resizable device list ─────────────────────────────────────────
  // Drag the handle between the map and the list to widen/narrow it. The width is a
  // CSS var (the sidebar is fixed-width, the map flexes to fill the rest) and is
  // remembered per-browser.
  const sidebarResizer = document.getElementById('sidebar-resizer');
  if (sidebarResizer) {
    const SIDEBAR_KEY = 'apmon.sidebarw';
    const SIDEBAR_MIN = 220;
    const sidebarMax = () => Math.min(680, window.innerWidth - 360);   // leave room for the map
    const setSidebarW = w => {
      w = Math.max(SIDEBAR_MIN, Math.min(w, sidebarMax()));
      document.documentElement.style.setProperty('--sidebar-w', w + 'px');
      return w;
    };
    const savedW = parseInt(localStorage.getItem(SIDEBAR_KEY), 10);
    if (savedW) setSidebarW(savedW);

    sidebarResizer.addEventListener('mousedown', e => {
      e.preventDefault();
      const rightEdge = document.getElementById('sidebar').getBoundingClientRect().right;
      document.body.classList.add('resizing-sidebar');
      let curW;
      const onMove = ev => { curW = setSidebarW(rightEdge - ev.clientX); positionDetail(); };
      const onUp = () => {
        document.body.classList.remove('resizing-sidebar');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        if (curW) localStorage.setItem(SIDEBAR_KEY, String(Math.round(curW)));
        window.dispatchEvent(new Event('resize'));   // let the map recompute its zoom limits
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    // Reclamp if the window shrinks so the list can't crowd out the map.
    window.addEventListener('resize', () => {
      const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w'), 10);
      if (cur) setSidebarW(cur);
    });
  }

  // ── Sidebar search + filters ──────────────────────────────────────
  document.getElementById('device-search')
    ?.addEventListener('input', applyFilters);

  // Collapse/expand a sidebar section by clicking its header (delegated, since the
  // list re-renders every 30s). State persists in collapsedSections.
  document.getElementById('device-list')?.addEventListener('click', e => {
    const label = e.target.closest('.sidebar-section-label');
    if (!label) return;
    const key = label.dataset.section;
    if (collapsedSections.has(key)) collapsedSections.delete(key);
    else collapsedSections.add(key);
    const collapsed = collapsedSections.has(key);
    label.classList.toggle('collapsed', collapsed);
    const caret = label.querySelector('.sec-caret');
    if (caret) caret.textContent = collapsed ? '▸' : '▾';
    applyFilters();
  });

  // Filter group checkboxes (Type / Monitoring / Status) re-filter on change. A
  // manual edit supersedes the status-dropdown filter, so drop its highlight.
  document.querySelectorAll('#filter-panel input[data-filter]')
    .forEach(cb => cb.addEventListener('change', () => {
      activeStatFilter = null;
      renderSummary();
      applyFilters();
    }));

  // Toggle the filter panel open/closed.
  const filterToggle = document.getElementById('filter-toggle');
  const filterPanel  = document.getElementById('filter-panel');
  filterToggle?.addEventListener('click', () => {
    const open = filterPanel.hidden;
    filterPanel.hidden = !open;
    filterToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    positionDetail();   // device-list height changed → keep the panel aligned
  });

  // ── Location (site) dropdown ──────────────────────────────────────
  // Custom dropdown styled like the status dropdown. Each location has its own
  // devices/map/agent. currentSite scopes every data fetch; it comes from
  // ?site= or localStorage, else the default.
  const siteDropdown = document.getElementById('site-dropdown');
  const siteBtn = document.getElementById('site-summary-btn');
  const siteMenu = document.getElementById('site-menu');
  const siteText = document.getElementById('site-summary-text');
  const siteRows = () => [...siteMenu.querySelectorAll('.site-menu-row[data-site]')];
  const siteRowFor = key => siteRows().find(r => r.dataset.site === key);
  const siteName = key => siteRowFor(key)?.querySelector('.site-menu-name').textContent.trim() || key;
  const siteKeys = new Set(siteRows().map(r => r.dataset.site));

  // Build a location row (name + pencil) matching the server-rendered markup.
  function makeSiteRow(key, name) {
    const row = document.createElement('div');
    row.className = 'stat-menu-row site-menu-row';
    row.dataset.site = key;
    const label = document.createElement('span');
    label.className = 'site-menu-name';
    label.textContent = name;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'site-edit-btn';
    btn.title = 'Edit location';
    btn.setAttribute('aria-label', 'Edit location');
    btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" '
      + 'stroke="currentColor" stroke-width="2"><path d="M12 20h9"/>'
      + '<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
    row.append(label, btn);
    return row;
  }

  let currentSite = new URLSearchParams(location.search).get('site')
    || localStorage.getItem('site')
    || siteBtn?.dataset.default;
  if (!siteKeys.has(currentSite)) currentSite = siteBtn?.dataset.default;
  const siteQuery = () => 'site=' + encodeURIComponent(currentSite);

  // Deep link from a Slack outage alert: ?device=<ip> opens that device's view once
  // the first topology load is in. Consumed a single time (see fetchAndRender).
  let pendingDeviceFocus = new URLSearchParams(location.search).get('device');

  function renderSiteButton() {
    siteText.textContent = siteName(currentSite);
    siteRows().forEach(r => r.classList.toggle('active', r.dataset.site === currentSite));
  }
  renderSiteButton();

  function setSiteMenu(open) {
    siteMenu.hidden = !open;
    siteBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  siteBtn?.addEventListener('click', e => { e.stopPropagation(); setSiteMenu(siteMenu.hidden); });
  document.addEventListener('click', e => { if (!siteDropdown.contains(e.target)) setSiteMenu(false); });

  function switchSite(key) {
    setSiteMenu(false);
    if (key === currentSite) return;
    currentSite = key;
    localStorage.setItem('site', currentSite);
    const u = new URL(location); u.searchParams.set('site', currentSite);
    history.replaceState(null, '', u);
    renderSiteButton();
    preFocusTransform = null;   // new location refits below — don't restore an old view
    closeDetail();
    // Re-arm the decommission popup for the newly-selected site.
    pendingPopupShownFor = null;
    if (decomOverlay) decomOverlay.hidden = true;
    firstLoad = true;        // refit the map for the new location
    if (areaMode) exitAreaMode();
    // Each location has its own imagery (or none), so reload the basemap BEFORE the
    // topology — projectNodes needs the new georeferencing to place devices.
    loadBasemap().then(fetchAndRender);
  }

  siteMenu?.addEventListener('click', e => {
    // Pencil → edit this location (don't switch to it).
    if (e.target.closest('.site-edit-btn')) {
      const row = e.target.closest('.site-menu-row[data-site]');
      openSiteEdit(row.dataset.site, row.querySelector('.site-menu-name').textContent.trim());
      return;
    }
    const row = e.target.closest('.site-menu-row[data-site]');
    if (row) { switchSite(row.dataset.site); return; }
    if (e.target.closest('#site-add-btn')) { setSiteMenu(false); openSiteModal(); }
  });

  // ── Add Location modal ────────────────────────────────────────────
  const siteModalOverlay = document.getElementById('site-modal-overlay');
  const siteModalForm = document.getElementById('site-modal-form');
  const siteNameInput = document.getElementById('site-name-input');
  const siteModalErr = document.getElementById('site-modal-error');

  function openSiteModal() {
    siteModalForm.reset();
    siteModalErr.hidden = true;
    siteModalOverlay.hidden = false;
    setTimeout(() => siteNameInput.focus(), 0);
  }
  function closeSiteModal() { siteModalOverlay.hidden = true; }

  document.getElementById('site-modal-close')?.addEventListener('click', closeSiteModal);
  document.getElementById('site-modal-cancel')?.addEventListener('click', closeSiteModal);
  siteModalOverlay?.addEventListener('click', e => { if (e.target === siteModalOverlay) closeSiteModal(); });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (!siteModalOverlay.hidden) closeSiteModal();
    else setSiteMenu(false);
  });

  siteModalForm?.addEventListener('submit', async e => {
    e.preventDefault();
    const name = siteNameInput.value.trim();
    if (!name) return;
    const submit = document.getElementById('site-modal-submit');
    submit.disabled = true;
    try {
      const resp = await fetch('/api/sites', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (resp.status === 401) { window.location = '/login'; return; }
      const data = await resp.json();
      if (!resp.ok) {
        siteModalErr.textContent = data.error || 'Could not add location';
        siteModalErr.hidden = false;
        return;
      }
      // Insert the new row before the "Add location…" action and switch to it.
      siteMenu.insertBefore(makeSiteRow(data.site.key, data.site.name),
                            document.getElementById('site-add-btn'));
      siteKeys.add(data.site.key);
      closeSiteModal();
      switchSite(data.site.key);
    } catch (err) {
      siteModalErr.textContent = 'Network error — please try again';
      siteModalErr.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });

  // ── Edit / delete Location modal ──────────────────────────────────
  // Rename is login-only; delete additionally requires the admin password
  // (ADMIN_PASSWORD config var) and is a two-step confirm.
  const siteEditOverlay = document.getElementById('site-edit-overlay');
  const siteEditForm = document.getElementById('site-edit-form');
  const siteEditName = document.getElementById('site-edit-name');
  const siteEditErr = document.getElementById('site-edit-error');
  const siteDeleteRow = document.getElementById('site-delete-row');
  const siteDeletePw = document.getElementById('site-delete-pw');
  const siteDeleteBtn = document.getElementById('site-edit-delete');
  const siteSaveBtn = document.getElementById('site-edit-save');
  const siteCancelBtn = document.getElementById('site-edit-cancel');
  let editingSiteKey = null;
  let editingSiteName = '';
  let deleteArmed = false;

  // Show "Cancel" until the name is actually changed, then swap it for
  // "Save Changes" — the two share the same slot.
  function refreshSiteEditActions() {
    const dirty = siteEditName.value.trim() !== '' && siteEditName.value.trim() !== editingSiteName;
    siteCancelBtn.hidden = dirty;
    siteSaveBtn.hidden = !dirty;
  }
  siteEditName?.addEventListener('input', refreshSiteEditActions);

  function openSiteEdit(key, name) {
    setSiteMenu(false);
    editingSiteKey = key;
    editingSiteName = name;
    deleteArmed = false;
    siteEditForm.reset();
    siteEditName.value = name;
    siteEditErr.hidden = true;
    siteDeleteRow.hidden = true;
    siteDeleteBtn.textContent = 'Delete';
    // Can't delete the only remaining location — hide the option entirely then.
    siteDeleteBtn.style.display = siteKeys.size <= 1 ? 'none' : '';
    refreshSiteEditActions();          // starts on "Cancel" (name unchanged)
    siteEditOverlay.hidden = false;
    setTimeout(() => siteEditName.focus(), 0);
  }
  function closeSiteEdit() { siteEditOverlay.hidden = true; editingSiteKey = null; }

  document.getElementById('site-edit-close')?.addEventListener('click', closeSiteEdit);
  document.getElementById('site-edit-cancel')?.addEventListener('click', closeSiteEdit);
  siteEditOverlay?.addEventListener('click', e => { if (e.target === siteEditOverlay) closeSiteEdit(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !siteEditOverlay.hidden) closeSiteEdit();
  });

  // Save = rename.
  siteEditForm?.addEventListener('submit', async e => {
    e.preventDefault();
    const name = siteEditName.value.trim();
    if (!name || !editingSiteKey) return;
    siteSaveBtn.disabled = true;
    try {
      const resp = await fetch(`/api/sites/${encodeURIComponent(editingSiteKey)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (resp.status === 401) { window.location = '/login'; return; }
      const data = await resp.json();
      if (!resp.ok) {
        siteEditErr.textContent = data.error || 'Could not rename location';
        siteEditErr.hidden = false;
        return;
      }
      const row = siteRowFor(editingSiteKey);
      if (row) row.querySelector('.site-menu-name').textContent = data.site.name;
      closeSiteEdit();
      renderSiteButton();   // refresh the header label if the current site was renamed
    } catch (err) {
      siteEditErr.textContent = 'Network error — please try again';
      siteEditErr.hidden = false;
    } finally {
      siteSaveBtn.disabled = false;
    }
  });

  // Delete = two-step: first click reveals the admin-password field, second
  // click (as "Confirm Delete") submits it.
  siteDeleteBtn?.addEventListener('click', async () => {
    if (!deleteArmed) {
      deleteArmed = true;
      siteDeleteRow.hidden = false;
      siteDeleteBtn.textContent = 'Confirm Delete';
      siteEditErr.hidden = true;
      setTimeout(() => siteDeletePw.focus(), 0);
      return;
    }
    const pw = siteDeletePw.value;
    if (!pw) {
      siteEditErr.textContent = 'Enter the admin password to delete this location.';
      siteEditErr.hidden = false;
      siteDeletePw.focus();
      return;
    }
    siteDeleteBtn.disabled = true;
    try {
      const resp = await fetch(`/api/sites/${encodeURIComponent(editingSiteKey)}`, {
        method: 'DELETE', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pw }),
      });
      if (resp.status === 401) { window.location = '/login'; return; }
      const data = await resp.json();
      if (!resp.ok) {
        siteEditErr.textContent = data.error || 'Could not delete location';
        siteEditErr.hidden = false;
        return;
      }
      // Soft-delete: the location isn't removed — it's scheduled for deletion in
      // 24h and stays viewable (with the undo popup) until then. Switch to it so
      // the requester immediately sees the decommission/undo popup.
      const scheduled = editingSiteKey;
      closeSiteEdit();
      if (currentSite === scheduled) {
        pendingPopupShownFor = null;   // re-arm the popup for the current site
        fetchAndRender();
      } else {
        switchSite(scheduled);
      }
    } catch (err) {
      siteEditErr.textContent = 'Network error — please try again';
      siteEditErr.hidden = false;
    } finally {
      siteDeleteBtn.disabled = false;
    }
  });

  document.getElementById('btn-agent')?.addEventListener('click', () => {
    window.location = '/agent/download?' + siteQuery();
  });

  // ── Hamburger menu (Download agent / Logout) ──────────────────────
  const menuEl = document.getElementById('menu');
  const menuToggle = document.getElementById('menu-toggle');
  const menuPanel = document.getElementById('menu-panel');
  menuToggle?.addEventListener('click', e => {
    e.stopPropagation();
    const open = menuPanel.hidden;
    menuPanel.hidden = !open;
    menuToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  document.addEventListener('click', e => {
    if (menuEl && !menuEl.contains(e.target)) menuPanel.hidden = true;
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && menuPanel) menuPanel.hidden = true; });

  // Mobile-only menu duplicates of the header toolbar buttons: they just trigger
  // the real (CSS-hidden on mobile) header controls so behavior stays identical.
  document.getElementById('menu-view-log')?.addEventListener('click', () => {
    if (menuPanel) menuPanel.hidden = true;
    document.getElementById('btn-view-log')?.click();
  });
  document.getElementById('menu-check-now')?.addEventListener('click', () => {
    if (menuPanel) menuPanel.hidden = true;
    document.getElementById('btn-check-now')?.click();
  });

  // ── Deleted (decommissioned) devices page ─────────────────────────────
  // Lists devices removed from the fleet; clicking one opens its detail panel just
  // like a live device (its logs are retained). Read-only — no monitoring actions.
  const deletedOverlay = document.getElementById('deleted-overlay');
  const deletedListEl  = document.getElementById('deleted-list');
  let deletedNodes = [];

  function deletedToNode(e) {
    const type = e.kind === 'switch' ? 'switch' : e.kind === 'other' ? 'other' : 'ap';
    // status 'unknown' (no longer monitored); `deleted` suppresses the panel's
    // monitoring actions and adds a "Removed" row.
    return { ...e, type, switch_name: e.switch, status: 'unknown', deleted: true,
             enabled: true, notify: true };
  }

  function renderDeletedList() {
    if (!deletedNodes.length) {
      deletedListEl.innerHTML = '<div class="log-empty">No deleted devices.</div>';
      return;
    }
    const kindLabel = k => k === 'switch' ? 'Switch' : k === 'other' ? 'Other' : 'AP';
    deletedListEl.innerHTML = deletedNodes.map((e, i) => {
      const node = deletedToNode(e);
      const when = e.decommissioned_at ? fmtDateTime(e.decommissioned_at) : '';
      return `<button class="deleted-item" type="button" data-idx="${i}">
          <span class="deleted-name">${esc(displayName(node))}</span>
          <span class="deleted-meta">${esc(kindLabel(e.kind))}${DEMO ? '' : ' · ' + esc(e.ip)}${when ? ' · removed ' + esc(when) : ''}</span>
        </button>`;
    }).join('');
  }

  async function openDeletedDevices() {
    if (menuPanel) menuPanel.hidden = true;
    deletedListEl.innerHTML = LOG_LOADING;
    deletedOverlay.hidden = false;
    try {
      const resp = await fetch('/api/deleted-devices?' + siteQuery());
      if (resp.status === 401) { window.location = '/login'; return; }
      deletedNodes = (await resp.json()).devices || [];
      renderDeletedList();
    } catch (e) { deletedListEl.innerHTML = '<div class="log-empty">Failed to load.</div>'; }
  }
  function closeDeletedDevices() { deletedOverlay.hidden = true; }

  document.getElementById('btn-deleted')?.addEventListener('click', openDeletedDevices);
  document.getElementById('deleted-close')?.addEventListener('click', closeDeletedDevices);
  deletedOverlay?.addEventListener('click', e => { if (e.target === deletedOverlay) closeDeletedDevices(); });
  deletedListEl?.addEventListener('click', e => {
    const btn = e.target.closest('.deleted-item');
    if (!btn) return;
    const entry = deletedNodes[+btn.dataset.idx];
    if (!entry) return;
    closeDeletedDevices();
    // Open the detail panel directly (no map focus/zoom — it's not on the map).
    showDetail(deletedToNode(entry), []);
  });

  // ── Viewer preview (admin only): hide all edit affordances client-side so an
  // admin can see the read-only experience. The server still treats them as admin.
  function refreshOpenDetail() {
    if (selectedIp == null) return;
    const fresh = (topology.switches || []).find(s => s.ip === selectedIp)
               || (topology.devices || []).find(d => d.ip === selectedIp);
    if (!fresh) return;
    if (fresh.type === 'switch') showDetail(fresh, topology.devices.filter(d => d.switch_name === fresh.name));
    else showDetail(fresh, []);
  }
  function applyViewerPreview() {
    document.body.classList.toggle('preview-viewer', previewViewer);
    const b = document.getElementById('btn-view-as');
    if (b) b.textContent = previewViewer ? 'Exit viewer preview' : 'Preview as viewer';
  }
  applyViewerPreview();   // reflect the persisted flag at load
  document.getElementById('btn-view-as')?.addEventListener('click', () => {
    previewViewer = !previewViewer;
    sessionStorage.setItem('apmon.previewViewer', previewViewer ? '1' : '0');
    if (menuPanel) menuPanel.hidden = true;
    if (previewViewer && editMode) toggleEdit();   // viewers can't be in Edit Map
    applyViewerPreview();
    refreshOpenDetail();    // re-render an open device panel (notes/actions gating)
  });

  // ── Settings (hamburger → Settings): DB-backed tuning knobs, apply live ──
  const settingsOverlay = document.getElementById('settings-overlay');
  const settingsForm = document.getElementById('settings-form');
  const settingsErr = document.getElementById('settings-error');
  // Numeric fields (id suffix = setting key) + the one boolean toggle.
  const SETTING_NUM = ['outage_window_days', 'frequent_min_outages', 'down_confirm_checks',
                       'ping_count', 'ping_timeout', 'watchdog_stale_minutes', 'reminder_hours'];
  const setField = key => document.getElementById('set-' + key);

  // Tuck each setting's description behind a small ⓘ icon next to its title. Clicking
  // the icon shows the description in a small popover that points up at the icon (the
  // hint node is relocated into a positioned wrapper on the icon). Injected once.
  function closeSettingsHints(except) {
    document.querySelectorAll('#settings-form .settings-hint.show').forEach(h => {
      if (h === except) return;
      h.classList.remove('show');
      const b = h.closest('.info-wrap')?.querySelector('.info-toggle');
      if (b) { b.classList.remove('active'); b.setAttribute('aria-expanded', 'false'); }
    });
  }
  function setupSettingsHints() {
    const INFO_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
      + 'stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/>'
      + '<line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>';
    document.querySelectorAll('#settings-form .form-row').forEach(row => {
      const label = row.querySelector('label');
      const hint = row.querySelector('.settings-hint');
      if (!label || !hint || label.querySelector('.info-toggle')) return;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'info-toggle';
      btn.setAttribute('aria-label', 'Show description');
      btn.setAttribute('aria-expanded', 'false');
      btn.innerHTML = INFO_SVG;
      // Wrap the icon + its (relocated) hint so the popover positions off the icon.
      const wrap = document.createElement('span');
      wrap.className = 'info-wrap';
      wrap.appendChild(btn);
      wrap.appendChild(hint);          // move the hint out of the grid into the popover
      btn.addEventListener('click', e => {
        e.preventDefault();            // don't let a click inside the label toggle its control
        e.stopPropagation();
        const willShow = !hint.classList.contains('show');
        closeSettingsHints(hint);      // only one open at a time
        hint.classList.toggle('show', willShow);
        btn.classList.toggle('active', willShow);
        btn.setAttribute('aria-expanded', willShow ? 'true' : 'false');
        if (willShow) positionSettingsHint(btn, hint);
      });
      // Clicks inside an open popover shouldn't close it (outside-click handler below).
      hint.addEventListener('click', e => e.stopPropagation());
      label.appendChild(wrap);
    });
  }
  // Place the popover as a viewport-fixed box centred under (or, if there's no room,
  // above) the icon, so the modal-body's scroll/overflow can't clip it. The caret is
  // shifted to sit under the icon via --caret-x.
  function positionSettingsHint(btn, hint) {
    hint.classList.remove('place-above');
    hint.style.left = '0px';
    hint.style.top = '0px';                       // reset so offsetWidth/Height measure cleanly
    const r = btn.getBoundingClientRect();
    const hw = hint.offsetWidth, hh = hint.offsetHeight, gap = 9, pad = 8;
    let left = r.left + r.width / 2 - hw / 2;
    left = Math.max(pad, Math.min(left, window.innerWidth - hw - pad));
    let top = r.bottom + gap;
    if (top + hh > window.innerHeight - pad && r.top - gap - hh >= pad) {
      top = r.top - gap - hh;                     // flip above when it won't fit below
      hint.classList.add('place-above');
    }
    hint.style.left = left + 'px';
    hint.style.top = top + 'px';
    hint.style.setProperty('--caret-x', (r.left + r.width / 2 - left) + 'px');
  }
  setupSettingsHints();
  // Dismiss any open description when clicking elsewhere in the settings modal, and
  // when the body scrolls (the popover is viewport-fixed and wouldn't follow along).
  document.getElementById('settings-overlay')?.addEventListener('click', () => closeSettingsHints());
  document.querySelector('#settings-form .modal-body')
    ?.addEventListener('scroll', () => closeSettingsHints(), { passive: true });

  async function openSettings() {
    if (menuPanel) menuPanel.hidden = true;
    settingsErr.hidden = true;
    try {
      const r = await fetch('/api/settings');
      if (!r.ok) throw new Error('load failed');
      const s = await r.json();
      SETTING_NUM.forEach(k => { if (setField(k)) setField(k).value = s[k]; });
      setField('alert_on_recovery').checked = !!s.alert_on_recovery;
    } catch (e) {
      settingsErr.textContent = 'Could not load current settings.';
      settingsErr.hidden = false;
    }
    settingsOverlay.hidden = false;
  }
  const closeSettings = () => { settingsOverlay.hidden = true; };

  document.getElementById('btn-settings')?.addEventListener('click', openSettings);
  document.getElementById('settings-close')?.addEventListener('click', closeSettings);
  document.getElementById('settings-cancel')?.addEventListener('click', closeSettings);
  settingsOverlay?.addEventListener('click', e => { if (e.target === settingsOverlay) closeSettings(); });

  settingsForm?.addEventListener('submit', async e => {
    e.preventDefault();
    settingsErr.hidden = true;
    const payload = {};
    for (const k of SETTING_NUM) {
      const el = setField(k);
      if (!el) continue;
      const v = parseInt(el.value, 10);
      if (Number.isNaN(v)) { settingsErr.textContent = 'Please fill in every field with a whole number.'; settingsErr.hidden = false; return; }
      payload[k] = v;
    }
    payload.alert_on_recovery = setField('alert_on_recovery').checked ? 1 : 0;
    const saveBtn = document.getElementById('settings-save');
    saveBtn.disabled = true;
    try {
      const r = await fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error('save failed');
      closeSettings();
      showToast('Settings saved.', 'success');
      fetchAndRender(true);   // frequent-outage flags may change on this refresh
    } catch (err) {
      settingsErr.textContent = 'Could not save settings. Please try again.';
      settingsErr.hidden = false;
    } finally {
      saveBtn.disabled = false;
    }
  });

  // ── Events (overlay + optional pause; the Event Logs tab + Mark-an-Event form) ──
  const evListBody = document.getElementById('ev-list-body');
  const evFormOverlay = document.getElementById('ev-form-overlay');
  const evForm = document.getElementById('ev-form');
  const evDeviceList = document.getElementById('ev-device-list');
  const evFormErr = document.getElementById('ev-form-error');
  const evPauseRow = document.getElementById('ev-pause-row');
  const evDetailOverlay = document.getElementById('ev-detail-overlay');
  const evDetailBody = document.getElementById('ev-detail-body');
  const evDetailTitle = document.getElementById('ev-detail-title');
  const EV_CATS = { maintenance: 'Maintenance', lightning: 'Lightning',
                    power_outage: 'Power outage', scheduled_downtime: 'Scheduled downtime',
                    other: 'Other' };
  const evCatLabel = it => EV_CATS[it.category || 'maintenance'] || (it.category || 'maintenance');
  // The event's TITLE is its free-text field (was "description"), falling back to the
  // category name when none is set. The category is shown as a secondary label.
  // In demo mode the free-text title/description can carry org-identifying detail, so
  // fall back to the generic category name and never surface the custom text.
  const evTitle = it => (!DEMO && it.description && it.description.trim()) ? it.description.trim() : evCatLabel(it);
  const evHasTitle = it => !DEMO && !!(it.description && it.description.trim());
  let evEditGroup = null;   // group_id being edited, or null when creating

  const nodeByIp = ip =>
    topology.switches.find(s => s.ip === ip) || topology.devices.find(d => d.ip === ip);
  // Resolve a device reference (Device ID `name`, preferred) to its node. Falls back
  // to matching by IP so older ?device=<ip> links still work during the migration.
  const nodeByRef = ref => {
    const all = [...(topology.switches || []), ...(topology.devices || [])];
    return all.find(n => n.name === ref) || all.find(n => n.ip === ref) || null;
  };
  // A device's stable API reference for /api/devices/<ref>/… calls: the Device ID
  // (`name`), URL-encoded. Prefer this over the IP — the server resolves it to the
  // current IP, and it survives an IP change. (Storage is still IP-keyed underneath.)
  const devRef = node => encodeURIComponent(node.name);
  const ipLabel = ip => { const n = nodeByIp(ip); return n ? displayName(n) : ip; };
  // Preview device names for an event, collapsing long lists to "A, B, and N more devices".
  function evDevPreview(ips) {
    if (!ips || !ips.length) return 'No devices';
    const names = ips.map(ipLabel);
    const SHOWN = 2;
    if (names.length <= SHOWN + 1) return names.join(', ');
    const extra = names.length - SHOWN;
    return `${names.slice(0, SHOWN).join(', ')}, and ${extra} more device${extra === 1 ? '' : 's'}`;
  }
  const spWhen = iso => new Date(iso + 'Z').toLocaleString('en-US',
    { timeZone: TZ, month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });

  // Loads the current location's events + device pauses (grouped Present/Upcoming/Past).
  let evBuckets = null;
  let evEditMode = false;
  const evSelected = new Set();
  const evKey = ev => ev.group_id || ('single:' + ev.maint_ids[0]);
  const OPEN_ENDED_ISO = '9999-12-31T23:59:59';   // matches routes.OPEN_ENDED_ISO
  const evIsOngoing = end => typeof end === 'string' && end.startsWith('9999');
  const evEndLabel = end => evIsOngoing(end) ? 'ongoing' : spWhen(end);
  // A key that uniquely identifies any row in the list (event or device pause).
  const itemKey = item => item.type === 'pause'
    ? `pause:${item.device_ip}:${item.start}` : evKey(item);
  // "5 minutes · started Jul 19, 9:37 AM" — the device-log style summary line.
  const evWhenLine = (start, end, ongoing) =>
    `${durationText(start, end, ongoing)} · started ${spWhen(start)}`;

  async function loadEvents() {
    evEditMode = false; evSelected.clear();
    // Keep the list where it was on a reload (e.g. after editing an event); only
    // show the "Loading…" placeholder on the very first load, so a refresh after an
    // edit doesn't jump the scroll back to the top.
    const prevScroll = evListBody.scrollTop;
    if (!evBuckets) evListBody.innerHTML = LOG_LOADING;
    try {
      const resp = await fetch('/api/events?' + siteQuery());
      if (resp.status === 401) { window.location = '/login'; return; }
      evBuckets = await resp.json();
      renderEvents();
      evListBody.scrollTop = prevScroll;
    } catch (e) { evListBody.innerHTML = '<div class="log-empty">Failed to load.</div>'; }
    evSyncFooter();
  }

  // Open an event's detail from a "part of an event" chip. Loads the event list
  // (populating evBuckets) then shows the detail popup — works from any log view,
  // since the detail is its own overlay layered on top.
  async function openEventByRef(groupId, maintId) {
    if (!evBuckets) await loadEvents();
    const ev = evAllItems().find(x => x.type === 'event' &&
      (groupId ? x.group_id === groupId : (x.maint_ids || []).includes(maintId)));
    if (ev) renderEventDetail(ev);
    else if (logTabs && !logTabs.hidden) showLogTab('events');
  }

  const evAllItems = () => evBuckets
    ? [...(evBuckets.present || []), ...(evBuckets.upcoming || []), ...(evBuckets.past || [])] : [];

  function evItemHtml(item) {
    if (item.type === 'pause') {
      const label = item.kind === 'notifications' ? 'Notifications paused' : 'Monitoring paused';
      const pkey = itemKey(item);
      // Pauses are selectable in edit mode too — they can be folded into a combined event.
      const pcheck = evEditMode
        ? `<input type="checkbox" class="ev-check" ${evSelected.has(pkey) ? 'checked' : ''}>` : '';
      return `<div class="sp-item ev-pause-item ${evEditMode ? '' : 'clickable'}" data-key="${esc(pkey)}">
        ${pcheck}
        <div class="sp-item-main">
          <div class="sp-item-name"><span class="log-cat-dot scheduled_downtime"></span> ${label} <span class="sp-badge">Device pause</span></div>
          <div class="sp-item-when">${evWhenLine(item.start, item.end, item.ongoing)}</div>
          <div class="sp-item-devs">${esc(ipLabel(item.device_ip))}</div>
        </div>
      </div>`;
    }
    const cat = item.category || 'maintenance';
    let pauseBadge = '';
    if (item.pause) {
      const label = item.pause.mode === 'notifications' ? 'Notifications paused' : 'Monitoring paused';
      pauseBadge = `<span class="sp-badge ${item.pause.status === 'active' ? 'active' : ''}">${label}</span>`;
    }
    // Title = the free-text field; category shown beneath as a label (only when a real
    // title exists, so it isn't duplicated when the title already IS the category).
    const catSub = evHasTitle(item) ? `<div class="sp-item-cat">${esc(evCatLabel(item))}</div>` : '';
    const key = evKey(item);
    const check = evEditMode
      ? `<input type="checkbox" class="ev-check" ${evSelected.has(key) ? 'checked' : ''}>` : '';
    return `<div class="sp-item ev-event-item ${evEditMode ? '' : 'clickable'}" data-key="${esc(key)}">
      ${check}
      <div class="sp-item-main">
        <div class="sp-item-name"><span class="log-cat-dot ${cat}"></span> ${esc(evTitle(item))} ${pauseBadge}</div>
        ${catSub}
        <div class="sp-item-when">${evWhenLine(item.start, item.end, evIsOngoing(item.end))}</div>
        <div class="sp-item-devs">${esc(evDevPreview(item.device_ips))}</div>
      </div>
    </div>`;
  }

  function renderEvents() {
    const sections = [['present', 'Happening now'], ['upcoming', 'Upcoming'], ['past', 'Past']];
    if (!evAllItems().length) {
      evListBody.innerHTML = '<div class="log-empty">No events at this location.</div>'; return;
    }
    evListBody.innerHTML = sections.map(([key, title]) => {
      const items = evBuckets[key] || [];
      if (!items.length) return '';
      return `<div class="ev-section"><div class="ev-section-label">${title}</div>${items.map(evItemHtml).join('')}</div>`;
    }).join('');
  }

  evListBody.addEventListener('click', e => {
    const item = e.target.closest('.ev-event-item, .ev-pause-item');
    if (!item) return;
    const key = item.dataset.key;
    // Edit mode multi-selects events AND device pauses (both can be combined).
    if (evEditMode) {
      if (evSelected.has(key)) evSelected.delete(key); else evSelected.add(key);
      const cb = item.querySelector('.ev-check'); if (cb) cb.checked = evSelected.has(key);
      evSyncFooter();
      return;
    }
    const obj = evAllItems().find(x => itemKey(x) === key);
    if (!obj) return;
    if (obj.type === 'pause') renderPauseDetail(obj);
    else renderEventDetail(obj);
  });

  // The event/pause detail opens as its own popup layered over the Event Logs list.
  function openEvDetail() { evDetailOverlay.hidden = false; }
  function closeEvDetail() { evDetailOverlay.hidden = true; }
  document.getElementById('ev-detail-close').addEventListener('click', closeEvDetail);
  evDetailOverlay.addEventListener('click', e => { if (e.target === evDetailOverlay) closeEvDetail(); });

  // Full detail view for one event — opens as its own popup over the Event Logs list.
  function renderEventDetail(ev) {
    const cat = ev.category || 'maintenance';
    evDetailTitle.textContent = evTitle(ev);
    let pauseLine = '';
    if (ev.pause) {
      const m = ev.pause.mode === 'notifications' ? 'Slack notifications' : 'Monitoring';
      pauseLine = `<div class="detail-row"><span>Pause</span><span>${m} — ${esc(ev.pause.status)}</span></div>`;
    }
    const editable = isAdmin() && !!ev.group_id;   // viewers get a read-only detail view
    // For an ONGOING event, each device can be resolved independently (its window
    // capped when it's fixed) so later unrelated outages aren't tagged, while the
    // event stays open for the others.
    // Only devices STILL being affected (open-ended windows) are listed; resolved ones
    // are dropped. When none remain, the section is omitted entirely.
    const devList = ev.devices || ev.device_ips.map(ip => ({ ip, start: ev.start, end: ev.end }));
    const affected = devList.filter(dv => evIsOngoing(dv.end));
    const devHtml = affected.map(dv => {
      const ip = dv.ip;
      const n = nodeByIp(ip); const st = n ? n.status : 'unknown';
      const ctrl = editable
        ? `<button class="ap-child-action ev-resolve-btn" data-ip="${esc(ip)}" title="End this device's part of the event (it's fixed)">Resolve</button>` : '';
      return `<div class="ap-child ${st}"><span class="device-status-dot ${st}"></span><span class="ap-child-name">${esc(ipLabel(ip))}</span>${ctrl}</div>`;
    }).join('');
    const affectedSection = affected.length ? `
        <div class="detail-row"><span>Still affected</span><span>${affected.length}</span></div>
        ${editable ? '<div class="ev-detail-hint">Resolve a device once it\'s fixed so its later, unrelated outages aren\'t counted as part of this ongoing event.</div>' : ''}
        <div class="ap-child-list">${devHtml}</div>` : '';
    evDetailBody.innerHTML = `
      <div class="ev-detail">
        <h3 class="ev-detail-cat"><span class="log-cat-dot ${cat}"></span> ${esc(evTitle(ev))}</h3>
        <div class="detail-row"><span>Category</span><span>${esc(evCatLabel(ev))}</span></div>
        <div class="detail-row"><span>When</span><span>${spWhen(ev.start)} → ${evEndLabel(ev.end)}</span></div>
        <div class="detail-row"><span>Duration</span><span>${durationText(ev.start, ev.end, evIsOngoing(ev.end))}</span></div>
        ${pauseLine}
        ${affectedSection}
        <div class="ev-detail-activity">
          <div class="ev-section-head">
            <div class="ev-section-label">Device activity during this event</div>
            <button type="button" class="btn btn-soft" id="ev-detail-export" disabled>Export Excel</button>
          </div>
          <div id="ev-detail-log" class="ev-detail-log"><div class="log-empty">Loading…</div></div>
        </div>
      </div>
      ${isAdmin() ? `<div class="ev-detail-actions">
        ${editable ? '<button class="btn" id="ev-detail-edit-all">Edit</button>' : ''}
        <button class="btn btn-danger" id="ev-detail-delete">Delete</button>
      </div>` : ''}`;
    openEvDetail();
    loadEventActivity(ev);
    // Per-device resolve / reopen (only shown for an ongoing event).
    async function evResolveDevice(ip, payload) {
      const resp = await fetch(`/api/events/${encodeURIComponent(ev.group_id)}/resolve-device?` + siteQuery(), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ip, ...payload }) });
      if (resp.status === 401) { window.location = '/login'; return; }
      if (!resp.ok) { const d = await resp.json().catch(() => ({})); alert(d.error || 'Could not update.'); return; }
      await loadEvents(); fetchAndRender(true);
      const updated = evAllItems().find(x => x.type === 'event' && x.group_id === ev.group_id);
      if (updated) renderEventDetail(updated); else closeEvDetail();
    }
    evDetailBody.querySelectorAll('.ev-reopen-btn').forEach(b =>
      b.addEventListener('click', () => evResolveDevice(b.dataset.ip, { reopen: true })));
    evDetailBody.querySelectorAll('.ev-resolve-btn').forEach(b =>
      b.addEventListener('click', () => {
        // Reveal an inline "when was it fixed?" date/time (default now) in the row.
        const now = new Date();
        const d = now.toLocaleDateString('en-CA', { timeZone: TZ });
        const t = now.toLocaleTimeString('en-GB', { timeZone: TZ, hour: '2-digit', minute: '2-digit' });
        const form = document.createElement('span');
        form.className = 'ev-resolve-form';
        form.innerHTML = `<input type="date" class="ev-res-date" value="${d}"><input type="time" class="ev-res-time" value="${t}"><button class="ap-child-action ev-res-save">Save</button><button class="ap-child-action ev-res-cancel">Cancel</button>`;
        b.replaceWith(form);
        form.querySelector('.ev-res-cancel').addEventListener('click', () => renderEventDetail(ev));
        form.querySelector('.ev-res-save').addEventListener('click', () => evResolveDevice(b.dataset.ip, {
          end_date: form.querySelector('.ev-res-date').value,
          end_time: form.querySelector('.ev-res-time').value }));
      }));
    document.getElementById('ev-detail-edit-all')?.addEventListener('click', () => renderEventEdit(ev));
    const evDelBtn = document.getElementById('ev-detail-delete');
    if (evDelBtn) evDelBtn.onclick = async (evt) => {
      evt.target.disabled = true;
      const url = ev.group_id ? `/api/events/${encodeURIComponent(ev.group_id)}?` + siteQuery()
                              : `/api/maintenance/${ev.maint_ids[0]}`;
      const resp = await fetch(url, { method: 'DELETE' });
      if (resp.status === 401) { window.location = '/login'; return; }
      closeEvDetail();
      await loadEvents(); fetchAndRender(true);
    };
  }

  // A single read-only activity row (outage / event / unknown / pause) for the
  // event detail — same text/coloring as the main Device Logs, minus the actions.
  function evActivityRow(e) {
    const dotClass = e.event === 'maintenance' ? (e.category || 'maintenance') : e.event;
    return `<div class="log-entry">
      <span class="log-cat-dot ${dotClass}"></span>
      <span class="log-text">${entryText(e)}</span></div>`;
  }

  // Fetch + render each included device's log clipped to the event window.
  async function loadEventActivity(ev) {
    const box = document.getElementById('ev-detail-log');
    if (!box) return;
    try {
      const resp = await fetch('/api/events/device-log?' + siteQuery(), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_ips: ev.device_ips, start: ev.start, end: ev.end }) });
      if (!resp.ok) { box.innerHTML = '<div class="log-empty">Failed to load activity.</div>'; return; }
      const entries = (await resp.json()).entries || [];
      if (!entries.length) { box.innerHTML = '<div class="log-empty">No device activity during this event.</div>'; return; }
      // Insert a date header whenever the day changes (entries are newest-first), so
      // the activity shows dates, not just clock times.
      let html = '', day = null;
      for (const en of entries) {
        const key = en.start ? estDayKey(en.start) : null;
        if (key && key !== day) { day = key; html += `<div class="log-day">${estDayLabel(en.start)}</div>`; }
        html += evActivityRow(en);
      }
      box.innerHTML = html;
      // Enable the "Export Excel" button now that we have the rows (incl. ongoing ones).
      const exportBtn = document.getElementById('ev-detail-export');
      if (exportBtn) {
        exportBtn.disabled = false;
        exportBtn.onclick = () => downloadXlsx(
          `event_${safeName(evTitle(ev))}_${stamp()}`,
          evTitle(ev).slice(0, 31), XLSX_HEADERS, entriesToRows(entries));
      }
    } catch (e) { box.innerHTML = '<div class="log-empty">Failed to load activity.</div>'; }
  }

  // Detail view for a device monitoring pause (view-only).
  function renderPauseDetail(p) {
    const label = p.kind === 'notifications' ? 'Notifications paused' : 'Monitoring paused';
    evDetailTitle.textContent = label;
    const n = nodeByIp(p.device_ip); const st = n ? n.status : 'unknown';
    evDetailBody.innerHTML = `
      <div class="ev-detail">
        <h3 class="ev-detail-cat"><span class="log-cat-dot scheduled_downtime"></span> ${label}</h3>
        <div class="detail-row"><span>When</span><span>${spWhen(p.start)} → ${p.ongoing ? 'ongoing' : spWhen(p.end)}</span></div>
        <div class="detail-row"><span>Duration</span><span>${durationText(p.start, p.end, p.ongoing)}</span></div>
        <div class="detail-row"><span>Device</span><span></span></div>
        <div class="ap-child-list"><div class="ap-child ${st}"><span class="device-status-dot ${st}"></span>${esc(ipLabel(p.device_ip))}</div></div>
      </div>`;
    openEvDetail();
  }

  // Add/remove devices on an event (rendered inside the detail popup).
  // All-in-one edit view: category, description, start/end, and devices at once.
  function renderEventEdit(ev) {
    const cat = ev.category || 'maintenance';
    const ongoing = evIsOngoing(ev.end);
    const estDate = iso => new Date(iso + 'Z').toLocaleDateString('en-CA', { timeZone: TZ });
    const estTime = iso => new Date(iso + 'Z').toLocaleTimeString('en-GB', { timeZone: TZ, hour: '2-digit', minute: '2-digit' });
    const catOpts = Object.entries(EV_CATS)
      .map(([v, l]) => `<option value="${v}" ${v === cat ? 'selected' : ''}>${esc(l)}</option>`).join('');
    evDetailTitle.textContent = 'Edit event';
    evDetailBody.innerHTML = `
      <button class="btn btn-soft" id="ev-edit-back">← Back</button>
      <div class="form-row" style="margin-top:10px"><label for="ev-edit-cat">Event</label>
        <select id="ev-edit-cat">${catOpts}</select></div>
      <div class="notes-block" style="margin-top:8px"><label for="ev-edit-desc">Title <span class="optional">(defaults to the category)</span></label>
        <textarea id="ev-edit-desc" maxlength="500" placeholder="Add a title…">${esc(DEMO ? '' : (ev.description || ''))}</textarea></div>
      <div class="sp-datetime" style="margin-top:8px">
        <div class="form-row"><label>Start date</label><input id="ev-edit-sd" type="date" value="${estDate(ev.start)}"></div>
        <div class="form-row"><label>Start time</label><input id="ev-edit-st" type="time" value="${estTime(ev.start)}"></div>
      </div>
      <label class="ev-check-row"><input type="checkbox" id="ev-edit-ongoing" ${ongoing ? 'checked' : ''}> Ongoing (no end date)</label>
      <div class="sp-datetime" id="ev-edit-endrow" ${ongoing ? 'hidden' : ''}>
        <div class="form-row"><label>End date</label><input id="ev-edit-ed" type="date" value="${ongoing ? '' : estDate(ev.end)}"></div>
        <div class="form-row"><label>End time</label><input id="ev-edit-et" type="time" value="${ongoing ? '' : estTime(ev.end)}"></div>
      </div>
      <div class="form-row" style="margin-top:10px"><label>Devices <span class="optional">(Device list = full window; Activity log = capped to that outage)</span></label>
        <div id="ev-edit-devs" class="sp-device-list"></div></div>
      <div id="ev-edit-err" class="modal-error" hidden></div>
      <div class="ev-detail-actions"><button class="btn btn-primary" id="ev-edit-save">Save changes</button></div>`;
    openEvDetail();
    const picker = document.getElementById('ev-edit-devs');
    buildDevicePicker(picker, ev.device_ips, { mode: 'both' });
    const ongoingCb = document.getElementById('ev-edit-ongoing');
    ongoingCb.addEventListener('change', () => { document.getElementById('ev-edit-endrow').hidden = ongoingCb.checked; });
    document.getElementById('ev-edit-back').onclick = () => renderEventDetail(ev);
    document.getElementById('ev-edit-save').onclick = async (evt) => {
      const err = document.getElementById('ev-edit-err');
      const sel = picker._dpGetSelection();
      const chosen = new Set([...sel.full, ...sel.log.map(x => x.ip)]);
      if (!chosen.size) { err.textContent = 'Keep at least one device (use Delete to remove the event).'; err.hidden = false; return; }
      evt.target.disabled = true;
      // 1) Category / description / times.
      const on = ongoingCb.checked;
      const patch = {
        category: document.getElementById('ev-edit-cat').value,
        description: document.getElementById('ev-edit-desc').value.trim(),
        start_date: document.getElementById('ev-edit-sd').value,
        start_time: document.getElementById('ev-edit-st').value,
      };
      if (!on) { patch.end_date = document.getElementById('ev-edit-ed').value; patch.end_time = document.getElementById('ev-edit-et').value; }
      let resp = await fetch(`/api/events/${encodeURIComponent(ev.group_id)}?` + siteQuery(), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
      if (resp.status === 401) { window.location = '/login'; return; }
      if (!resp.ok) { const d = await resp.json().catch(() => ({})); err.textContent = d.error || 'Could not save changes.'; err.hidden = false; evt.target.disabled = false; return; }
      // 2) Device diff (only when something changed).
      const cur = new Set(ev.device_ips);
      const add = sel.full.filter(ip => !cur.has(ip));
      const add_log = sel.log.filter(x => !cur.has(x.ip));
      const remove = [...cur].filter(ip => !chosen.has(ip));
      if (add.length || add_log.length || remove.length) {
        await fetch(`/api/events/${encodeURIComponent(ev.group_id)}/devices?` + siteQuery(), {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ add, add_log, remove }) }).catch(() => {});
      }
      await loadEvents(); fetchAndRender(true);
      const updated = evAllItems().find(x => x.type === 'event' && x.group_id === ev.group_id);
      if (updated) renderEventDetail(updated); else closeEvDetail();
    };
  }

  // ── Event Logs edit mode (multi-select → Combine / Delete) ────────
  const evEditModeBtn = document.getElementById('ev-edit-mode-btn');
  const evEditToolbar  = document.getElementById('ev-edit-toolbar');
  const evNewBtn       = document.getElementById('ev-new-btn');
  function evSyncFooter() {
    const anyItems = evAllItems().length > 0;
    if (evEditModeBtn) evEditModeBtn.hidden = evEditMode || !anyItems;
    if (evNewBtn) evNewBtn.hidden = evEditMode;
    if (evEditToolbar) evEditToolbar.hidden = !evEditMode;
    if (evEditMode) {
      document.getElementById('ev-sel-count').textContent = `${evSelected.size} selected`;
      // Delete only removes events; device pauses can't be deleted here (only combined).
      const eventKeys = [...evSelected].filter(k => !k.startsWith('pause:'));
      document.getElementById('ev-delete-btn').disabled = eventKeys.length < 1;
      // Combine merges any ≥2 selected items (events, single marks, and/or pauses).
      document.getElementById('ev-combine-btn').disabled = evSelected.size < 2;
    }
  }
  evEditModeBtn?.addEventListener('click', () => { evEditMode = true; evSelected.clear(); renderEvents(); evSyncFooter(); });
  document.getElementById('ev-edit-done-btn')?.addEventListener('click', () => { evEditMode = false; evSelected.clear(); renderEvents(); evSyncFooter(); });
  document.getElementById('ev-delete-btn')?.addEventListener('click', async () => {
    // Device pauses aren't deletable here — only the selected events are removed.
    const eventKeys = [...evSelected].filter(k => !k.startsWith('pause:'));
    if (!eventKeys.length || !confirm(`Delete ${eventKeys.length} event(s)? This can't be undone.`)) return;
    for (const key of eventKeys) {
      const url = key.startsWith('single:') ? `/api/maintenance/${key.slice(7)}`
                : `/api/events/${encodeURIComponent(key)}?` + siteQuery();
      await fetch(url, { method: 'DELETE' }).catch(() => {});
    }
    await loadEvents(); fetchAndRender(true);
  });
  document.getElementById('ev-combine-btn')?.addEventListener('click', async () => {
    const keys = [...evSelected];
    if (keys.length < 2) return;
    const group_ids = keys.filter(k => !k.startsWith('single:') && !k.startsWith('pause:'));
    const maint_ids = keys.filter(k => k.startsWith('single:')).map(k => k.slice(7));
    // Device pauses fold into the event by their window/device (they aren't deleted).
    const pauses = keys.filter(k => k.startsWith('pause:')).map(k => {
      const p = evAllItems().find(x => x.type === 'pause' && itemKey(x) === k);
      return p ? { device_ip: p.device_ip, start: p.start, end: p.end } : null;
    }).filter(Boolean);
    const resp = await fetch('/api/events/combine?' + siteQuery(), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_ids, maint_ids, pauses }) });
    if (resp.status === 401) { window.location = '/login'; return; }
    evEditMode = false; evSelected.clear();
    await loadEvents(); fetchAndRender(true); evSyncFooter();
  });

  // Build a device picker into `container`. It has two toggle views — both multi-
  // select, selection kept in one shared IP set (`st.selected`) so switching views
  // preserves picks:
  //   • "Device list" — every device grouped by switch (a switch toggles its APs),
  //     with a live search box (name or IP).
  //   • "Activity log" — devices that have recent site activity, each with its latest
  //     outage/pause lines shown; check the device to include it.
  // `preIps` pre-selects those devices. Read the result with `container._dpGetIps()`
  // and add more with `container._dpAdd([...])`.
  const _dpDot = n => `<span class="device-status-dot ${n && n.status || 'unknown'}"></span>`;
  // `opts.mode`: 'both' (tabs), 'list' (device list only), or 'log' (activity log only).
  function buildDevicePicker(container, preIps, opts = {}) {
    const mode = opts.mode || 'both';
    // `selected` = devices picked from the DEVICE LIST (full event window). `logSel` =
    // entries picked from the ACTIVITY LOG, keyed "ip|start" → {ip, start, end}; those
    // devices join capped at the entry's end (auto-resolved). A device in both counts
    // as a full-window pick.
    const st = container._dp = { selected: new Set(preIps || []), logSel: new Map(),
                                 view: mode === 'log' ? 'activity' : 'list', search: '', activity: null };
    const logIps = () => new Set([...st.logSel.values()].map(v => v.ip));
    const tabsHtml = mode === 'both' ? `<div class="dp-tabs">
          <button type="button" class="dp-tab active" data-dp-view="list">Device list</button>
          <button type="button" class="dp-tab" data-dp-view="activity">Activity log</button>
        </div>` : '';
    const searchHtml = mode === 'log' ? '' : '<input type="text" class="dp-search" placeholder="Search name or IP…">';
    container.innerHTML = `
      ${(tabsHtml || searchHtml) ? `<div class="dp-toolbar">${tabsHtml}${searchHtml}</div>` : ''}
      <div class="dp-body"></div>`;
    const body = container.querySelector('.dp-body');
    const search = container.querySelector('.dp-search');

    function renderList() {
      const term = st.search.trim().toLowerCase();
      const lset = logIps();                       // devices picked via activity also show ticked
      const on = ip => st.selected.has(ip) || lset.has(ip);
      const match = n => !term || displayName(n).toLowerCase().includes(term)
                         || (n.ip || '').includes(term);
      const bySwitch = {};
      topology.devices.forEach(d => { (bySwitch[d.switch_name || '__none__'] ||= []).push(d); });
      const switches = [...topology.switches].sort((a, b) => displayName(a).localeCompare(displayName(b)));
      let html = '';
      switches.forEach(s => {
        const kids = (bySwitch[s.name] || []).sort((a, b) => displayName(a).localeCompare(displayName(b)));
        const swMatch = match(s);
        const listKids = swMatch ? kids : kids.filter(match);   // switch match → show all APs
        if (!swMatch && !listKids.length) return;
        html += `<div class="sp-dev-group">
          <label class="sp-dev-sw"><input type="checkbox" class="sp-sw" data-ip="${esc(s.ip)}" data-switch="${esc(s.name)}" ${on(s.ip) ? 'checked' : ''}>
          ${_dpDot(s)}<strong>${esc(displayName(s))}</strong></label>`;
        listKids.forEach(d => {
          html += `<label class="sp-dev-child"><input type="checkbox" class="sp-dev" data-ip="${esc(d.ip)}" data-switch="${esc(s.name)}" ${on(d.ip) ? 'checked' : ''}> ${_dpDot(d)}${esc(displayName(d))}</label>`;
        });
        html += `</div>`;
      });
      const orphans = (bySwitch['__none__'] || []).sort((a, b) => displayName(a).localeCompare(displayName(b))).filter(match);
      if (orphans.length) {
        html += `<div class="sp-dev-group"><div class="sp-dev-sw"><em>No switch</em></div>`;
        orphans.forEach(d => html += `<label class="sp-dev-child"><input type="checkbox" class="sp-dev" data-ip="${esc(d.ip)}" ${on(d.ip) ? 'checked' : ''}> ${_dpDot(d)}${esc(displayName(d))}</label>`);
        html += `</div>`;
      }
      body.innerHTML = html || '<div class="log-empty">No devices match your search.</div>';
    }

    async function renderActivity() {
      if (!st.activity) {
        body.innerHTML = '<div class="log-empty">Loading…</div>';
        try {
          const resp = await fetch('/api/log?' + siteQuery());
          st.activity = (await resp.json()).entries || [];
        } catch (e) { st.activity = []; }
      }
      if (!st.activity.length) { body.innerHTML = '<div class="log-empty">No recent activity at this location.</div>'; return; }
      // Render the activity log exactly as the Device Logs tab does — chronological
      // (newest first) with day separators — but each device row is a checkbox that
      // toggles that device's inclusion. App-downtime rows (no device) aren't selectable.
      let html = '', day = null;
      for (const en of st.activity) {
        const dk = en.start ? estDayKey(en.start) : null;
        if (dk && dk !== day) { day = dk; html += `<div class="log-day">${estDayLabel(en.start)}</div>`; }
        const dotClass = en.event === 'maintenance' ? (en.category || 'maintenance') : en.event;
        if (en.ip) {
          const key = `${en.ip}|${en.start}`;
          // A whole-device (list) pick, or this specific entry being ticked, shows checked.
          const checked = st.logSel.has(key) || st.selected.has(en.ip);
          html += `<label class="dp-log-entry"><input type="checkbox" class="sp-dev" data-ip="${esc(en.ip)}" data-start="${esc(en.start)}" data-end="${esc(en.end || '')}" ${checked ? 'checked' : ''}>
            <span class="log-cat-dot ${dotClass}"></span><span class="log-text">${entryText(en)}</span></label>`;
        } else {
          html += `<div class="dp-log-entry dp-log-noselect"><span class="log-cat-dot ${dotClass}"></span><span class="log-text">${entryText(en)}</span></div>`;
        }
      }
      body.innerHTML = html;
    }

    function render() {
      container.querySelectorAll('.dp-tab').forEach(b => b.classList.toggle('active', b.dataset.dpView === st.view));
      if (search) search.style.display = st.view === 'list' ? '' : 'none';
      if (st.view === 'list') renderList(); else renderActivity();
    }

    container.querySelector('.dp-tabs')?.addEventListener('click', e => {
      const t = e.target.closest('.dp-tab');
      if (t) { st.view = t.dataset.dpView; render(); }
    });
    search?.addEventListener('input', () => { st.search = search.value; if (st.view === 'list') renderList(); });
    // Drop every activity-log entry belonging to `ip` (used when a list pick / unpick
    // overrides a log pick).
    const clearLogFor = ip => {
      for (const k of [...st.logSel.keys()]) if (st.logSel.get(k).ip === ip) st.logSel.delete(k);
    };
    body.addEventListener('change', e => {
      const cb = e.target;
      if (cb.classList.contains('sp-sw')) {                    // list view: switch cascades to APs
        const ips = [cb.dataset.ip, ...topology.devices.filter(d => d.switch_name === cb.dataset.switch).map(d => d.ip)];
        ips.forEach(ip => { clearLogFor(ip); if (cb.checked) st.selected.add(ip); else st.selected.delete(ip); });
        body.querySelectorAll(`.sp-dev[data-switch="${CSS.escape(cb.dataset.switch)}"]`)
          .forEach(c => { c.checked = cb.checked; });
      } else if (cb.classList.contains('sp-dev') && st.view === 'activity') {
        // Activity view: each row is a specific log entry → capped window for that ip.
        const key = `${cb.dataset.ip}|${cb.dataset.start}`;
        if (cb.checked) {
          st.logSel.set(key, { ip: cb.dataset.ip, start: cb.dataset.start,
                               end: cb.dataset.end || OPEN_ENDED_ISO });
        } else {
          st.logSel.delete(key);
          st.selected.delete(cb.dataset.ip);   // an earlier list/full pick is also cleared
        }
      } else if (cb.classList.contains('sp-dev')) {            // list view device
        clearLogFor(cb.dataset.ip);
        if (cb.checked) st.selected.add(cb.dataset.ip); else st.selected.delete(cb.dataset.ip);
      }
    });

    // Public API for the forms.
    // Split selection: `full` = device-list picks (full event window); `log` = activity
    // picks collapsed per device to {ip, start, end} (min-start / max-end → auto-resolved).
    container._dpGetSelection = () => {
      const logMap = new Map();
      for (const v of st.logSel.values()) {
        if (st.selected.has(v.ip)) continue;                  // a full pick wins
        const cur = logMap.get(v.ip);
        logMap.set(v.ip, cur
          ? { ip: v.ip, start: v.start < cur.start ? v.start : cur.start, end: v.end > cur.end ? v.end : cur.end }
          : { ip: v.ip, start: v.start, end: v.end });
      }
      return { full: [...st.selected], log: [...logMap.values()] };
    };
    container._dpGetIps = () => {
      const sel = container._dpGetSelection();
      return [...new Set([...sel.full, ...sel.log.map(x => x.ip)])];
    };
    container._dpAdd = ips => {
      (ips || []).forEach(ip => {
        st.selected.add(ip);
        const sw = topology.switches.find(s => s.ip === ip);   // a switch pulls in its APs
        if (sw) topology.devices.filter(d => d.switch_name === sw.name).forEach(d => st.selected.add(d.ip));
      });
      render();
    };
    render();
  }
  // Mark form: main picker is LOG-ONLY (pick the specific outages that were part of it).
  const buildEvLogPicker = () => buildDevicePicker(evDeviceList, [], { mode: 'log' });
  const evOngoing = document.getElementById('ev-ongoing');
  const evAddStart = document.getElementById('ev-add-start');
  const evAddEnd = document.getElementById('ev-add-end');
  const evAffectedList = document.getElementById('ev-affected-list');
  const evShow = (id, on) => { const el = document.getElementById(id); if (el) el.hidden = !on; };

  function evSyncOngoing() {
    const on = evOngoing.checked;
    evShow('ev-affected-row', on);
    evShow('ev-pause-row', on);
    evShow('ev-add-end-row', !on);           // an ongoing event has no end
    if (on) { evAddEnd.checked = false; evShow('ev-end-row', false);
              if (evAffectedList && !evAffectedList._dp) buildDevicePicker(evAffectedList, [], { mode: 'list' }); }
  }
  evOngoing?.addEventListener('change', evSyncOngoing);
  evAddStart?.addEventListener('change', () => evShow('ev-start-row', evAddStart.checked));
  evAddEnd?.addEventListener('change', () => evShow('ev-end-row', evAddEnd.checked));

  // create: openEvForm(). edit: openEvForm(null, eventObj) — a slimmed form (category /
  // description / start / end only; devices via the detail's Edit devices & resolve).
  function openEvForm(preselectIp, editEv) {
    evForm.reset();
    evFormErr.hidden = true;
    evEditGroup = editEv ? editEv.group_id : null;
    const isEdit = !!editEv;
    evForm.querySelectorAll('.ev-create-only').forEach(el => { el.hidden = isEdit; });
    document.getElementById('ev-form-title').textContent = isEdit ? 'Edit event' : 'Mark an event';
    document.getElementById('ev-form-submit').textContent = isEdit ? 'Save changes' : 'Save event';
    const estDate = iso => new Date(iso + 'Z').toLocaleDateString('en-CA', { timeZone: TZ });
    const estTime = iso => new Date(iso + 'Z').toLocaleTimeString('en-GB', { timeZone: TZ, hour: '2-digit', minute: '2-digit' });

    // Reset toggles.
    evOngoing.checked = false;
    evAddStart.checked = false; evShow('ev-start-row', false);
    evAddEnd.checked = false; evShow('ev-end-row', false);
    evShow('ev-affected-row', false); evShow('ev-pause-row', false); evShow('ev-add-end-row', !isEdit);

    if (isEdit) {
      document.getElementById('ev-category').value = editEv.category || 'maintenance';
      document.getElementById('ev-description').value = DEMO ? '' : (editEv.description || '');
      evAddStart.checked = true; evShow('ev-start-row', true);
      document.getElementById('ev-start-date').value = estDate(editEv.start);
      document.getElementById('ev-start-time').value = estTime(editEv.start);
      if (!evIsOngoing(editEv.end)) {
        evAddEnd.checked = true; evShow('ev-end-row', true);
        document.getElementById('ev-end-date').value = estDate(editEv.end);
        document.getElementById('ev-end-time').value = estTime(editEv.end);
      }
    } else {
      buildEvLogPicker();
      if (evAffectedList) { evAffectedList.innerHTML = ''; evAffectedList._dp = null; }
    }
    evFormOverlay.hidden = false;
  }
  window.openEvForm = openEvForm;

  document.getElementById('ev-new-btn')?.addEventListener('click', () => openEvForm());
  document.getElementById('ev-form-close')?.addEventListener('click', () => { evFormOverlay.hidden = true; });
  document.getElementById('ev-form-cancel')?.addEventListener('click', () => { evFormOverlay.hidden = true; });
  evFormOverlay?.addEventListener('click', e => { if (e.target === evFormOverlay) evFormOverlay.hidden = true; });

  evForm?.addEventListener('submit', async e => {
    e.preventDefault();
    const payload = {
      category: document.getElementById('ev-category').value,
      description: document.getElementById('ev-description').value.trim(),
    };
    if (evAddStart.checked) {
      payload.start_date = document.getElementById('ev-start-date').value;
      payload.start_time = document.getElementById('ev-start-time').value;
    }
    if (evAddEnd.checked && !document.getElementById('ev-add-end-row').hidden) {
      payload.end_date = document.getElementById('ev-end-date').value;
      payload.end_time = document.getElementById('ev-end-time').value;
    }
    if (!evEditGroup) {
      const sel = evDeviceList._dpGetSelection();                 // log-only → sel.log
      const affected = (evOngoing.checked && evAffectedList._dp) ? evAffectedList._dpGetIps() : [];
      if (!sel.log.length && !affected.length) {
        evFormErr.textContent = 'Pick at least one outage from the activity log, or mark a device as still affected.';
        evFormErr.hidden = false; return;
      }
      if (sel.log.length) payload.log_devices = sel.log;
      if (evOngoing.checked) {
        payload.ongoing = true;
        payload.affected_ips = affected;
        const mode = evForm.querySelector('input[name="ev-pause"]:checked')?.value;
        if (mode && mode !== 'none') payload.pause_mode = mode;
      }
    }
    const submit = document.getElementById('ev-form-submit');
    submit.disabled = true;
    try {
      const url = evEditGroup ? `/api/events/${encodeURIComponent(evEditGroup)}?` + siteQuery()
                              : '/api/events?' + siteQuery();
      const resp = await fetch(url, { method: evEditGroup ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      if (resp.status === 401) { window.location = '/login'; return; }
      const data = await resp.json();
      if (!resp.ok) { evFormErr.textContent = data.error || 'Could not save the event.'; evFormErr.hidden = false; return; }
      evFormOverlay.hidden = true;
      logTabs.hidden = false; showLogTab('events'); openLog();
      fetchAndRender(true);
    } catch (e) {
      evFormErr.textContent = 'Network error — please try again.'; evFormErr.hidden = false;
    } finally { submit.disabled = false; }
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !evFormOverlay.hidden) evFormOverlay.hidden = true;
  });

  // ── Schedule (future) event — all future-events / pause infrastructure ─────
  const schedOverlay = document.getElementById('sched-form-overlay');
  const schedForm = document.getElementById('sched-form');
  const schedErr = document.getElementById('sched-form-error');
  const schedDeviceList = document.getElementById('sched-device-list');
  function openSchedForm(preselectIp) {
    schedForm.reset();
    schedErr.hidden = true;
    buildDevicePicker(schedDeviceList, preselectIp ? [preselectIp] : [], { mode: 'list' });
    document.getElementById('sched-start-date').value = new Date().toLocaleDateString('en-CA', { timeZone: TZ });
    schedOverlay.hidden = false;
  }
  window.openSchedForm = openSchedForm;
  document.getElementById('sched-new-btn')?.addEventListener('click', () => openSchedForm());
  document.getElementById('sched-form-close')?.addEventListener('click', () => { schedOverlay.hidden = true; });
  document.getElementById('sched-form-cancel')?.addEventListener('click', () => { schedOverlay.hidden = true; });
  schedOverlay?.addEventListener('click', e => { if (e.target === schedOverlay) schedOverlay.hidden = true; });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && schedOverlay && !schedOverlay.hidden) schedOverlay.hidden = true; });

  schedForm?.addEventListener('submit', async e => {
    e.preventDefault();
    const ips = schedDeviceList._dpGetIps();
    if (!ips.length) { schedErr.textContent = 'Select at least one device.'; schedErr.hidden = false; return; }
    const mode = schedForm.querySelector('input[name="sched-pause"]:checked')?.value;
    const payload = {
      category: document.getElementById('sched-category').value,
      description: document.getElementById('sched-description').value.trim(),
      start_date: document.getElementById('sched-start-date').value,
      start_time: document.getElementById('sched-start-time').value,
      end_date: document.getElementById('sched-end-date').value,
      end_time: document.getElementById('sched-end-time').value,
      device_ips: ips,
    };
    if (mode && mode !== 'none') payload.pause_mode = mode;
    const submit = schedForm.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const resp = await fetch('/api/events/schedule?' + siteQuery(), {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      if (resp.status === 401) { window.location = '/login'; return; }
      const data = await resp.json();
      if (!resp.ok) { schedErr.textContent = data.error || 'Could not schedule the event.'; schedErr.hidden = false; return; }
      schedOverlay.hidden = true;
      logTabs.hidden = false; showLogTab('events'); openLog();
      fetchAndRender(true);
    } catch (e) {
      schedErr.textContent = 'Network error — please try again.'; schedErr.hidden = false;
    } finally { submit.disabled = false; }
  });

  // ── User activity (read-only audit trail; a tab in the logs view) ──
  const uaBody = document.getElementById('ua-body');
  const uaFilters = document.getElementById('ua-filters');
  let uaEntries = [];

  // Show users by uniqname. New rows already store it; older rows stored the full
  // email, so strip any @domain for display (jdoe@example.com → jdoe).
  const uaActor = a => (a && a.includes('@')) ? a.split('@')[0] : (a || '');

  // Bucket an entry into a filterable action category; the category also drives
  // the row's dot color (see .ua-dot.<category> in CSS):
  //   added (green), deleted (red),
  //   changed_monitoring (purple) — pause/resume, schedule a pause, cancel a pause,
  //   changed_maintenance (blue)  — mark / edit / unmark maintenance,
  //   other (grey)                — everything else (device/note edits, locations, …).
  function uaCategory(e) {
    if (e.endpoint === 'main.add_device') return 'added';
    if (e.endpoint === 'main.delete_device') return 'deleted';
    if (e.endpoint === 'main.set_device_enabled'
        || e.endpoint === 'main.scheduled_pauses'
        || e.endpoint === 'main.cancel_scheduled_pause') return 'changed_monitoring';
    if (e.endpoint === 'main.add_maintenance'
        || e.endpoint === 'main.update_maintenance'
        || e.endpoint === 'main.delete_maintenance'
        || e.endpoint === 'main.events'
        || e.endpoint === 'main.event_detail') return 'changed_maintenance';
    return 'other';
  }

  // Categories currently ticked in the multi-select filter (all by default).
  const uaCheckedCats = () => new Set(
    [...uaFilters.querySelectorAll('input[data-ua-filter]:checked')].map(el => el.dataset.uaFilter));

  // ── Collapse repeated non-device actions ──────────────────────────────
  // Several identical audit rows in the same minute (e.g. deleting a few pause-log
  // entries in a row) read as clutter. Fold a consecutive run with the same actor,
  // endpoint and summary within one minute into a single counted row. Device-specific
  // summaries embed the device name, so identical-summary runs are inherently NOT
  // tied to a single device — exactly the ones we want to combine.
  const uaMinute = ts => (ts || '').slice(0, 16);   // 'YYYY-MM-DDTHH:MM'

  // Plural phrasing for a combined run of n rows, keyed by endpoint; anything without
  // a template falls back to a "· ×n" badge so the feature works for future actions too.
  function uaCombinedSummary(entry, n) {
    if (n <= 1) return entry.summary;
    const plurals = {
      'main.delete_pause_log': `Deleted ${n} monitoring-pause log entries`,
    };
    return plurals[entry.endpoint] || `${entry.summary} · ×${n}`;
  }

  function collapseUa(entries) {
    const out = [];
    for (const e of entries) {
      const last = out[out.length - 1];
      if (last && last.actor === e.actor && last.endpoint === e.endpoint
          && last.summary === e.summary && uaMinute(last.ts) === uaMinute(e.ts)) {
        last._count++;
      } else {
        out.push({ ...e, _count: 1 });
      }
    }
    return out;
  }

  // Loads the current location's audit trail into the User Activity pane.
  async function loadUserActivity() {
    uaFilters?.querySelectorAll('input[data-ua-filter]').forEach(cb => { cb.checked = true; });
    uaBody.innerHTML = LOG_LOADING;
    try {
      const resp = await fetch('/api/user-activity?' + siteQuery());
      if (resp.status === 401) { window.location = '/login'; return; }
      uaEntries = (await resp.json()).entries || [];
      renderUaList();
    } catch (e) { uaBody.innerHTML = '<div class="log-empty">Failed to load.</div>'; }
  }

  function renderUaList() {
    if (!uaEntries.length) {
      uaBody.innerHTML = '<div class="log-empty">No activity recorded yet.</div>';
      return;
    }
    const cats = uaCheckedCats();
    const rows = collapseUa(uaEntries.filter(e => cats.has(uaCategory(e))));
    if (!rows.length) {
      uaBody.innerHTML = '<div class="log-empty">No matching activity.</div>';
      return;
    }
    uaBody.innerHTML = rows.map(entry => `<div class="ua-item">
        <span class="ua-dot ${uaCategory(entry)}"></span>
        <div class="ua-item-body">
          <div class="ua-item-when">${esc(spWhen(entry.ts))}</div>
          <div class="ua-item-main"><strong>${esc(uaActor(entry.actor))}</strong> — ${esc(uaCombinedSummary(entry, entry._count))}</div>
        </div>
      </div>`).join('');
  }

  uaFilters?.addEventListener('change', e => {
    if (e.target.matches('input[data-ua-filter]')) renderUaList();
  });

  // ── Fetch & refresh ───────────────────────────────────────────────
  let firstLoad = true;
  async function fetchAndRender(forceFresh = false) {
    if (editMode) return;  // don't let a refresh reset in-progress drags
    // On the first fetch after a page load/refresh (or right after an event mutation,
    // forceFresh), ask the server to recompute the fresh-load-only data — the frequent
    // list and the ongoing-event symbols. The recurring 30s poll omits it and serves
    // those from cache in between.
    const resp = await fetch('/api/topology?' + siteQuery() +
      '&layout=' + mapLayout() + ((firstLoad || forceFresh) ? '&fresh=1' : ''));
    // Session expired (login required) — bounce to the login page so the
    // 30s poller doesn't silently spin against a 401.
    if (resp.status === 401) { window.location = '/login'; return; }
    const data = await resp.json();
    // A refresh started before edit mode could resolve mid-edit — re-check so
    // it can't clobber in-progress drags with stale server positions.
    if (editMode) return;
    render(data);
    handlePendingDelete(data.pending_delete);
    updateStaleBanner(data.monitoring_stale, data.stale_minutes);
    if (firstLoad) {
      firstLoad = false;
      setTimeout(fitView, 100);
      // Honor a ?device=<Device ID> deep link once the nodes exist (after fit, so the
      // focus zoom wins). Resolves by Device ID (name), falling back to IP for old
      // links; no-ops if the device isn't live (e.g. it was deleted).
      if (pendingDeviceFocus) {
        const ref = pendingDeviceFocus; pendingDeviceFocus = null;
        const n = nodeByRef(ref);
        if (n) setTimeout(() => window.focusDevice(n.ip), 150);
      }
    }
  }

  // Warn (banner across the top) when the current site's agent has gone quiet —
  // device statuses on the map are frozen and no longer live.
  const staleBanner = document.getElementById('stale-banner');
  const staleBannerText = document.getElementById('stale-banner-text');
  function updateStaleBanner(stale, minutes) {
    if (stale) {
      const mins = minutes != null ? minutes : '?';
      staleBannerText.textContent =
        `Monitoring may be down — no agent reports for this location in over ${mins} minutes. ` +
        `Statuses below may be out of date.`;
      staleBanner.hidden = false;
    } else {
      staleBanner.hidden = true;
    }
  }

  // ── Decommission (pending soft-delete) popup ──────────────────────
  // When the viewed location is scheduled for deletion, show a one-time popup
  // offering to undo. Tracked per-site so the 30s poll doesn't re-open it after
  // the user dismisses; switching sites (see switchSite) resets it.
  const decomOverlay = document.getElementById('decom-overlay');
  let pendingPopupShownFor = null;

  function handlePendingDelete(pending) {
    if (!isAdmin()) return;   // undo is an admin action — don't show viewers a popup they can't act on
    if (pending) {
      if (pendingPopupShownFor !== currentSite) {
        pendingPopupShownFor = currentSite;
        decomOverlay.hidden = false;
      }
    } else {
      decomOverlay.hidden = true;
      if (pendingPopupShownFor === currentSite) pendingPopupShownFor = null;
    }
  }

  document.getElementById('decom-dismiss')?.addEventListener('click', () => {
    decomOverlay.hidden = true;   // keep pending; won't re-open until site is re-selected
  });

  document.getElementById('decom-undo')?.addEventListener('click', async () => {
    const btn = document.getElementById('decom-undo');
    const err = document.getElementById('decom-error');
    btn.disabled = true; err.hidden = true;
    try {
      const resp = await fetch(`/api/sites/${encodeURIComponent(currentSite)}/undo-delete`,
                               { method: 'POST' });
      if (resp.status === 401) { window.location = '/login'; return; }
      if (!resp.ok) { err.textContent = 'Could not undo — please try again.'; err.hidden = false; return; }
      decomOverlay.hidden = true;
      pendingPopupShownFor = null;
      fetchAndRender();            // pending is now cleared server-side
    } catch (e) {
      err.textContent = 'Network error — please try again.'; err.hidden = false;
    } finally {
      btn.disabled = false;
    }
  });

  // The basemap first: projectNodes needs the georeferencing to place devices, and
  // fitView needs the map area to know what "fit" means. A location without imagery
  // resolves to {} and the map renders on the grid exactly as before.
  loadBasemap().then(fetchAndRender);
  setInterval(fetchAndRender, REFRESH_MS);

  // ── Confirm dialog (generic; used by delete) ──────────────────────
  // openConfirm({title, message, confirmLabel, onConfirm}) shows the modal and
  // only runs onConfirm when the user clicks the accept button. onConfirm may
  // throw to surface an error message and keep the dialog open.
  const confirmOverlay  = document.getElementById('confirm-overlay');
  const confirmAcceptBtn = document.getElementById('confirm-accept');
  const confirmExtraBtn = document.getElementById('confirm-extra');
  const confirmErr      = document.getElementById('confirm-error');
  let _confirmAction = null;
  let _confirmExtra = null;

  // Optional third button via { extraLabel, onExtra } — onExtra runs after the
  // dialog closes (used for "Schedule a pause" on the pause confirm).
  function openConfirm({ title, message, confirmLabel, onConfirm, extraLabel, onExtra }) {
    document.getElementById('confirm-title').textContent = title;
    document.getElementById('confirm-message').innerHTML = message;
    confirmAcceptBtn.textContent = confirmLabel || 'Confirm';
    confirmErr.hidden = true;
    _confirmAction = onConfirm;
    _confirmExtra = onExtra || null;
    const hasExtra = !!(extraLabel && onExtra);
    if (hasExtra) {
      confirmExtraBtn.textContent = extraLabel;
      confirmExtraBtn.hidden = false;
    } else {
      confirmExtraBtn.hidden = true;
    }
    // 3-button (pause) layout: accept | extra | cancel. Delete confirm (no extra)
    // keeps the default cancel | accept order.
    confirmExtraBtn.parentElement.classList.toggle('confirm-reordered', hasExtra);
    confirmOverlay.hidden = false;
  }

  function closeConfirm() {
    confirmOverlay.hidden = true;
    _confirmAction = null;
    _confirmExtra = null;
    confirmExtraBtn.hidden = true;
  }

  confirmExtraBtn.addEventListener('click', () => {
    const fn = _confirmExtra;
    closeConfirm();
    if (fn) fn();
  });

  confirmAcceptBtn.addEventListener('click', async () => {
    if (!_confirmAction) return;
    confirmAcceptBtn.disabled = true;
    try {
      await _confirmAction();
    } catch (e) {
      confirmErr.textContent = e.message || 'Action failed — please try again';
      confirmErr.hidden = false;
    } finally {
      confirmAcceptBtn.disabled = false;
    }
  });

  document.getElementById('confirm-cancel').addEventListener('click', closeConfirm);
  confirmOverlay.addEventListener('click', e => { if (e.target === confirmOverlay) closeConfirm(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !confirmOverlay.hidden) closeConfirm(); });

  // ── Activity log (per-device + global, with filters) ──────────────
  const logOverlay = document.getElementById('log-overlay');
  const logBody    = document.getElementById('log-body');
  const logTitle   = document.getElementById('log-title');
  const logFilters = document.getElementById('log-filters');
  const logFilterPopover = document.getElementById('log-filter-popover');
  const logFilterCats = document.getElementById('log-filter-cats');
  let updateLogFilterBadge = null, updateUaFilterBadge = null;   // assigned by wireFilterPopover
  const switchLogFilters = document.getElementById('switch-log-filters');
  let globalEntries = [];
  let globalSince = null;
  let logMode = null;          // 'device' | 'switch' | 'global' — what's shown
  let logEditMode = false;     // admin unlocked log editing (delete monitoring pauses)
  let deviceLog = null;        // {node, entries, since} for an open per-device log
  let switchLog = null;        // {node, entries, since} for an open switch log

  // Tabs (only shown for the site-wide "View logs" view): Device Logs /
  // User Activity / Scheduled Pauses. Per-device and switch logs reuse the
  // Device Logs pane with the tab bar hidden.
  const logTabs = document.getElementById('log-tabs');
  const logExportBtn = document.getElementById('log-export');
  const logPanes = {
    device: document.getElementById('log-pane-device'),
    user:   document.getElementById('log-pane-user'),
    events: document.getElementById('log-pane-events'),
  };
  // Show only the Device Logs pane (used for per-device/switch logs and as the
  // default for the global view). Export is meaningful only for device logs.
  function showDevicePaneOnly() {
    logPanes.user.hidden = true;
    logPanes.events.hidden = true;
    logPanes.device.hidden = false;
    logExportBtn.hidden = false;
    setLogEditVisible(true);
  }
  // The Edit (delete-pauses) button lives only on the Device Logs view, where
  // pause entries appear. Leaving edit mode when it's hidden avoids a stuck state.
  function setLogEditVisible(on) {
    const btn = document.getElementById('log-edit');   // absent for viewers
    if (!btn) return;
    btn.hidden = !on;
    if (!on && logEditMode) resetLogEdit();
  }
  // Switch tabs in the site-wide view. User Activity / Event Logs lazily
  // load their (per-location) data each time their tab is shown.
  function showLogTab(tab) {
    Object.entries(logPanes).forEach(([k, el]) => { el.hidden = k !== tab; });
    logTabs.querySelectorAll('.modal-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.logTab === tab));
    logExportBtn.hidden = tab !== 'device';
    setLogEditVisible(tab === 'device');
    if (tab === 'device')      logTitle.textContent = 'Activity Log';
    else if (tab === 'user')   { logTitle.textContent = 'User Activity'; loadUserActivity(); if (updateUaFilterBadge) updateUaFilterBadge(); }
    else if (tab === 'events') { logTitle.textContent = 'Event Logs';    loadEvents(); }
  }
  logTabs?.addEventListener('click', e => {
    const btn = e.target.closest('.modal-tab');
    if (btn) showLogTab(btn.dataset.logTab);
  });

  // Which event/category filter values are currently ticked (shared checkboxes).
  const checkedEvents = () => new Set([...logFilters.querySelectorAll('input[data-logfilter="event"]:checked')]
    .map(el => el.value));
  const checkedCats = () => new Set([...logFilters.querySelectorAll('input[data-logfilter="cat"]:checked')]
    .map(el => el.value));
  // Event-membership filter: which of {event, none} are ticked. An entry is "event"
  // when it carries any event tag, else "none".
  const checkedMembers = () => new Set([...logFilters.querySelectorAll('input[data-logfilter="member"]:checked')]
    .map(el => el.value));
  // Single "Event-related" toggle: unticking it hides entries tagged as part of an
  // event; entries that aren't event-related always pass this dimension.
  const passesMember = (e, members) => !(e.events && e.events.length) || members.has('event');
  // Frequent-outage membership filter: {frequent, other}. The frequent set comes from
  // the topology's per-device `frequent` flag.
  const checkedFreq = () => new Set([...logFilters.querySelectorAll('input[data-logfilter="freq"]:checked')]
    .map(el => el.value));
  const frequentIps = () => new Set(
    [...(topology.switches || []), ...(topology.devices || [])].filter(n => n.frequent).map(n => n.ip));
  const passesFreq = (e, freqs, fset) => freqs.has(fset.has(e.ip) ? 'frequent' : 'other');
  // Reset the shared filter checkboxes to a mode's defaults. On device/switch
  // pages app-downtime starts off; on the global page everything starts on.
  function resetLogFilters(appDowntimeOn) {
    logFilters.querySelectorAll('input[data-logfilter="event"]').forEach(cb => {
      cb.checked = cb.value === 'app_downtime' ? appDowntimeOn : true;
    });
    logFilters.querySelectorAll('input[data-logfilter="cat"]').forEach(cb => { cb.checked = true; });
    logFilters.querySelectorAll('input[data-logfilter="member"]').forEach(cb => { cb.checked = true; });
    logFilters.querySelectorAll('input[data-logfilter="freq"]').forEach(cb => { cb.checked = true; });
    logFilterPopover.hidden = true;   // reset opens the log with filters collapsed
    if (updateLogFilterBadge) updateLogFilterBadge();
  }
  function renderCurrentLog() {
    if (logMode === 'switch') renderSwitchLog();
    else if (logMode === 'device') renderDeviceLog();
    else if (logMode === 'global') renderGlobalLog();
  }

  // ── Excel (.xlsx) export helpers ──────────────────────────────────
  const EVENT_LABEL = {
    tracked: 'Tracked since', down: 'Offline', unknown: 'Unknown',
    paused: 'Monitoring paused', app_downtime: 'Not pinged (agent downtime)',
    maintenance: 'Maintenance',
  };
  // Date/time in Eastern, split, and WITHOUT the zone name.
  const csvDate = iso => iso
    ? new Date(iso + 'Z').toLocaleDateString('en-US', { timeZone: TZ }) : '';
  const csvTime = iso => iso
    ? new Date(iso + 'Z').toLocaleTimeString('en-US',
        { timeZone: TZ, hour: 'numeric', minute: '2-digit', second: '2-digit' }) : '';
  const endDate = e => e.ongoing ? 'Ongoing' : csvDate(e.end);
  const endTime = e => e.ongoing ? '' : csvTime(e.end);
  // POST the (already-filtered) rows to the server, which returns an .xlsx with
  // columns auto-sized to their widest value, then trigger the download.
  async function downloadXlsx(filename, sheet, headers, rows) {
    try {
      const resp = await fetch('/api/export/xlsx', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename, sheet, headers, rows }),
      });
      if (resp.status === 401) { window.location = '/login'; return; }
      if (!resp.ok) { alert('Export failed — please try again.'); return; }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = filename.endsWith('.xlsx') ? filename : filename + '.xlsx';
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert('Export failed — please try again.');
    }
  }
  const stamp = () => new Date().toISOString().slice(0, 10);
  const safeName = s => (s || 'device').replace(/[^\w.-]+/g, '_');

  const sinceNote = (since, suffix = '') => since
    ? `Showing activity since ${estDate(since)}${suffix}.`
    : `Showing all recorded activity${suffix}.`;

  // All log times render in Eastern (see TZ), matching the rest of the UI.
  const estDate = iso => new Date(iso + 'Z')
    .toLocaleDateString('en-US', { timeZone: TZ, month: 'long', day: 'numeric', year: 'numeric' });
  const estClock = iso => new Date(iso + 'Z')
    .toLocaleTimeString('en-US', { timeZone: TZ, hour: 'numeric', minute: '2-digit' })
    .toLowerCase().replace(/\s/g, '');
  // Stable per-day key (Eastern) for grouping, and its "Friday - July 3, 2026" label.
  // Eastern date (YYYY-MM-DD) + time (HH:MM) for pre-filling the maintenance form's
  // <input type=date/time>, so the wall-clock shown matches the rest of the UI.
  const estDateInput = iso => new Date(iso + 'Z').toLocaleDateString('en-CA', { timeZone: TZ });
  const estTimeInput = iso => new Date(iso + 'Z')
    .toLocaleTimeString('en-GB', { timeZone: TZ, hour: '2-digit', minute: '2-digit', hour12: false });

  const estDayKey = iso => new Date(iso + 'Z').toLocaleDateString('en-CA', { timeZone: TZ });
  const estWeekday = iso => new Date(iso + 'Z').toLocaleDateString('en-US', { timeZone: TZ, weekday: 'long' });
  const estDayLabel = iso => `${estWeekday(iso)} - ${estDate(iso)}`;

  // Render a (newest-first) entry list with a small date subtitle inserted each
  // time the day changes, so different days' logs are visually separated.
  function entriesHtml(rows) {
    let day = null, html = '';
    for (const e of rows) {
      const key = e.start ? estDayKey(e.start) : null;
      if (key && key !== day) {
        day = key;
        html += `<div class="log-day">${estDayLabel(e.start)}</div>`;
      }
      html += entryRow(e);
    }
    return html;
  }

  // Prefix a device name with its type: "AP <name>" / "Switch <name>" / "Other <name>".
  const kindLabel = cat => cat === 'switch' ? 'Switch' : cat === 'other' ? 'Other' : 'AP';

  // "2:04 am" — start time, lowercase am/pm, Eastern.
  const clockAmPm = iso => new Date(iso + 'Z')
    .toLocaleTimeString('en-US', { timeZone: TZ, hour: 'numeric', minute: '2-digit' }).toLowerCase();

  // "2 days 3 hours 15 minutes" between start and end (or now, if ongoing). Zero
  // leading units are dropped; minutes always shown (so a brief event isn't blank).
  function durationText(start, end, ongoing) {
    const from = new Date(start + 'Z');
    const to = (ongoing || !end) ? new Date() : new Date(end + 'Z');
    let mins = Math.max(0, Math.round((to - from) / 60000));
    const d = Math.floor(mins / 1440); mins -= d * 1440;
    const h = Math.floor(mins / 60);   const m = mins - h * 60;
    const parts = [];
    if (d) parts.push(`${d} day${d !== 1 ? 's' : ''}`);
    if (h) parts.push(`${h} hour${h !== 1 ? 's' : ''}`);
    if (m || !parts.length) parts.push(`${m} minute${m !== 1 ? 's' : ''}`);
    return parts.join(' ');
  }

  function entryText(e) {
    const dur = () => durationText(e.start, e.end, e.ongoing);
    const at = () => `starting at ${clockAmPm(e.start)}`;
    if (e.event === 'app_downtime') return `Agent Down · offline for ${dur()} · ${at()}`;
    // Only APs are tagged with their type, as a suffix; switches and other
    // devices show just the name. In demo mode the site-wide log's server-supplied
    // names aren't abstracted, so remap by IP to the stable demo label here.
    const nm = (DEMO && e.ip) ? (nodeByIp(e.ip) ? demoName(nodeByIp(e.ip)) : 'Device') : e.name;
    const who = e.category === 'ap' ? `${esc(nm)} AP` : esc(nm);
    if (e.event === 'tracked')      return `${who} · added at ${clockAmPm(e.start)}`;
    if (e.event === 'deleted')      return `${who} · deleted at ${clockAmPm(e.start)}`;
    if (e.event === 'down')         return `${who} · offline for ${dur()} · ${at()}`;
    if (e.event === 'maintenance')  return `${who} · ${(EV_CATS[e.category] || 'Maintenance').toLowerCase()} for ${dur()} · ${at()}`;
    if (e.event === 'paused')       return `${who} · ${e.kind === 'notifications' ? 'notifications' : 'monitoring'} paused for ${dur()} · ${at()}`;
    if (e.event === 'unknown')      return `${who} · unknown status for ${dur()} · ${at()}`;
    return '';
  }

  // Icon (inside the right-aligned event tag) indicating the event category.
  const EV_ICON = {
    lightning:          '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    maintenance:        '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
    power_outage:       '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>',
    scheduled_downtime: '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/>',
    other:              '<path d="M9.1 9a3 3 0 1 1 5.2 2c-.9.7-1.3 1.2-1.3 2.2"/><line x1="12" y1="17.4" x2="12.01" y2="17.4"/>',
  };
  const evIconSvg = cat =>
    `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
       stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${EV_ICON[cat] || EV_ICON.other}</svg>`;

  // Right-aligned blue circular tag(s) marking a log entry as part of an event; the icon
  // shows the category. Clickable → opens that event. Duplicate events are collapsed.
  function eventTags(e) {
    if (!e.events || !e.events.length) return '';
    const seen = new Set();
    const tags = [];
    for (const ev of e.events) {
      const key = ev.event_group_id || ('m:' + ev.maint_id);
      if (seen.has(key)) continue;
      seen.add(key);
      const cat = ev.category || 'maintenance';
      tags.push(`<button class="log-event-tag" data-event-group="${esc(ev.event_group_id || '')}"
        data-event-maint="${esc(ev.maint_id != null ? ev.maint_id : '')}"
        title="Part of “${esc(evTitle(ev))}” (${esc(evCatLabel(ev))}) — click to view">${evIconSvg(cat)}</button>`);
    }
    return `<span class="log-event-tags">${tags.join('')}</span>`;
  }

  const entryRow = e => {
    // Device entries (those with an IP) are clickable → focus that device.
    const attrs = e.ip ? `class="log-entry clickable" data-ip="${esc(e.ip)}"` : 'class="log-entry"';
    // Dot color reflects the event type (see CSS): offline=yellow, tracked=green,
    // unknown=grey, app downtime=red. Events are an OVERLAY now — a covered outage
    // still shows as "offline", with a right-aligned event tag appended.
    let actions = '';
    const ob = `data-ostart="${esc(e.outage_start || e.start)}" data-oend="${esc(e.outage_end || e.end || '')}"`;
    if (isAdmin() && e.event === 'down' && e.ip) {
      actions = `<button class="log-action maint-mark" data-ip="${esc(e.ip)}"
                   data-start="${esc(e.start)}" data-end="${esc(e.end || '')}" ${ob}
                   title="Mark this outage (or part of it) as an event">Mark as event</button>`;
    }
    // Log-edit mode: a delete control on monitoring-pause entries only (non-destructive
    // to ping_history/uptime). Requires an admin-password unlock (logEditMode).
    if (logEditMode && e.event === 'paused' && e.id != null) {
      actions += `<button class="log-action log-del-pause" data-pause-id="${esc(e.id)}"
                   title="Delete this pause log entry">Delete</button>`;
    }
    return `<div ${attrs}>
       <span class="log-cat-dot ${e.event}"></span>
       <span class="log-text">${entryText(e)}</span>
       ${actions ? `<span class="log-actions">${actions}</span>` : ''}
       ${eventTags(e)}
     </div>`;
  };

  function openLog()  { logOverlay.hidden = false; }
  function closeLog() { logOverlay.hidden = true; evDetailOverlay.hidden = true; resetLogEdit(); }

  // Flatten a single device's /log response into event entries (global-log
  // shape) so it filters/renders the same way as the global and switch logs.
  function buildDeviceEntries(node, data) {
    const kind = node.type === 'switch' ? 'switch' : node.type === 'other' ? 'other' : 'ap';
    const meta = { category: kind, kind, name: displayName(node), device_id: node.name, ip: node.ip };
    const es = [];
    if (data.tracked_since)
      es.push({ ...meta, event: 'tracked', start: data.tracked_since, end: null, ongoing: false });
    if (data.deleted)
      es.push({ ...meta, event: 'deleted', start: data.deleted, end: null, ongoing: false });
    (data.down        || []).forEach(p => es.push({ ...meta, event: 'down', ...p }));
    (data.unknown || []).forEach(p => es.push({ ...meta, event: 'unknown', ...p }));
    (data.paused  || []).forEach(p => es.push({ ...meta, event: 'paused', ...p }));
    (data.app_downtime || []).forEach(p => es.push(
      { category: 'app', kind: 'app', name: null, ip: null, event: 'app_downtime', ...p }));
    es.sort((a, b) => (b.start || '').localeCompare(a.start || ''));
    return es;
  }

  function filteredDeviceEntries() {
    if (!deviceLog) return [];
    const evs = checkedEvents(), members = checkedMembers(), freqs = checkedFreq(), fset = frequentIps();
    return deviceLog.entries.filter(e => evs.has(e.event) && passesMember(e, members) && passesFreq(e, freqs, fset));
  }

  function renderDeviceLog() {
    const rows = filteredDeviceEntries();
    logBody.innerHTML = rows.length
      ? entriesHtml(rows) +
        `<div class="log-note">${sinceNote(deviceLog.since, ', newest first')}</div>`
      : '<div class="log-empty">No log entries match the selected filters.</div>';
  }

  async function openDeviceLog(node) {
    // A switch gets the "this switch" / "connected devices" filtered view.
    if (node.type === 'switch') return openSwitchLog(node);
    logMode = 'device';
    deviceLog = null;
    logTabs.hidden = true;              // per-device log: no tabs
    showDevicePaneOnly();
    logTitle.textContent = `Log — ${displayName(node)}`;
    logFilters.hidden = false;
    logFilterCats.hidden = true;        // single device → no device-type filter
    switchLogFilters.hidden = true;
    resetLogFilters(false);             // start without app downtime
    logBody.innerHTML = LOG_LOADING;
    openLog();
    try {
      const resp = await fetch(`/api/devices/${devRef(node)}/log`);
      if (resp.status === 401) { window.location = '/login'; return; }
      const data = await resp.json();
      deviceLog = { node, entries: buildDeviceEntries(node, data), since: data.since || null };
      renderDeviceLog();
    } catch (e) {
      logBody.innerHTML = '<div class="log-empty">Failed to load log.</div>';
    }
  }

  // ── Switch log: this switch + (optionally) all connected devices ──
  const slfSelf     = document.getElementById('slf-self');
  const slfChildren = document.getElementById('slf-children');

  function filteredSwitchEntries() {
    if (!switchLog) return [];
    const self = slfSelf.checked, children = slfChildren.checked, evs = checkedEvents(),
          members = checkedMembers(), freqs = checkedFreq(), fset = frequentIps();
    return switchLog.entries.filter(e => {
      if (!evs.has(e.event)) return false;      // event-type filter (incl. app downtime)
      if (!passesMember(e, members)) return false;   // event-membership filter
      if (!passesFreq(e, freqs, fset)) return false; // frequent-outage filter
      if (e.category === 'app') return true;    // app downtime isn't tied to self/connected
      return e.self ? self : children;
    });
  }

  function renderSwitchLog() {
    const rows = filteredSwitchEntries();
    const scope = slfChildren.checked
      ? (slfSelf.checked ? 'this switch and connected devices' : 'connected devices')
      : (slfSelf.checked ? 'this switch only' : 'nothing selected');
    logBody.innerHTML = rows.length
      ? entriesHtml(rows) +
        `<div class="log-note">${sinceNote(switchLog.since, `, newest first — showing ${scope}`)}</div>`
      : `<div class="log-empty">${slfSelf.checked || slfChildren.checked
          ? 'No log entries for the selected scope.'
          : 'Select “This switch” or “Connected devices” to show activity.'}</div>`;
  }

  async function openSwitchLog(node) {
    logMode = 'switch';
    switchLog = null;
    logTabs.hidden = true;         // per-switch log: no tabs
    showDevicePaneOnly();
    logTitle.textContent = `Log — ${displayName(node)}`;
    logFilters.hidden = false;
    logFilterCats.hidden = true;   // one switch subtree → no device-type filter
    switchLogFilters.hidden = false;
    resetLogFilters(false);        // start without app downtime
    slfSelf.checked = true;        // start with only the switch itself
    slfChildren.checked = false;
    logBody.innerHTML = LOG_LOADING;
    openLog();
    try {
      const resp = await fetch(`/api/devices/${devRef(node)}/tree-log`);
      if (resp.status === 401) { window.location = '/login'; return; }
      const data = await resp.json();
      switchLog = { node, entries: data.entries || [], since: data.since || null };
      renderSwitchLog();
    } catch (e) {
      logBody.innerHTML = '<div class="log-empty">Failed to load log.</div>';
    }
  }

  [slfSelf, slfChildren].forEach(cb => cb.addEventListener('change', renderCurrentLog));

  // Entries currently visible in the global log, respecting the filter toggles:
  // a Device-type group (switch/ap/other) and an Event group (down/unknown/
  // paused/tracked/app_downtime). All checked by default → everything shows.
  function filteredGlobalEntries() {
    const cats = new Set([...document.querySelectorAll('#log-filters input[data-logfilter="cat"]:checked')]
      .map(el => el.value));
    const evs = new Set([...document.querySelectorAll('#log-filters input[data-logfilter="event"]:checked')]
      .map(el => el.value));
    const members = checkedMembers(), freqs = checkedFreq(), fset = frequentIps();
    return globalEntries.filter(e => {
      if (!evs.has(e.event)) return false;      // event-type filter (incl. app downtime)
      if (!passesMember(e, members)) return false;   // event-membership filter
      if (!passesFreq(e, freqs, fset)) return false; // frequent-outage filter
      if (e.category === 'app') return true;    // app downtime has no device type
      return cats.has(e.category);              // device-type filter
    });
  }

  function renderGlobalLog() {
    const rows = filteredGlobalEntries();
    logBody.innerHTML = rows.length
      ? entriesHtml(rows) +
        `<div class="log-note">${sinceNote(globalSince, ', newest first')}</div>`
      : '<div class="log-empty">No log entries match the selected filters.</div>';
  }

  async function openGlobalLog() {
    logMode = 'global';
    logTabs.hidden = false;         // site-wide view gets the tab bar
    showLogTab('device');           // default to Device Logs
    logFilters.hidden = false;
    logFilterCats.hidden = false;   // device-type filter is meaningful site-wide
    switchLogFilters.hidden = true;
    resetLogFilters(true);          // everything on, including app downtime
    logBody.innerHTML = LOG_LOADING;
    openLog();
    try {
      const resp = await fetch('/api/log?' + siteQuery());
      if (resp.status === 401) { window.location = '/login'; return; }
      const data = await resp.json();
      globalEntries = data.entries || [];
      globalSince = data.since || null;
      renderGlobalLog();
    } catch (e) {
      logBody.innerHTML = '<div class="log-empty">Failed to load log.</div>';
    }
  }

  document.querySelectorAll('#log-filters input[data-logfilter]')
    .forEach(cb => cb.addEventListener('change', renderCurrentLog));

  // ── Compact "Filters" dropdown (device logs + user activity) ──────
  // The existing checkboxes are kept intact (all filter logic reads them by
  // selector); they're just tucked into a popover to save vertical space.
  function wireFilterPopover(btnId, popoverId, countId) {
    const btn = document.getElementById(btnId);
    const pop = document.getElementById(popoverId);
    const badge = document.getElementById(countId);
    if (!btn || !pop) return;
    // Count ticked-off (active) filters, ignoring controls in a hidden group.
    const updateBadge = () => {
      let n = 0;
      pop.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        for (let el = cb; el && el !== pop; el = el.parentElement)
          if (el.hasAttribute('hidden')) return;
        if (!cb.checked) n++;
      });
      btn.classList.toggle('has-active', n > 0);
      if (badge) { badge.textContent = n; badge.hidden = n === 0; }
    };
    btn.addEventListener('click', e => {
      e.stopPropagation();
      updateBadge();
      pop.hidden = !pop.hidden;
      btn.setAttribute('aria-expanded', pop.hidden ? 'false' : 'true');
    });
    pop.addEventListener('click', e => e.stopPropagation());
    pop.addEventListener('change', updateBadge);
    document.addEventListener('click', () => { pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') { pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); } });
    updateBadge();
    return updateBadge;
  }
  const updateLogFilterBadgeFn = wireFilterPopover('log-filter-btn', 'log-filter-popover', 'log-filter-count');
  const updateUaFilterBadgeFn = wireFilterPopover('ua-filter-btn', 'ua-filters', 'ua-filter-count');
  updateLogFilterBadge = updateLogFilterBadgeFn;
  updateUaFilterBadge = updateUaFilterBadgeFn;
  // Clicking a device entry in the (global) log jumps to that device.
  logBody.addEventListener('click', e => {
    // A right-aligned event tag opens that event's detail (Event Logs tab).
    const chip = e.target.closest('.log-event-tag');
    if (chip) {
      openEventByRef(chip.dataset.eventGroup || null,
                     chip.dataset.eventMaint ? +chip.dataset.eventMaint : null);
      return;
    }
    // Maintenance actions take priority over the row's device-focus click.
    const mark = e.target.closest('.maint-mark');
    if (mark) { openMaintModal({ mode: 'create', ip: mark.dataset.ip,
                                 start: mark.dataset.start, end: mark.dataset.end || null,
                                 outageStart: mark.dataset.ostart, outageEnd: mark.dataset.oend || null }); return; }
    const edit = e.target.closest('.maint-edit');
    if (edit) { openMaintModal({ mode: 'edit', id: +edit.dataset.id,
                                 start: edit.dataset.start, end: edit.dataset.end || null,
                                 category: edit.dataset.category || 'maintenance', description: edit.dataset.desc || '',
                                 outageStart: edit.dataset.ostart, outageEnd: edit.dataset.oend || null }); return; }
    const unmark = e.target.closest('.maint-unmark');
    if (unmark) { unmarkMaintenance(+unmark.dataset.id); return; }
    const delPause = e.target.closest('.log-del-pause');
    if (delPause) { deletePauseLog(+delPause.dataset.pauseId); return; }
    const row = e.target.closest('.log-entry[data-ip]');
    if (!row) return;
    closeLog();
    window.focusDevice(row.dataset.ip);
  });

  // ── Log editing (admin): unlock with ADMIN_PASSWORD → delete monitoring-pause
  //    log entries (non-destructive — pause_periods rows only). ────────────────
  const logEditBtn     = document.getElementById('log-edit');
  const logEditLabel   = document.getElementById('log-edit-label');
  const logEditOverlay = document.getElementById('log-edit-overlay');
  const logEditForm    = document.getElementById('log-edit-form');
  const logEditPw      = document.getElementById('log-edit-pw');
  const logEditError   = document.getElementById('log-edit-error');

  function reflectLogEdit() {
    if (!logEditBtn) return;
    logEditBtn.classList.toggle('active', logEditMode);
    if (logEditLabel) logEditLabel.textContent = logEditMode ? 'Done' : 'Edit';
  }
  // Reset edit mode whenever the log modal closes (unlock is per-session server-side,
  // but the UI starts locked each time the modal is reopened).
  function resetLogEdit() { logEditMode = false; reflectLogEdit(); }

  logEditBtn?.addEventListener('click', () => {
    if (logEditMode) { resetLogEdit(); rerenderCurrentLog(); return; }
    logEditError.hidden = true;
    logEditPw.value = '';
    logEditOverlay.hidden = false;
    setTimeout(() => logEditPw.focus(), 30);
  });
  function closeLogEdit() { logEditOverlay.hidden = true; }
  document.getElementById('log-edit-cancel')?.addEventListener('click', closeLogEdit);
  document.getElementById('log-edit-close')?.addEventListener('click', closeLogEdit);
  logEditOverlay?.addEventListener('click', e => { if (e.target === logEditOverlay) closeLogEdit(); });

  logEditForm?.addEventListener('submit', async e => {
    e.preventDefault();
    logEditError.hidden = true;
    try {
      const resp = await fetch('/api/log-edit/unlock', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: logEditPw.value }),
      });
      if (resp.ok) {
        logEditMode = true;
        reflectLogEdit();
        rerenderCurrentLog();
        closeLogEdit();
      } else {
        const data = await resp.json().catch(() => ({}));
        logEditError.textContent = data.error || 'Could not unlock log editing.';
        logEditError.hidden = false;
      }
    } catch (err) {
      logEditError.textContent = 'Network error — please try again.';
      logEditError.hidden = false;
    }
  });

  // Re-render whichever log is on screen without a server round-trip (used when
  // toggling edit mode on/off so the per-pause delete controls appear/disappear).
  function rerenderCurrentLog() {
    if (logMode === 'device' && deviceLog)      renderDeviceLog();
    else if (logMode === 'switch' && switchLog) renderSwitchLog();
    else if (logMode === 'global')              renderGlobalLog();
  }

  async function deletePauseLog(pauseId) {
    if (!Number.isFinite(pauseId)) return;
    if (!confirm('Delete this monitoring-pause log entry? This cannot be undone.')) return;
    try {
      const resp = await fetch(`/api/pause-periods/${pauseId}`, { method: 'DELETE' });
      if (resp.ok) { reloadCurrentLog(); return; }
      if (resp.status === 403) {   // unlock expired — re-prompt
        resetLogEdit(); rerenderCurrentLog();
        alert('Your log-editing session expired. Click Edit and enter the admin password again.');
        return;
      }
      const data = await resp.json().catch(() => ({}));
      alert(data.error || 'Could not delete that log entry.');
    } catch (err) { alert('Network error — please try again.'); }
  }

  // ── Maintenance: mark an outage (or part of it) as maintenance ─────
  const maintOverlay = document.getElementById('maint-overlay');
  const maintForm    = document.getElementById('maint-form');
  const maintError   = document.getElementById('maint-error');
  const maintTitle   = document.getElementById('maint-title');
  let maintState = null;   // { mode:'create'|'edit', ip?, id? }

  // Re-fetch whichever log is currently open (after a maintenance change) and
  // refresh the map so the frequent-outage flags update too.
  // Re-fetch the open log's data in place after a maintenance change — without the
  // "Loading…" flash, without touching the filters, and preserving scroll position
  // (so the user stays where they were instead of being bounced to the top).
  async function reloadCurrentLog() {
    const scroll = logBody.scrollTop;
    try {
      if (logMode === 'device' && deviceLog) {
        const resp = await fetch(`/api/devices/${devRef(deviceLog.node)}/log`);
        if (resp.status === 401) { window.location = '/login'; return; }
        const data = await resp.json();
        deviceLog = { node: deviceLog.node, entries: buildDeviceEntries(deviceLog.node, data),
                      since: data.since || null };
        renderDeviceLog();
      } else if (logMode === 'switch' && switchLog) {
        const resp = await fetch(`/api/devices/${devRef(switchLog.node)}/tree-log`);
        if (resp.status === 401) { window.location = '/login'; return; }
        const data = await resp.json();
        switchLog = { node: switchLog.node, entries: data.entries || [], since: data.since || null };
        renderSwitchLog();
      } else if (logMode === 'global') {
        const resp = await fetch('/api/log?' + siteQuery());
        if (resp.status === 401) { window.location = '/login'; return; }
        const data = await resp.json();
        globalEntries = data.entries || [];
        globalSince = data.since || null;
        renderGlobalLog();
      }
      logBody.scrollTop = scroll;     // stay where we were
    } catch (e) { /* leave the current view as-is on error */ }
    fetchAndRender();                 // refresh the map's frequent-outage flags
  }

  const maintDeviceList = document.getElementById('maint-device-list');
  const maintAddDevicesCb = document.getElementById('maint-add-devices');
  const maintExistingRow = document.getElementById('maint-existing-row');
  const maintExistingSelect = document.getElementById('maint-existing-select');
  const maintExistingToggle = document.getElementById('maint-existing-toggle');
  const maintCreateOnly = () => maintOverlay.querySelectorAll('.maint-create-only');

  function maintSetExisting(on) {
    maintState.existingMode = on;
    maintExistingRow.hidden = !on;
    maintCreateOnly().forEach(el => { el.hidden = on; });
    maintExistingToggle.textContent = on ? 'Create a new event instead' : 'Add to existing event';
    document.getElementById('maint-submit').textContent = on ? 'Add to event' : 'Save';
    maintTitle.textContent = on ? 'Add to an event' : 'Mark as event';
  }

  async function maintPopulateExisting() {
    maintExistingSelect.innerHTML = '<option value="">Loading…</option>';
    try {
      const buckets = await (await fetch('/api/events?' + siteQuery())).json();
      const all = [...(buckets.present || []), ...(buckets.upcoming || []), ...(buckets.past || [])]
        .filter(x => x.type === 'event' && x.group_id);
      maintExistingSelect.innerHTML = all.length
        ? all.map(ev => `<option value="${esc(ev.group_id)}">${esc(evTitle(ev))} · ${esc(spWhen(ev.start))} (${ev.device_ips.length} dev)</option>`).join('')
        : '<option value="">No existing events yet</option>';
    } catch (e) { maintExistingSelect.innerHTML = '<option value="">Failed to load</option>'; }
  }

  maintExistingToggle?.addEventListener('click', () => {
    const on = !maintState.existingMode;
    if (on) maintPopulateExisting();
    maintSetExisting(on);
  });
  maintAddDevicesCb?.addEventListener('change', () => {
    maintDeviceList.hidden = !maintAddDevicesCb.checked;
    if (maintAddDevicesCb.checked && !maintDeviceList._built) {
      maintDeviceList._built = true;
      buildDevicePicker(maintDeviceList, maintState.ip ? [maintState.ip] : []);
    }
  });

  function openMaintModal(opts) {
    maintState = opts;
    const editing = opts.mode === 'edit';
    maintTitle.textContent = editing ? 'Edit event' : 'Mark as event';
    document.getElementById('maint-category').value = opts.category || 'maintenance';
    document.getElementById('maint-description').value = DEMO ? '' : (opts.description || '');
    // Ongoing outage (no end) → default the window end to now.
    const endIso = opts.end || new Date().toISOString().slice(0, 19);
    document.getElementById('maint-start-date').value = estDateInput(opts.start);
    document.getElementById('maint-start-time').value = estTimeInput(opts.start);
    document.getElementById('maint-end-date').value = estDateInput(endIso);
    document.getElementById('maint-end-time').value = estTimeInput(endIso);
    // Reset the create-from-log extras.
    maintState.existingMode = false;
    maintExistingRow.hidden = true;
    maintCreateOnly().forEach(el => { el.hidden = false; });
    maintExistingToggle.textContent = 'Add to existing event';
    document.getElementById('maint-submit').textContent = 'Save';
    maintAddDevicesCb.checked = false;
    maintDeviceList.hidden = true;
    maintDeviceList._built = false;
    maintDeviceList.innerHTML = '';
    // The "add other devices" + "add to existing" options only make sense when marking
    // from an outage — not when editing an existing event slice.
    maintAddDevicesCb.closest('label').hidden = editing;
    maintExistingToggle.hidden = editing;
    maintError.hidden = true;
    maintOverlay.hidden = false;
  }

  // Only rule now: the end must be after the start (the window may be widened past the
  // outage in either direction — the user asked to allow that).
  function maintValidate() {
    if (maintState.existingMode) return null;
    const sd = document.getElementById('maint-start-date').value;
    const st = document.getElementById('maint-start-time').value;
    const ed = document.getElementById('maint-end-date').value;
    const et = document.getElementById('maint-end-time').value;
    if (!sd || !st || !ed || !et) return null;   // incomplete → don't nag yet
    if (`${ed}T${et}` <= `${sd}T${st}`) return 'The end must be after the start.';
    return null;
  }

  maintForm?.addEventListener('input', () => {
    const msg = maintValidate();
    if (msg) { maintError.textContent = msg; maintError.hidden = false; }
    else { maintError.hidden = true; }
  });

  async function unmarkMaintenance(id) {
    try {
      const resp = await fetch(`/api/maintenance/${id}`, { method: 'DELETE' });
      if (resp.status === 401) { window.location = '/login'; return; }
      reloadCurrentLog();
    } catch (e) { /* leave the log as-is on network error */ }
  }

  maintForm?.addEventListener('submit', async e => {
    e.preventDefault();
    const submit = document.getElementById('maint-submit');
    // Extra devices from the picker (if opened), split full (device list) vs log
    // (activity). The clicked device is handled per mode below.
    const sel = (maintAddDevicesCb.checked && maintDeviceList._dpGetSelection)
      ? maintDeviceList._dpGetSelection() : { full: [], log: [] };
    const pickFull = sel.full.filter(ip => ip !== maintState.ip);
    const pickLog = sel.log.filter(x => x.ip !== maintState.ip);

    let url, method, payload;
    if (maintState.existingMode) {
      const group = maintExistingSelect.value;
      if (!group) { maintError.textContent = 'Pick an event to add to.'; maintError.hidden = false; return; }
      url = `/api/events/${encodeURIComponent(group)}/devices?` + siteQuery();
      method = 'POST';
      // Adding to an EXISTING event from a log entry → the clicked device joins capped
      // at its outage end (auto-resolved); a blank end (ongoing outage) stays open.
      const add_log = [...pickLog];
      if (maintState.ip) add_log.push({ ip: maintState.ip, start: maintState.start, end: maintState.end || '' });
      payload = { add: pickFull, add_log };
    } else {
      const warn = maintValidate();
      if (warn) { maintError.textContent = warn; maintError.hidden = false; return; }
      payload = {
        start_date: document.getElementById('maint-start-date').value,
        start_time: document.getElementById('maint-start-time').value,
        end_date:   document.getElementById('maint-end-date').value,
        end_time:   document.getElementById('maint-end-time').value,
        category:   document.getElementById('maint-category').value,
        description: document.getElementById('maint-description').value.trim(),
      };
      if (maintState.mode === 'edit') { url = `/api/maintenance/${maintState.id}`; method = 'PATCH'; }
      else {
        // New event: the clicked device spans the (widenable) event window; picker
        // device-list picks are full, activity picks are capped/resolved.
        payload.device_ips = [...new Set([maintState.ip, ...pickFull].filter(Boolean))];
        if (pickLog.length) payload.log_devices = pickLog;
        url = '/api/events?' + siteQuery(); method = 'POST';
      }
    }
    submit.disabled = true;
    try {
      const resp = await fetch(url, {
        method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      if (resp.status === 401) { window.location = '/login'; return; }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) { maintError.textContent = data.error || 'Could not save.'; maintError.hidden = false; return; }
      maintOverlay.hidden = true;
      reloadCurrentLog();
    } catch (e) {
      maintError.textContent = 'Network error — please try again.'; maintError.hidden = false;
    } finally { submit.disabled = false; }
  });

  document.getElementById('maint-close')?.addEventListener('click', () => { maintOverlay.hidden = true; });
  document.getElementById('maint-cancel')?.addEventListener('click', () => { maintOverlay.hidden = true; });
  maintOverlay?.addEventListener('click', e => { if (e.target === maintOverlay) maintOverlay.hidden = true; });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !maintOverlay.hidden) maintOverlay.hidden = true; });

  // ── Excel export (respects the current view + filters) ────────────
  const XLSX_HEADERS = ['Type', 'Name', 'Device ID', 'IP', 'Event',
                        'Start Date', 'Start Time', 'End Date', 'End Time', 'Duration',
                        'Part of Event', 'Event Title'];

  // Period length as DD:HH:MM:SS (start→end, or start→now if ongoing). Blank for
  // point events like "added" that have no duration.
  function durationClock(e) {
    if (e.event === 'tracked' || !e.start) return '';
    const from = new Date(e.start + 'Z');
    const to = (e.ongoing || !e.end) ? new Date() : new Date(e.end + 'Z');
    let secs = Math.max(0, Math.round((to - from) / 1000));
    const d = Math.floor(secs / 86400); secs -= d * 86400;
    const h = Math.floor(secs / 3600);  secs -= h * 3600;
    const m = Math.floor(secs / 60);    const s = secs - m * 60;
    const p2 = n => String(n).padStart(2, '0');
    return `${p2(d)}:${p2(h)}:${p2(m)}:${p2(s)}`;
  }

  function entriesToRows(entries) {
    const typeLabel = c => c === 'app' ? 'App' : kindLabel(c);
    // Event-membership columns: category label(s) + title(s) when the entry is tagged
    // as part of one or more events, otherwise blank. Distinct values joined by "; ".
    const uniq = arr => [...new Set(arr)].join('; ');
    return entries.map(e => [
      typeLabel(e.category), e.name || '', e.device_id || '', e.ip || '',
      EVENT_LABEL[e.event] || e.event,
      csvDate(e.start), csvTime(e.start), endDate(e), endTime(e), durationClock(e),
      uniq((e.events || []).map(ev => evCatLabel(ev))),
      uniq((e.events || []).map(ev => evTitle(ev))),
    ]);
  }

  function exportCurrentLog() {
    if (logMode === 'device') {
      if (!deviceLog) return;
      downloadXlsx(`activity_${safeName(deviceLog.node.name)}_${stamp()}`,
        deviceLog.node.name, XLSX_HEADERS, entriesToRows(filteredDeviceEntries()));
    } else if (logMode === 'switch') {
      if (!switchLog) return;
      downloadXlsx(`activity_${safeName(switchLog.node.name)}_${stamp()}`,
        switchLog.node.name, XLSX_HEADERS, entriesToRows(filteredSwitchEntries()));
    } else {
      downloadXlsx(`activity-log_${stamp()}`,
        'Activity Log', XLSX_HEADERS, entriesToRows(filteredGlobalEntries()));
    }
  }

  document.getElementById('log-export').addEventListener('click', exportCurrentLog);

  document.getElementById('btn-view-log').addEventListener('click', openGlobalLog);
  document.getElementById('log-close').addEventListener('click', closeLog);
  logOverlay.addEventListener('click', e => { if (e.target === logOverlay) closeLog(); });
  // Don't close the logs modal when Escape is dismissing something layered on top of it.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (!evDetailOverlay.hidden) { closeEvDetail(); return; }
    if (!logOverlay.hidden && evFormOverlay.hidden) closeLog();
  });

  // ── Add / Edit Device Modal ───────────────────────────────────────
  // The same modal handles adding and editing; both pick the device type via the
  // Device Type dropdown. editingIp holds the IP being edited (null when adding).
  const overlay    = document.getElementById('modal-overlay');
  const form       = document.getElementById('modal-form');
  const modalTitle = document.getElementById('modal-title');
  const fTypeRow   = document.getElementById('f-type-row');
  const fType      = document.getElementById('f-type');
  const swRow      = document.getElementById('f-switch-row');
  const swSelect   = document.getElementById('f-switch');
  const uplinkRow  = document.getElementById('f-uplink-row');
  const uplinkSel  = document.getElementById('f-uplink');
  const errBox     = document.getElementById('modal-error');
  const submitBtn  = document.getElementById('modal-submit');
  const modalDelete = document.getElementById('modal-delete');
  let activeTab    = 'ap';
  let editingIp    = null;
  let editingNode  = null;

  const addLabel = { ap: 'Add AP', switch: 'Add Switch', other: 'Add Device' };
  const submitLabel = () =>
    editingIp ? 'Save changes' : (addLabel[activeTab] || 'Add');

  // Edit mode: the "Save changes" button only appears once a field actually
  // differs from the values the modal opened with (captured in editSnapshot).
  let editSnapshot = null;
  function deviceFormSignature() {
    const kind = fType.value;
    const parent = kind === 'switch' ? uplinkSel.value : swSelect.value;
    return JSON.stringify({
      kind,
      name: document.getElementById('f-name').value.trim(),
      ip:   document.getElementById('f-ip').value.trim(),
      host: document.getElementById('f-hostname').value.trim(),
      imap: document.getElementById('f-intermapper').value.trim(),
      loc:  document.getElementById('f-location').value.trim(),
      parent,
    });
  }
  function refreshDeviceSubmit() {
    // Adding → always available; editing → only once something changed.
    submitBtn.hidden = editingIp ? (deviceFormSignature() === editSnapshot) : false;
  }

  function populateSwitchOptions(select) {
    select.innerHTML = '<option value="">— None —</option>';
    topology.switches
      .slice().sort((a, b) => a.name.localeCompare(b.name))
      .forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.name;
        opt.textContent = s.name;
        select.appendChild(opt);
      });
  }

  function openModal() {
    editingIp = null;
    editingNode = null;
    overlay.hidden = false;
    form.reset();
    errBox.hidden = true;
    modalTitle.textContent = 'Add Device';
    modalTitle.hidden = false;
    fTypeRow.style.display = '';         // type chosen via the dropdown
    fType.value = 'ap';
    modalDelete.hidden = true;          // Delete is edit-only
    submitBtn.hidden = false;           // adding: always available
    setTab('ap');
    // Populate both switch dropdowns (parent-of-AP and switch uplink).
    populateSwitchOptions(swSelect);
    populateSwitchOptions(uplinkSel);
    document.getElementById('f-name').focus();
  }

  // Open the modal pre-filled to edit an existing device/switch. The Device Type
  // dropdown allows changing the kind; the matching parent field is shown.
  function openEditModal(node) {
    editingIp = node.ip;
    editingNode = node;
    overlay.hidden = false;
    form.reset();
    errBox.hidden = true;
    populateSwitchOptions(swSelect);
    populateSwitchOptions(uplinkSel);
    modalTitle.textContent = 'Edit Device';
    modalTitle.hidden = false;
    modalDelete.hidden = false;         // Delete is available only in edit mode
    const kind = node.type === 'switch' ? 'switch' : node.type === 'other' ? 'other' : 'ap';
    fTypeRow.style.display = '';
    fType.value = kind;
    setTab(kind);
    document.getElementById('f-name').value = node.name || '';
    document.getElementById('f-ip').value = node.ip || '';
    document.getElementById('f-hostname').value = node.hostname || '';
    document.getElementById('f-intermapper').value = node.intermapper_url || '';
    document.getElementById('f-location').value = node.location || '';
    if (kind === 'switch') uplinkSel.value = node.uplink || '';
    else swSelect.value = node.switch_name || '';
    submitBtn.textContent = submitLabel();
    editSnapshot = deviceFormSignature();   // baseline to compare edits against
    refreshDeviceSubmit();                   // hide "Save changes" until something changes
    document.getElementById('f-name').focus();
  }

  function closeModal() { overlay.hidden = true; editingIp = null; editingNode = null; }

  const namePlaceholder = {
    ap: 'e.g. NORTHCAMP-CABIN5-AP1',
    switch: 'e.g. sw-north-hall-2',
    other: 'e.g. NORTHGATE-CAMERA-1',
  };

  function setTab(tab) {
    activeTab = tab;
    // APs and 'other' devices both have an optional parent switch.
    swRow.style.display = (tab === 'ap' || tab === 'other') ? '' : 'none';
    uplinkRow.style.display = tab === 'switch' ? '' : 'none';
    submitBtn.textContent = submitLabel();
    document.getElementById('f-name').placeholder = namePlaceholder[tab] || '';
  }

  document.getElementById('btn-add-device')?.addEventListener('click', openModal);
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('modal-cancel').addEventListener('click', closeModal);
  modalDelete.addEventListener('click', () => { if (editingNode) deleteNodeFlow(editingNode); });
  overlay.addEventListener('click', e => { if (e.target === overlay) closeModal(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !overlay.hidden) closeModal(); });

  // The Device Type dropdown drives which parent field shows + the submit label.
  fType.addEventListener('change', () => setTab(fType.value));

  // Any field edit re-evaluates whether "Save changes" should show (edit mode).
  form.addEventListener('input', refreshDeviceSubmit);
  form.addEventListener('change', refreshDeviceSubmit);

  form.addEventListener('submit', async e => {
    e.preventDefault();
    errBox.hidden = true;
    submitBtn.disabled = true;
    submitBtn.textContent = editingIp ? 'Saving…' : 'Adding…';

    const name = document.getElementById('f-name').value.trim();
    const ip   = document.getElementById('f-ip').value.trim();
    const host = document.getElementById('f-hostname').value.trim();
    const imap = document.getElementById('f-intermapper').value.trim();
    const loc  = document.getElementById('f-location').value.trim();
    const sw   = swSelect.value;

    // The Intermapper link is required (also enforced natively via `required` and by
    // the server) — guard here too so the message is friendly if validation is bypassed.
    if (!imap) {
      errBox.textContent = 'An Intermapper link is required.';
      errBox.hidden = false;
      submitBtn.disabled = false;
      submitBtn.textContent = editingIp ? 'Save changes' : (addLabel[activeTab] || 'Add');
      document.getElementById('f-intermapper').focus();
      return;
    }

    // Always send hostname (empty string clears it) so an edit can remove a DNS name.
    const body = { name, ip, location: loc, hostname: host, intermapper_url: imap };
    let url, method;
    if (editingIp) {
      // Edit: reference the device by its stable Device ID (the server resolves it to
      // the current IP); the new IP travels in the body, so an IP change still works.
      url = `/api/devices/${devRef(editingNode)}`;
      method = 'PUT';
      body.kind = activeTab;                        // type can change while editing
      if (activeTab === 'switch') body.uplink = uplinkSel.value;
      else body.switch = sw;                       // ap or other
    } else if (activeTab === 'switch') {
      url = '/api/switches/add?' + siteQuery();   // add to the current location
      method = 'POST';
      if (uplinkSel.value) body.uplink = uplinkSel.value;
    } else {
      // ap or other → same endpoint, distinguished by kind
      url = '/api/devices/add?' + siteQuery();     // add to the current location
      method = 'POST';
      body.kind = activeTab;
      if (sw) body.switch = sw;
    }

    try {
      const resp = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (resp.status === 401) { window.location = '/login'; return; }
      const data = await resp.json();
      if (!resp.ok) {
        errBox.textContent = data.error || 'Unknown error';
        errBox.hidden = false;
      } else {
        const wasEditing = !!editingIp;
        closeModal();
        await fetchAndRender();
        if (wasEditing) window.focusDevice(ip);   // re-open panel on the edited device
        else setTimeout(fitView, 100);
      }
    } catch (err) {
      errBox.textContent = 'Network error — please try again';
      errBox.hidden = false;
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = submitLabel();
    }
  });
}());
