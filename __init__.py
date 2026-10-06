"""RenderDoc Extension API entry point."""
import os
import subprocess
import threading
from pathlib import Path

panels=[]


def register(version,pyrenderdoc):
    import qrenderdoc as qrd
    pyrenderdoc.Extensions().RegisterWindowMenu(qrd.WindowMenu.Tools,['Mesh Exporter','Export meshes and textures...'],open_window)


def unregister():
    for state in list(panels):
        state['cancelled'].set()
        if not state['closed']:
            state['helper'].CloseToplevelWidget(state['window'])
    panels.clear()


def open_window(ctx,data):
    import qrenderdoc as qrd
    from .exporter import Options,export_capture
    helper=ctx.Extensions().GetMiniQtHelper()
    state={'cancelled':threading.Event(),'closed':False,'busy':False,'gallery':None,'helper':helper}
    def closed(*args):
        state['closed']=True;state['cancelled'].set()
        if state in panels:panels.remove(state)
    top=helper.CreateToplevelWidget('Mesh Exporter',closed)
    state['window']=top;panels.append(state)
    layout=helper.CreateVerticalContainer();helper.AddWidget(top,layout)
    def label(text):
        widget=helper.CreateLabel();helper.SetWidgetText(widget,text);helper.AddWidget(layout,widget);return widget
    def textbox(text):
        widget=helper.CreateTextBox(True);helper.SetWidgetText(widget,text);helper.AddWidget(layout,widget);return widget
    def combo(values):
        widget=helper.CreateComboBox(False);helper.SetComboOptions(widget,values);helper.AddWidget(layout,widget);return widget
    def button(parent,text,callback):
        widget=helper.CreateButton(callback);helper.SetWidgetText(widget,text);helper.AddWidget(parent,widget);return widget
    def next_output(parent,stem):
        candidate=parent/(stem+'_meshes');n=2
        while candidate.exists() and any(candidate.iterdir()):
            candidate=parent/(stem+'_meshes_%02d'%n);n+=1
        return candidate
    filename=ctx.GetCaptureFilename() if ctx.IsCaptureLoaded() else ''
    capture=Path(filename) if filename else Path.home()/'capture.rdc'
    label('Output directory (existing files are never overwritten)')
    output=textbox(str(next_output(capture.parent,capture.stem)))
    def browse(*args):
        directory=ctx.Extensions().OpenDirectoryName('Choose export parent folder',str(capture.parent))
        if directory:helper.SetWidgetText(output,str(next_output(Path(directory),Path(ctx.GetCaptureFilename()).stem or 'capture')))
    button(layout,'Choose output folder...',browse)
    label('Scope');scope=combo(['All draw calls','Current draw call'])
    label('Geometry source');mode=combo(['Source vertices (pre-transform)','Post-VS diagnostics (projected, no recovered UVs)'])
    label('Minimum referenced vertices');minimum=helper.CreateSpinbox(0,1.0)
    helper.SetSpinboxBounds(minimum,1,100000000);helper.SetSpinboxValue(minimum,10);helper.AddWidget(layout,minimum)
    label('Textures');textures=combo(['Export used textures and draw groups','Geometry only'])
    status=label('Open a capture, then export. Double-click model cards in the viewer for textured 3D.')
    buttons=helper.CreateHorizontalContainer();helper.AddWidget(layout,buttons)
    def set_status(text):
        helper.InvokeOntoUIThread(lambda: None if state['closed'] else helper.SetWidgetText(status,text))
    def complete(message,gallery=None):
        def update():
            state['busy']=False;state['gallery']=gallery
            if state['closed']:return
            helper.SetWidgetText(status,message);helper.SetWidgetEnabled(export_button,True)
            helper.SetWidgetEnabled(open_button,gallery is not None)
        helper.InvokeOntoUIThread(update)
    def export(*args):
        if state['busy']:return
        if not ctx.IsCaptureLoaded():
            ctx.Extensions().ErrorDialog('Load a capture before exporting.','Mesh Exporter');return
        try:
            dest=Path(helper.GetWidgetText(output).strip()).expanduser()
            if not str(dest) or str(dest)=='.':raise ValueError('Choose an output directory')
            if dest.exists() and any(dest.iterdir()):raise ValueError('The output directory is not empty. Choose another directory.')
            rules={}
            state['cancelled'].clear()
            options=Options(minimum_vertices=int(helper.GetSpinboxValue(minimum)),
                event=ctx.CurEvent() if helper.GetWidgetText(scope)=='Current draw call' else 0,
                mode='post_vs' if helper.GetWidgetText(mode).startswith('Post-VS') else 'source',
                textures=helper.GetWidgetText(textures).startswith('Export'),overrides=rules,cancelled=state['cancelled'])
            filename=ctx.GetCaptureFilename();restore=ctx.CurEvent()
        except Exception as error:
            ctx.Extensions().ErrorDialog(str(error),'Mesh Exporter');return
        state['busy']=True;helper.SetWidgetEnabled(export_button,False);helper.SetWidgetEnabled(open_button,False)
        def worker(controller):
            try:
                result=export_capture(controller,Path(filename).name,dest,options,set_status)
                s=result['summary'];message='%d meshes, %d texture groups, %d images. %d unsupported, %d errors.\n%s'%(s['models'],s['textureGroups'],s['uniqueImages'],s['unsupported'],s['errors'],dest)
                if result['cancelled']:message='Cancelled. Partial export retained.\n'+message
                complete(message,dest)
            except Exception as error:
                complete('Export failed: '+str(error))
            finally:
                controller.SetFrameEvent(restore,True)
        ctx.Replay().AsyncInvoke(worker)
    export_button=button(buttons,'Export',export)
    button(buttons,'Cancel export',lambda *args:state['cancelled'].set())
    def open_gallery(*args):
        if not state['gallery']:return
        root=Path(state['gallery'])
        if os.name=='nt':
            os.startfile(str(root/'viewer.cmd'))
        else:
            subprocess.Popen(['python3',str(root/'viewer'/'server.py'),str(root)])
    open_button=button(buttons,'Open viewer',open_gallery)
    helper.SetWidgetEnabled(open_button,False)
    ctx.AddDockWindow(top,qrd.DockReference.MainToolArea,None)
