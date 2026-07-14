"""
Stage 13：即時控制面板的 web server（比照 stage12 server.py 複製再改）。

不直接 import stage13/graph.py 來跑會議——每個「跑一段」的動作都透過
subprocess 呼叫 run_worker.py（理由見該檔案開頭的說明：避免多場會議共用
模組層級可變狀態）。這支檔案只負責：
- run 的生命週期管理（開始/查狀態/回答問題或跳過）
- 用 SSE 把某場會議的 events.jsonl 即時串給前端
- persona 設定的讀寫（真實檔 practice/personas.yaml，不動 .example. 檔）
- 跑完的會議提供最終報告 .md，以及完整事件回放頁（`stage13/build_replay.py`
  是 stage13 自己的一份，不是 stage12 那份——Double Diamond 重排後
  node 拓樸/state 形狀完全不同，不能共用同一套 compute_comparison()，
  見 build_replay.py 開頭的說明）
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel

STAGE13_DIR = Path(__file__).resolve().parent
PRACTICE_DIR = STAGE13_DIR.parent
RUNS_DIR = PRACTICE_DIR / "outputs" / "runs"
WORKER_SCRIPT = STAGE13_DIR / "run_worker.py"
STATIC_DIR = STAGE13_DIR / "static"

PERSONAS_REAL = PRACTICE_DIR / "personas.yaml"
PERSONAS_EXAMPLE = PRACTICE_DIR / "personas.example.yaml"
USERS_REAL = PRACTICE_DIR / "users.yaml"
USERS_EXAMPLE = PRACTICE_DIR / "users.example.yaml"

sys.path.insert(0, str(STAGE13_DIR))
import build_replay  # noqa: E402

app = FastAPI(title="Agentic Brainstorming — 即時控制面板")

# run_id -> 目前正在跑的 worker subprocess（None／不存在＝目前沒有 subprocess 在跑這場）
_processes: dict[str, subprocess.Popen] = {}


def _slugify(topic: str) -> str:
    """run_id 會直接出現在 URL path 跟本機檔案路徑裡——中文字元沒 percent-encode
    時會讓 h11/uvicorn 判定成 invalid HTTP request line（實測踩到），所以只留
    ASCII 字元；主題全文仍完整存在 meta.json 裡，不會因此遺失。"""
    s = re.sub(r"[^A-Za-z0-9]+", "-", topic).strip("-").lower()
    return s[:24] or "topic"


def _run_dir(run_id: str) -> Path:
    d = RUNS_DIR / run_id
    if not d.exists():
        raise HTTPException(404, f"找不到 run_id={run_id}")
    return d


def _read_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _is_alive(run_id: str) -> bool:
    proc = _processes.get(run_id)
    return proc is not None and proc.poll() is None


def _current_status(run_id: str) -> dict:
    if _is_alive(run_id):
        return {"status": "running"}
    return _read_json(RUNS_DIR / run_id / "state.json", {"status": "running"})


class CreateRunRequest(BaseModel):
    topic: str
    example_config: bool = False


class ResumeRequest(BaseModel):
    action: str  # "ask" | "skip"
    target_idea_id: Optional[str] = None
    question: Optional[str] = None
    asked_by: Optional[str] = None


class PersonasRequest(BaseModel):
    personas: list[dict]


class UsersRequest(BaseModel):
    users: list[dict]


@app.get("/", response_class=HTMLResponse)
def index():
    path = STATIC_DIR / "index.html"
    if not path.exists():
        return HTMLResponse("<h1>Stage 13</h1><p>static/index.html 還沒建立。</p>")
    return FileResponse(path)


@app.get("/static/shared_renderers.js")
def shared_renderers_js():
    # 這份檔案的正本在 practice/stage10/（跟 stage10/build_replay.py 共用
    # 同一份原始碼，不是複製過來的）——stage13/build_replay.py 已經在載入
    # 時把內容讀成字串常數，這裡直接原樣 serve 出去，不用另外 mount 整個
    # stage10 目錄（那會連 build_replay.py 原始碼都曝露出去）。
    return Response(build_replay.SHARED_RENDERERS_JS, media_type="application/javascript")


@app.get("/api/personas")
def get_personas():
    path = PERSONAS_REAL if PERSONAS_REAL.exists() else PERSONAS_EXAMPLE
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"personas": data.get("personas", []), "source": "real" if path == PERSONAS_REAL else "example"}


@app.get("/api/users")
def get_users():
    # 跟 /api/personas 一樣走 dual-track fallback。
    path = USERS_REAL if USERS_REAL.exists() else USERS_EXAMPLE
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"users": data.get("users", []), "source": "real" if path == USERS_REAL else "example"}


@app.put("/api/users")
def put_users(body: UsersRequest):
    # 使用者要求模擬用戶也能像 persona 一樣在開始前先編輯——跟
    # put_personas 同款式，只寫真實檔，絕不動 .example. 檔。
    for u in body.users:
        if not u.get("id") or not u.get("name"):
            raise HTTPException(400, "每個模擬用戶都要有 id 跟 name")
    USERS_REAL.write_text(
        yaml.safe_dump({"users": body.users}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return {"status": "saved", "count": len(body.users)}


@app.put("/api/personas")
def put_personas(body: PersonasRequest):
    for p in body.personas:
        if not p.get("id") or not p.get("name"):
            raise HTTPException(400, "每個 persona 都要有 id 跟 name")
    PERSONAS_REAL.write_text(
        yaml.safe_dump({"personas": body.personas}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return {"status": "saved", "count": len(body.personas)}


@app.post("/api/runs")
def create_run(body: CreateRunRequest):
    if any(_is_alive(rid) for rid in _processes):
        raise HTTPException(409, "已經有一場會議正在進行，請先等它結束或暫停後再開新的")
    if not body.topic.strip():
        raise HTTPException(400, "主題不能是空的")

    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_slugify(body.topic)}-{uuid.uuid4().hex[:6]}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {"run_id": run_id, "topic": body.topic, "created_at": datetime.now().isoformat(),
             "example_config": body.example_config},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    args = [sys.executable, str(WORKER_SCRIPT), "start",
            "--run-dir", str(run_dir), "--thread", run_id, "--topic", body.topic]
    if body.example_config:
        args.append("--example-config")
    log = (run_dir / "worker.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
    _processes[run_id] = proc
    return {"run_id": run_id}


@app.get("/api/runs")
def list_runs():
    if not RUNS_DIR.exists():
        return []
    runs = []
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta = _read_json(d / "meta.json", {})
        status = _current_status(d.name)
        runs.append({**meta, **status})
    return runs


@app.get("/api/runs/{run_id}/status")
def run_status(run_id: str):
    _run_dir(run_id)
    return _current_status(run_id)


@app.get("/api/runs/{run_id}/config")
def run_config(run_id: str):
    # run_worker.cmd_start() 存的這場 run 實際用的 personas/users 快照——
    # 跟 /api/personas、/api/users 不是同一份資料：那兩個反映的是設定畫面
    # 「現在」的狀態，這場 run 可能早就開始了，或者是 --example-config
    # 但使用者真實設定檔存在，兩者內容會對不起來。即時畫面顯示這場 run
    # 的事件細節（persona_id／user_id 解析成姓名、被訪談者人物設定）要用
    # 這份快照，不能用 /api/personas、/api/users。
    run_dir = _run_dir(run_id)
    return _read_json(run_dir / "config_snapshot.json", {"personas": [], "users": []})


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: str, body: ResumeRequest):
    run_dir = _run_dir(run_id)
    if _is_alive(run_id):
        raise HTTPException(409, "這場會議目前正在跑，還不能 resume")
    state = _current_status(run_id)
    if state.get("status") != "paused":
        raise HTTPException(409, f"這場會議目前狀態是 {state.get('status')}，不是 paused，無法 resume")
    if body.action == "ask" and not (body.question and body.question.strip()):
        raise HTTPException(400, "action=ask 需要附上非空的 question")
    if body.action == "ask" and not (body.target_idea_id and body.target_idea_id.strip()):
        raise HTTPException(400, "action=ask 需要附上要提問的 target_idea_id")

    meta = _read_json(run_dir / "meta.json", {})
    args = [sys.executable, str(WORKER_SCRIPT), "resume",
            "--run-dir", str(run_dir), "--thread", run_id, "--action", body.action]
    if body.action == "ask":
        args += ["--target-idea-id", body.target_idea_id,
                  "--question", body.question, "--asked-by", body.asked_by or "匿名"]
    if meta.get("example_config"):
        args.append("--example-config")
    log = (run_dir / "worker.log").open("a", encoding="utf-8")
    proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
    _processes[run_id] = proc
    return {"status": "resuming"}


@app.get("/api/runs/{run_id}/events")
def stream_events(run_id: str):
    run_dir = _run_dir(run_id)
    events_path = run_dir / "events.jsonl"

    def gen():
        pos = 0
        while True:
            if events_path.exists():
                with events_path.open("r", encoding="utf-8") as f:
                    f.seek(pos)
                    new_text = f.read()
                    pos = f.tell()
                for line in new_text.splitlines():
                    if line.strip():
                        yield f"data: {line}\n\n"
            if not _is_alive(run_id):
                state = _current_status(run_id)
                if state.get("status") in ("done", "paused", "error"):
                    yield f"event: state\ndata: {json.dumps(state, ensure_ascii=False)}\n\n"
                    break
            time.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}/report")
def get_report(run_id: str):
    # 跑完後「回顧」有兩種方式：這份最終報告 .md（快速看結論），或下面的
    # /replay 完整事件回放頁（想看「怎麼做到的」逐步過程）——兩者互補，
    # 不是二選一。
    run_dir = _run_dir(run_id)
    state = _current_status(run_id)
    if state.get("status") != "done":
        raise HTTPException(409, "這場會議還沒跑完，還沒有最終報告")
    if not state.get("report_path"):
        raise HTTPException(404, "這場會議沒有產生報告檔案")
    # report_path 是 run_worker._inspect_final_state() 存的，
    # relative_to(sg.OUTPUT_DIR.parent) 算出來——sg.OUTPUT_DIR 被 monkeypatch
    # 成 run_dir 本身（不是 run_dir/outputs），所以那個「parent」正是
    # RUNS_DIR，也就是 run_dir.parent，不是 run_dir.parent.parent。
    report_path = run_dir.parent / state["report_path"]
    if not report_path.exists():
        raise HTTPException(404, "找不到報告檔案")
    return PlainTextResponse(report_path.read_text(encoding="utf-8"), media_type="text/markdown")


@app.get("/api/runs/{run_id}/replay", response_class=HTMLResponse)
def get_replay(run_id: str):
    # 完整事件回放頁——零依賴單檔 HTML，內嵌整場會議的事件資料，可以
    # 逐步重播每一步、看結構化差異對照表（stage13/build_replay.py）。
    run_dir = _run_dir(run_id)
    state = _current_status(run_id)
    if state.get("status") != "done":
        raise HTTPException(409, "這場會議還沒跑完，還沒有完整回放頁")
    if not state.get("run_json_path"):
        raise HTTPException(404, "這場會議沒有產生 run JSON，無法組回放頁")
    # run_json_path 是 run_worker._inspect_final_state() 存的，
    # relative_to(sg.OUTPUT_DIR.parent) 算出來——sg.OUTPUT_DIR 被 monkeypatch
    # 成 run_dir 本身，所以那個「parent」正是 RUNS_DIR，也就是
    # run_dir.parent，不是 run_dir.parent.parent。
    run_json_path = run_dir.parent / state["run_json_path"]
    events_path = run_dir / "events.jsonl"
    if not run_json_path.exists() or not events_path.exists():
        raise HTTPException(404, "找不到回放所需的資料檔案")

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    run_data = json.loads(run_json_path.read_text(encoding="utf-8"))
    comparison = build_replay.compute_comparison(run_data)
    title = run_data.get("topic", run_id)
    # 訪談對象（現在巢狀在每個候選 job 的 interview_pool 裡，不是扁平的
    # interviewees 欄位）＋最終評估者都是「使用者」身分，合併成一份清單給
    # findUser()／renderUserProfile() 查找（id 前綴 u.../e... 不會互相
    # 覆蓋，跟 static/index.html 即時畫面同一套慣例）。
    interviewees = [
        person for cj in (run_data.get("candidate_jobs") or [])
        for person in (cj.get("interview_pool") or [])
    ]
    users = interviewees + (run_data.get("evaluators") or [])
    html = build_replay.build_replay_html(
        events, comparison, title, personas=run_data.get("personas"), users=users,
    )
    return HTMLResponse(html)


if STATIC_DIR.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
