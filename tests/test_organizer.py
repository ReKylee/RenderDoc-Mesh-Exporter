import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from viewer_server import Handler

class OrganizerServerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'viewer').mkdir()
        (self.root/'viewer'/'index.html').write_text('ok', encoding='utf8')
        (self.root/'textures').mkdir()
        (self.root/'textures'/'albedo.png').write_bytes(b'png')
        model = self.root/'meshes'/'event_000123_instance_000'
        model.mkdir(parents=True)
        (model/'mesh.json').write_text(json.dumps({'eventId':123,'instance':0}), encoding='utf8')
        (model/'mesh.glb').write_bytes(b'glb')
        (model/'mesh.obj').write_text('mtllib mesh.mtl\no old\n', encoding='utf8')
        (model/'mesh.mtl').write_text('newmtl captured\nmap_Kd ../../textures/albedo.png\n', encoding='utf8')
        handler = lambda *a, **kw: Handler(*a, directory=str(self.root), **kw)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.export_root = self.root
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = Request(self.url+path, data=data, headers={'Content-Type':'application/json'})
        with urlopen(req) as response:
            return json.loads(response.read())

    def test_move_and_rename_are_filesystem_backed(self):
        tree = self.request('/api/tree')
        self.assertEqual(tree['models'][0]['modelPath'], 'event_000123_instance_000')

        self.request('/api/folders', {'parent':'', 'name':'Characters'})
        moved = self.request('/api/models/move', {
            'paths':['event_000123_instance_000'], 'target':'Characters'})
        self.assertEqual(moved['models'][0]['modelPath'], 'Characters/event_000123_instance_000')

        renamed = self.request('/api/models/rename', {
            'path':'Characters/event_000123_instance_000', 'name':'Hero'})
        self.assertEqual(renamed['models'][0]['modelPath'], 'Characters/Hero')
        folder = self.root/'meshes'/'Characters'/'Hero'
        self.assertTrue((folder/'Hero.glb').is_file())
        self.assertTrue((folder/'Hero.obj').is_file())
        self.assertTrue((folder/'Hero.mtl').is_file())
        self.assertIn('mtllib Hero.mtl', (folder/'Hero.obj').read_text())
        self.assertIn('../../../textures/albedo.png', (folder/'Hero.mtl').read_text())

        final = self.request('/api/folders/rename', {'path':'Characters', 'name':'Cast'})
        self.assertEqual(final['models'][0]['modelPath'], 'Cast/Hero')
        self.assertTrue((self.root/'meshes'/'Cast'/'Hero').is_dir())

if __name__ == '__main__':
    unittest.main()
