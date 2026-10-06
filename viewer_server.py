"""Local viewer server. Filesystem layout is the organization database."""
from __future__ import annotations
import argparse, json, os, re, shutil, threading, webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

MODEL_JSON = "mesh.json"
FS_LOCK = threading.RLock()

def safe_rel(root: Path, value: str) -> Path:
    value = str(value or "").replace("\\", "/").strip("/")
    path = (root / value).resolve()
    root = root.resolve()
    if path != root and root not in path.parents:
        raise ValueError("Path escapes meshes folder")
    return path

def clean_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "").strip()).rstrip(". ")
    if not value or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", value):
        raise ValueError("Invalid Windows file name")
    return value

def metadata(folder: Path):
    path = folder / MODEL_JSON
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def model_id(meta: dict) -> str:
    return "event_%06d_instance_%03d" % (int(meta["eventId"]), int(meta.get("instance", 0)))

def model_files(folder: Path):
    files = [p for p in folder.iterdir() if p.is_file()]
    glb = next((p for p in files if p.suffix.lower() == ".glb"), None)
    obj = next((p for p in files if p.suffix.lower() == ".obj"), None)
    mtl = next((p for p in files if p.suffix.lower() == ".mtl"), None)
    return glb, obj, mtl

def inspect_model(meshes: Path, folder: Path):
    meta = metadata(folder)
    if not meta:
        return None
    glb, obj, mtl = model_files(folder)
    if not glb:
        return None
    parent = folder.parent.relative_to(meshes).as_posix()
    if parent == ".":
        parent = ""
    rel = folder.relative_to(meshes).as_posix()
    return {
        "id": model_id(meta),
        "folderPath": parent,
        "folderName": folder.name,
        "displayName": folder.name,
        "modelPath": rel,
        "path": "meshes/" + rel + "/" + glb.name,
        "obj": "meshes/" + rel + "/" + obj.name if obj else None,
        "metadata": "meshes/" + rel + "/" + MODEL_JSON,
    }

def _scan_tree_unlocked(meshes: Path):
    models = []
    def scan(folder: Path, rel: str, name: str):
        node = {"name": name, "path": rel, "models": 0, "children": []}
        for child in sorted((p for p in folder.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
            model = inspect_model(meshes, child)
            if model:
                models.append(model)
                node["models"] += 1
            else:
                child_rel = child.relative_to(meshes).as_posix()
                node["children"].append(scan(child, child_rel, child.name))
        return node
    return {"tree": scan(meshes, "", "meshes"), "models": models}

def scan_tree(meshes: Path):
    with FS_LOCK:
        return _scan_tree_unlocked(meshes)

def rewrite_obj(obj: Path, model_name: str, mtl_name: str | None):
    if not obj or not obj.is_file():
        return
    text = obj.read_text(encoding="utf-8", errors="replace")
    if mtl_name:
        text = re.sub(r"(?m)^mtllib\s+.*$", "mtllib " + mtl_name, text)
    text = re.sub(r"(?m)^o\s+.*$", "o " + model_name, text)
    obj.write_text(text, encoding="utf-8", newline="\n")

def rewrite_mtl(root: Path, folder: Path, mtl: Path | None):
    if not mtl or not mtl.is_file():
        return
    texture_root = root / "textures"
    rel_textures = os.path.relpath(texture_root, folder).replace("\\", "/")
    lines = []
    commands = ("map_Kd", "map_Ka", "map_Ks", "map_Bump", "bump", "norm")
    for line in mtl.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.lstrip()
        command = next((c for c in commands if stripped.lower().startswith(c.lower() + " ")), None)
        if command:
            parts = line.split()
            if len(parts) >= 2:
                basename = Path(parts[-1].replace("\\", "/")).name
                if (texture_root / basename).exists():
                    parts[-1] = rel_textures + "/" + basename
                    line = " ".join(parts)
        lines.append(line)
    mtl.write_text("\n".join(lines) + "\n", encoding="utf-8")

def fix_model(root: Path, folder: Path, rename_to: str | None = None):
    glb, obj, mtl = model_files(folder)
    if rename_to:
        renamed = {}
        for path in (glb, obj, mtl):
            if path:
                target = folder / (rename_to + path.suffix.lower())
                if target != path:
                    if target.exists():
                        raise FileExistsError(target.name)
                    path.rename(target)
                renamed[path.suffix.lower()] = target
        glb, obj, mtl = renamed.get(".glb", glb), renamed.get(".obj", obj), renamed.get(".mtl", mtl)
    rewrite_obj(obj, rename_to or folder.name, mtl.name if mtl else None)
    rewrite_mtl(root, folder, mtl)

class Handler(SimpleHTTPRequestHandler):
    server_version = "RenderDocMeshViewer/1"
    def log_message(self, format, *args):
        pass

    @property
    def export_root(self):
        return self.server.export_root

    @property
    def meshes(self):
        return self.export_root / "meshes"

    def json_response(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 1024 * 1024:
            raise ValueError("Request too large")
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/api/tree":
            try:
                return self.json_response(scan_tree(self.meshes))
            except Exception as exc:
                return self.json_response({"error": str(exc)}, 500)
        if route == "/":
            self.send_response(302)
            self.send_header("Location", "/viewer/index.html")
            self.end_headers()
            return
        return super().do_GET()

    def do_POST(self):
        route = urlparse(self.path).path
        try:
            with FS_LOCK:
                data = self.read_json()
                if route == "/api/folders":
                    parent = safe_rel(self.meshes, data.get("parent", ""))
                    if not parent.is_dir():
                        raise ValueError("Parent folder does not exist")
                    target = parent / clean_name(data.get("name"))
                    target.mkdir()
                elif route == "/api/folders/rename":
                    source = safe_rel(self.meshes, data.get("path", ""))
                    if source == self.meshes or not source.is_dir() or inspect_model(self.meshes, source):
                        raise ValueError("Select an organization folder")
                    target = source.with_name(clean_name(data.get("name")))
                    if target.exists():
                        raise FileExistsError(target.name)
                    source.rename(target)
                elif route == "/api/models/move":
                    target = safe_rel(self.meshes, data.get("target", ""))
                    if not target.is_dir() or inspect_model(self.meshes, target):
                        raise ValueError("Target is not an organization folder")
                    sources = [safe_rel(self.meshes, p) for p in data.get("paths", [])]
                    for source in sources:
                        if not inspect_model(self.meshes, source):
                            raise ValueError("Not a model folder: " + source.name)
                        destination = target / source.name
                        if destination.exists():
                            raise FileExistsError(destination.name)
                    for source in sources:
                        destination = target / source.name
                        shutil.move(str(source), str(destination))
                        fix_model(self.export_root, destination)
                elif route == "/api/models/rename":
                    source = safe_rel(self.meshes, data.get("path", ""))
                    if not inspect_model(self.meshes, source):
                        raise ValueError("Not a model folder")
                    name = clean_name(data.get("name"))
                    target = source.with_name(name)
                    if target.exists():
                        raise FileExistsError(target.name)
                    source.rename(target)
                    fix_model(self.export_root, target, name)
                else:
                    return self.json_response({"error": "Unknown API route"}, 404)
                payload = scan_tree(self.meshes)
            return self.json_response(payload)
        except Exception as exc:
            return self.json_response({"error": str(exc)}, 400)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("export", nargs="?", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    root = Path(args.export).resolve()
    if not (root / "meshes").is_dir() or not (root / "viewer" / "index.html").is_file():
        raise SystemExit("This is not a Mesh Exporter output folder: " + str(root))
    handler = lambda *a, **kw: Handler(*a, directory=str(root), **kw)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    server.export_root = root
    url = "http://127.0.0.1:%d/" % server.server_port
    print("Mesh viewer:", url)
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.15, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
