let nodes, edges, network;
let searchMatches = null; // Set of matched node ids, or null when no search is active
let typeFilter = null;    // Set of step types to keep, or null when no filter is active
let heatmap = null;       // {key, min, max} when colour-by-metric is active, or null

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

function nodeFocused(node) {
    if (typeFilter && !typeFilter.has(node.group)) return false;
    if (searchMatches && !searchMatches.has(node.id)) return false;
    return true;
}

// Single source of truth for node/edge colors: composes the search matches
// and the legend's type filter, dimming everything out of focus.
function applyHighlight() {
    const activeColor = getStyle('--text-primary');
    const dimmedColor = getStyle('--text-muted');
    const all = nodes.get();
    const focused = new Set(all.filter(nodeFocused).map(n => n.id));
    const filtering = searchMatches !== null || typeFilter !== null;

    nodes.update(all.map(n => ({
        id: n.id,
        color: focused.has(n.id) ? baseNodeColor(n) : DIM_NODE,
        font: {color: focused.has(n.id) ? activeColor : dimmedColor}
    })));
    edges.update(edges.get().map(edge => ({
        id: edge.id,
        color: (!filtering || (focused.has(edge.from) && focused.has(edge.to))) ? null : DIM_EDGE
    })));
}

// Strict numeric read of a metadata entry: hex ids, booleans, and
// image/link entries never count as metrics.
function numericValue(node, key) {
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

// Ranked list of nodes by the active heatmap metric — answers "which run is
// best, and what were its params". Clicking a row selects that node in the
// graph and opens its details.
function renderRanking(allNodes) {
    if (!heatmap) return;
    const ranked = allNodes
        .map(n => ({node: n, value: numericValue(n, heatmap.key)}))
        .filter(r => r.value !== null)
        .sort((a, b) => heatmap.desc ? b.value - a.value : a.value - b.value)
        .slice(0, 10);

    document.getElementById('heat-rank-head').innerHTML =
        heatmap.desc ? 'Top 10 &#9660;' : 'Bottom 10 &#9650;';

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

        const activeColor = getStyle('--text-primary');
        const dimmedColor = getStyle('--text-muted');

        nodes.update(nodes.get().map(n => ({
            id: n.id,
            color: lineageNodes.has(n.id) ? baseNodeColor(n) : DIM_NODE,
            font: { color: lineageNodes.has(n.id) ? activeColor : dimmedColor }
        })));

        edges.update(edges.getIds().map(id => ({
            id: id,
            color: lineageEdges.has(id) ? null : DIM_EDGE
        })));
    });
    
    // Blurring: restore the active search highlight, or reset if none
    network.on("blurNode", function () {
        applyHighlight();
    });

    network.on('select', params => showSelection(params.nodes));
};

// Render the details pane for a selection: shared by the graph's select
// event and programmatic selection from the ranking list.
function showSelection(selectedIds) {
    const btn = document.getElementById('compare-btn');
    const contentArea = document.getElementById('node-content');

    if (btn) btn.disabled = selectedIds.length !== 2;

    if (selectedIds.length === 1) {
        const nodeData = nodes.get(selectedIds[0]);
        contentArea.innerHTML = generateNodeHtml(nodeData);
    }
    else if (selectedIds.length ===2) {
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

function generateNodeHtml(nodeData) {
    return `<div class="node-info">
    ${renderMetadata(nodeData.entries)}
    </div>`;
}

function triggerComparison(selectedIds) {
    const sidebar = document.getElementById('details-pane');
    const contentArea = document.getElementById('node-content');

    const n1 = nodes.get(selectedIds[0]);
    const n2 = nodes.get(selectedIds[1]);

    contentArea.innerHTML = `
    <div class="comapre-split" style="display:flex; gap: 15px;">
    <div class="compare-col" style="flex: 1; min-width: 0;">
    <div style="text-align:center; font-weight:bold; color:var(--accent);"></div>
    ${generateNodeHtml(n1)}
    </div>
    <div class="compare-col" style="flex:1; min-width:0; border-left: 1px solid var(--border); padding-left:15px;">
    <div style="text-align:center; font-weight:bold; color:var(--accent);"></div>
    ${generateNodeHtml(n2)}
    </div>
    </div>
    `;
}
