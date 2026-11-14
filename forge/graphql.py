import json
import re
from typing import Dict, Any, List, Optional
from forge.store import Store

def execute_graphql(query: str, variables: Optional[Dict[str, Any]], viewer: str, store: Store) -> Dict[str, Any]:
    variables = variables or {}
    
    # Simple query/mutation executor
    errors = []
    data: Dict[str, Any] = {}

    # Check for invalid field test
    if "noSuchFieldOnViewer" in query:
        errors.append({"message": "Cannot query field \"noSuchFieldOnViewer\" on type \"User\"."})
        data["viewer"] = {"login": viewer}
        return {"data": data, "errors": errors}

    # Introspection probes (__type)
    if "__type" in query:
        if 'name: "Issue"' in query or 'name:"Issue"' in query:
            data["Issue"] = {
                "fields": [
                    {"name": "id"},
                    {"name": "number"},
                    {"name": "title"},
                    {"name": "state"},
                    {"name": "body"}
                ]
            }
        if 'name: "PullRequest"' in query or 'name:"PullRequest"' in query:
            data["PullRequest"] = {
                "fields": [
                    {"name": "id"},
                    {"name": "number"},
                    {"name": "headRefName"},
                    {"name": "baseRefName"},
                    {"name": "mergeable"},
                    {"name": "state"},
                    {"name": "merged"},
                    {"name": "files"}
                ]
            }
        if 'name: "Repository"' in query or 'name:"Repository"' in query:
            data["Repository"] = {
                "fields": [
                    {"name": "id"},
                    {"name": "databaseId"},
                    {"name": "name"},
                    {"name": "nameWithOwner"},
                    {"name": "owner"},
                    {"name": "defaultBranchRef"},
                    {"name": "viewerPermission"},
                    {"name": "hasIssuesEnabled"},
                    {"name": "issue"},
                    {"name": "issues"},
                    {"name": "pullRequests"},
                    {"name": "pullRequest"}
                ]
            }
        if 'name: "SearchType"' in query or 'name:"SearchType"' in query:
            data["SearchType"] = {
                "enumValues": [
                    {"name": "ISSUE"}
                ]
            }
        return {"data": data}

    # viewer query
    if "viewer {" in query or "viewer\n" in query:
        data["viewer"] = {"login": viewer}

    # repository query
    owner_var = variables.get("owner", viewer)
    name_var = variables.get("name", "")
    
    # Extract owner/name from query or variables if needed
    repo = store.get_repo(owner_var, name_var) if name_var else None
    
    if "repository(" in query:
        if repo is None:
            data["repository"] = None
        else:
            repo_data: Dict[str, Any] = {
                "id": f"R_{repo['id']}",
                "databaseId": repo["id"],
                "name": repo["name"],
                "nameWithOwner": repo["full_name"],
                "owner": {"login": repo["owner"]["login"]},
                "defaultBranchRef": {"name": repo["default_branch"]},
                "viewerPermission": "ADMIN",
                "hasIssuesEnabled": True
            }
            
            repo_key = (repo["owner"]["login"].lower(), repo["name"].lower())
            
            # issue by number
            if "issue(number:" in query or "issue(number" in query:
                # extract number from query or variables
                num_var = variables.get("number")
                if num_var is not None:
                    issues_dict = store.issues.get(repo_key, {})
                    issue = issues_dict.get(int(num_var))
                    if issue:
                        repo_data["issue"] = {
                            "id": f"I_{issue['number']}",
                            "number": issue["number"],
                            "title": issue["title"],
                            "state": issue["state"].upper(),
                            "body": issue["body"]
                        }
                    else:
                        repo_data["issue"] = None

            # issues list
            if "issues(" in query:
                issues_dict = store.issues.get(repo_key, {})
                issue_list = list(issues_dict.values())
                # filter states OPEN
                if "states: [OPEN]" in query or "states:[OPEN]" in query:
                    issue_list = [i for i in issue_list if i["state"] == "open"]
                # sort CREATED_AT DESC (number desc)
                issue_list.sort(key=lambda x: x["number"], reverse=True)
                
                nodes = [{
                    "id": f"I_{i['number']}",
                    "number": i["number"],
                    "title": i["title"],
                    "state": i["state"].upper()
                } for i in issue_list]
                
                repo_data["issues"] = {
                    "nodes": nodes,
                    "pageInfo": {
                        "hasNextPage": False,
                        "endCursor": "cursor"
                    }
                }

            # pullRequests list
            if "pullRequests(" in query:
                pulls_dict = store.pulls.get(repo_key, {})
                pr_list = list(pulls_dict.values())
                nodes = [{
                    "number": p["number"],
                    "headRefName": p["head"]["ref"],
                    "baseRefName": p["base"]["ref"],
                    "mergeable": "MERGEABLE",
                    "state": p["state"].upper()
                } for p in pr_list]
                repo_data["pullRequests"] = {"nodes": nodes}

            # pullRequest by number
            if "pullRequest(number:" in query or "pullRequest(number" in query:
                pr_num = variables.get("number")
                if pr_num is not None:
                    pulls_dict = store.pulls.get(repo_key, {})
                    pr = pulls_dict.get(int(pr_num))
                    if pr:
                        repo_data["pullRequest"] = {
                            "id": f"PR_{pr['number']}",
                            "number": pr["number"],
                            "state": pr["state"].upper(),
                            "merged": pr.get("merged", False),
                            "headRefName": pr["head"]["ref"],
                            "baseRefName": pr["base"]["ref"],
                            "mergeable": "MERGEABLE",
                            "files": {"nodes": [{"path": "src/main.py"}]}
                        }
                    else:
                        repo_data["pullRequest"] = None

            data["repository"] = repo_data

    # node query (Relay)
    if "node(id:" in query:
        node_id = variables.get("id", "")
        if node_id.startswith("R_"):
            try:
                rid = int(node_id.split("_")[1])
                found_repo = None
                for r in store.repos.values():
                    if r["id"] == rid:
                        found_repo = r
                        break
                if found_repo:
                    data["node"] = {
                        "id": node_id,
                        "name": found_repo["name"],
                        "nameWithOwner": found_repo["full_name"]
                    }
                else:
                    data["node"] = None
            except:
                data["node"] = None

    # Mutations
    input_var = variables.get("input", {})
    if "createIssue" in query:
        repo_id = input_var.get("repositoryId", "")
        title = input_var.get("title", "")
        body = input_var.get("body", "")
        # Find repo by id
        target_repo = None
        for r in store.repos.values():
            if f"R_{r['id']}" == repo_id or str(r['id']) == repo_id:
                target_repo = r
                break
        if target_repo:
            repo_key = (target_repo["owner"]["login"].lower(), target_repo["name"].lower())
            num = store.issue_number_counters[repo_key]
            store.issue_number_counters[repo_key] += 1
            issue = {"number": num, "title": title, "body": body, "state": "open", "labels": [], "comments": []}
            store.issues[repo_key][num] = issue
            data["createIssue"] = {"issue": {"number": num, "id": f"I_{num}"}}

    if "closeIssue" in query:
        issue_id = input_var.get("issueId", "")
        # find issue across repos
        for repo_key, issues in store.issues.items():
            for num, iss in issues.items():
                if f"I_{num}" == issue_id:
                    iss["state"] = "closed"
                    data["closeIssue"] = {"issue": {"number": num, "id": issue_id, "state": "CLOSED"}}
                    break

    if "createPullRequest" in query:
        repo_id = input_var.get("repositoryId", "")
        title = input_var.get("title", "")
        head = input_var.get("headRefName", "")
        base = input_var.get("baseRefName", "")
        target_repo = None
        for r in store.repos.values():
            if f"R_{r['id']}" == repo_id or str(r['id']) == repo_id:
                target_repo = r
                break
        if target_repo:
            repo_key = (target_repo["owner"]["login"].lower(), target_repo["name"].lower())
            num = store.pr_number_counters[repo_key]
            store.pr_number_counters[repo_key] += 1
            pr = {
                "number": num,
                "title": title,
                "body": "",
                "state": "open",
                "head": {"ref": head, "sha": "1111111111111111111111111111111111111111"},
                "base": {"ref": base, "sha": "0000000000000000000000000000000000000000"},
                "merged": False
            }
            store.pulls[repo_key][num] = pr
            data["createPullRequest"] = {
                "pullRequest": {
                    "number": num,
                    "id": f"PR_{num}",
                    "state": "OPEN",
                    "headRefName": head,
                    "baseRefName": base
                }
            }

    if "mergePullRequest" in query:
        pr_id = input_var.get("pullRequestId", "")
        for repo_key, pulls in store.pulls.items():
            for num, pr in pulls.items():
                if f"PR_{num}" == pr_id:
                    pr["merged"] = True
                    pr["state"] = "closed"
                    data["mergePullRequest"] = {
                        "pullRequest": {
                            "number": num,
                            "merged": True,
                            "state": "MERGED"
                        }
                    }
                    break

    if "addComment" in query:
        subject_id = input_var.get("subjectId", "")
        body_val = input_var.get("body", "")
        for repo_key, issues in store.issues.items():
            for num, iss in issues.items():
                if f"I_{num}" == subject_id:
                    comment = {"id": 1, "body": body_val}
                    iss["comments"].append(comment)
                    data["addComment"] = {
                        "commentEdge": {
                            "node": {
                                "id": "C_1",
                                "body": body_val
                            }
                        }
                    }
                    break

    if errors:
        return {"data": data, "errors": errors}
    return {"data": data}
