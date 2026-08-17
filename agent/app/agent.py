import json,time,httpx
from pathlib import Path
from .config import BASE_DIR,ROUTER_URL,DEFAULT_MODEL
from .tools import SCHEMAS,FUNCS,should_search,web_search
SYSTEM=(BASE_DIR/'system_prompt.md').read_text(encoding='utf-8')
def models():
 with httpx.Client(timeout=15) as c: r=c.get(f'{ROUTER_URL}/models',headers={'Authorization':'Bearer local'}); r.raise_for_status(); return [x['id'] for x in r.json().get('data',[]) if x.get('id')]
def complete(payload):
 with httpx.Client(timeout=90) as c: r=c.post(f'{ROUTER_URL}/chat/completions',headers={'Authorization':'Bearer local','Content-Type':'application/json'},json=payload); r.raise_for_status(); return r.json()
def choose(requested):
 if requested: return requested
 if DEFAULT_MODEL: return DEFAULT_MODEL
 m=models(); return m[0] if m else ''
def run(message,history,model,auto_web=True):
 model=choose(model); msgs=[{'role':'system','content':SYSTEM},*history[-30:],{'role':'user','content':message}]; events=[]
 if auto_web and should_search(message):
  try:
   s=web_search(message,6); lines=['LIVE WEB SEARCH RESULTS','Cite source URLs.','']
   for i,x in enumerate(s['results'],1): lines += [f'[{i}] {x["title"]}',f'URL: {x["url"]}',f'Snippet: {x["snippet"]}','']
   msgs.append({'role':'system','content':'\n'.join(lines)}); events.append({'name':'web_search','ok':True,'automatic':True,'result_count':len(s['results'])})
  except Exception as e: events.append({'name':'web_search','ok':False,'automatic':True,'error':str(e)})
 for _ in range(4):
  payload={'model':model,'messages':msgs,'tools':SCHEMAS,'tool_choice':'auto','temperature':0.2,'stream':False}
  try: result=complete(payload)
  except httpx.HTTPStatusError as e:
   if e.response.status_code in {400,404,422,500} and not events:
    payload.pop('tools',None); payload.pop('tool_choice',None); result=complete(payload)
   else: raise
  a=result['choices'][0]['message']; calls=a.get('tool_calls') or []
  if not calls: return a.get('content') or '',model,events
  msgs.append(a)
  for call in calls:
   name=call.get('function',{}).get('name',''); raw=call.get('function',{}).get('arguments','{}'); cid=call.get('id') or f'tool-{int(time.time()*1000)}'
   try:
    args=raw if isinstance(raw,dict) else json.loads(raw or '{}'); out=FUNCS[name](**args); events.append({'name':name,'ok':True,'arguments':args})
   except Exception as e: out={'error':str(e)}; events.append({'name':name,'ok':False,'error':str(e)})
   msgs.append({'role':'tool','tool_call_id':cid,'name':name,'content':json.dumps(out,ensure_ascii=False)})
 result=complete({'model':model,'messages':msgs+[{'role':'user','content':'Answer now using collected results.'}],'temperature':0.2,'stream':False}); return result['choices'][0]['message'].get('content') or '',model,events
