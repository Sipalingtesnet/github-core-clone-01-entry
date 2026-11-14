import hashlib
import zlib
import base64
import time
from typing import Dict, List, Optional, Set, Any, Tuple

class Store:
    """In-memory deterministic store for repositories, issues, PRs, labels, and git objects."""
    def __init__(self):
        self.repos: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.repo_id_counter = 1

        self.issues: Dict[Tuple[str, str], Dict[int, Dict[str, Any]]] = {}
        self.issue_number_counters: Dict[Tuple[str, str], int] = {}
        self.comment_id_counter = 1

        self.labels: Dict[Tuple[str, str], Dict[str, Dict[str, str]]] = {}
        self.pulls: Dict[Tuple[str, str], Dict[int, Dict[str, Any]]] = {}
        self.pr_number_counters: Dict[Tuple[str, str], int] = {}

        self.git_objects: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
        self.git_refs: Dict[Tuple[str, str], Dict[str, str]] = {}
        self.git_head: Dict[Tuple[str, str], str] = {}

    def get_repo(self, owner: str, name: str) -> Optional[Dict[str, Any]]:
        return self.repos.get((owner.lower(), name.lower()))

    def create_repo(self, owner: str, name: str, private: bool = True, description: str = "") -> Dict[str, Any]:
        key = (owner.lower(), name.lower())
        if key in self.repos:
            return self.repos[key]
        repo = {
            "id": self.repo_id_counter,
            "databaseId": self.repo_id_counter,
            "node_id": f"R_{self.repo_id_counter}",
            "owner": {"login": owner},
            "name": name,
            "full_name": f"{owner}/{name}",
            "nameWithOwner": f"{owner}/{name}",
            "private": private,
            "description": description,
            "default_branch": "main",
            "hasIssuesEnabled": True,
            "viewerPermission": "ADMIN",
            "created_at": "2026-01-01T00:00:00Z"
        }
        self.repo_id_counter += 1
        self.repos[key] = repo
        self.issues[key] = {}
        self.issue_number_counters[key] = 1
        self.labels[key] = {}
        self.pulls[key] = {}
        self.pr_number_counters[key] = 1
        self.git_objects[key] = {}
        self.git_refs[key] = {}
        self.git_head[key] = "refs/heads/main"
        return repo

    def delete_repo(self, owner: str, name: str) -> bool:
        key = (owner.lower(), name.lower())
        if key in self.repos:
            del self.repos[key]
            self.issues.pop(key, None)
            self.labels.pop(key, None)
            self.pulls.pop(key, None)
            self.git_objects.pop(key, None)
            self.git_refs.pop(key, None)
            self.git_head.pop(key, None)
            return True
        return False

    def list_user_repos(self, owner: str) -> List[Dict[str, Any]]:
        owner_lower = owner.lower()
        return [r for (o, n), r in self.repos.items() if o == owner_lower]

    def hash_object(self, obj_type: str, data: bytes) -> str:
        header = f"{obj_type} {len(data)}\0".encode("ascii")
        return hashlib.sha1(header + data).hexdigest()

    def store_object(self, owner: str, name: str, obj_type: str, data: bytes) -> str:
        key = (owner.lower(), name.lower())
        sha = self.hash_object(obj_type, data)
        if key not in self.git_objects:
            self.git_objects[key] = {}
        self.git_objects[key][sha] = {"type": obj_type, "data": data}
        return sha
