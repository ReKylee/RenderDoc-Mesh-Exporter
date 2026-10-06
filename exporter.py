"""Replay-thread export entry point shared by the extension and regression tests."""
import base64
import hashlib
import json
import re
import shutil
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from .geometry import Mesh, UnsupportedMesh, add_normals, enum_name, native_mesh, export_triangles, lines, validate_mesh
from . import xenia
from .materials import TextureExporter, choose_material
from .gltf import write_glb, write_obj, write_uv_svg


@dataclass
class Options:
    minimum_vertices: int = 10
    event: int = 0
    mode: str = 'source'
    textures: bool = True
    overrides: dict = field(default_factory=dict)
    cancelled: threading.Event = field(default_factory=threading.Event)


def walk(actions):
    for action in actions:
        if action.children:
            yield from walk(action.children)
        else:
            yield action


def post_shader_mesh(controller,action,instance,rd):
    mf=controller.GetPostVSData(instance,0,rd.MeshDataStage.VSOut)
    if not mf.vertexResourceId or not mf.vertexByteStride or not mf.vertexByteSize:
        raise UnsupportedMesh(mf.status or 'No post-vertex-shader output')
    if enum_name(mf.format.compType)!='Float' or mf.format.compByteWidth!=4:
        raise UnsupportedMesh('Non-float post-shader position output')
    raw=bytes(controller.GetBufferData(mf.vertexResourceId,mf.vertexByteOffset,mf.vertexByteSize))
    stride=int(mf.vertexByteStride);count=len(raw)//stride
    values=[struct.unpack_from('<%df'%int(mf.format.compCount),raw,i*stride) for i in range(count)]
    pos=[v[:3] for v in values]
    if mf.indexByteStride:
        width=int(mf.indexByteStride)
        data=bytes(controller.GetBufferData(mf.indexResourceId,mf.indexByteOffset,int(mf.numIndices)*width))
        ids=list(struct.unpack('<'+{2:'H',4:'I'}[width]*(len(data)//width),data))
        restart=int(mf.restartIndex)&((1<<(8*width))-1)
        ids=[None if mf.allowRestart and v==restart else v+int(mf.baseVertex) for v in ids]
    else:ids=list(range(int(mf.numIndices)))
    mesh=Mesh({'POSITION':pos},ids,enum_name(mf.topology),{'space':'post-VS clip/projected coordinates; NOT object space',
                 'adapter':'RenderDoc post-shader','warnings':['Projected output is for diagnostics. Shader-input UV and normal data are not mixed into this remapped vertex stream.']})
    add_normals(mesh)
    return mesh


def dump_json(path,data):
    Path(path).write_text(json.dumps(data,indent=2,ensure_ascii=True,allow_nan=False),encoding='utf8')


def export_capture(controller,capture_name,output,options=None,progress=None):
    import renderdoc as rd
    options=options or Options();progress=progress or (lambda text:None)
    root=Path(output).resolve()
    if root.exists() and any(root.iterdir()):raise ValueError('Choose an empty output directory; existing files will not be overwritten.')
    root.mkdir(parents=True,exist_ok=True);(root/'meshes').mkdir(exist_ok=True)
    records=[];report=[];shader_cache={};groups={};blobs={}
    tex=TextureExporter(controller,root,rd) if options.textures else None
    actions=[a for a in walk(controller.GetRootActions()) if a.flags&rd.ActionFlags.Drawcall and (not options.event or int(a.eventId)==options.event)]
    if not actions:raise ValueError('No draw calls in the selected scope')
    def shader(pipe,stage):
        refl=pipe.GetShaderReflection(stage)
        if not refl:return None,[],''
        key=(str(refl.resourceId),enum_name(stage)) if hasattr(refl,'resourceId') else (str(pipe.GetShader(stage)),enum_name(stage))
        if key not in shader_cache:
            dis=controller.DisassembleShader(pipe.GetGraphicsPipelineObject(),refl,'')
            shader_cache[key]=(xenia.shader_regions(bytes(refl.rawBytes),dis),dis)
        regions,dis=shader_cache[key]
        return refl,regions,dis
    for ordinal,action in enumerate(actions,1):
        if options.cancelled.is_set():break
        eid=int(action.eventId)
        progress('Draw %d/%d, event %d'%(ordinal,len(actions),eid))
        controller.SetFrameEvent(eid,True);pipe=controller.GetPipelineState()
        for instance in range(max(1,int(action.numInstances))):
            if options.cancelled.is_set():break
            row={'eventId':eid,'instance':instance,'status':'pending'}
            try:
                if action.numIndices<=0 or action.numInstances==0:
                    row.update(status='skipped',reason='empty draw');report.append(row);continue
                indexed=bool(action.flags&rd.ActionFlags.Indexed)
                vs=pipe.GetShaderReflection(rd.ShaderStage.Vertex)
                is_xenia=vs and any(c.name=='xe_fetch_cbuffer' for c in vs.constantBlocks)
                varyings={};fetches=[]
                if options.mode=='post_vs':
                    mesh=post_shader_mesh(controller,action,instance,rd)
                elif is_xenia:
                    _,regions,dis=shader(pipe,rd.ShaderStage.Vertex)
                    mesh,varyings=xenia.source_mesh(controller,pipe,action,indexed,rd.ShaderStage.Vertex,regions,dis)
                else:
                    mesh=native_mesh(controller,pipe,action,indexed,instance,options.overrides.get('attribute_map',{}))
                if len({v for v in mesh.indices if v is not None})<options.minimum_vertices:
                    row.update(status='skipped',reason='fewer than %d referenced vertices'%options.minimum_vertices);report.append(row);continue
                validate_mesh(mesh)
                if is_xenia and options.mode=='source':
                    _,regions,dis=shader(pipe,rd.ShaderStage.Pixel)
                    if regions:fetches=xenia.texture_fetches(controller,pipe,rd.ShaderStage.Pixel,mesh,regions,dis,varyings)
                bindings=tex.export_draw(pipe,eid,fetches) if tex else []
                if not is_xenia and 'TEXCOORD_0' in mesh.attributes:
                    for b in bindings:
                        if b['role'] in ('normal','base_color') and b.get('uvSet') is None:
                            b['uvSet']=0;b['uvEvidence']='UV0 preview default; shader coordinate association not inferred'
                if not is_xenia and 'TEXCOORD_0' in mesh.attributes and len(bindings)==1 and bindings[0].get('image'):
                    b=bindings[0]
                    if b['role']=='unassigned':
                        b.update(role='base_color_candidate',uvSet=0,roleEvidence='single bound texture with UV0; editable, not shader-traced')
                signature=sorted({(b.get('imageHash',b['resource']),b['mip'],b['slice']) for b in bindings})
                group_id=hashlib.sha256(json.dumps(signature).encode()).hexdigest()[:16] if signature else 'no_textures'
                overrides=options.overrides.get('groups',{}).get(group_id,{})
                material=choose_material(bindings,overrides)
                name='event_%06d_instance_%03d'%(eid,instance)
                folder=root/'meshes'/name;folder.mkdir()
                for filename,data in mesh.metadata.pop('_raw_streams',{}).items():(folder/filename).write_bytes(data)
                for key,values in mesh.attributes.items():
                    data=b''.join(struct.pack('<%df'%len(v),*v) for v in values)
                    (folder/(key.lower()+'.f32le.bin')).write_bytes(data)
                source_indices=mesh.metadata.get('sourceIndices',mesh.indices)
                (folder/'source_indices.u32le.bin').write_bytes(b''.join(struct.pack('<I',0xffffffff if v is None else v) for v in source_indices))
                blob=write_glb(folder/'mesh.glb',mesh,bindings,material,root)
                write_obj(folder/'mesh.obj',mesh,bindings,material,root)
                uv_sets=sorted(k for k in mesh.attributes if k.startswith('TEXCOORD_'))
                for key in uv_sets:write_uv_svg(folder/(key.lower()+'.svg'),mesh,key)
                geometry_hash=hashlib.sha256(b''.join(struct.pack('<3f',*p) for p in mesh.positions)+json.dumps([mesh.indices,mesh.topology]).encode()).hexdigest()
                meta={'capture':capture_name,'eventId':eid,'instance':instance,'vertexCount':len(mesh.positions),
                      'referencedVertexCount':len({i for i in mesh.indices if i is not None}),'topology':mesh.topology,
                      'exportTopology':'TriangleList' if mesh.topology in ('TriangleList','TriangleStrip','TriangleFan','TriangleList_Adj') else mesh.topology,
                      'triangleCount':len(export_triangles(mesh)),'lineCount':len(lines(mesh.indices,mesh.topology)),
                      'attributes':{k:len(v[0]) for k,v in mesh.attributes.items()},'uvSets':uv_sets,
                      'textures':bindings,'textureGroup':group_id,'material':material,'geometryHash':geometry_hash,
                      'source':mesh.metadata,'indices':mesh.indices}
                dump_json(folder/'mesh.json',meta)
                record={k:v for k,v in meta.items() if k not in ('source','indices','attributes')}
                record['attributeHash']=hashlib.sha256(json.dumps(mesh.attributes,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                record.update(id=name,adapter=mesh.metadata.get('adapter','unknown'),path='meshes/'+name+'/mesh.glb',metadata='meshes/'+name+'/mesh.json',
                              obj='meshes/'+name+'/mesh.obj',space=mesh.metadata['space'],normalSource=mesh.metadata.get('normalSource'))
                records.append(record);blobs[name]=blob
                groups.setdefault(group_id,{'id':group_id,'models':[],'images':signature})['models'].append(name)
                row.update(status='exported',id=name,vertices=len(mesh.positions),textures=len(bindings),uvSets=len(uv_sets))
            except UnsupportedMesh as e:row.update(status='unsupported',reason=str(e))
            except Exception as e:
                import traceback
                row.update(status='error',reason=str(e),traceback=traceback.format_exc())
            report.append(row)
    # Hide duplicate *geometry and material*, not distinct texture variants of a mesh.
    seen={}
    for r in records:
        key=(r['geometryHash'],r['attributeHash'],r['textureGroup'],json.dumps(r['material'],sort_keys=True),
             tuple((b['key'],b.get('uvSet')) for b in r['textures']))
        r['duplicateOf']=seen.get(key)
        seen.setdefault(key,r['id'])
    result={'capture':capture_name,'models':records,'groups':groups,'report':report,'cancelled':options.cancelled.is_set(),
            'summary':{'draws':len(actions),'models':len(records),'unsupported':sum(r['status']=='unsupported' for r in report),
                       'errors':sum(r['status']=='error' for r in report),'skipped':sum(r['status']=='skipped' for r in report),
                       'textureGroups':len([g for g in groups if g!='no_textures']),
                       'uniqueImages':len(tex.images) if tex else 0}}
    from .gallery import build_gallery
    progress('Building offline Three.js gallery')
    build_gallery(root,result,blobs)
    return result
