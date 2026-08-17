from fastapi import FastAPI,HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from typing import Any
from .config import STATIC_DIR,TEMPLATE_DIR,ROUTER_URL,DEFAULT_MODEL
from .db import init,ensure,add,history,conversations
from .agent import run,models
from .stream import orchestrate
from .tools import list_knowledge,read_knowledge,save_knowledge
from .fs_tools import list_allowed_directories,add_allowed_directory,remove_allowed_directory,browse_directories  # PATCH-DIRTOOLS-V1
from .fs_tools import start_upload_session,save_uploaded_file  # PATCH-DIRUPLOAD-V1
from .model_store import search_models,install_model,list_downloads,cancel_download,pause_download,resume_download  # PATCH-MODELSTORE-V1
from .model_store import list_models,set_visible_interfaces,set_default_interfaces,set_order_position,set_active,uninstall_model,prune,get_usage,get_model  # PATCH-MODELSTORE-V1
from .model_store import get_exec,get_exec_all,set_exec  # PATCH-MODELEXEC-V1
from .visions import list_visions,get_vision,create_vision,update_vision,delete_vision,reorder_visions,get_vision_ids  # PATCH-VISIONS-V1
from .model_bench import run_benchmark,get_status,get_log,cancel_benchmark,optimize_cpu  # PATCH-MODELBENCH-V1
from fastapi import Body,Form,File,UploadFile
from fastapi.responses import StreamingResponse
import json
class Req(BaseModel):
 message:str=Field(min_length=1,max_length=50000); model:str|None=None; conversation_id:str|None=None; auto_web_search:bool=True
app=FastAPI(title='CylinderUI Local AI Agent',version='2.0.0'); app.mount('/static',StaticFiles(directory=str(STATIC_DIR)),name='static')
@app.on_event('startup')
def startup(): init()
@app.get('/')
def index(): return FileResponse(TEMPLATE_DIR/'index.html')
@app.get('/health')
def health():
 try: m=models(); ok=True
 except Exception: m=[]; ok=False
 return {'status':'healthy','router':ROUTER_URL,'router_ok':ok,'default_model':DEFAULT_MODEL,'models':m,'version':'2.0.0'}
@app.get('/api/conversations')
def convs(): return {'conversations':conversations()}
@app.post('/api/chat/simple')
def chat(r:Req):
 cid=ensure(r.conversation_id,r.message); h=history(cid); add(cid,'user',r.message)
 try: answer,model,events=run(r.message,h,r.model,r.auto_web_search)
 except Exception as e: raise HTTPException(status_code=502,detail=str(e))
 add(cid,'assistant',answer); return {'answer':answer,'model':model,'conversation_id':cid,'tool_events':events}
def _sse(cfg):
 try:
  for ev,data in orchestrate(cfg):
   yield 'event: '+ev+'\ndata: '+json.dumps(data,ensure_ascii=False)+'\n\n'
 except Exception as e:
  yield 'event: error\ndata: '+json.dumps({'message':str(e)})+'\n\n'
  yield 'event: done\ndata: {}\n\n'
@app.post('/api/chat')
def chat_stream(cfg: dict = Body(...)):
 return StreamingResponse(_sse(cfg), media_type='text/event-stream')


class KUp(BaseModel):
 filename:str=Field(min_length=1,max_length=255); content_b64:str=Field(min_length=1)
@app.get('/api/knowledge')
def klist():
 try: return list_knowledge('',True,1000)
 except Exception as e: raise HTTPException(status_code=500,detail=str(e))
@app.get('/api/knowledge/file')
def kfile(path:str):
 try: return read_knowledge(path)
 except Exception as e: raise HTTPException(status_code=400,detail=str(e))
@app.post('/api/knowledge')
def kadd(u:KUp):
 import base64
 try: raw=base64.b64decode(u.content_b64)
 except Exception: raise HTTPException(status_code=400,detail='base64 invalido')
 try: return {'ok':True,'file':save_knowledge(u.filename,raw)}
 except ValueError as e: raise HTTPException(status_code=400,detail=str(e))
 except Exception as e: raise HTTPException(status_code=500,detail=str(e))


# ---------------------------------------------------------------- PATCH-DIRTOOLS-V1
# Diretorios permitidos (icone "Diretorios" no topo do console, ao lado do RAG).
# Adicionar/remover e SEMPRE acao humana (UI) -- nunca uma tool chamavel pelo
# modelo. O modelo so pode listar/ler/escrever DENTRO do que ja foi adicionado
# (ver app/fs_tools.py e o switch "files" em app/stream.py).
class DirReq(BaseModel):
 path:str=Field(min_length=1,max_length=1000)
@app.get('/api/directories')
def directories_list():
 return {'directories':list_allowed_directories()}
@app.get('/api/directories/browse')
def directories_browse(path:str=''):
 return browse_directories(path)
@app.post('/api/directories')
def directories_add(r:DirReq):
 result=add_allowed_directory(r.path)
 if not result.get('ok'): raise HTTPException(status_code=400,detail=result.get('error','invalid path'))
 return result
@app.post('/api/directories/remove')
def directories_remove(r:DirReq):
 return remove_allowed_directory(r.path)
# ------------------------------------------------------------ end PATCH-DIRTOOLS-V1


# ---------------------------------------------------------------- PATCH-DIRUPLOAD-V1
# Upload de uma pasta escolhida no NAVEGADOR do usuario (maquina cliente),
# ja que a pagina roda em HTTP simples e nao pode montar/ler o disco do
# cliente ao vivo (File System Access API exige contexto seguro). O upload
# copia os arquivos para o servidor (data/uploaded_dirs/<nome>) e registra
# esse destino como diretorio permitido. Isso e uma COPIA, nao uma conexao
# ao vivo: edicoes do agente ficam so na copia. Sempre acao humana (UI).
class UploadStartReq(BaseModel):
 folder_name:str=Field(min_length=1,max_length=200)
@app.post('/api/directories/upload/start')
def upload_start(r:UploadStartReq):
 return start_upload_session(r.folder_name)
@app.post('/api/directories/upload/file')
async def upload_file(base:str=Form(...),relpath:str=Form(...),file:UploadFile=File(...)):
 data=await file.read()
 return save_uploaded_file(base,relpath,data)
# ------------------------------------------------------------ end PATCH-DIRUPLOAD-V1

# ---------------------------------------------------------------- PATCH-MODELSTORE-V1
# Model Store (CylinderUI): busca HF+ModelScope, fila de download (1
# simultaneo, pausa/cancela/retoma), instalar/desinstalar (bloco marcado no
# llama-swap.yaml, ver app/model_store.py), estado por interface (C/CC/GOD)
# em data/models.json. Delega tudo a app/model_store.py -- main.py so expoe
# HTTP (Body/Field/HTTPException), sem logica de negocio aqui.
class StoreSearchReq(BaseModel):
 query:str=Field(default='',max_length=500); source:str|None=None   # "hf" | "modelscope" | None=ambos
@app.get('/api/store/search')
def store_search(query:str='',source:str|None=None):
 return search_models(query,source)

class StoreInstallReq(BaseModel):
 repo_id:str=Field(min_length=1,max_length=500); file:str=Field(min_length=1,max_length=500); source:str='hf'
@app.post('/api/store/install')
def store_install(r:StoreInstallReq):
 try: res=install_model(r.repo_id,r.file,r.source)
 except ValueError as e: raise HTTPException(status_code=400,detail=str(e))
 if not res.get('ok'): raise HTTPException(status_code=409,detail=res.get('error','falha ao iniciar download'))
 return res

@app.get('/api/store/downloads')
def store_downloads():
 return {'downloads':list_downloads()}

class StoreCancelReq(BaseModel):
 download_id:str
@app.post('/api/store/cancel')
def store_cancel(r:StoreCancelReq):
 res=cancel_download(r.download_id)
 if not res.get('ok'): raise HTTPException(status_code=404,detail=res.get('error'))
 return res

class StoreDownloadIdReq(BaseModel):
 download_id:str
@app.post('/api/store/pause')
def store_pause(r:StoreDownloadIdReq):
 res=pause_download(r.download_id)
 if not res.get('ok'): raise HTTPException(status_code=409,detail=res.get('error'))
 return res
@app.post('/api/store/resume')
def store_resume(r:StoreDownloadIdReq):
 res=resume_download(r.download_id)
 if not res.get('ok'): raise HTTPException(status_code=409,detail=res.get('error'))
 return res

@app.get('/api/models')
def models_list():
 return {'models':list_models()}

class ModelPatchReq(BaseModel):
 id:str=Field(min_length=1,max_length=200)
 order:int|None=None
 visible_in:list[str]|None=None   # ["C","CC","GOD"]
 default_for:list[str]|None=None  # ["C","CC","GOD"]
 active:bool|None=None
@app.post('/api/models')
def models_patch(r:ModelPatchReq):
 if not get_model(r.id): raise HTTPException(status_code=404,detail='modelo não encontrado')
 try:
  if r.visible_in is not None: set_visible_interfaces(r.id,r.visible_in)
  if r.default_for is not None: set_default_interfaces(r.id,r.default_for)
 except ValueError as e: raise HTTPException(status_code=400,detail=str(e))
 if r.order is not None: set_order_position(r.id,r.order)
 if r.active is not None:
  res=set_active(r.id,r.active)
  if not res.get('ok'): raise HTTPException(status_code=409,detail=res.get('error'))
 return {'ok':True,'model':get_model(r.id)}

@app.post('/api/models/uninstall')
def models_uninstall(r:DirReq):
 res=uninstall_model(r.path)
 if not res.get('ok'): raise HTTPException(status_code=400,detail=res.get('error'))
 return res

class ModelsPruneReq(BaseModel):
 ids:list[str]|None=None   # PATCH-MODELSTORE-V1 (Onda 4, R1): opcional -- sem
                            # ids, cai na politica automatica (orfaos reais);
                            # com ids, tenta remover so os pedidos (padrao continua bloqueado)
@app.post('/api/models/prune')
def models_prune(r:ModelsPruneReq):
 return prune(r.ids)

@app.get('/api/models/usage')
def models_usage():
 return get_usage()
# ------------------------------------------------------------ end PATCH-MODELSTORE-V1

# ----------------------------------------------------------------- PATCH-MODELBENCH-V1
# Benchmark & Otimizar CPU (CylinderUI, Onda 3): roda llama-bench (e,
# quando existir, llama-perplexity) por subprocess sobre o .gguf instalado,
# 1 job assincrono por vez (thread em background, log em
# data/bench-logs/<id>.log), persistindo status/resultado em data/models.json
# via model_store.set_bench/set_tuned/apply_cpu_tuning. Delega tudo a
# app/model_bench.py -- main.py so expoe HTTP, sem logica de negocio aqui
# (mesmo padrao do PATCH-MODELSTORE-V1).
class BenchRunReq(BaseModel):
 id:str=Field(min_length=1,max_length=200); profile:str='rapido'   # "rapido" | "medio" | "detalhado"
@app.post('/api/bench/run')
def bench_run(r:BenchRunReq):
 try: res=run_benchmark(r.id,r.profile)
 except ValueError as e: raise HTTPException(status_code=400,detail=str(e))
 if not res.get('ok'): raise HTTPException(status_code=409,detail=res.get('error','falha ao iniciar benchmark'))
 return res

@app.get('/api/bench/status')
def bench_status():
 return get_status()

@app.get('/api/bench/log')
def bench_log(tail:int=200):
 return get_log(tail)

@app.post('/api/bench/cancel')
def bench_cancel():
 res=cancel_benchmark()
 if not res.get('ok'): raise HTTPException(status_code=409,detail=res.get('error'))
 return res

class BenchOptimizeReq(BaseModel):
 id:str=Field(min_length=1,max_length=200)
@app.post('/api/bench/optimize')
def bench_optimize(r:BenchOptimizeReq):
 try: res=optimize_cpu(r.id)
 except ValueError as e: raise HTTPException(status_code=400,detail=str(e))
 if not res.get('ok'): raise HTTPException(status_code=409,detail=res.get('error','falha ao iniciar otimização'))
 return res
# ------------------------------------------------------------- end PATCH-MODELBENCH-V1

# ---------------------------------------------------------------- PATCH-MODELEXEC-V1
# Modos de execucao por interface (Onda 4): single/dual/agent, guardado em
# data/models.json (state["exec"][cylinderui|cyber|god], ver
# app/model_store.py::get_exec/get_exec_all/set_exec). main.py so expoe HTTP;
# a orquestracao real (dual/router) mora em app/model_exec.py e e' plugada em
# app/stream.py::orchestrate() via patch-stream-exec.py.txt.
#
# Import (colar junto aos outros imports do topo do arquivo):
#   from .model_store import get_exec,get_exec_all,set_exec  # PATCH-MODELEXEC-V1
class ExecPatchReq(BaseModel):
 interface:str=Field(min_length=1,max_length=80)   # "C"|"CC"|"GOD"|cylinderui/cyber/god|qualquer vision-id (Onda V2)
 mode:str|None=None      # "single"|"dual"|"agent" -- None = nao mexe
 main:str|None=None      # id do modelo principal; "" limpa
 aux:str|None=None       # id do modelo auxiliar; "" limpa
 role:str|None=None      # "second"|"review"|"draft"|"router"
 tools:dict|None=None    # {files,rag,web} (so usado no modo "agent")

@app.get('/api/exec')
def exec_get(interface:str|None=None):
 try:
  return get_exec(interface) if interface else get_exec_all()
 except ValueError as e: raise HTTPException(status_code=400,detail=str(e))

@app.post('/api/exec')
def exec_patch(r:ExecPatchReq):
 try:
  res=set_exec(r.interface,mode=r.mode,main=r.main,aux=r.aux,role=r.role,tools=r.tools)
 except ValueError as e: raise HTTPException(status_code=400,detail=str(e))
 if not res.get('ok'): raise HTTPException(status_code=400,detail=res.get('error'))
 return res
# ------------------------------------------------------------ end PATCH-MODELEXEC-V1

# ---------------------------------------------------------------- PATCH-VISIONS-V1
# Visões (CylinderUI): CRUD dinâmico, estilo Spaces do Mac. Cada visão
# encapsula modelos+defaultModel, exec (single/dual/agent), chats próprios
# (conversationKey), systemPrompt, theme e showOtherVisionsChats. As 3
# herdadas (ids "cylinderui"/"cyber"/"god") são builtin mas totalmente livres
# (editáveis e apagáveis) -- ver backend/visions.py::_seed_if_empty.
class VisionExecReq(BaseModel):
 mode:str|None=None       # "single"|"dual"|"agent"
 main:str|None=None
 aux:str|None=None
 role:str|None=None       # "second"|"review"|"draft"|"router"
 tools:dict|None=None     # {files,rag,web}

class VisionCreateReq(BaseModel):
 name:str=Field(min_length=1,max_length=200)
 theme:Any=None
 color:Any=None
 badge:str|None=None
 systemPrompt:str|None=None
 models:list[str]|None=None
 defaultModel:str|None=None
 exec:VisionExecReq|None=None
 conversationKey:str|None=None
 showOtherVisionsChats:bool|None=None
 hero:dict|None=None
 order:int|None=None

class VisionUpdateReq(BaseModel):
 name:str|None=None
 theme:Any=None
 color:Any=None
 badge:str|None=None
 systemPrompt:str|None=None
 models:list[str]|None=None
 defaultModel:str|None=None
 exec:VisionExecReq|None=None
 conversationKey:str|None=None
 showOtherVisionsChats:bool|None=None
 hero:dict|None=None
 order:int|None=None

class VisionReorderReq(BaseModel):
 ids:list[str]=Field(min_length=1)

# NOTA (fix pos-smoke, ver STATUS-fix-endpoints.md): prompt-router/router.py
# só proxia GET e POST -- não existe do_PATCH/do_DELETE lá (e o console fala
# com o Agent sempre via router :8088). Por isso update/delete de visão usam
# POST com verbo na URL (/update, /delete), igual ao padrão já usado em
# /api/models/uninstall (Model Store) -- NÃO usar PATCH/DELETE aqui.
class VisionUpdateBody(VisionUpdateReq):
 id:str=Field(min_length=1)

class VisionDeleteReq(BaseModel):
 id:str=Field(min_length=1)

@app.get('/api/visions')
def visions_list():
 return {'visions':list_visions()}

@app.get('/api/visions/{vision_id}')
def visions_get(vision_id:str):
 v=get_vision(vision_id)
 if not v: raise HTTPException(status_code=404,detail='visão não encontrada')
 return v

@app.post('/api/visions')
def visions_create(r:VisionCreateReq):
 payload=r.dict(exclude_unset=True); name=payload.pop('name')
 try: return create_vision(name,**payload)
 except ValueError as e: raise HTTPException(status_code=400,detail=str(e))

@app.post('/api/visions/update')
def visions_update(r:VisionUpdateBody):
 payload=r.dict(exclude_unset=True); vision_id=payload.pop('id')
 try: return update_vision(vision_id,**payload)
 except KeyError as e: raise HTTPException(status_code=404,detail=str(e))
 except ValueError as e: raise HTTPException(status_code=400,detail=str(e))

@app.post('/api/visions/delete')
def visions_delete(r:VisionDeleteReq):
 res=delete_vision(r.id)
 if not res.get('ok'): raise HTTPException(status_code=404,detail=res.get('error'))
 return res

@app.post('/api/visions/reorder')
def visions_reorder(r:VisionReorderReq):
 return reorder_visions(r.ids)
# ------------------------------------------------------------ end PATCH-VISIONS-V1

