#!/usr/bin/env python3
"""analyze_offline_run.py — 复盘一次 reg-auto-req-analysis-offline run 的执行过程。

设计目标：在沙箱内零依赖复盘离线需求分析 skill 的一次 run。
- 优先读磁盘产物与跨运行留痕（测试日志），不需要 docker.sock；
- 可选 --check 重跑离线 skill 自带的结构自检 check_deliverables.py；
- 网关步骤级日志（[SandboxAudit]）由 SKILL.md 走 docker.sock 脚本另行补充，本脚本不依赖。

布局探测（自动）：
- 线上模式：检测到 /mnt/user-data/outputs 目录即线上。
    run 目录   = /mnt/user-data/outputs/<run_id>/
    跨运行留痕 = /mnt/skills/过程文件/         （测试日志.md / 经验候选池.md / skill-feedback.md）
    skill 包   = /mnt/skills/public/reg-auto-req-analysis-offline/
- 本地模式：从 CWD 向上找含 过程文件/ 的目录。
    run 目录   = <过程文件根>/<id>/<run_id>/
    跨运行留痕 = <过程文件根>/过程文件/
    skill 包   = <本脚本所在>/../../reg-auto-req-analysis-offline/

用法：
  python3 analyze_offline_run.py <run_id>
  python3 analyze_offline_run.py <run_id> --check              # 重跑结构自检
  python3 analyze_offline_run.py <run_id> --json               # 机器可读 JSON
  python3 analyze_offline_run.py <item_id> --by-item          # 按工作项号定位最近 run
  python3 analyze_offline_run.py <item_id> --by-item --check
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

RUN_ID_RE = re.compile(r"^run_(\d{8})_(\d{6})_(\d+)_([0-9a-fA-F]{4,8})$")
ONLINE_OUTPUTS = "/mnt/user-data/outputs"
ONLINE_CLOSURE = "/mnt/skills/过程文件"
ONLINE_SKILL = "/mnt/skills/public/reg-auto-req-analysis-offline"

ALL_VERDICTS = {"SKIP-ANALYSIS", "NEED-INFO", "NEED-REVIEW", "AUTO-ANA", "MANUAL-REVIEW", "MANUAL-REVIEW-STOP"}
VERDICT_MEANING = {
    "SKIP-ANALYSIS": "纯联调/支持，无新增分析面，仅产出结果（skip_reason）",
    "NEED-INFO": "质控缺材料，责任方补充后重触发新 run",
    "NEED-REVIEW": "质控需现场/排期方等责任方决策或复核",
    "PASS": "质控通过，进入阶段二分析（离线 skill 中 PASS 为内部闸，不写盘）",
    "AUTO-ANA": "自动分析通过，建议 PM-AI-AUTO-ANA（已分析）",
    "MANUAL-REVIEW": "需人工复核（含功能已存在 satisfied=true 强制路径）",
    "MANUAL-REVIEW-STOP": "命中高风险类别，停止自动流程并移交人工",
}


def detect_layout() -> dict:
    """返回布局信息；mode 为 online/local/unknown。"""
    here = Path(__file__).resolve()
    local_skill = here.parents[2] / "reg-auto-req-analysis-offline"
    if os.path.isdir(ONLINE_OUTPUTS):
        return {
            "mode": "online",
            "outputs": Path(ONLINE_OUTPUTS),
            "closure": Path(ONLINE_CLOSURE),
            "skill": Path(ONLINE_SKILL),
        }
    # 本地：从 CWD 向上找 过程文件/
    cur = Path.cwd().resolve()
    root = None
    for parent in [cur, *cur.parents]:
        if (parent / "过程文件").is_dir():
            root = parent
            break
    if root is None:
        root = cur  # 兜底：在 CWD 下创建/查找
    return {
        "mode": "local",
        "outputs": root / "过程文件",
        "closure": root / "过程文件",
        "skill": local_skill,
    }


def locate_runs(layout: dict, item_id: str) -> list[Path]:
    """按工作项号列出匹配 run 目录（新→旧）。"""
    matches: list[Path] = []
    pat = re.compile(rf"^run_\d{{8}}_\d{{6}}_{re.escape(item_id)}_[0-9a-fA-F]{{4,8}}$")
    base = layout["outputs"]
    if layout["mode"] == "online":
        if base.is_dir():
            for d in base.iterdir():
                if d.is_dir() and pat.match(d.name):
                    matches.append(d)
    else:
        # 本地：过程文件/<id>/<run_id>
        for id_dir in base.iterdir() if base.is_dir() else []:
            if not id_dir.is_dir():
                continue
            for d in id_dir.iterdir():
                if d.is_dir() and pat.match(d.name):
                    matches.append(d)
    matches.sort(key=lambda p: p.name, reverse=True)
    return matches


def locate_by_run_id(layout: dict, run_id: str) -> Path | None:
    m = RUN_ID_RE.match(run_id)
    if not m:
        return None
    item_id = m.group(3)
    if layout["mode"] == "online":
        cand = layout["outputs"] / run_id
        return cand if cand.is_dir() else None
    cand = layout["outputs"] / item_id / run_id
    if cand.is_dir():
        return cand
    # 兜底：全盘慢搜
    for id_dir in (layout["outputs"].iterdir() if layout["outputs"].is_dir() else []):
        if id_dir.is_dir() and (id_dir / run_id).is_dir():
            return id_dir / run_id
    return None


def parse_iso(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def duration_str(published, generated):
    p, g = parse_iso(published), parse_iso(generated)
    if p is None or g is None:
        return "unavailable"
    secs = (g - p).total_seconds()
    if secs < 0:
        return "异常（generated < published）"
    if secs < 60:
        return f"{secs:.0f} 秒"
    return f"{secs / 60:.1f} 分钟"


def extract_test_log_entry(closure_root: Path, run_id: str) -> str | None:
    path = closure_root / "测试日志.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = f"auto-req-test-log:{run_id}"
    start = text.find(marker)
    if start == -1:
        return None
    # 从 marker 所在行首开始，到下一个同格式 marker 或文件尾
    line_start = text.rfind("\n", 0, start) + 1
    nxt = text.find("auto-req-test-log:", start + len(marker))
    end = text.rfind("\n", 0, nxt) if nxt != -1 else len(text)
    return text[line_start:end].strip()


def read_result(run_dir: Path) -> dict | None:
    for name in run_dir.iterdir():
        if name.is_file() and name.name.startswith("需求处理结果_") and name.name.endswith(".json"):
            try:
                return json.loads(name.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
    return None


def run_check(run_dir: Path, layout: dict) -> tuple[int, str]:
    checker = layout["skill"] / "scripts" / "check_deliverables.py"
    if not checker.is_file():
        return -1, f"未找到结构自检脚本：{checker}"
    try:
        proc = subprocess.run(
            [sys.executable, str(checker), str(run_dir)],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        return -1, f"重跑自检失败：{exc}"
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode, out


def build_report(run_dir: Path, layout: dict, do_check: bool) -> dict:
    result = read_result(run_dir)
    m = RUN_ID_RE.match(run_dir.name)
    item_id = m.group(3) if m else "?"
    info: dict = {
        "mode": layout["mode"],
        "run_id": run_dir.name,
        "item_id": item_id,
        "run_dir": str(run_dir),
        "files": sorted(p.name for p in run_dir.iterdir() if p.is_file()),
        "verdict": None,
        "published_at_utc": None,
        "generated_at_utc": None,
        "duration": None,
        "tags": None,
        "state_to": None,
        "knowledge_status": None,
        "warnings": [],
        "change_point_ids": [],
        "skip_reason": None,
        "next": None,
        "checklist_items": None,
        "test_log": extract_test_log_entry(layout["closure"], run_dir.name),
        "audit_record": None,
        "check": None,
    }
    ar = run_dir / f"审计记录_{run_dir.name}.md"
    if ar.is_file():
        info["audit_record"] = ar.read_text(encoding="utf-8", errors="replace").strip()

    if result:
        info["verdict"] = result.get("verdict")
        info["published_at_utc"] = result.get("published_at_utc")
        info["generated_at_utc"] = result.get("generated_at_utc")
        info["duration"] = duration_str(result.get("published_at_utc"), result.get("generated_at_utc"))
        info["tags"] = result.get("tags")
        info["state_to"] = result.get("state_to")
        info["skip_reason"] = result.get("skip_reason")
        info["next"] = result.get("next")
        info["change_point_ids"] = result.get("change_point_ids") or []
        kn = result.get("knowledge")
        if isinstance(kn, dict):
            info["knowledge_status"] = kn.get("status")
            info["warnings"] = kn.get("warnings") or []
        cl = result.get("checklist")
        if isinstance(cl, dict) and isinstance(cl.get("items"), list):
            info["checklist_items"] = len(cl["items"])

    if do_check:
        rc, out = run_check(run_dir, layout)
        info["check"] = {"returncode": rc, "output": out}

    return info


def render_text(info: dict) -> str:
    lines = []
    lines.append(f"# 离线 run 复盘 · {info['run_id']}")
    lines.append("")
    lines.append(f"- 模式：{info['mode']}（run 目录 `{info['run_dir']}`）")
    lines.append(f"- 工作项：{info['item_id']}")
    v = info["verdict"]
    lines.append(f"- 终局 verdict：{v}　{VERDICT_MEANING.get(v, '') if v else '（未读到需求处理结果，可能 run 未完成）'}")
    if info["published_at_utc"]:
        lines.append(f"- 执行窗口：{info['published_at_utc']} ～ {info['generated_at_utc']}　总耗时：{info['duration']}")
    if info["tags"] is not None:
        lines.append(f"- 建议标签：{info['tags'] or '（空，离线不执行）'}")
    if info["state_to"]:
        lines.append(f"- 建议流转：{info['state_to']}")
    if info["knowledge_status"]:
        lines.append(f"- 知识状态：{info['knowledge_status']}（来源五类见需求处理结果 knowledge.source_status）")
    if info["checklist_items"] is not None:
        lines.append(f"- 待确认清单条目数：{info['checklist_items']}")
    if info["change_point_ids"]:
        lines.append(f"- 改动点：{', '.join(info['change_point_ids'])}")
    if info["skip_reason"]:
        lines.append(f"- skip_reason：{info['skip_reason']}")
    if info["next"]:
        lines.append(f"- next（下一步）：{info['next']}")
    if info["warnings"]:
        lines.append(f"- knowledge.warnings（{len(info['warnings'])} 条）：")
        for w in info["warnings"]:
            lines.append(f"    - {w}")
    lines.append("")
    lines.append("## run 目录产物")
    if info["files"]:
        for f in info["files"]:
            lines.append(f"- `{f}`")
    else:
        lines.append("- （空目录或无产物）")
    lines.append("")
    if info["check"]:
        rc = info["check"]["returncode"]
        label = "PASS" if rc == 0 else ("FAIL" if rc == 1 else "ERROR")
        lines.append(f"## 结构自检（check_deliverables.py · {label} · rc={rc}）")
        lines.append("```")
        lines.append(info["check"]["output"] or "（无输出）")
        lines.append("```")
        lines.append("")
    if info["audit_record"]:
        lines.append("## 审计记录（run 目录叙事复盘）")
        lines.append(info["audit_record"])
        lines.append("")
    if info["test_log"]:
        lines.append("## 跨运行留痕 · 测试日志条目")
        lines.append(info["test_log"])
        lines.append("")
    lines.append("---")
    lines.append("提示：本文本已覆盖 run 目录产物 + 跨运行测试日志。若要步骤级网关动作追踪（[SandboxAudit]），")
    lines.append("另跑 fetch_gateway_logs.py 并按 SKILL.md 的离线步骤字典归类。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="复盘一次 reg-auto-req-analysis-offline run")
    ap.add_argument("target", help="run_id（run_YYYYMMDD_HHMMSS_<id>_<hex>）或工作项号（配合 --by-item）")
    ap.add_argument("--by-item", action="store_true", help="target 为工作项号，定位最近 run")
    ap.add_argument("--check", action="store_true", help="重跑离线 skill 的结构自检")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args()

    layout = detect_layout()

    run_dirs: list[Path] = []
    if args.by_item:
        run_dirs = locate_runs(layout, args.target)
        if not run_dirs:
            print(f"ERROR: 在工作项 {args.target} 下未找到任何 run 目录"
                  f"（mode={layout['mode']}，base={layout['outputs']}）", file=sys.stderr)
            return 2
        if len(run_dirs) > 1:
            print(f"# 找到 {len(run_dirs)} 个 run，默认分析最新一个：{run_dirs[0].name}", file=sys.stderr)
            for d in run_dirs[:10]:
                print(f"#   - {d.name}", file=sys.stderr)
    else:
        rd = locate_by_run_id(layout, args.target)
        if rd is None:
            print(f"ERROR: 未定位到 run 目录：{args.target}（mode={layout['mode']}）", file=sys.stderr)
            return 2
        run_dirs = [rd]

    if args.json:
        out = [build_report(d, layout, args.check) for d in run_dirs]
        print(json.dumps(out if len(out) > 1 else out[0], ensure_ascii=False, indent=2))
    else:
        for d in run_dirs:
            print(render_text(build_report(d, layout, args.check)))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
