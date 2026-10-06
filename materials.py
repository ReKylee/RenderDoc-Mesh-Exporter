"""Per-draw texture snapshots and conservative, editable role suggestions."""
import hashlib
import json
import math
import re
import struct
import zlib
from pathlib import Path
from .geometry import enum_name


def read_png8(path):
    """Decode RenderDoc's non-interlaced 8-bit PNGs without a Pillow dependency."""
    data=bytes(path) if isinstance(path,(bytes,bytearray)) else Path(path).read_bytes()
    if data[:8]!=b'\x89PNG\r\n\x1a\n':raise ValueError('Not PNG')
    cursor=8;parts=[]
    while cursor+12<=len(data):
        size=struct.unpack_from('>I',data,cursor)[0]
        tag=data[cursor+4:cursor+8];chunk=data[cursor+8:cursor+8+size]
        if tag==b'IHDR':w,h,depth,kind,compression,filtering,interlace=struct.unpack('>IIBBBBB',chunk)
        if tag==b'IDAT':parts.append(chunk)
        cursor+=size+12
        if tag==b'IEND':break
    channels={0:1,2:3,4:2,6:4}.get(kind)
    if depth!=8 or interlace or not channels:raise ValueError('PNG layout not handled for role inspection')
    packed=zlib.decompress(b''.join(parts));stride=w*channels
    if len(packed)!=(stride+1)*h:raise ValueError('PNG scanline size mismatch')
    previous=bytearray(stride);pixels=bytearray()
    for row in range(h):
        offset=row*(stride+1);mode=packed[offset]
        line=bytearray(packed[offset+1:offset+1+stride])
        for i in range(stride):
            a=line[i-channels] if i>=channels else 0;b=previous[i];c=previous[i-channels] if i>=channels else 0
            if mode==0:predict=0
            elif mode==1:predict=a
            elif mode==2:predict=b
            elif mode==3:predict=(a+b)//2
            elif mode==4:
                p=a+b-c;pa,pb,pc=abs(p-a),abs(p-b),abs(p-c)
                predict=a if pa<=pb and pa<=pc else b if pb<=pc else c
            else:raise ValueError('PNG filter')
            line[i]=(line[i]+predict)&255
        for i in range(0,stride,channels):
            v=line[i:i+channels]
            if kind==6:pixels.extend(v)
            elif kind==2:pixels.extend(v+b'\xff')
            elif kind==4:pixels.extend((v[0],v[0],v[0],v[1]))
            else:pixels.extend((v[0],v[0],v[0],255))
        previous=line
    return w,h,pixels


def normal_evidence(path):
    try:
        w,h,p=read_png8(path)
        samples=[p[i*4:i*4+4] for i in range(0,w*h,max(1,w*h//2048))]
        opaque=[v for v in samples if v[3]>32]
        if len(opaque)<20:return False,{}
        unit=sum(abs(sum((x/127.5-1)**2 for x in v[:3])-1)<.22 for v in opaque)/len(opaque)
        blue=sum(v[2]>180 for v in opaque)/len(opaque)
        centered=sum(55<v[0]<205 and 55<v[1]<205 for v in opaque)/len(opaque)
        evidence={'unitVectorFraction':unit,'positiveZFraction':blue,'centeredXYFraction':centered}
        return unit>.85 and blue>.8 and centered>.7,evidence
    except ValueError:
        return False,{}


class TextureExporter:
    def __init__(self,controller,root,rd):
        self.controller=controller;self.root=Path(root);self.rd=rd
        self.folder=self.root/'textures';self.folder.mkdir(exist_ok=True)
        self.descriptions={str(t.resourceId):t for t in controller.GetTextures()}
        self.names={str(r.resourceId):r.name for r in controller.GetResources()}
        self.images={};self.evidence={}

    def export_draw(self,pipe,event,fetches):
        rd=self.rd;result=[]
        refl=pipe.GetShaderReflection(rd.ShaderStage.Pixel)
        if not refl:return result
        # Preserve co-occurrence for each draw. A group is a binding set, not a claim
        # that its first two resources must be albedo and normals.
        for used in pipe.GetReadOnlyResources(rd.ShaderStage.Pixel,True):
            desc=used.descriptor
            texture=self.descriptions.get(str(desc.resource))
            if texture is None:continue
            name=self.names.get(str(desc.resource),'')
            index=int(used.access.index)
            shader_name=refl.readOnlyResources[index].name if index<len(refl.readOnlyResources) else ''
            matched=[f for f in fetches if int(used.access.arrayElement) in f['arrays']]
            if not matched:matched=[None]
            # Every accessed array view/slice is represented. A PNG is the selected
            # mip/slice; DDS preserves the complete underlying texture separately.
            for slice_index in range(int(desc.firstSlice),int(desc.firstSlice)+max(1,int(desc.numSlices))):
                row={'resource':str(desc.resource),'resourceName':name,'shaderName':shader_name,
                     'arrayElement':int(used.access.arrayElement),'bindingIndex':index,
                     'format':desc.format.Name(),'components':int(desc.format.compCount),'mip':int(desc.firstMip),'slice':slice_index,
                     'width':max(1,int(texture.width)>>int(desc.firstMip)),
                     'height':max(1,int(texture.height)>>int(desc.firstMip)),'dimension':enum_name(texture.type)}
                temp=self.folder/'_pending.png'
                save=rd.TextureSave();save.resourceId=desc.resource;save.destType=rd.FileType.PNG
                save.mip=int(desc.firstMip);save.slice.sliceIndex=slice_index
                save.alpha=rd.AlphaMapping.Preserve;save.typeCast=desc.format.compType
                try:
                    status=self.controller.SaveTexture(save,str(temp))
                    if status!=rd.ResultCode.Succeeded or not temp.exists():raise RuntimeError(str(status))
                    blob=temp.read_bytes();digest=hashlib.sha256(blob).hexdigest()
                    path=self.folder/(digest[:24]+'.png')
                    if not path.exists():temp.replace(path)
                    else:temp.unlink()
                    row.update(image='textures/'+path.name,imageHash=digest)
                    inspect_normal = int(desc.format.compCount)>=3 and any(f and f['uvSet'] is not None for f in matched)
                    if digest not in self.evidence and inspect_normal:
                        self.evidence[digest]=normal_evidence(path)
                    looks_normal,evidence=self.evidence.get(digest,(False,{}))
                    row['normalEvidence']=evidence
                    self.images[digest]=str(path)
                    # PNG is a display conversion for HDR/depth/signed formats.
                    if enum_name(desc.format.compType) not in ('UNorm',) or int(desc.format.compByteWidth)>1:
                        dds=self.folder/(digest[:24]+'.dds')
                        if not dds.exists():
                            save.destType=rd.FileType.DDS;save.mip=-1;save.slice.sliceIndex=-1
                            dds_status=self.controller.SaveTexture(save,str(dds))
                            if dds_status!=rd.ResultCode.Succeeded:row['ddsError']=str(dds_status)
                        if dds.exists():row['dds']='textures/'+dds.name
                except Exception as e:
                    row['error']=str(e);looks_normal=False
                    if temp.exists():temp.unlink()
                for match in matched:
                    binding=dict(row)
                    if match:
                        binding.update(guestSlot=match['slot'],uvSet=match['uvSet'],sampleInstruction=match['instruction'],uvExpression=match['uvExpression'])
                    else:binding['uvSet']=None
                    role='unassigned';basis='group membership only'
                    label=(name+' '+shader_name).lower()
                    if re.search(r'normal|(?:^|[_ .-])nrm(?:[_ .-]|$)',label):role='normal';basis='resource/shader name'
                    elif re.search(r'albedo|base.?colou?r|diffuse',label):role='base_color';basis='resource/shader name'
                    elif looks_normal and binding['uvSet'] is not None:
                        role='normal_candidate';basis='source UV sample and RGB unit-normal pattern'
                    binding.update(role=role,roleEvidence=basis)
                    key=[binding.get('imageHash',str(desc.resource)),slice_index]
                    binding['key']=hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]
                    result.append(binding)
        # Suggest a color texture only among direct mesh-UV samples. Never assume
        # the ordering of RenderDoc's descriptor list implies a material role.
        suggest_base_color(result)
        return result


def suggest_base_color(bindings):
    # A two-channel mask/shadow lookup is not an RGB color map. Leave multiple
    # plausible color images unresolved instead of choosing descriptor order.
    candidates=[r for r in bindings if r.get('image') and r.get('uvSet') is not None
                and r.get('components',4)>=3 and r['role']=='unassigned']
    if not any(r['role']=='base_color' for r in bindings) and len({r['key'] for r in candidates})==1:
        for row in candidates:
            row['role']='base_color_candidate'
            row['roleEvidence']='sole RGB mesh-UV image in this draw; editable preview suggestion'


def choose_material(bindings,overrides=None):
    overrides=overrides or {}
    material={}
    for role in ('base_color','normal'):
        selected=overrides.get(role)
        row=next((b for b in bindings if b['key']==selected),None) if selected else None
        if role not in overrides:
            row=next((b for b in bindings if b['role']==role),None) or next((b for b in bindings if b['role']==role+'_candidate'),None)
        uv_key='base_uv' if role=='base_color' else 'normal_uv'
        uv=overrides.get(uv_key,row.get('uvSet') if row else None)
        if uv is None and role in overrides and row:uv=0;material[uv_key]=0
        if row and row.get('image') and uv is not None:
            material[role]=row['key']
            if uv_key in overrides:material[uv_key]=int(uv)
    for flag in ('flip_v','flip_normal_y'):
        if overrides.get(flag):material[flag]=True
    return material


def flip_normal_green(blob):
    width,height,pixels=read_png8(blob)
    pixels[1::4]=bytes(255-v for v in pixels[1::4])
    def chunk(tag,data):
        return struct.pack(">I",len(data))+tag+data+struct.pack(">I",zlib.crc32(tag+data)&0xffffffff)
    rows=b"".join(b"\0"+pixels[y*width*4:(y+1)*width*4] for y in range(height))
    return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",width,height,8,6,0,0,0))+chunk(b"IDAT",zlib.compress(rows))+chunk(b"IEND",b"")
