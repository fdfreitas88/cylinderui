from pathlib import Path
import os
BASE_DIR=Path(__file__).resolve().parent.parent
STATIC_DIR=BASE_DIR/'static'; TEMPLATE_DIR=BASE_DIR/'templates'; DATA_DIR=BASE_DIR/'data'
ROUTER_URL=os.getenv('ROUTER_URL','http://127.0.0.1:8088/v1').rstrip('/')
LLAMA_SWAP_URL=os.getenv('LLAMA_SWAP_URL','http://127.0.0.1:8080').rstrip('/')
DEFAULT_MODEL=os.getenv('AGENT_MODEL','')
KNOWLEDGE_DIR=Path(os.getenv('KNOWLEDGE_DIR',str(Path.home()/'local-ai/knowledge'))).resolve()
LOG_DIR=Path(os.getenv('LOG_DIR',str(Path.home()/'local-ai/logs'))).resolve()
for p in (DATA_DIR,KNOWLEDGE_DIR,LOG_DIR): p.mkdir(parents=True,exist_ok=True)
