"""
Stage 13 的執行單位（比照 stage12 run_worker.py 複製再改）：每次「開始一場
會議」或「回答/跳過一次人類提問」都是獨立的一個 subprocess 呼叫，不是常駐
process 裡的一條 thread——理由跟 stage12 完全一樣：graph.py 的
OUTPUT_DIR／EVENTS_PATH／CHECKPOINT_DB_PATH 等路徑是模組層級變數，同一個
process 裡如果同時有兩場會議的 thread 都在跑，monkeypatch 會互相覆蓋。

跟 stage12 的差異：graph.py 就在同一個目錄下，不用 `sys.path` 額外插入
路徑；`_refresh_dynamic_snapshot()` 要改成從巢狀的
`candidate_jobs[*].interview_pool` 挖訪談對象（stage12 是扁平的
`interviewees` 欄位）；存檔檔名前綴改成 `stage15-run-*.json`。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph as sg  # noqa: E402

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

_real_run_meeting = sg.run_meeting


def _safe_run_meeting(graph, config, initial_input, *, script, stop_after_first_interrupt):
    """monkeypatch 掉 sg.run_meeting，補上它自己沒有的一個檢查（跟
    stage11 run_worker.py 同一個理由、同一個修法，逐字複製）。

    graph.py 的 run_meeting() 開頭只看 snapshot.next 是不是空的來判斷
    「這個 thread 有沒有未完成工作」——空的話直接假設是全新 thread，呼叫
    `graph.invoke(initial_input, config)`。但「從沒開始過」跟「剛剛才跑完」
    這兩種情況 snapshot.next 都是空 tuple，run_meeting() 分不出來，會把
    已經跑完的 thread 誤判成全新 thread、從頭重跑一次。

    修法：resume 完先看 snapshot.next 是不是已經空了，空的話就不要再讓
    run_meeting() 去呼叫 initial_input 那個分支，直接回傳目前的 state。
    光看 snapshot.next 是空的還不夠：一個「從沒開始過」的全新 thread_id
    一樣會回報 snapshot.next == ()，跟「已經跑完」長得一模一樣——這裡兩者
    的區別要看 snapshot.values 是不是空字典：全新 thread 是 {}，已完成的
    thread 是最後一次 checkpoint 的完整 state（必然非空）。"""
    snapshot = graph.get_state(config)
    if not snapshot.next and snapshot.values:
        return snapshot.values
    return _real_run_meeting(
        graph, config, initial_input, script=script, stop_after_first_interrupt=stop_after_first_interrupt
    )


sg.run_meeting = _safe_run_meeting


def _patch_paths(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    sg.OUTPUT_DIR = run_dir
    sg.EVENTS_PATH = run_dir / "events.jsonl"
    sg.CHECKPOINT_DB_PATH = run_dir / "checkpoints.sqlite"
    sg.PROTOTYPE_DIR = run_dir / "prototypes"
    sg.REPORT_DIR = run_dir / "reports"


def _use_example_config() -> None:
    """只給測試/試跑用：強制走 personas.example.yaml 等公開範例設定，
    不動使用者真實的 personas.yaml／users.yaml。跟 stage10/run_sample_meeting.py
    monkeypatch load_personas 等函式的手法一樣。"""
    def _personas():
        data = __import__("yaml").safe_load((sg.PRACTICE_DIR / "personas.example.yaml").read_text(encoding="utf-8"))
        return data["personas"]

    def _users():
        data = __import__("yaml").safe_load((sg.PRACTICE_DIR / "users.example.yaml").read_text(encoding="utf-8"))
        return data["users"]

    def _company():
        return (sg.PRACTICE_DIR / "company.example.md").read_text(encoding="utf-8")

    sg.load_personas = _personas
    sg.load_users = _users
    sg.load_company = _company


def _write_state(run_dir: Path, **kwargs) -> None:
    (run_dir / "state.json").write_text(
        json.dumps(kwargs, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _refresh_dynamic_snapshot(state: dict) -> None:
    """stage15-market-fit 的腦力激盪參與者／訪談對象／最終評估者三組人都是動態生成
    （不是啟動前就固定的 personas.yaml／users.yaml），但 cmd_start() 寫
    config_snapshot.json 時（見下方 cmd_start()）會議根本還沒開始跑，
    只能先寫 sg.load_personas()／sg.load_users() 的靜態保底名單。
    `desk_research_hypothesize_jobs`／`discover_and_evaluate_jobs`／
    `select_job_and_define_problem`／`assemble_persona_team` 是父圖最
    前面四個節點，一定會在第一個（也是唯一一個）人類介入點
    `ask_question` 之前就跑完，所以只要這些欄位非空，這裡拿到的就已經
    是真正動態生成的名單——把它寫回 config_snapshot.json，即時畫面重新
    整理／首次載入時 loadRunConfig() 才不會拿到跟真實訪談對象/評估者
    對不上的舊快照。訪談對象現在巢狀在每個候選 job 的 `interview_pool`
    裡（不是 stage12 那樣扁平的 `interviewees` 欄位），跟最終評估者的
    id 前綴不同（u1.../e1...），直接合併成一份 `users` 清單給前端的
    `findUser()` 查找，不會互相覆蓋。"""
    personas = state.get("personas")
    interviewees = [
        person for cj in (state.get("candidate_jobs") or [])
        for person in (cj.get("interview_pool") or [])
    ]
    evaluators = state.get("evaluators") or []
    users = interviewees + evaluators
    if not personas and not users:
        return
    snapshot_path = sg.OUTPUT_DIR / "config_snapshot.json"
    if not snapshot_path.exists():
        return
    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if personas:
        data["personas"] = personas
    if users:
        data["users"] = users
    snapshot_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _inspect_final_state(thread_id: str) -> dict:
    """`sg.main()` 執行完（不管是暫停還是跑完）都不會回傳有用的值——它是設計
    給 CLI 用的。真正的狀態要重新打開 checkpointer、查 graph.get_state() 才知道：
    這跟 graph.py 的 run_meeting() 判斷「真正 interrupt 還是節點崩潰」用的是
    同一招（task.interrupts 是不是空 tuple）。"""
    conn = sqlite3.connect(str(sg.CHECKPOINT_DB_PATH), check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    graph = sg.build_parent_graph(checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    conn.close()

    _refresh_dynamic_snapshot(snapshot.values or {})

    if not snapshot.next:
        result = {"status": "done"}
        report_matches = sorted(sg.REPORT_DIR.glob(f"{thread_id}-final-report.md"))
        run_json_matches = sorted(sg.OUTPUT_DIR.glob("stage15-run-*.json"))
        if report_matches:
            result["report_path"] = str(report_matches[0].relative_to(sg.OUTPUT_DIR.parent))
        if run_json_matches:
            result["run_json_path"] = str(run_json_matches[-1].relative_to(sg.OUTPUT_DIR.parent))
        return result

    task = snapshot.tasks[0] if snapshot.tasks else None
    if task and task.interrupts:
        return {"status": "paused", "payload": task.interrupts[0].value}
    return {"status": "error", "message": str(task.error) if task else "未知狀態：沒有 task 也沒有 interrupts"}


def _run_main_and_capture(thread_id: str, run_dir: Path) -> None:
    try:
        sg.main()
    except SystemExit:
        pass
    except Exception:  # noqa: BLE001 - 這裡就是要接住所有例外寫進 state.json，不能讓 subprocess 靜默死掉
        _write_state(run_dir, status="error", message=traceback.format_exc())
        return
    result = _inspect_final_state(thread_id)
    _write_state(run_dir, **result)


def cmd_start(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    _patch_paths(run_dir)
    if args.example_config:
        _use_example_config()

    # 這場 run 實際用的 personas/users 快照存下來，跟即時畫面自己的
    # /api/personas／/api/users（反映的是「設定畫面現在長怎樣」，可能在
    # 這場會議開始後又被改掉，或者這場是 --example-config 但使用者真實
    # personas.yaml／users.yaml 存在，兩者根本不是同一份資料）分開——
    # 不然前端解析 persona_id／user_id 找到的會是錯的（或找不到，人物設定
    # 顯示不出來），跟使用者要的「看得到被訪談者是基於什麼理由回答」正好
    # 相反。
    (run_dir / "config_snapshot.json").write_text(
        json.dumps({"personas": sg.load_personas(), "users": sg.load_users()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    os.environ["BRAINSTORM_TOPIC"] = args.topic
    if args.n_concept_test_interviewees:
        os.environ["BRAINSTORM_N_CONCEPT_TEST_INTERVIEWEES"] = str(args.n_concept_test_interviewees)
    if args.survey_n:
        os.environ["BRAINSTORM_SURVEY_N"] = str(args.survey_n)
    sys.argv = ["graph.py", "--thread", args.thread, "--stop-after-first-interrupt"]
    _run_main_and_capture(args.thread, run_dir)


def cmd_resume(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    _patch_paths(run_dir)
    if args.example_config:
        _use_example_config()

    meta_path = run_dir / "meta.json"
    if meta_path.exists():
        # 只是防禦性補上：main() 不管是否要真的用到都會先讀 BRAINSTORM_TOPIC
        # 組 initial_input，沒設就會退回預設主題字串，即使目前不會被用到
        # （resume 分支不吃 initial_input）也不該留著錯的主題在記憶體/log 裡。
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("topic"):
            os.environ["BRAINSTORM_TOPIC"] = meta["topic"]

    user_input = (
        {"action": "skip"}
        if args.action == "skip"
        else {
            "action": "ask",
            "target_idea_id": args.target_idea_id,
            "question": args.question,
            "asked_by": args.asked_by,
        }
    )

    conn = sqlite3.connect(str(sg.CHECKPOINT_DB_PATH), check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    graph = sg.build_parent_graph(checkpointer)
    config = {"configurable": {"thread_id": args.thread}}
    try:
        graph.invoke(Command(resume=user_input), config)
    except Exception:  # noqa: BLE001 - 這次 invoke 本身可能跑到會議結束前的任何一個
        # 節點（peer review／masters／write_wisdom…），不是只有「續跑到下一個
        # interrupt」這麼單純。真實跑測踩過：這裡沒接住例外時，subprocess 會
        # 直接帶著未寫入的 state.json 死掉——前端看到的還是上一次 pause 的
        # 舊 payload，使用者以為還在等回應，其實 process 已經崩潰，再點一次
        # 「跳過」或「提問」只會默默開新 subprocess 再撞同一個錯、白燒一次
        # API 成本，UI 完全沒有任何錯誤訊息。跟 _run_main_and_capture 用同一招
        # 接住例外寫進 state.json，讓前端至少看得到「error」狀態。
        conn.close()
        _write_state(run_dir, status="error", message=traceback.format_exc())
        return
    conn.close()

    # 不管這次 resume 本身有沒有直接讓會議跑到底，都照樣呼叫 sg.main()——
    # 這樣才能重用它收尾的 baseline 對照／寫最終報告／save_outputs 邏輯。
    # 不會重跑整場會議是因為 sg.run_meeting 已經被 _safe_run_meeting 換掉
    # （見檔案開頭），對已經完成的 thread 會直接回傳現有 state，不會誤判成
    # 全新 thread 再用 initial_input 跑一次。
    sys.argv = ["graph.py", "--thread", args.thread, "--stop-after-first-interrupt"]
    _run_main_and_capture(args.thread, run_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 13 會議 worker：跑一段直到下一個人類介入點或結束")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("--run-dir", required=True)
    p_start.add_argument("--thread", required=True)
    p_start.add_argument("--topic", required=True)
    p_start.add_argument("--n-concept-test-interviewees", type=int, default=None)
    p_start.add_argument("--survey-n", type=int, default=None)
    p_start.add_argument("--example-config", action="store_true")
    p_start.set_defaults(func=cmd_start)

    p_resume = sub.add_parser("resume")
    p_resume.add_argument("--run-dir", required=True)
    p_resume.add_argument("--thread", required=True)
    p_resume.add_argument("--action", choices=["ask", "skip"], required=True)
    p_resume.add_argument("--target-idea-id", default=None)
    p_resume.add_argument("--question", default=None)
    p_resume.add_argument("--asked-by", default=None)
    p_resume.add_argument("--example-config", action="store_true")
    p_resume.set_defaults(func=cmd_resume)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
