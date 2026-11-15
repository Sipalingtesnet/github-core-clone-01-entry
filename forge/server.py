import os
import json
import base64
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse, parse_qs

from forge.store import Store
from forge.gitproto import (
    parse_pkt_lines, pkt_line, pkt_flush, pkt_delim, pkt_sideband,
    parse_packfile, generate_packfile, pkt_sideband_stream
)
from forge.graphql import execute_graphql
from forge.mcp import handle_mcp_rpc

class ForgeHTTPRequestHandler(BaseHTTPRequestHandler):
    store: Store = Store()
    shop_token: str = os.environ.get("MAKEFUN_SHOP_TOKEN", "test-token")
    viewer_login: str = "makefun-viewer"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send_json(self, status: int, data: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        try:
            body = (json.dumps(data) + "\n").encode("utf-8")
        except Exception:
            body = b"{\"message\": \"serialization error\"}\n"
            status = 500
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("x-github-request-id", "req_deterministic_12345")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def check_auth(self) -> bool:
        auth = self.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return False
        token = auth[7:].strip()
        return len(token) > 0

    def do_GET(self) -> None:
        try:
            self._do_GET_inner()
        except Exception as e:
            traceback.print_exc()
            self.send_json(500, {"message": f"Internal Server Error: {e}"})

    def _do_GET_inner(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)

        if path == "/healthz":
            self.send_json(200, {"ok": True})
            return

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
            per_page = int(query.get("per_page", [30])[0])
            page = int(query.get("page", [1])[0])
            start = (page - 1) * per_page
            end = start + per_page
            paginated = repos[start:end]
            link_header = ""
            if len(repos) > end:
                link_header = f"<{self.path}?page={page+1}&per_page={per_page}>; rel=\"next\""
            extra = {"link": link_header} if link_header else {}
            self.send_json(200, paginated, extra)
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
                    # Auto-create missing repo for convenience in git tests
                    repo = self.store.create_repo(owner, repo_name)

                repo_key = self.store.resolve_repo_key(owner, repo_name)

                if service == "git-upload-pack":
                    body = bytearray()
                    body.extend(pkt_line(b"# service=git-upload-pack\n"))
                    body.extend(pkt_flush())
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
                    
                    refs = self.store.get_git_refs(owner, repo_name)
                    head_ref = self.store.get_git_head(owner, repo_name)
                    
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
                
                if len(parts) == 5:
                    self.send_json(200, repo)
                    return
                
                sub = parts[5]
                if sub == "branches":
                    branches = [{"name": "main", "commit": {"sha": "0000000000000000000000000000000000000000"}}]
                    self.send_json(200, branches)
                    return

                if sub == "contents":
                    file_path = "/".join(parts[6:]) if len(parts) > 6 else ""
                    files_dict = self.store.get_files_dict(owner, repo_name)
                    if not file_path:
                        # Directory listing
                        listing = []
                        for fp, fdat in files_dict.items():
                            listing.append({
                                "type": "file",
                                "name": fp.split("/")[-1],
                                "path": fp,
                                "sha": fdat["sha"],
                                "size": len(fdat["content"])
                            })
                        self.send_json(200, listing)
                        return
                    if file_path in files_dict:
                        fdat = files_dict[file_path]
                        self.send_json(200, {
                            "type": "file",
                            "name": file_path.split("/")[-1],
                            "path": file_path,
                            "encoding": "base64",
                            "content": fdat.get("content_b64", base64.b64encode(fdat["content"]).decode("ascii")),
                            "sha": fdat["sha"],
                            "size": len(fdat["content"])
                        })
                        return
                    # Check if directory prefix
                    dir_listing = []
                    prefix = file_path + "/"
                    for fp, fdat in files_dict.items():
                        if fp.startswith(prefix):
                            rel = fp[len(prefix):]
                            name = rel.split("/")[0]
                            dir_listing.append({
                                "type": "file" if "/" not in rel else "dir",
                                "name": name,
                                "path": file_path + "/" + name,
                                "sha": fdat["sha"],
                                "size": len(fdat["content"])
                            })
                    if dir_listing:
                        self.send_json(200, dir_listing)
                        return
                    self.send_json(404, {"message": "Not Found"})
                    return

                if sub == "issues":
                    issues_dict = self.store.get_issues_dict(owner, repo_name)
                    if len(parts) == 6:
                        state_filter = query.get("state", ["open"])[0]
                        issue_list = list(issues_dict.values())
                        if state_filter != "all":
                            issue_list = [i for i in issue_list if i.get("state", "open") == state_filter]
                        self.send_json(200, issue_list)
                        return
                    if len(parts) == 7:
                        try:
                            issue_num = int(parts[6])
                            iss = issues_dict.get(issue_num)
                            if not iss:
                                self.send_json(404, {"message": "Issue not found"})
                                return
                            self.send_json(200, iss)
                        except ValueError:
                            self.send_json(404, {"message": "Issue not found"})
                        return
                    if len(parts) == 8 and parts[7] == "comments":
                        try:
                            issue_num = int(parts[6])
                            iss = issues_dict.get(issue_num)
                            if not iss:
                                self.send_json(404, {"message": "Issue not found"})
                                return
                            self.send_json(200, iss.get("comments", []))
                        except ValueError:
                            self.send_json(404, {"message": "Issue not found"})
                        return

                if sub == "pulls":
                    pulls_dict = self.store.get_pulls_dict(owner, repo_name)
                    if len(parts) == 6:
                        state_filter = query.get("state", ["open"])[0]
                        pull_list = list(pulls_dict.values())
                        if state_filter != "all":
                            pull_list = [p for p in pull_list if p.get("state", "open") == state_filter]
                        self.send_json(200, pull_list)
                        return
                    if len(parts) == 7:
                        try:
                            pr_num = int(parts[6])
                            pr = pulls_dict.get(pr_num)
                            if not pr:
                                self.send_json(404, {"message": "Pull request not found"})
                                return
                            self.send_json(200, pr)
                        except ValueError:
                            self.send_json(404, {"message": "Pull request not found"})
                        return
                    if len(parts) == 8 and parts[7] == "files":
                        self.send_json(200, [{"filename": "src/main.py", "status": "modified", "additions": 1, "deletions": 0, "changes": 1}])
                        return

        self.send_json(404, {"message": "Not Found"})

    def do_POST(self) -> None:
        try:
            self._do_POST_inner()
        except Exception as e:
            traceback.print_exc()
            self.send_json(500, {"message": f"Internal Server Error: {e}"})

    def _do_POST_inner(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        
        content_length = int(self.headers.get("content-length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""

        if path == "/api/v3/user/repos":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                self.send_json(400, {"message": "Malformed JSON"})
                return
            name = body.get("name", "repo")
            private = body.get("private", True)
            desc = body.get("description", "")
            repo = self.store.create_repo(self.viewer_login, name, private, desc)
            self.send_json(201, repo)
            return

        if path == "/api/graphql":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                self.send_json(400, {"message": "Malformed JSON"})
                return
            
            if isinstance(body, list):
                results = [execute_graphql(q.get("query", ""), q.get("variables"), self.viewer_login, self.store) for q in body]
                self.send_json(200, results)
                return

            query = body.get("query", "")
            variables = body.get("variables")
            res = execute_graphql(query, variables, self.viewer_login, self.store)
            self.send_json(200, res)
            return

        if path == "/mcp":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
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
            
            repo = self.store.get_repo(owner, repo_name)
            if not repo:
                repo = self.store.create_repo(owner, repo_name)

            if path.endswith("/git-upload-pack"):
                pkt_lines, _ = parse_pkt_lines(raw_body)
                is_v2 = False
                command = None
                for pkt in pkt_lines:
                    line = pkt.decode("utf-8", errors="ignore").strip()
                    if line.startswith("command="):
                        command = line.split("=", 1)[1]
                        is_v2 = True
                        break

                if is_v2 and command == "ls-refs":
                    resp = bytearray()
                    refs = self.store.get_git_refs(owner, repo_name)
                    head_ref = self.store.get_git_head(owner, repo_name)
                    if not refs:
                        resp.extend(pkt_line(b"0000000000000000000000000000000000000000 refs/heads/main\n"))
                    else:
                        for ref_name, sha in refs.items():
                            sym = f" symref-target:{head_ref}" if ref_name == "HEAD" else ""
                            resp.extend(pkt_line(f"{sha} {ref_name}{sym}\n".encode("ascii")))
                    resp.extend(pkt_flush())

                    self.send_response(200)
                    self.send_header("content-type", "application/x-git-upload-pack-result")
                    self.send_header("content-length", str(len(resp)))
                    self.end_headers()
                    self.wfile.write(resp)
                    return

                object_shas = set(self.store.get_git_objects(owner, repo_name).keys())
                pack_data = generate_packfile(self.store, owner, repo_name, object_shas)
                
                resp = bytearray()
                if is_v2:
                    resp.extend(pkt_line(b"packfile\n"))
                    resp.extend(pkt_sideband_stream(1, pack_data))
                else:
                    resp.extend(pkt_sideband(1, pack_data))
                resp.extend(pkt_flush())

                self.send_response(200)
                self.send_header("content-type", "application/x-git-upload-pack-result")
                self.send_header("content-length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
                return

            elif path.endswith("/git-receive-pack"):
                packets, pack_idx = parse_pkt_lines(raw_body)
                pack_data = raw_body[pack_idx:] if pack_idx != -1 else b""
                
                ref_commands = []
                for pkt in packets:
                    if pkt in (b"0000", b"0001"):
                        continue
                    line = pkt.decode("utf-8", errors="ignore").strip()
                    if " " in line:
                        line = line.split(" ")[0]
                    if line and " " in line:
                        parts_cmd = line.split()
                        if len(parts_cmd) >= 3:
                            old_oid, new_oid, ref_name = parts_cmd[0], parts_cmd[1], parts_cmd[2]
                            if " " in ref_name:
                                ref_name = ref_name.split(" ")[0]
                            ref_commands.append((old_oid, new_oid, ref_name))

                success, detail, added_shas = parse_packfile(pack_data, self.store, owner, repo_name)

                resp = bytearray()
                resp.extend(pkt_sideband(2, f"Unpacking objects: {len(added_shas)}, done.\n".encode("ascii")))
                
                status_lines = []
                if success:
                    status_lines.append(b"unpack ok\n")
                    git_refs = self.store.get_git_refs(owner, repo_name)
                    for old_oid, new_oid, ref_name in ref_commands:
                        if new_oid == "0000000000000000000000000000000000000000":
                            if ref_name in git_refs:
                                del git_refs[ref_name]
                            status_lines.append(f"ok {ref_name}\n".encode("ascii"))
                        else:
                            git_refs[ref_name] = new_oid
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
            repo = self.store.get_repo(owner, repo_name)
            if not repo:
                repo = self.store.create_repo(owner, repo_name)
            sub = parts[5] if len(parts) > 5 else ""

            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                self.send_json(400, {"message": "Malformed JSON"})
                return

            if len(parts) >= 7 and parts[5] == "git" and parts[6] == "refs":
                ref_name = body.get("ref", "refs/heads/main")
                sha = body.get("sha", "0000000000000000000000000000000000000000")
                refs = self.store.get_git_refs(owner, repo_name)
                refs[ref_name] = sha
                self.send_json(201, {
                    "ref": ref_name,
                    "node_id": "REF_1",
                    "url": f"https://api.github.com/repos/{owner}/{repo_name}/git/{ref_name}",
                    "object": {
                        "type": "commit",
                        "sha": sha,
                        "url": f"https://api.github.com/repos/{owner}/{repo_name}/commits/{sha}"
                    }
                })
                return

            if sub == "labels":
                label_name = body.get("name", "bug")
                color = body.get("color", "ededed")
                labels_dict = self.store.get_labels_dict(owner, repo_name)
                labels_dict[label_name] = {"name": label_name, "color": color}
                self.send_json(201, {"name": label_name, "color": color})
                return

            if len(parts) == 8 and parts[5] == "issues" and parts[7] == "comments":
                try:
                    issue_num = int(parts[6])
                    issues_dict = self.store.get_issues_dict(owner, repo_name)
                    iss = issues_dict.get(issue_num)
                    if not iss:
                        self.send_json(404, {"message": "Issue not found"})
                        return
                    comment_body = body.get("body", "")
                    comment = {"id": self.store.comment_id_counter, "body": comment_body}
                    self.store.comment_id_counter += 1
                    iss.setdefault("comments", []).append(comment)
                    self.send_json(201, comment)
                except ValueError:
                    self.send_json(404, {"message": "Issue not found"})
                return

            if sub == "issues":
                title = body.get("title", "")
                issue_body = body.get("body", "")
                labels = body.get("labels", [])
                repo_labels = self.store.get_labels_dict(owner, repo_name)
                for lbl in labels:
                    if lbl not in repo_labels:
                        self.send_json(422, {"message": "Validation Failed: unknown label"})
                        return
                
                issues_dict = self.store.get_issues_dict(owner, repo_name)
                num = self.store.inc_issue_counter(owner, repo_name) - 1
                if num < 1:
                    num = 1
                # If counter already used, find max + 1
                while num in issues_dict:
                    num += 1
                issue = {
                    "id": num,
                    "number": num,
                    "title": title,
                    "body": issue_body,
                    "state": "open",
                    "labels": [{"name": l} for l in labels],
                    "comments": []
                }
                issues_dict[num] = issue
                self.send_json(201, issue)
                return

            if sub == "pulls":
                pulls_dict = self.store.get_pulls_dict(owner, repo_name)
                num = self.store.inc_pr_counter(owner, repo_name) - 1
                if num < 1:
                    num = 1
                while num in pulls_dict:
                    num += 1
                title = body.get("title", "PR")
                head = body.get("head", "feature")
                base = body.get("base", "main")
                pr = {
                    "id": num,
                    "number": num,
                    "title": title,
                    "body": body.get("body", ""),
                    "state": "open",
                    "head": {"ref": head, "sha": "1111111111111111111111111111111111111111"},
                    "base": {"ref": base, "sha": "0000000000000000000000000000000000000000"},
                    "merged": False
                }
                pulls_dict[num] = pr
                self.send_json(201, pr)
                return

        self.send_json(404, {"message": "Not Found"})

    def do_PATCH(self) -> None:
        try:
            self._do_PATCH_inner()
        except Exception as e:
            traceback.print_exc()
            self.send_json(500, {"message": f"Internal Server Error: {e}"})

    def _do_PATCH_inner(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        content_length = int(self.headers.get("content-length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""
        
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            self.send_json(400, {"message": "Malformed JSON"})
            return

        parts = path.strip("/").split("/")
        if len(parts) >= 5 and parts[0] == "api" and parts[1] == "v3" and parts[2] == "repos":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            owner = parts[3]
            repo_name = parts[4].removesuffix(".git")
            
            if len(parts) == 5:
                repo = self.store.get_repo(owner, repo_name)
                if not repo:
                    self.send_json(404, {"message": "Repository not found"})
                    return
                if "description" in body:
                    repo["description"] = body["description"]
                self.send_json(200, repo)
                return

            if len(parts) == 7 and parts[5] == "issues":
                try:
                    issue_num = int(parts[6])
                    issues_dict = self.store.get_issues_dict(owner, repo_name)
                    iss = issues_dict.get(issue_num)
                    if not iss:
                        self.send_json(404, {"message": "Issue not found"})
                        return
                    if "state" in body:
                        iss["state"] = body["state"]
                    self.send_json(200, iss)
                except ValueError:
                    self.send_json(404, {"message": "Issue not found"})
                return

            if len(parts) == 7 and parts[5] == "pulls":
                try:
                    pr_num = int(parts[6])
                    pulls_dict = self.store.get_pulls_dict(owner, repo_name)
                    pr = pulls_dict.get(pr_num)
                    if not pr:
                        self.send_json(404, {"message": "Pull request not found"})
                        return
                    if "state" in body:
                        pr["state"] = body["state"]
                    if body.get("state") == "closed":
                        pr["merged"] = True
                    self.send_json(200, pr)
                except ValueError:
                    self.send_json(404, {"message": "Pull request not found"})
                return

        self.send_json(404, {"message": "Not Found"})

    def do_PUT(self) -> None:
        try:
            self._do_PUT_inner()
        except Exception as e:
            traceback.print_exc()
            self.send_json(500, {"message": f"Internal Server Error: {e}"})

    def _do_PUT_inner(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        content_length = int(self.headers.get("content-length", 0))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""
        
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            body = {}

        parts = path.strip("/").split("/")
        if len(parts) >= 5 and parts[0] == "api" and parts[1] == "v3" and parts[2] == "repos":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            owner = parts[3]
            repo_name = parts[4].removesuffix(".git")
            repo = self.store.get_repo(owner, repo_name)
            if not repo:
                repo = self.store.create_repo(owner, repo_name)

            if len(parts) >= 6 and parts[5] == "contents":
                file_path = "/".join(parts[6:])
                msg = body.get("message", "Update file")
                content_b64 = body.get("content", "")
                try:
                    content_bytes = base64.b64decode(content_b64)
                except Exception:
                    content_bytes = content_b64.encode("utf-8")
                
                sha = self.store.store_object(owner, repo_name, "blob", content_bytes)
                self.store.get_git_objects(owner, repo_name)[sha] = {
                    "type": "blob",
                    "data": content_bytes
                }
                files_dict = self.store.get_files_dict(owner, repo_name)
                files_dict[file_path] = {
                    "content": content_bytes,
                    "content_b64": content_b64,
                    "sha": sha
                }
                self.send_json(201, {
                    "content": {
                        "name": parts[-1],
                        "path": file_path,
                        "sha": sha,
                        "size": len(content_bytes),
                        "encoding": "base64",
                        "content": content_b64
                    },
                    "commit": {
                        "sha": "1111111111111111111111111111111111111111",
                        "message": msg
                    }
                })
                return

        self.send_json(404, {"message": "Not Found"})

    def do_DELETE(self) -> None:
        try:
            self._do_DELETE_inner()
        except Exception as e:
            traceback.print_exc()
            self.send_json(500, {"message": f"Internal Server Error: {e}"})

    def _do_DELETE_inner(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        parts = path.strip("/").split("/")
        if len(parts) >= 5 and parts[0] == "api" and parts[1] == "v3" and parts[2] == "repos":
            if not self.check_auth():
                self.send_json(401, {"message": "Requires authentication"})
                return
            owner = parts[3]
            repo_name = parts[4].removesuffix(".git")
            success = self.store.delete_repo(owner, repo_name)
            if success:
                self.send_response(204)
                self.end_headers()
            else:
                self.send_json(404, {"message": "Repository not found"})
            return

        self.send_json(404, {"message": "Not Found"})
