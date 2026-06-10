from pathlib import Path
import shelve
import json
from .utils import parse_iso_utc, is_match
from datetime import datetime
from zoneinfo import ZoneInfo

class lineage_database:
    def __init__(self, root):
        self.root = root
        self.shelf_path = str(Path(self.root) / 'metadata_shelf.db')

    def rebuild_from_disk(self):
        folder = Path(self.root)
        with shelve.open(self.shelf_path) as db:
            for file_path in folder.rglob("*meta.json"):
                with open(file_path, 'r', encoding='utf-8') as f:
                    metadata = json.load(f)
                key = str(file_path.parent.name)
                db[key] = {
                    k: v.get('value')
                    for k, v in metadata.items()
                    if isinstance(v, dict) and v.get('searchable', True)
                }

    def add(self, node_id, meta):
        with shelve.open(self.shelf_path) as db:
            db[node_id] = meta

    def remove(self, node_id):
        with shelve.open(self.shelf_path) as db:
            del db[node_id]

    def find_matches(self, **kwargs):
        correct_nodes = []
        
        with shelve.open(self.shelf_path) as db:
            shelve_keys = list(db.keys())

            for shelve_key in shelve_keys:

                meta = db[shelve_key]

                if is_match(meta, **kwargs):
                    correct_nodes.append(shelve_key)
        
        return correct_nodes
    
    def get_lineage(self, curr_node):
        history = []
        visited = set()
        with shelve.open(self.shelf_path) as db:
            while curr_node:
                if curr_node in visited:
                    raise ValueError(
                        f"Cycle detected in lineage at node '{curr_node}'. "
                        "The store metadata may be corrupted."
                    )
                if curr_node not in db:
                    raise KeyError(
                        f"Node '{curr_node}' not found in the index. "
                        "It may have been pruned without recursive=True. "
                        "Call store.rebuild_from_disk() to resync the index."
                    )
                visited.add(curr_node)
                history.append(curr_node)
                curr_node = db[curr_node].get('parent_id')
        return history[::-1]
    
    def get_most_recent(self, **kwargs):
        all_nodes = self.find_matches(**kwargs)

        best_seen = None
        to_beat = datetime.min.replace(tzinfo=ZoneInfo("UTC"))

        with shelve.open(self.shelf_path) as db:
            for key in all_nodes:
                timestamp = parse_iso_utc(db[key].get('timestamp'))

                if timestamp > to_beat:
                    to_beat = timestamp
                    best_seen = key
        
        return best_seen