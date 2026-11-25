"""Joern Code Property Graph 适配器（方案 §4.1，P1 可选）。

按需深度分析：对选定语言（Java/Python）生成 CPG，查询方法级调用链与
异常处理覆盖，补充 Tree-sitter 浅层解析无法给出的跨函数关系。

非 P0 依赖：joern 未安装时 collect() 软失败返回空（与其余采集器一致），
主管线不受影响。仅在显式调用 deep_analysis() 或 joern 可用时启用。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from common.config import Config, git_commit
from common.models import Evidence

# Joern 查询脚本：导出方法、调用边、try-catch 覆盖（JSON 行）
_QUERY_SC = """
import io.shiftleft.semanticcpg.language._
import io.shiftleft.codepropertygraph.generated.Cpg
import java.io.PrintWriter
import ujson._

@main def exec(cpgFile: String, outFile: String) = {
  val cpg = io.shiftleft.semanticcpg.utils.ExternalParser.loadCpg(cpgFile)
  val pw = new PrintWriter(outFile)
  try {
    cpg.method.filterNot(_.isExternal).foreach { m =>
      val tries = m.ast.isControlStructure.filter(_.parserTypeName.toLowerCase.contains("try")).size
      val calls = m.callOut.filterNot(_.callee.isExternal).map(_.callee.fullName).l.distinct
      pw.println(ujson.write(ujson.Obj(
        "kind" -> "method",
        "name" -> m.fullName,
        "file" -> m.filename,
        "line" -> m.lineNumber.getOrElse(0),
        "try_catch_count" -> tries,
        "calls_out" -> ujson.Arr(calls.map(ujson.Str(_)): _*)
      )))
    }
  } finally { pw.close(); cpg.close() }
}
"""


def available() -> bool:
    return shutil.which("joern") is not None and shutil.which("joern-parse") is not None


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _query_cpg(src_dir: Path, workdir: Path) -> list[dict]:
    """joern-parse 生成 CPG，再执行查询脚本导出 JSON 行。"""
    cpg = workdir / "cpg.bin"
    out = workdir / "methods.jsonl"
    p = _run(["joern-parse", str(src_dir), "--output", str(cpg)])
    if p.returncode != 0 or not cpg.exists():
        return []
    script = workdir / "query.sc"
    script.write_text(_QUERY_SC)
    p = _run(["joern", "--script", str(script),
              "--param", f"cpgFile={cpg}", "--param", f"outFile={out}"])
    if not out.exists():
        return []
    rows = []
    for line in out.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def collect(cfg: Config, profile: dict) -> dict:
    """采集方法级 CPG 证据。joern 不可用时返回空（软失败）。"""
    if not available():
        return {"evidence": [], "available": False, "methods": []}
    repo = Path(cfg.source.repo_path)
    src_root = repo / profile.get("source_root", "src")
    commit = cfg.source.get("commit") or git_commit(str(repo))
    evidence, methods = [], []
    # 仅对 Java/Python 服务做深度分析（方案 §4.1：先支持 Java+Python）
    for svc, m in (profile.get("service_mapping") or {}).items():
        sd = (m or {}).get("source_dir")
        if not sd:
            continue
        svc_dir = repo / sd
        if not svc_dir.is_dir():
            continue
        langs = {f.suffix for f in svc_dir.rglob("*") if f.is_file()}
        if not (langs & {".java", ".py"}):
            continue
        with tempfile.TemporaryDirectory(prefix="cnaa-joern-") as td:
            rows = _query_cpg(svc_dir, Path(td))
        for r in rows:
            methods.append(r | {"service": svc})
            evidence.append(Evidence(
                kind="exception_handling_coverage", layer="source", subject=svc,
                attrs={"method": r["name"], "try_catch_count": r.get("try_catch_count", 0),
                       "calls_out": r.get("calls_out", [])},
                source="joern", method="code property graph query",
                location=f"{r.get('file', '')}:{r.get('line', 0)}",
                system_id=cfg.system_id, version=commit))
    return {"evidence": evidence, "available": True, "methods": methods,
            "commit": commit}


def deep_analysis(cfg: Config, profile: dict, service: str,
                  method_pattern: str) -> dict:
    """按需深度分析：查询指定方法的调用链与异常处理（方案 §4.1 按需触发）。"""
    if not available():
        return {"available": False, "reason": "joern not installed"}
    repo = Path(cfg.source.repo_path)
    m = (profile.get("service_mapping") or {}).get(service) or {}
    sd = m.get("source_dir")
    if not sd:
        return {"available": False, "reason": f"no source_dir for {service}"}
    with tempfile.TemporaryDirectory(prefix="cnaa-joern-deep-") as td:
        rows = _query_cpg(repo / sd, Path(td))
    matched = [r for r in rows if method_pattern in r.get("name", "")]
    return {"available": True, "service": service, "pattern": method_pattern,
            "methods": matched}