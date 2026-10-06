"""Source vertex decoding and primitive assembly; independent of the RenderDoc UI."""
import math
import re
import struct
from dataclasses import dataclass, field


class UnsupportedMesh(ValueError):
    pass


@dataclass
class Mesh:
    attributes: dict
    indices: list
    topology: str
    metadata: dict = field(default_factory=dict)

    @property
    def positions(self):
        return self.attributes['POSITION']


def enum_name(value):
    return str(value).split('.')[-1]


def swap32(value, endian):
    value &= 0xffffffff
    if endian not in (0, 1, 2, 3):
        raise UnsupportedMesh('Unknown byte order %s' % endian)
    if endian in (1, 2):
        value = ((value & 0x00ff00ff) << 8) | ((value & 0xff00ff00) >> 8)
    if endian in (2, 3):
        value = ((value << 16) | (value >> 16)) & 0xffffffff
    return value


def normalize(v):
    n = math.sqrt(sum(x*x for x in v))
    return tuple(x/n for x in v) if n > 1e-30 else (0.0, 0.0, 1.0)


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def segments(indices):
    start = 0
    for i, value in enumerate(indices):
        if value is None:
            if i > start:
                yield indices[start:i]
            start = i+1
    if start < len(indices):
        yield indices[start:]


def triangles(indices, topology):
    """Expand topology to faces while advancing strip parity across connector degenerates."""
    result = []
    for ids in segments(indices):
        if topology == 'TriangleList':
            it = (ids[i:i+3] for i in range(0, len(ids)-2, 3))
        elif topology == 'TriangleStrip':
            it = ((ids[i-1], ids[i-2], ids[i]) if i & 1 else
                  (ids[i-2], ids[i-1], ids[i]) for i in range(2, len(ids)))
        elif topology == 'TriangleFan':
            it = ((ids[0], ids[i-1], ids[i]) for i in range(2, len(ids)))
        elif topology == 'TriangleList_Adj':
            it = ((ids[i], ids[i+2], ids[i+4]) for i in range(0, len(ids)-5, 6))
        else:
            it = ()
        result.extend(tuple(f) for f in it if len(set(f)) == 3)
    return result


def export_triangles(mesh):
    """Return only real surface triangles for DCC export.

    Strip/fan connector degenerates are deliberately absent here. The capture's
    original topology and index stream remain on the Mesh and in metadata.
    """
    result = []
    for face in triangles(mesh.indices, mesh.topology):
        pa, pb, pc = (mesh.positions[i] for i in face)
        ab = tuple(pb[i]-pa[i] for i in range(3))
        ac = tuple(pc[i]-pa[i] for i in range(3))
        bc = tuple(pc[i]-pb[i] for i in range(3))
        normal = cross(ab, ac)
        area2 = sum(x*x for x in normal)
        edge2 = max(sum(x*x for x in edge) for edge in (ab, ac, bc))
        # Relative test: reject exact / numerically collapsed connector faces
        # without imposing a world-space minimum triangle size.
        if edge2 > 0.0 and area2 > edge2*edge2*1e-24:
            result.append(face)
    return result


def lines(indices, topology):
    out = []
    for ids in segments(indices):
        if topology == 'LineList_Adj':
            out.extend((ids[i+1], ids[i+2]) for i in range(0, len(ids)-3, 4))
        elif topology == 'LineStrip_Adj':
            out.extend(zip(ids[1:-2], ids[2:-1]))
        elif topology == 'LineList':
            out.extend(zip(ids[::2], ids[1::2]))
        elif topology == 'LineStrip':
            out.extend(zip(ids[:-1], ids[1:]))
        elif topology == 'LineLoop' and ids:
            out.extend(zip(ids, ids[1:]+ids[:1]))
    return [x for x in out if x[0] != x[1]]


def geometric_normals(mesh):
    out = [[0.0, 0.0, 0.0] for _ in mesh.positions]
    for a, b, c in triangles(mesh.indices, mesh.topology):
        pa, pb, pc = (mesh.positions[i] for i in (a, b, c))
        n = cross([pb[i]-pa[i] for i in range(3)], [pc[i]-pa[i] for i in range(3)])
        for j in (a, b, c):
            for k in range(3):
                out[j][k] += n[k]
    return [normalize(v) for v in out]


def add_normals(mesh, candidates=()):
    if 'NORMAL' in mesh.attributes:
        mesh.attributes['NORMAL'] = [normalize(v[:3]) for v in mesh.attributes['NORMAL']]
        mesh.metadata['normalSource'] = 'vertex input semantic'
        return
    if not triangles(mesh.indices, mesh.topology):
        return
    geometric = geometric_normals(mesh)
    # Xenia has no input semantics. Select only a unit direction agreeing with the
    # surface, not an arbitrary float3 at a familiar offset. Keep all raw attributes.
    used = sorted({i for i in mesh.indices if i is not None})
    scores = []
    for name in candidates:
        values = mesh.attributes[name]
        unit = sum(abs(sum(x*x for x in values[i])-1.0) < 0.25 for i in used)/len(used)
        score = sum(abs(sum(a*b for a, b in zip(normalize(values[i]), geometric[i]))) for i in used)/len(used)
        if unit > .85 and score > .7:
            scores.append((score, name))
    scores.sort(reverse=True)
    if scores and (len(scores) == 1 or scores[0][0]-scores[1][0] > .1):
        score, name = scores[0]
        mesh.attributes['NORMAL'] = [normalize(v) for v in mesh.attributes[name]]
        mesh.metadata['normalSource'] = {'attribute': name, 'method': 'unit direction / geometric agreement', 'score': score}
    else:
        mesh.attributes['NORMAL'] = geometric
        mesh.metadata['normalSource'] = 'generated area-weighted source-space normals'


def decode_format(raw, offset, fmt):
    kind, comp = enum_name(fmt.type), enum_name(fmt.compType)
    count, width = int(fmt.compCount), int(fmt.compByteWidth)
    if kind == 'Regular':
        if width not in (1, 2, 4, 8) or not 1 <= count <= 4:
            raise UnsupportedMesh('Unsupported regular vertex format')
        code = {2: 'e', 4: 'f', 8: 'd'}.get(width) if comp in ('Float', 'Double') else None
        if code:
            values = list(struct.unpack_from('<'+code*count, raw, offset))
        else:
            signed = comp in ('SInt', 'SNorm', 'SScaled')
            if comp not in ('SInt', 'SNorm', 'SScaled', 'UInt', 'UNorm', 'UScaled'):
                raise UnsupportedMesh('Unsupported vertex component type '+comp)
            values = [int.from_bytes(raw[offset+i*width:offset+(i+1)*width], 'little', signed=signed) for i in range(count)]
            if comp == 'UNorm':
                values = [v/((1 << (width*8))-1) for v in values]
            elif comp == 'SNorm':
                values = [max(-1, v/((1 << (width*8-1))-1)) for v in values]
    elif kind == 'R10G10B10A2':
        bits = struct.unpack_from('<I', raw, offset)[0]
        values = []
        shift = 0
        for size in (10, 10, 10, 2):
            v = (bits >> shift) & ((1 << size)-1)
            if comp in ('SNorm', 'SInt') and v & (1 << (size-1)):
                v -= 1 << size
            if comp == 'UNorm': v /= (1 << size)-1
            if comp == 'SNorm': v = max(-1, v/((1 << (size-1))-1))
            values.append(v)
            shift += size
    else:
        raise UnsupportedMesh('Unsupported packed vertex format '+kind)
    if getattr(fmt, 'BGRAOrder', lambda: False)() and len(values) >= 3:
        values[0], values[2] = values[2], values[0]
    return tuple(float(v) for v in values)


def semantic(name, overrides):
    if name in overrides:
        label=overrides[name]
        if not isinstance(label,str) or not re.fullmatch(r'(?:POSITION|NORMAL|TANGENT|TEXCOORD_\d+|COLOR_\d+|_[A-Za-z0-9_]+)',label):
            raise UnsupportedMesh('Invalid attribute semantic override: '+repr(label))
        return label
    n = re.sub('[^a-z0-9]', '', name.lower())
    for pattern, label in [(r'(?:in|a|vsin)?(?:position|pos)(?:0)?$', 'POSITION'),
                           (r'(?:in|a)?normal0?$', 'NORMAL'),
                           (r'(?:in|a)?tangent0?$', 'TANGENT')]:
        if re.fullmatch(pattern, n): return label
    for pattern, label in [(r'(?:in|a)?(?:texcoord|uv)(\d*)$', 'TEXCOORD'),
                           (r'(?:in|a)?(?:color|colour)(\d*)$', 'COLOR')]:
        m = re.fullmatch(pattern, n)
        if m: return label+'_'+(m[1] or '0')
    return '_INPUT_'+re.sub('[^A-Za-z0-9_]', '_', name).upper()


def read_indices(controller, pipe, action, indexed):
    count = int(action.numIndices)
    if count <= 0: return [], b'', 0
    if not indexed:
        return list(range(int(action.vertexOffset), int(action.vertexOffset)+count)), b'', 0
    ib = pipe.GetIBuffer()
    width = int(ib.byteStride)
    if width not in (1, 2, 4):
        raise UnsupportedMesh('Unknown index width %d' % width)
    size = count*width
    raw = bytes(controller.GetBufferData(ib.resourceId, int(ib.byteOffset)+int(action.indexOffset)*width, size))
    if len(raw) != size: raise ValueError('Short index-buffer read')
    values = list(struct.unpack('<'+{1:'B',2:'H',4:'I'}[width]*count, raw))
    restart = int(pipe.GetRestartIndex())
    if pipe.IsRestartEnabled():
        # D3D reports zero for this query; its enabled strip cut is all-ones.
        if restart == 0 and enum_name(controller.GetAPIProperties().pipelineType) in ('D3D11', 'D3D12'):
            restart = (1 << (width*8))-1
        values = [None if v == restart else v for v in values]
    return values, raw, width


def native_mesh(controller, pipe, action, indexed, instance, overrides):
    ids, raw_indices, index_width = read_indices(controller, pipe, action, indexed)
    base = int(action.baseVertex) if indexed else 0
    ids = [None if v is None else v+base for v in ids]
    used = sorted({v for v in ids if v is not None})
    if not used: raise UnsupportedMesh('No submitted vertices')
    if used[0] < 0: raise UnsupportedMesh('Negative effective vertex ID')
    inputs = pipe.GetVertexInputs()
    buffers = pipe.GetVBuffers()
    attributes, info, cache = {}, [], {}
    for attr in inputs:
        label = semantic(attr.name, overrides)
        if not attr.used and label != 'POSITION': continue
        try:
            if attr.genericEnabled:
                raise UnsupportedMesh('Constant vertex input requires explicit decoding')
            vb = buffers[int(attr.vertexBuffer)]
            stride = int(vb.byteStride)
            if attr.perInstance:
                rate = int(attr.instanceRate)
                # The base instance offsets the result, not the value divided by the step rate.
                fetch_ids = [int(action.instanceOffset)+(instance//rate if rate else 0)]*len(used)
            else:
                fetch_ids = used
            width = int(attr.format.compByteWidth)*int(attr.format.compCount)
            if not width: width = 4
            start = int(vb.byteOffset)+min(fetch_ids)*stride+int(attr.byteOffset)
            size = (max(fetch_ids)-min(fetch_ids))*stride+width
            if size > 256*1024*1024:
                raise UnsupportedMesh('Sparse vertex span exceeds 256 MiB; explicit layout extraction required')
            key = (str(vb.resourceId), start, size)
            if key not in cache:
                cache[key] = bytes(controller.GetBufferData(vb.resourceId, start, size))
            raw = cache[key]
            if len(raw) != size: raise ValueError('Short vertex-buffer read')
            vals = [decode_format(raw, (i-min(fetch_ids))*stride, attr.format) for i in fetch_ids]
            width = len(vals[0])
            if (label in ('POSITION', 'NORMAL') and width != 3) or (label.startswith('TEXCOORD_') and width != 2):
                attributes['_SOURCE_'+label] = vals
            if label == 'POSITION': vals = [tuple(v[:3])+(0.0,)*max(0,3-len(v)) for v in vals]
            if label == 'NORMAL' and width < 3:
                label = '_SOURCE_NORMAL'
                info.append({'name': attr.name, 'note': 'Packed/short normal needs shader decoding; derive geometric normals instead.'})
            elif label == 'NORMAL': vals = [v[:3] for v in vals]
            if label.startswith('TEXCOORD_'): vals = [tuple(v[:2])+(0.0,)*max(0,2-len(v)) for v in vals]
            if label.startswith('COLOR_') and width not in (3,4): label = '_SOURCE_'+label
            if not all(math.isfinite(x) for v in vals for x in v):
                raise UnsupportedMesh('Non-finite input '+attr.name)
            attributes[label] = vals
            info.append({'name': attr.name, 'semantic': label, 'format': attr.format.Name(), 'perInstance': bool(attr.perInstance)})
        except Exception as e:
            if label == 'POSITION': raise
            info.append({'name': attr.name, 'error': str(e)})
    if 'POSITION' not in attributes:
        raise UnsupportedMesh('No named position input. Set attribute_map for this shader; vertex pulling needs an adapter.')
    mapping = {v:i for i,v in enumerate(used)}
    mesh = Mesh(attributes, [None if v is None else mapping[v] for v in ids], enum_name(pipe.GetPrimitiveTopology()),
                {'space':'vertex shader input', 'sourceVertexIds': used, 'sourceIndices': ids,
                 'inputAttributes': info, 'indexByteWidth': index_width, 'adapter':'native vertex inputs'})
    add_normals(mesh)
    return mesh


def validate_mesh(mesh):
    count = len(mesh.positions)
    if not count: raise ValueError('Empty mesh')
    for name, values in mesh.attributes.items():
        if len(values) != count: raise ValueError('Attribute count mismatch: '+name)
        if not values or not 1 <= len(values[0]) <= 4: raise ValueError('Invalid attribute width: '+name)
        if any(not math.isfinite(x) for v in values for x in v): raise ValueError('Non-finite '+name)
    if any(v is not None and not 0 <= v < count for v in mesh.indices): raise ValueError('Index out of bounds')
