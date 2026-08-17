import html,ipaddress,re,socket
from pathlib import Path
from urllib.parse import urlparse
import httpx
from ddgs import DDGS
from .config import KNOWLEDGE_DIR,LOG_DIR
TRIGGERS=('search the web','search internet','browse','look online','verify online','latest','current','today','news','release','version','price','weather')
ALLOWED={'.txt','.md','.json','.yaml','.yml','.csv','.log','.conf','.ini','.toml','.xml','.html','.py','.sh','.pdf','.rtf','.docx'}
MAX_UPLOAD=25*1024*1024
def should_search(q): return any(x in q.lower() for x in TRIGGERS)
def web_search(query,max_results=6):
 out=[]
 with DDGS() as d:
  for x in d.text(query,max_results=max(1,min(int(max_results),10))): out.append({'title':x.get('title',''),'url':x.get('href',''),'snippet':x.get('body','')})
 return {'query':query,'results':out}
def private(host):
 if host.lower() in {'localhost','localhost.localdomain'}: return True
 try: infos=socket.getaddrinfo(host,None)
 except socket.gaierror: return True
 for i in infos:
  try: ip=ipaddress.ip_address(i[4][0])
  except ValueError: continue
  if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved: return True
 return False
def fetch_url(url,max_chars=24000):
 p=urlparse(url)
 if p.scheme not in {'http','https'} or private(p.hostname or ''): raise ValueError('URL blocked')
 with httpx.Client(timeout=25,follow_redirects=True,headers={'User-Agent':'CylinderUIAgent/2.0'}) as c: r=c.get(url); r.raise_for_status()
 text=re.sub(r'(?is)<script.*?>.*?</script>',' ',r.text); text=re.sub(r'(?is)<style.*?>.*?</style>',' ',text); text=re.sub(r'(?s)<[^>]+>',' ',text); text=html.unescape(re.sub(r'\s+',' ',text)).strip(); max_chars=max(1000,min(int(max_chars),50000))
 return {'url':str(r.url),'status':r.status_code,'content':text[:max_chars],'truncated':len(text)>max_chars}
def safe(root,rel):
 p=(root/rel.lstrip('/')).resolve(); p.relative_to(root); return p
def list_knowledge(path='',recursive=False,limit=200):
 t=safe(KNOWLEDGE_DIR,path)
 if not t.is_dir(): raise ValueError('Directory not found')
 it=t.rglob('*') if recursive else t.iterdir(); out=[]
 for x in it:
  s=x.stat(); out.append({'path':str(x.relative_to(KNOWLEDGE_DIR)),'directory':x.is_dir(),'size':s.st_size,'modified':int(s.st_mtime)})
  if len(out)>=min(max(int(limit),1),1000): break
 return {'entries':out}
def _extract_text(p):
 s=p.suffix.lower()
 if s=='.pdf':
  from pdfminer.high_level import extract_text as _pdf
  return _pdf(str(p)) or ''
 if s=='.docx':
  import docx
  return '\n'.join(par.text for par in docx.Document(str(p)).paragraphs)
 if s=='.rtf':
  from striprtf.striprtf import rtf_to_text
  return rtf_to_text(p.read_text(encoding='utf-8',errors='replace'))
 if s=='.doc':
  raise ValueError('Formato .doc antigo nao suportado; converta para .docx ou PDF')
 return p.read_text(encoding='utf-8',errors='replace')
def read_knowledge(path,max_chars=120000):
 p=safe(KNOWLEDGE_DIR,path)
 if not p.is_file() or p.suffix.lower() not in ALLOWED: raise ValueError('File unavailable')
 c=_extract_text(p); max_chars=max(1000,min(int(max_chars),500000)); return {'path':str(p.relative_to(KNOWLEDGE_DIR)),'content':c[:max_chars],'truncated':len(c)>max_chars}
def save_knowledge(filename,data):
 import os as _os
 name=_os.path.basename((filename or '').replace('\\','/')).strip()
 if not name or name.startswith('.'): raise ValueError('Nome invalido')
 ext=('.'+name.rsplit('.',1)[1].lower()) if '.' in name else ''
 if ext not in ALLOWED: raise ValueError('Extensao nao permitida: '+(ext or 'nenhuma'))
 if not isinstance(data,(bytes,bytearray)): raise ValueError('Dados invalidos')
 if len(data)>MAX_UPLOAD: raise ValueError('Arquivo excede o limite de 25MB')
 p=safe(KNOWLEDGE_DIR,name); p.write_bytes(bytes(data)); st=p.stat()
 return {'path':str(p.relative_to(KNOWLEDGE_DIR)),'size':st.st_size,'modified':int(st.st_mtime)}
def search_logs(query,max_matches=100):
 # PATCH-LOGSTREAM-V1: stream line by line; the old version read whole files
 # into RAM, which meant a 40 MB log became a 40 MB allocation per call.
 out=[]; q=query.lower(); cap=min(max(int(max_matches),1),500)
 for p in sorted(LOG_DIR.rglob('*.log')):
  try:
   with p.open('r',encoding='utf-8',errors='replace') as f:
    for n,line in enumerate(f,1):
     if q in line.lower():
      out.append({'file':str(p.relative_to(LOG_DIR)),'line':n,'text':line.rstrip()[:2000]})
      if len(out)>=cap: return {'matches':out,'truncated':True}
  except OSError: pass
 return {'matches':out,'truncated':False}
SCHEMAS=[
 {'type':'function','function':{'name':'web_search','description':'Search the public web','parameters':{'type':'object','properties':{'query':{'type':'string'},'max_results':{'type':'integer'}},'required':['query']}}},
 {'type':'function','function':{'name':'fetch_url','description':'Fetch a public webpage','parameters':{'type':'object','properties':{'url':{'type':'string'},'max_chars':{'type':'integer'}},'required':['url']}}},
 {'type':'function','function':{'name':'list_knowledge','description':'List approved knowledge files','parameters':{'type':'object','properties':{'path':{'type':'string'},'recursive':{'type':'boolean'},'limit':{'type':'integer'}}}}},
 {'type':'function','function':{'name':'read_knowledge','description':'Read an approved text file','parameters':{'type':'object','properties':{'path':{'type':'string'},'max_chars':{'type':'integer'}},'required':['path']}}},
 {'type':'function','function':{'name':'search_logs','description':'Search approved log files','parameters':{'type':'object','properties':{'query':{'type':'string'},'max_matches':{'type':'integer'}},'required':['query']}}}
]
FUNCS={'web_search':web_search,'fetch_url':fetch_url,'list_knowledge':list_knowledge,'read_knowledge':read_knowledge,'search_logs':search_logs}
