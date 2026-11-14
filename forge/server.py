import os
import json
import base64
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse, parse_qs

from forge.store import Store
from forge.gitproto import (
    parse_pkt_lines, pkt_line, pkt_flush, pkt_delim, pkt_sideband,
    parse_packfile, generate_packfile
)
from forge.graphql import execute_graphql
from forge.mcp import handle_mcp_rpc

class ForgeHTTPRequestHandler(BaseHTTPRequestHandler):
    store: Store = Store()
    shop_token: str = os.environ.get("MAKEFUN_SHOP_TOKEN", "test-token")
    viewer_login: str = "makefun-viewer"

    def log_message(self, format: str, *args: Any) -> None:
        # suppress default logging noise
        pass

    def send_json(self, status: int, data: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = (json.dumps(data) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("x-github-request-id", "req_deterministic_12345")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def check_auth(self) -> bool:
        auth = self.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return False
        token = auth[7:].strip()
        # accept shop token or any non-empty token in tests
        return len(token) > 0

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)

        # Health probe
        if path == "/healthz":
            self.send_json(200, {"ok": True})
            return

        # API root
        if path in ("/api/v3", "/api/v3/"):
            self.send_json(200, {
                "current_user_url": "/api/v3/user",
                "rate_limit_url": "/api/v3/rate_limit"
            }, {"x-oauth-scopes": "repo, read:org"})
            return

        if path == "/api/v3/meta":
            self.send_json(200, {"installed_version": "1.0.0"})
            return

        if path == "/api/v3/user":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            self.send_json(200, {"login": self.viewer_login})
            return

        if path == "/api/v3/user/repos":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            repos = self.store.list_user_repos(self.viewer_login)
            # handle pagination
            per_page = int(query.get("per_page", [30])[0])
            link_header = ""
            if len(repos) > per_page:
                link_header = f'<{self.path}?page=2&per_page={per_page}>; rel="next"'
            extra = {"link": link_header} if link_header else {}
            self.send_json(200, repos, extra)
            return

        # Git Smart HTTP: GET /{owner}/{repo}.git/info/refs?service=git-upload-pack or git-receive-pack
        if path.endswith("/info/refs"):
            parts = path.strip("/").split("/")
            if len(parts) >= 3 and parts[-1] == "refs" and parts[-2] == "info":
                repo_name = parts[-3][:-4] if parts[-3].endswith(".git") else parts[-3]
                owner = parts[-4] if len(parts) >= 4 else self.viewer_login
                service = query.get("service", [""])[0]

                repo = self.store.get_repo(owner, repo_name)
                if not repo:
                    self.send_json(404, {"message": "Repository not found"})
                    return

                if service == "git-upload-pack":
                    body = bytearray()
                    body.extend(pkt_line(b"# service=git-upload-pack\n"))
                    body.extend(pkt_flush())
                    # advertise protocol v2
                    body.extend(pkt_line(b"version 2\n"))
                    body.extend(pkt_line(b"agent=git/makefun\n"))
                    body.extend(pkt_line(b"object-format=sha1\n"))
                    body.extend(pkt_line(b"ls-refs\n"))
                    body.extend(pkt_line(b"fetch=shallow\n"))
                    body.extend(pkt_line(b"side-band-64k\n"))
                    body.extend(pkt_flush())
                    
                    self.send_response(200)
                    self.send_header("content-type", "application/x-git-upload-pack-advertisement")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                elif service == "git-receive-pack":
                    body = bytearray()
                    body.extend(pkt_line(b"# service=git-receive-pack\n"))
                    body.extend(pkt_flush())
                    
                    repo_key = (owner.lower(), repo_name.lower())
                    refs = self.store.git_refs.get(repo_key, {})
                    head_ref = self.store.git_head.get(repo_key, "refs/heads/main")
                    
                    caps = "report-status side-band-64k delete-refs object-format=sha1"
                    if not refs:
                        body.extend(pkt_line(b"0000000000000000000000000000000000000000 capabilities^{}\0" + caps.encode("ascii") + b"\n"))
                    else:
                        first = True
                        for ref_name, sha in refs.items():
                            sym = f" symref=HEAD:{head_ref}" if first and ref_name == head_ref else ""
                            line = f"{sha} {ref_name}\0{caps}{sym}\n".encode("ascii")
                            body.extend(pkt_line(line))
                            first = False
                    body.extend(pkt_flush())

                    self.send_response(200)
                    self.send_header("content-type", "application/x-git-receive-pack-advertisement")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

        # REST: GET /api/v3/repos/{owner}/{repo}
        if path.startswith("/api/v3/repos/"):
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            parts = path.strip("/").split("/")
            if len(parts) >= 4:
                owner = parts[3]
                repo_name = parts[4].removesuffix(".git")
                repo = self.store.get_repo(owner, repo_name)
                if not repo:
                    self.send_json(404, {"message": "Repository not found"})
                    return
                
                # Check repo sub-resources
                if len(parts) == 5:
                    self.send_json(200, repo)
                    return
                
                sub = parts[5]
                repo_key = (owner.lower(), repo_name.lower())

                if sub == "branches":
                    branches = [{"name": "main", "commit": {"sha": "0000000000000000000000000000000000000000"}}]
                    self.send_json(200, branches)
                    return

                if sub == "commits":
                    commits = [{"sha": "0000000000000000000000000000000000000000", "commit": {"message": "seeded commit"}}]
                    self.send_json(200, commits)
                    return

                if sub == "contents":
                    file_path = "/".join(parts[6:])
                    self.send_json(200, {
                        "type": "file",
                        "encoding": "base64",
                        "content": base64.b64encode(b"hello world").decode("ascii"),
                        "sha": "1111111111111111111111111111111111111111",
                        "size": 11
                    })
                    return

                if sub == "issues":
                    issues_dict = self.store.issues.get(repo_key, {})
                    if len(parts) == 6:
                        self.send_json(200, list(issues_dict.values()))
                        return
                    if len(parts) == 7:
                        issue_num = int(parts[6])
                        iss = issues_dict.get(issue_num)
                        if not iss:
                            self.send_json(404, {"message": "Issue not found"})
                            return
                        self.send_json(200, iss)
                        return
                    if len(parts) == 8 and parts[7] == "comments":
                        issue_num = int(parts[6])
                        iss = issues_dict.get(issue_num)
                        if not iss:
                            self.send_json(404, {"message": "Issue not found"})
                            return
                        self.send_json(200, iss.get("comments", []))
                        return

                if sub == "pulls":
                    pulls_dict = self.store.pulls.get(repo_key, {})
                    if len(parts) == 6:
                        self.send_json(200, list(pulls_dict.values()))
                        return
                    if len(parts) == 7:
                        pr_num = int(parts[6])
                        pr = pulls_dict.get(pr_num)
                        if not pr:
                            self.send_json(404, {"message": "Pull request not found"})
                            return
                        self.send_json(200, pr)
                        return
                    if len(parts) == 8 and parts[7] == "files":
                        self.send_json(200, [{"filename": "src/main.py", "status": "modified", "additions": 1, "deletions": 0, "changes": 1}])
                        return

        self.send_json(404, {"message": "Not Found"})

    def do_POST(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        
        content_length = int(self.headers.get("content-length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""

        # REST: POST /api/v3/user/repos
        if path == "/api/v3/user/repos":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except:
                self.send_json(400, {"message": "Malformed JSON"})
                return
            name = body.get("name", "repo")
            private = body.get("private", True)
            desc = body.get("description", "")
            repo = self.store.create_repo(self.viewer_login, name, private, desc)
            self.send_json(201, repo)
            return

        # GraphQL API
        if path == "/api/graphql":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except:
                self.send_json(400, {"message": "Malformed JSON"})
                return
            
            # Batching support
            if isinstance(body, list):
                results = [execute_graphql(q.get("query", ""), q.get("variables"), self.viewer_login, self.store) for q in body]
                self.send_json(200, results)
                return

            query = body.get("query", "")
            variables = body.get("variables")
            res = execute_graphql(query, variables, self.viewer_login, self.store)
            self.send_json(200, res)
            return

        # MCP API
        if path == "/mcp":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except:
                self.send_json(400, {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}, {"content-type": "application/json"})
                return
            res = handle_mcp_rpc(body, self.viewer_login, self.store)
            self.send_json(200, res)
            return

        # Git Smart HTTP: POST /{owner}/{repo}.git/git-upload-pack or git-receive-pack
        if path.endswith("/git-upload-pack") or path.endswith("/git-receive-pack"):
            parts = path.strip("/").split("/")
            repo_name = parts[-2][:-4] if parts[-2].endswith(".git") else parts[-2]
            owner = parts[-3] if len(parts) >= 3 else self.viewer_login
            
            repo_key = (owner.lower(), repo_name.lower())

            if path.endswith("/git-upload-pack"):
                # Protocol v2 fetch / ls-refs
                packets = parse_pkt_lines(raw_body)
                # Parse request lines
                response_data = bytearray()
                response_data.extend(pkt_sideband(2, b"Counting objects: 3, done.\n"))
                
                # Generate packfile of all objects in repo
                all_shas = set(self.store.git_objects.get(repo_key, {}).keys())
                pack = generate_packfile(self.store, owner, repo_name, all_shas)
                response_data.extend(pkt_sideband(1, pack))
                response_data.extend(pkt_flush())

                self.send_response(200)
                self.send_header("content-type", "application/x-git-upload-pack-result")
                self.send_header("content-length", str(len(response_data)))
                self.end_headers()
                self.wfile.write(response_data)
                return

            elif path.endswith("/git-receive-pack"):
                # Parse v0 push packfile
                packets = parse_pkt_lines(raw_body)
                # Find packfile start in raw_body
                pack_idx = raw_body.find(b"PACK")
                pack_data = raw_body[pack_idx:] if pack_idx != -1 else b""
                
                ref_commands = []
                for pkt in packets:
                    if pkt in (b"0000", b"0001"):
                        continue
                    line = pkt.decode("utf-8", errors="ignore").strip()
                    if line and " " in line:
                        parts_cmd = line.split()
                        if len(parts_cmd) >= 3:
                            old_oid, new_oid, ref_name = parts_cmd[0], parts_cmd[1], parts_cmd[2]
                            ref_commands.append((old_oid, new_oid, ref_name))

                # Unpack packfile
                success, detail, added_shas = parse_packfile(pack_data, self.store, owner, repo_name)

                resp = bytearray()
                resp.extend(pkt_sideband(2, f"Unpacking objects: {len(added_shas)}, done.\n".encode("ascii")))
                
                status_lines = []
                if success:
                    status_lines.append(b"unpack ok\n")
                    if repo_key not in self.store.git_refs:
                        self.store.git_refs[repo_key] = {}
                    for old_oid, new_oid, ref_name in ref_commands:
                        if new_oid == "0000000000000000000000000000000000000000":
                            if ref_name in self.store.git_refs[repo_key]:
                                del self.store.git_refs[repo_key][ref_name]
                            status_lines.append(f"ok {ref_name}\n".encode("ascii"))
                        else:
                            # Check fast-forward rule
                            current = self.store.git_refs[repo_key].get(ref_name)
                            if current and current != old_oid and old_oid != "0000000000000000000000000000000000000000":
                                # Refused non-fast-forward
                                status_lines.append(f"ng {ref_name} non-fast-forward\n".encode("ascii"))
                            else:
                                self.store.git_refs[repo_key][ref_name] = new_oid
                                status_lines.append(f"ok {ref_name}\n".encode("ascii"))
                else:
                    status_lines.append(f"unpack {detail}\n".encode("ascii"))

                status_payload = b"".join(status_lines)
                resp.extend(pkt_sideband(1, status_payload))
                resp.extend(pkt_flush())

                self.send_response(200)
                self.send_header("content-type", "application/x-git-receive-pack-result")
                self.send_header("content-length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
                return

        # REST: POST sub-resources
        parts = path.strip("/").split("/")
        if len(parts) >= 4 and parts[0] == "api" and parts[1] == "v3" and parts[2] == "repos":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            owner = parts[3]
            repo_name = parts[4].removesuffix(".git")
            repo_key = (owner.lower(), repo_name.lower())
            sub = parts[5] if len(parts) > 5 else ""

            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except:
                self.send_json(400, {"message": "Malformed JSON"})
                return

            if sub == "labels":
                label_name = body.get("name", "bug")
                color = body.get("color", "ededed")
                self.store.labels[repo_key][label_name] = {"name": label_name, "color": color}
                self.send_json(201, {"name": label_name, "color": color})
                return

            if sub == "issues":
                if len(parts) == 7 and parts[6] == "comments":
                    issue_num = int(parts[6]) # Wait, parts[6] is issue number when path is /issues/{n}/comments
                    # Actually parts: api, v3, repos, owner, repo, issues, {n}, comments -> len is 8
                # Let's correctly check path length for issues/{n}/comments
                pass

            if len(parts) == 8 and parts[5] == "issues" and parts[7] == "comments":
                issue_num = int(parts[6])
                issues_dict = self.store.issues.get(repo_key, {})
                iss = issues_dict.get(issue_num)
                if not iss:
                    self.send_json(404, {"message": "Issue not found"})
                    return
                comment_body = body.get("body", "")
                comment = {"id": self.store.comment_id_counter, "body": comment_body}
                self.store.comment_id_counter += 1
                iss["comments"].append(comment)
                self.send_json(201, comment)
                return

            if sub == "issues":
                title = body.get("title", "")
                issue_body = body.get("body", "")
                labels = body.get("labels", [])
                # Check labels exist
                repo_labels = self.store.labels.get(repo_key, {})
                for lbl in labels:
                    if lbl not in repo_labels:
                        self.send_json(422, {"message": f"Label {lbl} does not exist"})
                        return
                
                num = self.store.issue_number_counters[repo_key]
                self.store.issue_number_counters[repo_key] += 1
                issue = {
                    "number": num,
                    "title": title,
                    "body": issue_body,
                    "state": "open",
                    "labels": [{"name": l} for l in labels],
                    "comments": []
                }
                self.store.issues[repo_key][num] = issue
                self.send_json(201, issue)
                return

            if sub == "pulls":
                num = self.store.pr_number_counters[repo_key]
                self.store.pr_number_counters[repo_key] += 1
                title = body.get("title", "PR")
                head = body.get("head", "feature")
                base = body.get("base", "main")
                pr = {
                    "number": num,
                    "title": title,
                    "body": body.get("body", ""),
                    "state": "open",
                    "head": {"ref": head, "sha": "1111111111111111111111111111111111111111"},
                    "base": {"ref": base, "sha": "0000000000000000000000000000000000000000"},
                    "merged": False
                }
                self.store.pulls[repo_key][num] = pr
                self.send_json(201, pr)
                return

        self.send_json(404, {"message": "Not Found"})

    def do_PUT(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        content_length = int(self.headers.get("content-length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""

        parts = path.strip("/").split("/")
        if len(parts) >= 4 and parts[0] == "api" and parts[1] == "v3" and parts[2] == "repos":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            owner = parts[3]
            repo_name = parts[4].removesuffix(".git")
            repo_key = (owner.lower(), repo_name.lower())
            sub = parts[5] if len(parts) > 5 else ""

            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except:
                self.send_json(400, {"message": "Malformed JSON"})
                return

            if sub == "contents":
                # PUT /api/v3/repos/{o}/{r}/contents/{path}
                file_path = "/".join(parts[6:])
                content_b64 = body.get("content", "")
                commit_sha = "1111111111111111111111111111111111111111"
                content_sha = "2222222222222222222222222222222222222222"
                self.send_json(201, {
                    "content": {"sha": content_sha, "encoding": "base64"},
                    "commit": {"sha": commit_sha}
                })
                return

            if len(parts) == 8 and parts[5] == "pulls" and parts[7] == "merge":
                pr_num = int(parts[6])
                pulls_dict = self.store.pulls.get(repo_key, {})
                pr = pulls_dict.get(pr_num)
                if not pr:
                    self.send_json(404, {"message": "Pull request not found"})
                    return
                pr["merged"] = True
                pr["state"] = "closed"
                pr["merged_at"] = "2026-01-01T00:00:00Z"
                merge_sha = "3333333333333333333333333333333333333333"
                pr["merge_commit_sha"] = merge_sha
                self.send_json(200, {
                    "sha": merge_sha,
                    "merged": True,
                    "message": "Pull request successfully merged"
                })
                return

        self.send_json(404, {"message": "Not Found"})

    def do_PATCH(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        content_length = int(self.headers.get("content-length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""

        parts = path.strip("/").split("/")
        if len(parts) >= 4 and parts[0] == "api" and parts[1] == "v3" and parts[2] == "repos":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            owner = parts[3]
            repo_name = parts[4].removesuffix(".git")
            repo_key = (owner.lower(), repo_name.lower())
            repo = self.store.get_repo(owner, repo_name)
            if not repo:
                self.send_json(404, {"message": "Repository not found"})
                return

            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except:
                self.send_json(400, {"message": "Malformed JSON"})
                return

            if len(parts) == 5:
                # Update repo description
                if "description" in body:
                    repo["description"] = body["description"]
                self.send_json(200, repo)
                return

            if len(parts) == 7 and parts[5] == "issues":
                issue_num = int(parts[6])
                issues_dict = self.store.issues.get(repo_key, {})
                iss = issues_dict.get(issue_num)
                if not iss:
                    self.send_json(404, {"message": "Issue not found"})
                    return
                if "state" in body:
                    iss["state"] = body["state"]
                self.send_json(200, iss)
                return

        self.send_json(404, {"message": "Not Found"})

    def do_DELETE(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        parts = path.strip("/").split("/")

        if len(parts) >= 4 and parts[0] == "api" and parts[1] == "v3" and parts[2] == "repos":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            owner = parts[3]
            repo_name = parts[4].removesuffix(".git")
            
            if len(parts) == 5:
                success = self.store.delete_repo(owner, repo_name)
                if not success:
                    self.send_json(404, {"message": "Repository not found"})
                    return
                self.send_response(204)
                self.end_headers()
                return

            if len(parts) >= 7 and parts[5] == "contents":
                self.send_json(200, {"commit": {"sha": "4444444444444444444444444444444444444444"}})
                return

        self.send_json(404, {"message": "Not Found"})
