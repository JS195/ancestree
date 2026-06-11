from .utils import parse_time
import json
from datetime import datetime
from importlib import resources
from pathlib import Path
from collections import defaultdict

def _val(entries, key, default = None):
    e = entries.get(key)
    return e.get('value') if e else default

def assign_levels(node_ids, edges):
    children = defaultdict(list)

    indeg = {n: 0 for n in node_ids}
    for parent, child in edges:
        children[parent].append(child)
        indeg[child] = indeg.get(child, 0) + 1
        indeg.setdefault(parent, 0)
    
    level = {n:0 for n in indeg}
    queue = [n for n, d in indeg.items() if d==0]

    while queue:
        n = queue.pop()
        for c in children[n]:
            level[c] = max(level[c], level[n] + 1)
            indeg[c] -= 1

            if indeg[c] == 0:
                queue.append(c)
    return level

# Get the nodes and edges to use in the webapp
def visualise_nodes(store):
    raw = []
    for node_dir in store.root.iterdir():
        if not node_dir.is_dir():
            continue
        node_obj = store.get_node(str(node_dir.name))
        if node_obj is None:
            continue
        entries = dict(node_obj.metadata)

        # Timestamps are stored as ISO strings: attach the parsed epoch so the
        # web UI can treat them numerically (colour-by-time), and display the
        # human-readable form instead of the raw ISO value.
        iso = entries.get('timestamp', {}).get('value')
        if iso:
            try:
                entries['timestamp'] = {
                    **entries['timestamp'],
                    'value': parse_time(iso),
                    'epoch': datetime.fromisoformat(iso).timestamp(),
                }
            except (ValueError, TypeError):
                pass

        for item in node_obj.artifacts():
            path = Path(*item.parts[1:])
            entries[str(path)] = {
                'value': str(item),
                'type': 'link',
                'group': 'Artifacts',
            }
        raw.append(entries)

    node_ids = [_val(e, 'node_id') for e in raw]
    edges = [(_val(e, 'parent_id'), _val(e, 'node_id'))
                for e in raw if _val(e, 'parent_id')]
    
    levels = assign_levels(node_ids, edges)

    nodes = [{
        "id": _val(e, 'node_id'),
        "label": f"{_val(e, 'step_type')}\n {_val(e, 'node_id')}",
        "group": _val(e, 'step_type'),
        "level": levels[_val(e, 'node_id')],
        "entries": e,                 
    } for e in raw]

    return {"nodes": nodes, "edges": [{"from": p, "to": c} for p, c in edges]}

def run_web_generator(store):
    graph_data = visualise_nodes(store)

    source = resources.files("ancestree.assets").joinpath("template_new.html")
    with source.open("r", encoding = "utf-8") as f:
        template_content = f.read()
    
    final_html = template_content.replace("{{PYTHON_NODES}}", json.dumps(graph_data["nodes"]))
    final_html = final_html.replace("{{PYTHON_EDGES}}", json.dumps(graph_data["edges"]))

    vis_network = (resources.files("ancestree.assets").joinpath("vis-network.min.js")).read_text()
    final_html = final_html.replace('<script type="text/javascript" src="../../web_app/vis-network.min.js"></script>', f'<script type="text/javascript">{vis_network}</script>')

    css = (resources.files("ancestree.assets").joinpath("styles.css")).read_text()
    final_html = final_html.replace('<link rel="stylesheet" href ="../../web_app/styles.css">', f'<style>{css}</style>')
    custom_js = (resources.files("ancestree.assets").joinpath("actions.js")).read_text()
    final_html = final_html.replace('<script src="../../web_app/actions.js"></script>', f'<script>{custom_js}</script>')

    location = f"{store.root}/interactive_pipeline.html"
    with open(location,'w') as f:
        f.write(final_html)

    return location
