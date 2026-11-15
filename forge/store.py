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
        self.files: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}

    def get_repo(self, owner: str, name: str) -> Optional[Dict[str, Any]]:
        key = (owner.lower(), name.lower())
        if key in self.repos:
            return self.repos[key]
        name_lower = name.lower()
        for (o, n), repo in self.repos.items():
            if n == name_lower:
                return repo
        return None

    def resolve_repo_key(self, owner: str, name: str) -> Tuple[str, str]:
        repo = self.get_repo(owner, name)
        if repo:
            return (repo["owner"]["login"].lower(), repo["name"].lower())
        return (owner.lower(), name.lower())

    def get_issues_dict(self, owner: str, name: str) -> Dict[int, Dict[str, Any]]:
        key = self.resolve_repo_key(owner, name)
        if key not in self.issues:
            self.issues[key] = {}
            self.issue_number_counters[key] = 1
        return self.issues[key]

    def get_issue_counter(self, owner: str, name: str) -> int:
        key = self.resolve_repo_key(owner, name)
        if key not in self.issue_number_counters:
            self.issue_number_counters[key] = 1
        return self.issue_number_counters[key]

    def inc_issue_counter(self, owner: str, name: str) -> int:
        key = self.resolve_repo_key(owner, name)
        if key not in self.issue_number_counters:
            self.issue_number_counters[key] = 1
        val = self.issue_number_counters[key]
        self.issue_number_counters[key] += 1
        return val

    def get_pulls_dict(self, owner: str, name: str) -> Dict[int, Dict[str, Any]]:
        key = self.resolve_repo_key(owner, name)
        if key not in self.pulls:
            self.pulls[key] = {}
            self.pr_number_counters[key] = 1
        return self.pulls[key]

    def inc_pr_counter(self, owner: str, name: str) -> int:
        key = self.resolve_repo_key(owner, name)
        if key not in self.pr_number_counters:
            self.pr_number_counters[key] = 1
        val = self.pr_number_counters[key]
        self.pr_number_counters[key] += 1
        return val

    def get_labels_dict(self, owner: str, name: str) -> Dict[str, Dict[str, str]]:
        key = self.resolve_repo_key(owner, name)
        if key not in self.labels:
            self.labels[key] = {}
        return self.labels[key]

    def get_git_refs(self, owner: str, name: str) -> Dict[str, str]:
        key = self.resolve_repo_key(owner, name)
        if key not in self.git_refs:
            self.git_refs[key] = {}
        return self.git_refs[key]

    def get_git_head(self, owner: str, name: str) -> str:
        key = self.resolve_repo_key(owner, name)
        if key not in self.git_head:
            self.git_head[key] = "refs/heads/main"
        self.files[key] = {}
        return self.git_head[key]

    def get_git_objects(self, owner: str, name: str) -> Dict[str, Dict[str, Any]]:
        key = self.resolve_repo_key(owner, name)
        if key not in self.git_objects:
            self.git_objects[key] = {}
        return self.git_objects[key]

    def get_files_dict(self, owner: str, name: str) -> Dict[str, Dict[str, Any]]:
        key = self.resolve_repo_key(owner, name)
        if key not in self.files:
            self.files[key] = {}
        return self.files[key]

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
        self.files[key] = {}
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
            self.files.pop(key, None)
            return True
        return False

    def list_user_repos(self, owner: str) -> List[Dict[str, Any]]:
        owner_lower = owner.lower()
        matched = [r for (o, n), r in self.repos.items() if o == owner_lower]
        if not matched:
            return list(self.repos.values())
        return matched

    def hash_object(self, obj_type: str, data: bytes) -> str:
        header = f"{obj_type} {len(data)}\0".encode("ascii")
        return hashlib.sha1(header + data).hexdigest()

    def store_object(self, owner: str, name: str, obj_type: str, data: bytes) -> str:
        objs = self.get_git_objects(owner, name)
        sha = self.hash_object(obj_type, data)
        objs[sha] = {"type": obj_type, "data": data}
        return sha
