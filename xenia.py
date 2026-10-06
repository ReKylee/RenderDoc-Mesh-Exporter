"""Discover Xenia DXBC fetches from embedded guest instructions, not capture IDs."""
import math
import re
import struct
from .geometry import Mesh, UnsupportedMesh, add_normals, enum_name, read_indices, swap32

LANES = 'xyzw'


def shader_regions(raw, disassembly):
    if raw[:4] != b'DXBC' or len(raw) < 32:
        return []
    declarations = sum(line.strip().startswith('dcl_') for line in disassembly.splitlines())
    host = [(int(m[1]), m[2]) for line in disassembly.splitlines()
            if (m := re.match(r'\s*(\d+):\s*(.*)', line))]
    comments = []
    count = struct.unpack_from('<I', raw, 28)[0]
    if 32+4*count > len(raw): raise ValueError('Truncated DXBC table')
    for i in range(count):
        offset = struct.unpack_from('<I', raw, 32+4*i)[0]
        if offset+8 > len(raw): raise ValueError('Truncated DXBC chunk')
        if raw[offset:offset+4] not in (b'SHDR', b'SHEX'): continue
        size = struct.unpack_from('<I', raw, offset+4)[0]
        data = raw[offset+8:offset+8+size]
        words = struct.unpack('<%dI' % (len(data)//4), data)
        k, ordinal = 2, 0
        while k < len(words):
            token = words[k]
            op = token & 2047
            length = words[k+1] if op == 53 else (token >> 24) & 127
            if not length or k+length > len(words): raise ValueError('Invalid DXBC instruction length')
            if op == 53 and token >> 11 == 0:
                text = data[(k+2)*4:(k+length)*4].split(b'\0', 1)[0].decode('utf-8', 'replace')
                comments.append((ordinal-declarations, text))
            k += length
            ordinal += 1
    result = []
    for i, (start, text) in enumerate(comments):
        end = comments[i+1][0] if i+1 < len(comments) else float('inf')
        result.append({'text':text, 'host':'\n'.join(s for n,s in host if start < n < end)})
    return result


def read_block(controller, pipe, stage, name):
    refl = pipe.GetShaderReflection(stage)
    for i, block in enumerate(refl.constantBlocks):
        if block.name == name:
            desc = pipe.GetConstantBlock(stage, i, 0).descriptor
            raw = bytes(controller.GetBufferData(desc.resource, desc.byteOffset, block.byteSize))
            if len(raw) < int(block.byteSize): raise ValueError('Short constant buffer: '+name)
            return raw, block
    raise UnsupportedMesh('Missing '+name)


def host_cb(disassembly, binding):
    m = re.search(r'dcl_constantbuffer\s+(CB\d+)\[\d+\].*reg='+str(binding)+r'\b', disassembly)
    if not m: raise UnsupportedMesh('Unrecognized Xenia constant buffer declaration')
    return m[1]


def analyze_vertex(regions, disassembly):
    fetchcb = host_cb(disassembly, 3)
    fetches, stride = [], None
    for region in regions:
        line = region['text'].splitlines()[0]
        m = re.match(r'vfetch_(full|mini)\s+(r\d+)(?:\.([xyzw01_]+))?,\s*(.*)', line)
        if not m: continue
        params = dict(re.findall(r'(\w+)=([\w.+-]+)', m[4]))
        if m[1] == 'full':
            stride = 4*int(params.get('Stride', '0'))
            if not re.match(r'r0\.x,', m[4]):
                raise UnsupportedMesh('Xenia vfetch uses a computed index other than r0.x')
        if not stride: raise UnsupportedMesh('Missing vfetch stride')
        cb = re.search(r'\b(?:and|mov)\s+r\d+\.[xyzw],\s*'+fetchcb+r'\[(\d+)\]\.([xyzw]),', region['host'])
        if not cb: raise UnsupportedMesh('Cannot locate guest vfetch constant in translated shader')
        dword = int(cb[1])*4+LANES.index(cb[2])
        if dword & 1: raise UnsupportedMesh('Expected vertex-fetch address word')
        fetches.append({'constant':dword//2, 'stride':stride, 'offset':4*int(params.get('Offset','0')),
                        'format':params.get('DataFormat'), 'params':params, 'register':m[2],
                        'mask':m[3] or 'xyzw', 'instruction':region['text']})
    if not fetches: raise UnsupportedMesh('Xenia guest fetch instructions are absent')
    positions = [i for i,f in enumerate(fetches) if f['format']=='FMT_32_32_32_FLOAT' and f['mask']=='xyz1']
    if len(positions) != 1:
        raise UnsupportedMesh('Position fetch is ambiguous; a shader-specific decoder is required')
    return fetches, positions[0]


def decode_fetch(raw, offset, fetch, endian):
    name = fetch['format']
    params = fetch['params']
    formats = {'FMT_32_FLOAT':(4,1,True), 'FMT_32_32_FLOAT':(4,2,True),
               'FMT_32_32_32_FLOAT':(4,3,True), 'FMT_32_32_32_32_FLOAT':(4,4,True),
               'FMT_16_16_FLOAT':(2,2,True), 'FMT_16_16_16_16_FLOAT':(2,4,True),
               'FMT_8_8_8_8':(1,4,False), 'FMT_16_16':(2,2,False),
               'FMT_16_16_16_16':(2,4,False), 'FMT_32':(4,1,False),
               'FMT_32_32':(4,2,False)}
    if name not in formats: raise UnsupportedMesh('Unsupported guest format '+str(name))
    width, count, floating = formats[name]
    size = max(4, width*count)
    if offset<0 or offset+size > len(raw): raise ValueError('Vertex fetch outside captured range')
    data = b''.join(struct.pack('<I',swap32(struct.unpack_from('<I',raw,offset+j)[0],endian)) for j in range(0,size,4))
    if floating:
        vals = struct.unpack('<'+('f' if width==4 else 'e')*count, data[:width*count])
    else:
        signed = params.get('Signed') == 'true'
        vals = [int.from_bytes(data[j*width:(j+1)*width],'little',signed=signed) for j in range(count)]
        if params.get('NumFormat') != 'integer':
            denom = (1 << (width*8-int(signed)))-1
            vals = [max(-1.0,v/denom) if signed else v/denom for v in vals]
    scale = 2.0**int(params.get('ExpAdjust','0'))
    return tuple(float(v)*scale for v in vals)


def constant_values(controller, pipe, stage, regions):
    data, block = read_block(controller,pipe,stage,'xe_float_cbuffer')
    refs = sorted({int(s) for r in regions for s in re.findall(r'\bc(\d+)\b',r['text'])})
    if len(data) == 4096:
        refs = list(range(256))
    if len(refs)*16 != len(data):
        raise UnsupportedMesh('Cannot associate compressed guest float constants with registers')
    return {i:struct.unpack_from('<4f',data,n*16) for n,i in enumerate(refs)}


# An affine expression (UV set, U coefficient, V coefficient, constant).
# Unknown values are None. This deliberately does not emulate arbitrary shaders.
def constant(value): return (-1,0.,0.,float(value))


def add(a,b):
    if a is None or b is None: return None
    if a[0]>=0 and b[0]>=0 and a[0]!=b[0]: return None
    return (max(a[0],b[0]), a[1]+b[1], a[2]+b[2], a[3]+b[3])


def multiply(a,b):
    if a is None or b is None: return None
    if a[0]>=0 and b[0]>=0: return None
    if a[0]<0: a,b=b,a
    return (a[0],a[1]*b[3],a[2]*b[3],a[3]*b[3])


def operand(text,regs,consts):
    text=text.strip()
    negative=text.startswith('-')
    if negative:text=text[1:]
    m=re.fullmatch(r'([ro]\d+|c\d+)(?:\.([xyzw]+))?',text)
    if not m:return [None]*4
    values = [constant(v) for v in consts.get(int(m[1][1:]),())] if m[1].startswith('c') else regs.get(m[1],[None]*4)
    if len(values)!=4:return [None]*4
    swizzle=m[2] or 'xyzw'
    if len(swizzle)<4:swizzle=swizzle.ljust(4,swizzle[-1])
    values=[values[LANES.index(c)] for c in swizzle]
    return [multiply(v,constant(-1)) for v in values] if negative else values


def assign(regs,dest,values):
    m=re.fullmatch(r'([ro]\d+)(?:\.([xyzw01_]+))?',dest.strip())
    if not m:return
    mask=m[2] or 'xyzw'
    old=regs.setdefault(m[1],[None]*4)
    for i,c in enumerate(mask):
        if c=='_':continue
        old[i]=constant(int(c)) if c in '01' else values[i]


def trace_alu(text,regs,consts):
    # Xenos co-issued scalar operations have separate destination masks.
    for line in text.splitlines():
        line=line.strip().lstrip('+').strip()
        m=re.match(r'(\w+)\s+([^,]+),\s*(.*)',line)
        if not m:continue
        op,dest,rest=m.groups()
        src=[operand(s,regs,consts) for s in rest.split(',')]
        values=[None]*4
        if op in ('mov','max') and src and (op=='mov' or len(src)>1 and src[0]==src[1]):
            values=src[0]
        elif op=='add' and len(src)==2:
            values=[add(a,b) for a,b in zip(*src)]
        elif op=='mul' and len(src)==2:
            values=[multiply(a,b) for a,b in zip(*src)]
        elif op=='mad' and len(src)==3:
            values=[add(multiply(a,b),c) for a,b,c in zip(*src)]
        elif op in ('dp2add','dp3','dp4') and len(src)>=2:
            n={'dp2add':2,'dp3':3,'dp4':4}[op]
            val=constant(0)
            for i in range(n):val=add(val,multiply(src[0][i],src[1][i]))
            if op=='dp2add':val=add(val,src[2][0])
            values=[val]*4
        assign(regs,dest,values)


def vertex_uvs(regions,fetches,uv_for_fetch,consts):
    regs={}; cursor=0
    for r in regions:
        if r['text'].startswith('vfetch_'):
            f=fetches[cursor]; uv=uv_for_fetch.get(cursor);cursor+=1
            value=[None]*4
            if uv is not None:value=[(uv,1.,0.,0.),(uv,0.,1.,0.),constant(0),constant(1)]
            old=regs.setdefault(f['register'],[None]*4)
            for lane,c in enumerate(f['mask']):
                if c=='_':continue
                old[lane]=constant(int(c)) if c in '01' else value[LANES.index(c)]
        else:trace_alu(r['text'],regs,consts)
    return {k:v for k,v in regs.items() if k.startswith('o')}


def bake_uv(mesh,pair):
    if any(x is None for x in pair) or all(x[0]<0 for x in pair):return None
    coords=[]
    for i in range(len(mesh.positions)):
        v=[]
        for source,a,b,c in pair:
            uv=mesh.attributes.get('TEXCOORD_%d'%source)
            v.append(c if source<0 else uv[i][0]*a+uv[i][1]*b+c)
        coords.append(tuple(v))
    if not all(math.isfinite(x) for v in coords for x in v):return None
    n=0
    while 'TEXCOORD_%d'%n in mesh.attributes:
        if mesh.attributes['TEXCOORD_%d'%n]==coords:return n
        n+=1
    mesh.attributes['TEXCOORD_%d'%n]=coords
    return n


def texture_fetches(controller,pipe,stage,mesh,regions,disassembly,varyings):
    try:
        cb=host_cb(disassembly,4)
        data,_=read_block(controller,pipe,stage,'xe_descriptor_indices_cbuffer')
    except UnsupportedMesh:
        return []
    try:consts=constant_values(controller,pipe,stage,regions)
    except UnsupportedMesh:consts={}
    regs={'r'+key[1:]:list(v) for key,v in varyings.items()}
    result=[]
    for region in regions:
        text=region['text']
        m=re.match(r'tfetch\w*\s+([^,]+),\s*([^,]+),\s*tf(\d+)\b',text)
        if not m:
            trace_alu(text,regs,consts)
            continue
        pair=operand(m[2],regs,consts)[:2]
        uv_set=bake_uv(mesh,pair)
        assignments={}
        arrays=[]
        for line in region['host'].splitlines():
            load=re.match(r'mov\s+(r\d+\.[xyzw]),\s*'+cb+r'\[(\d+)\]\.([xyzw])',line)
            if load:
                off=int(load[2])*16+LANES.index(load[3])*4
                if off+4<=len(data):assignments[load[1]]=struct.unpack_from('<I',data,off)[0]
            if line.startswith('sample'):
                sample=re.search(r'T\d+\[(r\d+\.[xyzw])\s*\+\s*0\]',line)
                if sample and sample[1] in assignments:arrays.append(assignments[sample[1]])
        result.append({'slot':int(m[3]),'arrays':sorted(set(arrays)),'uvSet':uv_set,
                       'uvExpression':pair,'instruction':text,'destination':m[1]})
        assign(regs,m[1],[None]*4)
    return result


def source_mesh(controller,pipe,action,indexed,stage,regions,disassembly):
    fetches,pos_index=analyze_vertex(regions,disassembly)
    raw_ids,raw_indices,index_width=read_indices(controller,pipe,action,indexed)
    sysdata,sysblock=read_block(controller,pipe,stage,'xe_system_cbuffer')
    fields={v.name:int(v.byteOffset) for v in sysblock.variables}
    def integer(name,default=None):
        if name not in fields:
            if default is not None:return default
            raise UnsupportedMesh('Missing Xenia system field '+name)
        return struct.unpack_from('<I',sysdata,fields[name])[0]
    closing=integer('xe_line_loop_closing_index')
    endian=integer('xe_vertex_index_endian')
    offset=integer('xe_vertex_index_offset')
    lower=integer('xe_vertex_index_min_max')
    upper=struct.unpack_from('<I',sysdata,fields['xe_vertex_index_min_max']+4)[0]
    base=int(action.baseVertex) if indexed else 0
    ids=[]
    for value in raw_ids:
        if value is None:ids.append(None);continue
        value=(value+base)&0xffffffff
        value=0 if value==closing else value
        value=(swap32(value,endian)+offset)&0x00ffffff
        ids.append(min(upper,max(lower,value)))
    used=sorted({i for i in ids if i is not None})
    if not used:raise UnsupportedMesh('No source vertices')
    fdata,_=read_block(controller,pipe,stage,'xe_fetch_cbuffer')
    resource_list=pipe.GetReadOnlyResources(stage,True)
    if integer('xe_flags')&1:
        resource_list=pipe.GetReadWriteResources(stage,True)
    refl=pipe.GetShaderReflection(stage)
    refs=refl.readWriteResources if integer('xe_flags')&1 else refl.readOnlyResources
    ref_index=next((i for i,r in enumerate(refs) if r.name.startswith('xe_shared_memory_')),None)
    bound=next((u for u in resource_list if int(u.access.index)==ref_index),None)
    if bound is None:raise UnsupportedMesh('Xenia shared-memory binding unavailable')
    shared=bound.descriptor
    attrs,streams,descriptions,uv_for_fetch={}, {}, [], {}
    uvcount=0;directions=[]
    for i,f in enumerate(fetches):
        key=(f['constant'],f['stride'])
        if key not in streams:
            d0,d1=struct.unpack_from('<2I',fdata,f['constant']*8)
            if d0&3!=3:raise UnsupportedMesh('Non-vertex fetch descriptor')
            address=d0&~3
            size=((d1>>2)&0xffffff)*4
            need=(max(used)+1)*f['stride']
            if need>size or need>256*1024*1024:raise UnsupportedMesh('Vertex fetch range exceeds buffer or safety limit')
            raw=bytes(controller.GetBufferData(shared.resource,int(shared.byteOffset)+address,need))
            if len(raw)!=need:raise ValueError('Short Xenia source-buffer read')
            streams[key]=(raw,d1&3)
        raw,byteorder=streams[key]
        values=[decode_fetch(raw,j*f['stride']+f['offset'],f,byteorder) for j in used]
        label='_FETCH_%d_%d'%(f['constant'],f['offset'])
        if label in attrs:label+='_%d'%i
        attrs[label]=values
        f['attribute']=label
        if i==pos_index:attrs['POSITION']=[v[:3] for v in values]
        elif len(values[0])==3 and f['format'].endswith('FLOAT'):directions.append(label)
        if len(values[0])==2:
            uv_for_fetch[i]=uvcount
            attrs['TEXCOORD_%d'%uvcount]=values
            uvcount+=1
        descriptions.append(dict(f,byteOrder=byteorder))
    remap={v:i for i,v in enumerate(used)}
    mesh=Mesh(attrs,[None if i is None else remap[i] for i in ids],enum_name(pipe.GetPrimitiveTopology()),
              {'adapter':'Xenia DXBC guest vfetch','space':'pre-transform guest source','sourceVertexIds':used,
               'sourceIndices':ids,'fetches':descriptions,'rawUVSets':uvcount,
               'indexTransform':{'closing':closing,'endian':endian,'offset':offset,'min':lower,'max':upper},
               '_raw_streams':{'vertices_fc%d_s%d.bin'%key:raw for key,(raw,_) in streams.items()}})
    if raw_indices:mesh.metadata['_raw_streams']['submitted_indices.bin']=raw_indices
    add_normals(mesh,directions)
    try:
        consts=constant_values(controller,pipe,stage,regions)
        varyings=vertex_uvs(regions,fetches,uv_for_fetch,consts)
    except UnsupportedMesh as error:
        varyings={};mesh.metadata.setdefault('warnings',[]).append(str(error))
    return mesh,varyings
