let nodes, edges, network;

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
    document.getElementById('accent-picker').addEventListener('input', function(e){
        const newAccent = e.target.value;

        document.documentElement.style.setProperty('--accent', newAccent);

        if (network) {
            network.setOptions({
                edges: {
                    color: {
                        highlight:newAccent,
                        hover:newAccent
                    }
                }
            });
        }
    });

    if (!window.PIPELINE_DATA) {
        console.error("No pipeline data from Python");
        return
    }

    nodes=new vis.DataSet(window.PIPELINE_DATA.nodes);
    edges = new vis.DataSet(window.PIPELINE_DATA.edges)

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
                color: {color:'#848484', highlight:'#3498db', hover:'#3498db', inherit:false},
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

    network.on("hoverNode", function (params){
        let hoveredNodeId = params.node;

        let lineageNodes = [hoveredNodeId];
        let queue = [hoveredNodeId];

        while (queue.length > 0) {
            let curr = queue.shift();
            let ancestors = network.getConnectedNodes(curr, 'from');
            ancestors.forEach(a => {
                if (!lineageNodes.includes(a)) {
                    lineageNodes.push(a);
                    queue.push(a);
                }
            });
        }

        // Ensure upstream edges are not highlighted
        let allEdges = edges.get();
        let lineageEdges = allEdges.filter(edge => lineageNodes.includes(edge.to) && lineageNodes.includes(edge.from)).map(edge => edge.id);
        
        const activeColor = getStyle('--text-primary');
        const dimmedColor = getStyle('--text-muted');

        nodes.update(nodes.getIds().map(id => ({
            id: id,
            color: lineageNodes.includes(id) ? null : 'rgba(150, 150, 150, 0.1)',
            font: { color: lineageNodes.includes(id) ? activeColor : dimmedColor }
        })));

        edges.update(edges.getIds().map(id => ({
            id:id,
            color:lineageEdges.includes(id) ? null : 'rgba(200, 200, 200, 0.05)'
        })));
    });
    
    // Blurring
    network.on("blurNode", function () {
        const activeColor = getStyle('--text-primary')
        nodes.update(nodes.getIds().map(id => ({
            id:id,
            color: null,
            font: {color: activeColor}
        })));
        edges.update(edges.getIds().map(id=> ({
            id:id,
            color:null
        })));
    });    

    network.on('select', function(params){
        const selectedIds = params.nodes;
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
    });
};

function renderMetadata(entries) {
    if (!entries) return '';
    const groups = {};

    for (const [key, entry] of Object.entries(entries))
    (groups[entry.group] ??= []).push([key, entry]);

    return Object.entries(groups).map(([group, items]) => `
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
    <h4>Node: ${nodeData.id}</h4>
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
