"""Validate exported files and optionally compare Xenia source buffers to a prior export."""
import argparse
import collections
import json
from pathlib import Path
import struct


def validate(root, reference=None):
    text=(root/'viewer'/'catalog.js').read_text(encoding='utf8')
    prefix='window.MeshData={};window.MeshCatalog='
    if not text.startswith(prefix) or ';window.MeshImages=' not in text:
        raise ValueError('Invalid viewer catalog')
    manifest=json.loads(text[len(prefix):].split(';window.MeshImages=',1)[0])
    report=manifest.get('report',[])
    errors = []; compared = 0; images = set()
    for row in report:
        if row['status'] == 'error': errors.append('Event %s: %s' % (row['eventId'], row.get('reason')))
    for model in manifest['models']:
        name = model['id']
        try:
            folder = root/'meshes'/name
            meta = json.loads((folder/'mesh.json').read_text(encoding='utf8'))
            count = meta['vertexCount']
            assert count > 0
            assert all(i is None or 0 <= i < count for i in meta['indices'])
            for attr, width in meta['attributes'].items():
                assert (folder/(attr.lower()+'.f32le.bin')).stat().st_size == count * width * 4
            for binding in meta['textures']:
                assert not binding.get('error'), binding.get('error')
                assert not binding.get('ddsError'), binding.get('ddsError')
                if binding.get('image'):
                    image = root/binding['image']; data = image.read_bytes()
                    assert data.startswith(b'\x89PNG\r\n\x1a\n')
                    assert struct.unpack_from('>II', data, 16) == (binding['width'], binding['height'])
                    images.add(binding['image'])
                if binding.get('dds'): assert (root/binding['dds']).is_file()
            blob = (folder/'mesh.glb').read_bytes()
            assert struct.unpack_from('<III', blob) == (0x46546c67, 2, len(blob))
            length, kind = struct.unpack_from('<II', blob, 12)
            assert kind == 0x4e4f534a
            doc = json.loads(blob[20:20+length])
            bin_length, bin_kind = struct.unpack_from('<II', blob, 20+length)
            assert bin_kind == 0x004e4942 and bin_length+28+length == len(blob)
            for view in doc['bufferViews']:
                assert view.get('byteOffset', 0) + view['byteLength'] <= doc['buffers'][0]['byteLength']
            for accessor in doc['accessors']:
                view = doc['bufferViews'][accessor['bufferView']]
                width = {5126:4, 5125:4, 5123:2}[accessor['componentType']] * {'SCALAR':1,'VEC2':2,'VEC3':3,'VEC4':4}[accessor['type']]
                assert accessor['count'] * width <= view['byteLength']
            for primitive in doc['meshes'][0]['primitives']:
                attrs = primitive['attributes']
                for attr, index in attrs.items():
                    assert doc['accessors'][index]['count'] == count
                    if attr in ('POSITION', 'NORMAL'): assert doc['accessors'][index]['type'] == 'VEC3'
                    if attr.startswith('TEXCOORD_'): assert doc['accessors'][index]['type'] == 'VEC2'
                material = doc['materials'][primitive['material']]
                for texture in (material.get('normalTexture'), material.get('pbrMetallicRoughness', {}).get('baseColorTexture')):
                    if texture: assert 'TEXCOORD_%d' % texture.get('texCoord', 0) in attrs
            if reference and model['adapter'] == 'Xenia DXBC guest vfetch':
                old = reference/('event_%04d' % model['eventId'])
                assert (folder/'source_indices.u32le.bin').read_bytes() == (old/'logical_vertex_ids.u32le.bin').read_bytes(), 'Source index order differs'
                positions = (old/'positions.f32le.bin').read_bytes()
                expected = b''.join(positions[i*12:i*12+12] for i in meta['source']['sourceVertexIds'])
                assert (folder/'position.f32le.bin').read_bytes() == expected, 'Source positions differ'
                compared += 1
        except Exception as error:
            errors.append('%s: %s' % (name, error or type(error).__name__))
    result = {'summary': manifest['summary'], 'sourceComparisons': compared,
              'adapters': dict(collections.Counter(m['adapter'] for m in manifest['models'])),
              'uvModels': sum(bool(m['uvSets']) for m in manifest['models']),
              'baseColorModels': sum('base_color' in m['material'] for m in manifest['models']),
              'normalMappedModels': sum('normal' in m['material'] for m in manifest['models']),
              'pngImages': len(images), 'errors': errors}
    (root/'validation').mkdir(exist_ok=True)
    (root/'validation/export_test.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--reference', type=Path)
    args = parser.parse_args()
    result = validate(args.output, args.reference)
    print(json.dumps(result, indent=2))
    raise SystemExit(bool(result['errors']))
