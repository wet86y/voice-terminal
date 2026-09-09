"""Real DeepSeek diagnostics for speech envelope + business actions and label clarity.

No native tools, TTS, PCM, runtime/config changes, or generated text/reasoning are
stored.  Business actions are validated and their result is a clearly marked
synthetic execution event fed to the next real model request.
"""
from __future__ import annotations

import argparse, asyncio, json, sys, time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from voice_terminal.config import load_config
from probe_speech_decision_envelope_e2e import invoke

STATES = {"speak", "cancel", "wait"}
EMOTIONS = {"calm", "happy", "sad", "angry", "fearful", "disgusted", "surprised"}
ACTION_NAMES = {"files_search", "files_read_text", "web_search"}

ACTION_PROMPT = """你是中文持续语音 Agent。每轮最终 content 必须且只能是 JSON：
{"speak":{"state":"speak|cancel|wait", "emotion":"可选", "text":"仅 speak 必填"}, "actions":[...]}
语音规则：speak(text) 表示创建或替换文本语音队列；cancel 表示保留待播文本但挂起、不进入 TTS；wait 表示完全不修改当前语音队列。speak 必须有非空 text；cancel/wait 不得有 text。禁止普通正文、Markdown、JSON 外解释。
业务动作规则：当用户要求搜索或读取时，本轮应先使用 speak.state=wait，再在 actions 中放一个或多个合法动作；actions 只能是 files_search、files_read_text、web_search。不要编造动作结果。拿到下一轮的 TOOL_RESULT_EVENT 后，应把 actions 设为空数组，并用 speak(text) 给用户简短说明结果。不要因为轻微 ASR 变化高频替换文本。
动作形状：files_search={"name":"files_search","arguments":{"query":"非空字符串"}}；files_read_text={"name":"files_read_text","arguments":{"path":"非空字符串"}}；web_search={"name":"web_search","arguments":{"query":"非空字符串"}}。"""

LABEL_PROMPT = """你是中文持续语音 Agent。每轮只输出一个 JSON：
{"speak":{"state":"STATE", "text":"仅 speak 时必填"},"actions":[]}
state=speak：要让用户听到新文本；创建或替换文本队列。
state=wait：不改变当前队列，原状态和播放资格全部保持。
STATE_HOLD_LABEL：保留待播文本但暂时阻止其进入 TTS；不删除。只有后续 speak(text) 才恢复/替换；播放中不能打断。
本轮必须把 STATE_HOLD_LABEL 替换为指定候选词之一，并严格只输出 speak/cancel/wait 中的实际协议 state。不得输出普通正文。"""

ACTION_CASES = [
    {"name":"search_then_answer", "user":"请搜索一下香港今天的天气，然后告诉我结果。", "snapshot":"empty", "action":"web_search", "args":{"query":"香港今天的天气"}},
    {"name":"file_search_then_read", "user":"请在本地资料中搜索项目名称，然后读取最相关的文件。", "snapshot":"empty", "action":"files_search", "args":{"query":"项目名称"}},
    {"name":"read_specific_file", "user":"请读取资料文件 docs/README.md，并告诉我其中的主题。", "snapshot":"empty", "action":"files_read_text", "args":{"path":"docs/README.md"}},
]

LABEL_CASES = [
    {"name":"hold_candidate", "user":"当前已有一段待播回答。现在先不要让它进入 TTS，但不要删除；等我下一轮决定。", "snapshot":"queued", "expected":"hold"},
    {"name":"keep_unchanged", "user":"当前已有一段待播回答。保持它原样和原有播放资格，不要修改，等待下一轮。", "snapshot":"queued", "expected":"wait"},
    {"name":"resume", "user":"之前的待播内容已被暂时挂起。现在请给出新的正式回答。", "snapshot":"suspended", "expected":"speak"},
]
LABELS = ["cancel", "hold", "suspend"]

def envelope_messages(user: str, snapshot: str, *, result_event: dict[str, Any] | None = None) -> list[dict[str, str]]:
    data: dict[str, Any] = {"queue_status": snapshot, "text_only_queue": True, "now": time.time()}
    if result_event: data["tool_result_event"] = result_event
    return [{"role":"system","content":ACTION_PROMPT}, {"role":"user","content":user}, {"role":"system","content":"调度快照："+json.dumps(data, ensure_ascii=False)}]

def label_messages(user: str, snapshot: str, candidate: str) -> list[dict[str, str]]:
    prompt = LABEL_PROMPT.replace("STATE_HOLD_LABEL", candidate)
    return [{"role":"system","content":prompt}, {"role":"user","content":user}, {"role":"system","content":json.dumps({"speak_snapshot":{"queue_status":snapshot,"queued_text_chars":20,"text_only_queue":True}}, ensure_ascii=False)}]

def parse_envelope(raw: str, *, allow_actions: bool = True) -> dict[str, Any]:
    try: value = json.loads(raw or "")
    except json.JSONDecodeError as e: return {"valid":False,"error":"invalid_json"}
    if not isinstance(value, dict) or set(value) != {"speak","actions"}: return {"valid":False,"error":"envelope_keys"}
    s = value.get("speak")
    if not isinstance(s, dict) or set(s)-{"state","emotion","text"}: return {"valid":False,"error":"speak_schema"}
    state = s.get("state")
    if state not in STATES: return {"valid":False,"error":"state"}
    if s.get("emotion","calm") not in EMOTIONS: return {"valid":False,"error":"emotion"}
    if state == "speak" and not isinstance(s.get("text"),str) or state == "speak" and not s.get("text","").strip(): return {"valid":False,"error":"speak_text"}
    if state != "speak" and "text" in s: return {"valid":False,"error":"non_speak_text"}
    acts = value.get("actions")
    if not isinstance(acts,list): return {"valid":False,"error":"actions_type"}
    if not allow_actions and acts: return {"valid":False,"error":"actions_not_empty"}
    clean=[]
    for a in acts:
        if not isinstance(a,dict) or set(a)!={"name","arguments"} or a.get("name") not in ACTION_NAMES or not isinstance(a.get("arguments"),dict): return {"valid":False,"error":"action_schema"}
        if a["name"] in {"web_search","files_search"} and not isinstance(a["arguments"].get("query"),str): return {"valid":False,"error":"action_query"}
        if a["name"]=="files_read_text" and not isinstance(a["arguments"].get("path"),str): return {"valid":False,"error":"action_path"}
        clean.append({"name":a["name"]})
    return {"valid":True,"state":state,"text_chars":len(s.get("text", "")),"actions":clean}

def meta(result: dict[str,Any], parsed: dict[str,Any], expected_state: str|None=None, expected_action: str|None=None) -> dict[str,Any]:
    return {"http_status":result.get("http_status"),"finish_reason":result.get("finish_reason"),"content_chars":len(result.get("content") or ""),"reasoning_chars":len(result.get("reasoning") or ""),"sse_order":result.get("sse_order"),"valid":parsed.get("valid"),"state":parsed.get("state"),"text_chars":parsed.get("text_chars"),"action_names":[x["name"] for x in parsed.get("actions",[])],"state_match":expected_state is None or parsed.get("state")==expected_state,"action_match":expected_action is None or expected_action in [x["name"] for x in parsed.get("actions",[])],"error":parsed.get("error"),"provider_error":result.get("error")}

async def run(args: argparse.Namespace) -> dict[str,Any]:
    import httpx
    cfg=load_config((ROOT/args.config).resolve()); key=cfg.api_key(cfg.llm.api_key_env)
    if not key: raise RuntimeError(f"missing {cfg.llm.api_key_env}")
    action_rows=[]; label_rows=[]
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout,connect=min(10,args.timeout))) as client:
        for n in range(1,args.attempts+1):
            for case in ACTION_CASES:
                first=await invoke(client=client,base_url=cfg.llm.base_url.rstrip('/'),model=cfg.llm.model,api_key=key,messages=envelope_messages(case["user"],case["snapshot"]),max_tokens=args.max_tokens)
                p1=parse_envelope(first.get("content","")); action_rows.append({"attempt":n,"case":case["name"],"phase":"request","expected_state":"wait","expected_action":case["action"],**meta(first,p1,"wait",case["action"])})
                result_event={"type":"TOOL_RESULT_EVENT","source":"synthetic_safe_mock","action":case["action"],"ok":True,"result_summary":"simulated result available; generated content omitted"}
                second=await invoke(client=client,base_url=cfg.llm.base_url.rstrip('/'),model=cfg.llm.model,api_key=key,messages=envelope_messages("请根据工具结果给用户简短说明。", "empty", result_event=result_event),max_tokens=args.max_tokens)
                p2=parse_envelope(second.get("content",""),allow_actions=False); action_rows.append({"attempt":n,"case":case["name"],"phase":"after_result","expected_state":"speak","expected_action":None,**meta(second,p2,"speak",None)})
            for case in LABEL_CASES:
                for candidate in LABELS:
                    result=await invoke(client=client,base_url=cfg.llm.base_url.rstrip('/'),model=cfg.llm.model,api_key=key,messages=label_messages(case["user"],case["snapshot"],candidate),max_tokens=args.max_tokens)
                    p=parse_envelope(result.get("content",""),allow_actions=False)
                    # hold/suspend are tested as semantic alternatives to cancel; wait/speak are protocol decisions.
                    expected = "cancel" if case["expected"]=="hold" and candidate in {"cancel","hold","suspend"} else case["expected"]
                    label_rows.append({"attempt":n,"case":case["name"],"candidate":candidate,"semantic_expected":case["expected"],**meta(result,p,expected,None)})
    return {"probe":"speech-actions-and-labels","model":cfg.llm.model,"transport":"real DeepSeek thinking + response_format=json_object; no native tools; no TTS/PCM","attempts":args.attempts,"action_rows":action_rows,"label_rows":label_rows,"tts_or_pcm_started":False,"stored_generated_text":False,"stored_reasoning":False}

def main():
    p=argparse.ArgumentParser(); p.add_argument("--config",default="config/default.yaml"); p.add_argument("--attempts",type=int,default=2,choices=range(1,4)); p.add_argument("--timeout",type=float,default=90); p.add_argument("--max-tokens",type=int,default=900); p.add_argument("--output",type=Path,required=True); a=p.parse_args(); out=asyncio.run(run(a)); a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=="__main__": main()
