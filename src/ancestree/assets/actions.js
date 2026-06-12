let nodes, edges, network;
let searchMatches = null; // Set of matched node ids, or null when no search is active
let typeFilter = null;    // Set of step types to keep, or null when no filter is active
let heatmap = null;       // {key, min, max} when colour-by-metric is active, or null
let statusFilter = null;  // 'failed' | 'dirty' while a header status pill is active, or null
let dayFilter = null;     // 'YYYY-MM-DD' while a timeline day is selected, or null

const DIM_NODE = 'rgba(150, 150, 150, 0.1)';
const DIM_EDGE = 'rgba(200, 200, 200, 0.05)';

function parseQuery(query) {
    return query.trim().toLowerCase().split(/\s+/).filter(Boolean).map(term => {
        const m = term.match(/^([^=:<>]+)(>=|<=|=|:|>|<)(.+)$/);
        return m ? {field: m[1], op: m[2] === ':' ? '=' : m[2], value: m[3]}
                 : {op: 'text', value: term};
    });
}

function entryMatches(key, entry, term) {
    if (entry.searchable === false) return false;
    const value = String(entry.value ?? '').toLowerCase();

    if (term.op === 'text') return key.includes(term.value) || value.includes(term.value);
    if (!key.includes(term.field)) return false;
    if (term.op === '=') return value.includes(term.value);

    const num = parseFloat(value);
    const target = parseFloat(term.value);
    if (isNaN(num) || isNaN(target)) return false;
    switch (term.op) {
        case '>': return num > target;
        case '<': return num < target;
        case '>=': return num >= target;
        case '<=': return num <= target;
    }
}

function nodeMatches(node, terms) {
    const entries = Object.entries(node.entries || {});
    return terms.every(term => {
        if (term.op === 'text' &&
            (String(node.id).toLowerCase().includes(term.value) ||
             String(node.group).toLowerCase().includes(term.value))) {
            return true;
        }
        return entries.some(([key, entry]) => entryMatches(key.toLowerCase(), entry, term));
    });
}

function metaValue(node, key) {
    const entry = (node.entries || {})[key];
    return entry ? entry.value : undefined;
}

// healthy=false is written by the store when a step raised mid-run
function isUnhealthy(node) {
    return String(metaValue(node, 'healthy')).toLowerCase() === 'false';
}

// git_dirty=true means the run was built from uncommitted code
function isDirty(node) {
    return String(metaValue(node, 'git_dirty')).toLowerCase() === 'true';
}

// Local calendar day ('YYYY-MM-DD') the node was created, from the epoch
// the Python side attaches to timestamps; null when there is none.
function nodeDay(node) {
    const entry = (node.entries || {}).timestamp;
    if (!entry || typeof entry.epoch !== 'number') return null;
    const d = new Date(entry.epoch * 1000);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
         + '-' + String(d.getDate()).padStart(2, '0');
}

function nodeFocused(node) {
    if (dayFilter && nodeDay(node) !== dayFilter) return false;
    if (statusFilter === 'failed' && !isUnhealthy(node)) return false;
    if (statusFilter === 'dirty' && !isDirty(node)) return false;
    if (typeFilter && !typeFilter.has(node.group)) return false;
    if (searchMatches && !searchMatches.has(node.id)) return false;
    return true;
}

const DASH_PX = 4;            // on-screen dash/gap size the dirty ring holds
const DASH_MIN_SCALE = 0.55;  // below this zoom a dash pattern reads as noise

// vis-network keeps border thickness constant on screen (lineWidth is
// divided by the view scale) but draws dash lengths in world units, so far
// zoom-out degenerates the pattern into spikes. Counter-scale the dashes to
// hold their on-screen size, and once the node is too small to carry a
// pattern at all, fall back to a solid ring so it always reads as a circle.
function dirtyDashesForScale(scale) {
    if (scale < DASH_MIN_SCALE) return false;
    const len = DASH_PX / Math.min(scale, 1);
    return [len, len];
}

// Theme-, zoom- and selection-dependent values resolved once per repaint,
// not per node
function renderContext() {
    return {
        active: getStyle('--text-primary'),
        dimmed: getStyle('--text-muted'),
        danger: getStyle('--danger'),
        accent: getStyle('--accent'),
        dirtyDashes: dirtyDashesForScale(network.getScale()),
        selected: new Set(network.getSelectedNodes())
    };
}

// Full visual state for one node. On top of focus dimming and heat colour,
// failed runs keep their identity colour but wear a red ring, and runs built
// from a dirty worktree get a dashed border.
function nodeVisual(n, focused, ctx) {
    const update = {
        id: n.id,
        color: focused ? baseNodeColor(n) : DIM_NODE,
        font: {color: focused ? ctx.active : ctx.dimmed},
        borderWidth: 3,
        // vis doubles the border on selection by default; the stroke is
        // centred on the circle's edge, so a fatter border bulges outward
        // and chops dashed rings into spikes. Hold the width steady and
        // signal selection with a halo instead.
        borderWidthSelected: 3,
        shapeProperties: {borderDashes: false},
        shadow: {enabled: false}
    };
    // Status markers only apply to nodes that are actually visible; a node
    // faded out by the heatmap (no metric) stays uniformly dim.
    const visible = focused && update.color !== DIM_NODE;
    if (visible && isUnhealthy(n)) {
        const bg = update.color ? update.color.background : stringToColor(n.group).node_colour;
        update.color = {
            background: bg, border: ctx.danger,
            highlight: {background: bg, border: ctx.danger},
            hover: {background: bg, border: ctx.danger}
        };
        update.borderWidth = 4;
        update.borderWidthSelected = 4;
    }
    if (visible && isDirty(n)) {
        update.shapeProperties = {borderDashes: ctx.dirtyDashes};
    }
    if (visible && ctx.selected.has(n.id)) {
        // Soft selection halo, tinted by status. Canvas shadows ignore the
        // view transform, so it keeps a constant screen size at any zoom.
        const hue = isUnhealthy(n) ? ctx.danger : ctx.accent;
        update.shadow = {enabled: true, color: hue + '66', size: 18, x: 0, y: 0};
    }
    return update;
}

// Single source of truth for node/edge colors: composes the search matches,
// the legend's type filter and the header's status filter, dimming
// everything out of focus.
function applyHighlight() {
    const ctx = renderContext();
    const all = nodes.get();
    const focused = new Set(all.filter(nodeFocused).map(n => n.id));
    const filtering = searchMatches !== null || typeFilter !== null ||
                      statusFilter !== null || dayFilter !== null;

    nodes.update(all.map(n => nodeVisual(n, focused.has(n.id), ctx)));
    edges.update(edges.get().map(edge => ({
        id: edge.id,
        color: (!filtering || (focused.has(edge.from) && focused.has(edge.to))) ? null : DIM_EDGE
    })));
}

// Strict numeric read of a metadata entry: ids, booleans, and image/link
// entries never count as metrics. The id check must be explicit — an
// 8-char hex id made only of digits would otherwise parse as a number.
function numericValue(node, key) {
    if (key === 'node_id' || key === 'parent_id' || key === 'generation') return null;
    const entry = (node.entries || {})[key];
    if (!entry || entry.searchable === false) return null;
    if (entry.type === 'image' || entry.type === 'link') return null;
    // Timestamps carry a pre-parsed epoch from the Python side
    if (typeof entry.epoch === 'number' && Number.isFinite(entry.epoch)) return entry.epoch;
    if (typeof entry.value === 'boolean' || entry.value == null || entry.value === '') return null;
    const v = Number(entry.value);
    return Number.isFinite(v) ? v : null;
}

function numericMetricKeys(allNodes) {
    const keys = new Set();
    allNodes.forEach(n => Object.keys(n.entries || {}).forEach(key => {
        if (numericValue(n, key) !== null) keys.add(key);
    }));
    return [...keys].sort();
}

// Sequential green-spectrum ramp, t in [0, 1]: pale green for low values
// darkening through forest green to a near-brown dark olive for high.
// Luminance carries the ordering — the eye discerns the most shades in
// the green band — and the hue drift adds extra separation.
function heatColor(t) {
    const hue = 135 - 55 * t;         // 135 (green) -> 80 (olive)
    const sat = 35 + 35 * t;          // 35% -> 70%
    const light = 92 - 72 * t;        // 92% -> 20%
    const borderLight = Math.max(light - 16, 10);
    const bg = `hsl(${hue}, ${sat}%, ${light}%)`;
    const border = `hsl(${hue}, ${sat}%, ${borderLight}%)`;
    return {
        background: bg,
        border: border,
        highlight: {background: bg, border: border},
        hover: {background: bg, border: border}
    };
}

function formatMetric(v) {
    return String(Number.isInteger(v) ? v : +v.toPrecision(3));
}

function formatTick(v, isTime) {
    if (!isTime) return formatMetric(v);
    return new Date(v * 1000).toLocaleString(undefined,
        {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
}

// Base color when a node is in focus: heat colour when a metric is selected
// (nodes without the metric dim), otherwise null falls back to its group color.
function baseNodeColor(node) {
    if (!heatmap) return null;
    const v = numericValue(node, heatmap.key);
    if (v === null) return DIM_NODE;
    const span = heatmap.max - heatmap.min;
    const t = span > 0 ? (v - heatmap.min) / span : 0.5;
    return heatColor(t);
}

// Walk the graph from a node in one direction only ('from' = ancestors,
// 'to' = descendants), so sibling branches are never picked up.
function walkLineage(start, direction) {
    const seen = new Set([start]);
    const queue = [start];
    while (queue.length > 0) {
        const curr = queue.shift();
        network.getConnectedNodes(curr, direction).forEach(n => {
            if (!seen.has(n)) {
                seen.add(n);
                queue.push(n);
            }
        });
    }
    return seen;
}

function buildLegend(allNodes) {
    const container = document.getElementById('legend');
    if (!container) return;

    const types = [...new Set(allNodes.map(n => n.group))];
    const metricKeys = numericMetricKeys(allNodes);

    container.innerHTML = `
    <div class="legend-title" id="legend-title">Step types</div>
    <div id="legend-types">
    ${types.map(type => {
        const colors = stringToColor(type);
        return `
        <div class="legend-item" data-type="${type}" title="Click to filter">
            <span class="legend-dot" style="background:${colors.node_colour}; border-color:${colors.node_border_colour}"></span>
            <span class="legend-label">${type}</span>
        </div>`;
    }).join('')}
    </div>
    <div id="heat-scale" class="heat-scale" style="display:none">
        <span class="heat-bar"></span>
        <div class="heat-ticks">
            <span id="heat-tick-max"></span>
            <span id="heat-tick-mid"></span>
            <span id="heat-tick-min"></span>
        </div>
    </div>
    <div id="heat-rank" class="heat-rank" style="display:none">
        <div class="legend-title heat-rank-head" id="heat-rank-head" title="Click to flip sort order"></div>
        <div id="heat-rank-rows"></div>
    </div>
    ${metricKeys.length ? `
    <div class="legend-heat">
        <label class="legend-title" for="heat-select">Colour by</label>
        <select id="heat-select">
            <option value="">Step type</option>
            ${metricKeys.map(key => `<option value="${key}">${key}</option>`).join('')}
        </select>
    </div>` : ''}`;

    container.querySelectorAll('.legend-item').forEach(item => {
        item.addEventListener('click', () => {
            const type = item.dataset.type;
            if (!typeFilter) typeFilter = new Set();
            typeFilter.has(type) ? typeFilter.delete(type) : typeFilter.add(type);

            // No selection (or everything selected) means no filter
            if (typeFilter.size === 0 || typeFilter.size === types.length) typeFilter = null;

            container.querySelectorAll('.legend-item').forEach(el =>
                el.classList.toggle('inactive', typeFilter !== null && !typeFilter.has(el.dataset.type)));
            applyHighlight();
        });
    });

    const heatSelect = document.getElementById('heat-select');
    if (heatSelect) {
        heatSelect.addEventListener('change', () => {
            const key = heatSelect.value;
            const scale = document.getElementById('heat-scale');
            const rank = document.getElementById('heat-rank');
            const typeList = document.getElementById('legend-types');
            const title = document.getElementById('legend-title');

            if (!key) {
                heatmap = null;
                scale.style.display = 'none';
                rank.style.display = 'none';
                typeList.style.display = '';
                title.textContent = 'Step types';
            } else {
                const values = allNodes.map(n => numericValue(n, key)).filter(v => v !== null);
                const isTime = allNodes.some(n => {
                    const entry = (n.entries || {})[key];
                    return entry && typeof entry.epoch === 'number';
                });
                heatmap = {key: key, min: Math.min(...values), max: Math.max(...values),
                           isTime: isTime, desc: true};

                // The colour bar takes over the key's slot: clear any type
                // filter, since its controls are hidden in heatmap mode.
                typeFilter = null;
                container.querySelectorAll('.legend-item').forEach(el => el.classList.remove('inactive'));

                document.getElementById('heat-tick-max').textContent = formatTick(heatmap.max, isTime);
                document.getElementById('heat-tick-mid').textContent = formatTick((heatmap.min + heatmap.max) / 2, isTime);
                document.getElementById('heat-tick-min').textContent = formatTick(heatmap.min, isTime);
                typeList.style.display = 'none';
                scale.style.display = 'flex';
                rank.style.display = 'block';
                title.textContent = key;
                renderRanking(allNodes);
            }
            applyHighlight();
        });
    }

    const rankHead = document.getElementById('heat-rank-head');
    if (rankHead) {
        rankHead.addEventListener('click', () => {
            if (!heatmap) return;
            heatmap.desc = !heatmap.desc;
            renderRanking(allNodes);
        });
    }
}

// Header pills summarising pipeline health: failed runs and runs built from
// a dirty worktree. Each pill toggles a status filter on the graph; with
// nothing to report, a quiet "all healthy" tick takes their place.
function buildStatusPills(allNodes) {
    const container = document.getElementById('status-pills');
    if (!container) return;

    const failed = allNodes.filter(isUnhealthy).length;
    const dirty = allNodes.filter(isDirty).length;

    if (failed === 0 && dirty === 0) {
        container.innerHTML = `
        <div class="status-pill status-pill--ok" title="Every run completed cleanly from a clean worktree">
            <svg width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                <path d="M2 6.5L4.8 9.2L10 3.5" stroke="currentColor" stroke-width="1.8"
                      stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            All healthy
        </div>`;
        return;
    }

    container.innerHTML = `
    ${failed ? `
    <button class="status-pill status-pill--failed" data-filter="failed" title="Show only failed runs">
        <span class="status-pill-dot"></span>${failed} failed
    </button>` : ''}
    ${dirty ? `
    <button class="status-pill status-pill--dirty" data-filter="dirty" title="Show only runs built with uncommitted changes">
        <span class="status-pill-dot"></span>${dirty} uncommitted
    </button>` : ''}`;

    container.querySelectorAll('[data-filter]').forEach(pill => {
        pill.addEventListener('click', () => {
            statusFilter = statusFilter === pill.dataset.filter ? null : pill.dataset.filter;
            container.querySelectorAll('[data-filter]').forEach(el =>
                el.classList.toggle('active', el.dataset.filter === statusFilter));
            applyHighlight();
        });
    });
}

// Floating activity strip: one bar per active day, sized by run count and
// split into healthy/failed. Clicking a day filters the graph to it.
function buildTimeline(allNodes) {
    const container = document.getElementById('timeline');
    if (!container) return;

    const days = new Map(); // day -> {ok, failed}
    allNodes.forEach(n => {
        const day = nodeDay(n);
        if (!day) return;
        const bucket = days.get(day) || {ok: 0, failed: 0};
        bucket[isUnhealthy(n) ? 'failed' : 'ok'] += 1;
        days.set(day, bucket);
    });
    if (days.size === 0) return;

    const sorted = [...days.entries()].sort((a, b) => a[0] < b[0] ? -1 : 1);
    const max = Math.max(...sorted.map(([, b]) => b.ok + b.failed));
    const BAR_PX = 34; // tallest bar; counts scale within it

    const dayLabel = day => {
        const [y, m, d] = day.split('-').map(Number);
        return new Date(y, m - 1, d).toLocaleDateString(undefined, {day: 'numeric', month: 'short'});
    };
    const seg = (cls, count) => count ? `
        <span class="timeline-seg timeline-seg--${cls}"
              style="height:${Math.max(BAR_PX * count / max, 3)}px"></span>` : '';

    container.innerHTML = `
    <div class="legend-title">Activity</div>
    <div class="timeline-bars">
    ${sorted.map(([day, b]) => `
        <div class="timeline-bar" data-day="${day}"
             title="${dayLabel(day)} — ${b.ok + b.failed} run${b.ok + b.failed === 1 ? '' : 's'}${b.failed ? `, ${b.failed} failed` : ''}">
            ${seg('failed', b.failed)}${seg('ok', b.ok)}
        </div>`).join('')}
    </div>
    <div class="timeline-range">
        <span>${dayLabel(sorted[0][0])}</span>
        <span>${sorted.length > 1 ? dayLabel(sorted[sorted.length - 1][0]) : ''}</span>
    </div>`;

    container.querySelectorAll('.timeline-bar').forEach(bar => {
        bar.addEventListener('click', () => {
            dayFilter = dayFilter === bar.dataset.day ? null : bar.dataset.day;
            container.querySelectorAll('.timeline-bar').forEach(el =>
                el.classList.toggle('active', el.dataset.day === dayFilter));
            applyHighlight();
        });
    });
}

// Ranked list of nodes by the active heatmap metric — answers "which run is
// best, and what were its params". Clicking a row selects that node in the
// graph and opens its details.
function renderRanking(allNodes) {
    if (!heatmap) return;
    const ranked = allNodes
        .map(n => ({node: n, value: numericValue(n, heatmap.key)}))
        .filter(r => r.value !== null)
        .sort((a, b) => heatmap.desc ? b.value - a.value : a.value - b.value)
        .slice(0, 5);

    document.getElementById('heat-rank-head').innerHTML =
        heatmap.desc ? 'Top 5 &#9660;' : 'Bottom 5 &#9650;';

    const rowsEl = document.getElementById('heat-rank-rows');
    rowsEl.innerHTML = ranked.map((r, i) => `
    <div class="heat-rank-row" data-id="${r.node.id}" title="${r.node.id}">
        <span class="heat-rank-pos">${i + 1}</span>
        <span class="heat-rank-label">${r.node.group}<span class="heat-rank-id">${r.node.id}</span></span>
        <span class="heat-rank-value">${formatTick(r.value, heatmap.isTime)}</span>
    </div>`).join('');

    rowsEl.querySelectorAll('.heat-rank-row').forEach(row =>
        row.addEventListener('click', () => selectNode(row.dataset.id)));
}

// Jump the graph to a node: select it, pan to it, and show its details.
// Programmatic selection fires no select event, so render the pane directly.
function selectNode(id) {
    network.selectNodes([id]);
    network.focus(id, {
        scale: Math.max(network.getScale(), 0.8),
        animation: {duration: 400, easingFunction: 'easeInOutQuad'}
    });
    showSelection([id]);
}

function runSearch(query) {
    const counter = document.getElementById('search-count');
    const clearBtn = document.getElementById('search-clear');
    const terms = parseQuery(query);

    if (terms.length === 0) {
        searchMatches = null;
        counter.textContent = '';
        clearBtn.style.display = 'none';
    } else {
        const matched = nodes.get().filter(n => nodeMatches(n, terms)).map(n => n.id);
        searchMatches = new Set(matched);
        counter.textContent = `${matched.length}/${nodes.length}`;
        clearBtn.style.display = 'inline';
    }
    applyHighlight();
}

function toggleTheme() {
    const body = document.documentElement;
    const isDark = body.getAttribute('data-theme') === 'dark';
    body.setAttribute('data-theme', isDark ? 'light': 'dark');
    
    const nodeColor = getStyle('--text-primary');
    const edgeColor = getStyle('--text-primary');

    nodes.update(nodes.getIds().map(id => ({ 
        id:id, 
        font: {color:nodeColor} 
    })));

    edges.update(nodes.getIds().map(id => ({
        id:id,
        font: {color:edgeColor, inherit: false}
    })));

    // The accent has light/dark variants: re-resolve it for edge highlights
    const accent = getStyle('--accent');
    network.setOptions({edges: {color: {highlight: accent, hover: accent}}});

    // Re-dim non-matches with the new theme's colors
    applyHighlight();
}

function getStyle(prop) {
    return getComputedStyle(document.documentElement).getPropertyValue(prop).trim();
}

function initResizer() {
    // Have a bug here where readjustments cause text to get highlighted and it tries ot drag
    const resizer = document.getElementById('resizer');
    const sidebar = document.getElementById('details-pane');
    const leftPanel = document.getElementById('mynework');


    resizer.addEventListener('mousedown', function(e) {
        e.preventDefault();
        document.body.classList.add('is-dragging');
        const startX = e.clientX;
        const startWidth = parseInt(document.defaultView.getComputedStyle(sidebar).width, 10);

        function onMouseMove(e) {
            const newWidth = startWidth + (startX - e.clientX);

            if (newWidth > 100 && newWidth < (document.body.offsetWidth*0.9)) {
                sidebar.style.width = newWidth + 'px';
            }        
        }
        function onMouseUp() {
            document.body.classList.remove('is-dragging');
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);

            if (typeof network !== 'undefined'){
                network.redraw();
            }
        }
        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', onMouseUp)
    });
}

window.onload = function() {
    initResizer();

    if (!window.PIPELINE_DATA) {
        console.error("No pipeline data from Python");
        return
    }

    nodes=new vis.DataSet(window.PIPELINE_DATA.nodes);
    edges = new vis.DataSet(window.PIPELINE_DATA.edges)

    function getOptions(nodes) {
        const groups = {};
        const uniqueTypes = [...new Set(nodes.map(n=>n.group))];

        uniqueTypes.forEach(type => {
            const colors = stringToColor(type);
            groups[type] = {
                color: {background: colors.node_colour, border: colors.node_border_colour},
                shape: 'dot',
                size: 25,
                font: {color: '#000000', size:18, align: 'center'},
                borderWidth: 3
            };
        });

        return {
            groups:groups, 
            layout: {
                hierarchical: {
                    enabled:true,
                    direction:'LR',
                    sortMethod:'directed',
                    levelSeparation:300, // Must match sep on line 135
                    nodeSpacing: 100,
                    treeSpacing:200,
                    blockShifting:true,
                    edgeMinimization: true,
                    parentCentralization:true
                }},
            

            physics: {
                enabled:false,
                hierarchicalRepulsion: {
                    nodeDistance:100,
                    avoidOverlap:1
                },
                // solver:'forceAtlas2Based',
                // forceAtlas2Based: {
                //     gravitationalConstant: -10,
                //     centralGravity: 0.001,
                //     springLength: 100,
                //     springConstant: 0.02
                // },
                // stabilization: {
                //     iterations: 1000,
                //     updateInterval: 100,
                //     onlyDynamicEdges:false,
                //     fit:true
                // },
                // adaptiveTimestep:true
            },
            interaction: {multiselect:true, hover:true},
            edges: {
                arrows: {to: {enabled:true}},
                smooth: {type:'cubicBezier', forceDirection:'horizontal'},
                color: {color:'#848484', highlight:getStyle('--accent'), hover:getStyle('--accent'), inherit:false},
                // opacity: 0.6
                },
            // nodes: {
            //     // shape:'dot',
            //     // size: 25,
            //     font: {
            //         color: '#000000',
            //         size:18,
            //         align: 'center'
            //     },
            //     borderWidth: 2
            //     },
            };
        }
    
    const options = getOptions(nodes)
    options.interaction.multiselect = true;
    network = new vis.Network(document.getElementById('mynetwork'), {nodes, edges}, options);

    buildLegend(nodes.get());
    buildStatusPills(nodes.get());
    buildTimeline(nodes.get());
    applyHighlight(); // first paint: draws the failed/dirty markers

    const searchInput = document.getElementById('search-input');
    if (searchInput) {
        searchInput.addEventListener('input', e => runSearch(e.target.value));
        searchInput.addEventListener('keydown', e => {
            if (e.key === 'Escape') {
                searchInput.value = '';
                runSearch('');
                searchInput.blur();
            }
        });
        document.getElementById('search-clear').addEventListener('click', () => {
            searchInput.value = '';
            runSearch('');
            searchInput.focus();
        });
    }

    network.on("hoverNode", function (params){
        // Full lineage: everything upstream and everything downstream
        const lineageNodes = walkLineage(params.node, 'from');
        walkLineage(params.node, 'to').forEach(n => lineageNodes.add(n));

        const lineageEdges = new Set(edges.get()
            .filter(edge => lineageNodes.has(edge.from) && lineageNodes.has(edge.to))
            .map(edge => edge.id));

        const ctx = renderContext();
        nodes.update(nodes.get().map(n => nodeVisual(n, lineageNodes.has(n.id), ctx)));

        edges.update(edges.getIds().map(id => ({
            id: id,
            color: lineageEdges.has(id) ? null : DIM_EDGE
        })));
    });
    
    // Blurring: restore the active search highlight, or reset if none
    network.on("blurNode", function () {
        applyHighlight();
    });

    // Keep the dirty rings' dash pattern at a constant on-screen size at any
    // zoom level. The 'zoom' event only fires for user zooming, while the
    // initial fit and network.focus() change the scale silently — so verify
    // after every redraw instead: a cheap scale check per frame, repainting
    // only when the pattern actually changes. The repaint must go through
    // applyHighlight: vis re-applies a node's group colour on any update
    // that omits 'color', so a minimal shapeProperties patch would wipe
    // heatmap and dim colours.
    let lastDashKey = null;
    network.on('afterDrawing', () => {
        const dashes = dirtyDashesForScale(network.getScale());
        const dashKey = dashes === false ? 'solid' : dashes[0].toFixed(1);
        if (dashKey === lastDashKey) return;
        lastDashKey = dashKey;
        applyHighlight();
    });

    network.on('select', params => showSelection(params.nodes));
};

// Render the details pane for a selection: shared by the graph's select
// event and programmatic selection from the ranking list.
function showSelection(selectedIds) {
    applyHighlight(); // repaint the selection halos

    const contentArea = document.getElementById('node-content');

    if (selectedIds.length === 1) {
        contentArea.innerHTML = generateNodeHtml(nodes.get(selectedIds[0]));
    } else if (selectedIds.length === 2) {
        triggerComparison(selectedIds);
    } else {
        contentArea.innerHTML = `
        <div class="empty-state">
        <p>Select a node to view details. Cmd click a second node to trigger comparison.</p>
        </div>
        `;
    }
}

function stringToColor(str) {
    let hash = 2166136261;
    for (let i = 0; i<str.length; i++) {
        hash ^= str.charCodeAt(i);
        // hash += (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
        hash = Math.imul(hash, 16777619)
    }
    const phi = 0.618033988749895;
    let hue = Math.abs(hash*phi) % 1;
    hue = Math.floor(hue*360)

    return {
        node_colour: `hsl(${hue}, 70%, 60%)`,
        node_border_colour: `hsl(${hue}, 80%, 35%)`
    };
}

function renderMetadata(entries) {
    if (!entries) return '';
    const groups = {};

    for (const [key, entry] of Object.entries(entries))
    (groups[entry.group] ??= []).push([key, entry]);

    // Provenance is reference material, not results: render it last.
    const ordered = Object.entries(groups).sort((a, b) =>
        (a[0] === 'Provenance') - (b[0] === 'Provenance'));

    return ordered.map(([group, items]) => `
    <div class="section-card">
    <div class="section-header">${group}</div>
    <div class="section-body">
    ${items.map(([key,entry]) => `
    <div class="metadata-row">
    <div class="metadata-key">${entry.label || key}</div>
    <div class="metadata-value">${renderValue(entry)}</div>
    </div>`).join('')}
    </div>
    </div>
    `).join('');
}

function renderValue(entry) {
    switch (entry.type) {
        case "link":
            return `<a href="${entry.value.path ?? entry.value}" target="_blank" class="report-link">${entry.label || 'View'}</a>`;
        case "image":
            return `<img src="${entry.value.path ?? entry.value}" style="max-width:100%">`;
        default:
            return `<span>${entry.value ?? 'N/A'}</span>`;
    }
}

// Status strip at the top of the details pane: run health, plus a
// reproducibility warning when the run came from a dirty worktree. Warning
// states carry a one-line explanation; a clean run is just a quiet badge.
function renderStatusBanner(node) {
    const items = [];
    if ('healthy' in (node.entries || {})) {
        items.push(isUnhealthy(node)
            ? {cls: 'failed', title: 'Run failed',
               note: 'The step raised before completing — artifacts may be partial.'}
            : {cls: 'ok', title: 'Completed', note: null});
    }
    if (isDirty(node)) {
        items.push({cls: 'dirty', title: 'Uncommitted changes',
                    note: 'Built from a dirty worktree — this result may not be reproducible.'});
    }
    if (items.length === 0) return '';

    return `<div class="status-banner">
    ${items.map(item => item.note ? `
    <div class="status-card status-card--${item.cls}">
        <div class="status-card-title"><span class="status-dot"></span>${item.title}</div>
        <div class="status-card-note">${item.note}</div>
    </div>` : `
    <span class="status-badge status-badge--${item.cls}"><span class="status-dot"></span>${item.title}</span>`).join('')}
    </div>`;
}

function generateNodeHtml(nodeData) {
    return `<div class="node-info">
    ${renderStatusBanner(nodeData)}
    ${renderMetadata(nodeData.entries)}
    </div>`;
}

// Plain numeric read of an entry for delta display; unlike numericValue it
// takes the entry directly and never treats timestamps as numbers.
function entryNumber(entry) {
    if (!entry || entry.type === 'image' || entry.type === 'link') return null;
    if (typeof entry.value === 'boolean' || entry.value == null || entry.value === '') return null;
    const v = Number(entry.value);
    return Number.isFinite(v) ? v : null;
}

function compareRow(key, ea, eb) {
    const same = JSON.stringify(ea ? ea.value : null) === JSON.stringify(eb ? eb.value : null);
    const na = entryNumber(ea), nb = entryNumber(eb);
    const epoch = (ea && typeof ea.epoch === 'number') || (eb && typeof eb.epoch === 'number');
    const delta = !same && !epoch && na !== null && nb !== null
        ? `<span class="compare-delta">${nb >= na ? '+' : '−'}${formatMetric(Math.abs(nb - na))}</span>`
        : '';
    const cell = e => e === undefined ? '<span class="compare-missing">—</span>' : renderValue(e);
    return `
    <div class="compare-row ${same ? 'compare-same' : 'compare-diff'}">
        <div class="metadata-key">${key}</div>
        <div class="compare-vals">
            <div class="compare-val">${cell(ea)}</div>
            <div class="compare-val">${cell(eb)}${delta}</div>
        </div>
    </div>`;
}

// Aligned diff of two nodes: rows matched by key across both, identical
// values greyed out, differences highlighted with a numeric delta (B − A)
// where one is meaningful. Reached by cmd-clicking a second node.
function triggerComparison(selectedIds) {
    const a = nodes.get(selectedIds[0]);
    const b = nodes.get(selectedIds[1]);

    // Union of keys grouped under their section headings, in first-seen order
    const groups = new Map();
    [a, b].forEach(n => Object.entries(n.entries || {}).forEach(([key, entry]) => {
        if (key === 'node_id') return; // already in the column headers
        const keys = groups.get(entry.group) || [];
        if (!keys.includes(key)) keys.push(key);
        groups.set(entry.group, keys);
    }));
    const ordered = [...groups.entries()].sort((x, y) =>
        (x[0] === 'Provenance') - (y[0] === 'Provenance'));

    const headCol = n => `
        <div class="compare-head-col" title="${n.id}">
            <span class="legend-dot" style="background:${stringToColor(n.group).node_colour}; border-color:${stringToColor(n.group).node_border_colour}"></span>
            <span class="compare-head-type">${n.group}</span>
            <span class="compare-head-id">${n.id}</span>
        </div>`;

    document.getElementById('node-content').innerHTML = `
    <div class="node-info">
    <div class="compare-head">${headCol(a)}${headCol(b)}</div>
    ${ordered.map(([group, keys]) => `
    <div class="section-card">
    <div class="section-header">${group}</div>
    <div class="section-body">
    ${keys.map(key => compareRow(key, (a.entries || {})[key], (b.entries || {})[key])).join('')}
    </div>
    </div>`).join('')}
    </div>`;
}
