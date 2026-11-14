import json
from typing import Dict, Any, List, Optional
from forge.store import Store

def handle_mcp_rpc(req_body: Dict[str, Any], viewer: str, store: Store) -> Dict[str, Any]:
    method = req_body.get("method")
    msg_id = req_body.get("id")
    params = req_body.get("params", {})
    
    if method == "server/discover":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "serverInfo": {
                    "name": "github-core-clone-mcp",
                    "version": "1.0.0"
                },
                "supportedVersions": ["2026-07-28", "2025-11-25"]
            }
        }

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2026-07-28",
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": {
                    "name": "github-core-clone-mcp",
                    "version": "1.0.0"
                }
            }
        }

    if method == "notifications/initialized":
        return {}

    if method == "tools/list":
        tools = [
            {
                "name": "create_repo",
                "description": "Create a new repository",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "private": {"type": "boolean"}
                    },
                    "required": ["name"]
                }
            },
            {
                "name": "get_repo",
                "description": "Get repository details",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                }
            },
            {
                "name": "create_issue",
                "description": "Create an issue",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "title": {"type": "string"},
                        "body": {"type": "string"}
                    },
                    "required": ["owner", "repo", "title"]
                }
            },
            {
                "name": "list_issues",
                "description": "List repository issues",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "state": {"type": "string"}
                    },
                    "required": ["owner", "repo"]
                }
            },
            {
                "name": "create_pull_request",
                "description": "Create a pull request",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "title": {"type": "string"},
                        "head": {"type": "string"},
                        "base": {"type": "string"}
                    },
                    "required": ["owner", "repo", "title", "head", "base"]
                }
            },
            {
                "name": "merge_pull_request",
                "description": "Merge a pull request",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "pull_number": {"type": "integer"}
                    },
                    "required": ["owner", "repo", "pull_number"]
                }
            }
        ]
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": tools}
        }

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        
        owner = arguments.get("owner", viewer)
        # Handle full_name like owner/repo
        full_name = arguments.get("full_name") or arguments.get("repo_full_name")
        if full_name and "/" in full_name:
            parts = full_name.split("/", 1)
            owner = parts[0]
            repo_name = parts[1]
        else:
            repo_name = arguments.get("repo") or arguments.get("name", "")

        if not tool_name:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": "Missing tool name"}
            }

        # Check required args for create_issue
        if tool_name == "create_issue" and not arguments.get("title"):
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": "Missing required argument: title"}],
                    "isError": True
                }
            }

        if tool_name == "create_repo":
            name = arguments.get("name", repo_name)
            desc = arguments.get("description", "")
            private = arguments.get("private", True)
            repo_obj = store.create_repo(owner, name, private, desc)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(repo_obj)}],
                    "structuredContent": repo_obj
                }
            }

        if tool_name == "get_repo":
            repo_obj = store.get_repo(owner, repo_name)
            if not repo_obj:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": "Repository not found"}],
                        "isError": True
                    }
                }
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(repo_obj)}],
                    "structuredContent": repo_obj
                }
            }

        if tool_name == "create_issue":
            repo_key = (owner.lower(), repo_name.lower())
            if repo_key not in store.repos:
                store.create_repo(owner, repo_name)
            num = store.issue_number_counters[repo_key]
            store.issue_number_counters[repo_key] += 1
            title = arguments.get("title", "")
            body = arguments.get("body", "")
            issue = {"number": num, "title": title, "body": body, "state": "open", "labels": [], "comments": []}
            store.issues[repo_key][num] = issue
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(issue)}],
                    "structuredContent": issue
                }
            }

        if tool_name == "list_issues":
            repo_key = (owner.lower(), repo_name.lower())
            issues_dict = store.issues.get(repo_key, {})
            issue_list = list(issues_dict.values())
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(issue_list)}],
                    "structuredContent": issue_list
                }
            }

        if tool_name == "create_pull_request":
            repo_key = (owner.lower(), repo_name.lower())
            if repo_key not in store.repos:
                store.create_repo(owner, repo_name)
            num = store.pr_number_counters[repo_key]
            store.pr_number_counters[repo_key] += 1
            title = arguments.get("title", "")
            head = arguments.get("head", "")
            base = arguments.get("base", "main")
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
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(pr)}],
                    "structuredContent": pr
                }
            }

        if tool_name == "merge_pull_request":
            repo_key = (owner.lower(), repo_name.lower())
            pull_num = arguments.get("pull_number") or arguments.get("number")
            pulls_dict = store.pulls.get(repo_key, {})
            pr = pulls_dict.get(int(pull_num)) if pull_num is not None else None
            if pr:
                pr["merged"] = True
                pr["state"] = "closed"
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(pr)}],
                        "structuredContent": pr
                    }
                }
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": "Pull request not found"}],
                    "isError": True
                }
            }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Tool not found: {tool_name}"}
        }

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"}
    }
