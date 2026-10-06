import importlib.util
import json
import math
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('mesh_exporter',ROOT/'__init__.py',submodule_search_locations=[str(ROOT)])
package=importlib.util.module_from_spec(spec);sys.modules['mesh_exporter']=package;spec.loader.exec_module(package)
from mesh_exporter.geometry import Mesh,swap32,triangles,export_triangles,decode_format,native_mesh,validate_mesh
from mesh_exporter.xenia import add,multiply,constant,vertex_uvs,bake_uv
from mesh_exporter.gltf import write_glb
from mesh_exporter.materials import choose_material

class Tests(unittest.TestCase):
    def test_byte_order_and_strip_parity(self):
        self.assertEqual(swap32(0x12345678,1),0x34127856)
        self.assertEqual(swap32(0x12345678,2),0x78563412)
        self.assertEqual(swap32(0x12345678,3),0x56781234)
        self.assertEqual(triangles([0,1,2,3], 'TriangleStrip'),[(0,1,2),(2,1,3)])
        self.assertEqual(triangles([0,1,1,2,3,None,4,5,6],'TriangleStrip'),[(1,2,3),(4,5,6)])
    def test_normalized_formats(self):
        fmt=NS(type='Regular',compType='SNorm',compCount=2,compByteWidth=1)
        self.assertEqual(decode_format(bytes([128,127]),0,fmt),(-1.0,1.0))
    def test_uv_affine(self):
        regions=[{'text':'vfetch_full r3.xy__, r0.x, vf0'},
                 {'text':'dp2add o1.x___, r3.yxxx, c41.yxxx, c41.wwww'},
                 {'text':'dp2add o1._y__, r3.yxxx, c42.yxxx, c42.wwww'}]
        fetches=[{'register':'r3','mask':'xy__'}]
        result=vertex_uvs(regions,fetches,{0:0},{41:(2,0,0,.25),42:(0,3,0,.5)})
        mesh=Mesh({'POSITION':[(0,0,0),(1,0,0)],'TEXCOORD_0':[(.1,.2),(.5,.4)]},[0,1],'LineList')
        index=bake_uv(mesh,result['o1'][:2])
        self.assertEqual(index,1)
        self.assertAlmostEqual(mesh.attributes['TEXCOORD_1'][0][0],.45)
        self.assertAlmostEqual(mesh.attributes['TEXCOORD_1'][0][1],1.1)
    def test_native_interleaved_signed_base_and_offsets(self):
        header=b'12345678';vertices=b''.join(struct.pack('<5f',x,y,z,u,v) for x,y,z,u,v in [(0,0,0,0,0),(1,0,0,1,0),(0,1,0,0,1)])
        index=b'ABCD'+struct.pack('<4H',99,1,2,3)
        class Controller:
            def GetBufferData(self,r,o,n):return {1:header+vertices,2:index}[r][o:o+n]
            def GetAPIProperties(self):return NS(pipelineType='D3D11')
        class Format:
            type='Regular';compType='Float';compByteWidth=4
            def __init__(self,n):self.compCount=n
            def Name(self):return 'float'+str(self.compCount)
        attrs=[NS(name='POSITION',used=True,genericEnabled=False,vertexBuffer=0,byteOffset=0,perInstance=False,format=Format(3)),
               NS(name='TEXCOORD0',used=True,genericEnabled=False,vertexBuffer=0,byteOffset=12,perInstance=False,format=Format(2))]
        p=NS(GetIBuffer=lambda:NS(byteStride=2,resourceId=2,byteOffset=4),GetRestartIndex=lambda:0,IsRestartEnabled=lambda:False,
             GetVertexInputs=lambda:attrs,GetVBuffers=lambda:[NS(resourceId=1,byteOffset=8,byteStride=20)],GetPrimitiveTopology=lambda:'TriangleList')
        a=NS(numIndices=3,indexOffset=1,baseVertex=-1,instanceOffset=0,vertexOffset=0)
        mesh=native_mesh(Controller(),p,a,True,0,{})
        self.assertEqual(mesh.indices,[0,1,2]);self.assertEqual(mesh.attributes['TEXCOORD_0'][1],(1.,0.));validate_mesh(mesh)
    def test_native_float2_position(self):
        raw=struct.pack('<6f',0,0,1,0,0,1)
        fmt=NS(type='Regular',compType='Float',compByteWidth=4,compCount=2,Name=lambda:'float2')
        attr=NS(name='POSITION',used=True,genericEnabled=False,vertexBuffer=0,byteOffset=0,perInstance=False,format=fmt)
        ctl=NS(GetBufferData=lambda r,o,n:raw[o:o+n])
        pipe=NS(GetVertexInputs=lambda:[attr],GetVBuffers=lambda:[NS(resourceId=1,byteOffset=0,byteStride=8)],GetPrimitiveTopology=lambda:'TriangleList')
        action=NS(numIndices=3,vertexOffset=0,instanceOffset=0,baseVertex=0)
        mesh=native_mesh(ctl,pipe,action,False,0,{})
        self.assertEqual(mesh.positions,[(0.,0.,0.),(1.,0.,0.),(0.,1.,0.)])
        validate_mesh(mesh)
    def test_instance_base_is_not_divided(self):
        positions=struct.pack('<9f',0,0,0,1,0,0,0,1,0)
        values=struct.pack('<6f',10,20,30,40,50,60)
        fmt=lambda n:NS(type='Regular',compType='Float',compByteWidth=4,compCount=n,Name=lambda:'float')
        attrs=[NS(name='POSITION',used=True,genericEnabled=False,vertexBuffer=0,byteOffset=0,perInstance=False,format=fmt(3)),
               NS(name='INSTANCE_VALUE',used=True,genericEnabled=False,vertexBuffer=1,byteOffset=0,perInstance=True,instanceRate=2,format=fmt(1))]
        ctl=NS(GetBufferData=lambda r,o,n:{1:positions,2:values}[r][o:o+n])
        pipe=NS(GetVertexInputs=lambda:attrs,GetVBuffers=lambda:[NS(resourceId=1,byteOffset=0,byteStride=12),NS(resourceId=2,byteOffset=0,byteStride=4)],GetPrimitiveTopology=lambda:'TriangleList')
        action=NS(numIndices=3,vertexOffset=0,instanceOffset=3,baseVertex=0)
        mesh=native_mesh(ctl,pipe,action,False,2,{})
        self.assertEqual(mesh.attributes['_INPUT_INSTANCE_VALUE'],[(50.,)]*3)
    def test_color_candidates_do_not_use_descriptor_order(self):
        from mesh_exporter.materials import suggest_base_color
        make=lambda key,components:{'key':key,'image':key+'.png','uvSet':0,'components':components,'role':'unassigned'}
        mask=make('mask',2); rgb=make('color',4)
        suggest_base_color([mask,rgb])
        self.assertEqual(mask['role'],'unassigned')
        self.assertEqual(rgb['role'],'base_color_candidate')
        ambiguous=[make('one',4),make('two',4)]
        suggest_base_color(ambiguous)
        self.assertTrue(all(r['role']=='unassigned' for r in ambiguous))
    def test_glb_has_uv_normals_and_clean_triangle_topology(self):
        positions=[(0.,0.,0.),(1.,0.,0.),(0.,1.,0.),(10.,0.,0.),(11.,0.,0.),(10.,1.,0.)]
        mesh=Mesh({'POSITION':positions, 'TEXCOORD_0':[(0.,0.)]*6, 'NORMAL':[(0.,0.,1.)]*6},
                  [0,1,2,2,3,3,4,5],'TriangleStrip',{'space':'source'})
        self.assertEqual(export_triangles(mesh),[(0,1,2),(4,3,5)])
        with tempfile.TemporaryDirectory() as d:
            b=write_glb(Path(d)/'mesh.glb',mesh,[],{},d)
        self.assertEqual(struct.unpack_from('<III',b), (0x46546c67,2,len(b)))
        n=struct.unpack_from('<I',b,12)[0];doc=json.loads(b[20:20+n]);p=doc['meshes'][0]['primitives'][0]
        self.assertEqual(p['mode'],4)
        self.assertEqual(doc['accessors'][p['indices']]['count'],6)
        self.assertEqual(doc['meshes'][0]['extras']['sourceTopology'],'TriangleStrip')
        self.assertEqual(doc['meshes'][0]['extras']['exportTopology'],'TriangleList')
        self.assertIn('TEXCOORD_0',p['attributes']);self.assertIn('NORMAL',p['attributes'])
    def test_roles_do_not_assume_descriptor_order(self):
        rows=[{'key':'shadow','role':'unassigned','image':'a.png','uvSet':None},{'key':'color','role':'base_color_candidate','image':'b.png','uvSet':1}]
        self.assertEqual(choose_material(rows),{'base_color':'color'})
        self.assertEqual(choose_material(rows,{'base_color':''}),{})

if __name__=='__main__':unittest.main()
