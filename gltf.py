"""Portable GLB/OBJ files with original input topology retained in metadata."""
import json
import struct
from pathlib import Path
from .geometry import segments, export_triangles, lines
from .materials import flip_normal_green


def write_glb(path,mesh,bindings,material,root):
    path=Path(path);root=Path(root)
    binary=bytearray();views=[];accessors=[]
    def view(data,target=None):
        binary.extend(b'\0'*((-len(binary))%4));offset=len(binary);binary.extend(data)
        item={'buffer':0,'byteOffset':offset,'byteLength':len(data)}
        if target:item['target']=target
        views.append(item);return len(views)-1
    def accessor(values,components,component_type=5126,target=34962,bounds=False):
        fmt={5126:'f',5125:'I'}[component_type]
        data=b''.join(struct.pack('<'+fmt*components,*v) for v in values)
        item={'bufferView':view(data,target),'componentType':component_type,'count':len(values),
              'type':{1:'SCALAR',2:'VEC2',3:'VEC3',4:'VEC4'}[components]}
        if bounds:
            item['min']=[min(v[i] for v in values) for i in range(components)]
            item['max']=[max(v[i] for v in values) for i in range(components)]
        accessors.append(item);return len(accessors)-1
    attrs={}
    for name,values in mesh.attributes.items():
        if material.get('flip_v') and name.startswith('TEXCOORD_'):values=[(u,1-v) for u,v in values]
        if name=='TANGENT' and len(values[0])!=4:name='_SOURCE_TANGENT'
        attrs[name]=accessor(values,len(values[0]),bounds=name=='POSITION')
    images=[];textures=[];image_indices={}
    pbr={'baseColorFactor':[1,1,1,1],'metallicFactor':0,'roughnessFactor':.85}
    mat={'name':'captured texture group','pbrMetallicRoughness':pbr,'doubleSided':True,
         'extras':{'roleAssignments':material,'note':'Editable reconstruction; original game shader is not a glTF material.'}}
    for role in ('base_color','normal'):
        key=material.get(role)
        row=next((r for r in bindings if r['key']==key),None)
        uv=material.get('base_uv' if role=='base_color' else 'normal_uv',row.get('uvSet') if row else None)
        if not row or uv is None or 'TEXCOORD_%d'%int(uv) not in attrs:continue
        file=row['image']
        image_key=(file,role=='normal' and bool(material.get('flip_normal_y')))
        if image_key not in image_indices:
            image_bytes=(root/file).read_bytes()
            if role=='normal' and material.get('flip_normal_y'):image_bytes=flip_normal_green(image_bytes)
            images.append({'bufferView':view(image_bytes),'mimeType':'image/png','name':row.get('resourceName') or row['resource']})
            textures.append({'source':len(images)-1,'sampler':0})
            image_indices[image_key]=len(textures)-1
        tex={'index':image_indices[image_key],'texCoord':int(uv)}
        if role=='base_color':pbr['baseColorTexture']=tex
        elif role=='normal':mat['normalTexture']=tex
    surface_topologies=('TriangleList','TriangleStrip','TriangleFan','TriangleList_Adj')
    if mesh.topology in surface_topologies:
        # DCC-facing files always use explicit triangles. Keeping strips/fans in
        # glTF preserves degenerate stitch indices as visible loose/long edges in
        # importers such as Blender even though the GPU rasterizes no triangle.
        mode=4
        faces=export_triangles(mesh)
        batches=[[v for face in faces for v in face]]
    else:
        mode={'PointList':0,'LineList':1,'LineLoop':2,'LineStrip':3}.get(mesh.topology)
        if mode is not None:
            batches=[s for s in segments(mesh.indices) if len(s)>={0:1,1:2,2:2,3:2}[mode]]
            if mode==1:
                batches=[b[:len(b)//2*2] for b in batches]
        elif mesh.topology in ('LineList_Adj','LineStrip_Adj'):
            mode=1;batches=[[v for f in lines(mesh.indices,mesh.topology) for v in f]]
        else:
            mode=0;batches=[sorted({i for i in mesh.indices if i is not None})]
    prims=[{'attributes':attrs,'indices':accessor([(i,) for i in batch],1,5125,34963),
            'mode':mode,'material':0} for batch in batches if batch]
    if not prims:raise ValueError('No glTF primitives')
    doc={'asset':{'version':'2.0','generator':'RenderDoc Mesh Exporter'},'buffers':[{'byteLength':len(binary)}],
         'bufferViews':views,'accessors':accessors,'materials':[mat],
         'meshes':[{'name':path.parent.name,'primitives':prims,'extras':{'sourceTopology':mesh.topology,'exportTopology':'TriangleList' if mesh.topology in surface_topologies else mesh.topology,'space':mesh.metadata['space']}}],
         'nodes':[{'mesh':0,'name':path.parent.name}],'scenes':[{'nodes':[0]}],'scene':0}
    if images:
        doc.update(images=images,textures=textures,samplers=[{'wrapS':10497,'wrapT':10497,'magFilter':9729,'minFilter':9987}])
    js=json.dumps(doc,separators=(',',':'),ensure_ascii=True).encode();js+=b' '*((-len(js))%4)
    binary.extend(b'\0'*((-len(binary))%4))
    blob=struct.pack('<III',0x46546c67,2,12+8+len(js)+8+len(binary))+struct.pack('<II',len(js),0x4e4f534a)+js+struct.pack('<II',len(binary),0x004e4942)+binary
    path.write_bytes(blob)
    return blob


def write_obj(path,mesh,bindings,material,root):
    path=Path(path)
    base=next((r for r in bindings if r['key']==material.get('base_color')),None)
    uv=mesh.attributes.get('TEXCOORD_%d'%int(material.get('base_uv',base.get('uvSet') or 0))) if base else mesh.attributes.get('TEXCOORD_0')
    if uv and material.get('flip_v'):uv=[(u,1-v) for u,v in uv]
    normals=mesh.attributes.get('NORMAL')
    with path.open('w',encoding='utf8',newline='\n') as f:
        f.write('# Source-space geometry. OBJ expands strips; mesh.json retains original input topology.\n')
        f.write('mtllib mesh.mtl\no '+path.parent.name+'\nusemtl captured\n')
        for p in mesh.positions:f.write('v %.9g %.9g %.9g\n'%tuple(p))
        if uv:
            # OBJ has a bottom-left V convention; glTF and the gallery use top-left.
            for u,v in uv:f.write('vt %.9g %.9g\n'%(u,1-v))
        if normals:
            for n in normals:f.write('vn %.9g %.9g %.9g\n'%tuple(n))
        def vertex(i):
            i+=1
            return '%d/%d/%d'%(i,i,i) if uv and normals else '%d/%d'%(i,i) if uv else '%d//%d'%(i,i) if normals else str(i)
        for tri in export_triangles(mesh):f.write('f '+' '.join(vertex(i) for i in tri)+'\n')
        for a,b in lines(mesh.indices,mesh.topology):f.write('l %d %d\n'%(a+1,b+1))
    mtl='newmtl captured\nKd 1 1 1\n'
    if base:mtl+='map_Kd ../../'+base['image']+'\n'
    # Normal maps are kept in GLB; OBJ/MTL bump conventions are inconsistent.
    path.with_suffix('.mtl').write_text(mtl,encoding='utf8')


def write_uv_svg(path,mesh,uv_key):
    uv=mesh.attributes[uv_key];lo=[min(v[i] for v in uv) for i in range(2)];hi=[max(v[i] for v in uv) for i in range(2)]
    scale=min(460/max(hi[i]-lo[i],1e-8) for i in range(2))
    def point(v):return (256+(v[0]-(hi[0]+lo[0])/2)*scale,256+(v[1]-(hi[1]+lo[1])/2)*scale)
    paths=[]
    for face in export_triangles(mesh):
        pts=[point(uv[i]) for i in face]
        paths.append('M%.3f %.3fL%.3f %.3fL%.3f %.3fZ'%tuple(x for p in pts for x in p))
    text='<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512"><rect width="512" height="512" fill="#181b21"/><path d="%s" fill="none" stroke="#c6d9f3" stroke-width="0.5"/><text x="12" y="20" fill="white" font-family="sans-serif">%s | UV bounds %.3g, %.3g to %.3g, %.3g</text></svg>'%(''.join(paths),uv_key,*lo,*hi)
    Path(path).write_text(text,encoding='utf8')
